"""agent.config prompt loading is cached.

[FIX-PROMPT-CACHE] call_model() loads the persona + three shared prompts on
EVERY reasoning-loop iteration; they were re-read from disk each time.
"""
from pathlib import Path

import pytest

from agent import config as cfgmod


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(cfgmod, "_PROMPT_CACHE", {})   # isolate from other tests
    return cfgmod.RuntimeConfig()


def test_each_prompt_file_is_read_from_disk_only_once(cfg, monkeypatch):
    reads = []
    original = Path.read_text

    def counting(self, *a, **kw):
        reads.append(self.name)
        return original(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", counting)
    for _ in range(5):
        cfg.load_persona_prompt("standard")
        cfg.load_shared_prompt("memory_policy")
        cfg.load_system_prompt()

    assert sorted(reads) == ["memory_policy.md", "standard.md", "system_agent.md"]


def test_cached_content_is_identical(cfg):
    assert cfg.load_persona_prompt("coder") == cfg.load_persona_prompt("coder")


def test_missing_shared_module_returns_empty_string(cfg):
    assert cfg.load_shared_prompt("no_such_module_xyz") == ""


def test_missing_persona_is_not_cached_so_a_later_file_is_picked_up(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(cfgmod, "PERSONAS_PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "PROMPTS_DIR", tmp_path)   # legacy fallback dir

    with pytest.raises(FileNotFoundError):
        cfg.load_persona_prompt("late_persona")
    with pytest.raises(FileNotFoundError):          # still raising, not a cached value
        cfg.load_persona_prompt("late_persona")

    (tmp_path / "late_persona.md").write_text("hello")
    assert cfg.load_persona_prompt("late_persona") == "hello"
