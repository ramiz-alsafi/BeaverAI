# Tool manual: yarn (run via os_exec)

## When to use
Only in a project that actually uses yarn — check for a yarn.lock file
with list_directory first. If the project has package-lock.json instead,
use npm, not yarn — mixing package managers in one project corrupts the
lockfile state.

## Common correct invocations (Windows PowerShell)
| Goal | Command |
|------|---------|
| Install all deps | `yarn install` (or just `yarn`) |
| Add a package | `yarn add express` |
| Add as dev dependency | `yarn add --dev jest` |
| Run a script | `yarn build` |
| Run tests | `yarn test` |
| Check version | `yarn --version` |

## Windows-specific gotchas
- Same path-separator guidance as npm: prefer forward slashes in any path
  argument.
- **`yarn` may not be installed globally** even if `npm` is — if the
  command fails with something like "yarn is not recognized," that means
  yarn itself isn't installed (`npm install -g yarn`), not that the
  yarn command syntax was wrong. Report this distinction to the user.
- Yarn's lockfile (`yarn.lock`) getting out of sync with package.json can
  cause install failures — if `yarn install` fails and package.json was
  recently edited, that's the likely cause, not a yarn bug.

## Known failure modes
- **"error Couldn't find package.json"**: wrong working directory, same as
  npm — verify with get_workspace()/list_directory before retrying.
- **Long-running dev servers**: same caution as npm — `yarn dev`/`yarn start`
  typically doesn't exit on its own.
