"""
LangGraph Workflow Engine for Beaver Agent

STARTUP CHANGE (required)
--------------------------
The graph is no longer compiled at import time.  You must now build it
inside your async entry point after the checkpointer context is open:

    from memory.checkpointer import lifespan_checkpointer
    from agent.graph import create_beaver_graph

    async def main():
        async with lifespan_checkpointer() as checkpointer:
            beaver_graph = create_beaver_graph(checkpointer)
            await run_app(beaver_graph)

This is required because AsyncSqliteSaver holds an open aiosqlite connection
that must remain live for the entire lifetime of the compiled graph.
Compiling at import time (the old pattern) raced against the event loop
and passed a context-manager object instead of a live saver instance.

Model communication
-------------------
- graph.py no longer imports or instantiates any specific LLM class.
- get_llm() from agent.model_router handles provider detection and
  instantiation. Switch providers by changing MODEL_NAME / MODEL_PROVIDER
  in .env or calling runtime_config.set_model() — no graph changes needed.
- get_llm() is now cached at module level via _LLM_CACHE to avoid
  re-instantiating the client on every LLM call.

Memory integrations
-------------------
[FIX-DOC-MEMORY] This section previously described a Redis short-term
buffer + Postgres/pgvector long-term store. Neither exists in this
codebase anymore — see agent/config.py's own "Memory — ChromaDB (replaces
Redis + PostgreSQL/pgvector)" comment. Corrected to match what's actually
implemented:
- Short-term / conversation history: persisted entirely by the LangGraph
  checkpointer (memory/checkpointer.py — AsyncSqliteSaver, falling back to
  in-process MemorySaver if unavailable), keyed by thread_id. There is no
  separate short-term buffer — RunnableConfig's thread_id is used for the
  checkpointer's own per-thread state and for structured_notes.py's
  per-thread note isolation (active_thread_id_var), not for any Redis key.
- Long-term RAG: search_similar() (memory/long_term.py, ChromaDB +
  Ollama's /api/embed) results are appended to the system prompt before
  every LLM call. If ChromaDB is unavailable (package not installed) the
  call proceeds without memories — never blocks. See _inject_long_term_memories.

Personas
--------
- ``active_persona`` in AgentState selects which prompt + toolset to load.
- Persona prompts live in prompts/personas/<name>.md
- Persona tool sets are registered in skills/__init__.py under PERSONA_TOOLS.

Fix log
-------
[FIX-1] System prompt is now always stripped and re-injected so long-term
        memory enrichment applies on every turn, not just turn 1.
[FIX-2] get_llm() is cached in _LLM_CACHE; only rebuilt when the model
        name/provider changes, avoiding per-call client re-instantiation.
[FIX-3] Replaced X | Y union syntax with Optional[X] for Python 3.9 compat.
[FIX-4] get_persona_tools() called once per turn in call_model; the tool
        list is forwarded via state so execute_tools reuses it instead of
        calling get_persona_tools() again.
[FIX-5] Hardcoded os_exec special-case replaced with a streaming-capable
        tool interface: tools that expose an ``astream_invoke`` coroutine
        receive the tui_bus callback automatically.

[FIX-13] {{TOOL_LIST}} in persona prompts is now actually substituted with
        the persona's real, resolved tool list (name + first line of each
        tool's description). Previously nothing in the codebase replaced
        this token, so every persona referencing it sent the model the
        literal string "{{TOOL_LIST}}" on every call. Tool resolution was
        moved earlier in call_model (before prompt loading) so it's
        available at substitution time.

[FIX-19] Tool-manual correction system. Two changes:
        (a) The non-streaming tool dispatch path (tool.ainvoke) had NO
        exception handling — any raised error (dead MCP subprocess,
        connection refused, etc.) propagated up and crashed the entire
        turn instead of being reported back to the model as a normal tool
        failure. Now wrapped in try/except, matching the streaming path's
        existing behavior.
        (b) On any detected tool failure, the matching
        tool_manuals/<tool_name>.md file (if one exists) is appended to
        that ToolMessage's content as corrective guidance. No separate
        retry loop was added — the graph's existing tools → agent edge
        already gives the model a fresh generation to try again, now with
        the correct usage in front of it instead of just a bare error
        string. See tool_manuals/ for the per-tool files.

[FIX-20] Renamed skill_docs/ -> tool_manuals/ (matches how the team refers
        to these: "manuals," not "skills" — "skills" already means the
        skills/ Python package that implements the tools). Also extended
        failure detection and manual lookup:
        (a) _tool_result_failed() previously only caught "Error:"-prefixed
        strings and embedded MCP errors — it was blind to os_exec's own
        failure format, which never says "Error:" at all (a failed shell
        command still returns normal-looking "--- STDOUT/STDERR ---" text
        ending in "[Exit code: N]"). Non-zero exit codes are now detected.
        (b) os_exec failures are really about the shell UTILITY that was
        run (nmap, hydra, ...), not os_exec itself. _extract_shell_utility()
        pulls the utility name from the "command" arg, and if
        tool_manuals/<utility>.md exists, it's injected alongside
        os_exec.md's general guidance — so a failed `nmap -sV ...` call can
        get nmap-specific correction, not just generic os_exec advice.

[FIX-14] Shared prompt modules (prompts/shared/*.md) are now appended to
        every persona's system prompt after {{TOOL_LIST}} substitution.
        Two modules ship by default:
        (a) memory_policy.md — every persona has store_long_term_memory
        bound (see skills/__init__.py PERSONA_TOOLS), but before this fix
        only prompts/personas/researcher.md ever instructed the model to
        call it. Every other persona could recall memories but had no
        reason to ever write one, which is why long-term memory looked
        wired up end-to-end yet the store stayed empty.
        (b) language_dialect.md — automatic language/dialect detection and
        mirroring (currently: Egyptian Arabic vs. MSA vs. English), so this
        doesn't need to be duplicated into every persona file by hand.
        Add more prompts/shared/<name>.md files and list them in the
        `_shared_name in (...)` tuple in call_model() to extend this.

[FIX-6] _LLM_CACHE is now protected by a threading.Lock so concurrent
        A2A sub-agents (which run in separate threads) cannot race on
        the dict: one thread calling .clear() while another is mid-read
        would silently return a wrong or stale LLM client.

[FIX-7] _inject_long_term_memories is now guarded by asyncio.wait_for
        (3 s timeout).  A slow Postgres query or embedding call used to
        block the entire agent turn with no escape hatch.

[FIX-8] Streaming tools (astream_invoke) now manually fire on_tool_start
        and on_tool_end on all callbacks so Langfuse sees their spans.
        Previously astream_invoke bypassed LangChain's normal ainvoke()
        path entirely, making streaming tool calls invisible in Langfuse.

[FIX-21] _extract_json_tool_call's marker list has always recognized
        single-quoted tool-call starts ("{'tool'", "{'thought'") but
        nothing downstream ever actually parsed them: the quote-tracking
        in the brace matcher only toggled on '"', and the final parse was
        a bare json.loads() call, which rejects single-quoted JSON
        outright. Any local model that emitted {'tool': 'x', ...} instead
        of double-quoted JSON silently got treated as narration/blank and
        cost a full retry turn for a call that was actually well-formed.
        Added _lenient_json_loads() (strict JSON -> Python-literal
        normalization -> ast.literal_eval, in that order) and made the
        brace matcher's in_str tracking quote-aware so it doesn't
        miscount depth inside single-quoted string values either.

[FIX-ROUTER-2] _get_cached_llm() (and the get_llm() builder it calls in
        model_router.py) can raise ImportError (SDK not installed) or
        EnvironmentError (API key missing) the first time a given
        (model, provider, temperature) is requested — the exact failure a
        user hits when switching MODEL_PROVIDER to a new stack. This call
        used to sit outside call_model's try/except, so it crashed the
        whole turn with a raw unhandled exception instead of the same
        clean [MODEL ERROR] message the ainvoke()-failure path already
        produces. Now wrapped in its own try/except (no cache eviction
        needed — construction failures never get inserted into
        _LLM_CACHE in the first place). See also model_router.py's
        [FIX-ROUTER-1]: real Ollama library tags like "gpt-oss:20b" and
        "llama3-gradient:8b" were being name-prefix-matched to cloud
        providers (openai / groq) by _detect_provider before this
        propagation bug would even trigger for them.

[FIX-SCOPE-DOMAIN] The scope gate below only ever checked a tool_args key
        literally named "command" (os_exec), and only for raw IP
        addresses found inside it (agent/tools.py's is_in_scope). But
        pentester.md's own "Scope & authorization" section tells the
        model "every command is checked against target_scope... If a
        tool call against a domain/public IP comes back [SCOPE BLOCK],
        that's a configuration gap" — a promise that was simply false for
        every domain/URL-based tool: subdomain_enum, subdomain_bruteforce
        (domain=...), http_get/post/head/check, the http_session_* tools,
        and probe_payloads (url=..., urls=...) had zero scope enforcement.
        A pentester-mode agent could DNS-bruteforce or send SQLi/XSS probe
        payloads against a completely unauthorized target while being told
        by its own system prompt that the runtime "has its back." Added
        agent/tools.py's is_target_in_scope() (hostname exact/subdomain
        matching against domain scope entries, falling back to IP/CIDR
        matching for a literal-IP host) and wired it in below — gated to
        active_persona == "pentester" specifically, since http_probe.py's
        http_get/post/head/check are also bound to coder/seo/researcher/
        standard/orchestrator for ordinary internet API use, and
        target_scope's default (localhost + private ranges) has no
        domain entries — checking this universally would have silently
        broken normal HTTP calls for every persona not running a pentest
        engagement.
"""
import asyncio
import contextvars
import json
import re
import threading
import uuid as _uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import merge_configs
from langchain_core.tools import BaseTool
from langgraph.graph import END, StateGraph

