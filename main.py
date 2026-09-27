"""
Beaver Agent — Entry Point

Usage:
    python main.py                          # interactive CLI (full command loop)
    python main.py "your prompt"            # one-shot, default persona
    python main.py "your prompt" pentester  # one-shot, specify persona
    python main.py --models                 # list available Ollama models
    python server.py                        # web UI → http://localhost:9000

[FIX-8] Removed the Textual TUI (`--tui`, tui/) — dropped in favor of
        server.py's browser UI (see server.py's module docstring for why:
        UTF-8 end-to-end fixes the Arabic/terminal-encoding issues the TUI
        and console had).

Fix log
-------
[FIX-1] default_state(skill=skill) → default_state(persona=skill).
        The parameter is named `persona`, not `skill`; the old call raised
        TypeError on every non-TUI invocation.

[FIX-2] Removed unused `from skills import SKILL_TOOLS` import.

[FIX-3] Replaced `list[str] | None` with `Optional[List[str]]` for
        Python 3.9 compatibility.

[FIX-4] show_models() is now called when --models flag is passed, and also
        shown at the start of interactive CLI mode instead of being dead code.

[FIX-5] Added try/except around graph invocation in one-shot mode so LLM
        API failures, Redis unavailability, etc. produce a clean error
        message rather than a raw Python traceback.

[FIX-6] One-shot runs now use a uuid4-based thread_id so successive calls
        don't bleed into the same checkpoint history.  Pass --thread <id>
        to reuse a specific conversation thread.

[FIX-7] MCP connections are now torn down in a `finally` block at the end
        of every one-shot run.  Without this, the module-level
        `_active_stack`, `_cached_persona`, and `_cached_tools` in
        skills/mcp_loader.py are left pointing at ClientSession objects
        that were created inside the just-completed event loop.  If
        `main()` is called again in the same process (tests, scripted
        multi-call use) `get_mcp_tools()` fast-paths on the stale cache
        and returns already-closed tool handles — silently breaking every
        MCP tool call.  The `finally` block calls
        `_release_mcp_connections()` and zeros the cache so the next call
        always starts clean.
"""
import asyncio
import sys
import uuid
from typing import List, Optional

from langchain_core.messages import HumanMessage

from agent.config import runtime_config
from agent.graph import create_beaver_graph
from agent.state import default_state
from agent.telemetry import get_callbacks
from memory.checkpointer import lifespan_checkpointer


def show_models() -> None:
    """Print available Ollama models and flag the active one."""
    try:
        models = runtime_config.list_ollama_models()
        print(f"\n[OLLAMA] Available: {', '.join(models)}")
        print(f"[ACTIVE] {runtime_config.model}")
    except Exception as e:
        print(f"[WARN]   Ollama unreachable: {e}")
        print(f"[ACTIVE] {runtime_config.model} (unverified)")


async def run(
    prompt: str,
    graph,
    persona: str = "standard",
    scope: Optional[List[str]] = None,  # FIX-3
    thread_id: str = "main",
) -> None:
    """Invoke the Beaver graph and stream node-by-node output to stdout."""
    state = default_state(persona=persona)   # FIX-1: was default_state(skill=skill)
    state["messages"] = [HumanMessage(content=prompt)]
    if scope:
        state["target_scope"] = scope

    config = {"configurable": {"thread_id": thread_id}}

    print(f"\n[BEAVER] persona={persona}  model={runtime_config.model}  thread={thread_id}")
    print(f"[QUERY]  {prompt}")
    print("-" * 60)

    # ── Local tracer: register a parent trace for this run ──────────────────
    # set_run_context() groups all LLM generations and tool spans under one trace
    # and tool spans for this run will be nested under.  Without this call,
    # each LLM invocation creates its own orphaned trace and tools are invisible.
    run_trace_id = str(uuid.uuid4())
    for cb in get_callbacks():
        if hasattr(cb, "set_run_context"):
            await cb.set_run_context(
                trace_id=run_trace_id,
                session_id=thread_id,
                user_input=prompt,
            )

    async for event in graph.astream(state, config=config):
        for node_name, state_update in event.items():
            msgs = state_update.get("messages", [])
            if not msgs:
                continue
            latest = msgs[-1]
            if hasattr(latest, "tool_calls") and latest.tool_calls:
                for tc in latest.tool_calls:
                    print(f"[{node_name}] TOOL  -> {tc['name']}")
                    print(f"           ARGS  -> {tc['args']}")
            elif latest.content:
                print(f"[{node_name}] {latest.content}")
            print("-" * 60)


def _parse_args() -> dict:
    """Parse sys.argv into a simple dict of flags and positional args.

    [FIX-ARGPARSE] The previous version built `positional` with a blind
    "doesn't start with --" filter over the whole argv, then tried to
    strip out --thread's value afterwards with `positional.remove(thread_id)`
    — a value-based removal, which always deletes the FIRST matching
    string. Whenever the chosen thread_id happened to equal an earlier
    positional arg (e.g. `python main.py "urgent" coder --thread urgent`,
    where the thread name coincides with the prompt text), it silently
    removed the wrong token — corrupting which value ended up as the
    prompt vs. the persona. Tracking --thread's own argv INDEX (and the
    index right after it) instead makes exclusion unambiguous regardless
    of what the values are.
    """
    argv = sys.argv[1:]

    thread_id = "main"
    exclude_idx: set = set()
    if "--thread" in argv:
        i = argv.index("--thread")
        exclude_idx.add(i)
        if i + 1 < len(argv):
            thread_id = argv[i + 1]
            exclude_idx.add(i + 1)

    flags      = {a for j, a in enumerate(argv) if j not in exclude_idx and a.startswith("--")}
    positional = [a for j, a in enumerate(argv) if j not in exclude_idx and not a.startswith("--")]

    return {"flags": flags, "positional": positional, "thread_id": thread_id}


def main() -> None:
    parsed = _parse_args()
    flags = parsed["flags"]
    positional = parsed["positional"]

    # ── --models flag (FIX-4) ──────────────────────────────────────────────────
    if "--models" in flags:
        show_models()
        return

    # ── One-shot mode: python main.py "prompt" [persona] ──────────────────────
    if positional:
        prompt = positional[0]
        persona = positional[1] if len(positional) > 1 else runtime_config.active_persona
        runtime_config.set_persona(persona)

        # FIX-6: unique thread per one-shot run unless --thread was specified
        thread_id = parsed["thread_id"]
        if thread_id == "main":
            thread_id = f"oneshot-{uuid.uuid4().hex[:8]}"

        async def _one_shot() -> None:
            async with lifespan_checkpointer() as checkpointer:
                graph = create_beaver_graph(checkpointer)
                try:  # FIX-5
                    await run(prompt, graph=graph, persona=persona, thread_id=thread_id)
                except KeyboardInterrupt:
                    print("\n[BEAVER] Interrupted.")
                except Exception as exc:
                    print(f"\n[ERROR] Agent run failed: {exc}")
                    raise SystemExit(1) from exc
                finally:
                    # FIX-7: tear down MCP connections and zero the module-level
                    # cache so a subsequent call in the same process (e.g. from
                    # tests) never fast-paths onto stale, already-closed handles.
                    import skills.mcp_loader as _mcp_mod
                    await _mcp_mod._release_mcp_connections()
                    _mcp_mod._cached_persona = ""
                    _mcp_mod._cached_tools   = []

        asyncio.run(_one_shot())
        return

    # ── Default: full interactive CLI with /models /skills /persona /model ─────
    show_models()   # FIX-4: show available models on CLI startup
    from cli.ui import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
