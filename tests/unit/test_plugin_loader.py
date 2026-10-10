"""skills.plugin_loader hot-reload detection.

[FIX-PLUGIN-RELOAD] The loader fingerprinted only the plugins/ DIRECTORY's
mtime, which filesystems bump on add/remove but not when an existing file's
content is edited in place — so editing a plugin never triggered a reload.
"""
import os

import skills.plugin_loader as pl


def bump(path, seconds):
    st = path.stat()
    os.utime(path, (st.st_atime + seconds, st.st_mtime + seconds))


def test_editing_an_existing_plugin_changes_the_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "_PLUGINS_DIR", tmp_path)
    plugin = tmp_path / "a.py"
    plugin.write_text("X = 1\n")
    before = pl._plugins_fingerprint()

    plugin.write_text("X = 2\n")          # in-place edit: no add, no remove
    bump(plugin, 10)                      # deterministic, no sleeping on mtime resolution

    assert pl._plugins_fingerprint() != before


def test_adding_a_plugin_changes_the_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "_PLUGINS_DIR", tmp_path)
    (tmp_path / "a.py").write_text("X = 1\n")
    before = pl._plugins_fingerprint()

    new = tmp_path / "b.py"
    new.write_text("Y = 1\n")
    bump(new, 10)

    assert pl._plugins_fingerprint() != before


def test_unchanged_directory_has_a_stable_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "_PLUGINS_DIR", tmp_path)
    (tmp_path / "a.py").write_text("X = 1\n")
    assert pl._plugins_fingerprint() == pl._plugins_fingerprint()


def test_missing_plugins_dir_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "_PLUGINS_DIR", tmp_path / "does-not-exist")
    assert pl._plugins_fingerprint() == 0.0
