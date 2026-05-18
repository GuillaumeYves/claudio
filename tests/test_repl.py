"""Tests for the interactive REPL: tokenizer, slash commands, @ completer."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from claudio.repl import (
    _tokenize,
    _handle_slash,
    _print_banner,
    _history_file,
    _format_cwd,
)


# ---- _tokenize ----------------------------------------------------------

def test_tokenize_simple_build_line():
    assert _tokenize('build -r @main.py "reduce complexity"') == [
        "build", "-r", "@main.py", "reduce complexity"
    ]


def test_tokenize_preserves_windows_backslash_path():
    # This is the bug that motivated the tokenizer: posix=True would eat
    # the backslashes and produce @srcfile.py.
    assert _tokenize(r'build -r @src\file.py "msg"') == [
        "build", "-r", r"@src\file.py", "msg"
    ]


def test_tokenize_strips_single_quotes():
    assert _tokenize("ask -q 'what does this do'") == [
        "ask", "-q", "what does this do"
    ]


def test_tokenize_preserves_apostrophe_inside_double_quotes():
    assert _tokenize('ask -q "it\'s broken"') == [
        "ask", "-q", "it's broken"
    ]


def test_tokenize_f_flag_with_backslash_path_and_line_range():
    assert _tokenize(r'build -f src\path\to\f.py -10-25 "refactor"') == [
        "build", "-f", r"src\path\to\f.py", "-10-25", "refactor"
    ]


def test_tokenize_empty_string_returns_empty_list():
    assert _tokenize("") == []


# ---- _tokenize: prose semantics ----------------------------------------

def test_tokenize_unquoted_apostrophes_pass_through():
    assert _tokenize("ask -q i'm not sure which is better") == [
        "ask", "-q", "i'm not sure which is better",
    ]


def test_tokenize_at_file_then_unquoted_prose():
    assert _tokenize("build -r @main.py reduce nesting where it's hard to read") == [
        "build", "-r", "@main.py", "reduce nesting where it's hard to read",
    ]


def test_tokenize_line_range_after_at_file():
    assert _tokenize("build -r @main.py -10-25 simplify what's here") == [
        "build", "-r", "@main.py", "-10-25", "simplify what's here",
    ]


def test_tokenize_value_flag_pairs_with_its_value():
    assert _tokenize("ask -q --model haiku what's the time complexity") == [
        "ask", "-q", "--model", "haiku", "what's the time complexity",
    ]


def test_tokenize_swallows_trailing_flag_into_prose():
    # Convention: once prose starts, flag-looking tokens are part of the
    # message. Users who want flags applied must put them before the prompt.
    assert _tokenize("ask -q what's wrong --verbose") == [
        "ask", "-q", "what's wrong --verbose",
    ]


# ---- _augment_argv: REPL auto-chain ------------------------------------

def test_augment_argv_injects_session_id_on_first_turn():
    from claudio.repl import _augment_argv
    out = _augment_argv(["ask", "-q", "hello"], None, "sess-uuid", 0)
    assert "--session-id" in out
    assert "sess-uuid" in out
    assert "--resume" not in out


def test_augment_argv_uses_resume_on_subsequent_turns():
    from claudio.repl import _augment_argv
    out = _augment_argv(["ask", "-q", "hello"], None, "sess-uuid", 1)
    assert "--resume" in out
    assert "sess-uuid" in out
    assert "--session-id" not in out


def test_augment_argv_skips_when_user_passed_own_session():
    from claudio.repl import _augment_argv
    out = _augment_argv(
        ["ask", "-q", "hi", "--session-id", "mine"], None, "auto-sess", 1
    )
    # User-supplied session id wins; we don't add ours.
    assert out.count("--session-id") == 1
    assert "auto-sess" not in out


def test_augment_argv_skips_when_user_passed_resume():
    from claudio.repl import _augment_argv
    out = _augment_argv(
        ["ask", "-q", "hi", "--resume", "mine"], None, "auto-sess", 1
    )
    assert out.count("--resume") == 1
    assert "auto-sess" not in out
    assert "--session-id" not in out


def test_augment_argv_threads_session_model():
    from claudio.repl import _augment_argv
    out = _augment_argv(["ask", "-q", "hi"], "haiku", "sess", 0)
    assert "--model" in out
    assert "haiku" in out


def test_augment_argv_skips_model_when_user_passed_own():
    from claudio.repl import _augment_argv
    out = _augment_argv(
        ["ask", "-q", "hi", "--model", "opus"], "haiku", "sess", 0
    )
    # User override stays; we don't add the REPL session_model.
    assert out.count("--model") == 1
    assert "opus" in out
    assert "haiku" not in out


# ---- /fresh + /session slash commands ----------------------------------

def test_slash_fresh_returns_fresh():
    assert _handle_slash("/fresh", None, "old-sess") == "fresh"


def test_slash_session_prints_current_id(capsys):
    _handle_slash("/session", None, "abc-123")
    out = capsys.readouterr().out
    assert "abc-123" in out


def test_slash_session_handles_no_session(capsys):
    _handle_slash("/session", None, None)
    out = capsys.readouterr().out
    assert "(none)" in out or "None" in out


def test_tokenize_command_only():
    assert _tokenize("stats") == ["stats"]


# ---- _tokenize: quotes-as-emphasis semantics ---------------------------

def test_tokenize_inline_quotes_are_preserved_verbatim():
    # The whole prose blob isn't wrapped — only "ruby vs php" is. Inline
    # quotes survive into the prompt so Claude sees the emphasis.
    assert _tokenize('ask -q do you think "ruby vs php" is still relevant in 2026?') == [
        "ask", "-q", 'do you think "ruby vs php" is still relevant in 2026?',
    ]


def test_tokenize_at_symbol_inside_prose_is_not_a_file_ref():
    # The @ doesn't need to be quoted to be safe — once prose starts (the
    # word "what" after -q), the rest of the line is verbatim. Quotes are
    # only for emphasis, not for protection.
    assert _tokenize("ask -q what does the @ symbol mean in python?") == [
        "ask", "-q", "what does the @ symbol mean in python?",
    ]


def test_tokenize_flag_lookalikes_inside_prose_are_not_flags():
    # Same idea for `--`-prefixed tokens that appear mid-prose.
    assert _tokenize("ask -q how do you decide between --verbose and --debug logging?") == [
        "ask", "-q", "how do you decide between --verbose and --debug logging?",
    ]


def test_tokenize_whole_prompt_in_quotes_strips_them():
    # The traditional shell form still works — outer quotes that wrap the
    # entire prose blob are stripped. This is the only purpose of
    # wrap-the-whole-thing quoting; it's no longer required for @ or --.
    assert _tokenize('ask -q "the whole thing is wrapped"') == [
        "ask", "-q", "the whole thing is wrapped",
    ]


def test_tokenize_two_quoted_phrases_are_not_an_outer_wrap():
    # First and last char are both `"` but they're not the same pair —
    # don't strip, preserve the user's emphasis.
    assert _tokenize('ask -q "first thing" and "second thing"') == [
        "ask", "-q", '"first thing" and "second thing"',
    ]


def test_tokenize_unbalanced_quotes_pass_through_verbatim():
    # Earlier this raised ValueError; now the prose blob takes the line
    # verbatim and lets Claude make sense of it.
    assert _tokenize('ask -q "open and close" but "open') == [
        "ask", "-q", '"open and close" but "open',
    ]


def test_tokenize_unclosed_double_quote_passes_through():
    assert _tokenize('ask -q tell me about "shells') == [
        "ask", "-q", 'tell me about "shells',
    ]


def test_tokenize_preserves_internal_whitespace_in_prose():
    # Multiple spaces inside the prose blob survive — we only rstrip().
    assert _tokenize("ask -q hello    world") == [
        "ask", "-q", "hello    world",
    ]


# ---- _handle_slash ------------------------------------------------------

def test_slash_exit_returns_exit():
    assert _handle_slash("/exit", None) == "exit"
    assert _handle_slash("/quit", None) == "exit"
    assert _handle_slash("/q", None) == "exit"


def test_slash_help_prints_help(capsys):
    assert _handle_slash("/help", None) is None
    out = capsys.readouterr().out
    assert "Slash commands" in out
    assert "/exit" in out


def test_slash_model_without_arg_shows_current(capsys):
    _handle_slash("/model", "sonnet")
    out = capsys.readouterr().out
    assert "current model" in out
    assert "sonnet" in out


def test_slash_model_with_arg_returns_tuple():
    assert _handle_slash("/model haiku", None) == ("model", "haiku")


def test_slash_model_reset_returns_none_tuple():
    assert _handle_slash("/model auto", "opus") == ("model", None)
    assert _handle_slash("/model reset", "opus") == ("model", None)


def test_slash_cwd_no_arg_prints_current(capsys):
    _handle_slash("/cwd", None)
    out = capsys.readouterr().out
    assert "cwd:" in out


def test_slash_cwd_changes_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    _handle_slash(f"/cwd {sub}", None)
    assert Path.cwd() == sub


def test_slash_cwd_bad_path_reports_error(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _handle_slash("/cwd /definitely/not/a/real/path/xyz123", None)
    err = capsys.readouterr().err
    assert "error" in err.lower()


def test_slash_unknown_command_reports_error(capsys):
    _handle_slash("/nonsense", None)
    err = capsys.readouterr().err
    assert "unknown" in err.lower()


# ---- _print_banner ------------------------------------------------------

def test_banner_prints_on_utf8_stdout(capsys):
    _print_banner()
    out = capsys.readouterr().out
    assert "Claudio" in out or "Claude" in out


def test_banner_includes_claudio_and_cwd(capsys):
    """The minimal banner must show 'Claudio', a cwd line, and tip text.

    The leading logo (block-char art) is only emitted on utf-capable
    encodings — we don't assert on it here so the test works regardless
    of the test runner's stdout encoding.
    """
    _print_banner()
    out = capsys.readouterr().out
    assert "Claudio" in out
    assert "cwd:" in out
    assert "/help" in out


def test_banner_uses_no_sleep(monkeypatch):
    """The minimal banner is instant — no animation, no time.sleep."""
    import time
    calls = []
    monkeypatch.setattr(time, "sleep", lambda s: calls.append(s))
    _print_banner()
    assert calls == []


# ---- _format_cwd -------------------------------------------------------

def test_format_cwd_returns_string():
    out = _format_cwd()
    assert isinstance(out, str)
    assert out  # non-empty


def test_format_cwd_collapses_home_to_tilde(monkeypatch, tmp_path):
    """A cwd under the user's home appears as ~/relative/path."""
    # Build a fake home + cwd under it
    home = tmp_path / "home"
    sub = home / "proj" / "claudio"
    sub.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(sub)
    out = _format_cwd()
    assert out == "~/proj/claudio"


