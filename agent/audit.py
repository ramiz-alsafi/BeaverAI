"""Append-only, tamper-evident audit log of what the agent actually did.

Why this exists
---------------
Beaver can run shell commands and, in the pentester persona, probe hosts. If
you ever need to answer "what did it run, against what, under which
authorised scope, and when?", the chat transcript isn't evidence — it's
editable, per-thread, and doesn't record scope. This module writes one JSON
object per line to a file that only ever grows.

What is recorded (one line each)
--------------------------------
    tool_call     a tool is about to run. Written BEFORE it runs, so a crash
                  mid-command still leaves proof it was attempted.
    tool_result   how it ended: ok/failed, duration, size + SHA-256 of output.
    scope_block   the scope gate refused it (it never ran).
    tool_skipped  it never ran for another reason (repeat failure, bad
                  arguments, tool not registered for the persona, or the
                  approval gate refused it: reason "approval_user_denied",
                  "approval_timeout", "approval_no_approver", ...).
    scope_change  the authorised scope was edited (old -> new).

Every record carries the scope in effect, the thread id and the persona, so
each line stands on its own.

Privacy
-------
Arguments are redacted before they touch the disk (see redact_args): values
under secret-looking keys, URI passwords, bearer tokens, `password=...`,
cookies and well-known token shapes are masked; file-write bodies are stored
as length + SHA-256 only; long strings are truncated. Tool OUTPUT is not
stored by default — only its length and SHA-256 — because output is where
file contents and credentials end up. Set BEAVER_AUDIT_OUTPUT_CHARS to keep
a (redacted) preview. Redaction is best-effort pattern matching, not a
guarantee: don't paste secrets you can't afford to leak into prompts.

Tamper evidence (what it does and doesn't prove)
------------------------------------------------
Each record includes the SHA-256 of the previous record's exact bytes, and a
per-process sequence number, forming a hash chain per (file, process).
`verify()` detects an edited, deleted, inserted or reordered record — with one
structural exception: nothing follows the NEWEST record to vouch for it, so
editing only that record, or cutting records off the end, verifies clean from
the file alone. (The out-of-scope attempt you'd most want to hide is often
exactly the last line.) The fix is to anchor the head: `verify` prints the
chain's head hash, so note it somewhere off this machine — an engagement
report, a ticket — and compare later. It also can't stop someone with write
access from rewriting the whole file and its chain. Treat this as a tripwire
for accidents and casual tampering, not as a notary; for stronger guarantees,
ship the files somewhere append-only.

Configuration (all read at call time)
-------------------------------------
    BEAVER_AUDIT=0                  turn it off (default: on)
    BEAVER_AUDIT_DIR=...            where files go (default: <repo>/audit)
    BEAVER_AUDIT_STRICT=1           refuse to run a tool if its record can't
                                    be written (default: warn and carry on)
    BEAVER_AUDIT_OUTPUT_CHARS=N     keep N chars of redacted output (default 0)

Files are daily (UTC): audit-YYYY-MM-DD.jsonl, created mode 0600. Nothing is
ever rotated or deleted automatically.

CLI
---
    python -m agent.audit verify [file ...]   # default: every file in the dir
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("beaver.audit")

SCHEMA_VERSION = 1
GENESIS = "0" * 64

# Process-unique id: two Beaver processes appending to the same daily file
# (CLI + web, say) each keep their own independent chain, told apart by this.
_BOOT = secrets.token_hex(4)

_lock = threading.Lock()
_chains: Dict[str, Tuple[int, str]] = {}   # file path -> (last seq, last line hash)
_failures = 0

_MAX_STR = 2000            # longest string value kept in an argument
_MAX_ITEMS = 50            # most keys / list items kept per container
_MAX_DEPTH = 4
_MAX_ARGS_BYTES = 32_000   # beyond this the whole args blob is replaced by a digest
_MAX_SCOPE_ENTRIES = 50


# ── configuration ───────────────────────────────────────────────────────────

def enabled() -> bool:
    return os.getenv("BEAVER_AUDIT", "1").strip().lower() not in ("0", "false", "no", "off")


def strict() -> bool:
    return os.getenv("BEAVER_AUDIT_STRICT", "").strip().lower() in ("1", "true", "yes", "on")


def output_chars() -> int:
    try:
        return max(0, int(os.getenv("BEAVER_AUDIT_OUTPUT_CHARS", "0").strip() or "0"))
    except ValueError:
        return 0


def audit_dir() -> Path:
    configured = os.getenv("BEAVER_AUDIT_DIR", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parent.parent / "audit"


def _today_file() -> Path:
    return audit_dir() / f"audit-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"


def status_line() -> str:
    if not enabled():
        return (
            "[AUDIT-1] Audit log: DISABLED (BEAVER_AUDIT=0) — tool calls and "
            "scope blocks are NOT being recorded."
        )
    mode = "STRICT (tools refuse to run if logging fails)" if strict() else "best-effort"
    return f"[AUDIT-1] Audit log: ENABLED, {mode} -> {audit_dir()}"


# ── redaction ───────────────────────────────────────────────────────────────

_SENSITIVE_KEY_RE = re.compile(
    r"password|passwd|passphrase|secret|token|api[_-]?key|authorization|cookie|"
    r"credential|private[_-]?key|(?:^|[_-])(?:auth|pass|pwd)(?:$|[_-])",
    re.I,
)
# Arguments that hold bulk content being written somewhere: keep size + hash
# only. (A file body is rarely what an audit needs, and often what leaks.)
_BULK_KEYS = {"content", "new_str", "old_str"}

_URI_CRED_RE = re.compile(r"(://[^/\s:@]+:)[^/\s@]+@")
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey)[A-Za-z0-9_.-]*)"
    r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s&\"']+)"
)
_SECRET_FLAG_RE = re.compile(r"(?i)(--?(?:password|passwd|pass|token|secret|api-?key))(\s+)(\S+)")
# curl/wget-style basic auth: `-u user:pass`, `--user user:pass`, `--user=user:pass`.
# A bare `-p VALUE` is deliberately NOT masked: nmap uses -p for port lists
# (`nmap -p 80,443`), and masking that would destroy the very evidence an
# audit log is for. Known limit: `hydra -p secret` / `sshpass -p secret` leak.
_BASIC_AUTH_RE = re.compile(r"(?i)((?:^|\s)(?:-u|--user|--proxy-user)(?:\s+|=)[^\s:]+:)(\S+)")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:\w+\s+)?[^\s\"']+")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_COOKIE_RE = re.compile(r"(?i)((?:set-)?cookie\s*:\s*)[^\n\"']+")
_TOKEN_SHAPES_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)\b"
)


def redact_text(text: str) -> str:
    """Mask credentials inside a free-text string. Best-effort, pattern based."""
    text = _URI_CRED_RE.sub(r"\1***@", text)
    text = _AUTH_HEADER_RE.sub(r"\1***", text)
    text = _BEARER_RE.sub(r"\1 ***", text)
    text = _COOKIE_RE.sub(r"\1***", text)
    text = _SECRET_ASSIGN_RE.sub(r"\1\2***", text)
    text = _SECRET_FLAG_RE.sub(r"\1\2***", text)
    text = _BASIC_AUTH_RE.sub(r"\1***", text)
    return _TOKEN_SHAPES_RE.sub("[REDACTED-TOKEN]", text)


def _digest(text: str) -> Dict[str, Any]:
    return {"chars": len(text), "sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()}


def _truncate(text: str) -> str:
    if len(text) <= _MAX_STR:
        return text
    return text[:_MAX_STR] + f"…[+{len(text) - _MAX_STR} chars]"


def _redact_value(key: str, value: Any, depth: int) -> Any:
    if key and _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        if key in _BULK_KEYS:
            return _digest(value)
        return _truncate(redact_text(value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= _MAX_DEPTH:
        return "[…]"
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_ITEMS:
                out["…"] = f"+{len(value) - _MAX_ITEMS} more keys"
                break
            out[str(k)] = _redact_value(str(k), v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out_list = [_redact_value("", v, depth + 1) for v in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            out_list.append(f"…+{len(items) - _MAX_ITEMS} more items")
        return out_list
    return _truncate(redact_text(repr(value)))


def redact_args(args: Any) -> Any:
    """Return a JSON-safe, secret-masked, size-bounded copy of tool arguments."""
    cleaned = _redact_value("", args, 0)
    blob = json.dumps(cleaned, ensure_ascii=False, sort_keys=True, default=str)
    if len(blob.encode("utf-8")) > _MAX_ARGS_BYTES:
        return {"_truncated": True, **_digest(blob)}
    return cleaned


def digest_output(text: str) -> Dict[str, Any]:
    """Fields describing a tool's output without storing it (unless opted in)."""
    d = _digest(text)
    out: Dict[str, Any] = {"result_chars": d["chars"], "result_sha256": d["sha256"]}
    keep = output_chars()
    if keep:
        out["result_preview"] = _truncate(redact_text(text[:keep]))
    return out


