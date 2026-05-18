"""Tests for the whitespace-tightening pre-pass in prompt builder."""

from __future__ import annotations

from claudio.pipeline.prompt import _tighten, build_prompt


def test_tighten_collapses_blank_runs():
    src = "a\n\n\n\nb\n\n\n\nc"
    assert _tighten(src) == "a\n\nb\n\nc"


def test_tighten_strips_trailing_whitespace_per_line():
    src = "a   \nb\t\nc"
    assert _tighten(src) == "a\nb\nc"


def test_tighten_strips_leading_and_trailing_blank_lines():
    src = "\n\n  hello\n\n"
    assert _tighten(src).strip() == "hello"


def test_tighten_preserves_single_blank_lines():
    src = "a\n\nb"
    assert _tighten(src) == "a\n\nb"


def test_tighten_handles_empty_input():
    assert _tighten("") == ""


def test_build_prompt_emits_tightened_context():
    p = build_prompt(task="t", context="x\n\n\n\ny   \nz")
    assert "<context>\nx\n\ny\nz\n</context>" in p


def test_build_prompt_skips_context_when_only_whitespace():
    p = build_prompt(task="t", context="\n\n   \n\n")
    assert "<context>" not in p
    assert "<task>t</task>" in p


def test_build_prompt_keeps_task_verbatim_with_whitespace():
    # The user's task should NOT be normalised — quoted whitespace can carry
    # meaning (e.g. error messages).
    p = build_prompt(task="line one\n\n\nline two")
    assert "line one\n\n\nline two" in p
