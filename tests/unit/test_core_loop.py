"""Core agent loop — agent.graph.call_model / execute_tools / the compiled graph.

Everything here runs against a SCRIPTED FAKE MODEL: no Ollama, no network, no
ChromaDB. The fake returns pre-written responses in order and records every
prompt it was shown, so the tests can assert both *what the loop did* and
*what it asked the model*.

What is covered (each section below):

  1. Narration / blank-response retry inside call_model
  2. Hard stops: max_loops and the consecutive-failure streak (halt at 6)
  3. FIX-ROUTER-2: client-construction / ainvoke failures end the turn cleanly
  4. The stuck-streak warning (at 3) and its bookkeeping in execute_tools
  5. The same behaviours end to end through the real compiled graph

Tool calls reach the loop as JSON TEXT in the model's reply (the project does
not use bind_tools), so the fake emits ``{"tool": "...", "args": {...}}``.
"""
import asyncio
import json
from typing import Any, List

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END

import agent.graph as graph
from agent.config import runtime_config

THREAD_CFG = {"configurable": {"thread_id": "core-loop-test"}}


# ── Scripted fake model ──────────────────────────────────────────────────────

class ScriptedModel:
    """Stands in for the object _get_cached_llm() returns.

    ``script`` items are consumed one per ainvoke() call:
      * an AIMessage / str  -> returned (a str is wrapped in an AIMessage)
      * an Exception        -> raised
    Running off the end of the script raises AssertionError, so a loop that
    calls the model more often than the test expected fails loudly instead of
    hanging or silently looping.
    """

    def __init__(self, script: List[Any]):
        self.script = list(script)
        self.calls: List[list] = []  # the message list passed to each call

    async def ainvoke(self, messages, config=None, **_):
        self.calls.append(list(messages))
        if not self.script:
            raise AssertionError(
                f"model called {len(self.calls)} time(s) but the script only had "
                f"{len(self.calls) - 1} response(s)"
            )
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return AIMessage(content=item) if isinstance(item, str) else item


def tool_call_text(name: str, **args) -> str:
    """The text a model writes to call a tool."""
    return json.dumps({"tool": name, "args": args})


# ── Stand-in tools ───────────────────────────────────────────────────────────

@tool
def good_tool(n: int) -> str:
    """Always succeeds."""
    return f"ok {n}"


@tool
def bad_tool(n: int) -> str:
    """Always fails (the 'Error:' prefix is what _tool_result_failed looks for)."""
    return f"Error: boom {n}"


FAKE_TOOLS = [good_tool, bad_tool]


# ── Fixtures ─────────────────────────────────────────────────────────────────

class Env:
    """Handle returned by the `env` fixture."""

    def __init__(self):
        self.model: ScriptedModel = ScriptedModel([])
        self.lessons: List[str] = []   # reasons passed to _auto_store_halt_lesson
        self.evictions = 0

    def script(self, *items) -> ScriptedModel:
        self.model = ScriptedModel(list(items))
        return self.model


@pytest.fixture
def env(monkeypatch):
    """Patch every external dependency of the loop; return a handle to drive it."""
    e = Env()

    monkeypatch.setattr(graph, "_get_cached_llm", lambda: e.model)

    async def fake_tools(_persona):
        return FAKE_TOOLS

    monkeypatch.setattr(graph, "get_persona_tools_async", fake_tools)

    async def passthrough(system_text, _query):  # no ChromaDB
        return system_text

    monkeypatch.setattr(graph, "_inject_long_term_memories", passthrough)

    async def record_lesson(_state, reason):  # no ChromaDB / embeddings
        e.lessons.append(reason)

    monkeypatch.setattr(graph, "_auto_store_halt_lesson", record_lesson)

    def count_eviction():
        e.evictions += 1

    monkeypatch.setattr(graph, "_evict_cached_llm", count_eviction)

    # Keep tests independent of a developer's .env
    monkeypatch.setattr(runtime_config, "max_loops", 16)
    return e


def make_state(text="do the thing", **over):
    state = {
        "messages": [HumanMessage(content=text)],
        "active_persona": "standard",
        "target_scope": ["127.0.0.1"],
        "iteration": 0,
        "failed_tool_calls": {},
        "consecutive_tool_failures": 0,
    }
    state.update(over)
    return state


def run_call_model(state):
    return asyncio.run(graph.call_model(state, THREAD_CFG))


