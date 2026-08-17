# Skill: append_file

## When to use
Adding content to the END of an existing file without touching what's
already there. Use this instead of write_file when you're logging, adding
a new entry, or extending a file rather than replacing it.

## Parameters
| Param   | Type   | Required | Notes |
|---------|--------|----------|-------|
| path    | string | yes      | Path relative to the current workspace, or absolute. |
| content | string | yes      | Only the NEW text to add — not the whole file. |

## Correct call
{"path": "log.txt", "content": "New entry at the end\n"}
