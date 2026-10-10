"""skills.system_info.get_environment_vars — must never hand credentials to the model.

[FIX-ENVVAR-URI] The name-based blocklist missed connection strings whose
NAME is innocuous (DATABASE_URL ...) but whose VALUE embeds user:password@.
"""
import os

from skills.system_info import get_environment_vars


def run(monkeypatch, env):
    # Replace the whole environ with a small dict: the tool caps output at 30
    # lines, so a real CI environment could push our variables out of view.
    monkeypatch.setattr(os, "environ", env)
    return get_environment_vars.invoke({})


def test_hides_credentials_embedded_in_connection_strings(monkeypatch):
    out = run(monkeypatch, {
        "DATABASE_URL": "postgres://admin:SuperSecret123@db.internal:5432/app",
        "REDIS_URL": "redis://default:hunter2@cache:6379/0",
        "MONGODB_URI": "mongodb://user:p@ss@mongo:27017/db",
        "SAFE_VAR": "hello world",
    })
    assert "SuperSecret123" not in out
    assert "hunter2" not in out
    assert "DATABASE_URL" not in out and "REDIS_URL" not in out
    assert "SAFE_VAR=hello world" in out


def test_still_hides_secret_looking_names(monkeypatch):
    out = run(monkeypatch, {
        "OPENAI_API_KEY": "sk-abc", "GITHUB_TOKEN": "ghp_x", "DB_PASSWORD": "pw",
        "MY_SECRET": "s", "NPM_AUTH": "a", "PLAIN": "ok",
    })
    for leaked in ("sk-abc", "ghp_x", "pw", "NPM_AUTH"):
        assert leaked not in out
    assert "PLAIN=ok" in out


def test_does_not_over_block_ordinary_urls_and_paths(monkeypatch):
    out = run(monkeypatch, {
        "API_BASE": "https://api.example.com/v1",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "DB_NO_CREDS": "postgres://db.internal:5432/app",
    })
    assert "API_BASE=https://api.example.com/v1" in out
    assert "PATH=/usr/local/bin" in out
    assert "DB_NO_CREDS=postgres://db.internal:5432/app" in out
