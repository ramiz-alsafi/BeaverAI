# Tool manual: make (run via os_exec)

## When to use
Running build targets defined in a Makefile. Check for a file literally
named "Makefile" (no extension) with list_directory first.

## IMPORTANT Windows-specific note — read before using
**`make` is not built into Windows.** Unlike macOS/Linux, there is no
GNU Make preinstalled. If the command fails with "'make' is not
recognized," this does NOT mean the Makefile or command syntax is wrong —
it means `make` itself isn't installed. Do not keep retrying the same
command. Instead:
- Check if it's available via a package manager: `choco install make` or
  `scoop install make`, if the user has Chocolatey/Scoop.
- Check if this project expects WSL (Windows Subsystem for Linux) — many
  Linux-first projects assume a Linux-like environment for `make`.
- If MSVC tooling is present, `nmake` (Microsoft's own make equivalent,
  different syntax) may be the intended tool instead — check the project's
  README/docs before assuming.
Report this distinction clearly to the user rather than treating it as a
Makefile bug.

## Common correct invocations (once make IS available)
| Goal | Command |
|------|---------|
| Run the default target | `make` |
| Run a specific target | `make build` |
| Run tests defined as a target | `make test` |
| Clean build artifacts | `make clean` |
| Dry run (show commands without executing) | `make -n` |

## Known failure modes
- **"No rule to make target"**: the target name doesn't exist in the
  Makefile — read the Makefile (read_file) to see actual target names
  rather than guessing common ones like "build"/"test".
- **"missing separator"**: a Makefile syntax error (tabs vs. spaces
  matters in Makefiles) — this is a genuine file problem to report, not
  something retrying the make command fixes.
