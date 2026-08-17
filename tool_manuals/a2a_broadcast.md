# Skill: a2a_broadcast

## When to use
Sending the SAME task to multiple sub-agents at once for parallel/comparative
work (e.g. asking both "coder" and "pentester" to review the same file from
their own angle). Not for splitting one big task into different sub-tasks —
use separate a2a_delegate calls for that instead.

## Parameters
| Param       | Type          | Required | Notes |
|-------------|---------------|----------|-------|
| task        | string        | yes      | Self-contained instruction, sent identically to every targeted agent. |
| agent_names | string / null | no       | Comma-separated registered agent names. Omit or leave null to broadcast to all configured agents. |

## Correct call
a2a_broadcast("Summarize any security concerns in main.py", "coder,pentester")

## Known failure mode
Same as a2a_delegate — agent_names must be REGISTERED agent names (confirm
via a2a_list()), never model names.

## Hardware note
On limited VRAM, broadcasting to many agents at once can be slow since each
sub-agent may need its own model loaded. Prefer a2a_delegate for a single
best-fit agent unless you genuinely need multiple perspectives.
