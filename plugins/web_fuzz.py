"""
Beaver Plugin — Web Fuzz & Session
====================================

Closes two real gaps in the pentester toolset:

  1. http_get/http_post/http_head (http_probe.py) each open a brand-new
     httpx.Client and throw it away — there is no cookie jar, no session
     persistence between calls. Testing anything behind a login (IDOR
     across two accounts, authenticated SSRF, role-based access control)
     required manually pulling Set-Cookie/bearer tokens out of one
     response and re-threading them into the `headers` JSON param of
     every subsequent call. http_session_get/post fix this by keeping a
     real, reused httpx.Client (with its own cookie jar) alive per
     session_id across calls.

  2. There was no way to test more than one payload without spending one
     full tool-call round-trip per payload — a real SQLi/XSS/traversal/
     command-injection check against even a short candidate list meant
     5-8 separate http_get calls, each needing the model to manually
     compare against a baseline. probe_payloads runs a short, curated,
     non-destructive payload set for one category in a single call and
     returns only what looks anomalous, compared against a baseline
     request.

Useful for:
  pentester  — authenticated multi-step testing, injection probing

Tools
-----
  http_session_get    — GET through a persistent, cookie-aware session
  http_session_post   — POST through a persistent, cookie-aware session
  http_session_reset  — drop one session's cookie jar (or all of them)
  probe_payloads      — run a built-in payload set against one query
                         parameter and flag anomalous responses
"""

import os
import re
import time
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

import httpx
from langchain_core.tools import tool

PERSONA  = ["pentester"]
ENABLED  = True

_UA           = "Beaver-Agent/3.0 (web-fuzz)"
_MAX_BODY     = 4_000
_MAX_SESSIONS = 10  # simple insertion-order eviction beyond this, PER THREAD

_SESSIONS: "dict[str, httpx.Client]" = {}


def _current_thread_id() -> str:
    """Resolve the active conversation thread id.

    Mirrors structured_notes.py's _current_thread_id() exactly: ContextVar
    first (set by execute_tools on every real graph run), env var as a
    last-resort fallback for callers outside the graph.
    """
    try:
        from agent.graph import active_thread_id_var
        return active_thread_id_var.get()
    except Exception:
        return os.getenv("BEAVER_THREAD_ID", "default")


def _get_session(session_id: str) -> httpx.Client:
    """Return the persistent client for session_id, creating it if needed.

    [FIX-SESSION-THREAD] _SESSIONS used to be keyed by the bare session_id
    string alone — e.g. "user_a", exactly the label this module's own
    docstrings suggest as an example. On a single-user CLI process that's
    fine, but on the web server, every concurrent WebSocket connection
    shares this same module-level dict: two completely unrelated pentest
    engagements running at the same time, both following the docstring's
    own suggestion to use "user_a"/"user_b" labels, would land on the
    IDENTICAL dict key and share one authenticated httpx.Client — cookies
    (real login/session tokens for whatever target application is under
    test) from one user's engagement leaking straight into a different,
    unrelated user's session. Same root cause structured_notes.py already
    fixed for its own per-thread notes (its FIX-NOTES-THREAD) — namespaced
    the same way here: every key is "<thread_id>::<session_id>", so two
    engagements only collide if they're actually the same conversation.

    Eviction (_MAX_SESSIONS) is now naturally per-thread too, since a
    flood of session_ids from one busy engagement can no longer evict a
    completely different thread's sessions out from under it.
    """
    prefix = f"{_current_thread_id()}::"
    key = prefix + session_id
    if key not in _SESSIONS:
        # [FIX-SESSION-EVICT] The namespacing fix above stops cookies from
        # leaking across threads, but eviction was still counting and
        # picking from the GLOBAL _SESSIONS dict (len(_SESSIONS), next(iter(
        # _SESSIONS))) — so a busy engagement in one thread could still
        # silently close a completely unrelated thread's active session
        # purely because the TOTAL across every concurrent engagement
        # crossed _MAX_SESSIONS, even if that other thread only had one or
        # two sessions open itself. That's the same practical harm as the
        # original cookie-leak bug this module already fixed, just via a
        # different mechanism: a different user's authenticated session
        # gets silently force-logged-out instead of leaked. Both the count
        # check and the eviction target are now scoped to THIS thread's own
        # sessions only, via the same prefix used for the key itself.
        own_keys = [k for k in _SESSIONS if k.startswith(prefix)]
        if len(own_keys) >= _MAX_SESSIONS:
            _SESSIONS.pop(own_keys[0]).close()
        _SESSIONS[key] = httpx.Client(
            timeout=15.0, follow_redirects=True, headers={"User-Agent": _UA},
        )
    return _SESSIONS[key]


