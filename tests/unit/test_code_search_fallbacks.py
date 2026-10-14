"""plugins.code_search — the pure-Python fallbacks, which are the DEFAULT path on Windows.

[FIX-WIN-TOOLS] shutil.which("find") is truthy on Windows because System32\\find.exe is a
different program (a text-search tool) that fails on `find <dir> -name <pat>`; so find_files
ran it and returned an error instead of using the fallback written for exactly that case.
On Linux/macOS CI those fallbacks never run at all (rg/grep/find/wc are always there), so
they were untested — these tests force them, and fail loudly if anything shells out.
"""
import pytest

import plugins.code_search as cs


@pytest.fixture
def windows_like(monkeypatch):
    """Pretend to be Windows: the POSIX tools 'exist' on PATH (as System32\\find.exe does),
    and any attempt to launch a subprocess is a test failure."""
    monkeypatch.setattr(cs, "_IS_WINDOWS", True)
    monkeypatch.setattr(cs.shutil, "which", lambda name: f"C:\\fake\\{name}.exe")

    def no_subprocess(*a, **kw):
        raise AssertionError(f"shelled out on 'Windows': {a[0] if a else kw}")

    monkeypatch.setattr(cs.subprocess, "run", no_subprocess)


# ── _has ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", ["find", "grep", "wc"])
def test_posix_only_tools_are_never_trusted_on_windows(windows_like, tool):
    assert cs._has(tool) is False


def test_cross_platform_binaries_are_still_used_on_windows(windows_like):
    assert cs._has("rg") is True and cs._has("git") is True


def test_posix_tools_are_used_normally_elsewhere(monkeypatch):
    monkeypatch.setattr(cs, "_IS_WINDOWS", False)
    monkeypatch.setattr(cs.shutil, "which", lambda name: "/usr/bin/" + name)
    assert cs._has("find") and cs._has("grep") and cs._has("wc")


# ── find_files ──────────────────────────────────────────────────────────────

@pytest.fixture
def tree(tmp_path, workspace):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("y", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data.txt").write_text("z", encoding="utf-8")
    workspace(str(tmp_path))
    return tmp_path


def test_find_files_uses_the_fallback_not_windows_find_exe(windows_like, tree):
    out = cs.find_files.func("*.py", ".")
    assert str(tree / "a.py") in out and str(tree / "sub" / "b.py") in out
    assert "Error" not in out


def test_find_files_fallback_honours_file_type(windows_like, tree):
    only_dirs = cs.find_files.func("data*", ".", "dir")
    only_files = cs.find_files.func("data*", ".", "file")
    assert str(tree / "data") in only_dirs and "data.txt" not in only_dirs
    assert "data.txt" in only_files and str(tree / "data") + "\n" not in only_files + "\n"


def test_find_files_fallback_reports_no_match(windows_like, tree):
    assert "No files matching" in cs.find_files.func("*.nonexistent", ".")


# ── search_in_files ─────────────────────────────────────────────────────────

@pytest.fixture
def no_rg(windows_like, monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", lambda name: None)   # rg absent too -> pure Python


def test_search_fallback_finds_matches_with_path_and_line_number(no_rg, tree):
    (tree / "notes.txt").write_text("first\nhas NEEDLE here\nlast\n", encoding="utf-8")
    out = cs.search_in_files.func("needle", ".")        # case-insensitive by default
    assert "notes.txt:2:has NEEDLE here" in out.replace("\\", "/")


def test_search_fallback_respects_case_sensitivity_and_file_glob(no_rg, tree):
    (tree / "a.py").write_text("Needle\n", encoding="utf-8")
    (tree / "b.md").write_text("Needle\n", encoding="utf-8")
    assert "No matches" in cs.search_in_files.func("needle", ".", case_sensitive=True)
    only_py = cs.search_in_files.func("needle", ".", file_glob="*.py")
    assert "a.py" in only_py and "b.md" not in only_py


def test_search_fallback_reports_a_bad_regex_instead_of_crashing(no_rg, tree):
    assert "Invalid regex" in cs.search_in_files.func("([unclosed", ".")


def test_search_fallback_never_exceeds_max_results(no_rg, tree):
    """Several files in ONE directory all match: the old inner `break` only left the line
    loop, so every remaining file in that directory still added a match."""
    for i in range(12):
        (tree / f"m{i:02}.txt").write_text("needle\n", encoding="utf-8")
    out = cs.search_in_files.func("needle", ".", max_results=3)
    assert out.count(":1:needle") == 3


def test_search_fallback_reads_utf8_not_the_locale_codepage(no_rg, tree):
    """Windows' default codec is cp1252: without an explicit utf-8 an Arabic pattern never matches."""
    (tree / "ar.txt").write_text("السلام عليكم\n", encoding="utf-8")
    assert "ar.txt:1:" in cs.search_in_files.func("عليكم", ".").replace("\\", "/")


# ── count_lines ─────────────────────────────────────────────────────────────

def test_count_lines_fallback_counts_without_wc(windows_like, tree):
    (tree / "f.txt").write_text("a\nb\nc\n", encoding="utf-8", newline="\n")
    out = cs.count_lines.func("f.txt")
    assert "3" in out and "Error" not in out
