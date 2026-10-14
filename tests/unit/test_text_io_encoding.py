"""Static guard: no text-mode file I/O or text subprocess call without an explicit encoding.

Why this is a test and not a style preference: on Windows, open()/Path.read_text()/
Path.write_text() with no `encoding=` use the ANSI code page (cp1252), not UTF-8, and
subprocess(text=True) decodes the child's output the same way. This project is full of
non-ASCII text (Arabic prompts, em dashes, box-drawing in .env.example), so the same code
that works on Linux raises UnicodeDecodeError, or quietly returns mojibake, on Windows.
CI's Windows job finds those one failure at a time, and only when the data happens to
contain a byte cp1252 can't decode; this finds every one on any OS, immediately.

Fix a hit with encoding="utf-8" (add errors="replace" where you'd rather degrade than
raise, e.g. reading arbitrary user files or another program's output).
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "frontend", ".venv", "venv"}
NON_TEXT_OPENERS = {"os", "webbrowser", "Image", "tarfile", "zipfile", "gzip"}   # os.open etc.


def _py_files():
    for p in sorted(ROOT.rglob("*.py")):
        if not (set(p.relative_to(ROOT).parts) & SKIP_DIRS):
            yield p


def _has(call, name):
    return any(k.arg == name for k in call.keywords)


def _mode_of(call, func_is_name):
    mode = None
    pos = 1 if func_is_name else 0
    if len(call.args) > pos and isinstance(call.args[pos], ast.Constant):
        mode = call.args[pos].value
    for k in call.keywords:
        if k.arg == "mode" and isinstance(k.value, ast.Constant):
            mode = k.value.value
    return mode


def find_unspecified_encodings(source: str):
    """Return [(lineno, description)] for risky calls in `source`."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name in ("read_text", "write_text") and not _has(node, "encoding"):
            hits.append((node.lineno, f"{name}() without encoding="))
        elif name == "open":
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in NON_TEXT_OPENERS:
                continue
            mode = _mode_of(node, isinstance(f, ast.Name))
            if (mode is None or "b" not in str(mode)) and not _has(node, "encoding"):
                hits.append((node.lineno, f"open(mode={mode!r}) without encoding="))
        elif name in ("run", "Popen", "check_output", "check_call") and (
            _has(node, "text") or _has(node, "universal_newlines")
        ) and not _has(node, "encoding"):
            hits.append((node.lineno, f"subprocess {name}(text=True) without encoding="))
    return hits


def test_no_text_io_without_an_explicit_encoding():
    offenders = [
        f"{p.relative_to(ROOT).as_posix()}:{ln}  {what}"
        for p in _py_files()
        for ln, what in find_unspecified_encodings(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, "implicit (locale) text encoding — breaks on Windows:\n  " + "\n  ".join(offenders)


# ── the detector itself: it must catch what it claims to, and not cry wolf ──

def test_detector_catches_each_risky_pattern():
    src = (
        "from pathlib import Path\n"
        "import subprocess\n"
        "Path('a').read_text()\n"
        "(Path('a') / 'b').write_text('x')\n"
        "open('f')\n"
        "open('f', 'w')\n"
        "subprocess.run(['x'], text=True)\n"
    )
    assert [ln for ln, _ in find_unspecified_encodings(src)] == [3, 4, 5, 6, 7]


def test_detector_leaves_safe_code_alone():
    src = (
        "from pathlib import Path\n"
        "import os, subprocess\n"
        "Path('a').read_text(encoding='utf-8')\n"
        "Path('a').write_text('x', encoding='utf-8')\n"
        "open('f', 'rb')\n"
        "open('f', mode='wb')\n"
        "open('f', encoding='utf-8')\n"
        "os.open('f', os.O_RDONLY)\n"
        "subprocess.run(['x'], capture_output=True)\n"
        "subprocess.run(['x'], text=True, encoding='utf-8')\n"
    )
    assert find_unspecified_encodings(src) == []
