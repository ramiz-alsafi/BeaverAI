# Skill: replace_in_file

## When to use
Editing part of an existing file. This should be your default over
`write_file` any time the file already exists and you're changing less
than the whole thing — `write_file` overwrites everything, `replace_in_file`
touches only the exact snippet you target.

## Parameters
| Param   | Type   | Required | Notes |
|---------|--------|----------|-------|
| path    | string | yes      | File must already exist — this isn't for creating new files, use `write_file` for that. |
| old_str | string | yes      | Must match the file's CURRENT raw content exactly — whitespace, indentation, and line endings included. Must occur exactly once in the file. |
| new_str | string | yes      | Replacement text. Empty string deletes `old_str`. |

## Correct call sequence
1. `read_file` or `search_in_files` first to see the file's actual current content — do not guess `old_str` from memory of what you wrote earlier in the conversation, the file may have been edited since.
2. Copy the exact snippet you want to change into `old_str`.
3. Call `replace_in_file`.

## Known failure modes
- **"old_str not found"**: almost always a whitespace/indentation mismatch, or the file was already changed since you last read it. Re-run `read_file` on the current content and copy `old_str` from the actual output, not from memory.
- **"old_str appears N times"**: the snippet you picked isn't unique. Widen it — include a line above or below, or enough of the surrounding function signature — until it identifies exactly one location. Do not fall back to `write_file` to avoid this; that's exactly the full-file-rewrite risk this tool exists to prevent.
- **Multiple separate edits needed in one file**: call `replace_in_file` once per edit, each with its own uniquely-matched `old_str`. There's no batch/multi-edit mode — that's intentional, each edit is independently verified.
- **File is very large and you only saw a truncated `read_file` output**: that's fine — `replace_in_file` doesn't need the whole file in context, only the exact snippet being changed. This is the main reason to prefer it over `write_file` on large files.
