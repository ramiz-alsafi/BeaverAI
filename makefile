# Beaver — Makefile (Linux/macOS)
#
# Quick start:
#   make install     # create venv, install Python deps
#   make run          # CLI
#   make tui          # Textual TUI
#   make mcp-check    # verify npx/uvx are on PATH before /mcp reload
#   make clean        # remove venv + __pycache__
#
# Prerequisites this Makefile does NOT install for you:
#   - python3.11+          (python3 --version)
#   - Node.js + npx         (for filesystem / sequential-thinking MCP servers)
#   - uv (uvx)              (for git / fetch / time / sqlite MCP servers)
#                             https://docs.astral.sh/uv/getting-started/installation/
#   - Ollama itself          https://ollama.com/download
#
# NOTE: .env's MCP_SERVERS paths (git/chroma/sqlite --repository / --data-dir /
# --db-path) are currently Windows-style (C:/Users/...). On Linux, update those
# to your actual repo path (e.g. /home/<user>/beaver-3.0/...) before /mcp reload
# — this Makefile does not rewrite .env for you.

VENV        := .venv
PYTHON      := $(VENV)/bin/python3
PIP         := $(VENV)/bin/pip3
MODEL       ?= qwen2.5:7b

.PHONY: venv install run tui mcp-check models web-install web clean distclean

## Create the virtualenv if it doesn't exist yet
venv:
	@test -d $(VENV) || python3 -m venv $(VENV)

## Install/upgrade all Python dependencies into the venv
install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

## Run the CLI
run: install
	$(PYTHON) main.py

## Run the Textual TUI
tui: install
	$(PYTHON) main.py --tui

## Verify npx/uvx are on PATH — run this before /mcp reload if servers
## are failing to connect (matches the WinError2 issue from the Windows side)
mcp-check:
	@command -v npx  >/dev/null 2>&1 && echo "npx:  OK ($$(npx --version))"  || echo "npx:  MISSING — install Node.js (https://nodejs.org)"
	@command -v uvx  >/dev/null 2>&1 && echo "uvx:  OK ($$(uvx --version))"  || echo "uvx:  MISSING — install uv (https://docs.astral.sh/uv/getting-started/installation/)"
	@command -v ollama >/dev/null 2>&1 && echo "ollama: OK ($$(ollama --version))" || echo "ollama: MISSING — install from https://ollama.com/download"

## Pull the model set MODEL_NAME/A2A_AGENTS expect. Override with:
##   make models MODEL=qwen2.5:3b-instruct-q4_K_M
models:
	ollama pull $(MODEL)

## Install the web frontend's npm deps (once, or after pulling dependency changes)
web-install:
	cd web/frontend && npm install

## Start the FastAPI backend (serves the built frontend from web/frontend/dist/
## if present — run `cd web/frontend && npm run build` first, or use
## `npm run dev` separately for hot reload during frontend development)
web: install
	$(PYTHON) web/server.py

## Remove __pycache__ dirs (keeps venv, chroma_data, beaver_memory.sqlite)
clean:
	find . -type d -name "__pycache__" -not -path "./$(VENV)/*" -exec rm -rf {} +

## Full reset — also removes the venv. Does NOT touch chroma_data,
## beaver_memory.sqlite, or .env — those hold your actual data/config.
distclean: clean
	rm -rf $(VENV)