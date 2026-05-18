"""Tests for the streaming event parser.

_parse_stream_event returns (kind, payload):
  ('text', text)  -> stream-rendered response
  ('tool', label) -> tool_use surfaced via spinner / breadcrumb
  ('', '')        -> noise (system, message_stop, malformed, etc.)
"""

from __future__ import annotations

import json

from claudio.executor import (
    _parse_stream_event,
    _summarise_tool_input,
    _tool_status_label,
)


# ---- claude CLI assistant-snapshot format (primary path) ---------------

def test_parses_assistant_text_block():
    event = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Hello world"}]},
    })
    assert _parse_stream_event(event) == ("text", "Hello world")


def test_skips_thinking_block_within_assistant():
    event = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "thinking", "thinking": "internal chain of thought",
             "signature": "abc..."},
            {"type": "text", "text": "the actual answer"},
        ]},
    })
    assert _parse_stream_event(event) == ("text", "the actual answer")


def test_assistant_with_only_thinking_emits_nothing():
    event = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "thinking", "thinking": "...", "signature": "..."},
        ]},
    })
    assert _parse_stream_event(event) == ("", "")


def test_assistant_emits_tool_label_for_read():
    event = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": "/abs/path/main.py"}},
        ]},
    })
    kind, payload = _parse_stream_event(event)
    assert kind == "tool"
    assert "reading" in payload
    assert "main.py" in payload


def test_assistant_emits_tool_label_for_bash():
    event = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "ls -la"}},
        ]},
    })
    kind, payload = _parse_stream_event(event)
    assert kind == "tool"
    assert "running shell" in payload
    assert "ls -la" in payload


def test_assistant_text_wins_when_both_present():
    """When a snapshot has both text and tool blocks, the text is what the
    user sees this turn. The tool event will be re-surfaced in the next
    snapshot. Returning text keeps the streaming flow clean."""
    event = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "Reading the file."},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
        ]},
    })
    kind, payload = _parse_stream_event(event)
    assert kind == "text"
    assert payload == "Reading the file."


def test_assistant_with_empty_content_returns_empty():
    event = json.dumps({"type": "assistant", "message": {"content": []}})
    assert _parse_stream_event(event) == ("", "")


def test_assistant_with_no_message_returns_empty():
    event = json.dumps({"type": "assistant"})
    assert _parse_stream_event(event) == ("", "")


# ---- skipped event types ----------------------------------------------

def test_skips_system_init():
    event = json.dumps({"type": "system", "subtype": "init", "session_id": "x"})
    assert _parse_stream_event(event) == ("", "")


def test_skips_rate_limit_event():
    event = json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "allowed_warning"},
    })
    assert _parse_stream_event(event) == ("", "")


def test_skips_user_tool_result_echo():
    event = json.dumps({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": "file body..."}]},
    })
    assert _parse_stream_event(event) == ("", "")


def test_skips_result_event():
    event = json.dumps({"type": "result", "subtype": "success", "result": "all the text"})
    assert _parse_stream_event(event) == ("", "")


def test_skips_message_stop():
    assert _parse_stream_event(json.dumps({"type": "message_stop"})) == ("", "")


# ---- fallbacks for other event shapes ---------------------------------

def test_falls_back_to_content_block_delta():
    event = json.dumps({
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": "hi "},
    })
    assert _parse_stream_event(event) == ("text", "hi ")


def test_falls_back_to_simple_text_event():
    assert _parse_stream_event(json.dumps({"type": "text", "text": "x"})) == ("text", "x")


# ---- malformed / unexpected ------------------------------------------

def test_handles_malformed_json():
    assert _parse_stream_event("not json at all") == ("", "")
    assert _parse_stream_event("{not json") == ("", "")
    assert _parse_stream_event("") == ("", "")


def test_handles_non_dict_top_level():
    assert _parse_stream_event(json.dumps([1, 2, 3])) == ("", "")


# ---- _tool_status_label ----------------------------------------------

def test_tool_label_known_verb_for_read():
    assert _tool_status_label("Read", {"file_path": "main.py"}) == "reading main.py"


def test_tool_label_known_verb_for_grep():
    assert _tool_status_label("Grep", {"pattern": "TODO"}) == "searching TODO"


def test_tool_label_unknown_tool_falls_back_to_name():
    assert _tool_status_label("MysteryTool", {"foo": "bar"}) == "running MysteryTool"


# ---- _summarise_tool_input -------------------------------------------

def test_summarise_read_returns_path():
    assert _summarise_tool_input("Read", {"file_path": "main.py"}) == "main.py"


def test_summarise_bash_returns_first_line_truncated():
    long = "echo " + "x" * 200
    out = _summarise_tool_input("Bash", {"command": long})
    assert out.endswith("...")
    assert len(out) <= 63  # 60 chars + "..."


def test_summarise_grep_returns_pattern():
    assert _summarise_tool_input("Grep", {"pattern": "TODO"}) == "TODO"


def test_summarise_unknown_tool_returns_empty():
    assert _summarise_tool_input("MysteryTool", {"foo": "bar"}) == ""


def test_summarise_handles_non_dict_input():
    assert _summarise_tool_input("Read", None) == ""
    assert _summarise_tool_input("Read", "string") == ""
