# Skill: os_exec

## When to use
Running a real shell command and getting its actual stdout/stderr/exit code back.
NOT for reading or writing files — use read_file/write_file/list_directory for that
(they're safer and don't need shell quoting).

## Parameters (both go directly in the tool call, not nested)
| Param   | Type   | Required | Notes |
|---------|--------|----------|-------|
| command | string | yes      | The full shell command, e.g. `"whoami"`, `"nmap -sV 127.0.0.1"`. |
| timeout | int    | no       | Max seconds before the process is killed. Defaults to 60 if omitted. |

## Correct call
{"command": "dir C:\\Users\\ramiz", "timeout": 30}

## Exit codes
0 = success. Non-zero = failure. ALWAYS check which one you got before deciding
whether the command worked — a non-zero exit code with output on stdout is still
a failure, not a partial success.

## Known failure mode
Do not fabricate command output. If os_exec returns an error or non-zero exit
code, report the actual stderr/exit code to the user — do not invent what the
command "would have" printed.

## Anti-example — do NOT do this
Writing the command as prose or a markdown code block instead of a real tool
call (e.g. showing ```sh\nnmap -sV 192.168.1.2\n``` in your text response).
That does not execute anything — the command never runs, and the user has to
run it manually themselves, defeating the entire point of having this tool.