from agent.config import runtime_config
from agent.model_router import get_llm
from agent.state import AgentState
from agent.telemetry import get_callbacks, logger
from agent.tools import is_in_scope, is_target_in_scope
from memory.compactor import compact_messages
from skills import get_persona_tools, get_persona_tools_async
from agent.bus import tui_bus


# ── LLM cache (FIX-2, FIX-6) ─────────────────────────────────────────────────
# Keyed by (model_name, provider) so the client is rebuilt only when the
# active model actually changes, not on every call_model invocation.
#
# FIX-6: protected by a threading.Lock so A2A sub-agents running in separate
# threads can call _get_cached_llm() concurrently without racing on the dict.

_LLM_CACHE: Dict[Tuple[str, str, float], Any] = {}   # (model, provider, temperature) — FIX-11
_LLM_CACHE_LOCK = threading.Lock()  # FIX-6


def _get_active_config() -> Any:
    """Resolve the RuntimeConfig for whoever is currently running.

    Resolution order (FIX-10):
      1. Per-session config (agent.config._session_config_var) — set by
         web/server.py once per WebSocket connection via contextvars, so
         concurrent browser tabs on the same event-loop thread each see
         their own model/persona/max_loops/scope.
      2. Per-thread config (skills.a2a._thread_local) — set by A2A
         sub-agents, which run in their own OS thread + event loop and so
         are invisible to contextvars set on the orchestrator's thread.
      3. The global runtime_config singleton — CLI/TUI/one-shot runs, which
         never touch either of the above.
    """
    from agent.config import get_active_config as _get_session_cfg
    cfg = _get_session_cfg()
    if cfg is not runtime_config:
        return cfg
    try:
        from skills.a2a import _thread_local
        tcfg = getattr(_thread_local, "config", None)
        if tcfg is not None:
            return tcfg
    except ImportError:
        pass
    return runtime_config


def _evict_cached_llm() -> None:
    """Drop the current (model, provider, temperature) entry from the LLM cache.

    Called after an LLM call raises — e.g. a malformed generation causing
    ChatOllama's response parser to throw (ResponseError: XML syntax error,
    status code -1). The exception can leave the client's underlying HTTP
    connection in a bad state; because the client is cached and normally
    only rebuilt when the model/provider/temperature changes, every
    subsequent turn would keep reusing the same poisoned connection —
    which can hang indefinitely with no further error ever surfacing
    (looks like the whole app "froze"). Evicting forces a clean client on
    the next call. [FIX-11] Key includes temperature now — see
    _get_cached_llm for why.
    """
    cfg = _get_active_config()
    key = (cfg.model_name, cfg.model_provider, cfg.temperature)
    with _LLM_CACHE_LOCK:
        _LLM_CACHE.pop(key, None)


def _get_cached_llm() -> Any:
    """Return a cached LLM instance, rebuilding only when (model, provider,
    temperature) changes.

    [FIX-11] Cache key now includes temperature, not just (model, provider).
    Temperature is a per-session LIVE field (agent.config.LIVE_FIELDS) —
    two concurrent WebSocket sessions can legitimately run the *same*
    model at *different* temperatures. With temperature left out of the
    key, the second session to hit this model/provider pair would
    silently reuse the first session's cached client (and its baked-in
    temperature) instead of getting its own — a cross-session leak even
    though set_session_config() correctly isolated everything else.
    num_ctx/base_url/num_gpu/etc. stay out of the key on purpose: those
    are RESTART_FIELDS (agent.config), deliberately process-wide because
    they describe how one shared local Ollama daemon loads a model onto
    one physical GPU — not a per-request concept, so every session
    reads the same value already and there's nothing to key on.

    Uses the per-thread config when inside an A2A sub-agent so concurrent agents
    each get the right LLM without touching a global lock.  The cache retains one
    entry per (model, provider, temperature) triple — safe for multi-agent use
    because different sub-agents/sessions use different keys; no .clear() is
    needed (and clearing would evict a sibling session's live LLM mid-run).
    """
    cfg = _get_active_config()
    key = (cfg.model_name, cfg.model_provider, cfg.temperature)
    with _LLM_CACHE_LOCK:  # FIX-6: guard dict writes
        if key not in _LLM_CACHE:
            # Build LLM from this config's fields directly so model_router
            # doesn't fall back to reading the global runtime_config singleton.
            _LLM_CACHE[key] = get_llm(
                model=cfg.model,
                provider=cfg.provider or None,
                temperature=cfg.temperature,
                base_url=cfg.base_url,
                num_ctx=cfg.num_ctx,
            )
        return _LLM_CACHE[key]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _embedding_query(content) -> str:
    """Extract a short, embeddable string from a message's content.

    Handles multimodal content (list of dicts) and truncates to 512 chars —
    the effective range for nomic-embed-text.  Keeps every embedding call
    under 100 ms and avoids TypeError when content is not a plain string.
    """
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        content = " ".join(parts)
    return str(content)[:512]


def _thread_id(config: Optional[RunnableConfig]) -> str:  # FIX-3
    """Extract thread_id from LangGraph config, defaulting to 'main'."""
    if config and isinstance(config, dict):
        return config.get("configurable", {}).get("thread_id", "main")
    return "main"


# [FIX-NOTES-THREAD] plugins/structured_notes.py's own docstring claims
# thread_id is "read from the BEAVER_THREAD_ID environment variable, which
# graph.py sets before each node call" — but nothing in the codebase ever
# wrote to os.environ (confirmed: zero `os.environ[...]` assignments
# anywhere in the repo). Every notes_write/notes_read/notes_append call,
# from every persona, every conversation, every concurrent A2A sub-agent,
# fell back to the literal string "default" and shared ONE folder
# (./notes/default/) — the exact clobbering the thread-isolation feature
# was written to prevent, silently defeated since day one.
# An env var was also the wrong primitive even if it had been set: it's
# process-global, so concurrent asyncio tasks (multiple simultaneous
# websocket sessions in the same process) would stomp each other's value
# mid-request. Using a ContextVar instead — the same pattern already
# established for os_exec.py's _session_runner_var — is correct for
# concurrent async tool calls the same way env vars never could be.
active_thread_id_var: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "active_thread_id", default="default"
)


