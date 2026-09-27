import asyncio
import logging as _logging
import random
import time
import uuid
from typing import Any, Optional

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from langchain_core.messages import HumanMessage

from agent.graph import create_beaver_graph, _evict_cached_llm
from agent.config import runtime_config, PERSONAS_PROMPTS_DIR
from agent.telemetry import get_callbacks
from memory.checkpointer import lifespan_checkpointer
from skills import PERSONA_TOOLS
from agent.bus import tui_bus
from cli.hud import render_hud
from cli.cli_text_render import display_text

console = Console()

# ── Token tracking ─────────────────────────────────────────────────────────────
# Dynamic — reads OLLAMA_NUM_CTX from runtime_config so the token bar and
# warning thresholds fire at the correct points regardless of .env value.
def _context_limit() -> int:
    return runtime_config.num_ctx

current_token_count = 0

# ── Spinner config ─────────────────────────────────────────────────────────────
_SPINNER       = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_FPS           = 12
_PHRASE_FRAMES = _FPS * 10   # frames before cycling to next phrase

LOADING_PHRASES = [
    "working on it", "hold on", "doing stuff", "computing things",
    "probably fine", "this might take a sec", "not frozen, promise",
    "grepping for answers", "spawning subprocesses", "piping to /dev/brain",
    "forking the timeline", "diffing reality", "cat-ing the void",
    "chmod 777 your request", "segfault: just kidding", "killing -9 uncertainty",
    "bribing the model", "consulting the beaver council",
    "chewing through your request", "negotiating with neurons",
    "the tokens are scared", "yelling into the context window",
    "manifesting an answer", "asking nicely", "vibing computationally",
    "staring at your query", "arguing with the graph",
    "summoning output from thin air", "debugging the universe",
    "rebooting reality", "optimizing", "waiting for inspiration",
    "consulting the oracle", "asking the magic 8-ball",
    "looking for the silver lining",
    # [FIX-23] extra phrases, same tone/style as above
    "sharpening my teeth on this", "building a dam of context",
    "gnawing through the problem", "checking the context window twice",
    "reticulating splines", "asking the rubber duck",
    "counting to infinity, slowly", "convincing the GPU to cooperate",
    "un-fumbling the tokens", "assembling a coherent thought",
    "taming a stray semicolon", "buffering existential dread",
    "polling the universe for updates", "measuring twice, cutting never",
    "downloading more RAM", "aligning the stars and the tensors",
    "whispering to the vector store", "double-checking with myself",
    "letting the KV cache breathe", "consulting past me",
    "sniffing out the answer", "collecting my thoughts (literally)",
    "walking the graph", "waiting on the model, not the coffee",
    "beavering away", "gnashing on gigabytes",
]


def _shuffled_phrases() -> list[str]:
    p = LOADING_PHRASES.copy()
    random.shuffle(p)
    return p


def _mute_stream_handlers() -> list[tuple]:
    log = _logging.getLogger("beaver")
    saved = []
    for h in log.handlers:
        if isinstance(h, _logging.StreamHandler) and not isinstance(h, _logging.FileHandler):
            saved.append((h, h.level))
            h.setLevel(_logging.CRITICAL)
    return saved


def _restore_stream_handlers(saved: list[tuple]) -> None:
    for h, lvl in saved:
        h.setLevel(lvl)


def _active_persona() -> str:
    return getattr(runtime_config, "persona", "standard") or "standard"


def render_header() -> Panel:
    grid = Table.grid(expand=True)
    grid.add_column(justify="left")
    grid.add_column(justify="right")

    left = Text()
    left.append("🦫  beaver", style="bold white")

    right = Text()
    right.append(runtime_config.model, style="bold cyan")
    right.append("  ·  ", style="dim")
    right.append(_active_persona(), style="dim cyan")

    grid.add_row(left, right)
    return Panel(grid, border_style="cyan", padding=(0, 1))


def render_token_bar(current: int, limit: int) -> Text:
    pct    = min(1.0, current / limit) if limit else 0
    width  = 30
    filled = int(width * pct)
    color  = "green" if pct < 0.6 else ("yellow" if pct < 0.85 else "red")

    bar = Text()
    bar.append("  ")
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="dim " + color)
    bar.append(f"  {pct * 100:.0f}%", style="bold " + color)
    bar.append(f"  {current:,} / {limit:,}", style="dim")
    return bar


# ── Terminal UI ────────────────────────────────────────────────────────────────

