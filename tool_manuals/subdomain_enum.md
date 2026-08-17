# Skill: subdomain_enum

## When to use
Subdomain discovery during the recon phase, before active scanning.
Only against domains already confirmed in authorized scope.

## Parameters
| Param  | Type   | Required | Notes |
|--------|--------|----------|-------|
| domain | string | yes      | Bare domain, no protocol/www — "example.com" not "https://www.example.com" |
| timeout| float  | no       | crt.sh request timeout, defaults to 20s. crt.sh can be slow on large domains. |
| verify | bool   | no       | Defaults to False (raw, unverified list). Set True to DNS-resolve every result and split live vs. dead — this is the version you actually want before probing anything. |

## Correct call
`{"domain": "example.com", "verify": true}`

## Known failure modes
- **verify=False (default) result count looks huge and unreliable**: that's expected — crt.sh returns every name ever issued a cert, including decommissioned hosts and wildcard-cert noise. Re-run with `verify=True` before treating the list as a real target set; it costs one DNS round-trip per name but tells you which actually resolve today.
- **verify=True comes back mostly "NO DNS RECORD"**: normal for domains that rotate infrastructure often. This isn't a tool failure — it's real signal that most of the historical cert names are dead. Focus follow-up work on the LIVE section.
- Even with `verify=True`, a resolving hostname is not the same as "reachable app" — a follow-up `http_head`/`http_get` (or nmap) is still needed to confirm what's actually running there.

---

# Skill: subdomain_bruteforce

## When to use
When `subdomain_enum` (crt.sh, passive) comes back thin or empty. Some
subdomains never receive a public TLS certificate — internal admin panels,
DNS-only split-horizon staging environments — and simply won't exist in
certificate transparency logs no matter how thoroughly you query them.
This tool finds those by asking DNS directly instead.

**This is active reconnaissance** — it sends real DNS queries for every
candidate name. Only use it against domains already confirmed in
authorized scope, same rule as any active check.

## Parameters
| Param    | Type   | Required | Notes |
|----------|--------|----------|-------|
| domain   | string | yes      | Bare domain, no protocol/www. |
| wordlist | string | no       | Comma/newline-separated labels to try instead of the built-in ~70-word common list, e.g. "vpn,admin,api-v2". Capped at 300 entries even if you supply more. |
| timeout  | float  | no       | Per-name DNS resolution timeout, defaults to 8s. |

## Correct call
`{"domain": "example.com"}` — uses the built-in common-name wordlist.

## Known failure mode
Zero results doesn't mean the domain has no other subdomains — it means
none of the ~70 common labels tried happen to resolve. A custom
`wordlist` targeted at what you already know about the organization
(product names, team names, internal tool names picked up during other
recon) will usually outperform the generic list on a specific target.