async def _auto_store_halt_lesson(state: "AgentState", reason: str) -> None:
    """Deterministic self-learning: fires from the graph itself the moment
    a hard-stop condition is hit (max_loops OR the consecutive-failure
    streak), regardless of whether the model would ever have thought to
    call reflect_and_store_lesson voluntarily. A stuck/failed task always
    leaves a trace for next time instead of silently repeating itself in a
    future session with no memory of having failed before.

    Best-effort — mirrors _inject_long_term_memories' failure handling:
    never raises, never blocks the halt message from returning to the user.
    """
    try:
        from memory.long_term import get_memory_db

        persona = state.get("active_persona", "standard")
        messages = state.get("messages", [])
        # Filter out internally-injected HumanMessages (the stuck-streak nudge
        # from execute_tools, the narration/blank-retry enforcement from
        # call_model) — otherwise the "task" below can end up being one of
        # those instead of what the person actually asked for.
        _INTERNAL_MARKERS = ("[STUCK PATTERN DETECTED]", "TOOL CALL REQUIRED")
        human_msgs = [
            m for m in messages
            if isinstance(m, HumanMessage)
            and not str(m.content).startswith(_INTERNAL_MARKERS)
        ]
        task = human_msgs[-1].content if human_msgs else "(no user message found)"
        if isinstance(task, list):  # multimodal content — text part only
            task = next(
                (p.get("text", "") for p in task if isinstance(p, dict) and p.get("type") == "text"),
                "",
            )
        task = str(task)[:300]

        failed = state.get("failed_tool_calls", {}) or {}
        if failed:
            # Aggregate by tool name too, not just by exact fingerprint — a
            # varied-args stuck streak (the case this whole mechanism exists
            # to catch) produces many fingerprints each with count 1, which
            # read as a confusing "toolX failed 1x; toolX failed 1x; ..."
            # if summarized per-fingerprint alone.
            by_tool: Dict[str, int] = {}
            for k, v in failed.items():
                tool_name_part = k.split("::")[0]
                by_tool[tool_name_part] = by_tool.get(tool_name_part, 0) + v
            top = sorted(by_tool.items(), key=lambda kv: -kv[1])[:3]
            failure_summary = "; ".join(f"{name} failed {count}x total" for name, count in top)
        else:
            failure_summary = "no repeated single-tool failures logged — likely just a long task, not a stuck loop"

        lesson = (
            f"[auto-reflection] persona={persona} hit hard-stop ({reason}) on task: "
            f"\"{task}\". Repeated-failure pattern: {failure_summary}. "
            f"If a similar task starts again, check for this pattern early and "
            f"consider a different approach or asking the user for missing info sooner."
        )
        await asyncio.wait_for(
            get_memory_db().store_memory(lesson, category="lesson"),
            timeout=5.0,
        )
        logger.info("[SELF_REFLECT] Auto-lesson stored for persona=%s after %s halt.", persona, reason)
    except asyncio.TimeoutError:
        logger.warning("[SELF_REFLECT] Auto-lesson store timed out — skipping.")
    except Exception as e:
        logger.warning("[SELF_REFLECT] Auto-lesson store skipped: %s", e)


async def _inject_long_term_memories(system_text: str, query: str) -> str:
    """Append semantically relevant memories to the system prompt.

    Silently returns the original system_text if long-term memory is
    unavailable (Postgres down, empty table, embedding error, etc.)
    """
    try:
        from memory.long_term import get_memory_db

        # FIX-7 / FIX-KEEPALIVE: was 3.0s, tuned for a warm embedding model.
        # The real fix was long_term.py no longer unloading nomic-embed-text
        # after every call (see _OllamaEmbedFn) — this only needs to cover
        # the ONE cold-load on Beaver's first embed call, not every call.
        memories = await asyncio.wait_for(
            get_memory_db().search_similar(query, limit=3),
            timeout=8.0,
        )
        if memories:
            lines = "\n".join(
                f"- [{m['category']}] {m['content']}" for m in memories
            )
            return system_text + f"\n\n## Recalled memory\n{lines}"
    except asyncio.TimeoutError:
        logger.warning("[LONG_TERM] Memory retrieval timed out (>8 s) — skipping.")  # FIX-7
    except Exception as e:
        logger.warning("[LONG_TERM] Retrieval skipped: %s", e)
    return system_text


def _looks_like_narration(message: AIMessage) -> bool:
    """Return True when the model described an action instead of calling a tool.

    Catches the failure mode at 3-bit / 4-bit quantisation where ChatOllama
    ignores ``tool_choice`` and writes prose or code blocks instead of issuing
    a tool_call — for example:

        "To list tables I would run: SELECT table_name FROM information_schema…"
        "```sql\\nSELECT * FROM users;\\n```"

    Called only when tools are available and ``message.tool_calls`` is empty.
    Returns False immediately when the model already issued tool_calls (correct
    behaviour) so the fast-path is cheap.

    Three signal families, checked in order of cheapness:
      1. Fenced code block with an executable language tag (sql, bash, sh, py…)
      2. Inline SQL patterns written as plain prose
      3. Narration phrases that announce an intended action rather than taking it
    """
    # Fast-path: model behaved correctly — no further checks needed.
    if getattr(message, "tool_calls", None):
        return False

    content = message.content

    # Flatten multimodal content to a plain string.
    if isinstance(content, list):
        content = " ".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )

    if not isinstance(content, str) or not content.strip():
        return False

    lower = content.lower()

    # [FIX-NARRATION-FP] Every signal below used to fire on bare substring
    # matches anywhere in the text. That's indistinguishable from ordinary
    # finished-answer English: "let me know if you need anything else",
    # "I can help with that", and "select the best option from the two"
    # all matched and got the model's already-correct final answer thrown
    # away, force-retried with a "TOOL CALL REQUIRED" message, and — if the
    # retry (reasonably) still didn't fabricate an unneeded tool call —
    # replaced with a fake "[AGENT] I wasn't able to produce a valid
    # response" failure shown to the user, even though the original answer
    # was fine. The actual tell for genuine narration isn't any of these
    # phrases in isolation, it's the phrase being paired with an
    # execution-flavored verb shortly after ("let me CHECK the database",
    # "I'll RUN a query") — ordinary conversational uses of "let me" / "I
    # can" / "select ... from" are never followed by one. Signal 0 and 1
    # stay unchanged: an embedded {"tool": ...} fragment or ```sql block is
    # unambiguous regardless of surrounding text.
    _ACTION_VERBS = (
        r"run|execute|query|check|list|find|search|fetch|look\s*up|scan|"
        r"describe|inspect|verify|select|insert|update|delete|grep|curl|"
        r"ping|nmap|ssh|install"
    )

    # ── Signal 0: a malformed/incomplete JSON tool-call attempt ────────────────
    # The model tried to write {"tool": ...} but it didn't parse (truncated,
    # trailing prose glued on, wrong quoting, etc.) — still a failure to call
    # a tool correctly, just a different flavor of it.
    if '"tool"' in lower and "{" in content:
        return True

    # ── Signal 1: fenced code block with an executable language ───────────────
    # Model wrote ``sql or ```bash instead of calling a tool.
    if re.search(r"```\s*(sql|bash|sh|shell|python|py)\b", lower):
        return True

    # ── Signal 2: inline SQL written as prose ─────────────────────────────────
    # FROM/SET must be followed by something identifier-shaped (table/column
    # name), not an article + noun phrase — "SELECT * FROM users" vs.
    # "select the best plan from the two options I outlined".
    sql_patterns = [
        r"\bselect\b.{1,80}\bfrom\s+(?!the\b|a\b|an\b|my\b|our\b|your\b|this\b|these\b|those\b)[\w.]+",
        r"\bshow\s+tables\b",
        r"\bshow\s+databases\b",
        r"\bdescribe\s+\w+",
        r"\binsert\s+into\b",
        r"\bupdate\b.{1,40}\bset\s+(?!the\b|a\b|an\b|my\b|our\b|your\b)[\w.]+",
    ]
    for pat in sql_patterns:
        if re.search(pat, lower):
            return True

    # ── Signal 3: narration phrases — only when paired with an action verb ────
    # shortly after, e.g. "let me check", "I'll run", "to list the tables".
    # Bare "let me know", "I can help", "I'd say" etc. no longer match.
    narration_patterns = [
        rf"\b(i would|i'll|i will|i'd|let me|first,?\s*i|i can|i am going to)\b[^.!?]{{0,25}}\b(?:{_ACTION_VERBS})\b",
        r"\bhere('?s| is) the query\b",
        r"\bthe following query\b",
        r"\brunning the\b",
        rf"\bto\s+(?:do this by|{_ACTION_VERBS})\b",
    ]
    return any(re.search(pat, lower) for pat in narration_patterns)


_TOOL_MANUAL_CACHE: Dict[str, Optional[str]] = {}
_TOOL_MANUALS_DIR = Path(__file__).resolve().parent.parent / "tool_manuals"

# Consecutive-tool-failure thresholds. Separate from failed_tool_calls'
# exact-fingerprint repeat-block — this catches the case that guard can't:
# a model stuck trying VARIED-but-equally-futile calls (different payloads
# that all fail, different unreachable targets), which never produces two
# byte-identical fingerprints and so never trips the exact-match guard.
# Counts ANY tool call failing in a row, regardless of tool name or args;
# resets on any success. See execute_tools().
_STUCK_STREAK_WARNING = 3  # inject a stronger corrective message
_STUCK_STREAK_HALT     = 6  # force an early halt — don't wait for max_loops

