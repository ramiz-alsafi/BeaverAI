"""
Beaver Web UI — FastAPI + WebSocket backend

Third interface alongside CLI and TUI — built specifically to fix Arabic/RTL
rendering, which terminals cannot do reliably during live streaming (BiDi
reordering needs the full paragraph and terminals only get it right once
text is static — see conversation notes). Browsers handle this natively via
`dir="auto"` and re-render correctly on every incremental update, which is
the actual fix; this server exists to deliver that, not to replace CLI/TUI.

Reuses the exact same graph/checkpointer/astream_events pattern as
cli/ui.py and tui/app.py — no duplicated agent logic, just a different
transport (WebSocket instead of a terminal Live region).

Run directly (no Docker needed):
    python web/server.py
    # or
    uvicorn web.server:app --host 127.0.0.1 --port 8000

Host/port and TLS are configurable via .env (BEAVER_WEB_HOST, BEAVER_WEB_PORT,
BEAVER_TLS_CERT, BEAVER_TLS_KEY — see .env.example and _resolve_tls() below)
when running `python web/server.py` directly. If you invoke uvicorn yourself
instead, pass --ssl-certfile/--ssl-keyfile/--host/--port to it directly —
_resolve_tls() only runs in the __main__ block.

Dev mode: no production build in web/frontend/dist/ yet — this backend
auto-starts a Vite dev server as a child process ([VITE-1]); open the URL
IT prints (usually http://127.0.0.1:5173), not this server's own port.

Production mode: run `npm run build` in web/frontend/ first. Once
web/frontend/dist/index.html exists, this server serves it directly —
single port, single process, no Vite involved. Open this server's own URL
(http://127.0.0.1:8000) in that case.

Protocol (all messages are JSON over the WebSocket at /ws)
------------------------------------------------------------
Client -> Server:
    {"type": "user_message", "content": "..."}
    {"type": "stop"}                                           cancel the in-progress turn (STOP-1)
    {"type": "get_config"}                                     request current settings (CFG-1)
    {"type": "update_config", "changes": {"model": "...", ...}}  apply settings-panel changes (CFG-1)
    {"type": "list_personas"}                                  request available persona names (PERSONA-1)
    {"type": "add_persona", "name": "...", "content": "..."}   create a new persona file (PERSONA-1;
                                                                 content optional, falls back to a starter template)
    {"type": "list_plugins"}                                   request plugin list (SKILLS-1)
    {"type": "toggle_plugin", "name": "...", "enabled": true}  enable/disable a plugin (SKILLS-1)
    {"type": "list_tool_manuals"}                              request tool_manuals list (SKILLS-1)
    {"type": "add_tool_manual", "name": "...", "content": "..."}  create a new tool manual (SKILLS-1)
    {"type": "list_active_tools"}                               request the active persona's resolved
                                                                 tool set for the sidebar (SIDEBAR-1)
    {"type": "list_memories", "limit": N, "category": "..."}   request recent long-term memories (SIDEBAR-1)
    {"type": "list_sessions"}                                   request saved sessions for the sidebar,
                                                                 without touching the chat log (SIDEBAR-1)
    {"type": "tail_log", "which": "agent"|"traces"|"uvicorn", "lines": N}
                                                                 one-shot read of the last N lines (LOGS-1)
    {"type": "watch_log", "which": "agent"|"traces"|"uvicorn"}  start live-following that file — pushes
                                                                 log_line frames as new lines land (LOGS-1)
    {"type": "unwatch_log", "which": "agent"|"traces"|"uvicorn"} stop following it (LOGS-1)

Server -> Client:
    {"type": "gen_start"}                                    new generation begins
    {"type": "token", "text": "..."}                          streamed text chunk
    {"type": "retract_pending"}                                this generation turned out to be a
                                                                 JSON tool call — discard its streamed
                                                                 text, don't show it as a message
    {"type": "gen_end"}                                        generation finished as real narration —
                                                                 finalize the pending bubble
    {"type": "tool_start", "name": "...", "args": "..."}       tool call started
    {"type": "tool_end", "name": "...", "output": "...", "ok": true}
    {"type": "tokens_used", "count": N, "limit": N}
    {"type": "stopped"}                                        turn was cancelled (STOP-1) — finalize
                                                                 whatever partial content streamed so far
    {"type": "done"}                                           turn fully complete
    {"type": "error", "message": "..."}
    {"type": "command_result", "kind": "table"|"info"|"error"|"hud"|"sessions", ...}
                                                                slash-command response (see web/commands.py);
                                                                may also carry "switch_to_thread" and
                                                                "replay_messages" (from /continue)
    {"type": "config_state", "live": {...}, "restart": {...}}  current settings, in response to get_config
    {"type": "config_updated", "applied": {...}, "queued_for_restart": {...}, "errors": {...}}
    {"type": "status_update", "model": "...", "persona": "...", ...}
                                                                live banner data — pushed on connect and
                                                                whenever model/persona actually changes,
                                                                not on a timer (BANNER-1)
    {"type": "personas_list", "personas": [...]}                available persona names (PERSONA-1)
    {"type": "persona_added", "ok": true, "name": "..."}         result of add_persona (PERSONA-1)
    {"type": "plugins_list", "plugins": [{"name":..., "persona":..., "enabled":..., "tools":[...]}]}
    {"type": "plugin_toggled", "ok": true, "name": "...", "enabled": true}
    {"type": "tool_manuals_list", "manuals": [...]}
    {"type": "tool_manual_added", "ok": true, "name": "..."}
    {"type": "active_tools_list", "kind": "table", ...}         reply to list_active_tools (SIDEBAR-1)
    {"type": "memories_list", "kind": "table"|"info", ...}      reply to list_memories (SIDEBAR-1)
    {"type": "sessions_list", "kind": "sessions", ...}          reply to list_sessions (SIDEBAR-1)
    {"type": "log_tail", "which": "...", "kind": "ok"|"info"|"error", "lines": [...], ...}
                                                                reply to tail_log (LOGS-1)
    {"type": "log_line", "which": "...", "line": "..."}         one new line from a watch_log follower
                                                                (LOGS-1)
"""
import asyncio
import hmac
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, Optional

# [FIX-1] Running `python web/server.py` directly only puts web/ itself on
# sys.path, not the project root — so `agent`, `memory`, etc. (siblings of
# web/) can't be found. Insert the project root explicitly so this script
# works both ways: `python web/server.py` from anywhere, or
# `python -m web.server` / `uvicorn web.server:app` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.config import (
    runtime_config,
    RuntimeConfig,
    get_active_config,
    set_session_config,
    PERSONAS_PROMPTS_DIR,
)
from agent.graph import create_beaver_graph, _evict_cached_llm
from agent.telemetry import get_callbacks
from memory.checkpointer import lifespan_checkpointer
from web.commands import COMMANDS

