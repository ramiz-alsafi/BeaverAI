"""
Beaver Plugin — Code Search
=============================

Fast file and code search using ripgrep (preferred) or grep (fallback).
No extra Python dependencies — uses subprocess against system binaries.

Useful for:
  coder      — find function definitions, usages, TODOs across a codebase
  researcher — scan research files for keywords, cross-reference notes
  standard   — search any local file tree

Tools
-----
  search_in_files  — search for a pattern inside file contents (grep/rg)
  find_files       — find files by name pattern (find)
  count_lines      — line/word/char count for a file or directory summary
"""

import subprocess
import shutil
from pathlib import Path
from langchain_core.tools import tool
from skills.file_ops import get_active_workspace_root

PERSONA  = ["coder", "researcher", "standard", "pentester", "orchestrator"]
ENABLED  = True

_TIMEOUT    = 20   # seconds
_MAX_LINES  = 80   # max output lines — prevents context flooding
_MAX_FILES  = 100  # max files returned by find_files


def _resolve_path(path: str) -> str:
    """Resolve *path* against the active SESSION's workspace root, not the
    raw OS process cwd.

    [FIX-WORKSPACE] Same root cause as git_context.py's identical fix (see
    its docstring for the full rationale, rooted in file_ops.py's FIX-6):
    Path(path).resolve() resolves a relative path (including the "."
    default every tool below used) against os.getcwd(), which the web
    server never updates per-session on purpose. Every search_in_files /
    find_files / count_lines call with a relative path was silently
    ignoring /dir on the web server and always operating on wherever the
    process happened to launch, the same directory for every concurrent
    user regardless of their own workspace.
    """
    p = Path(path).expanduser()
    if p.is_absolute():
        return str(p.resolve())
    return str((Path(get_active_workspace_root()) / p).resolve())


