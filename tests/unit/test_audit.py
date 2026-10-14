"""agent.audit — the append-only audit log, and its wiring into execute_tools.

[AUDIT-1] Four things have to hold for the log to be worth anything:
  1. secrets never reach the disk,
  2. every path a tool call can take leaves exactly the right record,
  3. editing the file afterwards is detectable, and
  4. a broken log can't silently disable the thing it audits (strict mode).
The conftest `isolated_audit_log` fixture points BEAVER_AUDIT_DIR at a temp dir
for every test, so none of this touches <repo>/audit/.
"""
import asyncio
import hashlib
import json
import os
import re
import threading
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

import agent.graph as graph
from agent import audit
from agent.config import RuntimeConfig

ROOT = Path(__file__).resolve().parents[2]


# ── helpers ─────────────────────────────────────────────────────────────────

def audit_files(d):
    return sorted(Path(d).glob("audit-*.jsonl"))


def records(d):
    out = []
    for f in audit_files(d):
        out += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return out


def raw_text(d):
    return "".join(f.read_text(encoding="utf-8") for f in audit_files(d))


def write_n(n, **extra):
    for i in range(n):
        assert audit.record("tool_call", thread="t", persona="p", call_id=f"c{i}", tool="x", **extra)


# ═══ 1. redaction ═══════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "key",
    ["password", "api_key", "API-KEY", "access_token", "secret", "Authorization", "cookie",
     "private_key", "db_passwd", "auth", "pass", "client_secret"],
)
def test_values_under_secret_looking_keys_are_masked(key):
    assert audit.redact_args({key: "hunter2"}) == {key: "[REDACTED]"}


def test_secret_keys_are_masked_at_any_depth_and_inside_lists():
    out = audit.redact_args({"a": {"b": [{"token": "t0k3n", "ok": "visible"}]}})
    assert out == {"a": {"b": [{"token": "[REDACTED]", "ok": "visible"}]}}


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("psql postgres://admin:SuperSecret123@db.internal/app", "SuperSecret123"),
        ("curl -u admin:hunter2 https://x.test", "hunter2"),
        ("curl --user admin:hunter2 https://x.test", "hunter2"),
        ("curl --user=admin:hunter2 https://x.test", "hunter2"),
        ("curl -H 'Authorization: Bearer abcdefghijklmnop123' https://x", "abcdefghijklmnop123"),
        ("curl -H 'Authorization: Token abcdefghijklmnop123' https://x", "abcdefghijklmnop123"),
        ("curl -H 'Cookie: sid=abc123def456; theme=dark' https://x", "abc123def456"),
        ("login --password hunter2 --host x", "hunter2"),
        ("export DB_PASSWORD=hunter2", "hunter2"),
        ("api_key=\"sk-not-real-but-long-enough\"", "sk-not-real-but-long-enough"),
        ("token: ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        ("key AKIAABCDEFGHIJKLMNOP in the env", "AKIAABCDEFGHIJKLMNOP"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123def456ghi", "eyJhbGciOiJIUzI1NiJ9"),
    ],
)
def test_credentials_inside_free_text_are_masked(text, leaked):
    assert leaked not in audit.redact_text(text)
    assert leaked not in json.dumps(audit.redact_args({"command": text}))


@pytest.mark.parametrize(
    "text",
    [
        "nmap -sV -p 80,443 10.0.0.5",      # -p is a PORT list here, not a password
        "nmap -p- 10.0.0.5",
        "curl -u admin https://x.test",     # no password supplied
        "ls -la /home/user/bypass",         # 'pass' inside a word
        "https://api.example.com/v1/users?limit=10",
        "git log --oneline -n 5",
    ],
)
def test_ordinary_commands_are_left_intact(text):
    assert audit.redact_text(text) == text


def test_file_bodies_are_stored_as_size_and_hash_only():
    body = "line1\nBIG SECRET FILE BODY\n"
    out = audit.redact_args({"path": "a.txt", "content": body, "old_str": "x", "new_str": "y"})
    assert out["path"] == "a.txt"
    assert out["content"] == {"chars": len(body), "sha256": hashlib.sha256(body.encode()).hexdigest()}
    assert "BIG SECRET" not in json.dumps(out)
    assert set(out["old_str"]) == {"chars", "sha256"} == set(out["new_str"])


