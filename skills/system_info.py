"""
System Info Tools for Beaver Agent

Fixes applied:
- disk_usage("/") crashes on Windows — now uses platform-appropriate path.
- get_system_metrics() explicitly reports the shell type (cmd/powershell vs
  bash/sh) so the LLM stops guessing and picks the right command syntax.
"""
import os
import platform

import psutil
from langchain_core.tools import tool


def _disk_root() -> str:
    """Return the appropriate disk root for the current OS."""
    if platform.system() == "Windows":
        # Use SystemDrive env var (usually C:) — fallback to C:\
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


@tool
def get_system_metrics() -> str:
    """Retrieve host machine state: OS, shell type, CPU, RAM, network information, and disk usage.

    Always call this at the start of a session so you know which shell
    commands to use (Windows cmd/PowerShell vs Linux bash).
    """
    try:
        mem = psutil.virtual_memory()
        disk_path = _disk_root()
        disk = psutil.disk_usage(disk_path)

        os_name = platform.system()
        shell_hint = (
            "Windows — use PowerShell or cmd syntax (ipconfig, dir, etc.)"
            if os_name == "Windows"
            else f"{os_name} — use bash/sh syntax (ip addr, ls, etc.)"
        )

        info = [
            f"OS: {os_name} {platform.release()} ({platform.architecture()[0]})",
            f"Shell: {shell_hint}",
            f"Host Node: {platform.node()}",
            f"CPU Usage: {psutil.cpu_percent(interval=0.5)}%",
            f"CPU Cores: {psutil.cpu_count(logical=False)} physical / "
            f"{psutil.cpu_count(logical=True)} logical",
            f"RAM: {mem.percent}% used "
            f"({mem.used // (1024**2)} MB / {mem.total // (1024**2)} MB)",
            f"Disk ({disk_path}): {disk.percent}% used "
            f"({disk.used // (1024**3)} GB / {disk.total // (1024**3)} GB)",
        ]
        return "\n".join(info)
    except Exception as e:
        return f"ERROR: Failed to fetch system metrics: {e}"


@tool
def get_environment_vars() -> str:
    """Retrieve non-sensitive environment variables active in the host session."""
    try:
        blocked = {"PASSWORD", "SECRET", "KEY", "TOKEN", "AUTH"}
        lines = [
            f"{k}={v}"
            for k, v in os.environ.items()
            if not any(sub in k.upper() for sub in blocked)
        ]
        return "\n".join(lines[:30])
    except Exception as e:
        return f"ERROR: Failed to fetch environment variables: {e}"