"""Pre-flight specification check.

The bar for a finding here is high: a check that cries wolf gets ignored,
and then it protects nothing. So these tests pin the false-positive side
as hard as the true-positive side — a well-specified request must come
back completely silent.

The other invariant: the check never rewrites the prompt. It reports, and
the user's words go through untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from claudio import spec_check


@dataclass
class FakeFile:
    path: str
    lines: str | None = None


def codes(*args, **kwargs) -> set[str]:
    return {f.code for f in spec_check.check(*args, **kwargs)}


# ---- true positives -----------------------------------------------------

def test_vague_verb_with_nothing_to_act_on():
    assert "no-target" in codes("fix it", [], "build", "refactor")


@pytest.mark.parametrize("verb", ["fix", "clean up", "improve", "optimize",
                                  "refactor", "update", "tidy", "handle"])
def test_all_vague_openers_are_caught(verb):
    assert "no-target" in codes(f"{verb} this", [], "ask", "question")


def test_debug_without_a_symptom():
    assert "no-symptom" in codes("the server is slow sometimes in production",
                                 [FakeFile("app.py")], "ask", "debug")


def test_build_without_a_stated_constraint():
    assert "no-criterion" in codes("simplify", [FakeFile("a.py")],
                                   "build", "refactor")


def test_bare_files_with_no_description():
    found = spec_check.check("", [FakeFile("a.py")], "build", "refactor")
    assert [f.code for f in found] == ["no-task"]


def test_large_attachment_without_a_line_range():
    assert "unranged-large" in codes(
        "review this module for race conditions in the retry path",
        [FakeFile("big.py")], "ask", "review", input_tokens=40_000)


def test_large_attachment_with_a_range_is_fine():
    """The user already narrowed it; do not nag."""
    assert "unranged-large" not in codes(
        "review this module for race conditions in the retry path",
        [FakeFile("big.py", lines="100-200")], "ask", "review",
        input_tokens=40_000)


def test_terse_prompt_is_flagged():
    assert "terse" in codes("make faster", [FakeFile("a.py")], "ask", "question")


# ---- false positives: silence is the requirement ------------------------

def test_a_well_specified_request_is_silent():
    assert spec_check.check(
        "why does the retry loop in executor.py give up after 3 attempts when "
        "the error is a 503, so that transient overload still recovers?",
        [FakeFile("executor.py", lines="100-160")], "ask", "debug",
        input_tokens=2_000,
    ) == []


def test_vague_verb_with_a_file_is_not_flagged_as_targetless():
    """`fix` plus an attachment has a target; that is enough."""
    assert "no-target" not in codes(
        "fix the off-by-one in the range handling so ranges stay inclusive",
        [FakeFile("args.py")], "build", "refactor")


def test_vague_verb_with_an_error_is_not_flagged_as_targetless():
    assert "no-target" not in codes(
        "fix this: TypeError: unsupported operand type(s) for +", [],
        "ask", "debug")


def test_debug_with_a_traceback_is_silent_on_symptom():
    assert "no-symptom" not in codes(
        "Traceback (most recent call last): ValueError at line 42",
        [FakeFile("a.py")], "ask", "debug")


def test_build_with_a_preservation_constraint_is_accepted():
    assert "no-criterion" not in codes(
        "extract the retry loop into a helper without changing the public API",
        [FakeFile("a.py")], "build", "refactor")


@pytest.mark.parametrize("phrase", [
    "so that the tests pass",
    "without breaking the CLI",
    "must keep backwards compatibility",
    "do not touch the public API",
    "instead of a global",
])
def test_criterion_phrasings_are_recognised(phrase):
    assert "no-criterion" not in codes(
        f"rework the cache {phrase}", [FakeFile("a.py")], "build", "refactor")


def test_empty_prompt_with_no_files_says_nothing():
    """Argument parsing already rejects this; no need to pile on."""
    assert spec_check.check("", [], "ask", "question") == []


# ---- the non-rewriting invariant ---------------------------------------

def test_check_never_mutates_the_prompt():
    prompt = "fix it"
    spec_check.check(prompt, [], "build", "refactor")
    assert prompt == "fix it"


def test_findings_carry_an_actionable_hint():
    """'Be more specific' is not actionable; a concrete addition is."""
    for f in spec_check.check("fix it", [], "build", "refactor"):
        assert f.hint
        assert f.hint != f.message


# ---- rendering ----------------------------------------------------------

def test_render_is_empty_when_nothing_is_wrong():
    assert spec_check.render([], strict=False) == ""


def test_render_says_whether_it_is_blocking():
    findings = spec_check.check("fix it", [], "build", "refactor")
    assert "sending anyway" in spec_check.render(findings, strict=False)
    assert "refusing" in spec_check.render(findings, strict=True)


def test_advisory_render_mentions_the_escape_hatches():
    findings = spec_check.check("fix it", [], "build", "refactor")
    rendered = spec_check.render(findings, strict=False)
    assert "--strict-spec" in rendered
    assert "--no-spec-check" in rendered
