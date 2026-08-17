"""
Beaver file operation tools.

Fix log
-------
[FIX-1] Added WORKSPACE_ROOT and _safe_path() path-traversal guard.
        Without it, read_file("/etc/passwd"), write_file("/etc/crontab", ...)
        and list_directory("/root/.ssh") were all valid LLM tool calls.

        _safe_path() resolves the real absolute path (following symlinks) and
        raises PermissionError if it falls outside WORKSPACE_ROOT, which
        defaults to the current working directory at startup.

        BEAVER_WORKSPACE env var overrides the root — useful when running
        inside a container or a sandboxed project directory.

[FIX-2] WORKSPACE_ROOT is now a module-level variable (not a constant) so
        that set_workspace() and the CLI /dir command can update it at
        runtime without restarting Beaver.

[FIX-3] Added get_workspace() tool so the agent always knows its current
        working directory and can report it to the user accurately.

[FIX-4] Added set_workspace() function (not a tool — called by CLI /dir)
        so the CLI can update WORKSPACE_ROOT and os.chdir() atomically.

[FIX-5] Added replace_in_file() tool. Before this, editing part of an
        existing file meant write_file()-ing the ENTIRE file back — costly
        and risky on anything near/over MAX_READ_CHARS (4000), since
        read_file() truncates past that point and the model would be
        regenerating content it never actually saw in full. replace_in_file
        edits a targeted, uniquely-matched substring in place instead —
        no full-file round-trip required.

[FIX-6] Workspace root is now contextvar-backed (_workspace_var), not a bare
        module global. Two problems with the old module global:
          1. os.chdir() is process-wide — one WebSocket session's /dir call
             would move every other concurrent session's file tools too.
          2. _safe_path() resolved relative paths via os.path.abspath(),
             which resolves against the process's cwd. Even fixing (1) by
             not calling os.chdir() per-session would leave relative paths
             silently resolving against whatever cwd the process happened
             to have — not this session's workspace.
        _resolve() below joins relative paths against the *active* workspace
        root explicitly (get_active_workspace_root()) and never touches
        os.getcwd(). set_workspace() (CLI/TUI — one process, one session)
        keeps the os.chdir() convenience it always had. set_session_workspace()
        (web server — many concurrent sessions) sets only the contextvar.
"""
import contextvars
import os
from typing import Optional
from langchain_core.tools import tool

MAX_READ_CHARS = 4000

# ── Workspace root ─────────────────────────────────────────────────────────────
# All file operations are restricted to this directory tree.  Set
# BEAVER_WORKSPACE in .env to override (e.g. a dedicated project folder).
# FIX-2: mutable module variable so /dir command can update it at runtime.
# FIX-6: this is now the *default* root (CLI/TUI, no session context).
WORKSPACE_ROOT: str = os.path.realpath(
    os.getenv("BEAVER_WORKSPACE", os.getcwd())
)

# FIX-6: per-session override, set by web/server.py per WebSocket connection.
# None everywhere else, in which case get_active_workspace_root() falls back
# to WORKSPACE_ROOT above — no behavior change for CLI/TUI/one-shot/A2A.
_workspace_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "beaver_session_workspace", default=None
)


def get_active_workspace_root() -> str:
    """Return the current session's workspace root, or the process default."""
    return _workspace_var.get() or WORKSPACE_ROOT


def set_workspace(path: str) -> str:
    """Update the workspace root at runtime (called by CLI /dir command).

    Changes both the module-level WORKSPACE_ROOT and os.chdir() so that
    relative paths used by all tools resolve correctly.

    Single-session use only (CLI/TUI) — os.chdir() is process-wide, so
    calling this from a multi-connection server would move every other
    session's workspace too. Use set_session_workspace() there instead.

    Returns the new absolute path on success, raises ValueError on bad path.
    """
    global WORKSPACE_ROOT
    resolved = os.path.realpath(os.path.abspath(path))
    if not os.path.isdir(resolved):
        raise ValueError(f"'{path}' is not a directory or does not exist.")
    WORKSPACE_ROOT = resolved
    os.chdir(resolved)
    return resolved


def set_session_workspace(path: str) -> str:
    """Set the workspace root for the *current session only* (FIX-6).

    Used by the web server's /dir handler. Sets only the contextvar — never
    calls os.chdir(), so concurrent WebSocket sessions on the same process
    don't affect each other's working directory. Raises ValueError on bad path.
    """
    resolved = os.path.realpath(os.path.abspath(path))
    if not os.path.isdir(resolved):
        raise ValueError(f"'{path}' is not a directory or does not exist.")
    _workspace_var.set(resolved)
    return resolved


def _resolve(path: str) -> str:
    """Resolve *path* against the active workspace root — never os.getcwd().

    FIX-6: relative paths must resolve against this session's workspace, not
    the OS process's current directory (which is shared across every
    concurrent session and may not match any of them).
    """
    root = get_active_workspace_root()
    if os.path.isabs(path):
        return os.path.realpath(path)
    return os.path.realpath(os.path.join(root, path))


def _safe_path(path: str) -> str:
    """Resolve *path* and assert it stays within the active workspace root.

    Returns the resolved absolute path string on success.
    Raises PermissionError if the path escapes the workspace.
    """
    root = get_active_workspace_root()
    resolved = _resolve(path)
    if not resolved.startswith(root + os.sep) and resolved != root:
        raise PermissionError(
            f"Path '{path}' resolves to '{resolved}' which is outside the "
            f"workspace root '{root}'. "
            f"Use /dir <path> to change the workspace, or set "
            f"BEAVER_WORKSPACE in .env."
        )
    return resolved


