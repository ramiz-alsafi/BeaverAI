# Skill: context7 (resolve-library-id / get-library-docs)

## When to use
BEFORE writing code that uses an external library/framework you're not
100% certain of the current API for — React, Next.js, a Python package,
etc. This exists specifically to avoid guessing at APIs from training
data, which can be wrong or outdated.

## Two-step usage — always in this order
1. `resolve-library-id` with a free-text name (e.g. "Next.js 15",
   "Pydantic v2") — returns a Context7-compatible library ID.
2. `get-library-docs` with that exact ID from step 1 — returns real,
   version-specific documentation and code examples.

Do not skip step 1 and guess a library ID directly — IDs are specific
identifiers, not just the library name.

## Known failure mode
If context7 returns no results for a library, that means it isn't in
their index — fall back to what you actually know, and say plainly that
you're working from general knowledge rather than verified current docs
for that specific case. Don't silently treat a failed lookup as
confirmation your prior knowledge is correct.
