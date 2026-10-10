"""Cross-file consistency guards for things that broke silently before.

Each of these was a real defect that no compiler or type-checker could see.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_requirements_list_the_cli_arabic_rendering_deps():
    """[FIX-REQS-BIDI] cli_text_render degrades silently without these, so a
    plain `pip install -r requirements.txt` left the feature non-functional."""
    reqs = (ROOT / "requirements.txt").read_text().lower()
    src = (ROOT / "cli/cli_text_render.py").read_text()
    assert "import arabic_reshaper" in src and "from bidi" in src
    assert re.search(r"^arabic-reshaper", reqs, re.M)
    assert re.search(r"^python-bidi", reqs, re.M)


def test_env_example_has_no_duplicate_section_headers():
    headers = re.findall(r"^# ── .+$", (ROOT / ".env.example").read_text(), re.M)
    dupes = {h for h in headers if headers.count(h) > 1}
    assert not dupes, dupes


def test_env_example_has_no_smashed_together_assignments():
    """e.g. '#BEAVER_NOTES_DIR=./notesOLLAMA_NUM_CTX=16384' — two vars with the
    newline missing, which sets BEAVER_NOTES_DIR to garbage if uncommented."""
    for n, line in enumerate((ROOT / ".env.example").read_text().splitlines(), 1):
        m = re.match(r"^#?([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            assert not re.search(r"[A-Z][A-Z0-9_]{3,}=", m.group(2)), f"line {n}: {line}"


def test_env_example_documents_every_restart_setting_the_settings_ui_edits():
    tree = ast.parse((ROOT / "web/server.py").read_text())
    restart = next(
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", "") == "RESTART_FIELDS" for t in node.targets)
    )
    env_example = (ROOT / ".env.example").read_text()
    missing = [v for v in restart.values() if not re.search(rf"^#?{v}=", env_example, re.M)]
    assert not missing, missing


def test_no_personal_filesystem_paths_leak_into_shipped_prompts_or_manuals():
    """Prompts and tool_manuals are injected into EVERY user's agent context
    (manuals specifically right after a tool call failed)."""
    leak = re.compile(r"C:[\\/]+Users[\\/]+(?!yourname\b)\w+", re.I)
    offenders = []
    for path in [*(ROOT / "tool_manuals").glob("*.md"), *(ROOT / "prompts").rglob("*.md")]:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if leak.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert not offenders, offenders


def test_readme_does_not_claim_a_ci_workflow_that_does_not_exist():
    readme = (ROOT / "README.md").read_text()
    if "ci.yml" in readme and not (ROOT / ".github/workflows/ci.yml").exists():
        # Allowed only if the README explicitly says it's absent.
        assert re.search(r"(no|doesn't|does not|isn't|there isn't).{0,80}(CI|workflow|\.github)", readme, re.I | re.S)