def test_long_strings_are_truncated_with_a_marker():
    out = audit.redact_args({"command": "A" * 5000})["command"]
    assert len(out) < 2100 and out.endswith("…[+3000 chars]")


def test_containers_and_depth_are_bounded():
    wide = audit.redact_args({"xs": list(range(500))})["xs"]
    assert len(wide) == 51 and "more items" in wide[-1]
    deep = {"a": {"b": {"c": {"d": {"e": "too deep"}}}}}
    assert "too deep" not in json.dumps(audit.redact_args(deep))


def test_oversized_args_collapse_to_a_digest():
    out = audit.redact_args({f"k{i}": "v" * 1900 for i in range(40)})
    assert out["_truncated"] is True and set(out) == {"_truncated", "chars", "sha256"}


def test_non_json_values_do_not_crash_redaction():
    out = audit.redact_args({"obj": object(), "b": b"bytes", "s": {1, 2}, "n": None, "f": 1.5})
    json.dumps(out)  # must be serialisable
    assert out["n"] is None and out["f"] == 1.5


def test_unicode_is_preserved_readably():
    assert audit.redact_args({"query": "مرحبا بالعالم"}) == {"query": "مرحبا بالعالم"}


# ═══ 2. writing ═════════════════════════════════════════════════════════════

def test_record_writes_one_valid_json_line_with_the_expected_fields(isolated_audit_log):
    assert audit.record("tool_call", thread="thr-1", persona="coder", call_id="c1", tool="os_exec",
                        args={"command": "whoami"}, scope=["10.0.0.0/8"])
    (rec,) = records(isolated_audit_log)
    assert rec["event"] == "tool_call" and rec["tool"] == "os_exec"
    assert rec["thread"] == "thr-1" and rec["persona"] == "coder"
    assert rec["args"] == {"command": "whoami"} and rec["scope"] == ["10.0.0.0/8"]
    assert rec["seq"] == 1 and rec["prev"] == audit.GENESIS and rec["v"] == audit.SCHEMA_VERSION
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00", rec["ts"])
    assert rec["pid"] == os.getpid() and rec["boot"] == audit._BOOT


def test_files_are_daily_utc_named(isolated_audit_log):
    write_n(1)
    (f,) = audit_files(isolated_audit_log)
    assert re.fullmatch(r"audit-\d{4}-\d\d-\d\d\.jsonl", f.name)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_files_are_created_owner_only(isolated_audit_log):
    write_n(1)
    (f,) = audit_files(isolated_audit_log)
    assert f.stat().st_mode & 0o077 == 0


def test_secrets_never_reach_the_file(isolated_audit_log):
    audit.record("tool_call", thread="t", persona="p", call_id="c", tool="os_exec", args={
        "command": "curl -u admin:hunter2 https://x --password hunter2",
        "api_key": "sk-live-abcdefghijklmnop", "content": "TOP SECRET BODY"})
    text = raw_text(isolated_audit_log)
    for leaked in ("hunter2", "sk-live-abcdefghijklmnop", "TOP SECRET BODY"):
        assert leaked not in text


def test_unicode_is_stored_readably_not_escaped(isolated_audit_log):
    audit.record("tool_call", thread="t", persona="p", call_id="c", tool="web_search", args={"query": "مرحبا"})
    assert "مرحبا" in raw_text(isolated_audit_log)
    assert audit.verify(audit_files(isolated_audit_log)[0]).ok


def test_long_scope_lists_are_capped(isolated_audit_log):
    audit.record("tool_call", thread="t", persona="p", call_id="c", tool="x", scope=[f"10.0.{i}.0/24" for i in range(80)])
    scope = records(isolated_audit_log)[0]["scope"]
    assert len(scope) == 51 and "more" in scope[-1]


