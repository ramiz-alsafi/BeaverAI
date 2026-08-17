"""
OS Execution Engine for Beaver Agent
Async shell command execution with live output streaming and process tree cancellation.

Fix log
-------
[FIX-1] Added asyncio.Lock inside AsyncProcessRunner so concurrent tool calls
        (multiple tool_calls in one LLM response) are serialised rather than
        racing over the shared _current_process handle. Without this, the
        second call overwrites _current_process, corrupting both outputs and
        making cancellation of the first call impossible.

[FIX-2] Fixed Unix process group kill. The old code called proc.send_signal()
        which targets only the parent process. Because start_new_session=True
        creates a dedicated process group, os.killpg() is required to reach
        the entire subprocess tree (e.g. shells spawned by nmap, bash -c, etc.).
        SIGINT is sent first (graceful), then SIGKILL after 0.5 s if still alive.

[FIX-3] Zombie-process prevention. After kill, the runner now awaits the
        process to ensure the OS reaps it before the lock is released.

[FIX-4] Converted os_exec from a @tool-decorated function to an OsExecTool
        (BaseTool subclass) that exposes astream_invoke(args, output_callback).
        graph.py FIX-5 routes streaming tools through this interface — no more
        hardcoded tool-name branch in the executor.

[FIX-5] Removed os_exec_with_callback standalone function. It is fully
        superseded by OsExecTool.astream_invoke.

[FIX-24] _format_output now converts an exit code >= 2**31 back to its
        signed 32-bit value for display. Windows reports process exit
        codes as an unsigned DWORD, so a process exiting with -1 (a common
        "invalid argument" signal) previously showed as the confusing
        4294967295 instead of -1 — both to the user reading logs and to
        the model deciding how to interpret/retry the failure.

[FIX-25] AsyncProcessRunner.execute()/astream_invoke() now accept an
        explicit cwd, passed straight to create_subprocess_shell(cwd=...)
        instead of relying on the process-wide os.chdir() that
        file_ops.set_workspace() calls. os.chdir() can't be scoped to one
        session — it moves every concurrent WebSocket connection's shell
        commands. Passing cwd explicitly makes each call's working
        directory correct regardless of what the process cwd currently is.
        OsExecTool resolves it from skills.file_ops.get_active_workspace_root()
        by default so single-session (CLI/TUI) behavior is unchanged.

[FIX-26] The module singleton `_runner` is no longer the only runner in
        play. It remains the default for CLI/TUI/A2A sub-agent threads, but
        web/server.py now gives each WebSocket connection its own
        AsyncProcessRunner via set_session_runner() (contextvar-backed, same
        pattern as agent.config's session config). Without this, every
        session's os_exec calls funneled through one shared instance's
        asyncio.Lock (FIX-1) — commands from unrelated sessions queued
        behind each other, and cancel_active_process()/the STOP button in
        one tab could kill another tab's in-flight command.
"""
import asyncio
import contextvars
import os
import signal
import subprocess
from typing import Callable, Optional, Tuple, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


# ── Input schema ───────────────────────────────────────────────────────────────

class OsExecInput(BaseModel):
    command: str = Field(..., description="The shell command to run (e.g. 'whoami', 'nmap 127.0.0.1').")
    timeout: int = Field(60, description="Max execution time in seconds. Defaults to 60.")


# ── Process runner ─────────────────────────────────────────────────────────────

class AsyncProcessRunner:
    """Manages execution of shell commands with real-time streaming and cancellation."""

    def __init__(self) -> None:
        self._current_process: Optional[asyncio.subprocess.Process] = None
        self._cancelled: bool = False
        self._lock = asyncio.Lock()  # FIX-1: serialise concurrent calls

    async def execute(
        self,
        command: str,
        output_callback: Optional[Callable[[str, str], None]] = None,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,  # FIX-25
    ) -> Tuple[int, str, str]:

        async with self._lock:  # FIX-1
            return await self._execute_locked(command, output_callback, timeout, cwd)

    async def _execute_locked(
        self,
        command: str,
        output_callback: Optional[Callable[[str, str], None]],
        timeout: Optional[int],
        cwd: Optional[str] = None,  # FIX-25
    ) -> Tuple[int, str, str]:
        self._cancelled = False
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        # Unix: start_new_session=True creates a new process group so the
        # entire subprocess tree can be killed with os.killpg (FIX-2).
        # Windows: CREATE_NEW_PROCESS_GROUP + taskkill /T achieves the same.
        spawn_kwargs: dict = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )

        # FIX-25: explicit cwd instead of relying on process-wide os.chdir().
        self._current_process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            **spawn_kwargs,
        )

        async def _read_stream(
            stream: asyncio.StreamReader,
            stream_name: str,
            storage: list[str],
        ) -> None:
            while True:
                line_bytes = await stream.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace")
                storage.append(line)
                if output_callback:
                    output_callback(line, stream_name)

        exit_code: int = -1
        try:
            read_tasks = asyncio.gather(
                _read_stream(self._current_process.stdout, "stdout", stdout_lines),
                _read_stream(self._current_process.stderr, "stderr", stderr_lines),
            )

            if timeout:
                await asyncio.wait_for(read_tasks, timeout=timeout)
            else:
                await read_tasks

            await self._current_process.wait()
            exit_code = self._current_process.returncode or 0

        except asyncio.TimeoutError:
            await self.cancel()
            stderr_lines.append(f"\n[!] Command timed out after {timeout}s.")
            exit_code = -1

        except asyncio.CancelledError:
            await self.cancel()
            stderr_lines.append("\n[!] Execution interrupted.")
            exit_code = -2

        finally:
            self._current_process = None

        return exit_code, "".join(stdout_lines), "".join(stderr_lines)

    async def cancel(self) -> None:
        """Terminate the active process tree."""
        proc = self._current_process
        if proc is None or proc.returncode is not None:
            return

        self._cancelled = True
        pid = proc.pid

        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_shell(
                    f"taskkill /F /T /PID {pid}",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            except Exception:
                proc.kill()
                await proc.wait()  # FIX-3: reap zombie
        else:
            # FIX-2: kill the entire process group, not just the parent
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGINT)   # graceful first
                await asyncio.sleep(0.5)
                if proc.returncode is None:
                    os.killpg(pgid, signal.SIGKILL)  # force if still alive
            except (ProcessLookupError, PermissionError):
                pass

            # FIX-3: reap so the OS doesn't leave a zombie
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass  # process already gone or unkillable — move on