def test_format_cwd_outside_home_returns_absolute(monkeypatch, tmp_path):
    """A cwd NOT under home is returned as-is (no ~ collapse)."""
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "elsewhere"))
    monkeypatch.chdir(other)
    out = _format_cwd()
    assert out == other.as_posix()


def test_banner_does_not_crash_on_cp1252(monkeypatch):
    """On a legacy cp1252 console the banner must still print without
    raising. The minimal layout uses ✻ as its only non-cp1252 glyph, and
    _print_banner swaps it for `*` when stdout reports a non-utf encoding,
    so cp1252 streams can encode the whole banner."""
    class CP1252Stdout:
        encoding = "cp1252"

        def __init__(self):
            self.buffer = io.BytesIO()

        def write(self, s):
            self.buffer.write(s.encode("cp1252"))
            return len(s)

        def flush(self):
            pass

    fake = CP1252Stdout()
    monkeypatch.setattr(sys, "stdout", fake)
    # Must not raise; legacy consoles get the ASCII-glyph variant.
    _print_banner()
    written = fake.buffer.getvalue()
    assert b"Claudio" in written
    # The non-cp1252 glyph must NOT appear; the ASCII fallback is `*`.
    assert "✻".encode("utf-8") not in written
    assert b"*" in written


# ---- _history_file ------------------------------------------------------