# ── writing ─────────────────────────────────────────────────────────────────

def _current_thread() -> str:
    try:  # lazy: agent.graph imports this module, not the other way round
        from agent.graph import active_thread_id_var
        return active_thread_id_var.get()
    except Exception:
        return "unknown"


def _write_all(path: Path, data: bytes) -> None:
    # One os.write per record on an O_APPEND descriptor: concurrent appenders
    # (another Beaver process) can't interleave inside a line.
    #
    # O_BINARY matters on Windows: os.open() there creates TEXT-mode descriptors
    # unless it's passed, and os.write() on one silently turns every "\n" into
    # "\r\n". Each record's hash is computed over the LF-terminated bytes, so
    # without this the file on disk no longer matches its own chain and
    # `verify` reports every untouched log as tampered. (Absent on POSIX: 0.)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def _note_failure(exc: BaseException) -> None:
    global _failures
    _failures += 1
    if _failures == 1 or _failures % 100 == 0:
        logger.error(
            "[AUDIT-1] Could not write the audit log (%s: %s) — %d failure(s) so far. %s",
            type(exc).__name__, exc, _failures,
            "Tools will be refused (BEAVER_AUDIT_STRICT=1)." if strict()
            else "Carrying on WITHOUT a record; set BEAVER_AUDIT_STRICT=1 to refuse instead.",
        )


