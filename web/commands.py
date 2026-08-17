"""
Slash-command handlers for server.py — ported 1:1 from cli/ui.py's
_cmd_models / _cmd_personas / _cmd_plugins / _cmd_agents / _cmd_mcp /
_cmd_mcp_check / _cmd_hud, plus /dir, /model, /persona, /reset.

Same commands, same underlying calls (skills.plugin_loader, skills.a2a,
skills.mcp_loader, agent.config.get_active_config()) — this module just
returns plain dicts instead of printing Rich tables, so server.py can
ship them to the browser as JSON. If you add a command to cli/ui.py, port
it here the same way: copy the data-gathering lines, drop the
console.print() calls, return a dict instead.

Every handler returns one of:
    {"kind": "table", "title": "...", "columns": [...], "rows": [[...]], "footer": "..."}
    {"kind": "info",  "title": "...", "lines": [...]}
    {"kind": "error", "message": "..."}
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from agent.config import get_active_config, PERSONAS_PROMPTS_DIR
from skills import PERSONA_TOOLS
# [FIX-1] Reuse the SAME path resolution as memory/checkpointer.py instead of
# a separately hardcoded "beaver_memory.sqlite" literal — if SQLITE_CHECKPOINT_PATH
# is ever set, the old hardcoded path would silently look at the wrong file
# and every session command would report "not found" even with real sessions.
from memory.checkpointer import _DB_PATH as _SESSIONS_DB_PATH


# ── /models ───────────────────────────────────────────────────────────────────

async def cmd_models(cfg: Any = None) -> Dict[str, Any]:
    cfg = cfg or get_active_config()
    try:
        models = await cfg.list_ollama_models_async()
    except Exception as e:
        return {"kind": "error", "message": f"ollama unreachable — {e}"}
    if not models:
        return {"kind": "info", "title": "models", "lines": ["no models found"]}
    rows = [[m, "active" if m == cfg.model else ""] for m in sorted(models)]
    return {
        "kind": "table",
        "title": "models",
        "columns": ["model", ""],
        "rows": rows,
        "footer": "switch with /model <name>",
    }


# ── /personas ─────────────────────────────────────────────────────────────────

async def cmd_personas(cfg: Any = None) -> Dict[str, Any]:
    cfg = cfg or get_active_config()
    current = cfg.active_persona or "standard"
    rows = []
    for name in PERSONA_TOOLS.keys():
        active = name == current
        prompt_ok = (PERSONAS_PROMPTS_DIR / f"{name}.md").exists()
        rows.append([name, "active" if active else "", "" if prompt_ok else "no prompt file"])
    return {
        "kind": "table",
        "title": "personas",
        "columns": ["persona", "", ""],
        "rows": rows,
        "footer": "switch with /persona <name>",
    }


# ── /plugins ──────────────────────────────────────────────────────────────────

def cmd_plugins() -> Dict[str, Any]:
    try:
        from skills.plugin_loader import list_plugins
        plugins = list_plugins()
    except Exception as exc:
        return {"kind": "error", "message": f"error loading plugins: {exc}"}

    if not plugins:
        return {
            "kind": "info",
            "title": "plugins",
            "lines": ["no plugins found — drop .py files into plugins/"],
        }

    rows = []
    enabled_count = 0
    for p in plugins:
        if "error" in p:
            rows.append([p["name"], "—", "error", p["error"]])
            continue
        enabled = p.get("enabled", True)
        persona_val = p.get("persona", "?")
        persona_str = ", ".join(persona_val) if isinstance(persona_val, list) else str(persona_val)
        tools_str = ", ".join(p.get("tools", [])) or "none"
        if enabled:
            enabled_count += 1
        rows.append([p["name"], persona_str, "yes" if enabled else "no", tools_str])

    return {
        "kind": "table",
        "title": "plugins",
        "columns": ["plugin", "persona", "enabled", "tools"],
        "rows": rows,
        "footer": "drop a .py file into plugins/ — no restart needed",
        "enabled_count": enabled_count,
    }


# ── /agents ───────────────────────────────────────────────────────────────────

def cmd_agents() -> Dict[str, Any]:
    try:
        from skills.a2a import _load_agent_configs, _MAX_CONCURRENT, _A2A_TIMEOUT
        configs = _load_agent_configs()
    except Exception as exc:
        return {"kind": "error", "message": f"error loading agents: {exc}"}

    if not configs:
        return {
            "kind": "info",
            "title": "agents",
            "lines": ["no agents configured — set A2A_AGENTS in .env or pull Ollama models"],
        }

    rows = [[c.get("name", "?"), c.get("persona", "standard"), c.get("model", "?")] for c in configs]
    return {
        "kind": "table",
        "title": "agents",
        "columns": ["name", "persona", "model"],
        "rows": rows,
        "footer": f"max_concurrent={_MAX_CONCURRENT}  timeout={_A2A_TIMEOUT}s  "
                  f"— delegate with /persona orchestrator then ask normally",
        "agent_count": len(configs),
    }


# ── /tools (sidebar "active tools" panel — see SIDEBAR-1 in server.py) ────────

async def cmd_tools(cfg: Any = None) -> Dict[str, Any]:
    """Resolved tool list for the active persona.

    Mirrors skills.get_persona_tools_async's resolution order (base +
    plugins + MCP + A2A) so what's shown here is exactly what the running
    agent can call right now.
    """
    cfg = cfg or get_active_config()
    persona = cfg.active_persona or "standard"
    try:
        from skills import get_persona_tools_async
        tools = await get_persona_tools_async(persona)
    except Exception as exc:
        return {"kind": "error", "message": f"error loading tools: {exc}"}

    if not tools:
        return {"kind": "info", "title": "tools", "lines": [f"no tools resolved for persona '{persona}'"]}

    rows = []
    for t in tools:
        desc = (t.description or "").strip().splitlines()[0] if t.description else ""
        rows.append([t.name, "tool", desc[:140]])

    return {
        "kind": "table",
        "title": "tools",
        "columns": ["tool", "", ""],
        "rows": rows,
        "footer": f"{len(tools)} tool(s) resolved for persona: {persona}",
    }


# ── /memory (sidebar "memory" panel — see SIDEBAR-1 in server.py) ─────────────

async def cmd_memory(limit: int = 20, category: str = "") -> Dict[str, Any]:
    """Browse long-term memory entries directly, without a similarity query.

    Same underlying call as skills.memory_skill.list_long_term_memories,
    just returned as a dict (rows/kind) instead of a formatted string so
    server.py can ship it to the browser as JSON.
    """
    try:
        from memory.long_term import get_memory_db
        results = await get_memory_db().list_memories(limit=limit, category=category)
    except Exception as exc:
        return {"kind": "error", "message": f"error loading memory: {exc}"}

    if not results:
        scope = f" in category '{category}'" if category else ""
        return {"kind": "info", "title": "memory", "lines": [f"no memories found{scope}"]}

    rows = [[r.get("category", "general"), r.get("content", ""), r.get("id", "")] for r in results]
    return {
        "kind": "table",
        "title": "memory",
        "columns": ["category", "content", "id"],
        "rows": rows,
        "footer": f"{len(results)} memor{'y' if len(results) == 1 else 'ies'}",
    }


# ── /forget (sidebar memory panel's "forget" button + typed /forget) ──────────

async def cmd_forget(memory_id: str) -> Dict[str, Any]:
    """Delete one long-term memory by id.

    Backs both the sidebar memory panel's "forget" button (which sends
    this as a `/forget <id>` chat command) and the `/forget` slash command
    typed directly. Mirrors skills.memory_skill.forget_long_term_memory.
    """
    memory_id = (memory_id or "").strip()
    if not memory_id:
        return {"kind": "error", "message": "usage: /forget <memory_id> — get the id from the memory panel or /memory"}

    try:
        from memory.long_term import get_memory_db
        deleted = await get_memory_db().delete_memory(memory_id)
    except Exception as exc:
        return {"kind": "error", "message": f"error deleting memory: {exc}"}

    if not deleted:
        return {"kind": "error", "message": f"no memory found with id {memory_id} — it may already be deleted"}

    return {"kind": "info", "title": "forget", "lines": [f"deleted memory {memory_id}"]}


# ── /mcp, /mcp reload ─────────────────────────────────────────────────────────

async def cmd_mcp(reload: bool = False) -> Dict[str, Any]:
    from skills.mcp_loader import list_mcp_servers

    if reload:
        try:
            import skills.mcp_loader as _mcp_mod
            await _mcp_mod._release_mcp_connections()
            _mcp_mod._cached_persona = ""
            _mcp_mod._cached_tools = []

            persona = get_active_config().active_persona or "standard"
            from skills import invalidate_persona_tools_cache
            invalidate_persona_tools_cache(persona)  # [FIX-CACHE] see skills/__init__.py

            from skills.mcp_loader import get_mcp_tools
            tools = await get_mcp_tools(persona)

            if tools:
                return {
                    "kind": "info",
                    "title": "mcp reload",
                    "lines": [f"{len(tools)} tool(s) loaded: " + ", ".join(t.name for t in tools)],
                }
            return {"kind": "info", "title": "mcp reload", "lines": ["no MCP tools loaded (check MCP_SERVERS in .env)"]}
        except Exception as exc:
            return {"kind": "error", "message": f"reload failed: {exc}"}

    servers = list_mcp_servers()
    if not servers:
        return {
            "kind": "info",
            "title": "mcp",
            "lines": ["no MCP servers configured — add entries to MCP_SERVERS in .env"],
        }

    rows = []
    for srv in servers:
        persona_val = srv.get("persona", "*")
        persona_str = ", ".join(persona_val) if isinstance(persona_val, list) else str(persona_val)
        rows.append([srv.get("name", "?"), persona_str, srv.get("cmd", "")])

    import skills.mcp_loader as _mcp_mod
    cached_count = len(_mcp_mod._cached_tools)
    cached_persona = _mcp_mod._cached_persona or "—"
    connected = _mcp_mod._active_stack is not None

    return {
        "kind": "table",
        "title": "mcp",
        "columns": ["name", "persona", "command"],
        "rows": rows,
        "footer": (
            f"connections: {'live' if connected else 'not connected'}  "
            f"cached tools: {cached_count}  for persona: {cached_persona}  "
            f"— reconnect with /mcp reload"
        ),
    }


async def cmd_mcp_check() -> Dict[str, Any]:
    from skills.mcp_loader import diagnose_mcp
    report = await diagnose_mcp()
    return {"kind": "info", "title": "mcp check", "lines": report.splitlines()}


# ── /dir ──────────────────────────────────────────────────────────────────────

def cmd_dir(new_dir: str = "", cfg: Any = None) -> Dict[str, Any]:
    # [FIX] RuntimeConfig has no workspace_root / set_workspace_root — that
    # API never existed. The real, session-scoped workspace root lives in
    # skills/file_ops.py as a contextvar (get_active_workspace_root() /
    # set_session_workspace()), specifically so one WebSocket session's /dir
    # doesn't move every other concurrent session's cwd. Route there instead.
    from skills.file_ops import get_active_workspace_root, set_session_workspace

    if not new_dir:
        return {"kind": "info", "title": "workspace", "lines": [get_active_workspace_root()]}
    try:
        resolved = set_session_workspace(new_dir)
        return {
            "kind": "info",
            "title": "workspace",
            "lines": [f"workspace → {resolved}",
                      "file tools (read_file, write_file, list_directory) now operate here"],
        }
    except ValueError as e:
        return {"kind": "error", "message": str(e)}


# ── /scope ────────────────────────────────────────────────────────────────────
# [MISSING-CMD] target_scope had no way to be changed from the web UI even
# though CommandPalette.tsx already advertises "/scope" and cli/ui.py has a
# full working inline implementation. Ported 1:1 from cli/ui.py's /scope
# handling: no-arg shows the current scope, an arg replaces it (comma-
# separated IPs/CIDRs), via RuntimeConfig.set_scope() (agent/config.py).

def cmd_scope(new_scope: str = "", cfg: Any = None) -> Dict[str, Any]:
    cfg = cfg or get_active_config()
    if not new_scope:
        current = ", ".join(cfg.scope) or "(empty — all targets allowed)"
        return {"kind": "info", "title": "scope", "lines": [current]}
    try:
        cfg.set_scope(new_scope)
    except Exception as e:
        return {"kind": "error", "message": str(e)}
    return {
        "kind": "info",
        "title": "scope",
        "lines": [f"scope → {', '.join(cfg.scope)}",
                  "os_exec calls with an IP outside this list/CIDR range will be blocked"],
    }


# ── /model ────────────────────────────────────────────────────────────────────

async def cmd_model(name: str, cfg: Any = None) -> Dict[str, Any]:
    cfg = cfg or get_active_config()
    if not name:
        return {"kind": "info", "title": "model", "lines": [cfg.model]}
    try:
        import asyncio
        await asyncio.to_thread(cfg.set_model, name)
        return {"kind": "info", "title": "model", "lines": [f"model → {name}"]}
    except ValueError as e:
        return {"kind": "error", "message": str(e)}


# ── /persona ──────────────────────────────────────────────────────────────────

async def cmd_persona(name: str, cfg: Any = None) -> Dict[str, Any]:
    cfg = cfg or get_active_config()
    if not name:
        return {"kind": "info", "title": "persona", "lines": [cfg.active_persona or "standard"]}
    try:
        cfg.set_persona(name)
        prompt_path = PERSONAS_PROMPTS_DIR / f"{name}.md"
        lines = [f"persona → {name}"]
        if not prompt_path.exists():
            lines.append(f"warn: no prompt file for '{name}' — add prompts/personas/{name}.md")

        import skills.mcp_loader as _mcp_mod
        if _mcp_mod._cached_persona != name:
            await _mcp_mod._release_mcp_connections()
            _mcp_mod._cached_persona = ""
            _mcp_mod._cached_tools = []
            lines.append("mcp cache cleared — tools reload on next message")

        return {"kind": "info", "title": "persona", "lines": lines, "persona": name}
    except ValueError as e:
        return {"kind": "error", "message": str(e)}


# ── /hud ──────────────────────────────────────────────────────────────────────

def cmd_hud(session: Dict[str, Any], thread_id: str, cfg: Any = None) -> Dict[str, Any]:
    cfg = cfg or get_active_config()
    persona = cfg.active_persona or "standard"
    tools = PERSONA_TOOLS.get(persona, [])
    return {
        "kind": "hud",
        "model": cfg.model,
        "persona": persona,
        "temperature": cfg.temperature,
        "msg_count": session.get("msg_count", 0),
        "token_count": session.get("token_count", 0),
        "context_limit": cfg.num_ctx,
        "thread_id": thread_id,
        "tool_count": len(tools),
        "plugin_count": session.get("plugin_count", 0),
        "agent_count": session.get("agent_count", 0),
    }


# ── /sessions ────────────────────────────────────────────────────────────────────

def _sync_fetch_sessions() -> list:
    """Blocking sqlite3 work, isolated so it can run via asyncio.to_thread —
    see this module's [FIX-ASYNC-LOOP] note above cmd_sessions for why.
    Always returns a plain list of (thread_id, count) rows — empty if the
    DB file doesn't exist yet. All dict-shaping stays in cmd_sessions."""
    import sqlite3
    db_path = Path(_SESSIONS_DB_PATH)
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "SELECT thread_id, COUNT(*) FROM checkpoints GROUP BY thread_id "
        "ORDER BY MAX(checkpoint_id) DESC"
    )
    rows_raw = cur.fetchall()
    conn.close()
    return rows_raw