def _format_headers(headers: httpx.Headers) -> str:
    return "\n".join(f"  {k}: {v}" for k, v in headers.items())


@tool
def http_session_get(session_id: str, url: str, headers: str = "", timeout: float = 15.0) -> str:
    """GET a URL through a persistent, cookie-aware session.

    Unlike http_get (which opens and discards a fresh client every call),
    this reuses one httpx.Client per session_id — cookies the server sets
    (e.g. on login) are automatically carried into every later call with
    the same session_id. Use this for any multi-step flow: log in with
    http_session_post, then hit protected pages/endpoints with
    http_session_get using the SAME session_id — no manual cookie copying.

    To run the same test as a second, differently-privileged user, use a
    different session_id (e.g. "user_a" and "user_b") — each gets its own
    isolated cookie jar, which is exactly what IDOR/access-control testing
    needs: two authenticated identities you can compare responses across.

    Parameters
    ----------
    session_id : your own label for this identity/session, e.g. "user_a" —
                 reused automatically on every call with the same value
    url        : full URL (must start with http:// or https://)
    headers    : optional JSON string of extra headers for this call only,
                 e.g. '{"X-Custom":"1"}' — cookies are handled automatically,
                 you don't need to set them here
    timeout    : request timeout in seconds (default 15)
    """
    if not url.startswith(("http://", "https://")):
        return f"ERROR: Invalid URL '{url}'"
    extra_headers = {}
    if headers.strip():
        import json
        try:
            extra_headers = json.loads(headers)
        except json.JSONDecodeError:
            return f"ERROR: headers must be valid JSON — got: {headers}"

    client = _get_session(session_id)
    try:
        t0 = time.monotonic()
        resp = client.get(url, headers=extra_headers, timeout=min(max(1.0, timeout), 120.0))
        elapsed_ms = int((time.monotonic() - t0) * 1000)
    except httpx.TimeoutException:
        return f"[http_session_get] Timeout after {timeout}s — {url}"
    except Exception as exc:
        return f"[http_session_get] Error: {exc}"

    body = resp.text
    truncated = ""
    if len(body) > _MAX_BODY:
        truncated = f"\n[... body truncated — {len(body) - _MAX_BODY} more chars ...]"
        body = body[:_MAX_BODY]

    cookie_note = f"  (session '{session_id}' cookie jar: {len(client.cookies)} cookie(s) stored)"
    return (
        f"GET {url}  [session: {session_id}]\n"
        f"Status : {resp.status_code} {resp.reason_phrase}\n"
        f"Time   : {elapsed_ms} ms\n"
        f"{cookie_note}\n"
        f"\nHeaders:\n{_format_headers(resp.headers)}\n"
        f"\nBody:\n{body}{truncated}"
    )


@tool
def http_session_post(session_id: str, url: str, body: str = "",
                       content_type: str = "application/json",
                       headers: str = "", timeout: float = 15.0) -> str:
    """POST to a URL through a persistent, cookie-aware session.

    Same session mechanics as http_session_get — use this for the login
    request itself (the response's Set-Cookie is captured automatically
    into this session_id's jar), then follow up with http_session_get
    using the same session_id to hit authenticated pages/endpoints.

    Parameters
    ----------
    session_id   : your own label for this identity/session, e.g. "user_a"
    url          : full URL
    body         : request body as a string (JSON, form-encoded, plain text)
    content_type : Content-Type header (default application/json)
    headers      : optional JSON string of extra headers for this call only
    timeout      : request timeout in seconds (default 15)
    """
    if not url.startswith(("http://", "https://")):
        return f"ERROR: Invalid URL '{url}'"
    extra_headers = {"Content-Type": content_type}
    if headers.strip():
        import json
        try:
            extra_headers.update(json.loads(headers))
        except json.JSONDecodeError:
            return "ERROR: headers must be valid JSON"

    client = _get_session(session_id)
    try:
        t0 = time.monotonic()
        resp = client.post(url, content=body.encode(), headers=extra_headers,
                            timeout=min(max(1.0, timeout), 120.0))
        elapsed_ms = int((time.monotonic() - t0) * 1000)
    except httpx.TimeoutException:
        return f"[http_session_post] Timeout after {timeout}s — {url}"
    except Exception as exc:
        return f"[http_session_post] Error: {exc}"

    resp_body = resp.text[:_MAX_BODY]
    cookie_note = f"  (session '{session_id}' cookie jar: {len(client.cookies)} cookie(s) stored)"
    return (
        f"POST {url}  [session: {session_id}]\n"
        f"Status : {resp.status_code} {resp.reason_phrase}\n"
        f"Time   : {elapsed_ms} ms\n"
        f"{cookie_note}\n"
        f"\nHeaders:\n{_format_headers(resp.headers)}\n"
        f"\nBody:\n{resp_body}"
    )