logger = logging.getLogger("beaver.web")

# [AUTH-1] Shared-secret gate for the web UI. Unset by default so a plain
# `python web/server.py` on localhost keeps working with zero setup — the
# moment you plan to bind this to 0.0.0.0 or any real network, set
# BEAVER_WEB_TOKEN in .env (see .env.example: `openssl rand -hex 32`) and
# every page load / WebSocket connection must present it as ?token=...
# The frontend (useBeaverSocket.ts) already forwards whatever token the
# page itself was loaded with onto the WS URL — nothing else to wire up.
_WEB_TOKEN = os.getenv("BEAVER_WEB_TOKEN", "").strip()


def _token_valid(candidate: Optional[str]) -> bool:
    """Constant-time compare against BEAVER_WEB_TOKEN.

    Always returns True when no token is configured — that's the explicit
    "local, no auth" mode. Never compares with plain `==`, which short-
    circuits on the first differing byte and leaks timing information an
    attacker could use to guess the token character by character.
    """
    if not _WEB_TOKEN:
        return True
    return hmac.compare_digest(candidate or "", _WEB_TOKEN)


_FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"   # [VITE-1]
_DIST_DIR = _FRONTEND_DIR / "dist"   # npm run build output — production serving

app = FastAPI(title="Beaver Web UI")
# Only mount if a build actually exists — during pure Vite-dev-server usage
# (BEAVER_NO_VITE unset, hitting the :5173 URL directly) there is no dist/
# yet, and StaticFiles would crash the whole app at import time otherwise.
# Vite's own build emits an /assets/ prefix (its default assetsDir) — that's
# what the built index.html actually references.
if (_DIST_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(_DIST_DIR / "assets")), name="assets")

_exit_stack = AsyncExitStack()
_graph: Optional[Any] = None
_checkpointer: Optional[Any] = None   # [CMD-1] raw checkpointer, needed by /continue for history replay
_vite_process: Optional[subprocess.Popen] = None   # [VITE-1]


def _spawn_vite_dev_server() -> Optional[subprocess.Popen]:
    """[VITE-1] Launch `npm run dev` (Vite) as a child process of server.py
    so `python server.py` is the ONLY command needed during frontend dev —
    no second terminal. Only fires when web/frontend/ actually exists and
    hasn't been disabled with BEAVER_NO_VITE=1 (set that if you'd rather
    run Vite yourself, e.g. to see its own terminal output more directly).
    Not called at all once a production build exists — see _startup().

    This does NOT change what port the browser hits — Vite still owns its
    own dev port (default 5173) with its own HMR websocket, which is how
    Vite is designed to work; proxying that THROUGH this backend's port
    would fight Vite's own client-side HMR socket for no real benefit. In
    production (`npm run build`), there is no Vite process at all —
    server.py serves the built dist/ directly, single port, single process.
    """
    if os.getenv("BEAVER_NO_VITE") == "1":
        return None
    if not (_FRONTEND_DIR / "package.json").exists():
        return None  # no frontend/ found

    npm = shutil.which("npm")
    if not npm:
        logger.warning("[VITE-1] npm not found on PATH — skipping auto-start; run `npm run dev` in web/frontend/ yourself.")
        return None

    logger.info("[VITE-1] Starting Vite dev server (web/frontend) …")
    try:
        proc = subprocess.Popen(
            [npm, "run", "dev"],
            cwd=str(_FRONTEND_DIR),
            stdout=None,   # inherit this process's stdout/stderr so Vite's
            stderr=None,   # own log lines (ready/errors/HMR) still show up
        )
        return proc
    except Exception as exc:
        logger.warning("[VITE-1] Failed to start Vite: %s — run `npm run dev` in web/frontend/ yourself.", exc)
        return None


def _stop_vite_dev_server() -> None:
    global _vite_process
    if _vite_process is None:
        return
    _vite_process.terminate()
    try:
        _vite_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _vite_process.kill()
    _vite_process = None


@app.on_event("startup")
async def _startup() -> None:
    global _graph, _checkpointer, _vite_process

    # [AUTH-1] This has to be loud and unmissable — a token that silently
    # failed to load (stray shell-level env var shadowing .env, a typo in
    # the key name, forgetting to save .env before restarting) means the
    # server comes up wide open with no other signal that anything's wrong.
    # `python-dotenv`'s load_dotenv() does NOT override a variable that
    # already exists in the process environment (override=False by
    # default) — if BEAVER_WEB_TOKEN is already set to "" in your shell
    # session from earlier testing, the .env value is silently ignored.
    if _WEB_TOKEN:
        masked = f"{_WEB_TOKEN[:4]}...{_WEB_TOKEN[-4:]}" if len(_WEB_TOKEN) > 8 else "***"
        print(f"[AUTH-1] Web token protection: ENABLED  (token: {masked}, {len(_WEB_TOKEN)} chars)")
    else:
        print(
            "[AUTH-1] Web token protection: DISABLED — BEAVER_WEB_TOKEN is not set "
            "(or loaded empty). Anyone who can reach this port has full access, "
            "including os_exec on coder/pentester/orchestrator personas. "
            "Fine for 127.0.0.1-only use; set BEAVER_WEB_TOKEN in .env before "
            "binding this to 0.0.0.0 or any real network."
        )

    _attach_uvicorn_file_handler()  # [LOGS-1] additive — console logging untouched

    _checkpointer = await _exit_stack.enter_async_context(lifespan_checkpointer())
    _graph = create_beaver_graph(_checkpointer)
    # Only auto-spawn Vite when there's no production build to serve
    # instead — if dist/ exists (you ran `npm run build`), that's the
    # single-port production path: no Vite process, this backend alone
    # serves everything.
    if not (_DIST_DIR / "index.html").is_file():
        _vite_process = _spawn_vite_dev_server()  # [VITE-1]
        if _vite_process:
            logger.info("[VITE-1] Vite dev server starting — open the URL it prints (usually http://localhost:5173).")
    else:
        logger.info("[SERVER] Serving production build from %s — single port, open this server's own URL directly.", _DIST_DIR)


@app.on_event("shutdown")
async def _shutdown() -> None:
    _stop_vite_dev_server()  # [VITE-1]
    await _exit_stack.aclose()


