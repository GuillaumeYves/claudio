"""Tests that terseness rules are appended to constraint lists per mode."""

from __future__ import annotations

from claudio.commands.build import _with_terseness as build_terseness
from claudio.commands.ask import _with_terseness as ask_terseness


def test_build_terseness_appends_no_preamble_and_stop():
    out = build_terseness(["Preserve behavior"])
    assert "Preserve behavior" in out
    assert "No preamble" in out
    assert "Stop after the artifact" in out


def test_build_terseness_handles_no_existing_constraints():
    out = build_terseness(None)
    assert out == ["No preamble", "Stop after the artifact"]


def test_ask_terseness_skips_for_question_mode():
    # `general` intent = -question: exploratory, deserves prose. We should
    # not constrain its output.
    out = ask_terseness("general", None)
    assert out is None


def test_ask_terseness_applies_to_review():
    out = ask_terseness("review", ["Severity-ranked"])
    assert "Severity-ranked" in out
    assert "No preamble" in out


def test_ask_terseness_applies_to_debug():
    out = ask_terseness("debug", ["Cause first"])
    assert "Cause first" in out
    assert "Stop after the artifact" in out


def test_terseness_does_not_mutate_input():
    original = ["Preserve behavior"]
    build_terseness(original)
    assert original == ["Preserve behavior"]  # untouched
