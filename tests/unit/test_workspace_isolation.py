"""git_context + code_search must follow the per-SESSION workspace.

[FIX-GIT-WORKSPACE] / [FIX-WORKSPACE] They resolved against the OS process
cwd. The web server deliberately never os.chdir()s per session (file_ops.py
FIX-6), so every session's git/search tools were pinned to wherever the
process launched, ignoring /dir.
"""
import shutil
import subprocess

import pytest

from plugins.code_search import _resolve_path, count_lines, find_files
from plugins.git_context import git_log


def test_resolve_path_is_relative_to_the_session_workspace(tmp_path, workspace):
    root = workspace(str(tmp_path))
    assert _resolve_path("sub/file.txt") == str((tmp_path / "sub" / "file.txt").resolve())
    assert _resolve_path(".") == root


def test_resolve_path_leaves_absolute_paths_alone(tmp_path, workspace):
    workspace(str(tmp_path))
    other = tmp_path.parent
    assert _resolve_path(str(other)) == str(other.resolve())


def test_find_files_sees_only_its_own_sessions_directory(tmp_path, workspace):
    s1, s2 = tmp_path / "s1", tmp_path / "s2"
    s1.mkdir(); s2.mkdir()
    (s1 / "only_in_session1.txt").write_text("x")
    (s2 / "only_in_session2.txt").write_text("y")
    # Assert on the full resolved PATH, not just the filename: the tool's
    # "No files matching '<pattern>'" message echoes the pattern, so a bare
    # filename check would pass even when the search ran in the wrong place.
    path1 = str((s1 / "only_in_session1.txt").resolve())
    path2 = str((s2 / "only_in_session2.txt").resolve())

    workspace(str(s1))
    assert path1 in find_files.func("only_in_session1.txt", ".")
    assert "No files matching" in find_files.func("only_in_session2.txt", ".")

    workspace(str(s2))
    assert path2 in find_files.func("only_in_session2.txt", ".")
    assert "No files matching" in find_files.func("only_in_session1.txt", ".")


def test_count_lines_resolves_relative_to_the_workspace(tmp_path, workspace):
    (tmp_path / "f.txt").write_text("a\nb\nc\n")
    workspace(str(tmp_path))
    assert "3" in count_lines.func("f.txt")


def _init_repo(path, message):
    git = lambda *a: subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "tester")
    (path / "f.txt").write_text(message)
    git("add", ".")
    git("commit", "-q", "-m", message)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_tools_follow_the_session_workspace(tmp_path, workspace):
    r1, r2 = tmp_path / "repo1", tmp_path / "repo2"
    r1.mkdir(); r2.mkdir()
    _init_repo(r1, "commit-in-repo1")
    _init_repo(r2, "commit-in-repo2")

    workspace(str(r1))
    out1 = git_log.func()
    workspace(str(r2))
    out2 = git_log.func()

    assert "commit-in-repo1" in out1 and "commit-in-repo2" not in out1
    assert "commit-in-repo2" in out2 and "commit-in-repo1" not in out2
