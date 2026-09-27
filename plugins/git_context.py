"""
Beaver Plugin — Git Context
============================

Exposes the local git repo state to the agent without going through the
heavyweight os_exec + scope-gate path.  Three focused read-only tools that
give coder and orchestrator real awareness of what's actually changed.

No API key required.  Uses subprocess against the repo that contains the
current working directory.  Falls back gracefully when git is not installed
or the cwd is not inside a repo.

Activate
--------
  Already enabled — no pip installs needed.
  Drop this file in plugins/ and restart Beaver (or it hot-reloads).

Tools
-----
  git_status      — show working tree state (modified, staged, untracked)
  git_log         — last N commits with author, date, and subject line
  git_diff        — unified diff (staged, unstaged, or a specific file)
"""

import subprocess
from langchain_core.tools import tool
from skills.file_ops import get_active_workspace_root

PERSONA = ["coder", "orchestrator", "standard", "researcher"]
ENABLED = True

_TIMEOUT = 15  # seconds — git operations should never hang this long


def _run(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a git sub-command, return (returncode, stdout, stderr).

    [FIX-GIT-WORKSPACE] cwd now defaults to the ACTIVE SESSION's workspace
    root (get_active_workspace_root()), not whatever the OS process's cwd
    happens to be. Previously every git_* call let subprocess inherit
    os.getcwd() unconditionally — harmless for the CLI (one session per
    process, and /dir there does call os.chdir()), but broken for the web
    server: set_session_workspace() deliberately never calls os.chdir()
    (see file_ops.py's own FIX-6) specifically so one user's /dir doesn't
    move every other concurrent session's tools. Without this, git_status
    / git_log / git_diff ignored /dir entirely on the web server and
    stayed pinned to wherever the process happened to launch, the same
    way file_ops.py's tools used to before FIX-6 fixed it there.
    """
    if cwd is None:
        cwd = get_active_workspace_root()
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            cwd=cwd,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return -1, "", "git is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return -1, "", f"git command timed out after {_TIMEOUT}s"
    except Exception as exc:
        return -1, "", f"Unexpected error: {exc}"


def _repo_root() -> str | None:
    """Return the absolute path of the current repo root, or None."""
    rc, out, _ = _run(["rev-parse", "--show-toplevel"])
    return out.strip() if rc == 0 else None


@tool
def git_status(short: bool = False) -> str:
    """Show the working tree status of the current git repository.

    Reports which files are staged, modified, untracked, or deleted,
    along with the current branch name and ahead/behind tracking info.

    Parameters
    ----------
    short : bool
        If True return the compact --short format (one line per file).
        Defaults to False (porcelain v2 with branch info).
    """
    root = _repo_root()
    if root is None:
        return "[git_status] Not inside a git repository."

    # Branch and tracking info
    rc_b, branch_out, _ = _run(["branch", "--show-current"])
    branch = branch_out.strip() if rc_b == 0 else "unknown"

    # Ahead/behind upstream
    rc_ab, ab_out, _ = _run(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
    ahead_behind = ""
    if rc_ab == 0 and ab_out.strip():
        parts = ab_out.strip().split()
        if len(parts) == 2:
            ahead, behind = parts
            ahead_behind = f"  ahead {ahead}  behind {behind}"

    # File status
    fmt_args = ["status", "--short"] if short else ["status"]
    rc, out, err = _run(fmt_args)
    if rc != 0:
        return f"[git_status] Error: {err.strip()}"

    header = f"Branch: {branch}{ahead_behind}\nRepo:   {root}\n\n"
    body = out if out.strip() else "nothing to commit, working tree clean"
    return header + body


@tool
def git_log(n: int = 10, file: str = "") -> str:
    """Show the last N git commits with author, date, and subject.

    Parameters
    ----------
    n    : int  — number of commits to show (default 10, max 50)
    file : str  — optional path to filter commits that touched this file
    """
    n = min(max(1, n), 50)

    args = [
        "log",
        f"-{n}",
        "--pretty=format:%h  %ad  %an  %s",
        "--date=short",
    ]
    if file:
        args += ["--", file]

    rc, out, err = _run(args)
    if rc != 0:
        return f"[git_log] Error: {err.strip()}"
    if not out.strip():
        return "[git_log] No commits found."

    header = f"Last {n} commit(s)"
    if file:
        header += f" touching '{file}'"
    return f"{header}:\n\n{out}"


@tool
def git_diff(staged: bool = False, file: str = "", context_lines: int = 3) -> str:
    """Show a unified diff of changes in the current repository.

    By default shows unstaged changes across all tracked files.
    Pass staged=True to see what is ready to commit.
    Pass a file path to narrow the diff to one file.

    Parameters
    ----------
    staged        : bool — diff staged (index) changes instead of working tree
    file          : str  — limit diff to this file path (relative to repo root)
    context_lines : int  — lines of context around each hunk (default 3, max 10)
    """
    context_lines = min(max(0, context_lines), 10)

    args = ["diff", f"--unified={context_lines}"]
    if staged:
        args.append("--staged")
    if file:
        args += ["--", file]

    rc, out, err = _run(args)
    if rc != 0:
        return f"[git_diff] Error: {err.strip()}"
    if not out.strip():
        label = "staged" if staged else "unstaged"
        target = f" in '{file}'" if file else ""
        return f"[git_diff] No {label} changes{target}."

    # Truncate large diffs so they don't flood the context window
    MAX_CHARS = 6000
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS] + f"\n\n[... diff truncated — {len(out) - MAX_CHARS} chars omitted ...]"

    return out


TOOLS = [git_status, git_log, git_diff]