def run_execute_tools(state):
    return asyncio.run(graph.execute_tools(state, THREAD_CFG))


def last_text(out) -> str:
    return out["messages"][-1].content


# ═════════════════════════════════════════════════════════════════════════════
# 1. Narration / blank-response retry
# ═════════════════════════════════════════════════════════════════════════════

def test_plain_final_answer_is_one_call_no_retry(env):
    model = env.script("The capital of France is Paris.")
    out = run_call_model(make_state("capital of France?"))
    assert len(model.calls) == 1
    assert last_text(out) == "The capital of France is Paris."
    assert out["iteration"] == 1
    assert env.lessons == []


def test_json_text_becomes_a_real_tool_call(env):
    env.script(tool_call_text("good_tool", n=7))
    out = run_call_model(make_state())
    msg = out["messages"][0]
    assert msg.tool_calls[0]["name"] == "good_tool"
    assert msg.tool_calls[0]["args"] == {"n": 7}


def test_narration_then_tool_call_retries_once_and_succeeds(env):
    model = env.script(
        "I'll run a query to check the users table.",   # narration
        tool_call_text("good_tool", n=1),                # the retry obeys
    )
    out = run_call_model(make_state())
    assert len(model.calls) == 2
    assert out["messages"][0].tool_calls[0]["name"] == "good_tool"
    assert env.lessons == []  # recovered -> no halt lesson

    # The retry prompt = original prompt + the rejected reply + enforcement.
    retry = model.calls[1]
    assert retry[-2].content == "I'll run a query to check the users table."
    assert isinstance(retry[-1], HumanMessage)
    assert retry[-1].content.startswith("TOOL CALL REQUIRED")
    assert "You wrote text or code" in retry[-1].content


def test_blank_then_tool_call_uses_blank_specific_enforcement(env):
    model = env.script("", tool_call_text("good_tool", n=1))
    out = run_call_model(make_state())
    assert len(model.calls) == 2
    assert out["messages"][0].tool_calls
    assert "You returned an empty response" in model.calls[1][-1].content


def test_whitespace_only_response_counts_as_blank(env):
    model = env.script("  \n\t ", tool_call_text("good_tool", n=1))
    run_call_model(make_state())
    assert len(model.calls) == 2


def test_narration_twice_gives_explicit_failure_after_exactly_two_calls(env):
    model = env.script(
        "First, I will run nmap against the host.",
        "Let me check the ports with a scan.",
        "this third reply must never be requested",
    )
    out = run_call_model(make_state())
    assert len(model.calls) == 2, "retry must happen exactly once, never twice"
    assert last_text(out).startswith("[AGENT] I wasn't able to produce a valid response")
    assert out["messages"][0].tool_calls == []
    assert env.lessons == ["two consecutive unusable responses (blank/narration retry exhausted)"]
    assert out["iteration"] == 1


def test_blank_twice_gives_explicit_failure(env):
    model = env.script("", "")
    out = run_call_model(make_state())
    assert len(model.calls) == 2
    assert last_text(out).startswith("[AGENT] I wasn't able")
    assert len(env.lessons) == 1


def test_blank_then_narration_is_still_a_failure(env):
    env.script("", "I'll run a scan now.")
    out = run_call_model(make_state())
    assert last_text(out).startswith("[AGENT] I wasn't able")


def test_narration_is_not_retried_when_no_tools_are_available(env, monkeypatch):
    async def no_tools(_persona):
        return []

    monkeypatch.setattr(graph, "get_persona_tools_async", no_tools)
    model = env.script("I'll run a query to check that for you.")
    out = run_call_model(make_state())
    assert len(model.calls) == 1
    assert last_text(out) == "I'll run a query to check that for you."


@pytest.mark.parametrize(
    "answer",
    [
        "Done! Let me know if you need anything else.",
        "I can help with that whenever you're ready.",
        "I'd say the second option is better overall.",
    ],
)
def test_ordinary_final_answers_are_not_mistaken_for_narration(env, answer):
    """FIX-NARRATION-FP regression: a correct answer must not be thrown away."""
    model = env.script(answer)
    out = run_call_model(make_state())
    assert len(model.calls) == 1
    assert last_text(out) == answer


