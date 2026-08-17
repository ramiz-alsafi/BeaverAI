# Skill: store_long_term_memory

## When to use
Saving a fact that should persist across sessions — not for anything already
in the current conversation that doesn't need to survive it ending.

## Parameters
| Param    | Type   | Required | Notes |
|----------|--------|----------|-------|
| fact     | string | yes      | The actual fact, written as a complete standalone statement. |
| category | string | no       | Groups related facts. Defaults to "general" if omitted. |

## Correct call
{"fact": "The user's Ollama models directory is on the D: drive.", "category": "environment"}

## Known failure mode
This tool can fail silently from the model's point of view — the call
succeeds (no exception) but the underlying embedding call can still fail,
in which case the tool's return string will say so explicitly. ALWAYS
check that return string for a success confirmation before telling the
user something was saved. If it reports an error, say so — do not tell
the user "I've saved that" unless the tool actually confirmed it.
