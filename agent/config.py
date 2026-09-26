import contextvars
import logging
import os
import platform
import getpass
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

logger = logging.getLogger("beaver")  # FIX-5

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR             = Path(__file__).resolve().parent.parent
PROMPTS_DIR          = BASE_DIR / "prompts"
PERSONAS_PROMPTS_DIR = PROMPTS_DIR / "personas"
SHARED_PROMPTS_DIR   = PROMPTS_DIR / "shared"

load_dotenv(BASE_DIR / ".env")

# [FIX-PROMPT-CACHE] Keyed by ("system", None) | ("persona", name) | ("shared", name).
# Prompt files are edit-then-restart, same as tool_manuals/ (see graph.py's
# _load_tool_manual) — no runtime hot-reload path exists for prompts/ the
# way one does for plugins/ and MCP_SERVERS, so caching them for the life
# of the process is safe and removes a disk read from every single
# reasoning-loop iteration. Shared module state (not per-instance) since
# RuntimeConfig itself is effectively a singleton per process/session and
# the prompt files on disk don't vary by session.
_PROMPT_CACHE: dict = {}


# ── RuntimeConfig ──────────────────────────────────────────────────────────────
class RuntimeConfig:
    def __init__(self) -> None:

        # ── Model ──────────────────────────────────────────────────────────────
        # provider: explicit override; model_router auto-detects when empty.
        # Set MODEL_PROVIDER=ollama (or "local") to force local inference.
        self.provider: str      = os.getenv("MODEL_PROVIDER",      "")
        self.model: str         = os.getenv("MODEL_NAME",          "qwen2.5:7b")
        self.temperature: float = float(os.getenv("MODEL_TEMPERATURE", "0.0"))
        self.base_url: str      = os.getenv("OLLAMA_BASE_URL",     "http://localhost:11434").rstrip("/")
        self.num_ctx: int       = int(os.getenv("OLLAMA_NUM_CTX",  "16384"))  # was 8192 — 4096 is faster for 7b models; set 8192 in .env if you have >=16GB VRAM

        # ── GPU offload tuning (FIX-7) ───────────────────────────────────────────
        # num_gpu    : layers to force onto GPU. -1 = leave unset, let Ollama's
        #              own VRAM estimate decide (safest on small cards). Try a
        #              positive number (e.g. number of layers in your model, or
        #              a high number like 99 to mean "all") only if you have
        #              headroom to spare — forcing too many onto a small card
        #              OOMs instead of falling back gracefully.
        # num_thread : CPU threads for whatever doesn't fit on GPU. Defaults to
        #              all logical cores.
        # num_batch  : prompt batch size. Lower uses less VRAM, leaving more
        #              room for layers to sit on GPU instead of CPU.
        # keep_alive : how long the model stays resident after a call before
        #              Ollama unloads it (e.g. "5m", "30m", "-1" for forever).
        #              A model that gets unloaded between agent turns pays the
        #              full reload + re-estimate cost every time.
        # low_vram   : Ollama's more conservative memory-management mode —
        #              helps on very tight VRAM (<=4-6GB), can hurt throughput
        #              on cards with room to spare.
        self.num_gpu: int       = int(os.getenv("OLLAMA_NUM_GPU", "-1"))
        self.num_thread: int    = int(os.getenv("OLLAMA_NUM_THREAD", str(os.cpu_count() or 4)))
        self.num_batch: int     = int(os.getenv("OLLAMA_NUM_BATCH", "256"))
        self.keep_alive: str    = os.getenv("OLLAMA_KEEP_ALIVE", "10m")
        self.low_vram: bool     = os.getenv("OLLAMA_LOW_VRAM", "false").lower() == "true"

        # ── Agent ──────────────────────────────────────────────────────────────
        self.max_loops: int        = int(os.getenv("AGENT_MAX_LOOPS",        "16"))   # was 15 — 8 prevents runaway loops on small models
        self.max_output_chars: int = int(os.getenv("AGENT_MAX_OUTPUT_CHARS", "3000"))
        self.system_prompt_path: Path = BASE_DIR / os.getenv(
            "AGENT_SYSTEM_PROMPT", "prompts/personas/system_agent.md"
        )

        # ── Runtime state (mutated at runtime, not from env) ───────────────────
        self.persona: str     = "standard"
        self.scope: list[str] = ["127.0.0.1", "localhost", "192.168.0.0/16", "10.0.0.0/8"]

        # ── Memory — ChromaDB (replaces Redis + PostgreSQL/pgvector) ──────────
        # chroma_path     : persist dir for ChromaDB — no Docker needed
        # embedding_model : Ollama model used to generate long-term memory vectors
        # [FIX-9] Default anchored to BASE_DIR instead of "./chroma_data".
        # A relative default resolves against the process's current working
        # directory, not the project folder — launch Beaver from a shortcut,
        # a different terminal cwd, or the TUI's own working dir and you get
        # a brand-new empty store each time, silently "losing" everything
        # written under the previous cwd. CHROMA_PATH in .env still wins if
        # set (it's an explicit absolute Windows path there already).
        self.chroma_path: str     = os.getenv("CHROMA_PATH",     str(BASE_DIR / "chroma_data"))
        self.embedding_model: str = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
        # [FIX-11] embedding_num_gpu : GPU layers Ollama should offload for
        # the EMBEDDING model specifically — independent of the main chat
        # model's num_gpu. Defaults to 0 (CPU-only) because on tight-VRAM
        # cards (e.g. a 4GB laptop 3050) the chat model and the embed model
        # both wanting GPU residency at once forces Ollama to swap one out
        # to load the other on every turn (chat call, then embed call, back
        # to chat) — that swap is what was blowing past the 3s recall
        # timeout in agent/graph.py ("[LONG_TERM] Memory retrieval timed
        # out"). nomic-embed-text is small enough that CPU inference is
        # fast regardless; forcing it off the GPU means it never competes
        # with the chat model for VRAM. Set EMBEDDING_NUM_GPU=-1 in .env to
        # let Ollama auto-decide instead (matches OLLAMA_NUM_GPU's own
        # semantics) if you have VRAM to spare for both.
        self.embedding_num_gpu: int = int(os.getenv("EMBEDDING_NUM_GPU", "0"))

        # Tracing is handled locally by LocalAgentTracer → beaver_traces.log
        # No Langfuse, no external services required.

    # ── Property aliases (FIX-1, FIX-2) ───────────────────────────────────────
    # graph.py caches the LLM by (model_name, model_provider).
    # main.py reads active_persona.
    # These properties keep the internal short names (self.model, self.provider,
    # self.persona) while exposing the fully-qualified names the rest of the
    # codebase expects.

    @property
    def model_name(self) -> str:
        """Alias for self.model — used by graph.py LLM cache."""
        return self.model

    @model_name.setter
    def model_name(self, value: str) -> None:
        self.model = value

    @property
    def model_provider(self) -> str:
        """Alias for self.provider — used by graph.py LLM cache."""
        return self.provider

    @model_provider.setter
    def model_provider(self, value: str) -> None:
        self.provider = value

    @property
    def active_persona(self) -> str:
        """Alias for self.persona — used by main.py."""
        return self.persona

    @active_persona.setter
    def active_persona(self, value: str) -> None:
        self.persona = value

    @property
    def recursion_limit(self) -> int:
        """LangGraph step ceiling for graph.astream_events()/.ainvoke() calls.

        [FIX-RECURSION] The graph is a simple agent <-> tools loop (see
        create_beaver_graph), so each reasoning iteration costs up to 2
        LangGraph "steps" (one call_model + one execute_tools), except the
        final iteration which may end on call_model alone. Nothing in this
        codebase ever passed recursion_limit into the graph invocation
        config, so LangGraph silently used its own hardcoded default of 25 —
        completely independent of max_loops (default 16, i.e. up to 32
        steps). Any turn that used enough tool calls to actually reach
        max_loops hit LangGraph's ceiling first and raised a raw
        GraphRecursionError instead of ever reaching the graceful
        "[AGENT HALTED]: Reached max loop limit" message call_model already
        builds for exactly this case (see agent/graph.py). Every call site
        that builds a graph invocation config (cli/ui.py, web/server.py,
        skills/a2a.py) should pass this instead of leaving recursion_limit
        unset. +4 is headroom for the halt/error messages themselves, which
        still cost a step to emit.
        """
        return (self.max_loops * 2) + 4

    # ── Scope ──────────────────────────────────────────────────────────────────
    def set_scope(self, raw_scope: "str | list[str]") -> None:
        """Update target scope. Accepts a comma-separated string or a list."""
        if isinstance(raw_scope, str):
            self.scope = [s.strip() for s in raw_scope.split(",") if s.strip()]
        elif isinstance(raw_scope, list):
            self.scope = raw_scope

    # ── System info ────────────────────────────────────────────────────────────
    def get_os_info(self) -> str:
        return f"{getpass.getuser()}@{platform.system()} {platform.release()}"

    def get_cwd(self) -> str:
        return os.getcwd()

    # ── Prompts ────────────────────────────────────────────────────────────────
    def load_system_prompt(self) -> str:
        """Load the base system prompt from disk. Falls back to a safe default.

        The disk file (system_agent.md by default) is the intended safety
        net for persona-prompt-load failures — see load_persona_prompt()'s
        caller in agent/graph.py. The hardcoded string below is the ABSOLUTE
        last resort, only reached if system_agent.md itself is also missing.
        It stays short by design (this is a "something is actually broken"
        state, not normal operation) but still carries the core tool-call
        mandate and honesty rule so the agent doesn't go completely
        uninstructed even in total prompt-loading failure.

        [FIX-PROMPT-CACHE] Cached in _PROMPT_CACHE — see load_persona_prompt
        for why.
        """
        cache_key = ("system", None)
        if cache_key in _PROMPT_CACHE:
            return _PROMPT_CACHE[cache_key]
        if self.system_prompt_path.exists():
            text = self.system_prompt_path.read_text(encoding="utf-8")
        else:
            text = (
                "You are Beaver, an autonomous local AI agent. No persona prompt "
                "could be loaded (persona: {{FAILED_PERSONA}}) and the system_agent.md "
                "safety-net prompt is also missing — tell the user this plainly, "
                "you are running in a minimally-configured fallback state.\n\n"
                "Tools bound to you are listed below even though this description "
                "is minimal — call them directly to complete tasks, never narrate "
                "what you would do instead of doing it. Never invent results, "
                "file contents, or tool output — report failures honestly.\n\n"
                "{{TOOL_LIST}}"
            )
        _PROMPT_CACHE[cache_key] = text
        return text

    def load_persona_prompt(self, persona_name: str) -> str:
        """Load a named persona prompt from prompts/personas/<persona_name>.md.

        Falls back to prompts/skills/<persona_name>.md for backwards
        compatibility while you migrate the directory.

        [FIX-PROMPT-CACHE] call_model() (agent/graph.py) calls this on
        every single reasoning-loop iteration — up to max_loops times per
        user turn, across every concurrent WebSocket session and every
        A2A sub-agent — and it was re-reading the .md file from disk every
        time. Prompt files have no hot-reload contract (unlike plugins/
        and MCP_SERVERS, which do — see skills/__init__.py and
        skills/mcp_loader.py): editing one requires a restart, same as
        tool_manuals/, which graph.py's _load_tool_manual() already
        caches. Cached here the same way. A missing-file FileNotFoundError
        is NOT cached — a persona file created after startup (or a typo
        fixed without restart) should be picked up on the very next call,
        not require yet another restart just to escape a cached failure.
        """
        cache_key = ("persona", persona_name)
        if cache_key in _PROMPT_CACHE:
            return _PROMPT_CACHE[cache_key]

        path = PERSONAS_PROMPTS_DIR / f"{persona_name}.md"
        if path.exists():
            text = path.read_text(encoding="utf-8")
            _PROMPT_CACHE[cache_key] = text
            return text

        legacy_path = PROMPTS_DIR / "skills" / f"{persona_name}.md"
        if legacy_path.exists():
            logger.warning(  # FIX-5
                "[PERSONA] Loaded '%s' from legacy prompts/skills/. "
                "Move it to prompts/personas/ to silence this warning.",
                persona_name,
            )
            text = legacy_path.read_text(encoding="utf-8")
            _PROMPT_CACHE[cache_key] = text
            return text

        raise FileNotFoundError(
            f"Persona prompt not found: {path}\n"
            f"Create prompts/personas/{persona_name}.md to define this persona."
        )

    def load_shared_prompt(self, name: str) -> str:
        """Load an optional shared prompt module from prompts/shared/<name>.md.

        Shared modules hold instructions that apply to every persona (e.g.
        language/dialect mirroring, the long-term memory persistence policy)
        so they're written once instead of duplicated into each persona
        file. Returns "" if the module doesn't exist so callers can append
        it unconditionally without checking existence themselves.

        [FIX-PROMPT-CACHE] Same reasoning as load_persona_prompt — this is
        appended to EVERY persona's prompt on EVERY reasoning-loop
        iteration (three separate files, currently), so it's the single
        hottest disk read in the whole prompt-assembly path. Cached here
        too, including the "" result for a module that doesn't exist, so
        a call site can't be fooled into re-checking the filesystem every
        turn just because a module was never created.
        """
        cache_key = ("shared", name)
        if cache_key in _PROMPT_CACHE:
            return _PROMPT_CACHE[cache_key]
        path = SHARED_PROMPTS_DIR / f"{name}.md"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        _PROMPT_CACHE[cache_key] = text
        return text

    # ── Ollama model listing (local only) ─────────────────────────────────────
    def list_ollama_models(self) -> list[str]:
        """Return names of all locally pulled Ollama models (sync)."""
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]

    async def list_ollama_models_async(self) -> list[str]:
        """Async variant — use inside async graph nodes or TUI startup."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]

    # ── Model switching ────────────────────────────────────────────────────────
    def set_model(self, model_name: str, provider: str = "", validate_ollama: bool = False) -> None:
        """Switch the active model at runtime.

        Parameters
        ----------
        model_name      : e.g. "qwen2.5:7b", "claude-sonnet-4-6", "gpt-4o"
        provider        : explicit provider override (auto-detected when empty)
        validate_ollama : if True and the resolved provider is ollama/local,
                          verifies the model is pulled locally before switching.
        """
        if provider:
            self.provider = provider

        # FIX-3: use the resolved provider (new value if given, existing if not)
        # so that set_model("gpt-4o", validate_ollama=True) doesn't incorrectly
        # look for gpt-4o in Ollama when no provider override was passed.
        resolved_provider = provider or self.provider
        if validate_ollama and resolved_provider in ("ollama", "local", ""):
            available = self.list_ollama_models()
            if model_name not in available:
                raise ValueError(
                    f"Model '{model_name}' not found in Ollama.\n"
                    f"Run: ollama pull {model_name}\n"
                    f"Available: {available}"
                )

        self.model = model_name

    # ── Persona switching ──────────────────────────────────────────────────────
    def set_persona(self, persona_name: str) -> None:
        """Switch the active persona. Raises ValueError for unregistered names."""
        from skills import PERSONA_TOOLS  # late import — avoids circular at module load
        if persona_name not in PERSONA_TOOLS:
            raise ValueError(
                f"Persona '{persona_name}' is not registered.\n"
                f"Available: {list(PERSONA_TOOLS.keys())}"
            )
        self.persona = persona_name

    # ── Backwards compatibility shims (deprecated) ────────────────────────────
    @property
    def skill(self) -> str:
        """Deprecated — use runtime_config.persona instead."""
        logger.warning("[DEPRECATED] runtime_config.skill → use runtime_config.persona")  # FIX-5
        return self.persona

    @skill.setter
    def skill(self, value: str) -> None:
        logger.warning("[DEPRECATED] runtime_config.skill → use runtime_config.persona")  # FIX-5
        self.persona = value

    def set_skill(self, skill_name: str) -> None:
        """Deprecated — use set_persona() instead."""
        logger.warning("[DEPRECATED] set_skill() → use set_persona()")  # FIX-5
        self.set_persona(skill_name)

    def load_skill_prompt(self, skill_name: str) -> str:
        """Deprecated — use load_persona_prompt() instead."""
        logger.warning("[DEPRECATED] load_skill_prompt() → use load_persona_prompt()")  # FIX-5
        return self.load_persona_prompt(skill_name)


# ── Singleton ──────────────────────────────────────────────────────────────────
runtime_config = RuntimeConfig()


# ── Session-scoped config (FIX-10) ──────────────────────────────────────────────
# Set by web/server.py once per WebSocket connection. Unset (None) in every
# other entry point (CLI, TUI, one-shot, A2A sub-agent threads), in which case
# get_active_config() transparently returns the global singleton above.
_session_config_var: "contextvars.ContextVar[Optional[RuntimeConfig]]" = contextvars.ContextVar(
    "beaver_session_config", default=None
)


def get_active_config() -> RuntimeConfig:
    """Return the current asyncio-task's RuntimeConfig, or the global singleton.

    Safe to call from anywhere — CLI/TUI code that never sets a session
    config gets `runtime_config` back, unchanged from before FIX-10.
    """
    cfg = _session_config_var.get()
    return cfg if cfg is not None else runtime_config


def set_session_config(cfg: Optional[RuntimeConfig]) -> "contextvars.Token":
    """Bind *cfg* as the active config for the current context.

    Call once near the top of a connection/session handler, before any
    `asyncio.create_task()` calls for that session — child tasks copy the
    context at creation time, so everything spawned afterwards (including
    each turn's task) sees this config via get_active_config(). Returns the
    Token from ContextVar.set() in case the caller wants to .reset() it.
    """
    return _session_config_var.set(cfg)
