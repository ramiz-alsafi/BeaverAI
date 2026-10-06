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
- **Web UI** — `BEAVER_WEB_TOKEN` (auth), `BEAVER_WEB_HOST`/`BEAVER_WEB_PORT`, `BEAVER_TLS_CERT`/`BEAVER_TLS_KEY`
- **MCP servers** — `MCP_SERVERS` (JSON array of server configs). Several of these (filesystem, git, chroma, sqlite) take **absolute paths** to your machine as command args — update them to your own repo location after cloning; they won't work as-is on a different machine or OS.
- **A2A sub-agents** — `A2A_AGENTS` (JSON array of `{name, persona, model}`), `A2A_TIMEOUT`, `A2A_MAX_CONCURRENT`
- **Plugins** — e.g. `BEAVER_NOTES_DIR` for `structured_notes`

## Security notes

- `BEAVER_WEB_TOKEN`, if set, gates the Web UI's WebSocket connection — the browser prompts for it once per session. Leave unset for pure localhost/solo use.
- TLS is opt-in (`BEAVER_TLS_CERT` + `BEAVER_TLS_KEY`) and required the moment you bind `BEAVER_WEB_HOST` to anything other than localhost — otherwise the auth token travels in plaintext.
- `.env` is gitignored. If a real `.env` was ever created, committed, screen-shared, or otherwise exposed before this repo went public, treat `BEAVER_WEB_TOKEN` (and any MCP-server credentials) as burned and rotate it.
- `os_exec` and the filesystem MCP server give the agent real shell/file access on your machine. Treat personas and plugins that expose them the same way you'd treat any tool with local code-execution — don't point them at anything you wouldn't want an LLM poking at.

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
.venv/bin/pip install pytest pytest-asyncio
.venv/bin/pytest tests/unit/ -v
```

> **Note:** this repo doesn't currently ship a `tests/` directory or a CI workflow (no `.github/workflows/`) — `pytest tests/unit/` above will fail with "no tests found" until you add tests. If you want CI running this suite (plus `ruff`/`mypy`) across Python 3.11/3.12 on every push, you'll need to add a `.github/workflows/ci.yml` yourself; there isn't one here yet.

## License

MIT — see [LICENSE](LICENSE).
