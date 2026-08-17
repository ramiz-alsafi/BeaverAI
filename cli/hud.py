"""
Beaver HUD - System telemetry panel.
"""

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Status indicator: colored dot + plain word, nothing AI-y
HUD_STATES = {
    "idle":      {"dot": "●", "label": "idle",    "color": "green",       "border": "bright_black"},
    "thinking":  {"dot": "●", "label": "working",  "color": "yellow",      "border": "yellow"},
    "executing": {"dot": "●", "label": "running",  "color": "cyan",        "border": "cyan"},
    "error":     {"dot": "●", "label": "error",    "color": "red",         "border": "red"},
}


def render_hud(
    state: str = "idle",
    model: str = "qwen2.5:7b",
    mode: str = "standard",
    focus_dir: str = "unset",
    temp: float = 0.0,
    msg_count: int = 0,
    estimated_tokens: int = 0,
    thread_id: str = "default",
    tool_count: int = 0,
    plugin_count: int = 0,
    agent_count: int = 0,
) -> Panel:
    """System telemetry panel for Beaver."""

    cfg = HUD_STATES.get(state, HUD_STATES["idle"])

    table = Table(show_header=False, expand=True, box=None, padding=(0, 2))
    table.add_column(justify="left")
    table.add_column(justify="left")

    col1 = (
        f"[dim]model[/dim]    [white]{model}[/white]\n"
        f"[dim]mode[/dim]     [cyan]{mode.lower()}[/cyan]\n"
        f"[dim]thread[/dim]   [dim white]{thread_id}[/dim white]\n"
        f"[dim]tools[/dim]    [white]{tool_count}[/white]  "
        f"[dim]plugins[/dim] [white]{plugin_count}[/white]  "
        f"[dim]agents[/dim] [white]{agent_count}[/white]"
    )

    col2 = (
        f"[dim]temp[/dim]     [magenta]{temp:.2f}[/magenta]\n"
        f"[dim]tokens[/dim]   [yellow]{estimated_tokens:,}[/yellow]  [dim]{msg_count} msgs[/dim]\n"
        f"[dim]dir[/dim]      [dim]{focus_dir}[/dim]"
    )

    table.add_row(col1, col2)

    title = Text()
    title.append("🦫  beaver  ", style="bold white")
    title.append(cfg["dot"], style=f"bold {cfg['color']}")
    title.append(f"  {cfg['label']}", style="dim")

    return Panel(
        table,
        title=title,
        border_style=cfg["border"],
        padding=(0, 1),
        expand=True,
    )


# Keep backward-compatible alias
render_cyber_hud = render_hud