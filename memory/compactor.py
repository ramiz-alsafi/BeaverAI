
from typing import List, Optional, Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)

from agent.config import runtime_config

# Max token/message threshold defaults for local LLMs.
# graph.py always passes max_messages explicitly — this default
# is here for direct/test callers only.
DEFAULT_MAX_MESSAGES = 12

# Truncate giant terminal outputs (e.g. full nmap scans) to preserve tokens.
# Sourced from runtime_config so AGENT_MAX_OUTPUT_CHARS in .env is respected.
MAX_TOOL_OUTPUT_CHARS = runtime_config.max_output_chars

# Cap for a single HumanMessage — ~1 500 tokens, leaves room for system
# prompt + history within an 8 192-token context window.
MAX_HUMAN_INPUT_CHARS = 6_000

# [FIX-CTX-BUDGET] Rough chars-per-token ratio used for the cheap token
# estimate below. Not a real tokenizer — just consistent with the ratio
# this file already assumed for MAX_HUMAN_INPUT_CHARS (6 000 chars ≈ 1 500
# tokens). Good enough to keep total context safely under budget; it
# doesn't need to be exact, only conservative.
_CHARS_PER_TOKEN = 4


def trim_human_input(message: BaseMessage) -> BaseMessage:
    """Cap excessively large human messages to prevent context overflow.

    A 50 000-char paste fills the entire 8 192-token context window before
    any system prompt or tool results are appended; Ollama then silently
    truncates the tail and the model never reads the end of the task.
    """
    if not isinstance(message, HumanMessage):
        return message
    if not isinstance(message.content, str):
        return message
    if len(message.content) <= MAX_HUMAN_INPUT_CHARS:
        return message
    truncated = (
        message.content[:MAX_HUMAN_INPUT_CHARS]
        + f"\n... [INPUT TRUNCATED — {len(message.content) - MAX_HUMAN_INPUT_CHARS} chars omitted] ..."
    )
    return message.model_copy(update={"content": truncated})


def trim_tool_output(message: BaseMessage) -> BaseMessage:
    """Truncate excessively large tool outputs to save context tokens.

    Uses model_copy() so all message fields (id, additional_kwargs, etc.)
    are preserved — only `content` is updated.
    """
    if not isinstance(message, ToolMessage):
        return message
    if not isinstance(message.content, str):
        return message
    if len(message.content) <= MAX_TOOL_OUTPUT_CHARS:
        return message

    truncated = (
        message.content[:MAX_TOOL_OUTPUT_CHARS]
        + f"\n... [TRUNCATED {len(message.content) - MAX_TOOL_OUTPUT_CHARS} CHARS] ..."
    )
    return message.model_copy(update={"content": truncated})  # FIX-1


def _estimate_tokens(message: BaseMessage) -> int:
    """Cheap, tokenizer-free size estimate for one message. [FIX-CTX-BUDGET]

    Flattens multimodal content (list of dicts) to a plain string first,
    same handling as graph.py's _embedding_query, so a multimodal message
    doesn't get badly under/over-estimated from stringifying the raw list.
    """
    content = message.content
    if isinstance(content, list):
        content = " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    return len(str(content)) // _CHARS_PER_TOKEN


def _apply_window_invariants(window: List[BaseMessage]) -> List[BaseMessage]:
    """Enforce the two structural invariants compact_messages() promises.

    Shared by both trim passes (message-count and, when requested,
    token-budget) since either one can newly expose an orphaned leading
    ToolMessage or a trailing tool_calls AIMessage at the boundary it cuts.
    """
    # No leading ToolMessage (its parent AIMessage was trimmed — drop it).
    while window and isinstance(window[0], ToolMessage):
        window.pop(0)
    # No trailing AIMessage with unresolved tool_calls (FIX-2) — its
    # ToolMessage responses were trimmed, so sending it to the LLM would
    # either error on the broken sequence or hallucinate the missing result.
    while (
        window
        and isinstance(window[-1], AIMessage)
        and getattr(window[-1], "tool_calls", None)
    ):
        window.pop()
    return window


def compact_messages(
    messages: Sequence[BaseMessage],
    max_messages: int = DEFAULT_MAX_MESSAGES,
    max_tokens: Optional[int] = None,
) -> List[BaseMessage]:
    """Prune conversation history to stay within context limits.

    Callers (graph.py) are responsible for stripping SystemMessages before
    calling this function.  compact_messages() treats all input messages
    equally and never reorders them.

    Parameters
    ----------
    max_messages : hard cap on how many messages survive, regardless of size.
    max_tokens : optional [FIX-CTX-BUDGET] second trim pass. When given,
        the oldest surviving messages are dropped (after the max_messages
        window is applied) until the estimated total fits the budget. Pass
        the ACTUAL remaining context budget (num_ctx minus system prompt
        size minus a generation-headroom reserve) — this function has no
        notion of num_ctx itself, it only trims to whatever number it's
        given. None (default) disables this pass entirely — pure
        message-count windowing, the original behavior.

    Invariants preserved
    --------------------
    - No leading ToolMessage (its parent AIMessage was trimmed — drop it).
    - No trailing AIMessage with unresolved tool_calls (its ToolMessage
      responses were trimmed — drop it to avoid LLM sequence errors).
    - All ToolMessage fields (id, additional_kwargs) are preserved when
      tool outputs are truncated.
    - Both invariants above are re-checked after EITHER trim pass, since
      dropping from the front on the token pass can newly expose a leading
      ToolMessage that the message-count pass never touched.
    """
    if not messages:
        return []

    # 1. Truncate oversized inputs/outputs across all messages.
    sanitized: List[BaseMessage] = [trim_tool_output(trim_human_input(m)) for m in messages]

    # 2. Take the most recent `max_messages` messages (no-op if already
    #    within the window).
    window: List[BaseMessage] = (
        sanitized if len(sanitized) <= max_messages else list(sanitized[-max_messages:])
    )
    window = _apply_window_invariants(window)

    # 3. [FIX-CTX-BUDGET] Token-budget pass — independent of whether the
    #    count-based window above actually trimmed anything, since even a
    #    handful of large messages within max_messages can exceed a real
    #    token budget. Drops the oldest surviving message repeatedly,
    #    re-applying invariants each time, until the total estimate fits
    #    (or only one message is left — never trim down to nothing here;
    #    an over-budget single message is a problem for the caller/model,
    #    not something this function can fix by returning an empty list).
    if max_tokens is not None:
        while len(window) > 1 and sum(_estimate_tokens(m) for m in window) > max_tokens:
            window.pop(0)
            window = _apply_window_invariants(window)

    return window