# [FIX-ASYNC-LOOP] Was a plain `def`, called synchronously in-line by
# _dispatch_command — its sqlite3.connect()/execute()/fetchall() ran
# DIRECTLY on the shared event loop thread, blocking every other
# concurrently-open websocket connection (not just this one) for the
# duration of the query. Unlike LangChain @tool functions (auto-offloaded
# to a thread by BaseTool.ainvoke() — see agent/graph.py's audit), these
# command handlers have no such automatic offloading; the blocking work
# has to be wrapped in asyncio.to_thread() explicitly. Same fix applied to
# cmd_continue (was already async def but did the same thing — being
# async doesn't make blocking code inside it non-blocking, only awaiting
# genuinely async work does) and cmd_delete_session below.
async def cmd_sessions(current_thread_id: str = "") -> Dict[str, Any]:
    """List all saved conversation sessions (thread_ids).

    Returns kind="sessions" (not "table") so the frontend can render a
    switch/delete control per row instead of a plain table — /sessions
    typed as a raw slash command still works fine, the frontend just
    special-cases this kind for the sessions panel triggered by the header
    button.
    """
    try:
        import asyncio
        rows_raw = await asyncio.to_thread(_sync_fetch_sessions)
    except Exception as e:
        return {"kind": "error", "message": f"error loading sessions: {e}"}

    if not rows_raw:
        return {"kind": "sessions", "title": "sessions", "rows": [], "current": current_thread_id}

    rows = [
        {
            "thread_id": tid,
            "type": "web" if tid.startswith("web-") else "cli",
            "checkpoints": count,
            "current": tid == current_thread_id,
        }
        for tid, count in rows_raw
    ]
    return {
        "kind": "sessions",
        "title": "sessions",
        "rows": rows,
        "current": current_thread_id,
        "footer": "resume with /continue <thread_id> — delete with /delete <thread_id>",
    }