def record(event: str, *, thread: Optional[str] = None, persona: Optional[str] = None, **fields: Any) -> bool:
    """Append one record. Returns True if it was written (or auditing is off).

    Never raises: a broken audit log must not take the agent down. Callers that
    care (strict mode) look at the return value.
    """
    if not enabled():
        return True
    try:
        if "args" in fields:
            fields["args"] = redact_args(fields["args"])
        for key in ("scope", "old", "new"):
            if isinstance(fields.get(key), (list, tuple)):
                items = [str(x) for x in fields[key]]
                fields[key] = items[:_MAX_SCOPE_ENTRIES] + (
                    [f"…+{len(items) - _MAX_SCOPE_ENTRIES} more"] if len(items) > _MAX_SCOPE_ENTRIES else []
                )

        path = _today_file()
        with _lock:
            seq, prev = _chains.get(str(path), (0, GENESIS))
            body = {
                "v": SCHEMA_VERSION,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "boot": _BOOT,
                "pid": os.getpid(),
                "seq": seq + 1,
                "prev": prev,
                "event": event,
                "thread": thread if thread is not None else _current_thread(),
                "persona": persona,
                **fields,
            }
            line = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_all(path, line + b"\n")
            _chains[str(path)] = (seq + 1, hashlib.sha256(line).hexdigest())
        return True
    except Exception as exc:  # noqa: BLE001 — see docstring
        _note_failure(exc)
        return False


# ── verifying ───────────────────────────────────────────────────────────────

@dataclass
class VerifyReport:
    path: str
    records: int = 0
    problems: List[str] = field(default_factory=list)
    # boot id -> (last seq, SHA-256 of its last record): the value to write down.
    heads: Dict[str, Tuple[int, str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


def verify(path: "str | Path") -> VerifyReport:
    """Check one audit file's hash chains. See the module docstring for limits."""
    report = VerifyReport(str(path))
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        report.problems.append(f"cannot read file: {exc}")
        return report

    chains: Dict[str, Tuple[int, str]] = {}   # boot -> (last seq, last hash)
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    elif lines:
        report.problems.append("file does not end with a newline — last record may be truncated")

    for n, line in enumerate(lines, 1):
        # Tolerate CRLF: git's autocrlf, an editor, or a copy through Windows
        # can convert line endings, and that isn't tampering. Hashes are over
        # the record's bytes WITHOUT its terminator, so dropping a trailing
        # "\r" makes a converted file verify exactly like the original — while
        # any change to the record's content still breaks the chain.
        # (A compact-JSON record can't itself end in a raw CR: it's escaped.)
        if line.endswith(b"\r"):
            line = line[:-1]
        if not line.strip():
            report.problems.append(f"line {n}: blank line")
            continue
        try:
            rec = json.loads(line)
            boot, seq, prev = rec["boot"], int(rec["seq"]), rec["prev"]
        except (ValueError, KeyError, TypeError):
            report.problems.append(f"line {n}: not a valid audit record")
            continue

        report.records += 1
        last_seq, last_hash = chains.get(boot, (0, GENESIS))
        if seq != last_seq + 1:
            report.problems.append(
                f"line {n}: process {boot} sequence jumps {last_seq} -> {seq} "
                "(a record was removed, inserted or reordered)"
            )
        elif prev != last_hash:
            report.problems.append(
                f"line {n}: process {boot} record {seq} does not chain from the previous one "
                "(an earlier record was altered)"
            )
        # Resync on whatever we actually saw so one fault is reported once,
        # not as a cascade through the rest of the file.
        chains[boot] = (seq, hashlib.sha256(line).hexdigest())
    report.heads = dict(chains)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "verify":
        print("usage: python -m agent.audit verify [file ...]", file=sys.stderr)
        return 2
    targets = [Path(p) for p in args[1:]] or sorted(audit_dir().glob("audit-*.jsonl"))
    if not targets:
        print(f"no audit files found in {audit_dir()}")
        return 2
    worst = 0
    for t in targets:
        rep = verify(t)
        if rep.ok:
            print(f"OK    {t}  ({rep.records} records)")
        else:
            worst = 1
            print(f"FAIL  {t}  ({rep.records} records)")
            for p in rep.problems:
                print(f"        - {p}")
        # The chain head vouches for everything before it AND for the newest
        # record, which nothing in the file can. Note it somewhere off this
        # machine (an engagement report, a ticket); a later rewrite of the tail
        # shows up as a different value.
        for boot, (seq, digest) in sorted(rep.heads.items()):
            print(f"        head  process {boot}  records {seq}  sha256 {digest}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
