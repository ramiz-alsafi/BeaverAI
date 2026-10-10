"""web.commands — slash-command dispatch table.

[FIX-RESET] /reset was advertised in /help and the frontend palette but had
no COMMANDS entry, so it always answered "unknown command".
"""
import re
from pathlib import Path

from web.commands import COMMANDS, cmd_help, cmd_reset

ROOT = Path(__file__).resolve().parents[2]


def test_reset_is_registered():
    assert COMMANDS["reset"] is cmd_reset


def test_reset_rotates_to_a_fresh_empty_thread():
    a, b = cmd_reset(), cmd_reset()
    assert a["switch_to_thread"].startswith("web-")
    assert a["switch_to_thread"] != b["switch_to_thread"]
    assert a["replay_messages"] == []          # frontend clears its log on an empty list
    assert a["reset_counters"] is True         # server.py zeroes msg/token counters on this


def test_every_command_advertised_in_help_is_implemented():
    rows = cmd_help()["rows"]
    advertised = {m.group(1) for r in rows if (m := re.match(r"/(\w+)", str(r[0])))}
    assert advertised, "could not parse any commands out of /help"
    assert advertised <= set(COMMANDS), advertised - set(COMMANDS)


def test_every_command_in_the_frontend_palette_is_implemented():
    palette = (ROOT / "web/frontend/src/components/CommandPalette.tsx").read_text()
    in_palette = set(re.findall(r'["\'`]/(\w+)', palette))
    assert in_palette, "could not parse any commands out of CommandPalette.tsx"
    assert in_palette <= set(COMMANDS), in_palette - set(COMMANDS)