def test_output_digest_has_no_preview_by_default():
    d = audit.digest_output("SECRET OUTPUT")
    assert d == {"result_chars": 13, "result_sha256": hashlib.sha256(b"SECRET OUTPUT").hexdigest()}


def test_output_preview_is_opt_in_truncated_and_redacted(monkeypatch):
    monkeypatch.setenv("BEAVER_AUDIT_OUTPUT_CHARS", "40")
    d = audit.digest_output("password=hunter2 and then a very long tail of ordinary output text")
    assert "hunter2" not in d["result_preview"] and len(d["result_preview"]) <= 40 + 20
    monkeypatch.setenv("BEAVER_AUDIT_OUTPUT_CHARS", "not-a-number")
    assert "result_preview" not in audit.digest_output("x")


def test_disabled_writes_nothing_and_reports_success(monkeypatch, isolated_audit_log):
    monkeypatch.setenv("BEAVER_AUDIT", "0")
    assert audit.record("tool_call", call_id="c", tool="x") is True
    assert not isolated_audit_log.exists()


def test_a_broken_log_never_raises_and_reports_failure(monkeypatch, tmp_path):
    blocker = tmp_path / "a_file"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("BEAVER_AUDIT_DIR", str(blocker / "sub"))   # mkdir must fail
    assert audit.record("tool_call", call_id="c", tool="x") is False   # and not raise


def test_concurrent_writers_keep_the_chain_intact(isolated_audit_log):
    def worker(n):
        for i in range(25):
            audit.record("tool_call", thread=f"w{n}", persona="p", call_id=f"{n}-{i}", tool="x")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    (f,) = audit_files(isolated_audit_log)
    rep = audit.verify(f)
    assert rep.records == 200 and rep.ok, rep.problems


# ═══ 3. verification / tamper detection ═════════════════════════════════════

@pytest.fixture
def chain(isolated_audit_log):
    write_n(6)
    (f,) = audit_files(isolated_audit_log)
    assert audit.verify(f).ok
    return f


def lines_of(f):
    return f.read_text(encoding="utf-8").split("\n")   # last element is ""


def save(f, lines):
    f.write_text("\n".join(lines), encoding="utf-8", newline="\n")   # no platform newline translation


def change(lines, idx, old, new):
    """Edit one line IN PLACE and prove the edit happened. (A tamper test whose
    'tamper' silently matched nothing would pass forever, whatever the verifier does.)"""
    assert old in lines[idx], f"test bug: {old!r} not in line {idx}: {lines[idx][:120]}"
    lines[idx] = lines[idx].replace(old, new)
    assert lines[idx].count(new) >= 1


def test_untouched_log_verifies(chain):
    rep = audit.verify(chain)
    assert rep.ok and rep.records == 6


def test_editing_a_record_is_detected_exactly_once(chain):
    ls = lines_of(chain)
    change(ls, 1, '"tool": "x"', '"tool": "rm"')
    save(chain, ls)
    rep = audit.verify(chain)
    assert len(rep.problems) == 1, rep.problems     # one fault, not a cascade
    assert "line 3" in rep.problems[0] and "altered" in rep.problems[0]


def test_deleting_a_middle_record_is_detected(chain):
    ls = lines_of(chain)
    del ls[2]
    save(chain, ls)
    rep = audit.verify(chain)
    assert not rep.ok and "removed, inserted or reordered" in rep.problems[0]


def test_deleting_the_first_record_is_detected(chain):
    ls = lines_of(chain)
    del ls[0]
    save(chain, ls)
    assert not audit.verify(chain).ok


def test_reordering_records_is_detected(chain):
    ls = lines_of(chain)
    ls[1], ls[2] = ls[2], ls[1]
    save(chain, ls)
    assert not audit.verify(chain).ok


def test_duplicating_a_record_is_detected(chain):
    ls = lines_of(chain)
    ls.insert(2, ls[1])
    save(chain, ls)
    assert not audit.verify(chain).ok


def test_garbage_and_blank_lines_are_flagged(chain):
    ls = lines_of(chain)
    ls.insert(3, "this is not json")
    ls.insert(3, "")
    save(chain, ls)
    problems = " ".join(audit.verify(chain).problems)
    assert "not a valid audit record" in problems and "blank line" in problems


