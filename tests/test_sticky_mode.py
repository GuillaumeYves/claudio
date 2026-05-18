"""Tests for the REPL mode-sticky behavior."""

from __future__ import annotations

from claudio.repl import (
    _parse_mode_arg,
    _tokenize,
    apply_sticky,
    extract_sticky,
)


# ---- apply_sticky: bare prompts inherit sticky context ----------------

def test_explicit_command_passes_through_unchanged():
    out, used_default = apply_sticky(
        'ask -review @auth.py "look for bugs"',
        sticky_mode=("ask", "-q"),
        sticky_files=["@old.py"],
    )
    assert out == 'ask -review @auth.py "look for bugs"'
    assert used_default is False


def test_slash_command_passes_through_unchanged():
    out, _ = apply_sticky(
        "/help", sticky_mode=("ask", "-rv"), sticky_files=[]
    )
    assert out == "/help"


def test_bare_prompt_with_no_sticky_defaults_to_ask_q():
    out, used_default = apply_sticky(
        "what is dependency injection?",
        sticky_mode=None,
        sticky_files=[],
    )
    assert out.startswith("ask -q ")
    assert "what is dependency injection?" in out
    assert used_default is True


def test_bare_prompt_inherits_sticky_mode():
    out, used_default = apply_sticky(
        "what about input validation?",
        sticky_mode=("ask", "-review"),
        sticky_files=[],
    )
    assert out.startswith("ask -review ")
    assert used_default is False


def test_bare_prompt_reattaches_sticky_files():
    out, _ = apply_sticky(
        "any race conditions?",
        sticky_mode=("ask", "-review"),
        sticky_files=["@auth.py", "-40-80"],
    )
    assert "@auth.py" in out
    assert "-40-80" in out
    assert out.startswith("ask -review @auth.py -40-80 ")


def test_new_at_files_replace_sticky_files():
    """If the user types a new @-token, the prior sticky files are dropped
    so we don't pile unrelated files into the context."""
    out, _ = apply_sticky(
        "@other.py compare it",
        sticky_mode=("ask", "-q"),
        sticky_files=["@auth.py"],
    )
    assert "@other.py" in out
    assert "@auth.py" not in out


def test_empty_line_returned_unchanged():
    out, used_default = apply_sticky("", sticky_mode=("ask", "-rv"), sticky_files=[])
    assert out == ""
    assert used_default is False


# ---- extract_sticky: argv -> sticky state -----------------------------

def test_extract_ask_review_mode():
    argv = ["ask", "-review", "@auth.py", "-40-80", "look for bugs"]
    mode, files = extract_sticky(argv)
    assert mode == ("ask", "-review")
    assert files == ["@auth.py", "-40-80"]


def test_extract_build_refactor_mode():
    argv = ["build", "-r", "@main.py", "extract helper"]
    mode, files = extract_sticky(argv)
    assert mode == ("build", "-r")
    assert files == ["@main.py"]


def test_extract_no_mode_returns_none():
    """Command without a mode flag (e.g. `ask "..."`) yields no sticky."""
    argv = ["ask", "what is foo?"]
    mode, _ = extract_sticky(argv)
    assert mode is None


def test_extract_non_sticky_command_returns_none():
    """run/stats/setup don't participate in sticky."""
    for cmd in ("run", "stats", "setup"):
        mode, files = extract_sticky([cmd, "--reset"])
        assert mode is None
        assert files == []


def test_extract_handles_multiple_files_with_ranges():
    argv = ["ask", "-rv", "@a.py", "-10-20", "@b.py", "@c.py", "-1", "x"]
    mode, files = extract_sticky(argv)
    assert mode == ("ask", "-rv")
    assert files == ["@a.py", "-10-20", "@b.py", "@c.py", "-1"]


def test_extract_empty_argv_returns_none():
    mode, files = extract_sticky([])
    assert mode is None
    assert files == []


# ---- round-trip: explicit command -> sticky -> bare follow-up ---------

def test_round_trip_explicit_then_followup():
    # User runs an explicit review
    argv1 = _tokenize('ask -review @auth.py "anything broken"')
    mode, files = extract_sticky(argv1)
    assert mode == ("ask", "-review")

    # Next turn: bare prompt — must produce an ask -review with the same file
    line2 = "what about token refresh?"
    adjusted, _ = apply_sticky(line2, mode, files)
    argv2 = _tokenize(adjusted)
    assert argv2[0] == "ask"
    assert "-review" in argv2
    assert "@auth.py" in argv2


def test_round_trip_mode_switch_drops_old_files():
    """Switching from review @auth.py to build -r @other.py wipes the
    sticky file when extracted from the new argv."""
    argv1 = _tokenize('ask -review @auth.py "x"')
    mode1, files1 = extract_sticky(argv1)
    assert mode1 == ("ask", "-review")
    assert "@auth.py" in files1

    argv2 = _tokenize('build -r @other.py "extract"')
    mode2, files2 = extract_sticky(argv2)
    assert mode2 == ("build", "-r")
    assert files2 == ["@other.py"]
    assert "@auth.py" not in files2


# ---- /mode argument parsing -------------------------------------------

def test_parse_mode_arg_valid_ask_review():
    assert _parse_mode_arg("ask -review") == ("ask", "-review")
    assert _parse_mode_arg("ask -q") == ("ask", "-q")
    assert _parse_mode_arg("build -r") == ("build", "-r")


def test_parse_mode_arg_unknown_command():
    """Only ask / build are valid sticky commands."""
    assert _parse_mode_arg("run -r") is None
    assert _parse_mode_arg("stats -q") is None


def test_parse_mode_arg_unknown_flag():
    assert _parse_mode_arg("ask -bogus") is None


def test_parse_mode_arg_wrong_token_count():
    assert _parse_mode_arg("ask") is None
    assert _parse_mode_arg("ask -review extra") is None
    assert _parse_mode_arg("") is None