@tool
def http_session_reset(session_id: str = "") -> str:
    """Drop a session's cookie jar so the next call starts fresh (logged out).

    Parameters
    ----------
    session_id : the session to clear. Leave empty to clear ALL sessions
                 in the current engagement/conversation at once (e.g. at
                 the end of an engagement) — this never touches another
                 conversation's sessions, even on a shared server process.
    """
    # [FIX-SESSION-THREAD] See _get_session's docstring — sessions are
    # namespaced "<thread_id>::<session_id>". "Clear ALL" must only ever
    # mean "all of THIS thread's sessions": without the prefix filter
    # below, one engagement's /reset-equivalent would silently close
    # every other concurrent user's active, authenticated sessions on the
    # web server too.
    prefix = f"{_current_thread_id()}::"
    if not session_id:
        keys = [k for k in _SESSIONS if k.startswith(prefix)]
        for k in keys:
            _SESSIONS.pop(k).close()
        return f"[http_session_reset] Cleared {len(keys)} session(s)."
    key = prefix + session_id
    client = _SESSIONS.pop(key, None)
    if client is None:
        return f"[http_session_reset] No active session '{session_id}' — nothing to clear."
    client.close()
    return f"[http_session_reset] Session '{session_id}' cleared."


# ── Payload probing ────────────────────────────────────────────────────────────

_PAYLOADS = {
    "sqli": [
        "'",
        "' OR '1'='1",
        "' OR '1'='1' -- ",
        "\" OR \"1\"=\"1",
        "1' AND '1'='2",
        "1 OR 1=1",
        "'; WAITFOR DELAY '0:0:3'--",
    ],
    "xss": [
        "<script>alert(1)</script>",
        "\"><script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "'><svg onload=alert(1)>",
        "javascript:alert(1)",
    ],
    "traversal": [
        "../../../../etc/passwd",
        "..\\..\\..\\..\\windows\\win.ini",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "....//....//....//etc/passwd",
    ],
    "cmdi": [
        ";id",
        "|id",
        "`id`",
        "$(id)",
        "; whoami",
    ],
}

_SQLI_SIGNATURES = [
    "sql syntax", "mysql_fetch", "unclosed quotation", "quoted string not properly terminated",
    "sqlite3.operationalerror", "pg::syntaxerror", "ora-00933", "odbc sql server driver",
    "warning: mysql", "syntax error at or near", "sqlstate",
]
_TRAVERSAL_SIGNATURES = ["root:x:0:0", "[extensions]", "[fonts]", "for 16-bit app support"]
_CMDI_SIGNATURES = [re.compile(r"uid=\d+.*gid=\d+"), re.compile(r"root:.*:0:0:")]