# ── Module-level singleton ─────────────────────────────────────────────────────
# Default runner — used by CLI/TUI/one-shot/A2A sub-agent threads, none of
# which call set_session_runner(). (FIX-26)
_runner = AsyncProcessRunner()

# Per-session override, set by web/server.py once per WebSocket connection.
_session_runner_var: "contextvars.ContextVar[Optional[AsyncProcessRunner]]" = contextvars.ContextVar(
    "beaver_session_os_exec_runner", default=None
)


def _active_runner() -> AsyncProcessRunner:
    """Return this context's AsyncProcessRunner, or the shared default (FIX-26)."""
    return _session_runner_var.get() or _runner


def set_session_runner(runner: Optional[AsyncProcessRunner]) -> "contextvars.Token":
    """Bind *runner* as the active AsyncProcessRunner for the current context.

    Call once per WebSocket connection, before spawning that connection's
    turn tasks — same pattern as agent.config.set_session_config(). Give each
    call a fresh AsyncProcessRunner() so sessions don't serialize behind one
    shared lock or race on _current_process for cancellation.
    """
    return _session_runner_var.set(runner)


def _format_output(exit_code: int, stdout: str, stderr: str) -> str:
    # [FIX-24] Windows reports process exit codes as an unsigned DWORD, so a
    # process that exits with a negative/error code (commonly -1) surfaces
    # here as 4294967295 (0xFFFFFFFF) instead of -1 — technically correct
    # but unreadable and misleading to both the user and the model deciding
    # whether/how to retry. Convert back to the signed 32-bit value for
    # display only; the raw value elsewhere (failure detection) is
    # unaffected since that only checks "is this zero or not."
    display_code = exit_code
    if isinstance(display_code, int) and display_code >= 2**31:
        display_code -= 2**32

    parts: list[str] = []
    if stdout.strip():
        parts.append(f"--- STDOUT ---\n{stdout.strip()}")
    if stderr.strip():
        parts.append(f"--- STDERR ---\n{stderr.strip()}")
    parts.append(f"\n[Exit code: {display_code}]")
    return "\n".join(parts)


# ── Tool class (FIX-4) ─────────────────────────────────────────────────────────

class OsExecTool(BaseTool):
    """Execute a shell command and return its output.

    Exposes astream_invoke() so graph.py can inject the tui_bus callback
    for live output streaming without any hardcoded tool-name branches.
    """

    name: str = "os_exec"
    description: str = (
        "Execute a shell command on the local OS and return its output. "
        "Args: command (str), timeout (int, default 60)."
    )
    args_schema: Type[BaseModel] = OsExecInput

    def _run(self, command: str, timeout: int = 60) -> str:
        """Sync entry point — not used; agent always calls _arun."""
        raise NotImplementedError("os_exec requires an async runtime. Use ainvoke().")

    async def _arun(self, command: str, timeout: int = 60) -> str:
        """Standard async invocation (no streaming)."""
        from skills.file_ops import get_active_workspace_root  # FIX-25
        exit_code, stdout, stderr = await _active_runner().execute(
            command, timeout=timeout, cwd=get_active_workspace_root()
        )
        return _format_output(exit_code, stdout, stderr)

    async def astream_invoke(
        self,
        args: dict,
        output_callback: Optional[Callable[[str, str], None]] = None,
    ) -> str:
        """Streaming invocation called by graph.execute_tools when tui_bus is live.

        graph.py checks hasattr(tool, 'astream_invoke') and calls this instead
        of ainvoke() so every output line is pushed to the TUI in real time.
        """
        from skills.file_ops import get_active_workspace_root  # FIX-25
        command: str = args.get("command", "")
        timeout: int = args.get("timeout", 60)
        exit_code, stdout, stderr = await _active_runner().execute(
            command,
            output_callback=output_callback,
            timeout=timeout,
            cwd=get_active_workspace_root(),
        )
        return _format_output(exit_code, stdout, stderr)


# ── Exported singleton instance ────────────────────────────────────────────────
os_exec = OsExecTool()


async def cancel_active_process() -> None:
    """Async-safe cancellation. Await this from an async context (e.g. TUI keybind).

    FIX-26: cancels the *active* (session or default) runner's process, not
    always the shared module singleton — so Stop in one WebSocket session
    can't kill another session's in-flight command.
    """
    await _active_runner().cancel()