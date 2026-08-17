"""
Beaver Skills — Persona Tool Registry
======================================

PERSONA_TOOLS maps each persona name to its base set of LangChain tools.
Additional tools are loaded dynamically at runtime from three sources:

  1. Plugins  — drop .py files into plugins/ (zero config, auto-discovered)
  2. MCP      — external MCP servers declared in MCP_SERVERS env var
  3. A2A      — sub-agent delegation tools (always added to every persona)

Adding a new built-in persona
------------------------------
1. Create  prompts/personas/<n>.md        ← system prompt
2. Create  skills/<n>.py                  ← tool implementations (if needed)
3. Register it below in PERSONA_TOOLS

Adding a plugin (no core changes needed)
-----------------------------------------
Drop a .py file into plugins/ with PERSONA and TOOLS defined.
See skills/plugin_loader.py for the full spec.

Adding an MCP server
---------------------
Add an entry to MCP_SERVERS in .env:
  MCP_SERVERS=[{"name":"github","cmd":["npx","-y","@modelcontextprotocol/server-github"]}]

Adding A2A sub-agents
----------------------
Add entries to A2A_AGENTS in .env:
  A2A_AGENTS=[{"name":"coder","persona":"coder","model":"qwen2.5-coder:7b"}]
If A2A_AGENTS is empty, all pulled Ollama models are auto-registered.

Fix log
-------
[FIX-1] get_persona_tools() returns list(tools) — a shallow copy — so
        callers cannot mutate the global registry.
[FIX-2] Logging moved to module level.
[FIX-3] PERSONA_TOOLS typed as Dict[str, List[BaseTool]].
[FIX-4] Plugin tools are injected at call time so newly dropped plugins
        are picked up without restarting Beaver.
[FIX-5] MCP tools loaded async at call time via get_mcp_tools().
[FIX-6] A2A tools (a2a_delegate, a2a_list, a2a_broadcast) are appended
        to every persona so the orchestrator can always delegate.
[FIX-7] Added 'researcher' persona — dedicated deep-research persona backed
        by llama3.1:8b-instruct-q3_K_M; tools mirror 'standard' so it can
        read/write files and persist findings to long-term memory.
[FIX-8] A2A import is now wrapped in try/except at module level.
        Previously, any misconfigured or missing A2A_AGENTS value caused
        skills.a2a to raise at import time, which crashed this entire module
        and silently left every persona with zero tools.  The guard degrades
        gracefully — A2A tools are simply omitted and a clear WARNING is
        written to agent.log so you know exactly what to fix.
"""
import logging
import time
from typing import Dict, List

from langchain_core.tools import BaseTool

from skills.file_ops import append_file, get_workspace, list_directory, read_file, replace_in_file, write_file
from skills.memory_skill import (
    forget_long_term_memory,
    list_long_term_memories,
    recall_relevant_memory,
    reflect_and_store_lesson,
    store_long_term_memory,
)
from skills.os_exec import os_exec
from skills.system_info import get_environment_vars, get_system_metrics

logger = logging.getLogger("beaver")

# [FIX-CACHE] See get_persona_tools_async's docstring below for why this
# exists and why it's safe (never persisted, never touches the checkpointer).
_PERSONA_TOOLS_CACHE: Dict[str, tuple] = {}  # persona -> (monotonic_time, tools_list)
_PERSONA_TOOLS_CACHE_TTL = 2.0  # seconds


def invalidate_persona_tools_cache(persona_name: str = "") -> None:
    """Clear the short-TTL persona tool cache — call after a plugin/MCP
    reload so the change is picked up immediately instead of waiting out
    the TTL. Empty string clears every persona's cache."""
    if persona_name:
        _PERSONA_TOOLS_CACHE.pop(persona_name, None)
    else:
        _PERSONA_TOOLS_CACHE.clear()  # FIX-2

