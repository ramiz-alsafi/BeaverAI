# Skill: http_session_get / http_session_post / http_session_reset

## When to use
Any web test that requires login state to carry across more than one
request — auth checks, IDOR/access-control testing (two accounts,
compared), anything behind a session cookie. Plain `http_get`/`http_post`
open a fresh client every call and have no cookie jar — they cannot do
this, they will silently look "logged out" on the second call even after
a successful login on the first.

## Parameters
| Param      | Type   | Required | Notes |
|------------|--------|----------|-------|
| session_id | string | yes      | Your own label for one identity, e.g. "user_a". Reused automatically on every call with the same value — that's what makes the cookie jar persist. |
| url        | string | yes      | Full URL, http(s):// required. |
| body       | string | no (POST)| Raw request body. |
| headers    | string | no       | JSON string of EXTRA headers for this call only — do not put cookies here, they're handled automatically. |

## Correct call sequence for an authenticated test
1. `http_session_post(session_id="user_a", url="https://target/login", body="...")` — login response's `Set-Cookie` is captured automatically.
2. `http_session_get(session_id="user_a", url="https://target/account")` — same session_id, cookie is sent automatically, no manual copying.
3. For a second identity to compare against (IDOR/access-control testing), repeat with a DIFFERENT `session_id` (e.g. `"user_b"`) — each session_id gets its own isolated cookie jar.
4. `http_session_reset(session_id="user_a")` when done, or `http_session_reset()` with no argument to clear every open session at once.

## Known failure modes
- **Still getting 401/unauthorized on step 2**: confirm you used the SAME `session_id` string on both calls — a typo (`"user_a"` vs `"user_A"`) creates a second, separate, unauthenticated session instead of reusing the first.
- **Login itself failing**: check the response body from the `http_session_post` call for the actual error — a 200 status with an error message in the body still means the login didn't set a real session cookie; check the "cookie jar: N cookie(s) stored" line in the response, 0 means nothing was set.
- **Session count exceeds 10 open at once**: the oldest session is auto-evicted (closed) to avoid leaking connections — if you need more than 10 concurrent identities in one engagement, call `http_session_reset` on ones you're done with rather than relying on eviction order.

---

# Skill: probe_payloads

## When to use
Fast triage across a short built-in payload set (`sqli`, `xss`, `traversal`,
`cmdi`) against ONE query parameter, in a single tool call instead of one
call per payload. This is a triage pass, not a fuzzer or an exploit tool —
it flags candidates for manual confirmation, it does not confirm anything
by itself.

## Parameters
| Param      | Type   | Required | Notes |
|------------|--------|----------|-------|
| url        | string | yes      | Existing query params are preserved; only `param` is added/overwritten. |
| param      | string | yes      | The query parameter name to inject into, e.g. "q", "id", "search". |
| category   | string | no       | One of "sqli" (default), "xss", "traversal", "cmdi". |
| session_id | string | no       | Reuse an `http_session_get`/`post` session's cookies to fuzz a logged-in endpoint. Omit for an anonymous probe. |

## Correct call
`probe_payloads(url="https://target/search", param="q", category="sqli")`

## Reading the output
Every row is either `[clean]` or `[ANOMALY: <reason(s)>]`. An ANOMALY row
means: a known error signature matched, the payload reflected unescaped,
the status code changed from baseline, or the response length shifted
more than 20% from baseline. **None of these alone is a confirmed
vulnerability** — a length shift can be entirely benign (e.g. an
apostrophe just changing display text). Always follow an ANOMALY row with
one manual `http_get`/`http_session_get` call reproducing that exact
payload before writing it into a findings table, and report it as
"confirmed" only once that manual check backs the claim.

## Known failure modes
- **"Baseline request failed" error**: the target URL itself isn't reachable — fix connectivity/URL first, this isn't a probe_payloads bug.
- **Category not in the fixed list**: only "sqli", "xss", "traversal", "cmdi" exist — there's no generic/custom payload list parameter. For anything outside these four categories, or POST-body injection (this tool is GET-query-param only), run the payloads manually via `http_post`/`http_session_post`.
- **Everything flagged, including the baseline-adjacent payloads**: some apps just have naturally noisy/dynamic responses (timestamps, CSRF tokens, ads) that shift length on every request regardless of payload — if every row flags, treat the whole category as inconclusive on this endpoint rather than reporting every row as a finding.
