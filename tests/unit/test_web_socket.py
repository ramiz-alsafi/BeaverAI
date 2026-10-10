"""web.server — WebSocket Origin allowlist ([ORIGIN-1]).

Browsers don't apply CORS to WebSockets, so any page open in the user's own
browser can connect to ws://127.0.0.1:<port>/ws. With no BEAVER_WEB_TOKEN set
(the zero-setup default) that page could drive an agent that has os_exec.
The Origin header is the one thing such a page can't forge.

These tests go through the real endpoint with Starlette's TestClient. The
app's startup hook is NOT run (TestClient used without a context manager),
so after a connection passes the origin and auth checks the server answers
"Agent graph not ready yet" — which is how we tell "got through" from
"was turned away".
"""
import json
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import web.server as server

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def default_policy(monkeypatch):
    """Pin the policy so the suite doesn't depend on the runner's environment."""
    monkeypatch.setattr(server, "_ALLOWED_ORIGINS", server._build_allowed_origins("8000", ""))
    monkeypatch.setattr(server, "_WEB_TOKEN", "")


@pytest.fixture
def client():
    return TestClient(server.app)


def connect_and_auth(client, headers, token=""):
    """Open /ws, send the mandatory auth frame, return the server's reply
    (a dict), or the close code (an int) if the server hung up instead."""
    with client.websocket_connect("/ws", headers=headers) as ws:
        try:
            ws.send_text(json.dumps({"type": "auth", "token": token}))
            return json.loads(ws.receive_text())
        except WebSocketDisconnect as e:
            return e.code


def got_through(result):
    return isinstance(result, dict) and "not ready" in result.get("message", "")


# ── rejected ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://evil.example:8000",          # right port, wrong host
        "http://127.0.0.1.evil.example:8000",  # loopback as a subdomain prefix
        "http://localhost:9999",             # loopback, wrong port
        "null",                              # sandboxed iframe / file:// page
    ],
)
def test_foreign_origin_is_turned_away_with_4403(client, origin):
    assert connect_and_auth(client, {"origin": origin}) == 4403


def test_dns_rebinding_shape_is_rejected(client):
    """Attacker's domain rebound to 127.0.0.1: Origin and Host MATCH each
    other (both evil.example:8000), so a same-origin-by-Host check would
    wave it through. The allowlist doesn't care what Host says."""
    result = connect_and_auth(client, {"origin": "http://evil.example:8000", "host": "evil.example:8000"})
    assert result == 4403


def test_rejection_happens_before_the_auth_frame_is_read(client):
    """4403, not 4401: the server must not even look at what a rejected page sends."""
    result = connect_and_auth(client, {"origin": "https://evil.example"}, token="whatever")
    assert result == 4403


# ── allowed ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://[::1]:8000",
        "https://localhost:8000",
        "http://localhost:5173",      # Vite dev server (proxy forwards the browser's Origin)
        "http://127.0.0.1:5173",
    ],
)
def test_loopback_and_dev_origins_still_work(client, origin):
    assert got_through(connect_and_auth(client, {"origin": origin}))


def test_missing_origin_is_allowed_non_browser_client(client):
    assert got_through(connect_and_auth(client, {}))


# ── configuration ───────────────────────────────────────────────────────────

def test_extra_origins_are_normalised_and_honoured(monkeypatch, client):
    built = server._build_allowed_origins("8000", " https://Beaver.Example.com/ , http://192.168.1.5:8000,,")
    assert "https://beaver.example.com" in built
    assert "http://192.168.1.5:8000" in built
    monkeypatch.setattr(server, "_ALLOWED_ORIGINS", built)
    assert got_through(connect_and_auth(client, {"origin": "https://beaver.example.com"}))
    assert connect_and_auth(client, {"origin": "https://other.example.com"}) == 4403


def test_wildcard_disables_the_check(monkeypatch, client):
    monkeypatch.setattr(server, "_ALLOWED_ORIGINS", server._build_allowed_origins("8000", "*"))
    assert got_through(connect_and_auth(client, {"origin": "https://evil.example"}))


@pytest.mark.parametrize(
    "port, origin",
    [("80", "http://localhost"), ("443", "https://127.0.0.1"), ("9000", "http://localhost:9000")],
)
def test_browsers_omit_default_ports_and_the_list_accounts_for_it(port, origin):
    assert origin in server._build_allowed_origins(port, "")


@pytest.mark.parametrize("origin", ["HTTP://LOCALHOST:8000", "http://localhost:8000/", " http://localhost:8000 "])
def test_matching_ignores_case_trailing_slash_and_whitespace(origin):
    assert server._origin_allowed(origin)


# ── interaction with the token ──────────────────────────────────────────────

def test_with_a_token_set_a_foreign_origin_is_not_blocked_but_still_needs_the_token(monkeypatch, client):
    """A hijacking page can't read the token out of this origin's storage, so
    it can't authenticate; enforcing the allowlist as well would only break
    legitimate LAN / reverse-proxy setups."""
    monkeypatch.setattr(server, "_WEB_TOKEN", "s3cret-token")
    evil = {"origin": "https://evil.example"}
    assert got_through(connect_and_auth(client, evil, token="s3cret-token"))
    assert connect_and_auth(client, evil, token="wrong") == 4401   # token failure, not origin


# ── documentation ───────────────────────────────────────────────────────────

def test_env_example_documents_beaver_web_origins():
    text = (ROOT / ".env.example").read_text()
    assert re.search(r"^#?BEAVER_WEB_ORIGINS=", text, re.M)