def test_retry_call_raising_evicts_client_and_does_not_crash(env):
    model = env.script("I'll run a scan.", RuntimeError("connection reset"))
    out = run_call_model(make_state())
    assert len(model.calls) == 2
    assert env.evictions == 1
    # The turn still ends with a clean AIMessage rather than an exception.
    assert isinstance(out["messages"][0], AIMessage)
    assert out["iteration"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# 2. Hard stops
# ═════════════════════════════════════════════════════════════════════════════

def test_max_loops_halts_without_calling_the_model(env, monkeypatch):
    monkeypatch.setattr(runtime_config, "max_loops", 3)
    model = env.script("must not be used")
    out = run_call_model(make_state(iteration=3))
    assert model.calls == []
    assert last_text(out) == "[AGENT HALTED]: Reached max loop limit (3)."
    assert out["iteration"] == 3          # halt does not increment
    assert env.lessons == ["max_loops=3"]


def test_one_below_max_loops_still_runs(env, monkeypatch):
    monkeypatch.setattr(runtime_config, "max_loops", 3)
    model = env.script("fine")
    out = run_call_model(make_state(iteration=2))
    assert len(model.calls) == 1
    assert out["iteration"] == 3
    assert env.lessons == []


def test_stuck_streak_of_six_halts_and_resets_counter(env):
    model = env.script("must not be used")
    out = run_call_model(make_state(iteration=4, consecutive_tool_failures=6))
    assert model.calls == []
    assert "[AGENT HALTED]: 6 tool calls in a row failed" in last_text(out)
    assert out["consecutive_tool_failures"] == 0
    assert env.lessons == ["consecutive_tool_failures=6"]


def test_stuck_streak_of_five_does_not_halt(env):
    model = env.script("fine")
    out = run_call_model(make_state(iteration=4, consecutive_tool_failures=5))
    assert len(model.calls) == 1
    assert "HALTED" not in last_text(out)
    assert env.lessons == []


def test_stale_streak_from_a_previous_turn_does_not_halt_a_new_turn(env):
    """FIX-STALE-STREAK: iteration 0 = fresh user message; an old checkpointed
    streak of 6+ must not instantly halt it."""
    model = env.script("fine")
    out = run_call_model(make_state(iteration=0, consecutive_tool_failures=9))
    assert len(model.calls) == 1
    assert "HALTED" not in last_text(out)
    # ...and the stale counters are zeroed in what gets persisted.
    assert out["consecutive_tool_failures"] == 0
    assert out["failed_tool_calls"] == {}


# ═════════════════════════════════════════════════════════════════════════════
# 3. FIX-ROUTER-2 — client construction / call failures end the turn cleanly
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "exc",
    [
        ImportError("Run: pip install langchain-anthropic"),
        EnvironmentError("ANTHROPIC_API_KEY is not set in .env"),
    ],
)
def test_client_construction_failure_is_a_clean_model_error(env, monkeypatch, exc):
    def boom():
        raise exc

    monkeypatch.setattr(graph, "_get_cached_llm", boom)
    out = run_call_model(make_state())        # must not raise
    text = last_text(out)
    assert text.startswith("[MODEL ERROR]: Could not initialize the model client")
    assert str(exc) in text
    assert out["iteration"] == 1
    assert env.evictions == 0   # nothing was cached, so nothing to evict


def test_construction_failures_are_never_cached_so_a_fixed_env_recovers(monkeypatch):
    """Drives the REAL _get_cached_llm: first build raises, second succeeds."""
    attempts = {"n": 0}
    sentinel = object()

    def flaky_get_llm(**_):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise EnvironmentError("ANTHROPIC_API_KEY is not set in .env")
        return sentinel

    monkeypatch.setattr(graph, "get_llm", flaky_get_llm)
    monkeypatch.setattr(graph, "_LLM_CACHE", {})

    with pytest.raises(EnvironmentError):
        graph._get_cached_llm()
    assert graph._LLM_CACHE == {}                 # failure left no entry behind
    assert graph._get_cached_llm() is sentinel    # user fixes .env -> works
    assert graph._get_cached_llm() is sentinel    # and is now cached
    assert attempts["n"] == 2


def test_ainvoke_failure_is_a_clean_model_error_and_evicts(env):
    env.script(RuntimeError("ResponseError: XML syntax error"))
    out = run_call_model(make_state())
    assert last_text(out).startswith("[MODEL ERROR]: The model call failed")
    assert "XML syntax error" in last_text(out)
    assert out["iteration"] == 1
    assert env.evictions == 1


