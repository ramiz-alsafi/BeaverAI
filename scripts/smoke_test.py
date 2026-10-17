"""Beaver smoke test - "does it actually start on this machine?"

Used by .github/workflows/compat.yml on Windows, macOS and Kali. The unit tests
use fakes; this goes one step further and checks, with NO Ollama and NO network:

  1. every Beaver module can be imported (catches missing deps, syntax that only
     breaks on one Python version, platform-specific import-time crashes);
  2. the real web server boots, listens, and answers HTTP.

Usage:
    python scripts/smoke_test.py               # backend only
    python scripts/smoke_test.py --frontend    # also require the built UI (200)
                                               # (run `npm run build` in web/frontend first)

Output is deliberately plain ASCII so it can't crash on a cp1252 Windows console.
Exit code 0 = pass, 1 = fail.
"""
import argparse
import importlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ["agent", "cli", "memory", "skills", "web", "plugins"]


def say(msg: str) -> None:
    print(msg, flush=True)


def discover_modules() -> list:
    mods = ["main"]
    for pkg in PACKAGES:
        for f in sorted((ROOT / pkg).glob("*.py")):
            mods.append(pkg if f.stem == "__init__" else f"{pkg}.{f.stem}")
    return mods


def check_imports() -> bool:
    sys.path.insert(0, str(ROOT))
    mods = discover_modules()
    failures = []
    for name in mods:
        try:
            importlib.import_module(name)
        except Exception as exc:  # report every failure, not just the first
            failures.append((name, f"{type(exc).__name__}: {exc}"))
    say(f"[imports] {len(mods) - len(failures)}/{len(mods)} modules imported OK")
    for name, err in failures:
        say(f"[imports] FAIL {name}: {err}".encode("ascii", "replace").decode())
    return not failures


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(url: str):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status, r.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:   # 503 etc. - server is up, just unhappy
        return exc.code, exc.read(2000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return None, ""


def check_server(need_frontend: bool, timeout: int) -> bool:
    port = free_port()
    env = dict(os.environ, BEAVER_NO_VITE="1", BEAVER_WEB_HOST="127.0.0.1",
               BEAVER_WEB_PORT=str(port), PYTHONUNBUFFERED="1")
    env.pop("BEAVER_WEB_TOKEN", None)
    log_path = Path(tempfile.gettempdir()) / f"beaver_smoke_{port}.log"
    say(f"[server] starting web/server.py on 127.0.0.1:{port}")
    with open(log_path, "wb") as log:
        proc = subprocess.Popen([sys.executable, str(ROOT / "web" / "server.py")],
                                cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
        status, body = None, ""
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if proc.poll() is not None:
                    say(f"[server] FAIL process exited early with code {proc.returncode}")
                    break
                status, body = fetch(f"http://127.0.0.1:{port}/")
                if status is not None:
                    break
                time.sleep(0.5)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()

    ok = False
    if status is None:
        say("[server] FAIL never answered HTTP")
    elif need_frontend:
        ok = status == 200 and 'id="root"' in body
        say(f"[server] GET / -> {status} (need 200 + app root): {'OK' if ok else 'FAIL'}")
    else:
        # Without a frontend build the server answers 503 "No production build"
        # - that still proves the backend booted and is serving requests.
        ok = status in (200, 503)
        say(f"[server] GET / -> {status}: {'OK' if ok else 'FAIL'}")

    if not ok:
        tail = log_path.read_bytes().decode("utf-8", "replace").splitlines()[-40:]
        say("---- last lines of server log ----")
        for line in tail:
            say(line.encode("ascii", "replace").decode())
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--frontend", action="store_true", help="require the built web UI (HTTP 200)")
    ap.add_argument("--timeout", type=int, default=60, help="seconds to wait for the server")
    args = ap.parse_args()

    say(f"[env] python {sys.version.split()[0]} on {sys.platform}")
    ok_imports = check_imports()
    ok_server = check_server(args.frontend, args.timeout)
    say("SMOKE TEST " + ("PASSED" if ok_imports and ok_server else "FAILED"))
    return 0 if ok_imports and ok_server else 1


if __name__ == "__main__":
    sys.exit(main())
