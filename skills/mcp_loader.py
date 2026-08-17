"""
Beaver MCP Loader
=================

Connects to Model Context Protocol (MCP) servers and exposes their tools
as standard LangChain ``BaseTool`` objects.

Two transport types are supported:

  stdio  — spawns a subprocess (npx, python, etc.) and communicates over
           stdin/stdout.  Use for local CLI-based MCP servers.

  url    — connects to an already-running HTTP/SSE MCP server via its URL.
           Use for Docker-hosted services, remote servers, or any MCP
           server that exposes an HTTP endpoint instead of a stdio process.
           Tries Streamable-HTTP first (mcp ≥1.6 default), falls back to
           the legacy SSE transport automatically.

MCP_SERVERS format in .env
---------------------------
All on ONE LINE (python-dotenv does not support multiline values).

  # stdio: spawn a local npx process
  MCP_SERVERS=[{"name":"filesystem","cmd":["npx","-y","@modelcontextprotocol/server-filesystem","/home"],"persona":"*"}]

  # url: connect to an already-running HTTP/SSE MCP server (e.g. in Docker)
  MCP_SERVERS=[{"name":"mydb","url":"http://localhost:3100/mcp","persona":"*"}]

  # multiple servers — mix stdio and url
  MCP_SERVERS=[{"name":"fs","cmd":["npx","-y","@mcp/server-filesystem","/home"]},{"name":"db","url":"http://localhost:3100/mcp","persona":"coder"}]

Server entry fields
-------------------
  name    : str              — label for logs and tool name prefix
  cmd     : list[str]        — [stdio only] command + args to launch
  url     : str              — [url only]   HTTP/SSE endpoint of a running server
  env     : dict             — [stdio only] extra env vars for the subprocess
  headers : dict             — [url only]   HTTP headers (e.g. Authorization)
  persona : str | list[str]  — restrict to persona(s); "*" = all (default)

Fix log
-------
[FIX-1] Each server isolated — one failure does not block others.
[FIX-2] Tool names prefixed with {server_name}__ to avoid collisions.
[FIX-3] Connections kept alive in AsyncExitStack so returned tools work.
[FIX-4] get_mcp_tools_sync: coroutine created inside worker thread loop.
[FIX-5] AsyncExitStack keeps contexts open past function return.
[FIX-6] Added URL/SSE transport for HTTP-based MCP servers.
[FIX-7] asyncio.gather uses return_exceptions=True — one bad server never
        kills the whole gather; results are checked individually.
[FIX-8] Per-server connection timeout (MCP_CONNECT_TIMEOUT, default 30 s)
        prevents a hung npx install from blocking the agent indefinitely.
[FIX-9] Actionable error hints for the most common failure modes
        (wrong password, bad DSN, npx not found, port not exposed).

[FIX-10] ImportError now logs the ACTUAL Python exception text, not just
         "packages missing".  This lets you see immediately whether the
         problem is a missing package, a wrong Python interpreter, or an
         API change.  The most common cause on Windows is an interpreter
         mismatch: pip3 installing into Python X while python3 runs Python Y.
         Fix: python3 -m pip install -r requirements.txt

[FIX-11] URL transport now tries Streamable-HTTP first, then falls back to
         the legacy SSE transport.  mcp ≥1.6 servers prefer Streamable-HTTP
         but many older or third-party servers still speak SSE; this makes
         the loader work with both without any config change.

[FIX-12] _diagnose_mcp_imports() helper — call it once on startup (or from
         /mcp check in the CLI) to get a clear per-import pass/fail table
         that tells you exactly which symbol is missing and which pip command
         fixes it.  Logged at DEBUG level on every startup.
"""
import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.tools import BaseTool

logger = logging.getLogger("beaver")

# Per-server connection timeout.  On a cold npx run the package download
# can take 20–40 s; 30 s is a reasonable cap before we give up and warn.
_CONNECT_TIMEOUT: float = float(os.getenv("MCP_CONNECT_TIMEOUT", "30"))