class TerminalUI:
    def __init__(self) -> None:
        self._animating      = False
        self._stream_buffer: list[tuple[str, str]] = []
        self._graph          = None
        self._history:       list = []
        self._msg_count:     int  = 0
        self._plugin_count:  int  = 0
        self._agent_count:   int  = 0
        tui_bus.subscribe(self.on_stream_event)

    def on_stream_event(self, line: str, stream_type: str) -> None:
        stripped = line.strip()
        if not stripped or stream_type == "thought":
            return
        if self._animating:
            self._stream_buffer.append((stripped, stream_type))
        elif stream_type == "stderr":
            console.print(f"  [red]✗[/red]  [dim]{stripped}[/dim]")
        else:
            console.print(f"  [dim]↳  {stripped}[/dim]")

    def _flush_buffer(self) -> None:
        for line, stream_type in self._stream_buffer:
            if stream_type == "stderr":
                console.print(f"  [red]✗[/red]  [dim]{line}[/dim]")
            else:
                console.print(f"  [dim]↳  {line}[/dim]")
        self._stream_buffer.clear()

    # ── Streaming invocation ───────────────────────────────────────────────────

    async def _invoke_streaming(self, inputs: dict, config: dict) -> dict:
        """Stream LLM tokens + tool activity as ONE growing transcript panel
        for the whole turn — nothing is ever removed or swapped out.

        FIX-10: background asyncio.Task ticks the spinner at _FPS so it
                animates even when astream_events is blocked on a tool.
        FIX-11: spinner + phrase shown immediately; switches to text panel
                only when the first token chunk arrives.
        FIX-12: on_chain_end filtered to root graph only; last event wins.

        [FIX-21] Rewritten again from FIX-18's multiple opened/closed Live()
                phases to a SINGLE Live() region for the entire turn. FIX-18
                fixed the duplication bug but still visually separated each
                phase into its own panel, and — worse — a generation with no
                text at all (a pure tool call, no narration first) left a
                dead, frozen spinner frame committed to the scrollback with
                nothing useful in it once that phase closed. Now: one Live()
                opens at the start of the turn and stays open until the turn
                ends. Internally it holds an ordered list of `segments`
                (Text renderables) that only ever grows — thinking phrase,
                streamed text, tool call, tool result, repeat — all stacked
                inside a single Panel via rich.console.Group. The one
                currently in progress updates in place each tick; once a
                phase finishes it's frozen (no more updates) but stays
                exactly where it is — nothing is removed, nothing flashes
                and vanishes. A thinking segment that ends with zero text
                (the pure-tool-call case) is dropped rather than committed
                as a dead frame, since it never showed anything meaningful.
        """
        global current_token_count

        _saved = _mute_stream_handlers()
        self._animating = True
        self._stream_buffer.clear()

        response_text  = ""
        final_result   = None
        first_token    = False        # FIX-11: have we left spinner phase?
        frame_idx      = 0
        phrases        = _shuffled_phrases()

        # [FIX-21] Growing, never-shrinking list of finished + in-progress
        # renderables for the whole turn. segments[cur_idx] is the only one
        # ever mutated; everything before it is frozen.
        segments: List[Text] = []
        cur_idx        = -1
        phase          = "thinking"   # "thinking" | "tool"
        cur_tool_name  = ""
        cur_tool_args  = ""
        tool_start_ts  = 0.0

        # ── Render helpers ─────────────────────────────────────────────────────

        def _current_phrase() -> str:
            return phrases[(frame_idx // _PHRASE_FRAMES) % len(phrases)]

        def _spinner_text(label: str = "") -> Text:
            spinner = _SPINNER[frame_idx % len(_SPINNER)]
            t = Text(justify="center")   # [FIX-23] center the loading phrase
            t.append(f"{spinner} ", style="bold cyan")
            t.append(label or _current_phrase(), style="italic")
            return t

        def _streaming_text(text: str, done: bool = False) -> Text:
            # [FIX-ARABIC-WIRE] cli/cli_text_render.py's display_text() —
            # Arabic reshaping + bidi reordering for correct terminal
            # display — was fully implemented (and cli/text_render.py has
            # an older, superseded copy of the same fix) but neither was
            # ever imported or called anywhere in this file. Every Arabic
            # response was rendered raw: disconnected letter forms in
            # visually-reversed order, exactly the failure mode the
            # module's own docstring warns about. display_text() no-ops
            # instantly on non-Arabic text (a regex miss), so this is safe
            # to call on every streaming tick.
            text = display_text(text)
            t = Text(overflow="fold")
            if not done:
                t.append("🦫 ", style="dim")
            t.append(text, style=("bright_white" if done else "bright_black"))
            if not done:
                t.append(" ▌", style="dim blink")
            return t

        def _tool_running_text(name: str, args: str) -> Text:
            spinner = _SPINNER[frame_idx % len(_SPINNER)]
            t = Text(overflow="fold")
            t.append(f"{spinner} ", style="bold yellow")
            t.append(name, style="bold white")
            if args:
                t.append(f"({args[:200]})", style="dim")
            return t

        def _tool_done_text(name: str, args: str, output: str, duration: float, ok: bool) -> Text:
            icon, style = ("✓", "green") if ok else ("✗", "red")
            t = Text(overflow="fold")
            t.append(f"{icon} ", style=f"bold {style}")
            t.append(name, style="bold white")
            if args:
                t.append(f"({args[:200]})", style="dim")
            t.append(f"   {duration:.2f}s", style="dim")
            preview = (output or "").strip()
            if preview:
                t.append("\n")
                lines = preview.splitlines()[:6]
                # [FIX-ARABIC-WIRE] Same fix as _streaming_text above — a
                # file read or command output containing Arabic text was
                # equally affected.
                t.append(display_text("\n".join(lines)[:600]), style="dim white")
                if len(preview.splitlines()) > 6 or len(preview) > 600:
                    t.append("\n…", style="dim")
            return t

        def _stringify_tool_output(output: Any) -> str:
            if output is None:
                return ""
            content = getattr(output, "content", None)
            if content is not None:
                if isinstance(content, list):
                    return " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                return str(content)
            return str(output)

        def _render_panel() -> Panel:
            spaced: List[Text] = []
            for i, seg in enumerate(segments):
                if i > 0:
                    spaced.append(Text(""))   # blank line between segments
                spaced.append(seg)
            return Panel(
                Group(*spaced) if spaced else Text(""),
                title="[dim]🦫  beaver[/dim]",
                title_align="left",
                border_style="cyan",
                padding=(1, 2),
            )

        # ── Segment management — [FIX-21] ────────────────────────────────────────
        # cur_idx always points at the one segment still being updated; every
        # prior index is frozen. Starting a new phase either appends a new
        # segment (cur_idx += 1) or, for the empty-thinking case, drops the
        # dead one instead of freezing it.

        def _start_thinking() -> None:
            nonlocal cur_idx, response_text, first_token, phase
            response_text = ""
            first_token   = False
            phase         = "thinking"
            segments.append(_spinner_text())
            cur_idx = len(segments) - 1

        def _finish_thinking(is_tool_call: bool = False) -> None:
            nonlocal cur_idx
            if cur_idx < 0 or cur_idx >= len(segments):
                return
            # [FIX-JSON-UI] [FIX-21] originally dropped the segment only when
            # response_text was empty — true for a pure tool-call generation
            # under bind_tools()'s native function-calling, where the call
            # lives in additional_kwargs and .content is empty. Since tool
            # calls are now written as JSON directly in the streamed text
            # (see agent/graph.py _format_tool_list/_extract_json_tool_call),
            # response_text for a tool-call generation is the raw
            # {"tool": ..., "args": {...}} blob — never empty — so that
            # check alone no longer catches it. The on_tool_start handler
            # unambiguously knows a tool call just happened, so it now
            # passes is_tool_call=True to force the drop instead of
            # freezing the raw JSON into the log as if it were narration.
            if response_text and not is_tool_call:
                segments[cur_idx] = _streaming_text(response_text, done=True)
            else:
                # Pure tool-call generation, no narration text worth keeping
                # — drop the dead spinner frame instead of freezing it.
                segments.pop(cur_idx)
                cur_idx -= 1

        def _start_tool(name: str, args: str) -> None:
            nonlocal cur_idx, cur_tool_name, cur_tool_args, tool_start_ts, phase
            cur_tool_name = name
            cur_tool_args = args
            tool_start_ts = time.time()
            phase         = "tool"
            segments.append(_tool_running_text(name, args))
            cur_idx = len(segments) - 1

        def _finish_tool(output: str, ok: bool = True) -> None:
            nonlocal cur_idx
            duration = time.time() - tool_start_ts
            if 0 <= cur_idx < len(segments):
                segments[cur_idx] = _tool_done_text(cur_tool_name, cur_tool_args, output, duration, ok)

        # ── FIX-10: background ticker ──────────────────────────────────────────
        ticker_stop = asyncio.Event()
        live_ref: List[Live] = []

        async def _ticker():
            nonlocal frame_idx
            while not ticker_stop.is_set():
                await asyncio.sleep(1 / _FPS)
                frame_idx += 1
                if not live_ref or cur_idx < 0 or cur_idx >= len(segments):
                    continue
                if phase == "thinking" and not first_token:
                    segments[cur_idx] = _spinner_text()
                    live_ref[0].update(_render_panel())
                elif phase == "tool":
                    segments[cur_idx] = _tool_running_text(cur_tool_name, cur_tool_args)
                    live_ref[0].update(_render_panel())

        try:
            with Live(
                console=console, refresh_per_second=_FPS + 4,
                transient=False, vertical_overflow="visible",
            ) as live:
                live_ref.append(live)
                _start_thinking()
                live.update(_render_panel())

                ticker_task = asyncio.create_task(_ticker())

                try:
                    async for event in self._graph.astream_events(
                        inputs, config=config, version="v2"
                    ):
                        etype = event.get("event", "")
                        data  = event.get("data",  {})

                        # ── New generation started ──────────────────────────────
                        if etype == "on_chat_model_start":
                            if phase != "thinking":
                                _start_thinking()
                                live.update(_render_panel())

                        # ── Token chunk ────────────────────────────────────────
                        elif etype == "on_chat_model_stream":
                            chunk = data.get("chunk")
                            if chunk and hasattr(chunk, "content") and chunk.content:
                                if isinstance(chunk.content, str):
                                    response_text += chunk.content
                                elif isinstance(chunk.content, list):
                                    for part in chunk.content:
                                        if isinstance(part, dict) and part.get("type") == "text":
                                            response_text += part.get("text", "")

                            if phase == "thinking" and response_text and 0 <= cur_idx < len(segments):
                                first_token = True   # FIX-11
                                segments[cur_idx] = _streaming_text(response_text)
                                live.update(_render_panel())

                        # ── Token count — handles chat + non-chat LLMs ────────
                        elif etype in ("on_chat_model_end", "on_llm_end"):
                            output = data.get("output")
                            tokens = 0
                            if output is not None:
                                # [FIX-TOKCOUNT] usage_metadata.total_tokens is
                                # input_tokens + output_tokens (LangChain's
                                # UsageMetadata schema) — it is NOT an
                                # output-token count. current_token_count is a
                                # running per-session accumulator of output
                                # tokens only (that's what render_token_bar
                                # displays as "context used"), so falling back
                                # to total_tokens here silently added the
                                # prompt length into the running total on any
                                # provider that populates total_tokens without
                                # output_tokens, inflating the displayed
                                # context usage every single turn. Removed —
                                # this now correctly falls through to the
                                # eval_count / word-count estimates below,
                                # both of which are genuinely output-only.
                                usage = getattr(output, "usage_metadata", None) or {}
                                tokens = usage.get("output_tokens") or 0
                                if not tokens:
                                    meta = getattr(output, "response_metadata", None) or {}
                                    tokens = meta.get("eval_count", 0)
                                if not tokens:
                                    llm_out    = getattr(output, "llm_output", None) or {}
                                    token_meta = llm_out.get("model_extra", {}) or {}
                                    tokens     = token_meta.get("eval_count", 0)
                            if tokens:
                                current_token_count += tokens
                            elif response_text:
                                current_token_count += max(
                                    1, len(response_text.split()) * 4 // 3
                                )

                        # ── Tool started ───────────────────────────────────────
                        elif etype == "on_tool_start":
                            _finish_thinking(is_tool_call=True)
                            name = event.get("name", "tool")
                            args = str(data.get("input", "") or "")
                            _start_tool(name, args)
                            live.update(_render_panel())

                        # ── Tool finished ──────────────────────────────────────
                        elif etype == "on_tool_end":
                            output = _stringify_tool_output(data.get("output"))
                            _finish_tool(output, ok=True)
                            live.update(_render_panel())

                        elif etype == "on_tool_error":
                            err = str(data.get("error", "unknown error"))
                            _finish_tool(err, ok=False)
                            live.update(_render_panel())

                        # ── FIX-12: root chain_end only ────────────────────────
                        elif etype == "on_chain_end":
                            output    = data.get("output", {})
                            node_name = event.get("name", "") or ""
                            if (
                                isinstance(output, dict)
                                and "messages" in output
                                and node_name in ("", "agent", "__root__")
                            ):
                                final_result = output   # last match wins

                finally:
                    ticker_stop.set()
                    await ticker_task
                    if phase == "thinking":
                        _finish_thinking()
                    # [FIX-23] _finish_thinking() correctly drops an empty
                    # spinner-only segment when a TOOL CALL follows it (that
                    # case has nothing worth showing). But if the model's
                    # final generation of the whole turn was also empty —
                    # no text, no tool call — dropping it left literally
                    # nothing rendered: the turn just silently ended with a
                    # blank panel and no feedback at all. Always show
                    # something for the terminal case.
                    if not segments:
                        segments.append(Text("(empty response — the model returned nothing)", style="dim italic"))
                    live.update(_render_panel())

        finally:
            self._animating = False
            _restore_stream_handlers(_saved)
            self._flush_buffer()

        # Fallback if chain_end never fired with a full result
        if final_result is None:
            from langchain_core.messages import AIMessage
            final_result = {
                "messages": self._history + [AIMessage(content=response_text)]
            }

        return final_result

    # ── /models ────────────────────────────────────────────────────────────────

    async def _cmd_models(self) -> None:
        with console.status("[dim]contacting ollama…[/dim]", spinner="dots"):
            try:
                models = await runtime_config.list_ollama_models_async()
            except Exception as e:
                console.print(f"\n  [red]error:[/red] ollama unreachable — {e}\n")
                return
        if not models:
            console.print("\n  [dim]no models found[/dim]\n")
            return
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column()
        table.add_column(style="dim")
        for m in sorted(models):
            active = m == runtime_config.model
            table.add_row(
                f"[bold cyan]{m}[/bold cyan]" if active else m,
                "[cyan]● active[/cyan]" if active else "",
            )
        console.print()
        console.print(table)
        console.print("\n  [dim]switch with[/dim] [white]/model <n>[/white]\n")

    # ── /personas ──────────────────────────────────────────────────────────────

    async def _cmd_personas(self) -> None:
        current = _active_persona()
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column()
        table.add_column(style="dim")
        table.add_column()
        for name in PERSONA_TOOLS.keys():
            active    = name == current
            prompt_ok = (PERSONAS_PROMPTS_DIR / f"{name}.md").exists()
            warn      = "[yellow]⚠ no prompt[/yellow]" if not prompt_ok else ""
            table.add_row(
                f"[bold cyan]{name}[/bold cyan]" if active else name,
                "[cyan]● active[/cyan]" if active else "",
                warn,
            )
        console.print()
        console.print(table)
        console.print("\n  [dim]switch with[/dim] [white]/persona <n>[/white]\n")

    # ── /plugins ──────────────────────────────────────────────────────────────

    def _cmd_plugins(self) -> None:
        try:
            from skills.plugin_loader import list_plugins
            plugins = list_plugins()
        except Exception as exc:
            console.print(f"\n  [red]error loading plugins:[/red] {exc}\n")
            return

        if not plugins:
            console.print(
                "\n  [dim]no plugins found — drop .py files into the "
                "[white]plugins/[/white] directory[/dim]\n"
            )
            return

        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("plugin",  style="white",  no_wrap=True)
        table.add_column("persona", style="cyan",   no_wrap=True)
        table.add_column("enabled", justify="center")
        table.add_column("tools",   style="dim")

        self._plugin_count = 0
        for p in plugins:
            if "error" in p:
                table.add_row(
                    p["name"], "—", "[red]error[/red]", f"[red]{p['error']}[/red]"
                )
                continue

            enabled     = p.get("enabled", True)
            persona_val = p.get("persona", "?")
            persona_str = (
                ", ".join(persona_val) if isinstance(persona_val, list) else str(persona_val)
            )
            tools_str   = ", ".join(p.get("tools", [])) or "[dim]none[/dim]"
            enabled_str = "[green]yes[/green]" if enabled else "[dim]no[/dim]"

            if enabled:
                self._plugin_count += 1

            table.add_row(p["name"], persona_str, enabled_str, tools_str)

        console.print()
        console.print(table)
        console.print(
            "\n  [dim]to add a plugin drop a .py file into [white]plugins/[/white]"
            " — no restart needed[/dim]\n"
        )

    # ── /agents ───────────────────────────────────────────────────────────────

    def _cmd_agents(self) -> None:
        try:
            from skills.a2a import _load_agent_configs, _MAX_CONCURRENT, _A2A_TIMEOUT
            configs = _load_agent_configs()
        except Exception as exc:
            console.print(f"\n  [red]error loading agents:[/red] {exc}\n")
            return

        if not configs:
            console.print(
                "\n  [dim]no agents configured — set [white]A2A_AGENTS[/white] "
                "in .env or pull Ollama models[/dim]\n"
            )
            return

        self._agent_count = len(configs)

        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("name",    style="white",  no_wrap=True)
        table.add_column("persona", style="cyan",   no_wrap=True)
        table.add_column("model",   style="magenta")

        for cfg in configs:
            table.add_row(
                cfg.get("name",    "?"),
                cfg.get("persona", "standard"),
                cfg.get("model",   "?"),
            )

        console.print()
        console.print(table)
        console.print(
            f"\n  [dim]max_concurrent=[white]{_MAX_CONCURRENT}[/white]  "
            f"timeout=[white]{_A2A_TIMEOUT}s[/white][/dim]\n"
            "  [dim]delegate with[/dim] [white]/persona orchestrator[/white] "
            "[dim]then ask normally[/dim]\n"
        )

    # ── /mcp ──────────────────────────────────────────────────────────────────

    async def _cmd_mcp(self, reload: bool = False) -> None:
        """List configured MCP servers and optionally force-reconnect them.

        /mcp        — show all servers declared in MCP_SERVERS (.env)
        /mcp reload — tear down existing connections and reconnect for the
                      active persona so server / .env changes take effect
                      without restarting Beaver.
        """
        from skills.mcp_loader import list_mcp_servers, _release_mcp_connections, _cached_persona

        if reload:
            console.print("\n  [dim]reloading MCP connections…[/dim]")
            try:
                await _release_mcp_connections()
                # Clear cached persona so next tool-load forces a reconnect
                import skills.mcp_loader as _mcp_mod
                _mcp_mod._cached_persona = ""
                _mcp_mod._cached_tools   = []

                persona = _active_persona()
                from skills import invalidate_persona_tools_cache
                invalidate_persona_tools_cache(persona)  # [FIX-CACHE] see skills/__init__.py
                with console.status("[dim]connecting to MCP servers…[/dim]", spinner="dots"):
                    from skills.mcp_loader import get_mcp_tools
                    tools = await get_mcp_tools(persona)

                if tools:
                    tool_names = ", ".join(t.name for t in tools)
                    console.print(
                        f"  [green]✓[/green]  [white]{len(tools)}[/white] tool(s) loaded: "
                        f"[dim]{tool_names}[/dim]\n"
                    )
                else:
                    console.print("  [dim]no MCP tools loaded (check MCP_SERVERS in .env)[/dim]\n")
            except Exception as exc:
                console.print(f"\n  [red]reload failed:[/red] {exc}\n")
            return

        servers = list_mcp_servers()
        if not servers:
            console.print(
                "\n  [dim]no MCP servers configured — add entries to "
                "[white]MCP_SERVERS[/white] in [white].env[/white]\n"
                "\n  example:\n"
                "  [dim]MCP_SERVERS=[\n"
                '    {"name":"filesystem","cmd":["npx","-y","@modelcontextprotocol/server-filesystem","/home"]},\n'
                '    {"name":"github","cmd":["npx","-y","@modelcontextprotocol/server-github"],'
                '"env":{"GITHUB_TOKEN":"ghp_xxx"}}\n'
                "  ][/dim]\n"
            )
            return

        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("name",    style="white",  no_wrap=True)
        table.add_column("persona", style="cyan",   no_wrap=True)
        table.add_column("command", style="dim")

        for srv in servers:
            persona_val = srv.get("persona", "*")
            if isinstance(persona_val, list):
                persona_str = ", ".join(persona_val)
            else:
                persona_str = str(persona_val)

            table.add_row(
                srv.get("name", "?"),
                persona_str,
                srv.get("cmd", ""),
            )

        # Show live tool cache status
        import skills.mcp_loader as _mcp_mod
        cached_count = len(_mcp_mod._cached_tools)
        cached_persona = _mcp_mod._cached_persona or "—"
        connected = _mcp_mod._active_stack is not None

        console.print()
        console.print(table)
        console.print(
            f"\n  [dim]connections:[/dim] "
            + ("[green]live[/green]" if connected else "[dim]not connected[/dim]")
            + f"  [dim]cached tools:[/dim] [white]{cached_count}[/white]"
            + f"  [dim]for persona:[/dim] [cyan]{cached_persona}[/cyan]"
        )
        console.print(
            "  [dim]reconnect with[/dim] [white]/mcp reload[/white]  "
            "[dim]·  tools auto-load when you send a message[/dim]\n"
        )

    async def _cmd_mcp_check(self) -> None:
        """Run the MCP import + config diagnostic and print a full report."""
        console.print("\n  [dim]running MCP diagnostic…[/dim]")
        from skills.mcp_loader import diagnose_mcp
        report = await diagnose_mcp()
        for line in report.splitlines():
            console.print(f"  {line}")
        console.print()

    # ── /hud ──────────────────────────────────────────────────────────────────

    def _cmd_hud(self, session_id: str = "cli") -> None:
        persona = _active_persona()
        tools   = PERSONA_TOOLS.get(persona, [])
        # [FIX-HUD-DIR] focus_dir defaults to the literal string "unset" in
        # render_hud() and was never passed here, so the HUD's "dir" field
        # showed "unset" unconditionally — even though /dir (a separate
        # command right next to this one) correctly reports the real,
        # live workspace root via the exact same import. Lazy-imported
        # (not a top-level import) to match /dir's own pattern and always
        # reflect the current value after a /dir <path> change, not the
        # value at process start.
        from skills.file_ops import WORKSPACE_ROOT
        console.print()
        console.print(
            render_hud(
                state="idle",
                model=runtime_config.model,
                mode=persona,
                focus_dir=str(WORKSPACE_ROOT),
                temp=runtime_config.temperature,
                msg_count=self._msg_count,
                estimated_tokens=current_token_count,
                thread_id=session_id,
                tool_count=len(tools),
                plugin_count=self._plugin_count,
                agent_count=self._agent_count,
            )
        )
        console.print()

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def run_loop(self) -> None:
        global current_token_count
        # FIX-15: unique thread_id per session so each launch starts fresh.
        # Resume a prior session with --thread <id> in main.py one-shot mode.
        _session_id = f"cli-{uuid.uuid4().hex[:12]}"

        async with lifespan_checkpointer() as checkpointer:
            self._graph = create_beaver_graph(checkpointer)
            console.clear()
            console.print(render_header())
            console.print(render_token_bar(current_token_count, _context_limit()))
            console.print()
            console.print(
                "  [dim]/models  /personas  /plugins  /agents  /mcp  /mcp reload  /mcp check  /dir  "
                "/persona <n>  /model <n>  /hud  /reset  exit[/dim]\n"
            )
            config = {
                "configurable": {"thread_id": _session_id},
                # [FIX-RECURSION] See web/server.py's identical fix — without
                # this, LangGraph's own default recursion_limit (25) governs
                # instead of runtime_config.max_loops (32 steps at default),
                # and crashes hard instead of halting gracefully.
                "recursion_limit": (runtime_config.max_loops * 2) + 10,
            }

            while True:
                try:
                    console.print()
                    user_input = console.input("[bold cyan]›[/bold cyan] ").strip()

                    if not user_input:
                        continue
                    low = user_input.lower()

                    if low in ("exit", "quit"):
                        console.print("\n[dim]bye.[/dim]\n")
                        break

                    if low == "/models":
                        await self._cmd_models()
                        continue

                    if low in ("/personas", "/skills"):
                        await self._cmd_personas()
                        continue

                    if low == "/plugins":
                        self._cmd_plugins()
                        continue

                    if low == "/agents":
                        self._cmd_agents()
                        continue

                    # FIX-14: /mcp and /mcp reload
                    if low == "/mcp":
                        await self._cmd_mcp(reload=False)
                        continue

                    if low == "/mcp reload":
                        await self._cmd_mcp(reload=True)
                        continue

                    if low == "/mcp check":
                        await self._cmd_mcp_check()
                        continue

                    if low in ("/hud", "/status", "/monitor"):
                        self._cmd_hud(_session_id)
                        continue

                    # FIX-16: /dir command — change workspace root at runtime
                    if low == "/dir":
                        from skills.file_ops import WORKSPACE_ROOT
                        console.print(f"  [dim]workspace:[/dim] [white]{WORKSPACE_ROOT}[/white]")
                        continue

                    if low.startswith("/dir "):
                        new_dir = user_input.split(None, 1)[1].strip()
                        try:
                            from skills.file_ops import set_workspace
                            resolved = set_workspace(new_dir)
                            console.print(f"  [dim]workspace →[/dim] [bold cyan]{resolved}[/bold cyan]")
                            console.print("  [dim]file tools (read_file, write_file, list_directory) now operate here[/dim]")
                        except ValueError as e:
                            console.print(f"\n  [red]error:[/red] {e}\n")
                        continue

                    # [MISSING-CMD] target_scope had no way to be changed from
                    # either UI — see web/commands.py's cmd_scope for the full
                    # explanation. Same shape as /dir: no-arg shows current
                    # scope, an arg replaces it (comma-separated IPs/CIDRs).
                    if low == "/scope":
                        current = ", ".join(runtime_config.scope) or "(empty — all targets allowed)"
                        console.print(f"  [dim]scope:[/dim] [white]{current}[/white]")
                        continue

                    if low.startswith("/scope "):
                        new_scope = user_input.split(None, 1)[1].strip()
                        runtime_config.set_scope(new_scope)
                        console.print(f"  [dim]scope →[/dim] [bold cyan]{', '.join(runtime_config.scope)}[/bold cyan]")
                        console.print("  [dim]os_exec calls with an IP outside this list/CIDR range will be blocked[/dim]")
                        continue

                    if low.startswith("/model "):
                        name = user_input.split(None, 1)[1].strip()
                        try:
                            await asyncio.to_thread(runtime_config.set_model, name)
                            console.print(f"  [dim]model →[/dim] [bold cyan]{name}[/bold cyan]")
                        except ValueError as e:
                            console.print(f"\n  [red]error:[/red] {e}\n")
                        continue

                    if low.startswith("/persona ") or low.startswith("/skill "):
                        name = user_input.split(None, 1)[1].strip()
                        try:
                            runtime_config.set_persona(name)
                            prompt_path = PERSONAS_PROMPTS_DIR / f"{name}.md"
                            if not prompt_path.exists():
                                console.print(
                                    f"  [yellow]warn:[/yellow] no prompt file for '{name}' "
                                    f"— add prompts/personas/{name}.md"
                                )
                            console.print(f"  [dim]persona →[/dim] [bold cyan]{name}[/bold cyan]")
                            # FIX-14: invalidate MCP cache so the next message reloads
                            # MCP tools filtered for the new persona.
                            import skills.mcp_loader as _mcp_mod
                            if _mcp_mod._cached_persona != name:
                                await _mcp_mod._release_mcp_connections()
                                _mcp_mod._cached_persona = ""
                                _mcp_mod._cached_tools   = []
                                console.print("  [dim]mcp cache cleared — tools reload on next message[/dim]")
                        except ValueError as e:
                            console.print(f"\n  [red]error:[/red] {e}\n")
                        continue

                    if low in ("/persona", "/skill"):
                        console.print(f"  [dim]persona →[/dim] [bold cyan]{_active_persona()}[/bold cyan]")
                        continue

                    if low == "/reset":
                        self._history.clear()
                        self._msg_count     = 0
                        current_token_count = 0   # FIX-9
                        # [FIX-RESET] Clearing local counters/history alone
                        # did NOT reset anything the model sees: the
                        # LangGraph checkpointer persists full conversation
                        # state in SQLite keyed by thread_id (FIX-8: "pass
                        # only the new message — checkpointer holds
                        # history"), and this loop reused the same
                        # thread_id for its whole lifetime (FIX-15 only
                        # rotates it on a fresh launch). So the very next
                        # message after a "reset" still resumed on top of
                        # the complete pre-reset history loaded from disk —
                        # "conversation reset" was cosmetic only. Rotate to
                        # a brand-new thread_id instead, exactly like a
                        # fresh launch — the old thread's rows are left
                        # alone on disk (harmless) rather than deleted.
                        # See web/commands.py's cmd_reset for the same fix
                        # on the web side.
                        _session_id = f"cli-{uuid.uuid4().hex[:12]}"
                        config["configurable"]["thread_id"] = _session_id
                        console.print("  [dim]conversation reset[/dim]")
                        continue

                    # [FIX-23] Unrecognized slash command — previously fell straight
                    # through to the LLM as literal chat text (e.g. "/pentester"
                    # instead of "/persona pentester"), which the model had no real
                    # way to act on and often produced an empty response to. Catch
                    # it here instead of silently sending it to the agent.
                    if low.startswith("/") and low not in ("/help", "/?"):
                        console.print(
                            f"\n  [yellow]unknown command:[/yellow] {user_input}\n"
                            f"  [dim]did you mean[/dim] [white]/persona {low.lstrip('/')}[/white] [dim]?[/dim]\n"
                            f"  [dim]/models  /personas  /plugins  /agents  /mcp  /mcp reload  /mcp check  /dir  /scope  "
                            f"/persona <n>  /model <n>  /hud  /reset  exit[/dim]\n"
                        )
                        continue

                    # ── Agent invocation ───────────────────────────────────────
                    # FIX-8: pass only the new message — checkpointer holds history
                    self._msg_count += 1
                    inputs = {
                        "messages":      [HumanMessage(content=user_input)],
                        "active_persona": _active_persona(),
                        "target_scope":  runtime_config.scope,
                        "iteration":     0,
                    }

                    # ── Langfuse: register parent trace for this turn ──────────
                    _run_trace_id = str(uuid.uuid4())
                    for _cb in get_callbacks():
                        if hasattr(_cb, "set_run_context"):
                            await _cb.set_run_context(
                                trace_id=_run_trace_id,
                                session_id=_session_id,
                                user_input=user_input,
                            )

                    # [FIX-24] Run the turn as its own task and only catch
                    # Ctrl+C around THIS call, not the whole loop body. Before
                    # this fix, a single try/except wrapped both console.input()
                    # (where Ctrl+C should quit) and the agent invocation (where
                    # Ctrl+C should just cancel a slow/stuck turn). A long local
                    # model call (qwen via Ollama can take 100s of seconds, or
                    # hang if a prior call left the client connection poisoned —
                    # see _evict_cached_llm's docstring) meant Ctrl+C during a
                    # turn silently killed the ENTIRE session instead of
                    # returning to the prompt — "frozen, and it never showed the
                    # part where I can continue the convo" is exactly this.
                    turn_task = asyncio.ensure_future(
                        self._invoke_streaming(inputs, config)
                    )
                    try:
                        result = await turn_task
                    except (KeyboardInterrupt, asyncio.CancelledError):
                        turn_task.cancel()
                        try:
                            await turn_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        # The cancelled call may have left the cached Ollama
                        # client mid-request; force a fresh one next turn
                        # instead of risking every future call hanging with
                        # no further error.
                        _evict_cached_llm()
                        console.print(
                            "\n  [yellow]⚠ turn cancelled[/yellow] "
                            "[dim](Ctrl+C) — back to prompt[/dim]\n"
                        )
                        continue
                    last_msg = result["messages"][-1]
                    self._history = list(result["messages"])

                    persona = _active_persona()
                    tools   = PERSONA_TOOLS.get(persona, [])
                    from skills.file_ops import WORKSPACE_ROOT  # [FIX-HUD-DIR]
                    console.print(
                        render_hud(
                            state="idle",
                            model=runtime_config.model,
                            mode=persona,
                            focus_dir=str(WORKSPACE_ROOT),
                            temp=runtime_config.temperature,
                            msg_count=self._msg_count,
                            estimated_tokens=current_token_count,
                            thread_id=_session_id,
                            tool_count=len(tools),
                            plugin_count=self._plugin_count,
                            agent_count=self._agent_count,
                        )
                    )
                    console.print(render_token_bar(current_token_count, _context_limit()))

                except (KeyboardInterrupt, EOFError):
                    console.print("\n[dim]bye.[/dim]\n")
                    break
                except Exception as e:
                    console.print(f"\n  [red]error:[/red] {e}\n")


async def _sigint_responsiveness_wakeup() -> None:
    """[FIX-25] Windows-only mitigation for delayed Ctrl+C delivery.

    asyncio's ProactorEventLoop (the default on Windows) can block for the
    full duration of a single long-running I/O wait — e.g. a 100s+ call to a
    local Ollama model — without ever returning control to the interpreter,
    so a pending SIGINT (Ctrl+C) isn't actually raised until that wait
    finishes on its own. This is a long-standing CPython/asyncio limitation
    on Windows (see bpo-23057), not something specific to this app, but it's
    exactly what made Ctrl+C feel like it did nothing during a slow turn.
    A cheap periodic no-op wakes the loop often enough for signals to be
    checked promptly. Harmless (and unnecessary, but still harmless) on
    non-Windows platforms.
    """
    while True:
        await asyncio.sleep(0.25)


def main() -> None:
    ui = TerminalUI()

    async def _runner() -> None:
        wakeup = asyncio.ensure_future(_sigint_responsiveness_wakeup())
        try:
            await ui.run_loop()
        finally:
            wakeup.cancel()

    asyncio.run(_runner())


if __name__ == "__main__":
    main()
