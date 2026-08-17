"""
Beaver A2A — Agent-to-Agent Orchestration
==========================================

Gives Beaver (the orchestrator) the ability to spin up specialised
sub-agents running locally on Ollama and delegate work to them.

Each sub-agent is a full, isolated Beaver graph with its own persona,
its own model, and its own MemorySaver (ephemeral — no shared Redis
state with the orchestrator).  Results are returned as plain strings.

Sub-agent roster is configured in ``.env``:

    A2A_AGENTS=[
      {"name":"coder",     "persona":"coder",     "model":"qwen2.5-coder:7b"},
      {"name":"pentester", "persona":"pentester",  "model":"qwen2.5:7b"},
      {"name":"researcher","persona":"standard",   "model":"llama3.1:8b"},
      {"name":"seo",       "persona":"seo",        "model":"qwen2.5:7b"}
    ]

If A2A_AGENTS is not set, all locally pulled Ollama models are surfaced
as generic agents named after their model.

Tools exposed to the orchestrator
-----------------------------------
- ``a2a_delegate``   — send a task to a named sub-agent, get the result
- ``a2a_list``       — list available sub-agents and their models
- ``a2a_broadcast``  — send the same task to multiple agents concurrently

Fix log
-------
[FIX-1] Sub-agent graphs use MemorySaver (not Redis) so they are fully
        isolated and don't pollute the orchestrator's checkpoint store.
[FIX-2] Sub-agent model is set via a temporary RuntimeConfig override
        inside a threading lock so concurrent sub-agents don't race on
        the global runtime_config.
[FIX-3] Timeout guard — sub-agents that hang are cancelled after
        A2A_TIMEOUT seconds (default 120).
[FIX-4] Global asyncio.Semaphore caps concurrent Ollama model loads to
        A2A_MAX_CONCURRENT (default 2) so VRAM isn't exhausted when
        a2a_broadcast fires multiple agents simultaneously.
[FIX-5] a2a_broadcast runs agents concurrently but throttled by the
        semaphore — faster than serial, safe for local hardware.
[FIX-6] _run_sub_agent spins up its own event loop in a thread so
        RuntimeConfig mutation is fully isolated per sub-agent call.

[FIX-7] _load_agent_configs() now caches its result after the first
        successful parse.  It was previously re-reading and JSON-parsing
        A2A_AGENTS (or hitting Ollama's /api/tags) on every call to
        a2a_delegate, a2a_list, and a2a_broadcast.  The cache is a
        (raw_env_value, result) pair so changes to A2A_AGENTS in a
        long-running process are still picked up on the next call.
"""
import asyncio
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

logger = logging.getLogger("beaver")

_A2A_TIMEOUT: int     = int(os.getenv("A2A_TIMEOUT",        "120"))
_MAX_CONCURRENT: int  = int(os.getenv("A2A_MAX_CONCURRENT", "2"))

# FIX-4: semaphore throttles concurrent Ollama model loads
# Shared across all a2a calls in the process.
_ollama_sem: Optional[asyncio.Semaphore] = None   # Optional[] for Python 3.9 compat
_sem_lock = threading.Lock()

# Per-thread RuntimeConfig so concurrent sub-agents never clobber each other.
# graph.py reads _thread_local.config (when set) instead of the global singleton,
# eliminating the need to hold _thread_config_lock for the full agent run duration.
_thread_local = threading.local()


def _get_semaphore() -> asyncio.Semaphore:
    """Lazy singleton semaphore — created on first use inside the event loop."""
    global _ollama_sem
    with _sem_lock:
        if _ollama_sem is None:
            _ollama_sem = asyncio.Semaphore(_MAX_CONCURRENT)
        return _ollama_sem


# ── Sub-agent config ──────────────────────────────────────────────────────────

# FIX-7: cache is a tuple (raw_env_str, parsed_list).  Comparing raw_env_str
# on each call detects A2A_AGENTS changes without a full re-parse every time.
_agent_config_cache: Optional[Tuple[str, list]] = None   # (raw: str, configs: list)
_agent_config_lock = threading.Lock()  # guards cache write (Issue #17)


