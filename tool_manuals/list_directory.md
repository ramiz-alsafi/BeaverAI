# Skill: list_directory

## When to use
Seeing what's actually in a folder BEFORE assuming a filename exists, or
before calling read_file/write_file on a guessed path.

## Parameters
| Param | Type   | Required | Notes |
|-------|--------|----------|-------|
| path  | string | no       | Defaults to "." (current workspace root) if omitted. |

## Correct call
{"path": "."}
{"path": "prompts/personas"}