# ── /continue ────────────────────────────────────────────────────────────────────

def _sync_check_thread_exists(thread_id: str) -> bool:
    """Blocking sqlite3 work, isolated for asyncio.to_thread — see
    [FIX-ASYNC-LOOP] above cmd_sessions."""
    import sqlite3
    db_path = Path(_SESSIONS_DB_PATH)
    if not db_path.exists():
        return False
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread_id,))
    exists = cur.fetchone()[0] > 0
    conn.close()
    return exists


async def cmd_continue(thread_id: str, checkpointer: Any = None) -> Dict[str, Any]:
    """Switch to an existing session by thread_id, and replay its past
    messages so the browser's chat log actually shows the resumed
    conversation instead of staying blank.

    [FIX-2] Previously this only validated the thread existed and told the
    caller to switch to it going forward — the backend correctly resumed
    the thread (checkpointer state is keyed by thread_id regardless), but
    nothing sent the PAST conversation back to the browser, so "continuing"
    a session looked identical to starting a blank new one. Now fetches the
    checkpointed message history via the checkpointer itself and returns it
    for the frontend to replay as message bubbles before the user sends
    anything new.

    [FIX-1] Also now checks the correct, configurable DB path instead of a
    hardcoded literal (see module-level _SESSIONS_DB_PATH import).

    [FIX-ASYNC-LOOP] Was already `async def`, but did sqlite3.connect() and
    friends directly in the function body — declaring a function async
    doesn't make blocking code inside it non-blocking, it just meant this
    was blocking the shared event loop from inside a coroutine instead of
    a plain function, same practical effect. Now offloaded via
    asyncio.to_thread(), same as cmd_sessions/cmd_delete_session.

    Always returns a plain dict now (previously returned a tuple on
    success and a dict on error — inconsistent shape the caller had to
    special-case). Success responses carry "switch_to_thread" and
    "replay_messages" keys instead.
    """
    if not thread_id:
        return {"kind": "error", "message": "usage: /continue <thread_id>"}

    try:
        import asyncio
        exists = await asyncio.to_thread(_sync_check_thread_exists, thread_id)
        if not exists:
            return {"kind": "error", "message": f"session '{thread_id}' not found"}
    except Exception as e:
        return {"kind": "error", "message": f"error loading session: {e}"}

    # [FIX-2] Fetch and replay the actual conversation.
    replay: list[Dict[str, str]] = []
    if checkpointer is not None:
        try:
            from agent.graph import _extract_json_tool_call
            from langchain_core.messages import HumanMessage, AIMessage

            config = {"configurable": {"thread_id": thread_id}}
            tup = await checkpointer.aget_tuple(config)
            if tup and tup.checkpoint:
                messages = tup.checkpoint.get("channel_values", {}).get("messages", [])
                for m in messages:
                    if isinstance(m, HumanMessage):
                        content = str(m.content).strip()
                        if content:
                            replay.append({"role": "user", "content": content})
                    elif isinstance(m, AIMessage):
                        content = str(m.content).strip()
                        # Skip empty turns and raw JSON tool-call blobs — same
                        # suppression the live stream already applies, so a
                        # replayed session looks the same as it did live.
                        if content and _extract_json_tool_call(content) is None:
                            replay.append({"role": "beaver", "content": content})
        except Exception as e:
            # Non-fatal — still switch threads even if replay fails, just
            # tell the user their history didn't come along.
            return {
                "kind": "info",
                "title": "continue",
                "lines": [f"resumed session: {thread_id}", f"warn: could not replay history — {e}"],
                "switch_to_thread": thread_id,
                "replay_messages": [],
            }

    return {
        "kind": "info",
        "title": "continue",
        "lines": [f"resumed session: {thread_id} ({len(replay)} message(s) replayed)"],
        "switch_to_thread": thread_id,
        "replay_messages": replay,
    }


