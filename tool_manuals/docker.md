# Tool manual: docker (run via os_exec)

## When to use
Building, running, or inspecting containers for a project that has a
Dockerfile or docker-compose.yml. Check with list_directory first.

## Common correct invocations (Windows PowerShell)
| Goal | Command |
|------|---------|
| Build an image | `docker build -t myapp .` |
| Run a container | `docker run -p 8080:8080 myapp` |
| List running containers | `docker ps` |
| List all containers (incl. stopped) | `docker ps -a` |
| Stop a container | `docker stop <container-id>` |
| Compose up | `docker compose up -d` |
| Compose down | `docker compose down` |
| View logs | `docker logs <container-id>` |

## Windows-specific gotchas
- **Docker Desktop must be running**: on Windows, the `docker` CLI talks to
  Docker Desktop's engine — if it's not running, every command fails with
  something like "error during connect... the system cannot find the file
  specified" (same class of error as an unreachable pipe/socket). Report
  that Docker Desktop needs to be started, don't retry the same command.
- **Path mounting**: when mounting a volume (`-v`), Windows paths need to be
  either in `C:/Users/...` forward-slash form or Docker Desktop's WSL2
  path form (`/mnt/c/Users/...`) depending on backend — if a volume mount
  silently shows empty inside the container, this is the likely cause.
- **`docker compose` (v2, no hyphen) vs `docker-compose` (v1, hyphenated,
  separate binary)**: modern Docker Desktop ships v2 as `docker compose`
  (two words). If `docker-compose` (hyphenated) fails with "not
  recognized," use `docker compose` instead.
- **Never run detached containers and assume they immediately succeeded**:
  `docker run -d` returns instantly with a container ID even if the
  container crashes seconds later. Follow up with `docker ps` or
  `docker logs` to confirm it's actually still running before reporting
  success.

## Known failure modes
- **"Cannot connect to the Docker daemon"**: Docker Desktop isn't running —
  not a command syntax issue.
- **"port is already allocated"**: another process/container already uses
  that host port — pick a different host port in the `-p` mapping, don't
  retry the identical command.