@app.get("/", response_model=None)
async def index() -> FileResponse | HTMLResponse:
    # [AUTH-1] No token check here anymore — this page is just the static
    # app shell (no secrets baked into it), and the actual gate is the
    # WebSocket's first "auth" frame below. Keeping a plaintext token out
    # of the URL means it never ends up in browser history, this server's
    # own access logs, or a Referer header.
    index_path = _DIST_DIR / "index.html"
    if not index_path.is_file():
        return HTMLResponse(
            "<pre style='font:14px monospace;padding:2rem'>"
            "No production build found at web/frontend/dist/.\n\n"
            "For development: this backend auto-starts a Vite dev server —\n"
            "check this process's logs for the URL it printed (usually\n"
            "http://localhost:5173) and open THAT instead of this page.\n\n"
            "For production: run `npm run build` inside web/frontend/, then\n"
            "reload this page."
            "</pre>",
            status_code=503,
        )
    return FileResponse(str(index_path))


def _active_persona() -> str:
    # [FIX-10] get_active_config() resolves to this connection's own
    # RuntimeConfig (bound via set_session_config in websocket_endpoint),
    # not the global singleton — so each tab reports its own persona.
    return getattr(get_active_config(), "persona", "standard") or "standard"


def _stringify_tool_output(output: Any) -> str:
    if output is None:
        return ""
    content = getattr(output, "content", None)
    if content is not None:
        if isinstance(content, list):
            return " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return str(content)
    return str(output)


# ── Settings panel — [CFG-1] ────────────────────────────────────────────────
# Fields that apply to the running process instantly (no Ollama model
# reload / process restart needed) vs. fields that only take effect on the
# NEXT restart because they control how Ollama loads the model into VRAM,
# or are read once at startup (chroma_path, embedding_model, base_url).
# Getting this split wrong means either lying about something applying live
# when it didn't, or needlessly demanding a restart for something that
# didn't need one — see runtime_config's own field comments in config.py.
LIVE_FIELDS    = {"model", "temperature", "persona", "max_loops", "max_output_chars", "scope"}
RESTART_FIELDS = {
    "num_ctx":         "OLLAMA_NUM_CTX",
    "num_gpu":         "OLLAMA_NUM_GPU",
    "num_thread":      "OLLAMA_NUM_THREAD",
    "num_batch":       "OLLAMA_NUM_BATCH",
    "keep_alive":      "OLLAMA_KEEP_ALIVE",
    "low_vram":        "OLLAMA_LOW_VRAM",
    "base_url":        "OLLAMA_BASE_URL",
    "chroma_path":     "CHROMA_PATH",
    "embedding_model": "EMBEDDING_MODEL",
}
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _read_config_state() -> Dict[str, Any]:
    cfg = get_active_config()
    live = {f: getattr(cfg, f, None) for f in LIVE_FIELDS}
    # Restart-required fields (num_ctx, num_gpu, base_url, chroma_path, ...)
    # are process-wide by design (they control how Ollama loads the model
    # into VRAM / where the vector store lives) — always read off the
    # global singleton, not the per-session config, so the settings panel
    # shows the same restart-pending values in every tab.
    restart = {f: getattr(runtime_config, f, None) for f in RESTART_FIELDS}
    return {"live": live, "restart": restart}


def _update_env_file(changes: Dict[str, str]) -> None:
    """Write restart-required changes to .env, preserving every other line.
    Same find-the-line-or-append approach used throughout this project for
    MCP_SERVERS earlier — a single KEY=value line per variable.

    [SEC-1] Values are sanitized before writing. Without this, a
    settings-panel value containing a newline (e.g. num_ctx = "5\\nEVIL=1")
    would inject an arbitrary extra line into .env — which load_dotenv()
    reads back on every process start, so a value submitted through this
    endpoint could plant an arbitrary environment variable read anywhere
    else in the app. Reject rather than silently strip: a value that needs
    a newline was never a valid single-line .env value to begin with.
    """
    for key, value in changes.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"Invalid value for {key}: must not contain newlines.")

    lines = _ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True) if _ENV_PATH.exists() else []
    remaining = dict(changes)
    for i, line in enumerate(lines):
        for key in list(remaining):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={remaining.pop(key)}\n"
                break
    for key, value in remaining.items():
        lines.append(f"{key}={value}\n")
    _ENV_PATH.write_text("".join(lines), encoding="utf-8")


def _apply_config_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a batch of settings-panel changes. Live fields take effect on
    THIS connection's session config immediately (evicting the cached LLM
    client when model or temperature change, so the change is actually
    visible on the next call rather than silently ignored by a stale
    cached client — see agent.graph._evict_cached_llm, which itself
    resolves the session config via get_active_config()). Restart fields
    are process-wide (VRAM/model-load settings) — those still get written
    to .env and reported back as needing a restart, never silently
    pretended to apply.
    """
    cfg = get_active_config()
    applied: Dict[str, Any] = {}
    queued_for_restart: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    env_writes: Dict[str, str] = {}
    needs_llm_evict = False

    for key, value in changes.items():
        try:
            if key in LIVE_FIELDS:
                if key == "temperature":
                    value = float(value)
                    needs_llm_evict = True
                elif key == "model":
                    needs_llm_evict = True
                elif key in ("max_loops", "max_output_chars"):
                    value = int(value)
                elif key == "scope":
                    if isinstance(value, str):
                        value = [s.strip() for s in value.split(",") if s.strip()]
                setattr(cfg, key, value)
                applied[key] = value

            elif key in RESTART_FIELDS:
                env_key = RESTART_FIELDS[key]
                env_writes[env_key] = str(value)
                queued_for_restart[key] = value

            else:
                errors[key] = "unknown setting"

        except (TypeError, ValueError) as exc:
            errors[key] = str(exc)

    if needs_llm_evict:
        _evict_cached_llm()

    if env_writes:
        try:
            _update_env_file(env_writes)
        except ValueError as exc:
            # [SEC-1] A rejected value (e.g. contained a newline) shouldn't
            # crash the whole update — report it the same way a per-field
            # validation error is reported, and don't leave queued_for_restart
            # claiming these fields will apply when the write never happened.
            for key in list(queued_for_restart):
                errors[key] = str(exc)
                del queued_for_restart[key]

    return {"applied": applied, "queued_for_restart": queued_for_restart, "errors": errors}


_SERVER_START = time.time()


# ── Persona dropdown + add-new — [PERSONA-1] ────────────────────────────────
import re as _re
_SAFE_NAME = _re.compile(r"^[a-z0-9_-]{1,40}$")

_PERSONA_TEMPLATE = """You are Beaver, running as the "{name}" persona.

## Role
Describe what this persona is for — what kinds of tasks it should handle,
and what it should NOT do.

