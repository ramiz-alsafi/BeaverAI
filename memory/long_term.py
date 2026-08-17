
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid

import httpx

from agent.config import runtime_config

logger = logging.getLogger("beaver")

# ── Optional ChromaDB import (graceful fallback if not installed) ──────────────
# Hard module-level imports crash the entire skills stack on Windows when
# `python3` and the `pip` that installed the package point at different
# Python environments.  Wrapping here means Beaver starts (with a clear
# warning) even if chromadb isn't in the active interpreter's site-packages.
try:
    import chromadb
    import chromadb.config
    from chromadb import EmbeddingFunction, Documents, Embeddings
    _CHROMADB_AVAILABLE = True
except ImportError:
    _CHROMADB_AVAILABLE = False
    # Subscriptable stub so `class _OllamaEmbedFn(EmbeddingFunction[Documents])`
    # parses correctly without chromadb.  Plain `object` is not subscriptable,
    # which causes a TypeError at class-definition time.
    class EmbeddingFunction:  # type: ignore[no-redef]
        def __class_getitem__(cls, item): return cls
    Documents = list   # type: ignore[assignment]
    Embeddings = list  # type: ignore[assignment]
    logger.warning(
        "[LONG_TERM] chromadb not found in the current Python environment — "
        "long-term memory is DISABLED for this session.\n"
        "  Fix: pip install chromadb\n"
        "  If using a venv, make sure it is activated before running Beaver."
    )


# ── Custom embedding function (uses /api/embed — current Ollama endpoint) ──────

class _OllamaEmbedFn(EmbeddingFunction[Documents]):
    """Thin wrapper around Ollama's /api/embed endpoint.

    Uses the current endpoint and request format instead of the deprecated
    /api/embeddings used by chromadb's built-in OllamaEmbeddingFunction.
    Supports batched input — Ollama /api/embed accepts a list under 'input'.

    [FIX-KEEPALIVE] Previously sent keep_alive="0", which unloads the
    embedding model from Ollama immediately after every single call. That
    forced a full cold-load of nomic-embed-text from disk on every store
    AND every automatic recall (_inject_long_term_memories runs once per
    turn), which is almost certainly why recall was blowing past the 3s
    cap in graph.py and logging "[LONG_TERM] Memory retrieval timed out".
    Now reuses runtime_config.keep_alive — the same setting the main chat
    model already respects — so the embed model stays resident between
    calls and only ever cold-loads once.
    Also switched from a fresh httpx.post() per call to a persistent
    httpx.Client so repeated embeds reuse one TCP connection instead of
    paying handshake cost every time.

    [FIX-11] num_gpu (default 0, from runtime_config.embedding_num_gpu) is
    now sent as a per-request Ollama option, forcing the embed model onto
    CPU by default. On tight-VRAM cards, keep_alive alone wasn't enough:
    the chat model and the embed model still had to fight over the same
    GPU memory every turn (chat call, then embed call, then back to chat),
    forcing Ollama to swap one out to load the other — which is what kept
    blowing past the 3s recall timeout even after FIX-KEEPALIVE. Taking
    the embed model off the GPU entirely removes that contention; it's
    small enough that CPU inference is still fast.
    """

    def __init__(self, base_url: str, model_name: str, num_gpu: int = 0) -> None:
        self._url     = f"{base_url.rstrip('/')}/api/embed"
        self._model   = model_name
        self._num_gpu = num_gpu
        self._client  = httpx.Client(timeout=30.0)

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002
        texts = list(input) if not isinstance(input, list) else input
        try:
            resp = self._client.post(
                self._url,
                json={
                    "model": self._model,
                    "input": texts,
                    "keep_alive": runtime_config.keep_alive,  # FIX-KEEPALIVE
                    "options": {"num_gpu": self._num_gpu},    # FIX-11
                },
            )
            resp.raise_for_status()
            return resp.json()["embeddings"]
        except Exception as exc:
            # [FIX-ZEROVEC] Previously returned [[0.0]*768, ...] here so
            # Chroma's .add()/.query() call wouldn't crash — but that meant
            # a failed embed silently committed a meaningless zero-vector
            # into the permanent store (a doc that can never match any real
            # query again) instead of surfacing the failure. Raising here
            # instead lets it propagate into Chroma's call, which _sync_store
            # and _sync_search already catch and report correctly.
            logger.error("[LONG_TERM] Embedding call failed: %s", exc)
            raise


# ── LongTermMemory ─────────────────────────────────────────────────────────────

