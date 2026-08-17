# Skill: write_file

## When to use
Creating a new file, or REPLACING the entire contents of an existing one.
This overwrites — it does not merge or insert. If you only want to add
content to the end of an existing file, use append_file instead.

## Parameters
| Param   | Type   | Required | Notes |
|---------|--------|----------|-------|
| path    | string | yes      | Path relative to the current workspace, or absolute. |
| content | string | yes      | The FULL file content. Anything not included here is gone. |

## Correct call
{"path": "notes.txt", "content": "First line\nSecond line\n"}

## Known failure mode
Never call write_file on a file you haven't read first if the intent is to
modify existing content — you will destroy whatever was there. Read first,
then write the full merged content back.
