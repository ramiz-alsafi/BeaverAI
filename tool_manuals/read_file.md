# Skill: read_file

## When to use
Reading the contents of one specific file you already know the path to.
If you don't know the exact filename, use list_directory first — do not guess
a filename and call read_file on it hoping it exists.

## Parameters
| Param | Type   | Required | Notes |
|-------|--------|----------|-------|
| path  | string | yes      | Path relative to the current workspace, or absolute. |

## Correct call
{"path": "config.py"}
{"path": "C:/Users/ramiz/OneDrive/Desktop/beaver-3.0/requirements.txt"}

## Known failure mode
If the file doesn't exist, the tool returns an error string — report that
error to the user verbatim. Do not invent plausible-looking file contents
when the read fails.
