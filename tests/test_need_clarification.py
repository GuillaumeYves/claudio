"""Tests for parse_need_clarification (back-channel for ambiguous tasks)."""

from __future__ import annotations

from claudio.commands.run_prompt import parse_need_clarification


def test_no_signal_returns_none():
    assert parse_need_clarification("here's your answer") is None


def test_clarification_question_extracted():
    out = parse_need_clarification(
        '<need-clarification question="rename to camelCase or snake_case?"/>'
    )
    assert out == "rename to camelCase or snake_case?"


def test_clarification_with_leading_whitespace():
    out = parse_need_clarification(
        '   \n<need-clarification question="A or B?"/>'
    )
    assert out == "A or B?"


def test_clarification_mid_response_ignored():
    """Quoted tag in prose must NOT trigger the signal."""
    response = "Looks good. As an aside, <need-clarification question=\"x\"/>"
    assert parse_need_clarification(response) is None


def test_malformed_clarification_returns_none():
    """No `question` attribute -> not a valid signal."""
    assert parse_need_clarification("<need-clarification/>") is None


def test_empty_question_returns_none():
    assert parse_need_clarification('<need-clarification question=""/>') is None
