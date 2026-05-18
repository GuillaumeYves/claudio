"""Tests for parse_need_context (multi-range two-way feedback channel)."""

from __future__ import annotations

from claudio.commands.run_prompt import parse_need_context


def test_no_signal_returns_none():
    assert parse_need_context("Here is your answer.") is None


def test_single_range_returns_list_of_one():
    out = parse_need_context('<need-context file="a.py" lines="10-20" reason="x"/>')
    assert out == [("a.py", "10-20", "x")]


def test_missing_reason_defaults_to_empty():
    out = parse_need_context('<need-context file="a.py" lines="10-20"/>')
    assert out == [("a.py", "10-20", "")]


def test_multi_range_request_is_parsed():
    response = (
        '<need-context file="a.py" lines="10-20" reason="r1"/>\n'
        '<need-context file="b.py" lines="30-40" reason="r2"/>'
    )
    out = parse_need_context(response)
    assert out == [
        ("a.py", "10-20", "r1"),
        ("b.py", "30-40", "r2"),
    ]


def test_mid_response_signal_is_ignored():
    """Claude must START with the tag for it to be a signal -- otherwise
    we'd misinterpret a quoted tag inside prose."""
    response = "Looks fine. As an aside, <need-context file=\"a.py\" lines=\"1-5\"/>"
    assert parse_need_context(response) is None


def test_signal_with_leading_whitespace_works():
    response = '   \n<need-context file="a.py" lines="1-5"/>'
    out = parse_need_context(response)
    assert out == [("a.py", "1-5", "")]