def test_history_file_respects_claudio_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDIO_HOME", str(tmp_path))
    assert _history_file() == tmp_path / "repl_history"


def test_history_file_defaults_to_home(monkeypatch):
    monkeypatch.delenv("CLAUDIO_HOME", raising=False)
    path = _history_file()
    assert path.name == "repl_history"
    assert ".claudio" in path.parts


# ---- AtFileCompleter ----------------------------------------------------

def _completions_for(text: str) -> list[str]:
    """Return just the completion texts for the given input line."""
    from claudio.repl import iter_at_completions
    return [entry[0] for entry in iter_at_completions(text)]


def test_completer_returns_files_and_dirs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "main.py").write_text("x")
    (tmp_path / "utils").mkdir()
    (tmp_path / "README.md").write_text("x")
    results = _completions_for("build -r @")
    assert "@main.py" in results
    assert "@utils/" in results
    assert "@README.md" in results


def test_completer_filters_by_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "alpha.py").write_text("x")
    (tmp_path / "beta.py").write_text("x")
    (tmp_path / "gamma.py").write_text("x")
    results = _completions_for("build -r @al")
    assert results == ["@alpha.py"]


def test_completer_skips_git_and_node_modules(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "src").mkdir()
    results = _completions_for("build -r @")
    assert "@src/" in results
    assert "@.git/" not in results
    assert "@node_modules/" not in results


