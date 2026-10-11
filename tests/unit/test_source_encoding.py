"""Text-encoding guards.

Background: skills/plugin_loader.py used to call Path.write_text() with no
encoding. On Windows that means cp1252, so the em dash in the auto-generated
plugins/__init__.py header was written as byte 0x97 — not valid UTF-8 — and the
broken file got committed. Linux never shows this (its default IS UTF-8), so
these tests simulate the Windows default instead of relying on the host OS.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
_TEXT_SUFFIXES = {".py", ".md", ".txt", ".yml", ".yaml", ".toml", ".json", ".example"}


def _repo_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        if _SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        yield path


def test_every_text_file_in_the_repo_is_valid_utf8():
    bad = []
    for path in _repo_text_files():
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            bad.append(f"{path.relative_to(ROOT)}: {exc}")
    assert not bad, "non-UTF-8 files (re-save as UTF-8):\n" + "\n".join(bad)


def test_plugins_package_marker_is_written_as_utf8_even_under_a_cp1252_default(tmp_path, monkeypatch):
    """Simulate Windows: any write_text() that doesn't ask for an encoding
    gets cp1252. The generator must therefore ask for one explicitly."""
    from skills import plugin_loader

    original = Path.write_text

    def windows_default_write_text(self, data, encoding=None, errors=None, newline=None):
        return original(self, data, encoding=encoding or "cp1252", errors=errors, newline=newline)

    monkeypatch.setattr(Path, "write_text", windows_default_write_text)
    monkeypatch.setattr(plugin_loader, "_PLUGINS_DIR", tmp_path / "plugins")

    assert plugin_loader._ensure_plugins_package() is True
    raw = (tmp_path / "plugins" / "__init__.py").read_bytes()
    raw.decode("utf-8")   # raises UnicodeDecodeError if the encoding regressed
    assert raw.startswith(b"# Beaver plugin package")
