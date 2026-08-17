# Skill: check_structured_data

## When to use
Validating JSON-LD structured data (schema.org markup) on a page — this
catches malformed JSON-LD, which fails completely silently in search
engines (no visible error on the page itself) but means the structured
data has zero effect on search results.

## Parameters
| Param   | Type   | Required | Notes |
|---------|--------|----------|-------|
| url     | string | yes      | The specific page URL to check, not just the domain. |
| timeout | float  | no       | Defaults to 15s. |

## Correct call
{"url": "https://example.com/product/some-item"}

## Known failure mode
"No JSON-LD found" is a legitimate, common result — not every page needs
structured data. Report it as a fact, not as a failure of the tool. When a
block IS found but reports "INVALID JSON," that's a real, actionable
finding — quote the parse error, don't just say "structured data is
broken."