# ── A2A orchestration tools (FIX-6, FIX-8) ───────────────────────────────────
# Wrapped in try/except so a bad A2A_AGENTS value or missing Ollama never
# crashes the entire skills module.  Without this guard, ANY import error in
# skills.a2a (JSON parse failure, missing env var, network error probing Ollama)
# would propagate up here and leave every persona with an empty tool list —
# a silent total failure with no obvious error message at the call site.
try:
    from skills.a2a import a2a_broadcast, a2a_delegate, a2a_list
    _a2a_tools: List[BaseTool] = [a2a_delegate, a2a_list, a2a_broadcast]
    logger.debug("[A2A] Tools loaded: a2a_delegate, a2a_list, a2a_broadcast")
except Exception as _a2a_exc:
    logger.warning(
        "[A2A] Failed to import A2A tools — A2A delegation disabled for this session.\n"
        "      Error : %s\n"
        "      Fix   : Check A2A_AGENTS in .env is valid JSON and Ollama is reachable.\n"
        "      Check : python3 -c \"import os; from dotenv import load_dotenv; "
        "load_dotenv('.env'); print(os.getenv('A2A_AGENTS'))\"",
        _a2a_exc,
    )
    _a2a_tools = []


# ── Base persona → tool list ──────────────────────────────────────────────────
# These are the static, always-present tools for each persona.
# Plugins, MCP, and A2A tools are layered on top at runtime.

PERSONA_TOOLS: Dict[str, List[BaseTool]] = {  # FIX-3
    "standard": [
        get_workspace,
        get_system_metrics,
        get_environment_vars,
        read_file,
        write_file,
        replace_in_file,
        append_file,
        list_directory,
        store_long_term_memory,
        recall_relevant_memory,
        list_long_term_memories,
        forget_long_term_memory,
        reflect_and_store_lesson,
    ],
    "coder": [
        get_workspace,
        get_system_metrics,
        get_environment_vars,
        read_file,
        write_file,
        replace_in_file,
        append_file,
        list_directory,
        os_exec,
        store_long_term_memory,
        recall_relevant_memory,
        list_long_term_memories,
        forget_long_term_memory,
        reflect_and_store_lesson,
    ],
    "pentester": [
        get_workspace,
        get_system_metrics,
        get_environment_vars,
        os_exec,
        read_file,
        write_file,
        replace_in_file,
        list_directory,
        store_long_term_memory,
        recall_relevant_memory,
        list_long_term_memories,
        forget_long_term_memory,
        reflect_and_store_lesson,
    ],
    "seo": [
        get_workspace,
        get_system_metrics,
        get_environment_vars,
        read_file,
        write_file,
        replace_in_file,
        append_file,
        list_directory,
        store_long_term_memory,
        recall_relevant_memory,
        list_long_term_memories,
        forget_long_term_memory,
        reflect_and_store_lesson,
    ],
    "researcher": [
        get_workspace,
        get_system_metrics,
        get_environment_vars,
        read_file,
        write_file,
        replace_in_file,
        append_file,
        list_directory,
        store_long_term_memory,
        recall_relevant_memory,
        list_long_term_memories,
        forget_long_term_memory,
        reflect_and_store_lesson,
    ],
    "orchestrator": [
        get_workspace,
        get_system_metrics,
        get_environment_vars,
        read_file,
        write_file,
        replace_in_file,
        append_file,
        list_directory,
        os_exec,
        store_long_term_memory,
        recall_relevant_memory,
        list_long_term_memories,
        forget_long_term_memory,
        reflect_and_store_lesson,
    ],
    "social": [
        get_workspace,
        read_file,
        write_file,
        replace_in_file,
        append_file,
        list_directory,
        store_long_term_memory,
        recall_relevant_memory,
        list_long_term_memories,
        forget_long_term_memory,
        reflect_and_store_lesson,
    ],
}

# Backwards-compat alias
SKILL_TOOLS = PERSONA_TOOLS