# ── Active connection stack ────────────────────────────────────────────────────
# [FIX-13] `_active_stack` / `_cached_persona` / `_cached_tools` stay as
# module globals — main.py, cli/ui.py, and web/commands.py all reach into
# these directly (`_mcp_mod._cached_persona`, `_active_stack is not None`,
# etc.) for /mcp and /persona status reporting, so the public shape is kept
# unchanged. What changes is *how* the stack gets opened and closed — see
# _owner_loop below.
_active_stack:   Optional[AsyncExitStack] = None
_cached_persona: str                      = ""
_cached_tools:   List[BaseTool]           = []
_reload_generation: int                   = 0  # bumps on every real (re)connect — see get_mcp_cache_generation()

# [FIX-13] Owner-task machinery. anyio's cancel scopes (used internally by
# stdio_client for the subprocess transport) must be entered AND exited by
# the *same* asyncio Task. get_mcp_tools() used to open `_active_stack`
# inside whatever ephemeral per-turn task called it (web/server.py spawns a
# fresh asyncio.create_task(_run_turn(...)) for every message), then a
# later persona switch — on a different turn, i.e. a different, brand-new
# Task, or from a different concurrent WebSocket session — called
# `_active_stack.aclose()` from that unrelated Task. That's exactly
# "Attempted to exit cancel scope in a different task than it was entered
# in": the opening Task had already finished, so anyio rejected the
# cross-Task close. Fix: one dedicated, long-lived Task per connection
# generation owns opening the stack, holds it open, and is the *only* one
# that ever calls stack.aclose() — everyone else just asks it to close
# (via _close_requested) and awaits it, rather than closing anything
# themselves.
_owner_task:      Optional[asyncio.Task] = None
_owner_close:     Optional[asyncio.Event] = None
_owner_lock                              = asyncio.Lock()  # serializes concurrent get_mcp_tools()/reconnect calls


async def _owner_loop(
    persona: str,
    configs: List[Dict[str, Any]],
    ready: "asyncio.Event",
    close_requested: "asyncio.Event",
) -> None:
    """[FIX-13] Owns one MCP connection generation end-to-end.

    Opens every configured server's connections, publishes the resulting
    tools, then blocks until told to close — and does the actual
    stack.aclose() itself, since it's the only Task allowed to (see the
    module-level comment above _owner_task).
    """
    global _active_stack, _cached_persona, _cached_tools, _reload_generation

    stack = AsyncExitStack()
    try:
        results = await asyncio.gather(
            *[_load_tools_from_server(cfg, persona, stack) for cfg in configs],
            return_exceptions=True,
        )
        all_tools: List[BaseTool] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                name = configs[i].get("name", f"server[{i}]")
                logger.warning("[MCP] Unexpected error loading '%s': %s", name, result)
            elif isinstance(result, list):
                all_tools.extend(result)

        _active_stack   = stack
        _cached_persona = persona
        _cached_tools   = all_tools
        _reload_generation += 1
        if all_tools:
            logger.info("[MCP] Loaded %d tool(s) for persona '%s' (fresh connect)", len(all_tools), persona)
    except Exception as exc:
        logger.warning("[MCP] Unexpected error setting up connections for persona '%s': %s", persona, exc)
        _active_stack   = None
        _cached_persona = ""
        _cached_tools   = []
        try:
            await stack.aclose()
        except Exception:
            pass
        ready.set()
        return
    finally:
        ready.set()

    # Hold everything open until asked to stop — and do the close
    # ourselves, in this same Task, whenever that happens.
    await close_requested.wait()
    try:
        await stack.aclose()
    except Exception as exc:
        logger.warning("[MCP] Error while closing connections: %s", exc)
    _active_stack = None