def test_evict_removes_only_the_active_cache_entry(monkeypatch):
    """Uses the real _evict_cached_llm (the `env` fixture is deliberately not
    used here because it replaces it)."""
    cfg = graph._get_active_config()
    mine = (cfg.model_name, cfg.model_provider, cfg.temperature)
    other = ("some-other-model", "ollama", 0.5)
    monkeypatch.setattr(graph, "_LLM_CACHE", {mine: "mine", other: "other"})
    graph._evict_cached_llm()
    assert graph._LLM_CACHE == {other: "other"}


# ═════════════════════════════════════════════════════════════════════════════
# 4. Stuck-streak warning + bookkeeping in execute_tools
# ═════════════════════════════════════════════════════════════════════════════

def tool_state(calls, **over):
    """State whose last message is an AIMessage requesting `calls`
    ([(name, args), ...])."""
    tool_calls = [
        {"name": n, "args": a, "id": f"call-{i}", "type": "tool_call"}
        for i, (n, a) in enumerate(calls)
    ]
    return make_state(messages=[AIMessage(content="", tool_calls=tool_calls)], **over)


def nudges(out):
    return [m for m in out["messages"] if isinstance(m, HumanMessage)]


@pytest.mark.parametrize(
    "streak_before, expect_nudge, streak_after",
    [
        (0, False, 1),
        (1, False, 2),   # 2nd failure: still quiet
        (2, True, 3),    # 3rd failure: warning fires
        (3, True, 4),
        (4, True, 5),
        (5, False, 6),   # 6th: call_model halts next turn, so no pointless nudge
    ],
)
def test_streak_warning_window(env, streak_before, expect_nudge, streak_after):
    out = run_execute_tools(
        tool_state([("bad_tool", {"n": 100 + streak_before})],
                   consecutive_tool_failures=streak_before)
    )
    assert out["consecutive_tool_failures"] == streak_after
    if expect_nudge:
        assert len(nudges(out)) == 1
        assert "[STUCK PATTERN DETECTED]" in nudges(out)[0].content
        assert f"{streak_after} tool calls in a row" in nudges(out)[0].content
    else:
        assert nudges(out) == []


def test_nudge_comes_after_tool_messages_so_id_pairing_stays_valid(env):
    out = run_execute_tools(
        tool_state([("bad_tool", {"n": 1})], consecutive_tool_failures=2)
    )
    msgs = out["messages"]
    assert isinstance(msgs[0], ToolMessage) and msgs[0].tool_call_id == "call-0"
    assert isinstance(msgs[-1], HumanMessage)
    assert [m for m in msgs if isinstance(m, ToolMessage)].__len__() == 1


def test_a_success_resets_the_streak(env):
    out = run_execute_tools(
        tool_state([("good_tool", {"n": 1})], consecutive_tool_failures=5)
    )
    assert out["consecutive_tool_failures"] == 0
    assert nudges(out) == []


def test_streak_counts_across_a_batch_of_calls_in_one_turn(env):
    out = run_execute_tools(
        tool_state([("bad_tool", {"n": 1}), ("bad_tool", {"n": 2}), ("bad_tool", {"n": 3})])
    )
    assert out["consecutive_tool_failures"] == 3
    assert len(nudges(out)) == 1


def test_success_in_the_middle_of_a_batch_resets_midway(env):
    out = run_execute_tools(
        tool_state([("bad_tool", {"n": 1}), ("bad_tool", {"n": 2}),
                    ("good_tool", {"n": 3}), ("bad_tool", {"n": 4})])
    )
    assert out["consecutive_tool_failures"] == 1


def test_identical_repeat_of_a_failed_call_is_blocked_without_running(env, monkeypatch):
    ran = []
    real = bad_tool.ainvoke

    async def spy(args, config=None, **kw):
        ran.append(args)
        return await real(args, config=config, **kw)

    monkeypatch.setattr(type(bad_tool), "ainvoke", lambda self, a, config=None, **kw: spy(a, config, **kw))

    first = run_execute_tools(tool_state([("bad_tool", {"n": 1})]))
    assert len(ran) == 1
    second = run_execute_tools(
        tool_state([("bad_tool", {"n": 1})],
                   failed_tool_calls=first["failed_tool_calls"],
                   consecutive_tool_failures=first["consecutive_tool_failures"])
    )
    assert len(ran) == 1, "the identical failing call must not be dispatched again"
    assert second["messages"][0].content.startswith("[REPEAT-FAIL]")
    assert second["consecutive_tool_failures"] == 2   # blocked repeats still count


