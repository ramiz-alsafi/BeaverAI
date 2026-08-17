# Skill: sequential-thinking__sequentialthinking

## When to use
Multi-step reasoning where you need to revise earlier conclusions as new
information comes in. NOT for simple lookups or single-fact questions —
wastes a turn on those.

## Required parameters — ALL FOUR must be present on EVERY call
| Param             | Type    | Notes |
|-------------------|---------|-------|
| thought           | string  | Your actual reasoning content for this step. Never omit. |
| thoughtNumber     | integer | Starts at 1, increments each call. |
| totalThoughts     | integer | Your current estimate of total steps. Can revise upward later. |
| nextThoughtNeeded | boolean | true unless this is the final thought, then false. Never omit. |

## Correct call — copy this shape exactly
{"thought": "Breaking down the problem: comparing SQLite vs JSON on size, speed, and complexity.", "thoughtNumber": 1, "totalThoughts": 4, "nextThoughtNeeded": true}

## Known failure (seen in production, confirmed via logs)
This tool has failed 5 consecutive times in the past specifically from
omitting `thought` and `nextThoughtNeeded` while still including
`thoughtNumber`/`totalThoughts`. Every call returned "MCP error -32602:
Input validation error" and, instead of reporting that failure, the fake
reasoning was fabricated in prose as if the tool had worked. If this tool
returns an error, SAY SO in your response — do not silently fall back to
simulating its output yourself.

## Anti-example — do NOT do this
{"thoughtNumber": 1, "totalThoughts": 5}   ← missing thought and nextThoughtNeeded, WILL fail
