# Skill: check_robots_sitemap

## When to use
Auditing crawlability at the start of an SEO review — confirms robots.txt
is reachable, reports Disallow rules, and validates that any referenced
sitemap.xml actually exists and is valid XML (not just that the directive
is present).

## Parameters
| Param   | Type   | Required | Notes |
|---------|--------|----------|-------|
| url     | string | yes      | Any URL on the site — only the domain/base is used, e.g. "https://example.com" |
| timeout | float  | no       | Defaults to 15s. |

## Correct call
{"url": "https://example.com"}

## Known failure mode
A missing robots.txt (HTTP 404) is NOT itself an error — it means the site
allows crawling by default. Report it as "no robots.txt found, crawling
unrestricted," not as a broken check. Similarly, a sitemap directive
existing in robots.txt doesn't guarantee the sitemap itself is reachable
or valid — this tool checks both separately and will report each.
