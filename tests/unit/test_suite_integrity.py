"""Guards on the test suite itself.

Uploading files one at a time through a web UI makes it easy to paste the wrong file's
contents into the right filename. That has happened here: test_plugin_loader.py and
test_prompt_cache.py once held byte-for-byte copies of test_audit.py. Nothing failed —
the audit tests simply ran three times — while the plugin-loader and prompt-cache tests
quietly vanished from the repo. These checks make that loud.
"""
import hashlib
from collections import defaultdict
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _test_modules():
    return sorted(p for p in TESTS.glob("test_*.py") if p.name != Path(__file__).name)


def test_no_two_test_modules_are_identical_copies():
    by_digest = defaultdict(list)
    for p in _test_modules():
        by_digest[hashlib.sha256(p.read_bytes().strip()).hexdigest()].append(p.name)
    dupes = [names for names in by_digest.values() if len(names) > 1]
    assert not dupes, f"identical test files (a paste into the wrong filename?): {dupes}"


def test_every_test_module_actually_contains_tests():
    empty = [p.name for p in _test_modules() if "\ndef test_" not in p.read_text(encoding="utf-8")
             and "\n    def test_" not in p.read_text(encoding="utf-8")]
    assert not empty, f"test modules with no tests: {empty}"


def test_each_test_module_has_a_unique_set_of_test_names():
    """Two files defining the same test names means one is a stale or pasted copy."""
    seen = {}
    clashes = []
    for p in _test_modules():
        names = {line.split("(")[0].replace("def ", "").strip()
                 for line in p.read_text(encoding="utf-8").splitlines() if line.startswith("def test_")}
        for other, other_names in seen.items():
            shared = names & other_names
            if len(shared) >= 5:      # a couple of generic names can coincide; five can't
                clashes.append((p.name, other, len(shared)))
        seen[p.name] = names
    assert not clashes, f"test modules sharing many test names: {clashes}"
