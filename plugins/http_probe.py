"""
Beaver Plugin — HTTP Probe
===========================

Lightweight HTTP inspection tools — no new dependencies (uses httpx which
is already in requirements.txt).

Useful for:
  coder      — test REST APIs, check endpoints, debug request/response cycles
  pentester  — enumerate HTTP headers, test auth, check redirects
  seo        — check status codes, TTFB, headers, redirect chains
  standard   — quick URL health checks

Tools
-----
  http_get     — GET request: returns status, headers, and body preview
  http_post    — POST request with arbitrary body
  http_head    — HEAD request: status + headers only (no body download)
  http_check   — quick health check on a list of URLs (status only)
"""

import json
import time
import httpx
from langchain_core.tools import tool

PERSONA  = ["coder", "pentester", "seo", "standard", "researcher", "orchestrator"]
ENABLED  = True

_DEFAULT_TIMEOUT = 15.0
_MAX_BODY        = 6_000
_UA              = "Beaver-Agent/2.0 (http-probe)"


def _format_headers(headers: httpx.Headers) -> str:
    return "\n".join(f"  {k}: {v}" for k, v in headers.items())


@tool
def http_get(url: str, headers: str = "", timeout: float = 15.0) -> str:
    """Send an HTTP GET request and return status, headers, timing, and body.

    Use this to inspect REST APIs, check page availability, or read
    endpoint responses during development or security testing.

    Parameters
    ----------
    url     : full URL (must start with http:// or https://)
    headers : optional JSON string of extra headers, e.g. '{"Authorization":"Bearer xyz"}'
    timeout : request timeout in seconds (default 15)
    """
    if not url.startswith(("http://", "https://")):
        return f"ERROR: Invalid URL '{url}'"

    extra_headers = {}
    if headers.strip():
        try:
            extra_headers = json.loads(headers)
        except json.JSONDecodeError:
            return f"ERROR: headers must be valid JSON — got: {headers}"

    try:
        t0 = time.monotonic()
        with httpx.Client(
            timeout=min(max(1.0, timeout), 120.0),
            follow_redirects=True,
            headers={"User-Agent": _UA, **extra_headers},
        ) as client:
            resp = client.get(url)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
    except httpx.TimeoutException:
        return f"[http_get] Timeout after {timeout}s — {url}"
    except Exception as exc:
        return f"[http_get] Error: {exc}"

    body = resp.text
    truncated = ""
    if len(body) > _MAX_BODY:
        truncated = f"\n[... body truncated — {len(body) - _MAX_BODY} more chars ...]"
        body = body[:_MAX_BODY]

    return (
        f"GET {url}\n"
        f"Status : {resp.status_code} {resp.reason_phrase}\n"
        f"Time   : {elapsed_ms} ms\n"
        f"URL    : {resp.url}\n"
        f"\nHeaders:\n{_format_headers(resp.headers)}\n"
        f"\nBody:\n{body}{truncated}"
    )


@tool
def http_post(url: str, body: str = "", content_type: str = "application/json",
              headers: str = "", timeout: float = 15.0) -> str:
    """Send an HTTP POST request with an arbitrary body.

    Use for API testing, form submission, or webhook delivery.

    Parameters
    ----------
    url          : full URL
    body         : request body as a string (JSON, form-encoded, plain text, etc.)
    content_type : Content-Type header (default application/json)
    headers      : optional JSON string of extra headers
    timeout      : request timeout in seconds (default 15)
    """
    if not url.startswith(("http://", "https://")):
        return f"ERROR: Invalid URL '{url}'"

    extra_headers = {}
    if headers.strip():
        try:
            extra_headers = json.loads(headers)
        except json.JSONDecodeError:
            return f"ERROR: headers must be valid JSON"

    try:
        t0 = time.monotonic()
        with httpx.Client(
            timeout=min(max(1.0, timeout), 120.0),
            follow_redirects=True,
            headers={"User-Agent": _UA, "Content-Type": content_type, **extra_headers},
        ) as client:
            resp = client.post(url, content=body.encode())
        elapsed_ms = int((time.monotonic() - t0) * 1000)
    except httpx.TimeoutException:
        return f"[http_post] Timeout after {timeout}s — {url}"
    except Exception as exc:
        return f"[http_post] Error: {exc}"

    resp_body = resp.text[:_MAX_BODY]
    return (
        f"POST {url}\n"
        f"Status : {resp.status_code} {resp.reason_phrase}\n"
        f"Time   : {elapsed_ms} ms\n"
        f"\nHeaders:\n{_format_headers(resp.headers)}\n"
        f"\nBody:\n{resp_body}"
    )


@tool
def http_head(url: str, timeout: float = 10.0) -> str:
    """Send an HTTP HEAD request — returns status and headers without downloading the body.

    Use to quickly check if a URL is reachable, its content type, caching
    headers, server info, or redirect destination without a full GET.

    Parameters
    ----------
    url     : full URL
    timeout : request timeout in seconds (default 10)
    """
    if not url.startswith(("http://", "https://")):
        return f"ERROR: Invalid URL '{url}'"

    try:
        t0 = time.monotonic()
        with httpx.Client(
            timeout=min(max(1.0, timeout), 60.0),
            follow_redirects=True,
            headers={"User-Agent": _UA},
        ) as client:
            resp = client.head(url)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
    except httpx.TimeoutException:
        return f"[http_head] Timeout after {timeout}s — {url}"
    except Exception as exc:
        return f"[http_head] Error: {exc}"

    return (
        f"HEAD {url}\n"
        f"Status  : {resp.status_code} {resp.reason_phrase}\n"
        f"Final URL: {resp.url}\n"
        f"Time    : {elapsed_ms} ms\n"
        f"\nHeaders:\n{_format_headers(resp.headers)}"
    )


@tool
def http_check(urls: str, timeout: float = 8.0) -> str:
    """Check the HTTP status of one or more URLs (one per line or comma-separated).

    Returns a table: URL | status | time_ms | reachable.
    Use for bulk availability checks, link validation, or redirect auditing.

    Parameters
    ----------
    urls    : newline- or comma-separated list of URLs
    timeout : per-request timeout in seconds (default 8)
    """
    raw = [u.strip() for u in urls.replace(",", "\n").splitlines() if u.strip()]
    if not raw:
        return "[http_check] No URLs provided."

    raw = raw[:20]  # cap at 20 to avoid runaway calls
    results = []
    timeout = min(max(1.0, timeout), 30.0)

    for url in raw:
        if not url.startswith(("http://", "https://")):
            results.append((url, "INVALID", "-", "✗"))
            continue
        try:
            t0 = time.monotonic()
            with httpx.Client(
                timeout=timeout, follow_redirects=True,
                headers={"User-Agent": _UA}
            ) as client:
                resp = client.head(url)
            ms = int((time.monotonic() - t0) * 1000)
            ok = "✓" if resp.status_code < 400 else "✗"
            results.append((url[:70], str(resp.status_code), f"{ms}ms", ok))
        except httpx.TimeoutException:
            results.append((url[:70], "TIMEOUT", "-", "✗"))
        except Exception as exc:
            results.append((url[:70], f"ERR: {str(exc)[:40]}", "-", "✗"))

    col_w = max(len(r[0]) for r in results) + 2
    header = f"{'URL':<{col_w}}  {'Status':<8}  {'Time':<8}  OK"
    sep    = "-" * (col_w + 24)
    rows   = [f"{r[0]:<{col_w}}  {r[1]:<8}  {r[2]:<8}  {r[3]}" for r in results]
    return "\n".join([header, sep] + rows)


TOOLS = [http_get, http_post, http_head, http_check]