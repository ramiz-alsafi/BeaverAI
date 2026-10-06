# Skill: a2a_delegate

## When to use
Handing one self-contained task to a specialized sub-agent (persona-specific,
possibly a different model). Only for orchestrator-style workflows.

## Parameters (ORDER AND TYPE MATTER)
| Param      | Type   | Required | Notes |
|------------|--------|----------|-------|
| agent_name | string | yes      | The REGISTERED AGENT NAME (e.g. "coder", "pentester") — NEVER a model name like "qwen2.5-coder:7b". Always confirm the exact name with a2a_list() first if unsure. |
| task       | string | yes      | A complete, self-contained instruction. The sub-agent has NO access to this conversation's history — include every fact it needs (file paths, exact goal, constraints) directly in this string. |

## Correct call
a2a_delegate("coder", "Read agent/parsing.py in the current workspace. The function parse_date() raises ValueError on ISO-8601 strings with timezone offsets. Fix it and run the test suite.")

## Known failure mode
Calling a2a_delegate with a model string ("qwen2.5-coder:7b") instead of the
registered agent name ("coder") as the first argument. This always fails
with "Unknown agent". Call a2a_list() first if you are not 100% sure of the
exact registered name.

## Anti-example — do NOT do this
a2a_delegate("qwen2.5-coder:7b", "fix the bug")   ← wrong: model name, not agent name; task too vague