@tool
def get_workspace() -> str:
    """Return the current workspace root directory.

    Always call this when you need to know the current working directory,
    construct absolute paths, or verify where files will be written.
    Returns the absolute path of the workspace root.
    """
    root = get_active_workspace_root()
    return f"Workspace root: {root}\nCurrent directory: {root}"


@tool
def read_file(path: str) -> str:
    """
    Reads the contents of a file.
    Use this to inspect code, configuration files, or text logs.
    """
    try:
        safe = _safe_path(path)  # FIX-1
    except PermissionError as e:
        return f"ERROR: {e}"
    if not os.path.exists(safe):
        return f"ERROR: File not found: {path}"
    try:
        with open(safe, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > MAX_READ_CHARS:
            return content[:MAX_READ_CHARS] + f"\n\n[... Truncated ({len(content)} total chars) ...]"
        return content
    except Exception as e:
        return f"ERROR: Failed to read file: {str(e)}"


@tool
def write_file(path: str, content: str) -> str:
    """
    Writes or overwrites content to a file.
    Automatically creates missing parent directories.
    """
    try:
        # Resolve the parent first so makedirs doesn't create dirs outside root.
        # FIX-6: resolve against the active session's workspace, not os.getcwd().
        root = get_active_workspace_root()
        raw_target = path if os.path.isabs(path) else os.path.join(root, path)
        parent = os.path.dirname(raw_target)
        _safe_path(parent)           # FIX-1: check parent dir is in-scope
        safe = os.path.realpath(raw_target)
        # Verify the full path too (realpath before the file exists)
        if not safe.startswith(root + os.sep) and safe != root:
            raise PermissionError(
                f"Path '{path}' resolves outside workspace root '{root}'."
            )
        os.makedirs(os.path.dirname(safe), exist_ok=True)
        with open(safe, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} characters to {path}"
    except PermissionError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: Failed to write file: {str(e)}"


@tool
def replace_in_file(path: str, old_str: str, new_str: str) -> str:
    """
    Replace one exact, unique occurrence of old_str with new_str in an
    existing file — without reading or rewriting the rest of the file.

    Use this instead of write_file whenever you're editing PART of an
    existing file. write_file overwrites the entire file, which means
    regenerating content you may not have fully seen (read_file truncates
    past MAX_READ_CHARS) and risks silently dropping unrelated sections on
    a large file. replace_in_file only touches the exact text you target.

    old_str must match the file's raw current content exactly — including
    whitespace and indentation — and must occur exactly once. If it's not
    found, or found more than once, this returns an error instead of
    guessing; read_file (or search_in_files) first to confirm the exact
    current text, then widen old_str with more surrounding context if it
    isn't unique yet.
    """
    try:
        safe = _safe_path(path)  # FIX-1
    except PermissionError as e:
        return f"ERROR: {e}"
    if not os.path.exists(safe):
        return f"ERROR: File not found: {path}"
    if old_str == "":
        return "ERROR: old_str must not be empty."

    try:
        with open(safe, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return f"ERROR: Failed to read file: {str(e)}"

    count = content.count(old_str)
    if count == 0:
        return (
            "ERROR: old_str not found in file — it must match the file's "
            "current content exactly, including whitespace/indentation. "
            "Use read_file or search_in_files to confirm the exact text first."
        )
    if count > 1:
        return (
            f"ERROR: old_str appears {count} times in the file — it must be "
            "unique. Include more surrounding context in old_str so it "
            "matches only the one occurrence you want to change."
        )

    new_content = content.replace(old_str, new_str, 1)
    try:
        with open(safe, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return f"ERROR: Failed to write file: {str(e)}"

    delta = len(new_content) - len(content)
    return (
        f"Successfully replaced 1 occurrence in {path} "
        f"({len(old_str)} chars -> {len(new_str)} chars, file size delta: {delta:+d})"
    )


@tool
def append_file(path: str, content: str) -> str:
    """Appends text content to the end of an existing file."""
    try:
        safe = _safe_path(path)  # FIX-1
    except PermissionError as e:
        return f"ERROR: {e}"
    try:
        if not os.path.exists(safe):
            return f"ERROR: File does not exist to append: {path}"
        with open(safe, "a", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully appended {len(content)} characters to {path}"
    except Exception as e:
        return f"ERROR: Failed to append to file: {str(e)}"


@tool
def list_directory(path: str = ".") -> str:
    """
    Lists files and directories at the given path.
    Shows type (file/dir), size in bytes, and name for each entry.
    Defaults to the current working directory if no path is given.
    """
    try:
        safe = _safe_path(path)  # FIX-1
    except PermissionError as e:
        return f"ERROR: {e}"
    if not os.path.exists(safe):
        return f"ERROR: Path not found: {path}"
    if not os.path.isdir(safe):
        return f"ERROR: Not a directory: {path}"
    try:
        entries = sorted(os.scandir(safe), key=lambda e: (not e.is_dir(), e.name.lower()))
        if not entries:
            return f"(empty directory: {safe})"
        lines = [f"Contents of {safe}:\n"]
        for entry in entries:
            try:
                size = entry.stat().st_size if entry.is_file() else ""
                kind = "dir " if entry.is_dir() else "file"
                size_str = f"  {size:>10} bytes" if size != "" else "            "
                lines.append(f"  [{kind}]{size_str}  {entry.name}")
            except PermissionError:
                lines.append(f"  [????]               {entry.name}  (permission denied)")
        return "\n".join(lines)
    except PermissionError:
        return f"ERROR: Permission denied reading directory: {path}"
    except Exception as e:
        return f"ERROR: Failed to list directory: {str(e)}"