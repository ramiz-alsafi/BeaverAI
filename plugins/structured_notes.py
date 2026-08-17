"""
Beaver Plugin — Structured Notes
==================================

Persistent, thread-scoped key-value notes stored as JSON files under
./notes/<thread_id>/.  Unlike long-term memory (pgvector, fuzzy recall),
notes are deterministic and structured — the agent writes exactly what it
wants and reads it back verbatim.

Use cases
---------
  researcher  — build up a source list, outline, or findings doc over a
                long session without losing context between turns.
  orchestrator— write a task plan, tick off sub-tasks, pass a brief to
                other agents via a shared note key.
  coder       — track a todo list, record design decisions, accumulate
                test results across multiple tool calls.
  standard    — anything that benefits from a running log or scratch pad.

Thread isolation
----------------
Each LangGraph thread_id gets its own subdirectory so parallel A2A agents
never clobber each other's notes. Thread ID comes from agent.graph's
active_thread_id_var ContextVar, set by execute_tools() at the start of
every node call.

[FIX-NOTES-THREAD] Previously read from a BEAVER_THREAD_ID environment
variable that nothing in the codebase ever set — every note from every
conversation and every persona silently landed in the same "default"
folder, the exact clobbering this feature exists to prevent. A ContextVar
is also the structurally correct fix, not just the wiring fix: an env var
is process-global, so concurrent asyncio tool calls from different
sessions in the same process could stomp each other's value mid-request
even if something HAD been setting it. BEAVER_THREAD_ID is still read as a
last-resort fallback, for anything invoking these tools outside the graph
(scripts, tests) where the ContextVar was never set.

Storage layout
--------------
  ./notes/<thread_id>/<key>.json
  {"key": "...", "content": "...", "updated_at": "ISO-8601"}

No extra dependencies — stdlib json, pathlib, datetime only.

Activate
--------
Already enabled — drop this file in plugins/ and restart Beaver.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import tool

PERSONA = ["*"]   # available to every persona
ENABLED = True

_NOTES_BASE = Path(os.getenv("BEAVER_NOTES_DIR", "./notes"))
_MAX_CONTENT_CHARS = 8_000   # per note — avoids flooding context on notes_read
_MAX_LIST_NOTES    = 50      # guard against runaway note accumulation


def _current_thread_id() -> str:
    """Resolve the active thread id: ContextVar first (set by
    execute_tools on every real graph run), env var as a last-resort
    fallback for callers outside the graph, "default" if neither is set."""
    try:
        from agent.graph import active_thread_id_var
        return active_thread_id_var.get()
    except Exception:
        return os.getenv("BEAVER_THREAD_ID", "default")


def _notes_dir() -> Path:
    """Return the thread-scoped notes directory, creating it if needed."""
    thread_id = _current_thread_id()
    # Sanitise: strip path separators so a crafted thread_id can't escape the base
    safe_id = thread_id.replace("/", "_").replace("\\", "_").strip(".") or "default"
    d = _NOTES_BASE / safe_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _note_path(key: str) -> Path:
    """Return the file path for a note key, rejecting traversal attempts."""
    # Keys become filenames — strip any path components
    safe_key = Path(key).name.replace("/", "_").replace("\\", "_").strip(".") or "note"
    if not safe_key:
        safe_key = "note"
    return _notes_dir() / f"{safe_key}.json"


def _read_note(key: str) -> dict | None:
    path = _note_path(key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_note(key: str, content: str) -> None:
    path = _note_path(key)
    data = {
        "key": key,
        "content": content,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@tool
def notes_write(key: str, content: str) -> str:
    """Create or overwrite a named note with the given content.

    Notes are scoped to the current conversation thread so parallel agents
    don't interfere with each other.  Use notes_append() to add to an
    existing note without replacing it.

    Parameters
    ----------
    key     : short identifier, e.g. "outline", "sources", "decisions"
    content : the full text to store
    """
    if not key.strip():
        return "[notes_write] Error: key cannot be empty."
    content = content[:_MAX_CONTENT_CHARS]
    try:
        _write_note(key.strip(), content)
        return f"[notes] Saved '{key}' ({len(content)} chars)."
    except Exception as exc:
        return f"[notes_write] Error: {exc}"


@tool
def notes_read(key: str) -> str:
    """Read a named note back in full.

    Returns the stored content verbatim, or a message if the key does not
    exist.  Use notes_list() first if you are unsure which keys exist.

    Parameters
    ----------
    key : the note identifier used when it was written
    """
    if not key.strip():
        return "[notes_read] Error: key cannot be empty."
    note = _read_note(key.strip())
    if note is None:
        return f"[notes] No note found for key '{key}'. Use notes_list() to see available keys."
    updated = note.get("updated_at", "unknown")
    content = note.get("content", "")
    return f"[note: {key}  updated: {updated}]\n\n{content}"


@tool
def notes_append(key: str, line: str) -> str:
    """Append a line to an existing note (creates the note if it doesn't exist).

    Useful for building up a running log, adding a source to a list, or
    ticking off a task without rewriting the whole note.

    Parameters
    ----------
    key  : the note identifier
    line : text to append (a newline is added automatically before it)
    """
    if not key.strip():
        return "[notes_append] Error: key cannot be empty."
    existing = _read_note(key.strip())
    current = existing.get("content", "") if existing else ""
    separator = "\n" if current and not current.endswith("\n") else ""
    new_content = current + separator + line
    if len(new_content) > _MAX_CONTENT_CHARS:
        return (
            f"[notes_append] Error: note '{key}' would exceed {_MAX_CONTENT_CHARS} chars. "
            "Use notes_write() to replace it or read and trim it first."
        )
    try:
        _write_note(key.strip(), new_content)
        return f"[notes] Appended to '{key}' ({len(new_content)} chars total)."
    except Exception as exc:
        return f"[notes_append] Error: {exc}"


@tool
def notes_list() -> str:
    """List all note keys available in the current conversation thread.

    Returns key names, sizes, and last-updated timestamps so the agent
    can decide which notes to read without loading all of them at once.
    """
    try:
        d = _notes_dir()
        files = sorted(d.glob("*.json"))[:_MAX_LIST_NOTES]
        if not files:
            return "[notes] No notes in this thread yet. Use notes_write() to create one."
        lines = [f"Notes in thread '{d.name}':"]
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                key     = data.get("key", f.stem)
                chars   = len(data.get("content", ""))
                updated = data.get("updated_at", "?")[:19]   # trim microseconds
                lines.append(f"  {key:<24}  {chars:>6} chars  updated {updated}")
            except Exception:
                lines.append(f"  {f.stem:<24}  (unreadable)")
        return "\n".join(lines)
    except Exception as exc:
        return f"[notes_list] Error: {exc}"


@tool
def notes_delete(key: str) -> str:
    """Delete a named note permanently.

    Parameters
    ----------
    key : the note identifier to remove
    """
    if not key.strip():
        return "[notes_delete] Error: key cannot be empty."
    path = _note_path(key.strip())
    if not path.exists():
        return f"[notes] No note found for key '{key}'."
    try:
        path.unlink()
        return f"[notes] Deleted '{key}'."
    except Exception as exc:
        return f"[notes_delete] Error: {exc}"


TOOLS = [notes_write, notes_read, notes_append, notes_list, notes_delete]