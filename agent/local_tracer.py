import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Timestamp helper ──────────────────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── LocalAgentTracer ──────────────────────────────────────────────────────────

class LocalAgentTracer(AsyncCallbackHandler):
    """
    Writes every LLM call and tool invocation to beaver_traces.log.
    Implements LangChain's AsyncCallbackHandler so it plugs directly
    into graph.py's get_callbacks() list.

    No external services — all output goes to local files only.
    """

    def __init__(self, log_file: Optional[str] = None) -> None:
        self.log_file = log_file or str(_PROJECT_ROOT / "beaver_traces.log")
        self.step_times: Dict[str, float] = {}
        # run_id → span metadata (kept for timing only)
        self._spans: Dict[str, Dict[str, Any]] = {}
        self._spans_lock = threading.Lock()

        self._current_trace_id: Optional[str] = None
        self._current_session_id: Optional[str] = None

        self._log_handle = open(self.log_file, "a", encoding="utf-8", buffering=1)

    def __del__(self) -> None:
        try:
            self._log_handle.close()
        except Exception:
            pass

    # ── Run context ────────────────────────────────────────────────────────────

    async def set_run_context(
        self,
        trace_id: str,
        session_id: str,
        user_input: str = "",
    ) -> None:
        """Register a trace ID for the upcoming agent run."""
        self._current_trace_id = trace_id
        self._current_session_id = session_id
        self._write_log(
            f"\n=== [RUN START] trace_id={trace_id}  session={session_id} ===\n"
            f"    input: {user_input[:200]}"
        )

    # ── Chat model hooks ───────────────────────────────────────────────────────

    async def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[Any]],
        **kwargs: Any,
    ) -> None:
        run_id = str(kwargs.get("run_id", ""))
        model_name = serialized.get("name", "unknown")
        self.step_times[run_id] = time.time()

        flat_msgs = messages[0] if messages else []
        msg_summary = ", ".join(
            f"{getattr(m, 'type', '?')}({len(str(getattr(m, 'content', '')))} chars)"
            for m in flat_msgs
        )
        self._write_log(
            f"\n--- [LLM START] Model: {model_name} | Messages: [{msg_summary}] ---"
        )
        # [FIX-15] Log real content of the last 3 messages for debugging
        for m in flat_msgs[-3:]:
            role = getattr(m, "type", "?")
            content = getattr(m, "content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            self._write_log(f"    [{role}] {str(content)[:600]}")

        with self._spans_lock:
            self._spans[run_id] = {
                "model": model_name,
                "start": _utcnow(),
                "trace_id": self._current_trace_id or str(uuid.uuid4()),
            }

    async def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        **kwargs: Any,
    ) -> None:
        """Fallback for plain (non-chat) LLMs."""
        run_id = str(kwargs.get("run_id", ""))
        model_name = serialized.get("name", "unknown")
        self.step_times[run_id] = time.time()
        self._write_log(f"\n--- [LLM START] Model: {model_name} (non-chat) ---")

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        run_id = str(kwargs.get("run_id", ""))
        duration = round(time.time() - self.step_times.pop(run_id, time.time()), 2)

        output_text: Any = ""
        tool_calls_out: List[Dict] = []
        try:
            gen = response.generations[0][0]
            output_text = gen.text or ""
            if not output_text:
                msg = getattr(gen, "message", None)
                if msg:
                    raw_tcs = getattr(msg, "tool_calls", None) or []
                    if raw_tcs:
                        tool_calls_out = [
                            {"name": tc.get("name"), "args": tc.get("args")}
                            for tc in raw_tcs
                        ]
        except (IndexError, AttributeError):
            output_text = ""

        if tool_calls_out:
            summary = ", ".join(
                f"{tc['name']}({str(tc.get('args', ''))[:300]})"
                for tc in tool_calls_out
            )
            self._write_log(f"--- [LLM END] Duration: {duration}s | Tool calls: {summary} ---")
        else:
            self._write_log(
                f"--- [LLM END] Duration: {duration}s ---\n"
                f"Output: {str(output_text)[:1000]}\n"
            )

        usage = _extract_usage(response)
        if usage:
            self._write_log(
                f"    Tokens: input={usage['input']} output={usage['output']} "
                f"total={usage['total']}"
            )

        with self._spans_lock:
            self._spans.pop(run_id, None)

    async def on_llm_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        **kwargs: Any,
    ) -> None:
        run_id = str(kwargs.get("run_id", ""))
        self._write_log(f"--- [LLM ERROR] {type(error).__name__}: {error} ---")
        with self._spans_lock:
            self._spans.pop(run_id, None)

    # ── Tool hooks ─────────────────────────────────────────────────────────────

    async def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        run_id = str(kwargs.get("run_id", ""))
        tool_name = serialized.get("name", "unknown")
        key = f"tool:{run_id}"
        self.step_times[key] = time.time()
        self._write_log(
            f"--- [TOOL START] Function: {tool_name} | Args: {input_str[:800]} ---"
        )

    async def on_tool_end(self, output: str, **kwargs: Any) -> None:
        run_id = str(kwargs.get("run_id", ""))
        key = f"tool:{run_id}"
        duration = round(time.time() - self.step_times.pop(key, time.time()), 2)
        # [FIX-15] Log actual output content, not just byte length
        # [FIX-22] `output` isn't always a str — MCP tool results arrive as a
        # list of content blocks (e.g. [{'type': 'text', 'text': '...'}]).
        # len() on that gives the element count (almost always 1), not a
        # byte length, which is why every MCP tool call logged "Output (1
        # bytes)" regardless of actual size. Stringify first, then measure.
        output_str = str(output)
        self._write_log(
            f"--- [TOOL END] Duration: {duration}s | Output ({len(output_str)} bytes): "
            f"{output_str[:800]} ---\n"
        )

    async def on_tool_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        **kwargs: Any,
    ) -> None:
        run_id = str(kwargs.get("run_id", ""))
        key = f"tool:{run_id}"
        self.step_times.pop(key, None)
        self._write_log(f"--- [TOOL ERROR] {type(error).__name__}: {error} ---")

    # ── Chain hooks ────────────────────────────────────────────────────────────

    async def on_chain_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        **kwargs: Any,
    ) -> None:
        self._write_log(f"--- [CHAIN ERROR] {type(error).__name__}: {error} ---")

    # ── Internal ───────────────────────────────────────────────────────────────

    def _write_log(self, text: str) -> None:
        """Append a timestamped line to the trace log. Never raises."""
        try:
            self._log_handle.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")
        except OSError:
            pass


# ── Token-usage helper ────────────────────────────────────────────────────────

def _extract_usage(response: LLMResult) -> Optional[Dict[str, int]]:
    """Return token counts or None. Handles Ollama and OpenAI-style keys."""
    llm_out = response.llm_output or {}

    raw = llm_out.get("token_usage") or llm_out.get("usage") or {}
    if raw:
        return {
            "input":  raw.get("prompt_tokens")     or raw.get("input_tokens", 0),
            "output": raw.get("completion_tokens")  or raw.get("output_tokens", 0),
            "total":  raw.get("total_tokens", 0),
        }

    prompt_eval = llm_out.get("prompt_eval_count")
    eval_count  = llm_out.get("eval_count")
    if prompt_eval is not None or eval_count is not None:
        inp = prompt_eval or 0
        out = eval_count  or 0
        return {"input": inp, "output": out, "total": inp + out}

    return None