# [FIX-20] Utilities invoked THROUGH os_exec (nmap, hydra, ...) get their own
# manual files even though they're not separately-registered tools — the
# failure is os_exec's, but the corrective content is utility-specific.
# Extracted from the first word of the "command" arg passed to os_exec.


def _load_tool_manual(name: str) -> Optional[str]:
    """Read tool_manuals/<name>.md if it exists, cached after first read.

    [FIX-19] Manuals are corrective content, not proactive context — only
    ever attached to a ToolMessage after that tool (or, for os_exec, that
    shell utility) already failed, so a normal successful call costs
    exactly what it did before. See tool_manuals/ for the actual files.
    """
    if name in _TOOL_MANUAL_CACHE:
        return _TOOL_MANUAL_CACHE[name]
    path = _TOOL_MANUALS_DIR / f"{name}.md"
    doc = path.read_text(encoding="utf-8") if path.exists() else None
    _TOOL_MANUAL_CACHE[name] = doc
    return doc


_UTILITY_PREFIX_WRAPPERS = {"sudo", "doas"}


def _extract_shell_utility(command: str) -> Optional[str]:
    """Best-effort extraction of the utility name from an os_exec command
    string, e.g. "nmap -sV 127.0.0.1" -> "nmap". [FIX-20]

    [FIX-UTILITY-PREFIX] Skips a single leading sudo/doas wrapper so
    "sudo nmap -sS target" still resolves to "nmap" (and injects
    tool_manuals/nmap.md, which actually exists) instead of "sudo" (which
    has no manual, so the correction was silently lost). sudo is a very
    common prefix for exactly the commands this project's pentester
    persona runs (SYN scans and the like genuinely need root). Doesn't
    attempt to handle sudo's own flags (e.g. "sudo -u www-data nmap...")
    — real argument parsing would be needed to do that correctly in
    general, and this function is explicitly best-effort, not a full
    shell parser.
    """
    if not command:
        return None
    tokens = command.strip().split()
    if not tokens:
        return None

    idx = 0
    first_bare = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if first_bare in _UTILITY_PREFIX_WRAPPERS and len(tokens) > 1:
        idx = 1

    first = tokens[idx]
    # strip a leading path (e.g. /usr/bin/nmap -> nmap) and .exe suffix
    first = first.replace("\\", "/").rsplit("/", 1)[-1]
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if first.lower().endswith(ext):
            first = first[: -len(ext)]
            break
    return first.lower() or None


def _tool_result_failed(result: str) -> bool:

    if not result:
        return False
    head = result[:200]
    if (
        head.startswith("Error:")
        or "MCP error" in head
        or "Input validation error" in head
        or "Traceback (most recent call last)" in result[:500]
    ):
        return True
    m = re.search(r"\[Exit code:\s*(-?\d+)\]", result)
    if m and m.group(1) != "0":
        return True
    # Plugin dead-end format: "[plugin_name] <message indicating unavailability>"
    # e.g. "[code_search] Neither 'rg' nor 'grep' is available on PATH."
    if re.match(r"^\[\w+\]", head) and re.search(
        r"not available|not found|unavailable|cannot|failed",
        head, re.IGNORECASE,
    ):
        return True
    return False


def _json_arg_placeholder(field: Any) -> Any:
    
    try:
        if not field.is_required() and field.default is not None:
            return field.default
    except Exception:
        pass
    ann = getattr(field, "annotation", None)
    if ann in (int,):
        return 0
    if ann in (float,):
        return 0.0
    if ann in (bool,):
        return False
    if ann in (list, List):
        return []
    if ann in (dict, Dict):
        return {}
    return "..."


def _json_schema_placeholder(prop: Dict[str, Any]) -> Any:
    
    if not isinstance(prop, dict):
        return "..."
    if "default" in prop and prop["default"] is not None:
        return prop["default"]
    ptype = prop.get("type")
    if isinstance(ptype, list):  # e.g. ["string", "null"]
        ptype = next((p for p in ptype if p != "null"), ptype[0] if ptype else None)
    if ptype == "integer":
        return 0
    if ptype == "number":
        return 0.0
    if ptype == "boolean":
        return False
    if ptype == "array":
        return []
    if ptype == "object":
        return {}
    return "..."


def _json_call_example(t: BaseTool, required_only: bool = False) -> str:
    
    args: Dict[str, Any] = {}
    schema = getattr(t, "args_schema", None)
    if schema is not None:
        fields = getattr(schema, "model_fields", None)
        if fields:
            # Pydantic model class (normal @tool-decorated tools).
            for name, field in fields.items():
                if required_only:
                    try:
                        if not field.is_required():
                            continue
                    except Exception:
                        pass  # can't tell — err toward including it
                args[name] = _json_arg_placeholder(field)
        elif isinstance(schema, dict):
            # Raw JSON schema (MCP tools — tool.inputSchema).
            required_names = set(schema.get("required") or [])
            for name, prop in (schema.get("properties") or {}).items():
                if required_only and name not in required_names:
                    continue
                args[name] = _json_schema_placeholder(prop)
    # [Task 1 — thought field] "thought" is prepended first so the model
    # commits to a short rationale before the action fields, ReAct-style.
    # Key order matters here only for the rendered example text (dict order
    # is preserved by json.dumps) — it has no bearing on parsing.
    try:
        return json.dumps({"thought": "...", "tool": t.name, "args": args})
    except TypeError:
        return json.dumps({"thought": "...", "tool": t.name, "args": {}})


def _validate_tool_args(tool: BaseTool, tool_args: Dict[str, Any]) -> Optional[str]:
    
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return None
    if not isinstance(tool_args, dict):
        tool_args = {}

    required: List[str] = []
    fields = getattr(schema, "model_fields", None)
    if fields:
        # Pydantic model class (normal @tool-decorated tools).
        for name, field in fields.items():
            try:
                if field.is_required():
                    required.append(name)
            except Exception:
                # Can't determine requiredness for this field — don't guess.
                continue
    elif isinstance(schema, dict):
        # Raw JSON schema (MCP tools — tool.inputSchema).
        required = [r for r in (schema.get("required") or []) if isinstance(r, str)]
    else:
        # Unknown schema shape — skip validation rather than block the call.
        return None

    missing = [name for name in required if name not in tool_args]
    if not missing:
        return None
    return (
        f"Missing required argument(s) for '{tool.name}': {', '.join(missing)}."
    )


# [FIX-JSON] Tool-calling was rewritten from provider-native function-calling
# (langchain .bind_tools()) to plain JSON-in-text, mirroring the approach used
# by the standalone `reaper` prototype. Small/quantised local models are far
# more reliable at "write this JSON shape as your next tokens" than at hitting
# a provider's structured tool-call grammar — the old narration-detector retry
# loop existed largely to paper over the latter failing. This also makes tool
# calling provider-agnostic: no dependency on a given model/provider actually
# implementing function-calling correctly, since it's just text generation.
def _format_tool_list(tools: List[BaseTool]) -> str:
    
    if not tools:
        return "(No tools are available for this persona.)"

    lines = [
        "Call tools by outputting ONLY a single raw JSON object — nothing else,",
        "no prose, no code fences, no explanation before or after:",
        '  {"thought": "<1 short sentence — why this tool/these args>", "tool": "<name>", "args": {...}}',
        "",
        "Available tools:",
        "",
    ]
    for t in tools:
        desc = (t.description or "").strip().splitlines()[0] if t.description else ""
        desc = desc[:100]
        lines.append(f"{t.name}" + (f" — {desc}" if desc else ""))
        lines.append(f"  {_json_call_example(t, required_only=True)}")
    lines += [
        "",
        "Rules:",
        "- One tool call per response — wait for the result before the next one.",
        '- Include a brief "thought" before the tool/args — one short sentence,',
        "  not a plan or narration.",
        "- If no tool is needed, reply in plain text — no JSON at all.",
        "- Never describe what you would run. Call the tool.",
    ]
    return "\n".join(lines)


