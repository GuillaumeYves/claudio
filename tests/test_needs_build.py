"""Tests for the read-only -> build escalation signal.

ask/question/debug are read-only: Claude can't mutate from them. When a request
actually needs a write, Claude emits <needs-build/> and claudio offers to switch
the user into build mode. These cover the parser (run_prompt.parse_needs_build)
and the prompt-builder wiring that tells Claude the signal exists.
"""

from __future__ import annotations

from claudio.commands.run_prompt import parse_needs_build
from claudio.pipeline.prompt import build_prompt


def test_no_signal_returns_none():
    assert parse_needs_build("Here's the review: looks solid.") is None


def test_generate_with_reason():
    assert parse_needs_build(
        '<needs-build mode="generate" reason="writing a new file is a mutation"/>'
    ) == ("generate", "writing a new file is a mutation")


def test_refactor_with_reason():
    assert parse_needs_build(
        '<needs-build mode="refactor" reason="edit existing code"/>'
    ) == ("refactor", "edit existing code")


def test_bare_tag_defaults_to_generate():
    assert parse_needs_build("<needs-build/>") == ("generate", "")


def test_unknown_mode_falls_back_to_generate():
    assert parse_needs_build('<needs-build mode="destroy"/>') == ("generate", "")


def test_leading_whitespace_ok():
    assert parse_needs_build('  \n<needs-build mode="refactor"/>') == ("refactor", "")


def test_mid_response_tag_ignored():
    """A quoted tag in prose must not trigger the signal."""
    resp = "Looks good. You could later <needs-build/> from build mode."
    assert parse_needs_build(resp) is None


def test_protocol_present_when_escalation_on():
    prompt = build_prompt(task="review this", intent="review", readonly_escalation=True)
    assert "<needs-build" in prompt
    assert "READ-ONLY" in prompt


def test_protocol_absent_when_escalation_off():
    prompt = build_prompt(task="refactor this", intent="refactor", readonly_escalation=False)
    assert "<needs-build" not in prompt
