from typing import Annotated, Any, Dict, Sequence

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


# ── Reducers ───────────────────────────────────────────────────────────────────

def _iteration_reducer(current: int, update: int) -> int:  # FIX-2
    """Accept the incoming iteration value.

    Always use the *incoming* update so that ui.py passing ``iteration=0``
    at the start of each ainvoke() resets the counter even when the
    checkpointer has persisted a stale high value from a previous session
    that hit max_loops.  Using max() here caused every new conversation to
    immediately trigger the "Reached max loop limit" halt.
    """
    return update if update is not None else (current or 0)


# ── State schema ───────────────────────────────────────────────────────────────

class AgentState(Dict[str, Any]):
    """Total state carried through the Beaver LangGraph workflow.

    Fields
    ------
    messages : Sequence[BaseMessage]
        Full conversation history.  ``add_messages`` reducer appends
        new messages rather than replacing the list.
    active_persona : str
        Which persona is active: "standard" | "pentester" | "coder" | "seo" | "social".
    target_scope : list[str]
        IP ranges / hosts authorised for the pentester persona.
    iteration : int
        Incremented each reasoning loop; capped at runtime_config.max_loops.
    """

    messages: Annotated[Sequence[BaseMessage], add_messages]
    active_persona: str
    target_scope: list[str]
    iteration: Annotated[int, _iteration_reducer]
    failed_tool_calls: Dict[str, int]  # {call_fingerprint: failure_count} — repeat-fail guard
    consecutive_tool_failures: int  # ANY tool failing in a row, regardless of args — catches
    # a "varied but equally stuck" pattern that failed_tool_calls' exact-fingerprint match
    # can't see (different SQLi payloads that all fail, different unreachable IPs, etc.).
    # Reset to 0 on any tool success; incremented on any tool failure. See execute_tools().


def default_state(persona: str = "standard") -> AgentState:
    """Return a fully-initialised AgentState with safe defaults.

    Always pass this as the base when calling graph.invoke() so no node
    ever hits a KeyError on a missing state field:

        state = default_state(persona="pentester")
        state["target_scope"] = ["10.10.10.5"]
        result = graph.invoke(state)
    """
    return AgentState(
        messages=[],
        active_persona=persona,
        target_scope=["127.0.0.1"],
        iteration=0,
        failed_tool_calls={},
        consecutive_tool_failures=0,
    )