def get_persona_tools(persona_name: str) -> List[BaseTool]:
    """Return the full tool list for the given persona.

    Tool resolution order:
      1. Base tools from PERSONA_TOOLS
      2. Plugin tools from plugins/ directory  (FIX-4)
      3. A2A tools for every persona           (FIX-6)

    MCP tools are loaded separately via get_persona_tools_async() because
    they require async I/O to connect to MCP server processes.  Use that
    function inside async graph nodes when MCP_SERVERS is configured.

    Returns a shallow copy so callers cannot mutate the global registry (FIX-1).
    """
    base_tools = PERSONA_TOOLS.get(persona_name)
    if base_tools is None:
        logger.warning(
            "[PERSONA] Unknown persona '%s'. Falling back to 'standard'.",
            persona_name,
        )
        base_tools = PERSONA_TOOLS["standard"]

    tools: List[BaseTool] = list(base_tools)  # FIX-1

    # FIX-4: inject plugin tools (sync — plugin loader does file I/O only)
    try:
        from skills.plugin_loader import load_plugin_tools
        plugin_tools = load_plugin_tools(persona_name)
        if plugin_tools:
            tools.extend(plugin_tools)
    except Exception as exc:
        logger.warning("[PLUGIN] Plugin load failed for persona '%s': %s", persona_name, exc)

    # FIX-6 / FIX-8: A2A tools on every persona — empty list if import failed
    tools.extend(_a2a_tools)

    return tools


async def get_persona_tools_async(persona_name: str) -> List[BaseTool]:
    """Async version of get_persona_tools() that also loads MCP tools (FIX-5).

    Use this in async graph nodes (call_model) when MCP servers are configured.
    Falls back to the sync version if MCP loading fails.

    [FIX-CACHE] call_model and execute_tools each independently called this
    once per iteration (2x per iteration, every single tool round-trip) —
    the underlying MCP connections and plugin files were already cached at
    their own layers (mcp_loader/plugin_loader), but the OUTER assembly
    (static + plugin + MCP + a2a list construction, plus a full "[MCP]
    Added N tool(s)" log line) still ran twice for identical work every
    time. This was flagged as a fix in the module docstring above
    ([FIX-4]: "tool list is forwarded via state so execute_tools reuses
    it") and state.py even added an `active_tools` field for exactly this
    — but tool objects aren't msgpack-serializable, so they can't actually
    go through the persisted/checkpointed AgentState (see the "Do NOT
    store tool objects in state" note in graph.py), and the field was
    never wired up. This short-TTL, in-process (never persisted, never
    touches the checkpointer) cache achieves the same result safely: a
    few seconds is long enough to dedupe the back-to-back call_model +
    execute_tools calls within one iteration (they're milliseconds apart
    in practice), short enough that a plugin hot-reload or `/mcp reload`
    is reflected on the very next call almost immediately.
    """
    now = time.monotonic()
    cached = _PERSONA_TOOLS_CACHE.get(persona_name)
    if cached is not None and (now - cached[0]) < _PERSONA_TOOLS_CACHE_TTL:
        return cached[1]

    tools = get_persona_tools(persona_name)

    try:
        from skills.mcp_loader import get_mcp_tools
        mcp_tools = await get_mcp_tools(persona_name)
        if mcp_tools:
            logger.info("[MCP] Added %d MCP tool(s) for persona '%s'", len(mcp_tools), persona_name)
            tools.extend(mcp_tools)
    except Exception as exc:
        logger.warning("[MCP] MCP tool load failed for persona '%s': %s", persona_name, exc)

    _PERSONA_TOOLS_CACHE[persona_name] = (now, tools)
    return tools


def get_skill_tools(skill_name: str) -> List[BaseTool]:
    """Deprecated alias for get_persona_tools()."""
    logger.warning("[DEPRECATED] get_skill_tools() → use get_persona_tools()")
    return get_persona_tools(skill_name)