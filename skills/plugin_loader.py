"""
Beaver Plugin Loader
====================

Auto-discovers and loads tool plugins from the ``plugins/`` directory.
Each plugin is a plain Python module that declares:

    PERSONA  : str | list[str]   — which persona(s) this plugin serves
    TOOLS    : list[BaseTool]    — tools to register
    ENABLED  : bool              — (optional) set False to disable without deleting

Example plugin (plugins/web_search.py):
---------------------------------------
    from langchain_core.tools import tool

    PERSONA = "standard"        # or ["standard", "coder"]
    ENABLED = True

    @tool
    def web_search(query: str) -> str:
        \"\"\"Search the web for information.\"\"\"
        ...

    TOOLS = [web_search]

Adding a plugin
---------------
1. Drop a .py file into the ``plugins/`` directory next to main.py
2. Declare PERSONA and TOOLS at module level
3. Restart Beaver — no core code changes needed

Fix log
-------
[FIX-1] Plugin import errors are caught individually so one bad plugin
        never prevents the rest from loading.
[FIX-2] PERSONA can be a string or a list — both forms are normalised.
[FIX-3] Plugins missing TOOLS are skipped with a warning, not a crash.
"""
import importlib
import logging
import pkgutil
import sys
from pathlib import Path
from typing import List

from langchain_core.tools import BaseTool

logger = logging.getLogger("beaver")

# Plugins directory sits at project root alongside main.py
_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"

# Add plugins parent to sys.path once at import time — not on every call.
_plugins_parent = str(_PLUGINS_DIR.parent)
if _plugins_parent not in sys.path:
    sys.path.insert(0, _plugins_parent)

# mtime-based reload cache: only call importlib.reload() when the plugins
# directory has actually changed on disk.
_plugins_mtime: float = 0.0
_plugins_cache: dict  = {}  # persona → List[BaseTool]


def _ensure_plugins_package() -> bool:
    """Create plugins/__init__.py if it doesn't exist so pkgutil can scan it."""
    if not _PLUGINS_DIR.exists():
        _PLUGINS_DIR.mkdir(parents=True)
    init = _PLUGINS_DIR / "__init__.py"
    if not init.exists():
        init.write_text("# Beaver plugin package — auto-generated\n")
    return True


def _plugins_fingerprint() -> float:
    """Return a value that changes whenever anything under plugins/ changes
    — a file added, removed, OR edited in place.

    [FIX-PLUGIN-RELOAD] load_plugin_tools() used to check only
    _PLUGINS_DIR.stat().st_mtime. On every common filesystem (ext4, APFS,
    NTFS), a directory's own mtime is bumped by a directory-ENTRY change
    — a file being added or removed — but NOT by an existing file's
    content being edited and saved, which only updates that file's own
    mtime. Verified empirically (write a file, then overwrite its
    content in place: directory mtime is bit-for-bit unchanged, file
    mtime correctly updates). In practice this meant the hot-reload this
    module advertises (see plugins/git_context.py's own docstring: "Drop
    this file in plugins/ and restart Beaver (or it hot-reloads)") never
    actually fired for the single most common plugin-development
    workflow — editing an EXISTING plugin's code — only for adding or
    deleting a whole file. Fixed by taking the max mtime across the
    directory itself and every .py file inside it.
    """
    try:
        mtimes = [_PLUGINS_DIR.stat().st_mtime]
        mtimes.extend(p.stat().st_mtime for p in _PLUGINS_DIR.glob("*.py"))
        return max(mtimes)
    except OSError:
        return 0.0


def load_plugin_tools(persona: str) -> List[BaseTool]:
    """Return all tools from enabled plugins that target *persona*.

    Parameters
    ----------
    persona : str
        Active persona name, e.g. ``"standard"``, ``"coder"``.

    Returns
    -------
    list[BaseTool]
        Flat list of tools from all matching plugins.  Empty list if no
        plugins exist or none match the persona.
    """
    global _plugins_mtime, _plugins_cache

    _ensure_plugins_package()

    if not _PLUGINS_DIR.exists():
        return []

    # Only reload the plugins package when the directory mtime has changed
    # (i.e. a file was added, removed, or modified).  Calling importlib.reload()
    # on every agent turn was 50–200 ms of wasted work and broke singletons
    # inside plugins.
    current_mtime = _plugins_fingerprint() if _PLUGINS_DIR.exists() else 0.0
    if current_mtime != _plugins_mtime:
        _plugins_mtime = current_mtime
        _plugins_cache.clear()
        try:
            import plugins as _reload_target
            importlib.reload(_reload_target)
        except ImportError:
            return []

    if persona in _plugins_cache:
        return list(_plugins_cache[persona])

    collected: List[BaseTool] = []

    try:
        import plugins as _plugins_pkg
    except ImportError:
        return []

    for finder, module_name, _ in pkgutil.iter_modules(_plugins_pkg.__path__):
        full_name = f"plugins.{module_name}"
        try:
            mod = importlib.import_module(full_name)
        except Exception as exc:  # FIX-1: isolate bad plugins
            logger.warning("[PLUGIN] Failed to import '%s': %s", full_name, exc)
            continue

        # Skip explicitly disabled plugins
        if not getattr(mod, "ENABLED", True):
            logger.debug("[PLUGIN] Skipping disabled plugin: %s", module_name)
            continue

        # FIX-2: normalise PERSONA to a list
        raw_persona = getattr(mod, "PERSONA", None)
        if raw_persona is None:
            continue
        targets: list = [raw_persona] if isinstance(raw_persona, str) else list(raw_persona)

        if persona not in targets and "*" not in targets:
            continue

        # FIX-3: skip plugins with no TOOLS
        tools = getattr(mod, "TOOLS", None)
        if not tools:
            logger.warning("[PLUGIN] Plugin '%s' matched persona but has no TOOLS.", module_name)
            continue

        logger.info("[PLUGIN] Loaded %d tool(s) from '%s' for persona '%s'",
                    len(tools), module_name, persona)
        collected.extend(tools)

    _plugins_cache[persona] = collected
    return list(collected)


def list_plugins() -> list[dict]:
    """Return metadata for all discovered plugins (for CLI/TUI display)."""
    _ensure_plugins_package()
    results = []

    try:
        import plugins as _plugins_pkg
    except ImportError:
        return results

    for finder, module_name, _ in pkgutil.iter_modules(_plugins_pkg.__path__):
        try:
            mod = importlib.import_module(f"plugins.{module_name}")
            results.append({
                "name": module_name,
                "persona": getattr(mod, "PERSONA", "unknown"),
                "enabled": getattr(mod, "ENABLED", True),
                "tools": [t.name for t in getattr(mod, "TOOLS", [])],
                "description": getattr(mod, "__doc__", "").strip().splitlines()[0]
                               if getattr(mod, "__doc__", "") else "",
            })
        except Exception as exc:
            results.append({"name": module_name, "error": str(exc)})

    return results