def _lenient_json_loads(chunk: str) -> Optional[Dict[str, Any]]:
    """Parse a candidate tool-call chunk, tolerating the malformed-but-common
    variants small/quantised local models emit instead of strict JSON.

    [FIX-21] Strict json.loads() alone silently rejected two shapes the
    marker list in _extract_json_tool_call already anticipated but never
    actually handled:
      - Single-quoted strings, e.g. {'tool': 'x', 'args': {...}} — the
        marker search recognizes "{'tool'" / "{'thought'" as valid starts,
        but json.loads() has always rejected single-quoted JSON outright,
        so every such response silently returned None here and got treated
        as narration/blank upstream, burning a full retry call on a
        tool-call that was actually well-formed.
      - Python literals (True/False/None) inside args, which some local
        models substitute for true/false/null.
    Tries strict JSON first (the common case, cheapest), then a
    literal-normalized JSON pass, then ast.literal_eval as a last resort
    (safe here — it only ever evaluates literal structures, never executes
    code, and we discard anything that isn't a dict). Returns None if
    nothing parses.
    """
    try:
        return json.loads(chunk)
    except (json.JSONDecodeError, ValueError):
        pass

    # Python-literal booleans/None -> JSON equivalents. Word-boundaried so
    # this can't corrupt a real string value that happens to contain these
    # words, e.g. an arg like "reason": "True positive".
    normalized = re.sub(r"\bTrue\b", "true", chunk)
    normalized = re.sub(r"\bFalse\b", "false", normalized)
    normalized = re.sub(r"\bNone\b", "null", normalized)
    try:
        return json.loads(normalized)
    except (json.JSONDecodeError, ValueError):
        pass

    try:
        import ast
        obj = ast.literal_eval(normalized)
        if isinstance(obj, dict):
            return obj
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        pass
    return None


def _extract_json_tool_call(text: str) -> Optional[Dict[str, Any]]:
    
    if not text:
        return None
    cleaned = text.replace("```json", "").replace("```", "")

    start = -1
    for marker in (
        '{"tool"', "{ \"tool\"", "{'tool'",
        '{"thought"', "{ \"thought\"", "{'thought'",
    ):
        i = cleaned.find(marker)
        if i != -1 and (start == -1 or i < start):
            start = i
    if start == -1:
        return None

    depth = 0
    # [FIX-21] Track WHICH quote char opened the current string (or None),
    # not just a bool — the old bool-only version only toggled on '"',
    # so a single-quoted string value containing a literal '{' or '}'
    # (rare, but possible in free-text args) could throw off brace depth
    # counting for the single-quoted tool-call shape the marker list above
    # already claims to support.
    in_str: Optional[str] = None
    esc = False
    for i in range(start, len(cleaned)):
        c = cleaned[i]
        if esc:
            esc = False
        elif c == "\\" and in_str:
            esc = True
        elif in_str:
            if c == in_str:
                in_str = None
        elif c in ('"', "'"):
            in_str = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                chunk = cleaned[start : i + 1]
                obj = _lenient_json_loads(chunk)
                if obj is None:
                    return None
                if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
                    return obj
                return None
    return None


def _is_blank_response(response: AIMessage) -> bool:
    
    if getattr(response, "tool_calls", None):
        return False
    content = response.content
    if isinstance(content, list):
        content = " ".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return not isinstance(content, str) or not content.strip()


def _parse_tool_call_response(response: AIMessage, tools: List[BaseTool]) -> AIMessage:
    
    if not tools:
        return response
    content = response.content if isinstance(response.content, str) else str(response.content)
    parsed = _extract_json_tool_call(content)
    if not parsed:
        return response
    tool_args = parsed.get("args")
    if not isinstance(tool_args, dict):
        tool_args = {}
    tool_args.pop("thought", None)
    return AIMessage(
        content=content,
        tool_calls=[
            {
                "name": parsed["tool"],
                "args": tool_args,
                "id": f"call_{_uuid.uuid4().hex[:8]}",
                "type": "tool_call",
            }
        ],
    )


# ── Graph nodes ────────────────────────────────────────────────────────────────

