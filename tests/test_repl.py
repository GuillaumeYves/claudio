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


def test_tokenize_unclosed_quote_raises_valueerror():
    with pytest.raises(ValueError):
        _tokenize('build -r "unclosed')


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


def test_banner_falls_back_when_encoding_cannot_render_it(monkeypatch):
    class CP1252Stdout:
        def __init__(self):
            self.buffer = io.BytesIO()
            self._raised_once = False

        def write(self, s):
            # Simulate cp1252 choking on non-latin1 chars
            try:
                self.buffer.write(s.encode("cp1252"))
            except UnicodeEncodeError:
                raise
            return len(s)

        def flush(self):
            pass

    fake = CP1252Stdout()
    monkeypatch.setattr(sys, "stdout", fake)
    # Must not raise -- the fallback ASCII banner must be written instead.
    _print_banner()
    written = fake.buffer.getvalue()
    assert b"Claudio" in written
    # Fallback uses plain ASCII -- no high-bit chars
    assert all(b < 128 for b in written)


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
    # No @ in the text -> no completions
    assert _completions_for("build -r main") == []