def _inject_param(url: str, param: str, value: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[param] = value
    new_query = urlencode(query)
    return urlunparse(parsed._replace(query=new_query))


@tool
def probe_payloads(url: str, param: str, category: str = "sqli",
                    session_id: str = "", timeout: float = 10.0) -> str:
    """Run a short built-in payload set against one query parameter in a single call.

    Injects each payload from the chosen category into `param` on `url`,
    compares every response against a baseline (the same URL with a benign
    "1" value), and flags responses that look anomalous — a known error
    signature, a reflected-unescaped payload, a status code change, or a
    large response-length delta from baseline. This is a fast triage pass
    over a handful of common payloads, NOT an exhaustive fuzzer — treat a
    flagged row as "worth manually confirming", not "confirmed vulnerable".

    Non-destructive by design — every payload here is read-only (no DROP/
    DELETE/UPDATE variants). It only touches GET query parameters; for
    body/POST-field injection, use http_post or http_session_post manually
    with the same payload list from a category above.

    Parameters
    ----------
    url        : full URL to test, e.g. "https://example.com/search"
                 (existing query params, if any, are preserved — only
                 `param` is added/overwritten)
    param      : the query parameter name to inject into, e.g. "q" or "id"
    category   : one of "sqli", "xss", "traversal", "cmdi" (default "sqli")
    session_id : optional — reuse an authenticated http_session_get/post
                 session's cookie jar to fuzz a logged-in endpoint instead
                 of an anonymous one. Leave empty for an unauthenticated probe.
    timeout    : per-request timeout in seconds (default 10)
    """
    if category not in _PAYLOADS:
        return f"ERROR: category must be one of {list(_PAYLOADS.keys())} — got '{category}'"
    if not url.startswith(("http://", "https://")):
        return f"ERROR: Invalid URL '{url}'"
    if not param.strip():
        return "ERROR: param must not be empty."

    client = _get_session(session_id) if session_id else httpx.Client(
        timeout=15.0, follow_redirects=True, headers={"User-Agent": _UA},
    )
    owns_client = not session_id
    timeout = min(max(2.0, timeout), 30.0)

    def _do_request(u: str):
        t0 = time.monotonic()
        resp = client.get(u, timeout=timeout)
        return resp, int((time.monotonic() - t0) * 1000)

    try:
        baseline_url = _inject_param(url, param, "1")
        try:
            baseline_resp, baseline_ms = _do_request(baseline_url)
            baseline_status = baseline_resp.status_code
            baseline_len = len(baseline_resp.text)
        except Exception as exc:
            return f"[probe_payloads] Baseline request failed — {exc}. Fix connectivity before fuzzing."

        rows = [
            f"Baseline: status={baseline_status}  length={baseline_len}  time={baseline_ms}ms\n",
        ]
        flagged = 0
        for payload in _PAYLOADS[category]:
            test_url = _inject_param(url, param, payload)
            try:
                resp, ms = _do_request(test_url)
            except httpx.TimeoutException:
                rows.append(f"  [TIMEOUT >{timeout}s]  payload={payload!r}  -- possible time-based signal, worth a manual look")
                flagged += 1
                continue
            except Exception as exc:
                rows.append(f"  [ERROR: {exc}]  payload={payload!r}")
                continue

            body = resp.text
            reasons = []

            if category == "sqli" and any(sig in body.lower() for sig in _SQLI_SIGNATURES):
                reasons.append("SQL error signature in response")
            if category == "xss" and payload in body:
                reasons.append("payload reflected unescaped in response")
            if category == "traversal" and any(sig in body.lower() for sig in _TRAVERSAL_SIGNATURES):
                reasons.append("file-content signature in response")
            if category == "cmdi" and any(p.search(body) for p in _CMDI_SIGNATURES):
                reasons.append("command-output signature in response")

            if resp.status_code != baseline_status:
                reasons.append(f"status differs from baseline ({baseline_status} -> {resp.status_code})")
            if baseline_len > 0 and abs(len(body) - baseline_len) / baseline_len > 0.2:
                reasons.append(f"response length differs {baseline_len} -> {len(body)}")

            flag = "ANOMALY: " + "; ".join(reasons) if reasons else "clean"
            if reasons:
                flagged += 1
            rows.append(f"  [{flag}]  status={resp.status_code}  len={len(body)}  time={ms}ms  payload={payload!r}")
    finally:
        if owns_client:
            client.close()

    header = (
        f"probe_payloads: {category} — {len(_PAYLOADS[category])} payload(s) against "
        f"'{param}' on {url}{' [session: ' + session_id + ']' if session_id else ''}\n"
        f"{flagged} of {len(_PAYLOADS[category])} flagged for manual follow-up:\n"
    )
    return header + "\n".join(rows)


TOOLS = [http_session_get, http_session_post, http_session_reset, probe_payloads]