def _run(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            args, capture_output=True, text=True,
            timeout=_TIMEOUT, cwd=cwd,
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"Command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {_TIMEOUT}s"
    except Exception as exc:
        return -1, "", str(exc)


def _has(binary: str) -> bool:
    return shutil.which(binary) is not None


@tool
def search_in_files(pattern: str, path: str = ".", file_glob: str = "",
                    case_sensitive: bool = False, max_results: int = 50) -> str:
    """Search for a text pattern inside files under the given path.

    Uses ripgrep (rg) if available, falls back to grep.
    Returns matched lines with file path and line number.

    Parameters
    ----------
    pattern        : search pattern (literal text or regex)
    path           : directory to search (default: current directory)
    file_glob      : optional glob to limit file types, e.g. "*.py" or "*.go"
    case_sensitive : whether the search is case-sensitive (default False)
    max_results    : cap on returned matches (default 50, max 200)
    """
    max_results = min(max(1, max_results), 200)
    search_path = _resolve_path(path)

    if _has("rg"):
        cmd = ["rg", "--line-number", "--no-heading", "--color=never",
               f"--max-count={max_results}"]
        if not case_sensitive:
            cmd.append("--ignore-case")
        if file_glob:
            cmd += ["--glob", file_glob]
        cmd += [pattern, search_path]
    elif _has("grep"):
        cmd = ["grep", "--recursive", "--line-number", "--color=never",
               f"--max-count={max_results}"]
        if not case_sensitive:
            cmd.append("--ignore-case")
        if file_glob:
            cmd += ["--include", file_glob]
        cmd += [pattern, search_path]
    else:
        # ── Python fallback — no rg or grep on PATH (e.g. Windows) ───────────
        # Matches the convention already used by find_files and count_lines:
        # when the system binary is missing, fall back to stdlib rather than
        # returning a dead-end error that the model will retry identically.
        import re as _re
        import os as _os

        try:
            flags = 0 if case_sensitive else _re.IGNORECASE
            compiled = _re.compile(pattern, flags)
        except _re.error as exc:
            return f"[search_in_files] Invalid regex pattern: {exc}"

        py_matches: list[str] = []
        try:
            for root, dirs, files in _os.walk(search_path):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for filename in files:
                    if file_glob and not Path(filename).match(file_glob):
                        continue
                    filepath = Path(root) / filename
                    try:
                        text = filepath.read_text(errors="replace")
                        for lineno, line in enumerate(text.splitlines(), 1):
                            if compiled.search(line):
                                try:
                                    rel = str(filepath.relative_to(search_path))
                                except ValueError:
                                    rel = str(filepath)
                                py_matches.append(f"{rel}:{lineno}:{line.rstrip()}")
                                if len(py_matches) >= max_results:
                                    break
                    except (PermissionError, OSError):
                        continue
                if len(py_matches) >= max_results:
                    break
        except Exception as exc:
            return f"[search_in_files] Python fallback error: {exc}"

        if not py_matches:
            return f"[search_in_files] No matches for '{pattern}' in {search_path}"

        truncated = ""
        if len(py_matches) > _MAX_LINES:
            truncated = (
                f"\n[... {len(py_matches) - _MAX_LINES} more lines — "
                f"narrow your pattern to see the rest ...]"
            )
            py_matches = py_matches[:_MAX_LINES]

        header = (
            f"Matches for '{pattern}' in {search_path}"
            f" (Python fallback — install ripgrep for faster search)"
        )
        if file_glob:
            header += f" (files: {file_glob})"
        return f"{header}\n\n" + "\n".join(py_matches) + truncated

    rc, out, err = _run(cmd)
    if rc not in (0, 1):   # grep returns 1 for "no match" — that's fine
        return f"[search_in_files] Error: {err.strip() or 'unknown'}"

    lines = out.strip().splitlines()
    if not lines:
        return f"[search_in_files] No matches for '{pattern}' in {search_path}"

    truncated = ""
    if len(lines) > _MAX_LINES:
        # [FIX-SEARCH-MSG] max_results maps to rg/grep's --max-count, which is
        # a PER-FILE cap, not a global one — across many files, total matched
        # lines routinely exceed max_results even when it's respected
        # correctly, and this display truncation (_MAX_LINES) is a separate,
        # fixed cap that doesn't change with max_results at all. The old
        # message told the model to "increase max_results" to see more,
        # which doesn't help (and can make the per-file match count, and so
        # this truncation, worse) — dropped that suggestion.
        truncated = f"\n[... {len(lines) - _MAX_LINES} more lines — narrow your pattern to see the rest ...]"
        lines = lines[:_MAX_LINES]

    header = f"Matches for '{pattern}' in {search_path}"
    if file_glob:
        header += f" (files: {file_glob})"
    return f"{header}\n\n" + "\n".join(lines) + truncated


@tool
def find_files(name_pattern: str, path: str = ".", file_type: str = "any") -> str:
    """Find files by name pattern under the given directory.

    Uses the system 'find' command. Supports wildcards in name_pattern.
    Useful for locating config files, source modules, or any file by name.

    Parameters
    ----------
    name_pattern : filename pattern with wildcards, e.g. "*.py", "config.*", "main.go"
    path         : root directory to search from (default: current directory)
    file_type    : "file", "dir", or "any" (default "any")
    """
    search_path = _resolve_path(path)

    if not _has("find"):
        # Windows fallback — use Python's pathlib
        try:
            p = Path(search_path)
            matches = list(p.rglob(name_pattern))[:_MAX_FILES]
            if not matches:
                return f"[find_files] No files matching '{name_pattern}' in {search_path}"
            return f"Files matching '{name_pattern}' in {search_path}:\n\n" + \
                   "\n".join(str(m) for m in matches)
        except Exception as exc:
            return f"[find_files] Error: {exc}"

    cmd = ["find", search_path, "-name", name_pattern]
    if file_type == "file":
        cmd += ["-type", "f"]
    elif file_type == "dir":
        cmd += ["-type", "d"]

    rc, out, err = _run(cmd)
    if rc != 0:
        return f"[find_files] Error: {err.strip() or 'unknown'}"

    lines = out.strip().splitlines()
    if not lines:
        return f"[find_files] No files matching '{name_pattern}' in {search_path}"

    truncated = ""
    if len(lines) > _MAX_FILES:
        truncated = f"\n[... {len(lines) - _MAX_FILES} more results omitted ...]"
        lines = lines[:_MAX_FILES]

    return f"Files matching '{name_pattern}' in {search_path}:\n\n" + \
           "\n".join(lines) + truncated


@tool
def count_lines(path: str) -> str:
    """Count lines, words, and characters in a file, or summarise a directory.

    For a file: returns wc-style stats.
    For a directory: returns a breakdown by extension showing file count and
    total line count — useful for codebase orientation.

    Parameters
    ----------
    path : path to a file or directory
    """
    p = Path(_resolve_path(path))

    if not p.exists():
        return f"[count_lines] Path does not exist: {path}"

    if p.is_file():
        if not _has("wc"):
            # Python fallback
            try:
                text = p.read_text(errors="replace")
                lines = text.count("\n")
                words = len(text.split())
                chars = len(text)
                return f"{path}\n  Lines: {lines:,}\n  Words: {words:,}\n  Chars: {chars:,}"
            except Exception as exc:
                return f"[count_lines] Error reading file: {exc}"
        rc, out, err = _run(["wc", "-lwc", str(p)])
        if rc != 0:
            return f"[count_lines] Error: {err.strip()}"
        parts = out.split()
        if len(parts) >= 3:
            return f"{path}\n  Lines: {parts[0]:>8}\n  Words: {parts[1]:>8}\n  Chars: {parts[2]:>8}"
        return out.strip()

    # Directory summary
    ext_stats: dict[str, list[int]] = {}   # ext → [file_count, line_count]
    try:
        for f in p.rglob("*"):
            if not f.is_file():
                continue
            ext = f.suffix.lower() or "(no ext)"
            if ext not in ext_stats:
                ext_stats[ext] = [0, 0]
            ext_stats[ext][0] += 1
            try:
                ext_stats[ext][1] += f.read_text(errors="replace").count("\n")
            except Exception:
                pass
    except PermissionError as exc:
        return f"[count_lines] Permission denied: {exc}"

    if not ext_stats:
        return f"[count_lines] No files found in {path}"

    rows = sorted(ext_stats.items(), key=lambda x: -x[1][1])
    header = f"{'Extension':<14}  {'Files':>6}  {'Lines':>10}"
    sep    = "-" * 34
    lines  = [f"Directory: {path}", header, sep]
    total_files = total_lines = 0
    for ext, (fc, lc) in rows[:30]:
        lines.append(f"{ext:<14}  {fc:>6}  {lc:>10,}")
        total_files += fc
        total_lines += lc
    lines.append(sep)
    lines.append(f"{'TOTAL':<14}  {total_files:>6}  {total_lines:>10,}")
    return "\n".join(lines)


TOOLS = [search_in_files, find_files, count_lines]
