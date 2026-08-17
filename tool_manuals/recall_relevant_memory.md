# Skill: recall_relevant_memory

## When to use
Searching previously stored long-term facts BEFORE claiming you don't know
something, or before asking the user to repeat information they may have
given you in a past session.

## Parameters
| Param | Type   | Required | Notes |
|-------|--------|----------|-------|
| query | string | yes      | Natural-language description of what you're looking for — not a category name. |

## Correct call
{"query": "what operating system and hardware does the user run Beaver on"}

## Known failure mode
If this returns no results or an error (e.g. "Not ready"), say plainly that
you have no stored memory on the topic — do not fabricate a plausible-sounding
answer to fill the gap.