def _load_agent_configs() -> List[Dict[str, Any]]:
    """Parse A2A_AGENTS from env, or auto-discover from Ollama.

    FIX-7: result is cached by the raw env string so the JSON parse and
    Ollama /api/tags call only happen when the value actually changes.
    """
    global _agent_config_cache
    raw = os.getenv("A2A_AGENTS", "").strip()

    # Fast path: check without lock first (cache written atomically below).
    if _agent_config_cache is not None and _agent_config_cache[0] == raw:
        return _agent_config_cache[1]

    with _agent_config_lock:  # guard the write path (Issue #17)
        # Re-check inside the lock — another thread may have updated it.
        if _agent_config_cache is not None and _agent_config_cache[0] == raw:
            return _agent_config_cache[1]

        configs: List[Dict[str, Any]] = []
        if raw and raw not in ("[", "{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    configs = parsed
                else:
                    logger.warning("[A2A] A2A_AGENTS is not a JSON array — ignoring.")
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[A2A] Could not parse A2A_AGENTS: %s\n"
                    "  Tip: python-dotenv does not support multiline values. "
                    "Write the full JSON on one line in .env:\n"
                    "  A2A_AGENTS=[{\"name\":\"coder\",\"persona\":\"coder\","
                    "\"model\":\"qwen2.5-coder:7b\"}]",
                    exc,
                )
            except Exception as exc:
                logger.warning("[A2A] Could not parse A2A_AGENTS: %s", exc)
        elif raw in ("[", "{"):
            logger.debug(
                "[A2A] A2A_AGENTS looks truncated ('%s') — "
                "python-dotenv does not support multiline values. "
                "Write the JSON on one line in .env.", raw
            )

        if not configs:
            # Auto-discover: use all locally pulled Ollama models
            try:
                from agent.config import runtime_config
                models = runtime_config.list_ollama_models()
                configs = [
                    {
                        "name": m.replace(":", "_").replace(".", "_").replace("-", "_"),
                        "persona": "standard",
                        "model": m,
                    }
                    for m in models
                ]
            except Exception as exc:
                logger.warning("[A2A] Ollama auto-discover failed: %s", exc)

        _agent_config_cache = (raw, configs)  # FIX-7: store for next call
        return configs


def _get_agent_config(agent_name: str) -> Optional[Dict[str, Any]]:
    for cfg in _load_agent_configs():
        if cfg.get("name") == agent_name:
            return cfg
    return None


# ── Sub-agent runner ──────────────────────────────────────────────────────────

async def _run_sub_agent(
    task: str,
    persona: str,
    model: str,
    agent_name: str = "sub-agent",
    max_loops: int = 8,
) -> str:
    """Spin up an isolated sub-agent graph and run it against *task*.

    FIX-4: acquires the Ollama semaphore before loading the model so at most
    A2A_MAX_CONCURRENT agents compete for VRAM at any moment.

    FIX-6: model override is applied per-call inside a threading lock so
    concurrent broadcast calls don't clobber each other's config.
    """
    sem = _get_semaphore()

    logger.info(
        "[A2A] %s waiting for Ollama slot (max_concurrent=%d)",
        agent_name, _MAX_CONCURRENT,
    )

    async with sem:  # FIX-4: throttle concurrent model loads
        logger.info("[A2A] %s acquired slot — running persona=%s model=%s",
                    agent_name, persona, model)
        return await _run_isolated(task, persona, model, agent_name, max_loops)


async def _run_isolated(
    task: str,
    persona: str,
    model: str,
    agent_name: str,
    max_loops: int,
) -> str:
    """Run a sub-agent in an isolated thread with its own event loop.

    Each sub-agent stores its RuntimeConfig in _thread_local.config so that
    graph.py's _get_cached_llm() reads the correct model per thread — no
    global mutex needed, and concurrent sub-agents genuinely run in parallel.
    """
    def _thread_run() -> str:
        from agent.config import RuntimeConfig
        from agent.graph import create_beaver_graph
        from agent.state import default_state
        from langgraph.checkpoint.memory import MemorySaver

        # Private config for this sub-agent — never touches the global singleton.
        cfg = RuntimeConfig()
        cfg.model     = model
        cfg.provider  = ""       # let model_router auto-detect (ollama)
        cfg.max_loops = max_loops

        # Store in thread-local so graph.py's _get_cached_llm() picks it up
        # without reading or writing the global runtime_config at all.
        _thread_local.config = cfg

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _invoke():
                graph = create_beaver_graph(checkpointer=MemorySaver())  # FIX-1
                state = default_state(persona=persona)
                state["messages"] = [HumanMessage(content=task)]
                result = await asyncio.wait_for(
                    graph.ainvoke(
                        state,
                        config={
                            "configurable": {
                                "thread_id": f"a2a-{agent_name}-{id(task)}"
                            },
                            # [FIX-RECURSION] Sub-agents use their own private
                            # max_loops (cfg.max_loops, default 8 — see
                            # _STUCK_STREAK constants), not the global
                            # runtime_config. At the current default (8 ->
                            # 16 steps) this sits under LangGraph's own
                            # recursion_limit=25 default so it hasn't been
                            # observed to crash in practice, but leaving it
                            # unset is exactly the latent bug that DID crash
                            # the top-level agent — fix it here too rather
                            # than relying on today's specific numbers
                            # staying under the default forever.
                            "recursion_limit": (cfg.max_loops * 2) + 10,
                        },
                    ),
                    timeout=_A2A_TIMEOUT,  # FIX-3
                )
                msgs = result.get("messages", [])
                last = msgs[-1] if msgs else None
                return last.content if last else "[A2A] No response."

            try:
                return loop.run_until_complete(_invoke())
            except asyncio.TimeoutError:
                return f"[A2A] '{agent_name}' timed out after {_A2A_TIMEOUT}s."
            except Exception as exc:
                logger.warning("[A2A] Sub-agent '%s' error: %s", agent_name, exc)
                return f"[A2A] Error from '{agent_name}': {exc}"
            finally:
                loop.close()
        finally:
            # Clear thread-local so the slot doesn't linger if the thread is reused
            _thread_local.config = None

    # Run the blocking thread without blocking the orchestrator's event loop
    return await asyncio.to_thread(_thread_run)