async def call_model(
    state: AgentState,
    config: Optional[RunnableConfig] = None,  # FIX-3
) -> Dict[str, Any]:
    iteration = state.get("iteration", 0)
    tid = _thread_id(config)
    active_cfg = _get_active_config()

    # ── Hard stop ──────────────────────────────────────────────────────────────
    if iteration >= active_cfg.max_loops:
        await _auto_store_halt_lesson(state, f"max_loops={active_cfg.max_loops}")
        return {
            "messages": [
                AIMessage(
                    content=f"[AGENT HALTED]: Reached max loop limit ({active_cfg.max_loops})."
                )
            ],
            "iteration": iteration,
        }

   
    _consecutive_failures = 0 if iteration == 0 else int(state.get("consecutive_tool_failures", 0) or 0)
    if _consecutive_failures >= _STUCK_STREAK_HALT:
        await _auto_store_halt_lesson(state, f"consecutive_tool_failures={_consecutive_failures}")
        return {
            "messages": [
                AIMessage(
                    content=(
                        f"[AGENT HALTED]: {_consecutive_failures} tool calls in a row failed "
                        f"(varied arguments, same stuck pattern — this is separate from the "
                        f"exact-repeat guard). Stopping rather than burning the rest of the "
                        f"iteration budget on a pattern that isn't working. What was attempted "
                        f"and why it didn't work should be visible in the conversation above."
                    )
                )
            ],
            "iteration": iteration,
            "consecutive_tool_failures": 0,  # reset for any follow-up message in this thread
        }

    active_persona = state.get("active_persona", "standard")
    raw_messages = list(state["messages"])

    tools: List[BaseTool] = await get_persona_tools_async(active_persona)

 
    try:
        system_text = runtime_config.load_persona_prompt(active_persona)
    except FileNotFoundError as exc:
        logger.error(
            "[PERSONA] Failed to load prompt for persona '%s': %s — "
            "falling back to system_agent.md",
            active_persona, exc,
        )
        system_text = runtime_config.load_system_prompt()
        system_text = system_text.replace("{{FAILED_PERSONA}}", active_persona)


    if "{{TOOL_LIST}}" in system_text:
        system_text = system_text.replace("{{TOOL_LIST}}", _format_tool_list(tools))


    _live_scope = state.get("target_scope") or active_cfg.scope
    _scope_text = ", ".join(_live_scope) if _live_scope else "(empty — all targets allowed)"
    if "{{SCOPE}}" in system_text:
        system_text = system_text.replace("{{SCOPE}}", _scope_text)

    # ── Shared cross-persona modules (FIX-14) ──────────────────────────────────
    # Appended here instead of duplicated into every prompts/personas/*.md file.
    # Currently: language/dialect mirroring, and the long-term memory
    # persistence policy (store_long_term_memory was bound to every persona's
    # tool list but only researcher.md ever told the model to call it — the
    # other personas could recall memories but never wrote any, which is why
    # long-term memory looked "on" but never actually persisted anything).
    for _shared_name in ("language_dialect", "memory_policy", "self_reflection"):
        _shared_block = runtime_config.load_shared_prompt(_shared_name)
        if _shared_block:
            system_text = f"{system_text}\n\n{_shared_block}"

    # ── Long-term RAG injection ────────────────────────────────────────────────
    # Only inject on the first turn (iteration == 0): the system prompt doesn't
    # change on tool-loop re-entries, so re-embedding the same query 5+ times
    # on a multi-step task is pure waste.  _embedding_query() also handles
    # multimodal messages (content as list) and truncates to 512 chars so the
    # embedding call always finishes within the 3 s timeout.
    human_msgs = [m for m in raw_messages if isinstance(m, HumanMessage)]
    if human_msgs and iteration == 0:
        system_text = await _inject_long_term_memories(
            system_text, _embedding_query(human_msgs[-1].content)
        )

    # ── Compact message window (FIX-1) ─────────────────────────────────────────
    # Strip any existing SystemMessages before compaction so the freshly
    # enriched system_text (with long-term memories) is always prepended,
    # not silently discarded when a stale SystemMessage is already present.
    #
    # [FIX-CTX-BUDGET] max_messages alone bounds message COUNT, not size —
    # a handful of large tool outputs (nmap -sV dumps, etc.) can still add
    # up to more tokens than num_ctx has room for even within that count,
    # leaving zero tokens of headroom for the model to actually generate a
    # response (observed: qwen3.5:4b returning genuinely empty completions
    # several turns into a tool-use loop — see memory/compactor.py's
    # [FIX-CTX-BUDGET] note for the full explanation). Compute a real
    # remaining-token budget for the history — num_ctx minus this turn's
    # system prompt (already finalized above, TOOL_LIST + memories and
    # all) minus a reserved generation headroom — and pass it through so
    # compact_messages() can trim to it, not just to a message count.
    non_system = [m for m in raw_messages if not isinstance(m, SystemMessage)]
    _system_tokens = len(system_text) // 4
    _generation_reserve = max(512, int(active_cfg.num_ctx * 0.15))
    _history_budget = max(256, active_cfg.num_ctx - _system_tokens - _generation_reserve)
    pruned = compact_messages(non_system, max_messages=12, max_tokens=_history_budget)
    pruned = [SystemMessage(content=system_text)] + list(pruned)

    # ── LLM — cached, not re-instantiated every call (FIX-2) ──────────────────
    # [FIX-CALLBACK-CHAIN] Previously `invoke_cfg = {"callbacks": get_callbacks()}`
    # — a brand-new config carrying ONLY Beaver's own LocalAgentTracer,
    # completely discarding the `config` parameter LangGraph passed into this
    # node. That incoming `config` carries the callback manager that
    # graph.astream_events() (cli/ui.py, tui/app.py) relies on to observe
    # on_chat_model_start/stream/end for this exact llm.ainvoke() call. Since
    # it was never threaded through, those events never fired for the UI —
    # response_text stayed permanently empty regardless of what the model
    # actually generated, producing "(empty response — the model returned
    # nothing)" on every single turn even when the tool call worked
    # perfectly (tool output is printed via a separate tui_bus side-channel,
    # not through astream_events, which is why it still showed up). The
    # graph itself was never broken — only the UI's visibility into it.
    # merge_configs() combines the parent config's callback manager with
    # Beaver's own tracer instead of replacing it.
    invoke_cfg: Dict[str, Any] = merge_configs(config, {"callbacks": get_callbacks()})

    # [FIX-ROUTER-2] _get_cached_llm() can raise on first use of a given
    # (model, provider, temperature) — ImportError (SDK not installed, e.g.
    # "pip install langchain-anthropic") or EnvironmentError (API key not
    # set) from model_router.py's per-provider builders. This is exactly
    # the failure mode a user switching MODEL_PROVIDER to a new stack hits
    # first, and it used to propagate straight out of this node as a raw
    # unhandled exception — crashing the whole turn — instead of getting
    # the same clean, actionable [MODEL ERROR] message the ainvoke() path
    # below already produces for a live connection failure. Nothing is
    # cached on a construction failure (the dict write only happens after
    # get_llm() returns successfully), so there's nothing to evict here —
    # unlike the ainvoke() except block below, which evicts a client that
    # WAS successfully built but failed mid-call.
    try:
        llm = _get_cached_llm()
    except Exception as exc:
        logger.warning("[LLM] Client construction failed (%s) — ending turn cleanly.", exc)
        error_response = AIMessage(
            content=(
                f"[MODEL ERROR]: Could not initialize the model client ({exc}). "
                f"Check MODEL_NAME/MODEL_PROVIDER and the matching API key/package "
                f"in .env, then try again."
            )
        )
        return {"messages": [error_response], "iteration": iteration + 1}

    # [FIX-JSON] No more .bind_tools() — every call is a plain ainvoke() and
    # tool calls are recovered by parsing {"tool": ..., "args": {...}} out of
    # the model's own text (see _format_tool_list / _extract_json_tool_call).
    # This works identically across every provider/model and doesn't depend
    # on the model correctly hitting a provider-specific function-calling
    # grammar — the thing small quantised local models were unreliable at.
    try:
        response = await llm.ainvoke(pruned, config=invoke_cfg)
    except Exception as exc:
        # Left uncaught, a raw client exception (connection reset, malformed
        # response, etc.) crashes the turn AND can leave the cached client's
        # connection poisoned, hanging every subsequent turn with no further
        # error ("frozen" with no output). Evict the cache so the next call
        # gets a fresh client, and end this turn cleanly instead of
        # propagating a raw exception up through the graph/checkpointer.
        logger.warning(
            "[LLM] Call failed (%s) — evicting cached client and ending turn cleanly.",
            exc,
        )
        _evict_cached_llm()
        error_response = AIMessage(
            content=(
                f"[MODEL ERROR]: The model call failed ({exc}). Please try "
                f"rephrasing your request, or try again — a fresh model "
                f"connection will be used."
            )
        )
        return {"messages": [error_response], "iteration": iteration + 1}

    response = _parse_tool_call_response(response, tools)

    # ── Narration / blank-response detector (Fix A, FIX-JSON-BLANK) ────────────
    # The model wrote prose/code instead of a {"tool": ...} JSON object (or
    # attempted one that didn't parse), OR it produced nothing at all — no
    # tool call and no text. Either way, inject a hard-enforcement message
    # and retry exactly once. The bad response is included in the retry
    # context so the model sees what it got wrong; the enforcement message
    # makes the requirement explicit. Only retried when tools are actually
    # available — text responses are legitimate when there are no tools to
    # call.
    was_blank = _is_blank_response(response)
    if tools and (_looks_like_narration(response) or was_blank):
        logger.warning(
            "[%s DETECTED] %s (iteration=%d, content_len=%d). Retrying with "
            "enforcement injection.",
            "BLANK RESPONSE" if was_blank else "NARRATION",
            "Model returned neither a tool call nor any text"
            if was_blank else "Model wrote prose/code instead of calling a tool",
            iteration,
            len(str(response.content)),
        )
        enforcement = HumanMessage(
            content=(
                "TOOL CALL REQUIRED — your previous response was rejected.\n"
                + (
                    "You returned an empty response — no tool call, no text. "
                    "This is not allowed.\n\n"
                    if was_blank else
                    "You wrote text or code instead of calling a tool. This is not allowed.\n\n"
                )
                + "Rules:\n"
                "• Do NOT write SQL, shell commands, or code blocks.\n"
                "• Do NOT describe what you would do or plan out loud.\n"
                "• Do NOT return an empty response.\n"
                "• Output ONLY a raw JSON object right now: "
                '{"tool": "<name>", "args": {...}} — nothing else.\n\n'
                "Pick the correct tool from your tool list and call it immediately."
            )
        )
        retry_msgs = list(pruned) + [response, enforcement]
        try:
            retried = await llm.ainvoke(retry_msgs, config=invoke_cfg)
            response = _parse_tool_call_response(retried, tools)
        except Exception as exc:
            logger.warning(
                "[LLM] Narration-retry call failed (%s) — evicting cached client "
                "and keeping the original response for this turn.",
                exc,
            )
            _evict_cached_llm()
            # Keep the original response rather than crashing the turn — even
            # blank/narrated, it's valid content, and the model gets another
            # chance on the next user message.
        logger.info(
            "[NARRATION RETRY] Result: tool_calls=%s content_len=%d",
            bool(getattr(response, "tool_calls", None)),
            len(str(response.content)),
        )

    
        _still_stuck = not getattr(response, "tool_calls", None) and (
            _is_blank_response(response) or _looks_like_narration(response)
        )
        if _still_stuck:
            logger.warning(
                "[%s] Retry ALSO failed — model produced neither a tool call "
                "nor usable text twice in a row. Returning an explicit "
                "failure message instead of a silent/blank turn.",
                "BLANK RESPONSE" if was_blank else "NARRATION",
            )
            await _auto_store_halt_lesson(
                state, "two consecutive unusable responses (blank/narration retry exhausted)"
            )
            response = AIMessage(
                content=(
                    "[AGENT] I wasn't able to produce a valid response for this step "
                    "after two attempts — the model returned an empty or non-tool "
                    "response both times. This can happen when the model runs low on "
                    "usable context budget, or is struggling with this specific "
                    "request. Try rephrasing, breaking the task into a smaller step, "
                    "or check agent.log for details."
                )
            )


    _out: Dict[str, Any] = {
        "messages": [response],
        "iteration": iteration + 1,
        # [FIX-A2A] Do NOT store tool objects in state (not msgpack-serializable).
    }
    # Reset repeat-fail tracking on each fresh HumanMessage (iteration 0).
    # [FIX-STALE-STREAK] consecutive_tool_failures reset alongside
    # failed_tool_calls — see the note at the top of this function for why
    # it *also* has to be zeroed early (before the hard-stop check), this
    # write just makes sure the zeroed value is what gets persisted to the
    # checkpoint too, not the stale pre-reset one.
    if iteration == 0:
        _out["failed_tool_calls"] = {}
        _out["consecutive_tool_failures"] = 0
    return _out


