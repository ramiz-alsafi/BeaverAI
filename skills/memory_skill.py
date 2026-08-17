"""
Long-Term Memory Tools for Beaver Agent

Fixes applied (original):
- Replaced module-level `memory_db` import (deleted singleton) with
  `get_memory_db()` lazy factory.
- Both tools are now `async` — required because store_memory() and
  search_similar() are async (they call httpx for embeddings).
- Tools are exported for registration in skills/__init__.py.

Fix log
-------
[FIX-1] Wrapped both tool bodies in try/except so a Postgres outage,
        embedding failure, or schema mismatch returns a graceful error
        string to the LLM instead of crashing the graph turn.

[FIX-2] Replaced r['similarity'] with r.get('similarity', 0.0) to avoid
        KeyError if the returned memory dict is missing that field
        (e.g. after a schema migration or DB version mismatch).
"""
from langchain_core.tools import tool

from memory.long_term import get_memory_db


@tool
async def store_long_term_memory(fact: str, category: str = "general") -> str:
    """Store an important fact, preference, config, or state into long-term
    semantic vector memory so it can be recalled in future sessions.

    Args:
        fact:     The text to store (be specific and self-contained).
        category: Optional label e.g. 'network', 'preference', 'credential'.
    """
    try:  # FIX-1
        success = await get_memory_db().store_memory(content=fact, category=category)
    except Exception as exc:
        return f"[-] Error: Could not reach vector database — {exc}"

    if success:
        return f"[+] Saved to vector memory: '{fact}'"
    return "[-] Error: Failed to commit to vector database."


@tool
async def recall_relevant_memory(query: str) -> str:
    """Search long-term semantic memory for facts, configs, or instructions
    relevant to the query. Call this at the start of any task that may have
    been discussed in a previous session.

    Args:
        query: Natural language description of what to look for.
    """
    try:  # FIX-1
        results = await get_memory_db().search_similar(query=query, limit=3)
    except Exception as exc:
        return f"[-] Error: Memory search failed — {exc}"

    if not results:
        return "No relevant memories found."

    lines = [
        f"[{r.get('category', 'unknown')}] "           # FIX-2
        f"(score: {r.get('similarity', 0.0):.2f}) "    # FIX-2
        f"{r.get('content', '')}"
        for r in results
    ]
    return "\n".join(lines)


@tool
async def list_long_term_memories(limit: int = 20, category: str = "") -> str:
    """List stored long-term memories directly, without a similarity query.

    recall_relevant_memory only surfaces entries that match a specific
    query — this is the only way to actually browse/audit what's in
    memory, e.g. before deciding something is stale or wrong and should be
    removed with forget_long_term_memory. Each row's id is what that tool
    needs.

    Args:
        limit:    Max entries to return (default 20).
        category: Optional exact category filter, e.g. 'network', 'preference'.
                  Leave empty to list across all categories.
    """
    try:
        results = await get_memory_db().list_memories(limit=limit, category=category)
    except Exception as exc:
        return f"[-] Error: Memory list failed — {exc}"

    if not results:
        scope = f" in category '{category}'" if category else ""
        return f"No memories found{scope}."

    lines = [f"{len(results)} memor{'y' if len(results) == 1 else 'ies'} found:\n"]
    for r in results:
        lines.append(f"  id={r['id']}  [{r['category']}]  {r['content']}")
    return "\n".join(lines)


@tool
async def forget_long_term_memory(memory_id: str) -> str:
    """Permanently delete one long-term memory by its id.

    Get the id from list_long_term_memories first — ids aren't
    human-guessable, this isn't for deleting "the memory about X" by
    description, it's for removing a specific entry you've already
    identified as stale, wrong, or no longer relevant.

    Args:
        memory_id: The exact id shown by list_long_term_memories.
    """
    if not memory_id.strip():
        return "[-] Error: memory_id must not be empty — get it from list_long_term_memories first."
    try:
        deleted = await get_memory_db().delete_memory(memory_id.strip())
    except Exception as exc:
        return f"[-] Error: Memory delete failed — {exc}"

    if deleted:
        return f"[+] Deleted memory {memory_id}"
    return f"[-] No memory found with id {memory_id} — it may already be deleted, or the id is wrong. Use list_long_term_memories to check."


@tool
async def reflect_and_store_lesson(task_summary: str, what_happened: str, lesson: str) -> str:
    """Store a self-reflection: a lesson learned from how a task actually
    went, so a similar task in a future session starts smarter instead of
    repeating the same mistake or rediscovering the same workaround.

    This is deliberately separate from store_long_term_memory — every
    reflection is saved under category="lesson" specifically so it can be
    browsed on its own later (list_long_term_memories(category="lesson"))
    without being mixed into general facts/preferences.

    Call this when a task involved a real mistake, a non-obvious fix, a
    dead end that cost real effort, or a surprising result — not after
    every routine task. If nothing about how the task went would change
    your approach next time, there's nothing to reflect on, don't call this
    just to have called it.

    Args:
        task_summary  : One sentence — what the task actually was.
        what_happened : What went wrong or was surprising — the mistake,
                        the dead end, the wrong assumption. Be specific
                        enough that future-you recognizes the situation.
        lesson        : The concrete takeaway — what to do differently, or
                        what to check first, next time a similar task comes up.
    """
    if not task_summary.strip() or not lesson.strip():
        return "[-] Error: task_summary and lesson must not be empty."

    entry = (
        f"[reflection] Task: {task_summary.strip()} | "
        f"What happened: {what_happened.strip()} | "
        f"Lesson: {lesson.strip()}"
    )
    try:
        success = await get_memory_db().store_memory(content=entry, category="lesson")
    except Exception as exc:
        return f"[-] Error: Could not reach vector database — {exc}"

    if success:
        return f"[+] Reflection saved: '{lesson.strip()}'"
    return "[-] Error: Failed to commit reflection to vector database."