async def _release_mcp_connections() -> None:
    """Ask the current owner Task (if any) to close its connections, and
    wait for it to actually do so. [FIX-13] Never calls stack.aclose()
    directly from the caller's own Task — see _owner_loop.
    """
    global _owner_task, _owner_close
    task, close_event = _owner_task, _owner_close
    _owner_task, _owner_close = None, None
    if task is None or task.done():
        return
    close_event.set()
    try:
        await task
    except Exception as exc:
        logger.warning("[MCP] Error while closing connections: %s", exc)


# ── Diagnostic helper [FIX-12] ────────────────────────────────────────────────

def _diagnose_mcp_imports() -> Dict[str, Any]:
    """
    Check every MCP-related import and return a structured report.

    Returns a dict with:
      ok        : bool            — True if all critical imports pass
      python    : str             — sys.executable (the running interpreter)
      packages  : dict[name, ver] — installed package versions
      imports   : dict[stmt, err] — per-import pass/fail (err=None means OK)
      fix_cmd   : str | None      — the pip command to run if something is broken
    """
    from importlib.metadata import version as pkg_version, PackageNotFoundError

    result: Dict[str, Any] = {
        "ok":       True,
        "python":   sys.executable,
        "packages": {},
        "imports":  {},
        "fix_cmd":  None,
    }

    for pkg in ("mcp", "langchain-mcp-adapters", "langchain-core"):
        try:
            result["packages"][pkg] = pkg_version(pkg)
        except PackageNotFoundError:
            result["packages"][pkg] = "NOT INSTALLED"
            result["ok"] = False

    critical_imports = [
        "from mcp import ClientSession, StdioServerParameters",
        "from mcp.client.stdio import stdio_client",
        "from mcp.client.sse import sse_client",
        "from langchain_mcp_adapters.tools import load_mcp_tools",
    ]
    optional_imports = [
        "from mcp.client.streamable_http import streamablehttp_client",
    ]

    for stmt in critical_imports:
        try:
            exec(stmt, {})
            result["imports"][stmt] = None          # None = OK
        except Exception as exc:
            result["imports"][stmt] = str(exc)
            result["ok"] = False

    for stmt in optional_imports:
        try:
            exec(stmt, {})
            result["imports"][stmt] = None
        except Exception as exc:
            result["imports"][stmt] = f"(optional) {exc}"

    if not result["ok"]:
        result["fix_cmd"] = (
            f"{sys.executable} -m pip install 'mcp>=1.6,<2.0' 'langchain-mcp-adapters>=0.3'"
        )

    return result


def _log_mcp_diagnostics() -> None:
    """Run _diagnose_mcp_imports() and emit the result to the logger (DEBUG)."""
    d = _diagnose_mcp_imports()
    lines = [
        "[MCP] Import diagnostic",
        f"  Python  : {d['python']}",
    ]
    for pkg, ver in d["packages"].items():
        lines.append(f"  {pkg:35s}: {ver}")
    for stmt, err in d["imports"].items():
        status = "OK  " if err is None else "FAIL"
        lines.append(f"  [{status}] {stmt}")
        if err and not err.startswith("(optional)"):
            lines.append(f"         → {err}")
    if d["fix_cmd"]:
        lines.append(f"  Fix: {d['fix_cmd']}")
    logger.debug("\n".join(lines))


# ── Config loader ──────────────────────────────────────────────────────────────

def _load_server_configs() -> List[Dict[str, Any]]:
    """Parse MCP_SERVERS from the environment."""
    raw = os.getenv("MCP_SERVERS", "").strip()
    if not raw or raw in ("[", "{"):
        if raw in ("[", "{"):
            logger.debug(
                "[MCP] MCP_SERVERS looks truncated ('%s'). "
                "Write the JSON on ONE line in .env.", raw
            )
        return []
    try:
        configs = json.loads(raw)
        if not isinstance(configs, list):
            raise ValueError("MCP_SERVERS must be a JSON array")
        return configs
    except json.JSONDecodeError as exc:
        logger.warning(
            "[MCP] Could not parse MCP_SERVERS: %s\n"
            "  Tip: the entire JSON must be on ONE line in .env.", exc
        )
        return []
    except Exception as exc:
        logger.warning("[MCP] Could not parse MCP_SERVERS: %s", exc)
        return []


