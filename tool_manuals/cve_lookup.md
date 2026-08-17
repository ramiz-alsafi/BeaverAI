# Skill: cve_lookup

## When to use
After identifying a specific product AND version during enumeration (e.g.
from an nmap -sV scan) — search for known CVEs to cross-reference. Not
useful with just a product name and no version; results will be too broad.

## Parameters
| Param         | Type | Required | Notes |
|---------------|------|----------|-------|
| keyword       | string | yes    | Product + version, e.g. "Apache 2.4.49" — the more specific, the better the match. |
| results_limit | int    | no     | Defaults to 10, max 20. |

## Correct call
{"keyword": "OpenSSH 8.2"}

## Known failure mode
NVD rate-limits unauthenticated requests (~5 per 30s). If you get a
rate-limit response, wait before the next lookup rather than retrying
immediately — space CVE lookups out during a long engagement rather than
firing them back-to-back.