def test_unregistered_tool_counts_as_a_failure(env):
    out = run_execute_tools(tool_state([("no_such_tool", {"x": 1})]))
    assert "not registered" in out["messages"][0].content
    assert out["consecutive_tool_failures"] == 1


def test_missing_required_arg_is_blocked_before_dispatch_and_counts(env):
    out = run_execute_tools(tool_state([("good_tool", {})]))   # n is required
    assert out["messages"][0].content.startswith("Error:")
    assert out["consecutive_tool_failures"] == 1


def test_execute_tools_ignores_a_last_message_with_no_tool_calls(env):
    out = run_execute_tools(make_state(messages=[AIMessage(content="just text")]))
    assert out == {"messages": []}


def test_should_continue_routes_on_tool_calls():
    with_calls = AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}])
    assert graph.should_continue({"messages": [with_calls]}) == "tools"
    assert graph.should_continue({"messages": [AIMessage(content="done")]}) == END
    assert graph.should_continue({"messages": []}) == END


# ═════════════════════════════════════════════════════════════════════════════
# 5. End to end through the real compiled graph
# ═════════════════════════════════════════════════════════════════════════════

def run_graph(text="go"):
    g = graph.create_beaver_graph()           # MemorySaver
    cfg = {
        "recursion_limit": runtime_config.recursion_limit,
        "configurable": {"thread_id": "e2e"},
    }
    return asyncio.run(g.ainvoke(make_state(text), cfg))


def test_graph_tool_then_answer(env):
    model = env.script(tool_call_text("good_tool", n=1), "All done.")
    final = run_graph()
    assert len(model.calls) == 2
    assert final["messages"][-1].content == "All done."
    assert any(isinstance(m, ToolMessage) and m.content == "ok 1" for m in final["messages"])


def test_graph_six_varied_failures_halt_early_with_warnings_before(env):
    """The headline scenario: varied-args failures never trip the exact-repeat
    guard, so the streak logic has to catch them. 6 failures -> halt, and the
    model is NOT asked a 7th time. Warnings appear after failures 3, 4 and 5."""
    model = env.script(*[tool_call_text("bad_tool", n=i) for i in range(1, 9)])
    final = run_graph()

    assert len(model.calls) == 6
    assert final["messages"][-1].content.startswith("[AGENT HALTED]: 6 tool calls in a row")
    warnings = [m for m in final["messages"]
                if isinstance(m, HumanMessage) and "[STUCK PATTERN DETECTED]" in m.content]
    assert [w.content.split(":")[1].split()[0] for w in warnings] == ["3", "4", "5"]
    assert env.lessons == ["consecutive_tool_failures=6"]
    assert final["consecutive_tool_failures"] == 0   # reset for the next message


def test_graph_the_warning_is_visible_to_the_model_on_its_next_call(env):
    model = env.script(*[tool_call_text("bad_tool", n=i) for i in range(1, 9)])
    run_graph()
    # Call 4 is the first made after the 3rd failure.
    seen_by_call_4 = [str(m.content) for m in model.calls[3]]
    assert any("[STUCK PATTERN DETECTED]" in c for c in seen_by_call_4)
    # Call 3 happened after only 2 failures: no warning yet.
    assert not any("[STUCK PATTERN DETECTED]" in str(m.content) for m in model.calls[2])


def test_graph_a_success_interrupts_the_streak_so_no_halt(env):
    env.script(
        tool_call_text("bad_tool", n=1), tool_call_text("bad_tool", n=2),
        tool_call_text("good_tool", n=3),                 # resets
        tool_call_text("bad_tool", n=4), tool_call_text("bad_tool", n=5),
        "finished",
    )
    final = run_graph()
    assert final["messages"][-1].content == "finished"
    assert not any(isinstance(m, HumanMessage) and "STUCK PATTERN" in m.content
                   for m in final["messages"])
    assert env.lessons == []


def test_graph_max_loops_stops_a_runaway_that_never_fails(env, monkeypatch):
    monkeypatch.setattr(runtime_config, "max_loops", 3)
    model = env.script(*[tool_call_text("good_tool", n=i) for i in range(1, 10)])
    final = run_graph()
    assert len(model.calls) == 3
    assert final["messages"][-1].content == "[AGENT HALTED]: Reached max loop limit (3)."
    assert env.lessons == ["max_loops=3"]