# ── /delete ──────────────────────────────────────────────────────────────────────

def _sync_delete_session(thread_id: str) -> Dict[str, Any]:
    """Blocking sqlite3 work, isolated for asyncio.to_thread — see
    [FIX-ASYNC-LOOP] above cmd_sessions. Returns {"not_found": True} or
    {"deleted": N} rather than raising/returning error dicts directly, so
    cmd_delete_session (which knows the right "kind"/"message" shape) stays
    the single source of truth for the response format."""
    import sqlite3
    db_path = Path(_SESSIONS_DB_PATH)
    if not db_path.exists():
        return {"not_found": True}

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread_id,))
    if cur.fetchone()[0] == 0:
        conn.close()
        return {"not_found": True}

    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = {row[0] for row in cur.fetchall()}
    deleted = 0
    for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
        if table in existing_tables:
            cur.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
            deleted += cur.rowcount
    conn.commit()
    conn.close()
    return {"deleted": deleted}


async def cmd_delete_session(thread_id: str, current_thread_id: str = "") -> Dict[str, Any]:
    """Permanently delete a saved session's checkpoints from the sqlite db.

    Refuses to delete the session currently attached to this websocket —
    switch to a different one first (/continue <other_id>) or hit reset to
    start a fresh thread, then delete the old one. Wipes every table the
    langgraph-checkpoint-sqlite schema may have written to (checkpoints,
    checkpoint_writes, checkpoint_blobs) — older/newer package versions
    don't all have every table, so each DROP is existence-checked first.

    [FIX-ASYNC-LOOP] Was a plain `def`, blocking the shared event loop for
    every concurrently-open connection during the delete — see the note
    above cmd_sessions. Now async, offloaded via asyncio.to_thread().
    """
    if not thread_id:
        return {"kind": "error", "message": "usage: /delete <thread_id>"}
    if thread_id == current_thread_id:
        return {
            "kind": "error",
            "message": "can't delete the active session — /continue to another session "
                       "or hit reset first, then delete it",
        }

    try:
        import asyncio
        result = await asyncio.to_thread(_sync_delete_session, thread_id)
    except Exception as e:
        return {"kind": "error", "message": f"error deleting session: {e}"}

    if result.get("not_found"):
        return {"kind": "error", "message": f"session '{thread_id}' not found"}

    return {
        "kind": "info",
        "title": "delete",
        "lines": [f"deleted session: {thread_id} ({result['deleted']} row(s))"],
        "deleted_thread_id": thread_id,
    }