## How to work
1. Act, don't narrate. Call a tool and get real data.
2. Only use tools that are actually available — check {{{{TOOL_LIST}}}} below.
3. Never invent results. If a tool fails, report that — don't fabricate.
4. Stop when done. State the result clearly and stop.

## Available tools
{{{{TOOL_LIST}}}}
"""


def _list_personas() -> list[str]:
    if not PERSONAS_PROMPTS_DIR.exists():
        return []
    return sorted(p.stem for p in PERSONAS_PROMPTS_DIR.glob("*.md"))


def _add_persona(name: str, content: str = "") -> Dict[str, Any]:
    name = name.strip().lower()
    if not _SAFE_NAME.match(name):
        return {"ok": False, "error": "name must be lowercase letters/numbers/underscore/hyphen only, 1-40 chars"}
    path = PERSONAS_PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return {"ok": False, "error": f"persona '{name}' already exists"}
    PERSONAS_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() or _PERSONA_TEMPLATE.format(name=name), encoding="utf-8")
    return {"ok": True, "name": name}


# ── Plugins toggle + tool manuals browse/add — [SKILLS-1] ──────────────────
from skills.plugin_loader import list_plugins as _list_plugins_raw, _PLUGINS_DIR
_ENABLED_LINE = _re.compile(r"^ENABLED\s*=\s*(True|False)\s*$", _re.MULTILINE)

_TOOL_MANUALS_DIR = Path(__file__).resolve().parent.parent / "tool_manuals"
_SAFE_MANUAL_NAME = _re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")   # permits MCP-style "server__tool" names


def _list_plugins() -> list:
    return _list_plugins_raw()


def _toggle_plugin(name: str, enabled: bool) -> Dict[str, Any]:
    if not _re.match(r"^[a-zA-Z0-9_]{1,60}$", name):
        return {"ok": False, "error": "invalid plugin name"}
    path = _PLUGINS_DIR / f"{name}.py"
    if not path.exists():
        return {"ok": False, "error": f"plugin '{name}' not found"}

    source = path.read_text(encoding="utf-8")
    new_value = "True" if enabled else "False"
    if not _ENABLED_LINE.search(source):
        return {"ok": False, "error": f"plugin '{name}' has no 'ENABLED = True/False' line to toggle — edit it manually"}
    source = _ENABLED_LINE.sub(f"ENABLED = {new_value}", source)
    path.write_text(source, encoding="utf-8")

    # [SKILLS-1] load_plugin_tools()'s cache invalidates on the PLUGINS
    # DIRECTORY's mtime, not individual file mtimes — editing a file's
    # contents alone does not change its parent directory's mtime on most
    # filesystems, so the toggle would silently not take effect until
    # something else touched the directory. Force it explicitly.
    os.utime(_PLUGINS_DIR, None)

    return {"ok": True, "name": name, "enabled": enabled}


def _list_tool_manuals() -> list[str]:
    if not _TOOL_MANUALS_DIR.exists():
        return []
    return sorted(p.stem for p in _TOOL_MANUALS_DIR.glob("*.md"))


def _add_tool_manual(name: str, content: str) -> Dict[str, Any]:
    name = name.strip()
    if not _SAFE_MANUAL_NAME.match(name):
        return {"ok": False, "error": "name must be letters/numbers/underscore/hyphen/dot only, 1-80 chars"}
    if not content.strip():
        return {"ok": False, "error": "manual content cannot be empty"}
    path = _TOOL_MANUALS_DIR / f"{name}.md"
    if path.exists():
        return {"ok": False, "error": f"manual '{name}' already exists"}
    _TOOL_MANUALS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return {"ok": True, "name": name}


# ── Log/trace tailing for the Activity panel — [LOGS-1] ─────────────────────
#
# Three plain append-only files, read-only from this server's point of view:
#   agent          agent.log           — set up by agent/telemetry.py
#   traces         beaver_traces.log   — set up by agent/telemetry.py / local_tracer.py
#   uvicorn        uvicorn.log         — new; see _attach_uvicorn_file_handler()
#     below, which ADDS a file handler to uvicorn's own loggers without
#     touching its existing console output.
#
# _PROJECT_ROOT mirrors agent/telemetry.py's own constant so both sides
# agree on where these files live regardless of cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

LOG_FILES: Dict[str, Path] = {
    "agent": _PROJECT_ROOT / "agent.log",
    "traces": _PROJECT_ROOT / "beaver_traces.log",
    "uvicorn": _PROJECT_ROOT / "uvicorn.log",
}


def _attach_uvicorn_file_handler() -> None:
    """[LOGS-1] Uvicorn logs to stderr/stdout by default and never touches a
    file — there's nothing to tail. This bolts on a FileHandler alongside
    uvicorn's existing console handlers so `uvicorn.log` starts filling up
    too. Purely additive: console output is untouched, nothing else about
    uvicorn's logging config changes. Safe to call more than once (guarded
    by a marker attribute on the logger).
    """
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        ulogger = logging.getLogger(name)
        if getattr(ulogger, "_beaver_file_handler_attached", False):
            continue
        handler = logging.FileHandler(str(LOG_FILES["uvicorn"]), mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        ulogger.addHandler(handler)
        ulogger._beaver_file_handler_attached = True  # type: ignore[attr-defined]


def _tail_lines_efficient(path: Path, max_lines: int, chunk_size: int = 65536) -> list[str]:
    """[LOGS-1] Read the last `max_lines` lines of a file WITHOUT loading
    the whole thing into memory first.

    The naive `f.readlines()[-max_lines:]` this replaced works fine on a
    fresh log but agent.log/beaver_traces.log have no rotation — on a
    long-running prod instance they grow unbounded, and every panel-open
    or tab-switch would otherwise mean reading a multi-hundred-MB file
    just to throw away all but the last 200 lines. This instead seeks
    backwards from the end in chunk_size blocks, stopping as soon as it
    has collected enough newlines — cost is proportional to the tail
    requested, not to file size.
    """
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        data = b""
        newline_count = 0
        while pos > 0 and newline_count <= max_lines:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size) + data
            newline_count = data.count(b"\n")
        text = data.decode("utf-8", errors="replace")
        return text.splitlines()[-max_lines:]


def _read_log_tail(which: str, max_lines: int = 200) -> Dict[str, Any]:
    """Read up to the last `max_lines` lines of one of LOG_FILES. Returns a
    dict shaped like the sidebar's other *_list replies (kind/message on
    error) so the frontend can reuse the same empty-state handling.
    """
    path = LOG_FILES.get(which)
    if path is None:
        return {"kind": "error", "message": f"unknown log '{which}'"}
    if not path.exists():
        return {"kind": "info", "lines": [], "message": f"{path.name} doesn't exist yet"}
    try:
        return {"kind": "ok", "lines": _tail_lines_efficient(path, max_lines), "size": path.stat().st_size}
    except Exception as exc:
        return {"kind": "error", "message": f"error reading {path.name}: {exc}"}


async def _watch_log(ws: WebSocket, which: str, start_size: int) -> None:
    """[LOGS-1] Background follower for one log file on one connection.
    Polls mtime/size instead of a filesystem-events watcher — these are
    low-volume local log files, not something that justifies a `watchdog`
    dependency, and polling is trivially safe to cancel. Sends only the
    bytes appended since `start_size` (or since the last tick), one
    `log_line` frame per new line, tagged with `which` so the frontend can
    route it to the right tab.
    """
    path = LOG_FILES.get(which)
    if path is None:
        return
    last_size = start_size
    try:
        while True:
            await asyncio.sleep(1.0)
            if not path.exists():
                continue
            size = path.stat().st_size
            if size < last_size:
                # File was truncated/rotated out from under us — reset and
                # just pick up from wherever it is now rather than erroring.
                last_size = 0
            if size == last_size:
                continue
            with path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(last_size)
                new_text = f.read()
            last_size = size
            for line in new_text.splitlines():
                if line:
                    await ws.send_text(json.dumps({"type": "log_line", "which": which, "line": line}))
    except asyncio.CancelledError:
        pass


async def _send_status(ws: WebSocket, state: Dict[str, Any]) -> None:
    """[BANNER-1] Push the live-banner's current status. Only called when
    something actually changed (connect, settings applied, /model or
    /persona) — not on a timer, per design choice: a banner that updates
    every few seconds regardless of activity is noisier than useful.
    """
    cfg = get_active_config()
    plugin_count = 0
    agent_count = 0
    try:
        from skills.plugin_loader import list_plugins
        plugin_count = len(list_plugins())
    except Exception:
        pass
    try:
        from skills.a2a import _load_agent_configs
        agent_count = len(_load_agent_configs())
    except Exception:
        pass

    await ws.send_text(json.dumps({
        "type":           "status_update",
        "model":          cfg.model,
        "persona":        _active_persona(),
        "temperature":    cfg.temperature,
        "context_limit":  cfg.num_ctx,
        "token_count":    state["token_count"],
        "msg_count":      state["msg_count"],
        "thread_id":      state["session_id"],
        "plugin_count":   plugin_count,
        "agent_count":    agent_count,
        "uptime_seconds": round(time.time() - _SERVER_START),
        "max_loops":      cfg.max_loops,
        "scope":          list(cfg.scope) if isinstance(cfg.scope, (list, tuple)) else cfg.scope,
    }))


async def _dispatch_command(raw_input: str, session: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    """[CMD-1] Parse a leading-slash command and dispatch to web.commands.COMMANDS.

    Each handler in COMMANDS has a different signature (some take no args,
    some take a name/path, /hud needs the session dict, /continue needs the
    raw checkpointer, /mcp needs a reload flag) — this is the single place
    that knows how to call each one correctly, mirroring how cli/ui.py's
    own command block special-cases each command inline.
    """
    body = raw_input[1:].strip()
    if not body:
        return {"kind": "error", "message": "empty command"}

    parts = body.split(maxsplit=1)
    name = parts[0].lower()
    arg  = parts[1].strip() if len(parts) > 1 else ""

    # [FIX-10] This connection's own RuntimeConfig — threaded into every
    # handler below that accepts one (cmd_models, cmd_personas, cmd_dir,
    # cmd_model, cmd_persona, cmd_hud all default to the *global*
    # runtime_config when no cfg is passed, which is exactly the bug: a
    # /model change in one browser tab must not affect any other tab).
    cfg = get_active_config()

    # /mcp reload and /mcp check are sub-forms of /mcp, not separate entries
    # in COMMANDS — handle before the generic lookup.
    if name == "mcp":
        if arg == "reload":
            return await COMMANDS["mcp"](reload=True)
        if arg == "check":
            from web.commands import cmd_mcp_check
            return await cmd_mcp_check()
        return await COMMANDS["mcp"]()

    handler = COMMANDS.get(name)
    if handler is None:
        return {"kind": "error", "message": f"unknown command: /{name}"}

    try:
        if name in ("model", "persona", "skill"):
            result = handler(arg, cfg=cfg)
        elif name in ("dir", "scope"):
            result = handler(arg, cfg=cfg)
        elif name in ("hud", "status", "monitor"):
            result = handler(session, thread_id, cfg=cfg)
        elif name in ("models", "personas", "skills", "tools"):
            result = handler(cfg=cfg)
        elif name == "memory":
            limit = 20
            category = ""
            if arg:
                for tok in arg.split():
                    if tok.isdigit():
                        limit = int(tok)
                    else:
                        category = tok
            result = handler(limit=limit, category=category)
        elif name == "forget":
            result = handler(arg)
        elif name == "sessions":
            result = handler(current_thread_id=thread_id)
        elif name == "continue":
            result = await handler(arg, checkpointer=_checkpointer)
        elif name == "delete":
            result = handler(arg, current_thread_id=thread_id)
        elif name == "help":
            result = handler()
        else:
            result = handler()

        if hasattr(result, "__await__"):
            result = await result
        return result

    except Exception as exc:
        return {"kind": "error", "message": f"command failed: {exc}"}


async def _run_turn(ws: WebSocket, user_input: str, state: Dict[str, Any]) -> None:
    """[STOP-1] The actual agent-turn processing, run as an independently
    cancellable asyncio.Task so a "stop" message (or a new prompt sent
    mid-turn) can interrupt it instead of sitting unread until the turn
    finishes on its own — which is what happened when this ran inline in
    the WebSocket receive loop.

    `state` is a shared mutable dict (session_id, config, token_count,
    msg_count) so this function's effects (token counting) and /continue's
    effects (session_id/config swap) stay visible to the outer loop.
    """
    session_id = state["session_id"]
    config     = state["config"]
    cfg        = get_active_config()   # [FIX-10] this connection's own config

    inputs = {
        "messages":       [HumanMessage(content=user_input)],
        "active_persona": _active_persona(),
        "target_scope":   cfg.scope,
        "iteration":      0,
    }

    _run_trace_id = str(uuid.uuid4())
    for _cb in get_callbacks():
        if hasattr(_cb, "set_run_context"):
            await _cb.set_run_context(
                trace_id=_run_trace_id,
                session_id=session_id,
                user_input=user_input,
            )

    # Mirrors cli/ui.py's FIX-JSON-UI handling: tool calls arrive as
    # JSON embedded in the streamed text, not as a separate LangChain
    # tool_calls attribute. We stream tokens live as they come in (real
    # narration is the common case and should feel instant), but if
    # on_tool_start fires for THIS generation, we tell the frontend to
    # retract/discard what it just streamed — the user never sees the raw
    # {"tool": ..., "args": {...}} blob.
    pending_open = False

    try:
        async for event in _graph.astream_events(inputs, config=config, version="v2"):
            etype = event.get("event", "")
            data  = event.get("data", {})

            if etype == "on_chat_model_start":
                pending_open = True
                await ws.send_text(json.dumps({"type": "gen_start"}))

            elif etype == "on_chat_model_stream":
                chunk = data.get("chunk")
                text = ""
                if chunk and hasattr(chunk, "content") and chunk.content:
                    if isinstance(chunk.content, str):
                        text = chunk.content
                    elif isinstance(chunk.content, list):
                        text = "".join(
                            p.get("text", "") for p in chunk.content
                            if isinstance(p, dict) and p.get("type") == "text"
                        )
                if text:
                    await ws.send_text(json.dumps({"type": "token", "text": text}))

            elif etype in ("on_chat_model_end", "on_llm_end"):
                output = data.get("output")
                tokens = 0
                if output is not None:
                    # [FIX-TOKCOUNT] See cli/ui.py's identical fix: total_tokens
                    # is input_tokens + output_tokens (LangChain's UsageMetadata
                    # schema), not an output-token count, so using it as a
                    # fallback here silently added prompt length into the
                    # running total sent to the frontend as "tokens_used".
                    usage = getattr(output, "usage_metadata", None) or {}
                    tokens = usage.get("output_tokens") or 0
                    if not tokens:
                        meta = getattr(output, "response_metadata", None) or {}
                        tokens = meta.get("eval_count", 0)
                    if not tokens:
                        # [FIX-TOKCOUNT] Parity with cli/ui.py's third fallback
                        # — some non-chat LLM wrappers surface eval_count under
                        # llm_output.model_extra instead of response_metadata.
                        llm_out    = getattr(output, "llm_output", None) or {}
                        token_meta = llm_out.get("model_extra", {}) or {}
                        tokens     = token_meta.get("eval_count", 0)
                if tokens:
                    state["token_count"] += tokens
                    await ws.send_text(json.dumps({
                        "type": "tokens_used",
                        "count": state["token_count"],
                        "limit": cfg.num_ctx,
                    }))
                    # [FIX-TOKEN-STALE] StatusBanner's "tokens" text reads
                    # status.token_count from status_update, NOT tokenUsage
                    # from tokens_used above — two separate frontend states
                    # fed by two separate message types. _send_status() was
                    # only ever called on connect/config-change/session-switch,
                    # never here, so the text label froze at 0 for the whole
                    # session while TokenBar (fed by tokens_used) updated
                    # live right next to it. Both now share one source of
                    # truth per generation instead of drifting apart.
                    await _send_status(ws, state)

            elif etype == "on_tool_start":
                if pending_open:
                    await ws.send_text(json.dumps({"type": "retract_pending"}))
                    pending_open = False
                name = event.get("name", "tool")
                args = str(data.get("input", "") or "")
                await ws.send_text(json.dumps({"type": "tool_start", "name": name, "args": args[:300]}))

            elif etype == "on_tool_end":
                name = event.get("name", "tool")
                output = _stringify_tool_output(data.get("output"))
                await ws.send_text(json.dumps({"type": "tool_end", "name": name, "output": output[:2000], "ok": True}))

            elif etype == "on_tool_error":
                name = event.get("name", "tool")
                err = str(data.get("error", "unknown error"))
                await ws.send_text(json.dumps({"type": "tool_end", "name": name, "output": err[:2000], "ok": False}))

        if pending_open:
            await ws.send_text(json.dumps({"type": "gen_end"}))

        await ws.send_text(json.dumps({"type": "done"}))

    except asyncio.CancelledError:
        # [STOP-1] Cancelled via the stop button, or superseded by a new
        # prompt sent mid-turn. Tell the frontend to finalize whatever
        # partial content was streamed (matches "stop and send a new
        # prompt" — the partial answer stays visible, same as Claude's own
        # stop behavior) instead of leaving the pending bubble stuck open.
        await ws.send_text(json.dumps({"type": "stopped"}))

        # [FIX-CANCEL-ORPHAN] A cancellation mid-tool-call (e.g. os_exec
        # running nmap) kills execute_tools() mid-await — its ToolMessage
        # never gets built, so nothing about the attempt is ever committed
        # to the checkpoint. The last committed message is left as an
        # AIMessage with unresolved tool_calls and no record of what
        # actually happened. compact_messages()'s own [FIX-2] silently
        # strips exactly this shape before the next call (it has to, to
        # avoid a broken message sequence) — so on the next turn the model
        # has zero information that it ever attempted the call, and when
        # asked "why did you stop" it has nothing true to answer with and
        # fabricates a plausible-sounding cause instead (observed: claiming
        # a real, installed tool "isn't installed"). Patch the checkpoint
        # directly with a ToolMessage per dangling tool_call, stating
        # plainly that it was interrupted — not that it failed or is
        # missing — so the model has a true fact to work from next turn.
        try:
            snapshot = await _graph.aget_state(config)
            msgs = (snapshot.values or {}).get("messages") or []
            last = msgs[-1] if msgs else None
            pending_calls = getattr(last, "tool_calls", None) if isinstance(last, AIMessage) else None
            if pending_calls:
                notes = [
                    ToolMessage(
                        content=(
                            "[INTERRUPTED] This tool call was cancelled before it "
                            "finished — either the stop button was used or a new "
                            "message was sent while it was still running. This is "
                            "not a tool error and does not mean the tool is missing, "
                            "broken, or unavailable — no output was returned because "
                            "execution was cut short partway through. State this "
                            "plainly if asked; do not guess at or invent a cause."
                        ),
                        tool_call_id=tc["id"],
                        name=tc.get("name", "tool"),
                    )
                    for tc in pending_calls
                ]
                await _graph.aupdate_state(config, {"messages": notes})
        except Exception:
            logger.warning("[STOP-1] Failed to record cancellation note in graph state.", exc_info=True)
        # Deliberately not re-raised — this is a leaf task with nothing
        # above it that needs to observe cancellation propagate further.

    except Exception as exc:
        await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))


_WS_AUTH_TIMEOUT = 10.0  # seconds to wait for the client's first "auth" frame


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    # [AUTH-1] The token now travels as the WebSocket's first application
    # message, {"type": "auth", "token": "..."} — never as a query param.
    # A query string ends up in browser history, this server's own access
    # logs (uvicorn logs the full request line), and any Referer header,
    # none of which should ever carry a secret. The client always sends
    # this frame first, whether or not a token is actually configured —
    # _token_valid() trivially accepts anything when BEAVER_WEB_TOKEN is
    # unset, so one code path covers both the local no-auth default and
    # the token-protected case.
    await ws.accept()
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=_WS_AUTH_TIMEOUT)
        first = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError):
        logger.warning("[AUTH-1] Rejected WebSocket connection — no valid auth frame received.")
        await ws.close(code=4401, reason="auth required")
        return

    if first.get("type") != "auth" or not _token_valid(first.get("token")):
        logger.warning("[AUTH-1] Rejected WebSocket connection — missing or invalid token.")
        await ws.close(code=4401, reason="invalid or missing token")
        return

    session_id = f"web-{uuid.uuid4().hex[:12]}"

    if _graph is None:
        await ws.send_text(json.dumps({"type": "error", "message": "Agent graph not ready yet — try again in a moment."}))
        await ws.close()
        return

    # [FIX-10] Give this connection its own RuntimeConfig and bind it to
    # the current context *before* anything else reads config or spawns a
    # task. contextvars propagate to every `asyncio.create_task()` created
    # from here on (that's how _run_turn's task below inherits it), but a
    # task only sees whatever was set in its parent context at creation
    # time — so this has to happen first, not lazily on first use.
    # Without this, every tab shared the single global `runtime_config`:
    # one tab's /model or settings-panel change silently changed the model
    # for every other tab's next turn.
    session_cfg = RuntimeConfig()
    set_session_config(session_cfg)

    # [STOP-1] Shared mutable state _run_turn reads/writes, and /continue
    # can swap session_id/config mid-connection.
    state: Dict[str, Any] = {
        "session_id":  session_id,
        "config":      {"configurable": {"thread_id": session_id}},
        "token_count": 0,
        "msg_count":   0,
    }
    current_turn_task: Optional[asyncio.Task] = None
    log_watch_tasks: Dict[str, asyncio.Task] = {}  # [LOGS-1] which -> follower task, this connection only

    # [BANNER-1] Give the live banner real data immediately on connect,
    # rather than leaving it blank until the first change.
    await _send_status(ws, state)

    async def _cancel_current_turn() -> None:
        nonlocal current_turn_task
        if current_turn_task is not None and not current_turn_task.done():
            current_turn_task.cancel()
            try:
                await current_turn_task
            except asyncio.CancelledError:
                pass
        current_turn_task = None

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error", "message": "Invalid message — expected JSON."}))
                continue

            mtype = msg.get("type")

            # [STOP-1] Explicit stop button.
            if mtype == "stop":
                await _cancel_current_turn()
                continue

            # [CFG-1] Settings panel — read current values.
            if mtype == "get_config":
                await ws.send_text(json.dumps({"type": "config_state", **_read_config_state()}))
                continue

            # [CFG-1] Settings panel — apply a batch of changes.
            if mtype == "update_config":
                result = _apply_config_changes(msg.get("changes", {}))
                await ws.send_text(json.dumps({"type": "config_updated", **result}))
                if "model" in result["applied"] or "persona" in result["applied"]:
                    await _send_status(ws, state)
                continue

            # [PERSONA-1] Dropdown population.
            if mtype == "list_personas":
                await ws.send_text(json.dumps({"type": "personas_list", "personas": _list_personas()}))
                continue

            # [PERSONA-1] Add a new persona from the drawer.
            if mtype == "add_persona":
                result = _add_persona(msg.get("name", ""), msg.get("content", ""))
                await ws.send_text(json.dumps({"type": "persona_added", **result}))
                if result.get("ok"):
                    await ws.send_text(json.dumps({"type": "personas_list", "personas": _list_personas()}))
                continue

            # [SKILLS-1] Plugins — list + toggle.
            if mtype == "list_plugins":
                await ws.send_text(json.dumps({"type": "plugins_list", "plugins": _list_plugins()}))
                continue

            if mtype == "toggle_plugin":
                result = _toggle_plugin(msg.get("name", ""), bool(msg.get("enabled")))
                await ws.send_text(json.dumps({"type": "plugin_toggled", **result}))
                if result.get("ok"):
                    await ws.send_text(json.dumps({"type": "plugins_list", "plugins": _list_plugins()}))
                continue

            # [SKILLS-1] Tool manuals — list + add.
            if mtype == "list_tool_manuals":
                await ws.send_text(json.dumps({"type": "tool_manuals_list", "manuals": _list_tool_manuals()}))
                continue

            if mtype == "add_tool_manual":
                result = _add_tool_manual(msg.get("name", ""), msg.get("content", ""))
                await ws.send_text(json.dumps({"type": "tool_manual_added", **result}))
                if result.get("ok"):
                    await ws.send_text(json.dumps({"type": "tool_manuals_list", "manuals": _list_tool_manuals()}))
                continue

            # [SIDEBAR-1] Dedicated, non-chat-log data feeds for the sidebar
            # panels. Deliberately NOT routed through _dispatch_command /
            # command_result — that pipeline appends a CommandEntry into
            # the visible chat log on every call, which is fine for a user
            # typing "/tools" once but wrong for a sidebar tab that
            # refetches silently (e.g. every time it's opened, or after a
            # turn completes). Mirrors the existing list_plugins /
            # list_tool_manuals pattern instead — reuses the exact same
            # web.commands logic, just delivered as its own frame type.
            if mtype == "list_active_tools":
                from web.commands import cmd_tools
                try:
                    result = await cmd_tools(cfg=get_active_config())
                except Exception as exc:
                    result = {"kind": "error", "message": f"error loading tools: {exc}"}
                await ws.send_text(json.dumps({"type": "active_tools_list", **result}))
                continue

            if mtype == "list_memories":
                from web.commands import cmd_memory
                limit = int(msg.get("limit", 20) or 20)
                category = str(msg.get("category", "") or "")
                try:
                    result = await cmd_memory(limit=limit, category=category)
                except Exception as exc:
                    result = {"kind": "error", "message": f"error loading memory: {exc}"}
                await ws.send_text(json.dumps({"type": "memories_list", **result}))
                continue

            if mtype == "list_sessions":
                from web.commands import cmd_sessions
                try:
                    result = await cmd_sessions(current_thread_id=state["session_id"])
                except Exception as exc:
                    result = {"kind": "error", "message": f"error loading sessions: {exc}"}
                await ws.send_text(json.dumps({"type": "sessions_list", **result}))
                continue

            # [LOGS-1] Activity panel — agent.log / traces / uvicorn tabs.
            # One-shot tail on open, then an explicit watch_log to start
            # live-following (unwatch_log or disconnect stops it). Mirrors
            # the SIDEBAR-1 pattern: separate frame types, no chat-log noise.
            if mtype == "tail_log":
                which = str(msg.get("which", ""))
                max_lines = int(msg.get("lines", 200) or 200)
                result = _read_log_tail(which, max_lines)
                await ws.send_text(json.dumps({"type": "log_tail", "which": which, **result}))
                continue

            if mtype == "watch_log":
                which = str(msg.get("which", ""))
                if which not in LOG_FILES:
                    await ws.send_text(json.dumps({"type": "error", "message": f"unknown log '{which}'"}))
                    continue
                existing = log_watch_tasks.get(which)
                if existing is not None and not existing.done():
                    continue  # already watching this one
                path = LOG_FILES[which]
                start_size = path.stat().st_size if path.exists() else 0
                log_watch_tasks[which] = asyncio.create_task(_watch_log(ws, which, start_size))
                continue

            if mtype == "unwatch_log":
                which = str(msg.get("which", ""))
                task = log_watch_tasks.pop(which, None)
                if task is not None and not task.done():
                    task.cancel()
                continue

            if mtype != "user_message":
                continue
            user_input = str(msg.get("content", "")).strip()
            if not user_input:
                continue

            # [CMD-1] Slash commands never reach the agent graph.
            if user_input.startswith("/"):
                session = {"msg_count": state["msg_count"], "token_count": state["token_count"]}
                result = await _dispatch_command(user_input, session, state["session_id"])

                if result.get("switch_to_thread"):
                    state["session_id"] = result["switch_to_thread"]
                    state["config"] = {"configurable": {"thread_id": state["session_id"]}}

                # [FIX-RESET] See web/commands.py's cmd_reset docstring —
                # the `session` dict passed into _dispatch_command above is
                # a plain copy of state["msg_count"]/state["token_count"],
                # so a handler mutating it has no effect on the real state.
                # This explicit signal is how cmd_reset actually zeroes them.
                if result.get("reset_counters"):
                    state["msg_count"]   = 0
                    state["token_count"] = 0

                await ws.send_text(json.dumps({"type": "command_result", **result}))

                # [BANNER-1] /model and /persona change what the live banner
                # shows — push a status update so it reflects slash-command
                # changes too, not just the settings panel.
                cmd_name = user_input[1:].split(maxsplit=1)[0].lower()
                if cmd_name in ("model", "persona"):
                    await _send_status(ws, state)
                continue

            # [STOP-1] A new prompt sent while a turn is still running
            # implicitly stops it first — "I can stop and send a new
            # prompt" — rather than queuing behind it or being ignored.
            await _cancel_current_turn()

            state["msg_count"] += 1
            current_turn_task = asyncio.create_task(_run_turn(ws, user_input, state))

    except WebSocketDisconnect:
        if current_turn_task is not None and not current_turn_task.done():
            current_turn_task.cancel()
        for t in log_watch_tasks.values():  # [LOGS-1]
            if not t.done():
                t.cancel()

    except Exception as exc:
        # [SEC-1/PROD-1] Anything unexpected here (a bug in a handler, a
        # transient send failure, etc.) previously propagated straight out
        # of this coroutine — the running turn task, if any, was never
        # cancelled, and the failure was invisible except in server logs.
        # Log it, cancel any in-flight turn, and make one best-effort
        # attempt to tell the client before the connection goes away.
        logger.exception("[WS] Unhandled error in connection %s: %s", session_id, exc)
        if current_turn_task is not None and not current_turn_task.done():
            current_turn_task.cancel()
        for t in log_watch_tasks.values():  # [LOGS-1]
            if not t.done():
                t.cancel()
        try:
            await ws.send_text(json.dumps({"type": "error", "message": "Internal server error — connection closing."}))
        except Exception:
            pass


def _resolve_tls() -> Dict[str, Optional[str]]:
    """[TLS-1] Optional TLS for the web UI, off by default (matches
    BEAVER_WEB_TOKEN's own posture: zero-setup on localhost, opt-in the
    moment you're exposed beyond it). Set BOTH BEAVER_TLS_CERT and
    BEAVER_TLS_KEY in .env to enable — uvicorn refuses to start with only
    one of the two, so we fail loudly here instead of silently serving
    plain HTTP when someone typo'd one of the two var names.

    Self-signed cert for local testing:
        openssl req -x509 -newkey rsa:4096 -nodes -days 365 \\
            -keyout key.pem -out cert.pem -subj "/CN=localhost"
    Browsers will warn on a self-signed cert (expected) — click through,
    or use mkcert (https://github.com/FiloSottile/mkcert) for a
    locally-trusted one instead. For a real deployment, use a
    certificate from a real CA (e.g. Let's Encrypt / certbot) — a
    self-signed cert is fine for testing, not for anything public.
    """
    cert = os.getenv("BEAVER_TLS_CERT", "").strip()
    key = os.getenv("BEAVER_TLS_KEY", "").strip()
    if not cert and not key:
        return {"ssl_certfile": None, "ssl_keyfile": None}
    if bool(cert) != bool(key):
        raise SystemExit(
            "[TLS-1] Both BEAVER_TLS_CERT and BEAVER_TLS_KEY must be set together "
            "(only one was found in .env) — refusing to start rather than silently "
            "falling back to plain HTTP."
        )
    for label, path_str in (("BEAVER_TLS_CERT", cert), ("BEAVER_TLS_KEY", key)):
        if not Path(path_str).is_file():
            raise SystemExit(f"[TLS-1] {label} is set to '{path_str}' but that file doesn't exist.")
    return {"ssl_certfile": cert, "ssl_keyfile": key}


if __name__ == "__main__":
    import uvicorn

    _host = os.getenv("BEAVER_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    _port = int(os.getenv("BEAVER_WEB_PORT", "8000").strip() or "8000")
    _tls = _resolve_tls()

    if _tls["ssl_certfile"]:
        print(f"[TLS-1] TLS enabled — serving https://{_host}:{_port}")
    elif _host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"[TLS-1] WARNING: binding to {_host} (not localhost) without TLS — traffic, "
            "including the BEAVER_WEB_TOKEN auth frame, will be plaintext on the network. "
            "Set BEAVER_TLS_CERT / BEAVER_TLS_KEY in .env before exposing this beyond your own machine."
        )

    uvicorn.run(app, host=_host, port=_port, ssl_certfile=_tls["ssl_certfile"], ssl_keyfile=_tls["ssl_keyfile"])