# ── Error hints ────────────────────────────────────────────────────────────────

def _hint_for_error(exc: Exception, server_name: str, transport: str) -> str:
    """Return a human-readable fix hint for common MCP connection failures."""
    msg = str(exc).lower()

    if transport == "stdio":
        if "not found" in msg or "no such file" in msg:
            return (
                f"[MCP:{server_name}] npx / command not found.\n"
                "  Fix: ensure Node.js is installed and 'npx' is on PATH.\n"
                "  Run: node --version && npx --version"
            )
        if "authentication" in msg or "password" in msg or "pg_hba" in msg:
            return (
                f"[MCP:{server_name}] Postgres authentication failed.\n"
                "  Fix: check the DSN in MCP_SERVERS — password may still be 'change_me'.\n"
                "  Correct format: postgresql://USER:PASSWORD@localhost:5432/DBNAME\n"
                "  Verify: docker exec -it <container> psql -U USER -d DBNAME"
            )
        if "connection refused" in msg or "econnrefused" in msg:
            return (
                f"[MCP:{server_name}] Connection refused.\n"
                "  Fix: check that the target service is running and the port is mapped.\n"
                "  For Docker: 'docker ps' and verify the port binding (e.g. 0.0.0.0:5432->5432).\n"
                "  For Postgres in Docker: docker compose up -d && docker compose ps"
            )
    elif transport in ("url", "sse", "http"):
        if "connection refused" in msg or "econnrefused" in msg:
            return (
                f"[MCP:{server_name}] Cannot reach URL — connection refused.\n"
                "  Fix: check the MCP server is running and the port is correct.\n"
                "  For Docker: ensure the container is up and the port is published."
            )
        if "404" in msg or "not found" in msg:
            return (
                f"[MCP:{server_name}] 404 at the SSE/MCP URL.\n"
                "  Fix: verify the url path — it might be /sse, /mcp, or /v1/mcp."
            )

    return f"[MCP:{server_name}] Connection failed ({transport}): {exc}"


# ── Server loaders ─────────────────────────────────────────────────────────────