def test_completer_skips_hidden_when_no_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("x")
    (tmp_path / "main.py").write_text("x")
    results = _completions_for("build -r @")
    assert "@.env" not in results
    assert "@main.py" in results


def test_completer_matches_hidden_when_prefix_starts_with_dot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("x")
    (tmp_path / ".envrc").write_text("x")
    (tmp_path / "main.py").write_text("x")
    results = _completions_for("build -r @.")
    assert "@.env" in results
    assert "@.envrc" in results
    assert "@main.py" not in results


def test_completer_descends_into_subdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x")
    (tmp_path / "src" / "core.py").write_text("x")
    results = _completions_for("build -r @src/")
    assert "@src/app.py" in results
    assert "@src/core.py" in results


def test_completer_no_match_outside_at_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "main.py").write_text("x")
    # No @ in the text -> no completions from the @ completer
    assert _completions_for("build -r main") == []


# ---- iter_command_completions ------------------------------------------

def _cmd_completions_for(text: str) -> list[str]:
    from claudio.repl import iter_command_completions
    return [entry[0] for entry in iter_command_completions(text)]


def test_command_completer_top_level_commands_at_line_start():
    results = _cmd_completions_for("bu")
    assert "build" in results
    # Other commands shouldn't match the "bu" prefix
    assert "ask" not in results


def test_command_completer_offers_all_commands_for_empty_line():
    results = _cmd_completions_for("")
    assert "build" in results
    assert "ask" in results
    assert "run" in results
    assert "stats" in results


def test_command_completer_slash_commands():
    results = _cmd_completions_for("/he")
    assert "/help" in results
    assert "/model" not in results


def test_command_completer_slash_root_lists_all():
    results = _cmd_completions_for("/")
    assert "/help" in results
    assert "/model" in results
    assert "/exit" in results


def test_command_completer_model_names_after_slash_model():
    results = _cmd_completions_for("/model ha")
    assert results == ["haiku"]


def test_command_completer_modes_after_command():
    results = _cmd_completions_for("build -r")
    # -r and -refactor both start with "-r"
    assert "-r" in results
    assert "-refactor" in results
    # Build modes shouldn't appear under ask
    ask_results = _cmd_completions_for("ask -r")
    assert "-refactor" not in ask_results
    assert "-rv" in ask_results
    assert "-review" in ask_results


def test_command_completer_no_completions_for_unknown_command():
    assert _cmd_completions_for("frobnicate -r") == []


def test_command_completer_quiet_inside_argument_text():
    # Mid-line typing of a non-flag, non-@ argument should not pop noisy
    # completions for the file path the user is constructing.
    assert _cmd_completions_for("ask -q how does") == []


