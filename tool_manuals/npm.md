# Tool manual: npm (run via os_exec)

## When to use
Installing, running, or managing Node.js packages/scripts in a project that
already has a package.json. Check with list_directory or read_file first if
you're not sure the project uses npm (vs. yarn/pnpm — check for
package-lock.json vs yarn.lock vs pnpm-lock.yaml before assuming).

## Common correct invocations (Windows PowerShell)
| Goal | Command |
|------|---------|
| Install all deps from package.json | `npm install` |
| Install one package | `npm install express` |
| Install as dev dependency | `npm install --save-dev jest` |
| Run a script defined in package.json | `npm run build` |
| Run tests | `npm test` |
| Check installed version | `npm --version` |

## Windows-specific gotchas
- **Path separators**: when a command NEEDS a path argument, use forward
  slashes (`C:/Users/yourname/project`) — npm and most Node tooling accept
  these fine on Windows and it avoids PowerShell backslash-escaping issues.
- **`npm.cmd` vs `npm`**: on Windows, npm resolves to `npm.cmd` under the
  hood, but you should still just write `npm ...` in the command — the
  shell handles resolution. Do not manually write `npm.cmd` in the command
  string, it's unnecessary and can behave inconsistently in os_exec.
- **Long-running dev servers**: `npm run dev` / `npm start` for a dev
  server does NOT exit — it will run until the os_exec timeout kills it.
  Do not use this to "start the server" as a background task; report to
  the user that this needs to run in a separate terminal they manage, or
  set an explicit short timeout if you only need to confirm it starts
  without immediately crashing.
- **Global installs need admin sometimes**: `npm install -g <pkg>` can fail
  with a permissions error on Windows depending on install location. If it
  fails with an access-denied-type error, report that rather than retrying
  the same command.

## Known failure modes
- **"npm ERR! code ENOENT" / "no such file or directory, open 'package.json'"**:
  wrong working directory — check get_workspace() and confirm package.json
  exists there with list_directory before retrying.
- **"npm ERR! peer dep" warnings**: these are warnings, not failures — check
  the actual exit code before reporting this as a failed install.
