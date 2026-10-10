"""agent.graph._extract_json_tool_call — the JSON-in-text tool-call parser.

Small quantised local models don't reliably emit strict JSON, so the parser
has to tolerate the common malformed shapes ([FIX-21]) without ever
mistaking plain narration for a tool call.
"""
import pytest

from agent.graph import _extract_json_tool_call


def test_strict_json():
    out = _extract_json_tool_call('{"thought": "t", "tool": "list_directory", "args": {"path": "."}}')
    assert out["tool"] == "list_directory"
    assert out["args"] == {"path": "."}


def test_inside_markdown_fence_and_prose():
    text = 'Sure, here you go:\n```json\n{"tool": "get_workspace", "args": {}}\n```\nDone.'
    assert _extract_json_tool_call(text)["tool"] == "get_workspace"


def test_single_quoted_object_is_parsed():
    out = _extract_json_tool_call("{'thought': 'checking', 'tool': 'list_dir', 'args': {'path': '/tmp'}}")
    assert out is not None
    assert out["tool"] == "list_dir"
    assert out["args"] == {"path": "/tmp"}


def test_single_quoted_tool_first():
    out = _extract_json_tool_call("{'tool': 'read_file', 'args': {'path': 'a.txt'}}")
    assert out["tool"] == "read_file"


def test_python_literals_in_args():
    out = _extract_json_tool_call(
        '{"thought": "x", "tool": "os_exec", "args": {"command": "ls", "stream": True, "timeout": None, "x": False}}'
    )
    assert out["args"]["stream"] is True
    assert out["args"]["timeout"] is None
    assert out["args"]["x"] is False


def test_python_literal_words_inside_strings_are_untouched():
    # Word-boundaried normalisation must not corrupt real string values.
    out = _extract_json_tool_call('{"tool": "t", "args": {"reason": "True positive, None found"}}')
    assert out["args"]["reason"] == "True positive, None found"


@pytest.mark.parametrize(
    "text",
    [
        '{"tool": "t", "args": {"msg": "use {braces} carefully"}}',
        "{'tool': 't', 'args': {'msg': 'use {braces} carefully'}}",
    ],
)
def test_braces_inside_string_values_do_not_break_depth_tracking(text):
    out = _extract_json_tool_call(text)
    assert out is not None
    assert out["args"]["msg"] == "use {braces} carefully"


def test_nested_args_and_escaped_quote():
    out = _extract_json_tool_call(r'{"tool": "t", "args": {"a": {"b": [1, 2, {"c": "say \"hi\""}]}}}')
    assert out["args"]["a"]["b"][2]["c"] == 'say "hi"'


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Let me check the database by running a query",
        '{"thought": "only a thought, no tool"}',
        '{"tool": "unterminated", "args": {',
        '{"tool": 123, "args": {}}',
    ],
)
def test_non_tool_calls_return_none(text):
    assert _extract_json_tool_call(text) is None
