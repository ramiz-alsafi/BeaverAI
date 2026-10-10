"""plugins.web_fuzz session store — per-conversation isolation.

[FIX-SESSION-THREAD] _SESSIONS was keyed by the bare model-chosen label
("user_a"), so two concurrent web conversations shared one authenticated
cookie jar. [FIX-SESSION-EVICT] Eviction then still counted the GLOBAL dict,
so one busy conversation could force-logout another's session.
"""
import pytest

import plugins.web_fuzz as wf


@pytest.fixture(autouse=True)
def clean_sessions():
    wf._SESSIONS.clear()
    yield
    for client in list(wf._SESSIONS.values()):
        client.close()
    wf._SESSIONS.clear()


def test_same_label_in_different_threads_gets_separate_clients(thread_id):
    thread_id("thread-A")
    a = wf._get_session("user_a")
    a.cookies.set("session_token", "A_SECRET")

    thread_id("thread-B")
    b = wf._get_session("user_a")

    assert a is not b
    assert "session_token" not in b.cookies


def test_same_thread_gets_its_own_client_back(thread_id):
    thread_id("thread-A")
    first = wf._get_session("user_a")
    assert wf._get_session("user_a") is first


def test_another_threads_activity_never_evicts_my_sessions(thread_id):
    thread_id("thread-A")
    wf._get_session("s1")
    wf._get_session("s2")

    thread_id("thread-B")
    for i in range(wf._MAX_SESSIONS - 1):   # global total now exceeds the cap
        wf._get_session(f"b{i}")
    assert len(wf._SESSIONS) > wf._MAX_SESSIONS

    thread_id("thread-A")
    assert "thread-A::s1" in wf._SESSIONS
    assert "thread-A::s2" in wf._SESSIONS


def test_eviction_still_caps_a_single_thread_oldest_first(thread_id):
    thread_id("thread-C")
    for i in range(wf._MAX_SESSIONS + 2):
        wf._get_session(f"c{i}")

    mine = [k for k in wf._SESSIONS if k.startswith("thread-C::")]
    assert len(mine) == wf._MAX_SESSIONS
    assert "thread-C::c0" not in wf._SESSIONS
    assert "thread-C::c1" not in wf._SESSIONS
    assert f"thread-C::c{wf._MAX_SESSIONS + 1}" in wf._SESSIONS


def test_reset_all_only_clears_the_current_threads_sessions(thread_id):
    thread_id("thread-A")
    wf._get_session("keep_me")

    thread_id("thread-B")
    wf._get_session("x")
    wf._get_session("y")
    msg = wf.http_session_reset.invoke({"session_id": ""})

    assert "2" in msg
    assert "thread-A::keep_me" in wf._SESSIONS
    assert not [k for k in wf._SESSIONS if k.startswith("thread-B::")]


def test_reset_one_session_only_touches_that_label_in_this_thread(thread_id):
    thread_id("thread-A")
    wf._get_session("user_a")
    thread_id("thread-B")
    wf._get_session("user_a")

    wf.http_session_reset.invoke({"session_id": "user_a"})   # as thread-B
    assert "thread-B::user_a" not in wf._SESSIONS
    assert "thread-A::user_a" in wf._SESSIONS
