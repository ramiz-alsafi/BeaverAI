# Tool manual: node (run via os_exec)

## When to use
Running a JavaScript file directly, or checking the installed Node.js
version. NOT for installing packages — that's npm/yarn.

## Common correct invocations (Windows PowerShell)
| Goal | Command |
|------|---------|
| Run a script | `node script.js` |
| Check version | `node --version` |
| Run an inline one-liner | `node -e "console.log(1+1)"` |
| REPL | do NOT invoke bare `node` with no args via os_exec — it opens an interactive prompt that never exits and will hang until the os_exec timeout kills it. |

## Windows-specific gotchas
- Same forward-slash path guidance as npm/yarn.
- **PowerShell quoting for `-e` inline scripts**: use double quotes around
  the JS, and prefer single quotes *inside* the JS string if it needs its
  own quoting, since PowerShell's escaping rules for nested double quotes
  are inconsistent. Example: `node -e "console.log('hello')"` works
  reliably; nested double-quotes inside a double-quoted `-e` argument can
  break.
- **Long-running servers**: `node server.js` for anything that starts an
  HTTP server or listens on a port will not exit — same caution as
  npm/yarn dev servers above.

## Known failure modes
- **"'node' is not recognized"**: Node.js isn't installed or isn't on PATH
  for this shell session — report this plainly, don't retry the same
  command expecting a different result.
- **"Cannot find module"**: either a missing `npm install` (check for
  node_modules/ with list_directory) or a wrong relative path in a
  `require()`/`import` inside the script — not something more npm/yarn
  commands will fix on their own.
