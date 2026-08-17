# Skill: filesystem__list_directory

## When to use
Listing files/folders in a directory via the MCP filesystem server.

## IMPORTANT — different from the built-in `list_directory` tool
This is the MCP `filesystem` server's tool, not the built-in `list_directory`
skill. Unlike the built-in version, **`path` is REQUIRED and has NO default**
— calling this with an empty arguments object `{}` always fails with
"Invalid input: expected string, received undefined at path".

## Parameters
| Param | Type   | Required | Notes |
|-------|--------|----------|-------|
| path  | string | **yes**  | Must be an explicit absolute or relative path. No default. |

## Correct call
{"path": "."}
{"path": "C:\\Users\\ramiz\\OneDrive\\Desktop\\beaver-3.0"}

## If you don't know the path yet
Call `get_workspace` first (no arguments) to get the current workspace root,
then pass that path here explicitly.
