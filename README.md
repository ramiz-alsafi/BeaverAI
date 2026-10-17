# Beaver

A local-first AI agent built on [Ollama](https://ollama.com), with two interfaces: a **CLI** and a **Web UI**. No cloud LLM required — everything runs against a local Ollama model, with local memory (ChromaDB + SQLite) instead of hosted vector DBs.

## Features

- **Two interfaces, one agent** — CLI (`main.py`) and Web UI (`web/`, FastAPI + WebSocket backend, React/Vite frontend)
- **Personas** — switchable system prompts in `prompts/personas/`: `standard`, `coder`, `orchestrator`, `pentester`, `researcher`, `seo`, `social`, `system_agent`
- **Plugins** — tool integrations in `plugins/`: web search, Wikipedia research, code search, git context, HTTP probing, web fuzzing, SEO audit, pentest recon, structured notes
- **Skills** — core agent capabilities in `skills/`: file ops, OS exec, long-term memory, system info, MCP server loading, A2A (agent-to-agent) sub-agents, dynamic plugin loading
- **MCP servers** — connect external Model Context Protocol servers (filesystem, git, fetch, sqlite, sequential-thinking, etc.) via `.env`
- **Local memory** — ChromaDB for long-term/semantic memory, SQLite for checkpointing conversation state
- **Web UI sidebar** — browse and resume past sessions, inspect the tools resolved for the active persona, and browse/forget stored long-term memories, all live over the same WebSocket
- **Web UI activity panel** — tail `agent.log`, `beaver_traces.log`, and `uvicorn.log` from the browser, live
- **Web UI security** — token-gated WebSocket auth, with an optional TLS mode for anything beyond localhost

## Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com/download), running locally, with a model pulled (default `qwen2.5:7b`)
- Node.js + npx — required for the web frontend, and for some MCP servers
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (`uvx`) — required for some MCP servers (git, fetch, time, sqlite)

Run `make mcp-check` to verify `npx`/`uvx`/`ollama` are all on your `PATH`.

## Setup

```bash
git clone <your-repo-url>
cd beaver
cp .env.example .env       # then edit .env — see comments inline for every option
make install                # creates .venv, installs Python deps
make models                 # pulls the default Ollama model (override with MODEL=...)
```

`.env` is where all secrets and machine-specific paths live (web auth token, MCP server paths, etc.) — it's gitignored, and you fill it in locally after cloning. See [Configuration](#configuration) below before you set `MCP_SERVERS`, since those entries hardcode absolute filesystem paths for your machine.

## Running it

**CLI:**
```bash
make run
# or, one-shot:
.venv/bin/python main.py "your prompt here"
.venv/bin/python main.py "your prompt here" pentester   # one-shot, specify persona
.venv/bin/python main.py --models                        # list available Ollama models
```

**Web UI:**
```bash
make web-install            # once, installs web/frontend's npm deps
cd web/frontend && npm run build   # builds the production bundle server.py serves
make web                    # starts the FastAPI backend (python web/server.py)
```
Open `http://127.0.0.1:8000` (or whatever `BEAVER_WEB_HOST`/`BEAVER_WEB_PORT` you've set). If `BEAVER_WEB_TOKEN` is set in `.env`, the browser will prompt for it once per session.

For frontend development with hot reload, run the backend (`make web`) and the Vite dev server side by side:
```bash
cd web/frontend && npm run dev
```
This proxies `/ws` to the backend on `:8000`. For production, `npm run build` outputs to `web/frontend/dist/`, which `server.py` serves directly — rebuild after any frontend change.

## Configuration

All configuration lives in `.env` (gitignored — never commit your real one). `.env.example` is the fully-documented template; copy it and fill in what you need. Key sections:

- **Model** — `MODEL_NAME`, `MODEL_PROVIDER`, `OLLAMA_BASE_URL`, `OLLAMA_NUM_CTX`
- **Agent behavior** — `AGENT_MAX_LOOPS`, `AGENT_MAX_OUTPUT_CHARS`
- **Web UI** — `BEAVER_WEB_TOKEN` (auth), `BEAVER_WEB_ORIGINS` (extra allowed page origins), `BEAVER_WEB_HOST`/`BEAVER_WEB_PORT`, `BEAVER_TLS_CERT`/`BEAVER_TLS_KEY`
- **MCP servers** — `MCP_SERVERS` (JSON array of server configs). Several of these (filesystem, git, chroma, sqlite) take **absolute paths** to your machine as command args — update them to your own repo location after cloning; they won't work as-is on a different machine or OS.
- **A2A sub-agents** — `A2A_AGENTS` (JSON array of `{name, persona, model}`), `A2A_TIMEOUT`, `A2A_MAX_CONCURRENT`
- **Plugins** — e.g. `BEAVER_NOTES_DIR` for `structured_notes`
- **Audit log** — `BEAVER_AUDIT`, `BEAVER_AUDIT_DIR`, `BEAVER_AUDIT_STRICT`, `BEAVER_AUDIT_OUTPUT_CHARS` (see below)

## Audit log

Every tool call, scope block and scope change is appended to `audit/audit-YYYY-MM-DD.jsonl` (one JSON object per line, gitignored, mode `0600`, on by default). It records what ran, with which arguments, under which authorised scope, in which thread and persona — and when. The call is written **before** the tool runs, so a crash mid-command still leaves proof it was attempted.

- **Secrets are redacted before they reach disk** (passwords, tokens, URI credentials, cookies, `curl -u user:pass`); file-write bodies are stored as length + SHA-256 only, and tool **output** isn't stored at all unless you opt in with `BEAVER_AUDIT_OUTPUT_CHARS`. Redaction is best-effort pattern matching, not a guarantee.
- **Scope changes are recorded however they're made** — `/scope` in the CLI or web UI, or the Settings drawer.
- Each record carries the hash of the one before it. `python -m agent.audit verify` reports an edited, removed, inserted or reordered record, and prints the chain's **head hash**. Nothing in the file vouches for its *newest* record, so write that hash down somewhere off the machine (an engagement report, a ticket) and compare it later — otherwise editing only the last line, or truncating the end, goes unnoticed. This is a tripwire, not a notary: someone with write access can rewrite the whole file. Copy the files somewhere append-only if you need more.
- `BEAVER_AUDIT_STRICT=1` makes Beaver refuse to run a tool whose record can't be written; by default it logs an error and carries on.

## Security notes

- `BEAVER_WEB_TOKEN`, if set, gates the Web UI's WebSocket connection — the browser prompts for it once per session. Leave unset for pure localhost/solo use.
- With no token set, the server only accepts WebSocket connections from loopback origins (and the Vite dev server). Binding to localhost isn't enough on its own: browsers don't apply CORS to WebSockets, so any page open in your own browser could otherwise reach `ws://127.0.0.1:8000/ws` and drive the agent. If you reach the UI via a LAN address or reverse proxy, add that origin to `BEAVER_WEB_ORIGINS`.
- TLS is opt-in (`BEAVER_TLS_CERT` + `BEAVER_TLS_KEY`) and required the moment you bind `BEAVER_WEB_HOST` to anything other than localhost — otherwise the auth token travels in plaintext.
- `.env` is gitignored. If a real `.env` was ever created, committed, screen-shared, or otherwise exposed before this repo went public, treat `BEAVER_WEB_TOKEN` (and any MCP-server credentials) as burned and rotate it.
- **Tool approval.** Shell commands, file writes and MCP tools that write/edit/move/delete/execute need your explicit "yes" before they run (a popup in the Web UI, a prompt in the CLI), except a short read-only allowlist such as `whoami` or `git status`. Unattended runs with no one to ask are refused rather than allowed. This matters because Beaver reads untrusted web pages and files, and text hidden in them can try to steer the model. Tune it with `BEAVER_APPROVAL` (`ask` | `deny` | `off`) — see `.env.example`. It is a speed bump in front of a human, not a sandbox: an approved command runs with your full privileges, and `os_exec` is not confined to the workspace the way the file tools are.
- `os_exec` timeouts are clamped (default 60 s, ceiling `BEAVER_EXEC_MAX_TIMEOUT`) and captured output is capped (`BEAVER_EXEC_MAX_OUTPUT`).
- `os_exec` and the filesystem MCP server give the agent real shell/file access on your machine. Treat personas and plugins that expose them the same way you'd treat any tool with local code-execution — don't point them at anything you wouldn't want an LLM poking at. For real isolation, run Beaver in a container or VM.

## Project structure

```
agent/        core agent logic, config, graph, telemetry
cli/          CLI interface
web/          FastAPI backend + WebSocket protocol
web/frontend/ React/Vite web UI
memory/       checkpointing (SQLite) + long-term memory (ChromaDB)
plugins/      tool integrations, dynamically loaded
skills/       core capabilities (file ops, exec, MCP, A2A, memory)
prompts/      persona system prompts
tool_manuals/ per-tool usage docs surfaced to the agent
```

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests/unit/ -v
```

`tests/unit/` covers the pure logic and the cross-session isolation guarantees that are easy to break silently: tool-call JSON parsing, the pentester scope gate (driven through the real `execute_tools()`), provider auto-detection, per-session workspace/session isolation, plugin hot-reload detection, prompt caching, the slash-command table, and consistency checks over `.env.example` / `requirements.txt` / `tool_manuals/`. None of them need Ollama, a network connection, or an API key. (The two git tests skip themselves if `git` isn't installed.)

## License

MIT — see [LICENSE](LICENSE).