# ── /help ─────────────────────────────────────────────────────────────────────

def cmd_help() -> Dict[str, Any]:
    return {
        "kind": "table",
        "title": "available commands",
        "columns": ["command", "description"],
        "rows": [
            ["/personas", "list personas — /persona <name> to switch"],
            ["/models", "list Ollama models — /model <name> to switch"],
            ["/plugins", "list active plugins"],
            ["/agents", "list A2A sub-agents"],
            ["/mcp", "show MCP servers"],
            ["/mcp reload", "reconnect MCP servers"],
            ["/mcp check", "MCP diagnostic"],
            ["/sessions", "list saved conversation sessions"],
            ["/continue <id>", "resume a previous session"],
            ["/delete <id>", "permanently delete a saved session"],
            ["/hud", "show status"],
            ["/dir", "show workspace — /dir <path> to change"],
            ["/scope", "show target scope — /scope <ips/cidrs> to change"],
            ["/reset", "reset counters"],
            ["/help", "show this list"],
        ],
        "footer": "ESC stops turn • Persona dropdown above",
    }


# ── Dispatch table ────────────────────────────────────────────────────────────

COMMANDS = {
    "models": cmd_models,
    "personas": cmd_personas,
    "skills": cmd_personas,
    "plugins": cmd_plugins,
    "agents": cmd_agents,
    "tools": cmd_tools,
    "memory": cmd_memory,
    "forget": cmd_forget,
    "mcp": cmd_mcp,
    "dir": cmd_dir,
    "scope": cmd_scope,
    "model": cmd_model,
    "persona": cmd_persona,
    "skill": cmd_persona,
    "sessions": cmd_sessions,
    "continue": cmd_continue,
    "delete": cmd_delete_session,
    "hud": cmd_hud,
    "status": cmd_hud,
    "monitor": cmd_hud,
    "help": cmd_help,
}