"""Shared pytest setup for tests/unit/.

Puts the repo root on sys.path (the project has no installable package),
and provides fixtures that reset the two ContextVars several modules key
off — otherwise one test's workspace/thread id would leak into the next.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def thread_id():
    """Set agent.graph.active_thread_id_var for one test, then restore it.

    Usage: `tid = thread_id("thread-A")` — callable, may be called again
    mid-test to simulate a different concurrent conversation.
    """
    from agent.graph import active_thread_id_var

    tokens = []

    def _set(value: str) -> str:
        tokens.append(active_thread_id_var.set(value))
        return value

    yield _set
    for tok in reversed(tokens):
        active_thread_id_var.reset(tok)


@pytest.fixture
def workspace():
    """Set the per-session workspace contextvar for one test, then restore it."""
    from skills import file_ops

    tokens = []

    def _set(path: str) -> str:
        resolved = file_ops.set_session_workspace(path)
        return resolved

    # Snapshot so we can restore regardless of how many times _set is called.
    original = file_ops._workspace_var.get()
    yield _set
    file_ops._workspace_var.set(original)