def _flatten_callback_handlers(cb: Any) -> List[Any]:
    """Normalize a `callbacks` config value into a flat list of handler
    objects, whether it's already a list, a CallbackManager, or None.

    [FIX-CALLBACK-CHAIN] Streaming tools (astream_invoke) don't go through
    LangChain's ainvoke() path, so on_tool_start/on_tool_end are fired here
    manually. That loop previously iterated Beaver's own `callbacks =
    get_callbacks()` list only — after merge_configs() combines it with the
    parent node config's callback manager (see call_model / execute_tools),
    that combined value can be a CallbackManager instance rather than a
    plain list, and iterating a manager object directly doesn't yield its
    handlers. This unwraps either shape so every handler — including the
    one LangGraph attaches for graph.astream_events() — actually gets the
    manually-fired event, not just Beaver's own tracer.
    """
    if cb is None:
        return []
    if isinstance(cb, list):
        return list(cb)
    handlers = getattr(cb, "handlers", None)
    return list(handlers) if handlers else []


# [FIX-SCOPE-DOMAIN] Explicit allowlist, not a persona+argname heuristic.
# web_search.py's web_search/web_fetch/ddg_news also take a "url"-ish
# argument and are also bound to the pentester persona, but pentester.md
# documents them explicitly as general research ("General web search and
# page fetch... Exploit writeups, advisory detail") — not target-directed,
# and correctly should NOT be scope-gated. This list matches exactly the
# tools pentester.md itself documents as operating directly against the
# target (root domain, discovered subdomains, SSRF probes, etc.) — see
# that file's tool table for subdomain_enum / subdomain_bruteforce /
# http_get / http_post / http_head / http_check / http_session_get /
# http_session_post / probe_payloads. See execute_tools()'s scope gate
# below and agent/tools.py's is_target_in_scope() for the full finding.
_SCOPE_GATED_TARGET_TOOLS = frozenset({
    "subdomain_enum", "subdomain_bruteforce",
    "http_get", "http_post", "http_head", "http_check",
    "http_session_get", "http_session_post", "probe_payloads",
})