def test_graph_default_recursion_limit_leaves_room_for_the_max_loops_halt(env, monkeypatch):
    """config.recursion_limit exists so the graceful halt message is reached
    before LangGraph's own GraphRecursionError. Prove it at the default 16."""
    monkeypatch.setattr(runtime_config, "max_loops", 16)
    env.script(*[tool_call_text("good_tool", n=i) for i in range(1, 40)])
    final = run_graph()                      # would raise GraphRecursionError if too low
    assert final["messages"][-1].content.startswith("[AGENT HALTED]: Reached max loop limit")


def test_graph_narration_recovers_mid_run(env):
    model = env.script(
        "Let me check the directory first.",              # narration -> retried
        tool_call_text("good_tool", n=1),
        "Here are the results.",
    )
    final = run_graph()
    assert len(model.calls) == 3
    assert final["messages"][-1].content == "Here are the results."


def test_graph_client_failure_ends_turn_with_model_error(env, monkeypatch):
    def boom():
        raise ImportError("Run: pip install langchain-anthropic")

    monkeypatch.setattr(graph, "_get_cached_llm", boom)
    final = run_graph()
    assert final["messages"][-1].content.startswith("[MODEL ERROR]: Could not initialize")


# ═════════════════════════════════════════════════════════════════════════════
# 6. Gap found by mutation testing: a [SCOPE BLOCK] is a failed call
# ═════════════════════════════════════════════════════════════════════════════

def test_scope_block_counts_toward_the_stuck_streak(env):
    """A model hammering out-of-scope targets must eventually hit the halt,
    same as any other repeatedly failing call."""
    out = run_execute_tools(
        tool_state([("good_tool", {"n": 1, "command": "nmap 8.8.8.8"})])  # 8.8.8.8 is out of scope
    )
    assert out["messages"][0].content.startswith("[SCOPE BLOCK]")
    assert out["consecutive_tool_failures"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# 7. KNOWN BUGS — written as strict xfail so they are tracked, not forgotten.
#    When one is fixed the test starts XPASSing and strict mode turns the run
#    red: delete the marker and the test becomes a normal regression guard.
# ═════════════════════════════════════════════════════════════════════════════

# Ordinary, correct final answers that _looks_like_narration() currently flags.
# Each contains a first-person/infinitive phrase within ~25 chars of an
# "action verb" (confirm->update, running the, query, search, run, ...), which
# is how the phrase heuristics fire on normal prose.
LEGIT_ANSWERS_FLAGGED_AS_NARRATION = [
    "Done. Let me know if you want me to update it.",            # the phrase FIX-NARRATION-FP says it fixed
    "I can confirm that the update was applied successfully.",
    "The server is running the latest version of nginx.",
    "Here is the query result: 42 rows.",
    "I can see that the scan completed with no open ports.",
    "Nothing found. I can search again with different terms if you like.",
]


@pytest.mark.xfail(strict=True, reason=(
    "BUG: _looks_like_narration() false-positives on ordinary final answers. "
    "A correct answer is discarded, the model is re-prompted, and if it repeats "
    "the answer the user is shown '[AGENT] I wasn't able to produce a valid "
    "response' instead."))
@pytest.mark.parametrize("answer", LEGIT_ANSWERS_FLAGGED_AS_NARRATION)
def test_BUG_legit_final_answer_is_not_narration(answer):
    assert graph._looks_like_narration(AIMessage(content=answer)) is False


@pytest.mark.xfail(strict=True, reason=(
    "BUG (user-visible form of the above): a correct answer costs 2 model calls "
    "and is replaced by the generic failure message."))
def test_BUG_correct_answer_survives_the_loop(env):
    answer = "I can confirm that the update was applied successfully."
    model = env.script(answer, answer)   # model (reasonably) repeats itself when re-prompted
    out = run_call_model(make_state("apply the update", iteration=2))
    assert last_text(out) == answer
    assert len(model.calls) == 1


@pytest.mark.xfail(strict=True, reason=(
    "BUG (minor, message accuracy): when the RETRY call itself raises, the user "
    "is told the model 'returned an empty or non-tool response both times' — "
    "false, the second attempt never produced a response. The real cause "
    "(connection error) is only in agent.log."))
def test_BUG_retry_exception_is_not_reported_as_two_bad_responses(env):
    env.script("I'll run a scan.", RuntimeError("connection reset"))
    out = run_call_model(make_state())
    assert "both times" not in last_text(out)
