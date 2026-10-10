"""Scope enforcement — agent/tools.py matchers AND the real gate inside
agent.graph.execute_tools().

pentester.md tells the model that out-of-scope targets are blocked by the
runtime. These tests pin that promise down: they drive the REAL
execute_tools() with stand-in tools (so nothing touches the network) and
assert which calls get a [SCOPE BLOCK] and which reach the tool.
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

import agent.graph as graph
from agent.tools import is_in_scope, is_target_in_scope

DEFAULT_SCOPE = ["127.0.0.1", "localhost", "192.168.0.0/16", "10.0.0.0/8"]


# ── is_in_scope: raw IPs inside a shell command ─────────────────────────────

@pytest.mark.parametrize(
    "command, expected",
    [
        ("nmap -sV 10.1.2.3", True),
        ("nmap -sV 192.168.1.5", True),
        ("nmap -sV 8.8.8.8", False),
        ("ls -la", True),                      # no IP at all -> nothing to gate
        ("curl http://8.8.8.8/ && ping 10.0.0.1", False),  # one bad IP blocks it
    ],
)
def test_is_in_scope_ip_commands(command, expected):
    assert is_in_scope(command, DEFAULT_SCOPE) is expected


# ── is_target_in_scope: hostnames / URLs ────────────────────────────────────

@pytest.mark.parametrize(
    "value, scope, expected",
    [
        ("example.com", ["example.com"], True),
        ("sub.example.com", ["example.com"], True),
        ("https://api.example.com/v1/users", ["example.com"], True),
        ("deep.sub.example.com", ["example.com"], True),
        ("evil-corp.com", ["example.com"], False),
        # classic suffix-confusion bypasses must NOT match
        ("example.com.evil-corp.com", ["example.com"], False),
        ("notexample.com", ["example.com"], False),
        # literal-IP hosts fall back to the IP/CIDR entries
        ("http://127.0.0.1/admin", DEFAULT_SCOPE, True),
        ("http://10.5.5.5/internal", DEFAULT_SCOPE, True),
        ("http://8.8.8.8/", DEFAULT_SCOPE, False),
        ("http://localhost:8080/api", DEFAULT_SCOPE, True),
        # batch (http_check's `urls`): fail closed on any out-of-scope host
        ("https://example.com, https://evil-corp.com", ["example.com"], False),
        ("https://example.com\nhttps://sub.example.com", ["example.com"], True),
        # wildcard / empty scope allow everything
        ("anything.test", ["*"], True),
        ("anything.test", [], True),
    ],
)
def test_is_target_in_scope(value, scope, expected):
    assert is_target_in_scope(value, scope) is expected


# ── the real gate in execute_tools() ────────────────────────────────────────

REACHED = "REACHED"


@tool
def http_get(url: str) -> str:
    """stand-in for plugins.http_probe.http_get"""
    return REACHED


@tool
def http_check(urls: str) -> str:
    """stand-in for plugins.http_probe.http_check"""
    return REACHED


@tool
def subdomain_bruteforce(domain: str) -> str:
    """stand-in for plugins.pentester_recon.subdomain_bruteforce"""
    return REACHED


@tool
def web_fetch(url: str) -> str:
    """stand-in for plugins.web_search.web_fetch (general research)"""
    return REACHED


@tool
def os_exec(command: str) -> str:
    """stand-in for skills.os_exec.os_exec"""
    return REACHED


FAKE_TOOLS = [http_get, http_check, subdomain_bruteforce, web_fetch, os_exec]


def run_gate(monkeypatch, persona, tool_name, args, scope):
    async def fake_tools(_persona):
        return FAKE_TOOLS

    monkeypatch.setattr(graph, "get_persona_tools_async", fake_tools)
    state = {
        "messages": [
            AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "call-1"}])
        ],
        "active_persona": persona,
        "target_scope": scope,
    }
    result = asyncio.run(graph.execute_tools(state, {"configurable": {"thread_id": "t"}}))
    return result["messages"][0].content


@pytest.mark.parametrize(
    "persona, tool_name, args, scope, blocked",
    [
        # active, target-directed tools under the pentester persona are gated
        ("pentester", "http_get", {"url": "https://out-of-scope.test/"}, DEFAULT_SCOPE, True),
        ("pentester", "http_get", {"url": "http://localhost:8000/health"}, DEFAULT_SCOPE, False),
        ("pentester", "subdomain_bruteforce", {"domain": "somecompany.com"}, DEFAULT_SCOPE, True),
        ("pentester", "http_check", {"urls": "http://localhost/, https://out.test/"}, DEFAULT_SCOPE, True),
        ("pentester", "http_get", {"url": "https://example.com.evil.test/"}, ["example.com"], True),
        ("pentester", "http_get", {"url": "https://api.example.com/"}, ["example.com"], False),
        # general OSINT research is deliberately NOT gated for the pentester
        ("pentester", "web_fetch", {"url": "https://linkedin.com/company/acme"}, DEFAULT_SCOPE, False),
        # other personas use the same http tools for ordinary internet work
        ("coder", "http_get", {"url": "https://api.github.com/"}, DEFAULT_SCOPE, False),
        ("standard", "http_get", {"url": "https://example.org/"}, DEFAULT_SCOPE, False),
        # the original os_exec IP gate still applies to every persona
        ("coder", "os_exec", {"command": "nmap 8.8.8.8"}, DEFAULT_SCOPE, True),
        ("coder", "os_exec", {"command": "nmap 10.0.0.5"}, DEFAULT_SCOPE, False),
    ],
)
def test_execute_tools_scope_gate(monkeypatch, persona, tool_name, args, scope, blocked):
    content = run_gate(monkeypatch, persona, tool_name, args, scope)
    if blocked:
        assert content.startswith("[SCOPE BLOCK]"), content
    else:
        assert content == REACHED, content


# ── drift guards ────────────────────────────────────────────────────────────

def test_allowlist_excludes_general_research_tools():
    for name in ("web_search", "web_fetch", "ddg_news"):
        assert name not in graph._SCOPE_GATED_TARGET_TOOLS


def test_every_allowlisted_tool_exists_in_a_real_plugin():
    """If a gated tool is renamed in its plugin, the gate silently stops
    covering it — fail loudly instead."""
    from plugins import http_probe, pentester_recon, web_fuzz

    real = {t.name for mod in (http_probe, pentester_recon, web_fuzz) for t in mod.TOOLS}
    assert graph._SCOPE_GATED_TARGET_TOOLS <= real, graph._SCOPE_GATED_TARGET_TOOLS - real


def test_every_target_directed_plugin_tool_is_gated():
    """Conversely: a NEW url/domain-taking tool added to a pentester plugin
    must be added to the allowlist too (or consciously exempted here)."""
    from plugins import http_probe, pentester_recon, web_fuzz

    exempt = {"cve_lookup", "http_session_reset"}  # no target argument
    for mod in (http_probe, pentester_recon, web_fuzz):
        for t in mod.TOOLS:
            if t.name in exempt:
                continue
            assert t.name in graph._SCOPE_GATED_TARGET_TOOLS, (
                f"{t.name} takes a target but isn't in _SCOPE_GATED_TARGET_TOOLS"
            )