class LongTermMemory:
    """
    Semantic vector memory backed by ChromaDB + Ollama embeddings.
    Do not instantiate directly — use get_memory_db() for the lazy singleton.
    """

    def __init__(self) -> None:
        self._chroma_path: str     = runtime_config.chroma_path
        self._ollama_url: str      = runtime_config.base_url
        self._embedding_model: str = runtime_config.embedding_model
        self._embedding_num_gpu: int = runtime_config.embedding_num_gpu  # FIX-11
        self._initialized: bool    = False
        self._collection           = None  # chromadb.Collection
        self._init_db()

    def _init_db(self) -> None:
        """Open (or create) the ChromaDB persistent store and collection."""
        if not _CHROMADB_AVAILABLE:
            self._initialized = False
            return
        try:
            client = chromadb.PersistentClient(
                path=self._chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            embed_fn = _OllamaEmbedFn(
                base_url=self._ollama_url,
                model_name=self._embedding_model,
                num_gpu=self._embedding_num_gpu,  # FIX-11
            )
            self._collection = client.get_or_create_collection(
                name="long_term_memory",
                embedding_function=embed_fn,
                metadata={"hnsw:space": "cosine"},
            )
            self._embed_fn = embed_fn  # [PERF] kept so _sync_store can reuse
                                        # one embedding for both the dedup
                                        # check and the insert — see below.
            self._initialized = True
            logger.info(
                "[LONG_TERM] ChromaDB ready — path=%s  model=%s  docs=%d",
                self._chroma_path,
                self._embedding_model,
                self._collection.count(),
            )
        except Exception as exc:
            logger.error("[LONG_TERM] ChromaDB init failed: %s", exc)
            self._initialized = False

    # ── Sync helpers (always called via asyncio.to_thread) ────────────────────

    # [SCHEMA] Near-duplicate threshold for store-time dedup below. Cosine
    # similarity >= this means "this is already in memory, don't store it
    # again" rather than "this is merely related" (which is exactly what
    # search_similar's ranking is for). 0.97 is deliberately conservative —
    # two genuinely different facts about the same topic should still both
    # get stored; this only catches near-verbatim repeats (the model
    # re-storing the same fact worded almost identically across sessions,
    # which happens constantly with reflect_and_store_lesson on recurring
    # tasks).
    _DEDUP_SIMILARITY_THRESHOLD = 0.97

    def _sync_store(self, content: str, category: str) -> bool:
        """Embed and insert one memory entry. Sync — run in a thread.

        [SCHEMA] Two additions over the original bare .add():
          1. `timestamp` metadata on every entry — previously there was no
             way to know when a memory was stored, so list_memories could
             only offer an explicit warning that its ordering "is NOT
             guaranteed" instead of a real newest-first view, and
             search_similar had no way to ever prefer a fresher fact over a
             stale one with similar wording. Now it can (see _sync_list).
          2. Store-time near-duplicate dedup — the collection had no
             de-duplication at all, so calling this with near-identical
             content (very common: reflect_and_store_lesson re-learning the
             same lesson across sessions, or store_long_term_memory being
             called with slightly reworded versions of an already-known
             fact) silently bloated the collection AND crowded out the
             top-3 search_similar/recall budget with redundant near-copies
             of the same fact instead of three genuinely distinct relevant
             ones. This is the main lever on "better results" from a fixed
             k=3: fewer, more distinct candidates in the pool.
          Both together cost exactly one embedding call (the same one used
          for the dedup check is reused for the insert via `embeddings=`),
          so there's no added embedding latency versus before — the only
          new cost is one local ANN query against an already-open
          collection, which is fast (no network round trip).
        """
        try:
            count = self._collection.count()
            emb = self._embed_fn([content])[0]

            if count > 0:
                existing = self._collection.query(
                    query_embeddings=[emb], n_results=1, include=["distances"],
                )
                distances = existing.get("distances", [[]])[0]
                if distances and (1.0 - float(distances[0])) >= self._DEDUP_SIMILARITY_THRESHOLD:
                    logger.debug(
                        "[LONG_TERM] Skipped near-duplicate store (similarity=%.3f, category=%s).",
                        1.0 - float(distances[0]), category,
                    )
                    return True  # already effectively stored — not a failure

            self._collection.add(
                ids=[str(uuid.uuid4())],
                embeddings=[emb],
                documents=[content],
                metadatas=[{"category": category, "timestamp": time.time()}],
            )
            logger.debug("[LONG_TERM] Stored memory (category=%s).", category)
            return True
        except Exception as exc:
            logger.error("[LONG_TERM] Store failed: %s", exc)
            return False

    def _sync_search(self, query: str, limit: int) -> list[dict]:
        """Cosine similarity search. Sync — run in a thread.

        Returns [{content, category, similarity}] matching the original
        pgvector schema so graph.py needs no changes.
        """
        count = self._collection.count()
        if count == 0:
            return []

        n = min(limit, count)  # n_results must not exceed collection size
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=n,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            logger.error("[LONG_TERM] Search failed: %s", exc)
            return []

        docs      = results.get("documents",  [[]])[0]
        metas     = results.get("metadatas",  [[]])[0]
        distances = results.get("distances",  [[]])[0]

        output = []
        for doc, meta, dist in zip(docs, metas, distances):
            output.append({
                "content":    doc,
                "category":   (meta or {}).get("category", "general"),
                # cosine space: distance ∈ [0,2]; 0 = identical
                # mirror original pgvector: similarity = 1 − distance
                "similarity": round(1.0 - float(dist), 4),
            })
        return output

    def _sync_list(self, limit: int, category: str) -> list[dict]:
        """Return up to `limit` stored memories, newest first.

        [SCHEMA] Previously undocumented-and-unordered ("Chroma doesn't
        track insertion order natively") because there was no timestamp to
        order by. Now that _sync_store writes one, this fetches a wider
        page from Chroma (metadata filter only, cheap — no embedding
        involved) and sorts by it in Python, so browsing/audit — the
        actual use case for this method, per its own docstring — gives a
        real newest-first view instead of Chroma's arbitrary internal
        order. Entries stored before this change (no timestamp key) sort
        last, not first, so old data doesn't crowd out anything you
        actually want to audit.
        Sync — run in a thread.
        """
        try:
            where = {"category": category} if category else None
            # Overfetch, then trim after sorting — Chroma's .get() has no
            # native order_by, so "give me the newest N" means "give me
            # enough candidates to sort locally." Capped well above any
            # realistic limit= the model would pass.
            fetch_n = max(limit * 5, 200)
            results = self._collection.get(
                limit=fetch_n, where=where, include=["documents", "metadatas"],
            )
        except Exception as exc:
            logger.error("[LONG_TERM] List failed: %s", exc)
            return []

        ids   = results.get("ids", [])
        docs  = results.get("documents", [])
        metas = results.get("metadatas", [])
        rows = [
            {
                "id": _id,
                "content": doc,
                "category": (meta or {}).get("category", "general"),
                "_ts": (meta or {}).get("timestamp", 0.0),  # missing → sorts last
            }
            for _id, doc, meta in zip(ids, docs, metas)
        ]
        rows.sort(key=lambda r: r["_ts"], reverse=True)
        for r in rows:
            del r["_ts"]
        return rows[:limit]

    def _sync_delete(self, memory_id: str) -> bool:
        """Delete one memory by its exact id (as returned by list_memories).
        Sync — run in a thread."""
        try:
            existing = self._collection.get(ids=[memory_id])
            if not existing.get("ids"):
                return False
            self._collection.delete(ids=[memory_id])
            return True
        except Exception as exc:
            logger.error("[LONG_TERM] Delete failed: %s", exc)
            return False

    # ── Public async API (signatures unchanged from pgvector version) ──────────

    async def store_memory(self, content: str, category: str = "general") -> bool:
        """Embed and persist a memory. Returns True on success."""
        if not self._initialized:
            logger.warning("[LONG_TERM] Not ready — skipping store.")
            return False
        return await asyncio.to_thread(self._sync_store, content, category)

    async def search_similar(self, query: str, limit: int = 3) -> list[dict]:
        """Cosine similarity search against stored memories."""
        if not self._initialized:
            logger.warning("[LONG_TERM] Not ready — skipping search.")
            return []
        return await asyncio.to_thread(self._sync_search, query, limit)

    async def list_memories(self, limit: int = 20, category: str = "") -> list[dict]:
        """Browse stored memories directly (not similarity-ranked) — the only
        way to see what's actually in long-term memory without already
        knowing a query that would surface it via search_similar."""
        if not self._initialized:
            logger.warning("[LONG_TERM] Not ready — skipping list.")
            return []
        return await asyncio.to_thread(self._sync_list, limit, category)

    async def delete_memory(self, memory_id: str) -> bool:
        """Permanently delete one memory by id. Returns False if the id
        doesn't exist (not an error — already gone, or was never real)."""
        if not self._initialized:
            logger.warning("[LONG_TERM] Not ready — skipping delete.")
            return False
        return await asyncio.to_thread(self._sync_delete, memory_id)


# ── Lazy thread-safe singleton ─────────────────────────────────────────────────

_memory_db: LongTermMemory | None = None
_memory_db_lock = threading.Lock()


def get_memory_db() -> LongTermMemory:
    """Return the singleton LongTermMemory, initializing on first call.

    Double-checked locking: safe for concurrent calls from A2A sub-agent
    threads; only one _init_db() ever runs.
    """
    global _memory_db
    if _memory_db is None:
        with _memory_db_lock:
            if _memory_db is None:
                _memory_db = LongTermMemory()
    return _memory_db