async def _load_stdio_server(
    server_cfg: Dict[str, Any],
    persona: str,
    stack: AsyncExitStack,
) -> List[BaseTool]:
    """Connect to a stdio MCP server (subprocess transport)."""
    name  = server_cfg.get("name", "mcp")
    cmd: List[str] = server_cfg.get("cmd", [])
    extra_env: Dict[str, str] = server_cfg.get("env", {})

    if not cmd:
        logger.warning("[MCP] Server '%s' has no 'cmd' — skipping.", name)
        return []

    # [FIX-10] Catch ImportError and log the ACTUAL exception, not a generic message.
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from langchain_mcp_adapters.tools import load_mcp_tools
    except ImportError as exc:
        # Show the real error so the user knows exactly what's missing.
        logger.warning(
            "[MCP:%s] Import failed for stdio transport.\n"
            "  Error   : %s\n"
            "  Python  : %s\n"
            "  Fix     : %s -m pip install 'mcp>=1.6,<2.0' langchain-mcp-adapters\n"
            "  Common cause on Windows: pip3 and python3 point to different\n"
            "  Python installations. Always install with the same interpreter:\n"
            "    %s -m pip install -r requirements.txt",
            name, exc, sys.executable, sys.executable, sys.executable,
        )
        return []

    server_env = {**os.environ, **extra_env}
    server_params = StdioServerParameters(
        command=cmd[0],
        args=cmd[1:],
        env=server_env,
    )

    try:
        async def _connect():
            read, write = await stack.enter_async_context(stdio_client(server_params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return await load_mcp_tools(session)

        # [FIX-8] Per-server timeout so a hung npx never blocks indefinitely
        tools = await asyncio.wait_for(_connect(), timeout=_CONNECT_TIMEOUT)

        for t in tools:
            t.name = f"{name}__{t.name}"

        logger.info("[MCP] stdio: loaded %d tool(s) from '%s' for persona '%s'",
                    len(tools), name, persona)
        return tools

    except asyncio.TimeoutError:
        logger.warning(
            "[MCP] stdio: server '%s' did not respond within %gs.\n"
            "  Tip: on first run, npx downloads the package (~30 s). "
            "Increase MCP_CONNECT_TIMEOUT= in .env if needed.",
            name, _CONNECT_TIMEOUT,
        )
        return []
    except Exception as exc:
        logger.warning("%s", _hint_for_error(exc, name, "stdio"))  # [FIX-9]
        return []


async def _load_url_server(
    server_cfg: Dict[str, Any],
    persona: str,
    stack: AsyncExitStack,
) -> List[BaseTool]:
    """Connect to an HTTP MCP server.  [FIX-6, FIX-11]

    Tries Streamable-HTTP first (the mcp ≥1.6 default), then falls back to
    the legacy SSE transport.  This makes the loader work with both newer
    servers (Streamable-HTTP) and older/third-party servers (SSE) without
    any config change.

    Server config example:
        {"name": "mydb", "url": "http://localhost:3100/mcp", "persona": "*"}
    """
    name    = server_cfg.get("name", "mcp")
    url     = server_cfg.get("url", "")
    headers = server_cfg.get("headers", {})

    if not url:
        logger.warning("[MCP] url-transport server '%s' has no 'url' — skipping.", name)
        return []

    # [FIX-10] Import check with the actual error exposed
    try:
        from mcp import ClientSession
        from langchain_mcp_adapters.tools import load_mcp_tools
    except ImportError as exc:
        logger.warning(
            "[MCP:%s] Import failed for url transport.\n"
            "  Error   : %s\n"
            "  Python  : %s\n"
            "  Fix     : %s -m pip install 'mcp>=1.6,<2.0' langchain-mcp-adapters\n"
            "  Common cause on Windows: pip3 and python3 point to different\n"
            "  Python installations. Always install with the same interpreter:\n"
            "    %s -m pip install -r requirements.txt",
            name, exc, sys.executable, sys.executable, sys.executable,
        )
        return []

    # [FIX-11] Try Streamable-HTTP first (preferred in mcp ≥1.6), then SSE
    transport_tried: List[Tuple[str, Any]] = []

    try:
        from mcp.client.streamable_http import streamablehttp_client
        transport_tried.append(("streamable_http", streamablehttp_client))
    except ImportError:
        pass

    try:
        from mcp.client.sse import sse_client
        transport_tried.append(("sse", sse_client))
    except ImportError:
        pass

    if not transport_tried:
        logger.warning(
            "[MCP:%s] Neither streamable_http nor sse transport is available.\n"
            "  Fix: %s -m pip install 'mcp>=1.6,<2.0'",
            name, sys.executable,
        )
        return []

    last_exc: Optional[Exception] = None

    for transport_name, client_fn in transport_tried:
        try:
            async def _connect(tfn=client_fn):
                if transport_name == "streamable_http":
                    ctx = tfn(url, headers=headers)
                else:
                    ctx = tfn(url, headers=headers)
                read_or_tuple, write, *_ = await stack.enter_async_context(ctx)
                # streamablehttp_client returns (read_stream, write_stream, get_session_id)
                # sse_client returns (read_stream, write_stream)
                # normalise to (read, write)
                read = read_or_tuple
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                return await load_mcp_tools(session)

            tools = await asyncio.wait_for(_connect(), timeout=_CONNECT_TIMEOUT)

            for t in tools:
                t.name = f"{name}__{t.name}"

            logger.info(
                "[MCP] url(%s): loaded %d tool(s) from '%s' (%s) for persona '%s'",
                transport_name, len(tools), name, url, persona,
            )
            return tools

        except asyncio.TimeoutError:
            logger.warning(
                "[MCP] url(%s): server '%s' did not respond within %gs — url: %s",
                transport_name, name, _CONNECT_TIMEOUT, url,
            )
            # Don't fall through to the next transport on timeout; the server
            # is reachable but slow — retrying with a different protocol won't help.
            return []

        except Exception as exc:
            last_exc = exc
            logger.debug(
                "[MCP] url(%s): '%s' failed, trying next transport. Error: %s",
                transport_name, name, exc,
            )
            continue

    # All transports exhausted
    if last_exc is not None:
        logger.warning("%s", _hint_for_error(last_exc, name, "url"))
    return []


async def _load_tools_from_server(
    server_cfg: Dict[str, Any],
    persona: str,
    stack: AsyncExitStack,
) -> List[BaseTool]:
    """Route to the correct transport based on config fields."""
    name = server_cfg.get("name", "mcp")

    # Persona filter
    target_personas = server_cfg.get("persona", "*")
    if isinstance(target_personas, str):
        target_personas = [target_personas]
    if "*" not in target_personas and persona not in target_personas:
        return []

    # [FIX-6] Dispatch by transport type
    if server_cfg.get("url"):
        return await _load_url_server(server_cfg, persona, stack)
    elif server_cfg.get("cmd"):
        return await _load_stdio_server(server_cfg, persona, stack)
    else:
        logger.warning(
            "[MCP] Server '%s' has neither 'cmd' nor 'url' — skipping.\n"
            "  Add either: \"cmd\":[\"npx\",\"-y\",\"@mcp/server-X\"] "
            "or \"url\":\"http://host:port/mcp\"",
            name,
        )
        return []


# ── Public API ─────────────────────────────────────────────────────────────────

async def get_mcp_tools(persona: str = "*") -> List[BaseTool]:
    """Load tools from all configured MCP servers for the given persona.

    Connections are cached per persona.  The cache is invalidated when the
    persona changes or on the first call.

    [FIX-7] asyncio.gather uses return_exceptions=True so one bad server
    never kills loading of the others.
    [FIX-12] Runs import diagnostics at DEBUG level on every load so
    problems show up in the log even before they cause connection failures.
    [FIX-TOOLCACHE] Logging moved inside the fast-path gate: "[MCP] Loaded
    N tool(s)" now only fires when a real (re)connect happens below, not on
    every cache-hit call. Previously this was logged unconditionally one
    level up in skills.get_persona_tools_async(), on every single graph
    step (call_model AND execute_tools both call it), which made a 39-tool
    MCP setup look like it was reconnecting on every tool call when it was
    actually just hitting the cache. _reload_generation also bumps on every
    real reload so skills/__init__.py can cheaply detect "did anything
    actually change" without re-deriving it from log side effects.
    [FIX-13] Opening/closing now goes through a dedicated owner Task (see
    _owner_loop) instead of happening inline in whichever ephemeral
    per-turn Task calls this — fixes the anyio "cancel scope in a
    different task" crash on persona switch.
    """
    global _owner_task, _owner_close

    configs = _load_server_configs()
    if not configs:
        return []

    async with _owner_lock:
        # Fast path: persona unchanged and the owner task is still alive.
        if (
            _owner_task is not None
            and not _owner_task.done()
            and persona == _cached_persona
            and _active_stack is not None
        ):
            return list(_cached_tools)

        # [FIX-12] Emit diagnostics at DEBUG level so they're visible with
        # LOG_LEVEL=DEBUG but don't spam the normal console output. Only
        # on an actual reconnect, same as before.
        _log_mcp_diagnostics()

        # Tear down the previous generation's connections — safely, via
        # its own owner task (see _release_mcp_connections/_owner_loop).
        await _release_mcp_connections()

        ready = asyncio.Event()
        close_requested = asyncio.Event()
        _owner_close = close_requested
        _owner_task = asyncio.create_task(_owner_loop(persona, configs, ready, close_requested))

    # Wait for the new owner task to finish connecting, outside the lock
    # so other callers for the SAME persona can also just await it instead
    # of racing to spin up a second owner task.
    await ready.wait()
    return list(_cached_tools)


def get_mcp_cache_generation() -> int:
    """Increments every time get_mcp_tools() does a real (re)connect.

    Lets skills.get_persona_tools_async() know whether its own combined
    tool cache needs rebuilding without importing MCP internals directly.
    """
    return _reload_generation


def get_mcp_tools_sync(persona: str = "*") -> List[BaseTool]:
    """Synchronous wrapper around get_mcp_tools() for non-async contexts."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(lambda: asyncio.run(get_mcp_tools(persona)))
                return future.result(timeout=_CONNECT_TIMEOUT + 5)
        else:
            return loop.run_until_complete(get_mcp_tools(persona))
    except Exception as exc:
        logger.warning("[MCP] get_mcp_tools_sync failed: %s", exc)
        return []


def list_mcp_servers() -> List[Dict[str, Any]]:
    """Return metadata about all configured MCP servers (for CLI display)."""
    return [
        {
            "name":      cfg.get("name", "unnamed"),
            "transport": "url" if cfg.get("url") else "stdio",
            "endpoint":  cfg.get("url") or " ".join(cfg.get("cmd", [])),
            "persona":   cfg.get("persona", "*"),
        }
        for cfg in _load_server_configs()
    ]


async def diagnose_mcp() -> str:
    """
    Run a full MCP diagnostic and return a human-readable report string.

    Call this from /mcp check in the CLI, or from test_mcp.py.
    """
    d = _diagnose_mcp_imports()
    lines = [
        "─── MCP Diagnostic ───────────────────────────────────",
        f"Python interpreter : {d['python']}",
        "",
        "Installed packages:",
    ]
    for pkg, ver in d["packages"].items():
        status = "✓" if ver != "NOT INSTALLED" else "✗"
        lines.append(f"  {status}  {pkg:35s}  {ver}")

    lines.append("")
    lines.append("Import checks:")
    for stmt, err in d["imports"].items():
        if err is None:
            lines.append(f"  ✓  {stmt}")
        elif err.startswith("(optional)"):
            lines.append(f"  ○  {stmt}")
            lines.append(f"       {err}")
        else:
            lines.append(f"  ✗  {stmt}")
            lines.append(f"       {err}")

    lines.append("")
    if d["ok"]:
        configs = _load_server_configs()
        if configs:
            lines.append(f"Configured servers: {len(configs)}")
            for cfg in configs:
                t = "url" if cfg.get("url") else "stdio"
                lines.append(f"  • {cfg.get('name', '?'):20s}  [{t}]")
            lines.append("")
            lines.append("Run python3 test_mcp.py to attempt a live connection.")
        else:
            lines.append("No MCP_SERVERS configured in .env.")
            lines.append("See the MCP_SERVERS section in .env.example for examples.")
    else:
        lines.append("─── Action required ──────────────────────────────────")
        lines.append("")
        lines.append("Packages are missing or not importable by this Python.")
        lines.append("The most common cause on Windows is that pip3 and python3")
        lines.append("point to DIFFERENT Python installations.")
        lines.append("")
        lines.append("Fix — run this exact command:")
        lines.append(f"  {d['fix_cmd']}")
        lines.append("")
        lines.append("Or, using the same Python that runs beaver:")
        lines.append(f"  {d['python']} -m pip install -r requirements.txt")

    lines.append("──────────────────────────────────────────────────────")
    return "\n".join(lines)