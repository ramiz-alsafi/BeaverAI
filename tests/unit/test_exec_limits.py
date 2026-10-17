"""skills.os_exec resource limits (FIX-27) and skills.file_ops._is_within (FIX-7)."""
import asyncio
import os
import sys

import pytest

from skills import file_ops
from skills.os_exec import AsyncProcessRunner, clamp_timeout, max_output_bytes, max_timeout


# ── timeout clamp ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (30, 30), (1, 1), (600, 600),
    (601, 600), (10**9, 600),          # capped
    (0, 60), (-5, 60), (None, 60),     # never "unlimited"
    ("45", 45), ("abc", 60), (2.9, 2),
])
def test_clamp_timeout(monkeypatch, value, expected):
    monkeypatch.delenv("BEAVER_EXEC_MAX_TIMEOUT", raising=False)
    assert clamp_timeout(value) == expected


def test_limits_come_from_env(monkeypatch):
    monkeypatch.setenv("BEAVER_EXEC_MAX_TIMEOUT", "5")
    monkeypatch.setenv("BEAVER_EXEC_MAX_OUTPUT", "123")
    assert max_timeout() == 5 and clamp_timeout(999) == 5
    assert max_output_bytes() == 123


@pytest.mark.parametrize("bad", ["", "0", "-1", "lots"])
def test_bad_env_values_fall_back_to_defaults(monkeypatch, bad):
    monkeypatch.setenv("BEAVER_EXEC_MAX_TIMEOUT", bad)
    monkeypatch.setenv("BEAVER_EXEC_MAX_OUTPUT", bad)
    assert max_timeout() == 600 and max_output_bytes() == 1_000_000


# ── output cap (real subprocess) ────────────────────────────────────────────

def _python_cmd(tmp_path, source: str) -> str:
    script = tmp_path / "s.py"
    script.write_text(source, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def test_output_is_capped_but_the_command_still_finishes(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAVER_EXEC_MAX_OUTPUT", "2000")
    cmd = _python_cmd(tmp_path, "for _ in range(20000):\n    print('x' * 50)\n")
    code, out, err = asyncio.run(AsyncProcessRunner().execute(cmd, timeout=60, cwd=str(tmp_path)))
    assert code == 0                                  # drained, so the child never blocked
    assert len(out.encode()) < 2000 + 200             # cap + the one-line notice
    assert "truncated at 2000 bytes" in out


def test_small_output_is_untouched(tmp_path):
    cmd = _python_cmd(tmp_path, "print('hello')\n")
    code, out, err = asyncio.run(AsyncProcessRunner().execute(cmd, timeout=60, cwd=str(tmp_path)))
    assert code == 0 and out.strip() == "hello" and "truncated" not in out


def test_one_enormous_line_does_not_crash_the_tool(tmp_path):
    cmd = _python_cmd(tmp_path, "print('y' * 300_000)\nprint('after')\n")
    code, out, err = asyncio.run(AsyncProcessRunner().execute(cmd, timeout=60, cwd=str(tmp_path)))
    assert code == 0 and "over-long line dropped" in out


@pytest.mark.skipif(os.name == "nt", reason="process-tree kill is exercised on POSIX only")
def test_requested_timeout_is_clamped_to_the_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAVER_EXEC_MAX_TIMEOUT", "1")
    cmd = _python_cmd(tmp_path, "import time\ntime.sleep(30)\n")
    code, out, err = asyncio.run(AsyncProcessRunner().execute(cmd, timeout=10_000, cwd=str(tmp_path)))
    assert code == -1 and "timed out after 1s" in err


@pytest.mark.skipif(os.name == "nt", reason="process-tree kill is exercised on POSIX only")
def test_timeout_zero_is_not_unlimited(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAVER_EXEC_MAX_TIMEOUT", "1")
    cmd = _python_cmd(tmp_path, "import time\ntime.sleep(30)\n")
    code, out, err = asyncio.run(AsyncProcessRunner().execute(cmd, timeout=0, cwd=str(tmp_path)))
    assert code == -1 and "timed out" in err


# ── workspace containment ───────────────────────────────────────────────────

def test_is_within_basics(tmp_path):
    root = str(tmp_path / "ws")
    (tmp_path / "ws" / "sub").mkdir(parents=True)
    assert file_ops._is_within(root, root)
    assert file_ops._is_within(os.path.join(root, "sub", "a.txt"), root)
    assert not file_ops._is_within(str(tmp_path), root)                    # parent
    assert not file_ops._is_within(str(tmp_path / "ws-evil" / "a"), root)  # sibling sharing a prefix


def test_is_within_works_when_the_root_is_a_filesystem_root():
    """The old `startswith(root + os.sep)` check rejected everything here."""
    fs_root = os.path.abspath(os.sep)
    assert file_ops._is_within(os.path.join(fs_root, "anything", "x"), fs_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_is_within_ignores_case_on_windows():
    assert file_ops._is_within(r"c:\Users\Me\proj\a.txt", r"C:\users\me\PROJ")


@pytest.mark.skipif(os.name != "nt", reason="drive letters only exist on Windows")
def test_is_within_rejects_other_drives():
    assert not file_ops._is_within(r"D:\proj\a.txt", r"C:\proj")


def test_safe_path_still_blocks_escapes(tmp_path, workspace):
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace(str(ws))
    assert file_ops._safe_path("notes/a.txt").startswith(str(ws.resolve()))
    with pytest.raises(PermissionError):
        file_ops._safe_path("../outside.txt")
    with pytest.raises(PermissionError):
        file_ops._safe_path(str(tmp_path / "ws-evil" / "a.txt"))