async def execute_tools(
    state: AgentState,
    config: Optional[RunnableConfig] = None,  # FIX-3
) -> Dict[str, Any]:
    messages = state.get("messages") or []
    if not messages:
        return {"messages": []}
    last_message = messages[-1]
    if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
        return {"messages": []}

    tid = _thread_id(config)
    # [FIX-NOTES-THREAD] Set for the duration of this node so any tool that
    # imports and reads active_thread_id_var() (structured_notes.py) gets
    # the real per-conversation thread id, not the dead env var fallback.
    active_thread_id_var.set(tid)
    active_persona = state.get("active_persona", "standard")
    # FIX-10: fall back to the active (session/thread/global) config's scope,
    # not always the global singleton — state["target_scope"] is already set
    # per-turn by web/server.py, but tool call sites that construct state by
    # hand (e.g. tests, cli one-shots without target_scope) should still get
    # the right session's scope rather than always the global one.
    scope = state.get("target_scope") or _get_active_config().scope

    # ── Tool registry — always rebuilt async (FIX-A2A) ───────────────────────
    # We no longer store tool objects in state (they're not msgpack-serializable
    # and crashed every A2A sub-agent).  Rebuilding here is cheap: get_mcp_tools()
    # caches live connections and returns instantly on cache-hit turns.
    tool_registry: Dict[str, BaseTool] = {
        t.name: t for t in await get_persona_tools_async(active_persona)
    }

    tool_outputs: List[ToolMessage] = []
    stream_cb = tui_bus.emit_stream   # direct reference — no lambda, defined once
    callbacks = get_callbacks()

    # ── Repeat-fail tracking — loaded from state, updated as failures accumulate ─────
    # Fingerprint: "tool_name::repr(sorted_args)". On a cache-hit (count >= 1)
    # the call is blocked before dispatch so the loop can't spin indefinitely.
    failed_calls: Dict[str, int] = dict(state.get("failed_tool_calls") or {})

    # Coarser companion to failed_calls — counts ANY failure in a row regardless
    # of tool/args, so a "varied but equally stuck" pattern still gets caught.
    # See _STUCK_STREAK_WARNING/_STUCK_STREAK_HALT and call_model's hard-stop.
    consecutive_failures: int = int(state.get("consecutive_tool_failures", 0) or 0)

    for tc in last_message.tool_calls:
        tool_name: str = tc["name"]
        tool_args: dict = tc["args"]
        call_id: str = tc["id"]

        logger.info("[TOOL EXECUTION]: %s args=%s", tool_name, tool_args)
        tui_bus.emit_stream(f"\n[EXECUTING TOOL: {tool_name}]", "stdout")

        # ── Repeat-fail gate — block calls identical to a previous failure ───────
        _call_key = f"{tool_name}::{repr(sorted(tool_args.items()))}"
        if failed_calls.get(_call_key, 0) >= 1:
            _prior = failed_calls[_call_key]
            consecutive_failures += 1
            logger.warning(
                "[REPEAT-FAIL] Blocking repeat call to '%s' — identical args failed %d time(s).",
                tool_name, _prior,
            )
            tool_outputs.append(
                ToolMessage(
                    content=(
                        f"[REPEAT-FAIL] Tool '{tool_name}' with identical arguments has "
                        f"already failed {_prior} time(s) this turn. "
                        f"Do NOT call it again with the same arguments. "
                        f"Use a different tool, adjust the arguments, or report "
                        f"that this operation is not possible in this environment."
                    ),
                    tool_call_id=call_id,
                    name=tool_name,
                )
            )
            continue


        # ── Scope gate ─────────────────────────────────────────────────────────
        command = tool_args.get("command", "")
        # [FIX-SCOPE-DOMAIN] See agent/tools.py's is_target_in_scope docstring
        # for the full finding: pentester.md tells the model every tool call
        # is scope-checked, but only os_exec's "command" string (IP-only
        # matching) ever actually was. subdomain_enum/subdomain_bruteforce
        # (domain=...) and http_get/post/head/check + the http_session_*
        # tools + probe_payloads (url=..., urls=...) were completely
        # unchecked. Gated to active_persona == "pentester" AND tool_name in
        # _SCOPE_GATED_TARGET_TOOLS (an explicit allowlist, not just "has a
        # url/domain argument") — web_search.py's web_search/web_fetch/
        # ddg_news also take a url-ish argument and are also bound to the
        # pentester persona, but pentester.md documents those explicitly as
        # general research, not target-directed, so they're deliberately
        # excluded from the allowlist. And http_probe.py's http_get/post/
        # head/check are ALSO bound to coder/seo/researcher/standard/
        # orchestrator for ordinary internet API use outside any pentest
        # engagement — the persona check keeps those personas unaffected
        # even though their tool names are on the same allowlist.
        target_value = (
            tool_args.get("domain") or tool_args.get("url") or tool_args.get("urls") or ""
        )
        scope_violation = ""
        if command and not is_in_scope(command, scope):
            scope_violation = command
        elif (
            active_persona == "pentester"
            and tool_name in _SCOPE_GATED_TARGET_TOOLS
            and target_value
            and not is_target_in_scope(target_value, scope)
        ):
            scope_violation = target_value

        if scope_violation:
            consecutive_failures += 1
            tool_outputs.append(
                ToolMessage(
                    content=(
                        f"[SCOPE BLOCK]: '{scope_violation}' targets a host outside "
                        f"authorized scope {scope}. Aborted."
                    ),
                    tool_call_id=call_id,
                    name=tool_name,
                )
            )
            continue

        # ── Dispatch ───────────────────────────────────────────────────────────
        if tool_name not in tool_registry:
            result = f"Error: '{tool_name}' is not registered for persona '{active_persona}'."
        else:
            tool = tool_registry[tool_name]

            # ── Pre-dispatch arg validation (Task 2) ──────────────────────────
            # Presence-only check against the tool's real schema, run BEFORE
            # the tool is ever invoked. Catches the common "model forgot a
            # required field" case immediately instead of spending a full
            # tool.ainvoke() + a whole extra loop iteration to get the same
            # correction via tool_manuals/ injection on failure. Routed
            # through the same failed_calls bookkeeping as a normal failure
            # (so an identical bad call can't loop forever) but the
            # _tool_result_failed()/manual-injection path below is skipped
            # entirely — the validation message already states exactly
            # what's wrong and shows the correct shape via _json_call_example.
            _validation_error = _validate_tool_args(tool, tool_args)
            if _validation_error is not None:
                logger.warning(
                    "[ARG VALIDATION] Blocking dispatch to '%s' — %s",
                    tool_name, _validation_error,
                )
                failed_calls[_call_key] = failed_calls.get(_call_key, 0) + 1
                consecutive_failures += 1
                tool_outputs.append(
                    ToolMessage(
                        content=(
                            f"Error: {_validation_error}\n\n"
                            f"Correct call shape:\n{_json_call_example(tool)}"
                        ),
                        tool_call_id=call_id,
                        name=tool_name,
                    )
                )
                continue

            # [FIX-CALLBACK-CHAIN] Same fix as call_model's invoke_cfg: merge
            # the incoming node `config` (LangGraph's callback manager, which
            # graph.astream_events() needs to see on_tool_start/on_tool_end
            # for this dispatch) with Beaver's own tracer instead of
            # replacing it outright.
            tool_invoke_cfg: Dict[str, Any] = merge_configs(config, {"callbacks": callbacks})

            if hasattr(tool, "astream_invoke"):
                # [FIX-8] Streaming tools bypass LangChain's normal ainvoke() path,
                # so on_tool_start / on_tool_end never fire automatically.
                # We fire them manually here so Langfuse sees streaming tool spans
                # exactly like regular tool spans.
                # [FIX-CALLBACK-CHAIN] Iterate the merged handler list (Beaver's
                # tracer + the parent config's manager) instead of the bare
                # `callbacks` list, so graph.astream_events() actually observes
                # this tool's start/end too — see _flatten_callback_handlers.
                synthetic_run_id = _uuid.uuid4()
                serialized = {"name": tool_name}
                args_str = str(tool_args)
                all_handlers = _flatten_callback_handlers(tool_invoke_cfg.get("callbacks"))

                for cb in all_handlers:
                    if hasattr(cb, "on_tool_start"):
                        try:
                            await cb.on_tool_start(
                                serialized,
                                args_str,
                                run_id=synthetic_run_id,
                            )
                        except Exception as exc:
                            logger.warning("[TOOL] on_tool_start cb error: %s", exc)

                try:
                    result = await tool.astream_invoke(tool_args, output_callback=stream_cb)
                except Exception as exc:
                    for cb in all_handlers:
                        if hasattr(cb, "on_tool_error"):
                            try:
                                await cb.on_tool_error(exc, run_id=synthetic_run_id)
                            except Exception:
                                pass
                    result = f"Error: {exc}"
                else:
                    for cb in all_handlers:
                        if hasattr(cb, "on_tool_end"):
                            try:
                                await cb.on_tool_end(
                                    str(result),
                                    run_id=synthetic_run_id,
                                )
                            except Exception as exc:
                                logger.warning("[TOOL] on_tool_end cb error: %s", exc)
            else:
                # [FIX-19] Previously unguarded — any exception here (e.g. a
                # dead MCP subprocess, a connection refusal) propagated all
                # the way up and crashed the whole turn instead of being
                # reported back to the model as a normal tool failure.
                try:
                    result = await tool.ainvoke(tool_args, config=tool_invoke_cfg)
                except Exception as exc:
                    result = f"Error: {exc}"
                # [FIX-14 debug] Log the raw pre-stringification result so we can
                # diagnose cases where the final ToolMessage content ends up
                # suspiciously short (e.g. MCP adapter returning an empty content
                # list, or a non-text content block that stringifies to almost
                # nothing). Remove once the sequential-thinking / MCP output
                # issue is confirmed fixed.
                logger.debug(
                    "[TOOL RAW RESULT] %s type=%s repr=%s",
                    tool_name, type(result).__name__, repr(result)[:500],
                )

        result_str = str(result)

        # [FIX-19] On a detected failure, attach that tool's skill doc (if one
        # exists) as corrective guidance. No separate retry loop needed — the
        # graph already routes tools → agent on every turn, so the model sees
        # this correction on its very next generation and gets to try again
        # within the existing max_loops bound.
        if _tool_result_failed(result_str):
            # Track failure so identical retry is blocked immediately.
            failed_calls[_call_key] = failed_calls.get(_call_key, 0) + 1
            consecutive_failures += 1

            manuals: List[str] = []
            main_manual = _load_tool_manual(tool_name)
            if main_manual:
                manuals.append(f"--- Correct usage for '{tool_name}': ---\n{main_manual}")
            # [FIX-20] os_exec failures are really about the shell utility that
            # was run (nmap, hydra, ...), not os_exec itself — check for a
            # utility-specific manual too and include it alongside os_exec's.
            if tool_name == "os_exec":
                utility = _extract_shell_utility(tool_args.get("command", ""))
                if utility:
                    utility_manual = _load_tool_manual(utility)
                    if utility_manual:
                        manuals.append(f"--- Correct usage for '{utility}': ---\n{utility_manual}")
            if manuals:
                result_str = result_str + "\n\nThis call failed.\n\n" + "\n\n".join(manuals)
                logger.info(
                    "[TOOL MANUAL] Injected correction for failed tool '%s' (%d manual(s))",
                    tool_name, len(manuals),
                )
        else:
            consecutive_failures = 0  # any real success resets the stuck-streak counter

        tool_outputs.append(
            ToolMessage(content=result_str, tool_call_id=call_id, name=tool_name)
        )


    # ── Stuck-streak warning ──────────────────────────────────────────────────
    # A hard stop is handled in call_model (it owns turn-ending decisions);
    # this is the earlier, softer intervention — inject one extra corrective
    # message once the streak crosses the warning threshold, WITHOUT halting,
    # so the model gets a chance to course-correct before the hard stop fires
    # on a later iteration. Only fires below the hard-stop threshold — no
    # point warning on the same turn call_model is about to halt anyway.
    if _STUCK_STREAK_WARNING <= consecutive_failures < _STUCK_STREAK_HALT:
        logger.warning(
            "[STUCK STREAK] %d consecutive tool failures (varied args) — injecting course-correct nudge.",
            consecutive_failures,
        )
        # HumanMessage, not another ToolMessage — every tool_call_id in this
        # batch already has exactly one ToolMessage response above; adding a
        # second ToolMessage reusing one of those ids would break the strict
        # tool_call_id -> ToolMessage pairing some chat APIs enforce. A
        # trailing HumanMessage after ToolMessages is a normal, valid
        # sequence (same pattern call_model's narration-retry already uses).
        tool_outputs.append(
            HumanMessage(
                content=(
                    f"[STUCK PATTERN DETECTED]: {consecutive_failures} tool calls in a row have "
                    f"failed, even though the arguments keep changing (this is separate from the "
                    f"exact-repeat block above). Varying small details on a fundamentally broken "
                    f"approach usually doesn't help. Before the next call: either try a genuinely "
                    f"different tool/strategy, or if this isn't achievable in this environment, "
                    f"stop and tell the user why instead of continuing to retry."
                )
            )
        )

    return {
        "messages": tool_outputs,
        "failed_tool_calls": failed_calls,
        "consecutive_tool_failures": consecutive_failures,
    }


# ── Routing ────────────────────────────────────────────────────────────────────

def should_continue(state: AgentState) -> str:

    messages = state.get("messages") or []
    if not messages:
        return END
    last = messages[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END


# ── Graph factory ──────────────────────────────────────────────────────────────

def create_beaver_graph(checkpointer=None):
    """Build and compile the Beaver StateGraph.

    Parameters
    ----------
    checkpointer : BaseCheckpointSaver | None
        A ready checkpointer instance.  For production, obtain one from
        ``lifespan_checkpointer()`` and pass it here — do NOT call
        get_checkpointer() and pass the result; that returns MemorySaver only.
        Pass None to get a MemorySaver automatically (dev / tests).

    Returns
    -------
    CompiledStateGraph
        A fully compiled graph ready to call with .ainvoke() / .astream().

    Example
    -------
    In your async entry point (main.py, cli/ui.py):

        from memory.checkpointer import lifespan_checkpointer
        from agent.graph import create_beaver_graph

        async with lifespan_checkpointer() as checkpointer:
            graph = create_beaver_graph(checkpointer)
            await run_app(graph)
    """
    from langgraph.checkpoint.memory import MemorySaver

    workflow = StateGraph(AgentState)
    workflow.add_node("agent", call_model)
    workflow.add_node("tools", execute_tools)
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", END: END},
    )
    workflow.add_edge("tools", "agent")

    cp = checkpointer if checkpointer is not None else MemorySaver()
    return workflow.compile(checkpointer=cp)