# ── Orchestrator tools ────────────────────────────────────────────────────────

@tool
async def a2a_delegate(agent_name: str, task: str) -> str:
    """Delegate a task to a named local sub-agent and return its response.

    Use this when a task requires specialised expertise — e.g. delegate
    coding tasks to the 'coder' agent, security tasks to 'pentester'.

    Parameters
    ----------
    agent_name : str
        Name of the sub-agent as declared in A2A_AGENTS (e.g. "coder").
        Use a2a_list to see available agents.
    task : str
        Full task description for the sub-agent. Be specific — the
        sub-agent has no context from the current conversation.
    """
    cfg = _get_agent_config(agent_name)
    if not cfg:
        available = [c["name"] for c in _load_agent_configs()]
        return (
            f"[A2A] Unknown agent '{agent_name}'. "
            f"Available: {available}. Use a2a_list to see details."
        )

    persona = cfg.get("persona", "standard")
    model   = cfg.get("model",   "qwen2.5:7b")

    logger.info("[A2A] Delegating to agent='%s' persona='%s' model='%s'",
                agent_name, persona, model)

    return await _run_sub_agent(
        task=task, persona=persona, model=model, agent_name=agent_name
    )


@tool
def a2a_list() -> str:
    """List all available local sub-agents with their persona and model.

    Call this before delegating to check what agents are available.
    Returns current semaphore concurrency limit and timeout settings.
    """
    configs = _load_agent_configs()
    if not configs:
        return (
            "[A2A] No sub-agents configured.\n"
            "Set A2A_AGENTS in .env or pull Ollama models to auto-register them."
        )

    lines = [
        f"Available sub-agents  "
        f"(max_concurrent={_MAX_CONCURRENT}  timeout={_A2A_TIMEOUT}s)\n"
    ]
    for cfg in configs:
        lines.append(
            f"  • {cfg['name']:<20} "
            f"persona={cfg.get('persona','standard'):<12} "
            f"model={cfg.get('model','qwen2.5:7b')}"
        )
    return "\n".join(lines)


@tool
async def a2a_broadcast(task: str, agent_names: Optional[str] = None) -> str:
    """Send the same task to multiple sub-agents and collect all responses.

    Agents run concurrently but throttled by A2A_MAX_CONCURRENT so local
    VRAM is not exhausted. Useful for getting multiple perspectives or
    running the same analysis with different models/personas.

    Parameters
    ----------
    task : str
        Task to broadcast to all (or selected) sub-agents.
    agent_names : str, optional
        Comma-separated list of agent names to target. If omitted, all
        available agents receive the task.
    """
    configs = _load_agent_configs()
    if not configs:
        return "[A2A] No sub-agents available."

    if agent_names:
        targets = {n.strip() for n in agent_names.split(",")}
        configs = [c for c in configs if c.get("name") in targets]
        if not configs:
            return f"[A2A] None of the requested agents found: {agent_names}"

    logger.info(
        "[A2A] Broadcasting to %d agent(s) (max_concurrent=%d)",
        len(configs), _MAX_CONCURRENT,
    )

    # FIX-5: concurrent but semaphore-throttled
    async def run_one(cfg: Dict[str, Any]) -> tuple[str, str]:
        result = await _run_sub_agent(
            task=task,
            persona=cfg.get("persona", "standard"),
            model=cfg.get("model", "qwen2.5:7b"),
            agent_name=cfg["name"],
        )
        return cfg["name"], result

    pairs = await asyncio.gather(*[run_one(cfg) for cfg in configs])

    sections = [f"=== [{name}] ===\n{response}" for name, response in pairs]
    return "\n\n".join(sections)