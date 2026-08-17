# Tool manual: nmap (run via os_exec)

## When to use
Host/port/service discovery on a target already confirmed to be in scope.
NEVER run nmap (or any scan) against a target outside the authorized scope
list — os_exec's scope gate blocks this automatically, but do not attempt
to work around it or scan a target "just to check."

## Common correct invocations
| Goal | Command |
|------|---------|
| Quick host discovery | `nmap -sn 10.10.10.0/24` |
| Default port scan | `nmap 10.10.10.5` |
| Service/version detection | `nmap -sV 10.10.10.5` |
| Common vuln script scan | `nmap -sV -sC 10.10.10.5` |
| Specific ports only | `nmap -p 80,443,8080 10.10.10.5` |
| Full TCP port range (slow) | `nmap -p- 10.10.10.5` |

## Known failure modes
- **No `--timeout` flag exists in nmap** (confirmed real failure — the model
  invented this flag and nmap rejected it with "unrecognized option"). To
  limit how long a single host takes, use `--host-timeout <time>` (e.g.
  `--host-timeout 30s`). To limit the WHOLE command's runtime, use
  os_exec's own `timeout` parameter instead — that's what it's for, don't
  try to pass a made-up nmap flag for it.
- **Permission denied / requires root**: SYN scans (`-sS`) and OS detection
  (`-O`) need elevated privileges. If the exit code is non-zero and stderr
  mentions permissions, fall back to a TCP connect scan (`-sT`, no special
  privileges needed) instead of just reporting failure.
- **Host seems down (0 hosts up)**: nmap defaults to a ping check first and
  skips scanning hosts that don't respond to it. Add `-Pn` to skip the ping
  check and scan anyway if you have reason to believe the host is up but
  filtering ICMP.
- **Command hangs / times out**: `-p-` (all 65535 ports) is slow — increase
  the os_exec `timeout` parameter rather than assuming the scan failed.

## Anti-example — do NOT do this
Reporting scan results you did not actually get from a real os_exec call —
if the command times out or errors, say so. Never fabricate plausible-looking
open ports or service versions.