def test_missing_trailing_newline_is_flagged_as_possible_truncation(chain):
    chain.write_text(chain.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8", newline="\n")
    assert any("truncated" in p for p in audit.verify(chain).problems)


def test_unreadable_file_is_reported_not_raised(tmp_path):
    rep = audit.verify(tmp_path / "missing.jsonl")
    assert not rep.ok and "cannot read" in rep.problems[0]


def test_two_processes_appending_to_one_file_keep_independent_chains(monkeypatch, isolated_audit_log):
    """CLI + web writing the same daily file interleave lines; each keeps its own chain."""
    chains = {"A": {}, "B": {}}

    def as_process(name, boot, call_id):
        monkeypatch.setattr(audit, "_BOOT", boot)
        monkeypatch.setattr(audit, "_chains", chains[name])
        assert audit.record("tool_call", thread=name, persona="p", call_id=call_id, tool="x")

    for i, who in enumerate("ABABAB"):
        as_process(who, who.lower() * 8, f"c{i}")
    (f,) = audit_files(isolated_audit_log)
    rep = audit.verify(f)
    assert rep.records == 6 and rep.ok, rep.problems


def test_documented_limit_the_newest_record_is_unvouched_but_the_head_hash_exposes_it(chain):
    """Nothing follows the last record to vouch for it, so from the file alone, cutting the
    tail or editing only the final line verifies clean. That's inherent to a hash chain —
    and why verify() reports the chain HEAD: a hash noted down elsewhere catches exactly this."""
    (boot, (seq, noted)), = audit.verify(chain).heads.items()    # the value an operator writes down
    assert seq == 6 and len(noted) == 64

    ls = lines_of(chain)
    change(ls, -2, '"tool": "x"', '"tool": "rm"')                # rewrite the newest record
    save(chain, ls)
    rep = audit.verify(chain)
    assert rep.ok                                                 # file alone can't tell...
    assert rep.heads[boot][1] != noted                            # ...the noted head can

    ls = lines_of(chain)
    save(chain, ls[:-2] + [""])                                   # now cut the tail off
    rep = audit.verify(chain)
    assert rep.ok
    assert rep.heads[boot][0] == 5 and rep.heads[boot][1] != noted


def test_the_head_is_stable_while_nothing_changes(chain):
    assert audit.verify(chain).heads == audit.verify(chain).heads


def test_each_process_in_a_shared_file_gets_its_own_head(monkeypatch, isolated_audit_log):
    chains = {"A": {}, "B": {}}
    for who in "ABAB":
        monkeypatch.setattr(audit, "_BOOT", who.lower() * 8)
        monkeypatch.setattr(audit, "_chains", chains[who])
        audit.record("tool_call", thread=who, persona="p", call_id="c", tool="x")
    (f,) = audit_files(isolated_audit_log)
    heads = audit.verify(f).heads
    assert set(heads) == {"aaaaaaaa", "bbbbbbbb"} and all(seq == 2 for seq, _ in heads.values())


# ── Windows: line endings ───────────────────────────────────────────────────

def test_the_file_is_opened_in_binary_mode_when_the_platform_has_that_flag(monkeypatch, isolated_audit_log):
    """On Windows os.open() makes TEXT-mode descriptors unless O_BINARY is passed, and
    os.write() then turns every \\n into \\r\\n — so the bytes on disk stop matching the
    hashes in the chain and `verify` calls every untouched log tampered. POSIX has no
    O_BINARY; fake it here and check we ask for it."""
    fake_flag = 0x8000
    seen = []
    real_open = os.open

    def spy(path, flags, mode=0o777, **kw):
        seen.append(flags)
        return real_open(path, flags & ~fake_flag, mode, **kw)

    monkeypatch.setattr(os, "O_BINARY", fake_flag, raising=False)
    monkeypatch.setattr(os, "open", spy)
    assert audit.record("tool_call", thread="t", persona="p", call_id="c", tool="x")
    assert seen and all(f & fake_flag for f in seen), seen


def test_a_crlf_converted_log_still_verifies(chain):
    """git autocrlf, an editor, or a copy through Windows can rewrite line endings;
    that is not tampering, and must not read as tampering."""
    before = audit.verify(chain)
    chain.write_bytes(chain.read_bytes().replace(b"\n", b"\r\n"))
    after = audit.verify(chain)
    assert after.ok and after.records == before.records, after.problems
    assert after.heads == before.heads          # same hashes: an operator's noted head still matches


def test_a_content_edit_is_still_caught_under_crlf(chain):
    ls = lines_of(chain)
    change(ls, 1, '"tool": "x"', '"tool": "rm"')
    chain.write_bytes("\r\n".join(ls).encode("utf-8"))
    rep = audit.verify(chain)
    assert not rep.ok and len(rep.problems) == 1, rep.problems


def test_mixed_line_endings_are_tolerated(chain):
    parts = chain.read_bytes().split(b"\n")
    chain.write_bytes(b"\r\n".join(parts[:3]) + b"\n" + b"\n".join(parts[3:]))
    assert audit.verify(chain).ok


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_prints_the_head_hash_to_write_down(chain, capsys, isolated_audit_log):
    assert audit.main(["verify"]) == 0
    out = capsys.readouterr().out
    (boot, (seq, digest)), = audit.verify(chain).heads.items()
    assert f"head  process {boot}  records {seq}  sha256 {digest}" in out


def test_cli_verify_exit_codes(chain, capsys, isolated_audit_log):
    assert audit.main(["verify"]) == 0
    assert "OK" in capsys.readouterr().out
    ls = lines_of(chain)
    del ls[2]
    save(chain, ls)
    assert audit.main(["verify", str(chain)]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_cli_usage_and_empty_dir_exit_2(capsys, isolated_audit_log):
    assert audit.main([]) == 2
    assert audit.main(["frobnicate"]) == 2
    assert audit.main(["verify"]) == 2        # nothing to verify is not a pass
    capsys.readouterr()


# ═══ 4. the real execute_tools() ════════════════════════════════════════════

REACHED = "REACHED"
REACHED_LOG: list = []


@tool
def http_get(url: str) -> str:
    """stand-in http_get"""
    REACHED_LOG.append(("http_get", url))
    return "TOP-SECRET-OUTPUT"


@tool
def os_exec(command: str) -> str:
    """stand-in os_exec"""
    REACHED_LOG.append(("os_exec", command))
    return REACHED


@tool
def flaky(x: str) -> str:
    """stand-in that reports a failure"""
    REACHED_LOG.append(("flaky", x))
    return "Error: it broke"


@tool
def boom(x: str) -> str:
    """stand-in that raises"""
    raise RuntimeError("kaboom")


FAKE_TOOLS = [http_get, os_exec, flaky, boom]


@pytest.fixture(autouse=True)
def reset_reached():
    REACHED_LOG.clear()


def run(monkeypatch, calls, persona="pentester", scope=("127.0.0.1", "localhost", "10.0.0.0/8"), thread="thr-42"):
    async def fake_tools(_persona):
        return FAKE_TOOLS

    monkeypatch.setattr(graph, "get_persona_tools_async", fake_tools)
    state = {
        "messages": [AIMessage(content="", tool_calls=[
            {"name": n, "args": a, "id": f"call-{i}"} for i, (n, a) in enumerate(calls)])],
        "active_persona": persona,
        "target_scope": list(scope),
    }
    out = asyncio.run(graph.execute_tools(state, {"configurable": {"thread_id": thread}}))
    return [m.content for m in out["messages"]]


def events(d):
    return [(r["event"], r.get("tool"), r.get("reason")) for r in records(d)]


def test_a_normal_call_leaves_intent_then_outcome_linked_by_call_id(monkeypatch, isolated_audit_log):
    run(monkeypatch, [("os_exec", {"command": "whoami"})], persona="coder")
    call, result = records(isolated_audit_log)
    assert (call["event"], result["event"]) == ("tool_call", "tool_result")
    assert call["call_id"] == result["call_id"] == "call-0"
    assert call["args"] == {"command": "whoami"} and call["scope"] == ["127.0.0.1", "localhost", "10.0.0.0/8"]
    assert call["thread"] == "thr-42" and call["persona"] == "coder"
    assert result["ok"] is True and result["duration_ms"] >= 0
    assert result["seq"] == call["seq"] + 1


def test_the_intent_is_written_before_the_tool_runs(monkeypatch, isolated_audit_log):
    seen = {}

    @tool
    def spy(x: str) -> str:
        """checks the log while the tool is running"""
        seen["during"] = [r["event"] for r in records(isolated_audit_log)]
        return "done"

    async def fake_tools(_p):
        return [spy]

    monkeypatch.setattr(graph, "get_persona_tools_async", fake_tools)
    state = {"messages": [AIMessage(content="", tool_calls=[{"name": "spy", "args": {"x": "1"}, "id": "c"}])],
             "active_persona": "coder", "target_scope": []}
    asyncio.run(graph.execute_tools(state, {"configurable": {"thread_id": "t"}}))
    assert seen["during"] == ["tool_call"]        # crash-proof: intent precedes execution


def test_a_scope_block_is_recorded_and_the_tool_never_runs(monkeypatch, isolated_audit_log):
    out = run(monkeypatch, [("http_get", {"url": "https://out-of-scope.test/"})])
    assert out[0].startswith("[SCOPE BLOCK]")
    assert REACHED_LOG == []
    assert events(isolated_audit_log) == [("scope_block", "http_get", None)]
    (rec,) = records(isolated_audit_log)
    assert rec["violation"] == "https://out-of-scope.test/"
    assert rec["scope"] == ["127.0.0.1", "localhost", "10.0.0.0/8"]      # what it was refused against


def test_the_os_exec_ip_gate_is_recorded_too(monkeypatch, isolated_audit_log):
    run(monkeypatch, [("os_exec", {"command": "nmap 8.8.8.8"})], persona="coder")
    assert events(isolated_audit_log) == [("scope_block", "os_exec", None)]
    assert REACHED_LOG == []


def test_unregistered_tool_is_recorded_as_skipped(monkeypatch, isolated_audit_log):
    run(monkeypatch, [("no_such_tool", {"a": 1})])
    assert events(isolated_audit_log) == [("tool_skipped", "no_such_tool", "unregistered_tool")]


def test_invalid_arguments_are_recorded_as_skipped(monkeypatch, isolated_audit_log):
    run(monkeypatch, [("os_exec", {})], persona="coder")       # required `command` missing
    assert events(isolated_audit_log) == [("tool_skipped", "os_exec", "invalid_args")]
    assert REACHED_LOG == []


def test_a_repeat_of_a_failed_call_is_recorded_as_skipped(monkeypatch, isolated_audit_log):
    run(monkeypatch, [("flaky", {"x": "1"}), ("flaky", {"x": "1"})], persona="coder")
    assert events(isolated_audit_log) == [
        ("tool_call", "flaky", None), ("tool_result", "flaky", None), ("tool_skipped", "flaky", "repeat_failure"),
    ]
    assert len(REACHED_LOG) == 1
    assert [r for r in records(isolated_audit_log) if r["event"] == "tool_result"][0]["ok"] is False


def test_a_tool_that_raises_is_recorded_as_a_failed_result(monkeypatch, isolated_audit_log):
    run(monkeypatch, [("boom", {"x": "1"})], persona="coder")
    result = records(isolated_audit_log)[-1]
    assert result["event"] == "tool_result" and result["ok"] is False


def test_args_are_redacted_before_they_reach_the_file(monkeypatch, isolated_audit_log):
    run(monkeypatch, [("os_exec", {"command": "curl -u admin:hunter2 http://127.0.0.1/ --password hunter2"})], persona="coder")
    assert "hunter2" not in raw_text(isolated_audit_log)
    assert records(isolated_audit_log)[0]["args"]["command"].startswith("curl -u admin:***")


def test_tool_output_is_not_stored_only_its_size_and_hash(monkeypatch, isolated_audit_log):
    run(monkeypatch, [("http_get", {"url": "http://127.0.0.1/"})])
    assert "TOP-SECRET-OUTPUT" not in raw_text(isolated_audit_log)
    result = records(isolated_audit_log)[-1]
    assert result["result_chars"] == len("TOP-SECRET-OUTPUT")
    assert result["result_sha256"] == hashlib.sha256(b"TOP-SECRET-OUTPUT").hexdigest()


def test_output_preview_when_opted_in(monkeypatch, isolated_audit_log):
    monkeypatch.setenv("BEAVER_AUDIT_OUTPUT_CHARS", "9")
    run(monkeypatch, [("http_get", {"url": "http://127.0.0.1/"})])
    assert records(isolated_audit_log)[-1]["result_preview"] == "TOP-SECRE"


def test_every_record_from_a_real_run_verifies(monkeypatch, isolated_audit_log):
    run(monkeypatch, [("http_get", {"url": "http://127.0.0.1/"}), ("http_get", {"url": "https://evil.test/"}),
                      ("nope", {}), ("flaky", {"x": "1"})])
    (f,) = audit_files(isolated_audit_log)
    assert audit.verify(f).ok


def test_best_effort_by_default_a_broken_log_does_not_stop_the_tool(monkeypatch, tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("BEAVER_AUDIT_DIR", str(blocker / "sub"))
    out = run(monkeypatch, [("os_exec", {"command": "whoami"})], persona="coder")
    assert out == [REACHED] and REACHED_LOG == [("os_exec", "whoami")]


def test_strict_mode_refuses_to_run_a_tool_it_cannot_record(monkeypatch, tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("BEAVER_AUDIT_DIR", str(blocker / "sub"))
    monkeypatch.setenv("BEAVER_AUDIT_STRICT", "1")
    out = run(monkeypatch, [("os_exec", {"command": "whoami"})], persona="coder")
    assert out[0].startswith("[AUDIT FAILURE]")
    assert REACHED_LOG == []


def test_strict_mode_does_not_get_in_the_way_when_logging_works(monkeypatch, isolated_audit_log):
    monkeypatch.setenv("BEAVER_AUDIT_STRICT", "1")
    assert run(monkeypatch, [("os_exec", {"command": "whoami"})], persona="coder") == [REACHED]


def test_disabled_runs_tools_and_writes_nothing(monkeypatch, isolated_audit_log):
    monkeypatch.setenv("BEAVER_AUDIT", "off")
    assert run(monkeypatch, [("os_exec", {"command": "whoami"})], persona="coder") == [REACHED]
    assert not isolated_audit_log.exists()


# ═══ 5. scope changes ═══════════════════════════════════════════════════════

def test_constructing_a_config_is_not_a_scope_change(isolated_audit_log):
    RuntimeConfig()
    assert records(isolated_audit_log) == [] if isolated_audit_log.exists() else True


def test_set_scope_is_recorded_with_old_and_new(isolated_audit_log):
    cfg = RuntimeConfig()
    before = list(cfg.scope)
    cfg.set_scope("10.0.0.0/8, evil.example")
    (rec,) = records(isolated_audit_log)
    assert rec["event"] == "scope_change" and rec["old"] == before and rec["new"] == ["10.0.0.0/8", "evil.example"]


def test_the_settings_drawers_bare_setattr_is_recorded_too(isolated_audit_log):
    """web/server.py applies the Settings drawer with setattr(cfg, 'scope', ...), bypassing
    set_scope() entirely — the path most likely to widen scope must not go unseen."""
    cfg = RuntimeConfig()
    cfg.scope = ["*"]
    (rec,) = records(isolated_audit_log)
    assert rec["event"] == "scope_change" and rec["new"] == ["*"]


def test_setting_the_same_scope_again_is_not_a_change(isolated_audit_log):
    cfg = RuntimeConfig()
    cfg.set_scope(list(cfg.scope))
    assert not isolated_audit_log.exists() or records(isolated_audit_log) == []


def test_scope_still_behaves_like_a_plain_list_attribute():
    cfg = RuntimeConfig()
    cfg.scope = ["a", "b"]
    assert cfg.scope == ["a", "b"]
    assert RuntimeConfig().scope != ["a", "b"]    # per-instance, not shared state


# ═══ 5b. who changed it — attribution through the real WebSocket endpoint ══

def _web_session(monkeypatch, send):
    """Open the real /ws endpoint (graph stubbed so it gets past the readiness check),
    authenticate, send each frame in `send`, and return the frames the server replied with."""
    from starlette.testclient import TestClient
    import web.server as server

    monkeypatch.setattr(server, "_graph", object())          # only needs to be non-None
    monkeypatch.setattr(server, "_WEB_TOKEN", "")
    replies = []
    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": ""}))
        for frame in send:
            ws.send_text(json.dumps(frame))
            replies.append(json.loads(ws.receive_text()))
            # drain until the reply to THIS frame (the server also pushes status frames)
            while replies[-1].get("type") not in ("config_updated", "command_result", "error"):
                replies.append(json.loads(ws.receive_text()))
    return replies


def test_a_settings_drawer_scope_change_is_attributed_to_its_own_connection(monkeypatch, isolated_audit_log):
    _web_session(monkeypatch, [{"type": "update_config", "changes": {"scope": "10.0.0.0/8, evil.example"}}])
    (rec,) = [r for r in records(isolated_audit_log) if r["event"] == "scope_change"]
    assert rec["new"] == ["10.0.0.0/8", "evil.example"]
    assert re.fullmatch(r"web-[0-9a-f]{12}", rec["thread"]), rec["thread"]   # NOT the fallback "default"
    assert rec["persona"]                                                      # the active persona is recorded


def test_two_connections_are_told_apart_in_the_log(monkeypatch, isolated_audit_log):
    for scope in ("10.0.0.0/8", "172.16.0.0/12"):
        _web_session(monkeypatch, [{"type": "update_config", "changes": {"scope": scope}}])
    threads = [r["thread"] for r in records(isolated_audit_log) if r["event"] == "scope_change"]
    assert len(threads) == 2 and threads[0] != threads[1]


def test_after_a_reset_the_new_thread_is_what_gets_attributed(monkeypatch, isolated_audit_log):
    replies = _web_session(monkeypatch, [
        {"type": "user_message", "content": "/reset"},
        {"type": "update_config", "changes": {"scope": "192.168.5.0/24"}},
    ])
    new_thread = next(r["switch_to_thread"] for r in replies if r.get("switch_to_thread"))
    (rec,) = [r for r in records(isolated_audit_log) if r["event"] == "scope_change"]
    assert rec["thread"] == new_thread


def test_the_slash_scope_command_is_attributed_too(monkeypatch, isolated_audit_log):
    _web_session(monkeypatch, [{"type": "user_message", "content": "/scope 10.9.0.0/16"}])
    (rec,) = [r for r in records(isolated_audit_log) if r["event"] == "scope_change"]
    assert rec["new"] == ["10.9.0.0/16"] and rec["thread"].startswith("web-")


# ═══ 6. documentation / repo hygiene ════════════════════════════════════════

@pytest.mark.parametrize("var", ["BEAVER_AUDIT", "BEAVER_AUDIT_DIR", "BEAVER_AUDIT_STRICT", "BEAVER_AUDIT_OUTPUT_CHARS"])
def test_env_example_documents_every_audit_setting(var):
    assert re.search(rf"^#?{var}=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M), var


def test_audit_files_are_gitignored():
    assert any(line.strip() in ("audit/", "/audit/", "audit") for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())


def test_status_line_says_plainly_whether_it_is_on(monkeypatch):
    assert "ENABLED" in audit.status_line()
    monkeypatch.setenv("BEAVER_AUDIT_STRICT", "1")
    assert "STRICT" in audit.status_line()
    monkeypatch.setenv("BEAVER_AUDIT", "0")
    assert "DISABLED" in audit.status_line() and "NOT being recorded" in audit.status_line()
