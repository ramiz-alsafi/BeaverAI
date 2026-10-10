"""agent.graph._extract_shell_utility — picks the tool_manuals/<name>.md to inject.

[FIX-UTILITY-PREFIX] "sudo nmap ..." must resolve to nmap (which has a
manual), not sudo (which doesn't).
"""
import pytest

from agent.graph import _extract_shell_utility


@pytest.mark.parametrize(
    "command, expected",
    [
        ("nmap -sV target", "nmap"),
        ("sudo nmap -sS target", "nmap"),
        ("doas tcpdump -i eth0", "tcpdump"),
        ("/usr/bin/nmap -sV target", "nmap"),
        ("sudo /usr/bin/nmap -sV target", "nmap"),
        ("NMAP.EXE -sV target", "nmap"),
        ("C:\\tools\\nmap.exe -sV target", "nmap"),
        ("sudo", "sudo"),      # bare wrapper: don't crash, don't invent a utility
        ("", None),
        ("   ", None),
    ],
)
def test_extract_shell_utility(command, expected):
    assert _extract_shell_utility(command) == expected
