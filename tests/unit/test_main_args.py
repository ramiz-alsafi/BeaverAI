"""main._parse_args — CLI argument parsing.

[FIX-ARGPARSE] --thread's value used to be stripped with a value-based
list.remove(), which removed the WRONG token whenever the thread id equalled
an earlier positional (prompt text or persona name).
"""
import sys

import pytest

import main


def parse(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    return main._parse_args()


def test_prompt_only(monkeypatch):
    r = parse(monkeypatch, "hello world")
    assert r["positional"] == ["hello world"]
    assert r["thread_id"] == "main"


def test_prompt_persona_and_thread(monkeypatch):
    r = parse(monkeypatch, "hello world", "coder", "--thread", "mysession")
    assert r["positional"] == ["hello world", "coder"]
    assert r["thread_id"] == "mysession"


@pytest.mark.parametrize(
    "argv, positional",
    [
        (("urgent", "coder", "--thread", "urgent"), ["urgent", "coder"]),   # == prompt
        (("hello", "coder", "--thread", "coder"), ["hello", "coder"]),      # == persona
        (("--thread", "x", "x"), ["x"]),                                    # flag first
    ],
)
def test_thread_value_colliding_with_a_positional(monkeypatch, argv, positional):
    r = parse(monkeypatch, *argv)
    assert r["positional"] == positional


def test_flags_are_separated_from_positionals(monkeypatch):
    r = parse(monkeypatch, "--models")
    assert r["flags"] == {"--models"}
    assert r["positional"] == []


def test_thread_flag_without_a_value_does_not_crash(monkeypatch):
    r = parse(monkeypatch, "hello", "--thread")
    assert r["positional"] == ["hello"]
    assert r["thread_id"] == "main"
