import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Logging setup ──────────────────────────────────────────────────────────────

def setup_telemetry() -> logging.Logger:
    """Configure system-wide logging for agent execution and tool output."""
    logger = logging.getLogger("beaver")
    logger.setLevel(logging.DEBUG)

    # stderr keeps Rich/Textual TUI stdout clean
    c_handler = logging.StreamHandler(sys.stderr)
    c_handler.setLevel(logging.INFO)

    f_handler = logging.FileHandler(
        str(_PROJECT_ROOT / "agent.log"), mode="a", encoding="utf-8"
    )
    f_handler.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    c_handler.setFormatter(formatter)
    f_handler.setFormatter(formatter)

    if not logger.handlers:
        logger.addHandler(c_handler)
        logger.addHandler(f_handler)

    return logger


logger = setup_telemetry()


# ── Callback factory ───────────────────────────────────────────────────────────

_callbacks_cache: list | None = None


def get_callbacks() -> list:
    """
    Return a singleton callback list for injection into LLM invocations.
    Always contains LocalAgentTracer (local file tracing only).
    """
    global _callbacks_cache
    if _callbacks_cache is not None:
        return _callbacks_cache

    from agent.local_tracer import LocalAgentTracer

    _callbacks_cache = [
        LocalAgentTracer(log_file=str(_PROJECT_ROOT / "beaver_traces.log"))
    ]

    logger.info("[TELEMETRY] Local tracing active → beaver_traces.log")
    return _callbacks_cache
