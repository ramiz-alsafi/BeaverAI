
import logging
import os
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger("beaver")

_DB_PATH = os.getenv("SQLITE_CHECKPOINT_PATH", "./beaver_memory.sqlite")


# ── aiosqlite 0.20+ compatibility shim ────────────────────────────────────────
# langgraph-checkpoint-sqlite calls Connection.is_alive() on every checkpoint
# read/write.  aiosqlite 0.20 removed that method, raising:
#   AttributeError: 'Connection' object has no attribute 'is_alive'
# We patch it back unconditionally — safe because:
#   • It's a no-op if aiosqlite already has the method (< 0.20).
#   • The original implementation was simply `return self._connection is not None`.
try:
    import aiosqlite as _aiosqlite
    if not hasattr(_aiosqlite.Connection, "is_alive"):
        def _is_alive(self) -> bool:
            conn = getattr(self, "_connection", None)
            return conn is not None
        _aiosqlite.Connection.is_alive = _is_alive  # type: ignore[attr-defined]
        logger.debug(
            "[CHECKPOINTER] Applied aiosqlite.Connection.is_alive shim (0.20+ compat)."
        )
except Exception:
    pass  # aiosqlite not installed yet — ImportError will surface below


# ── JsonPlusSerializer.dumps / .loads shim ────────────────────────────────────
# langgraph-checkpoint-sqlite 2.x calls self.jsonplus_serde.dumps(metadata)
# and .loads(metadata) (old API) but langgraph-checkpoint 4.x removed those
# methods — JsonPlusSerializer now only exposes dumps_typed() / loads_typed().
#
# We bridge the gap by embedding the type tag as a null-byte-prefixed prefix
# so the round-trip is lossless:
#   dumps(obj)  -> b"<type>\x00<data>"
#   loads(blob) -> loads_typed(("<type>", <data>))
#
# Safe because:
#   - type tags ("msgpack", "json", "null", ...) are pure ASCII — no null bytes.
#   - It is a no-op when the methods already exist (older langgraph-checkpoint).
#   - Only metadata dicts go through jsonplus_serde; the main checkpoint payload
#     already uses dumps_typed / loads_typed correctly.
try:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer as _JPS

    if hasattr(_JPS, "dumps_typed") and not hasattr(_JPS, "dumps"):
        from typing import Any as _Any

        def _jps_dumps(self, obj: _Any) -> bytes:
            type_, data = self.dumps_typed(obj)
            return type_.encode("utf-8") + b"\x00" + data

        def _jps_loads(self, blob: bytes) -> _Any:
            sep = blob.index(b"\x00")
            return self.loads_typed((blob[:sep].decode("utf-8"), blob[sep + 1:]))

        _JPS.dumps = _jps_dumps  # type: ignore[attr-defined]
        _JPS.loads = _jps_loads  # type: ignore[attr-defined]
        logger.debug(
            "[CHECKPOINTER] Applied JsonPlusSerializer.dumps/loads shim "
            "(langgraph-checkpoint 4.x compat)."
        )
except Exception:
    pass  # langgraph not installed yet — ImportError will surface below


# ── Sync default (dev / testing) ───────────────────────────────────────────────

def get_checkpointer() -> MemorySaver:
    """Return an in-process MemorySaver.

    State is lost on restart.  Suitable for development and unit tests.
    For cross-session persistence use lifespan_checkpointer() instead.
    """
    logger.info("[CHECKPOINTER] MemorySaver (in-process, no persistence).")
    return MemorySaver()


# ── Production async context manager ──────────────────────────────────────────

@asynccontextmanager
async def lifespan_checkpointer():
    """Async context manager that yields a ready AsyncSqliteSaver.

    Priority
    --------
    1. AsyncSqliteSaver — persists graph state to ./beaver_memory.sqlite
                          across restarts; requires langgraph-checkpoint-sqlite
                          and aiosqlite.  Native async — no event-loop blocking.
    2. MemorySaver      — automatic fallback when a package is missing or the
                          SQLite file cannot be opened.  No cross-session state.

    [FIX-DOUBLE-YIELD] Acquisition and usage are now in separate try blocks.
    Previously `yield saver` sat inside the same try that catches setup
    failures — since `async with lifespan_checkpointer() as x: ...` wraps an
    entire CLI/TUI session, ANY unhandled exception raised anywhere in that
    session (a tool error, an agent bug, anything at all) got thrown back
    into this generator at the yield point, fell through to the broad
    `except Exception`, and triggered a SECOND `yield MemorySaver()` —
    illegal for an @asynccontextmanager generator. That produced
    `RuntimeError: generator didn't stop after athrow()`, burying whatever
    the real error was. Acquisition failures (import/connection) and
    consumer-side runtime errors are no longer funneled through the same
    except blocks.
    """
    saver_cm = None
    saver    = None
    try:
        # AsyncSqliteSaver lives in the .aio submodule and requires aiosqlite.
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        saver_cm = AsyncSqliteSaver.from_conn_string(_DB_PATH)
        saver    = await saver_cm.__aenter__()
    except ImportError as exc:
        logger.warning(
            "[CHECKPOINTER] AsyncSqliteSaver not available (%s) — "
            "run: pip install langgraph-checkpoint-sqlite aiosqlite",
            exc,
        )
    except AttributeError as exc:
        # aiosqlite version mismatch — AsyncSqliteSaver calls Connection.is_alive()
        # which was removed/renamed in some aiosqlite releases.
        # Run: python -m pip show aiosqlite langgraph-checkpoint-sqlite
        # then pin the matching pair.
        logger.warning(
            "[CHECKPOINTER] aiosqlite/langgraph-checkpoint-sqlite version mismatch "
            "(%s) — falling back to MemorySaver. State will NOT persist across restarts.",
            exc,
        )
    except Exception as exc:
        logger.warning(
            "[CHECKPOINTER] AsyncSqliteSaver failed (%s) — falling back to MemorySaver.",
            exc,
        )

    if saver is not None:
        logger.info("[CHECKPOINTER] AsyncSqliteSaver → %s", _DB_PATH)
        try:
            # Nothing but `yield` in this try — any exception raised by the
            # CALLER while using the checkpointer propagates straight through
            # untouched instead of being reinterpreted as a setup failure.
            yield saver
        finally:
            await saver_cm.__aexit__(None, None, None)
        return

    # ── MemorySaver fallback ───────────────────────────────────────────────────
    logger.warning("[CHECKPOINTER] Using MemorySaver — state lost on restart.")
    yield MemorySaver()