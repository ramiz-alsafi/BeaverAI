# Tool manual: pytest (run via os_exec)

## When to use
Running Python test suites. Check for a tests/ directory or files matching
test_*.py / *_test.py with list_directory first if unsure whether the
project uses pytest vs. unittest vs. something else.

## Common correct invocations (Windows PowerShell)
| Goal | Command |
|------|---------|
| Run all tests | `pytest` |
| Run one file | `pytest tests/test_config.py` |
| Run one specific test | `pytest tests/test_config.py::test_default_model` |
| Verbose output | `pytest -v` |
| Stop at first failure | `pytest -x` |
| Show print() output | `pytest -s` |

## Windows-specific gotchas
- **"'pytest' is not recognized"**: pytest is installed per-Python-environment,
  not globally by default. If this happens, try `python -m pytest` instead —
  that uses whichever Python environment is active, which is more reliable
  than depending on a `pytest` entry on PATH.
- If a venv is expected but not activated, `python -m pytest` will still
  run against the WRONG (system) Python and may report all tests failing
  with import errors — check `python -c "import sys; print(sys.executable)"`
  first if results look suspiciously wrong.

## Known failure modes
- **Exit code 1 with real test failures**: this is pytest working correctly
  and reporting failing tests — read the actual failure output (assertion
  details) and report those specifics, don't just say "tests failed."
- **Exit code 2 / collection errors**: usually an import error in a test
  file itself, not a failing assertion — different problem, needs to be
  diagnosed by reading the actual import traceback, not by re-running.
- **Exit code 5**: no tests were collected at all — wrong directory or no
  files match the test naming pattern, not a code bug to "fix."
