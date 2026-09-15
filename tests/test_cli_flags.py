"""Flags claudio passes to `claude`, and how it degrades on an older CLI.

These pin the *command line* claudio builds. Getting a flag name or its
placement wrong fails the whole call at runtime, and only against a real
`claude` binary — exactly the class of bug a unit test should catch first.
"""

from __future__ import annotations

import subprocess

import pytest

from claudio import executor
from claudio.cli import _pop_global_flags


@pytest.fixture
def captured_cmd(monkeypatch):
    """Run execute_prompt in buffered mode and capture the argv it built."""
    seen: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setenv("CLAUDIO_NO_STREAM", "1")
    monkeypatch.delenv("CLAUDIO_EFFORT", raising=False)
    monkeypatch.delenv("CLAUDIO_MAX_BUDGET_USD", raising=False)
    monkeypatch.setattr(executor, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(executor, "load_config", lambda: {})
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    return seen


# ---- --effort -----------------------------------------------------------

def test_effort_is_passed_through(captured_cmd):
    executor.execute_prompt("hi", effort="xhigh")
    cmd = captured_cmd[0]
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "xhigh"


def test_no_effort_flag_when_unset(captured_cmd):
    executor.execute_prompt("hi")
    assert "--effort" not in captured_cmd[0]


def test_unknown_effort_is_dropped_not_forwarded(captured_cmd, capsys):
    """Forwarding junk would make the CLI reject the entire call."""
    executor.execute_prompt("hi", effort="turbo")
    assert "--effort" not in captured_cmd[0]
    assert "turbo" in capsys.readouterr().err


def test_effort_from_env(captured_cmd, monkeypatch):
    monkeypatch.setenv("CLAUDIO_EFFORT", "LOW")
    executor.execute_prompt("hi")
    cmd = captured_cmd[0]
    assert cmd[cmd.index("--effort") + 1] == "low"   # normalised


# ---- --max-budget-usd ---------------------------------------------------

def test_budget_is_passed_through(captured_cmd):
    executor.execute_prompt("hi", max_budget_usd=0.5)
    cmd = captured_cmd[0]
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.5"


@pytest.mark.parametrize("bad", [0, -1, "abc", ""])
def test_nonsense_budgets_are_ignored(captured_cmd, bad):
    """A zero or negative cap would either be rejected or halt every run
    instantly; neither is what the user meant."""
    executor.execute_prompt("hi", max_budget_usd=bad)
    assert "--max-budget-usd" not in captured_cmd[0]


def test_budget_from_env(captured_cmd, monkeypatch):
    monkeypatch.setenv("CLAUDIO_MAX_BUDGET_USD", "1.25")
    executor.execute_prompt("hi")
    cmd = captured_cmd[0]
    assert cmd[cmd.index("--max-budget-usd") + 1] == "1.25"


# ---- --include-partial-messages ----------------------------------------

def test_partial_messages_on_by_default_when_streaming(monkeypatch):
    """Without this flag stream-json emits whole-block snapshots, so the
    'streaming' output arrives in chunks rather than tokens."""
    seen: list[list[str]] = []
    monkeypatch.delenv("CLAUDIO_NO_STREAM", raising=False)
    monkeypatch.delenv("CLAUDIO_NO_PARTIAL", raising=False)
    monkeypatch.setattr(executor, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(executor, "load_config", lambda: {})
    monkeypatch.setattr(executor, "_execute_streaming",
                        lambda cmd, *a, **k: seen.append(list(cmd))
                        or executor.ExecResult("", True, None))
    executor.execute_prompt("hi")
    assert "--include-partial-messages" in seen[0]


def test_partial_messages_not_sent_in_json_mode(captured_cmd):
    """--json means machine-parsed buffered output; deltas are meaningless."""
    executor.execute_prompt("hi", json_output=True)
    assert "--include-partial-messages" not in captured_cmd[0]


# ---- degrading on an older claude CLI ----------------------------------

def test_unsupported_optional_flag_is_dropped_and_retried(monkeypatch, capsys):
    """An older `claude` that doesn't know --effort must not fail the user's
    call outright; drop the flag and try again."""
    attempts: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        attempts.append(list(cmd))
        if "--effort" in cmd:
            return subprocess.CompletedProcess(
                cmd, 1, "", "error: unknown option '--effort'")
        return subprocess.CompletedProcess(cmd, 0, "recovered", "")

    monkeypatch.setenv("CLAUDIO_NO_STREAM", "1")
    monkeypatch.setattr(executor, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(executor, "load_config", lambda: {})
    monkeypatch.setattr(executor.subprocess, "run", fake_run)

    result = executor.execute_prompt("hi", effort="high")
    assert result.text == "recovered"
    assert len(attempts) == 2
    assert "--effort" not in attempts[1]
    assert "high" not in attempts[1]          # the value went with it
    assert "does not support" in capsys.readouterr().err


def test_a_genuine_error_still_fails(monkeypatch):
    """Only *optional* flags are droppable. An unknown --model is a real
    failure and must surface, not be silently retried away."""
    monkeypatch.setenv("CLAUDIO_NO_STREAM", "1")
    monkeypatch.setenv("CLAUDIO_MAX_RETRIES", "0")
    monkeypatch.setattr(executor, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(executor, "load_config", lambda: {})
    monkeypatch.setattr(executor.subprocess, "run", lambda cmd, *a, **k:
                        subprocess.CompletedProcess(
                            cmd, 1, "", "error: unknown option '--model'"))
    with pytest.raises(SystemExit):
        executor.execute_prompt("hi", model="opus")


def test_strip_returns_none_for_unrelated_errors():
    cmd = ["claude", "--print", "--effort", "low"]
    assert executor._strip_unsupported_flag(cmd, "ECONNRESET") is None
    assert executor._strip_unsupported_flag(cmd, "") is None


def test_strip_drops_valueless_flag_without_eating_its_neighbour():
    cmd = ["claude", "--include-partial-messages", "--model", "opus"]
    assert executor._strip_unsupported_flag(
        cmd, "unknown option '--include-partial-messages'"
    ) == ["claude", "--model", "opus"]


# ---- argv parsing -------------------------------------------------------

def test_cli_parses_effort_and_budget():
    rest, flags = _pop_global_flags(
        ["-q", "--effort", "max", "--max-budget-usd", "2.50", "hello"])
    assert flags["effort"] == "max"
    assert flags["max_budget_usd"] == "2.50"
    assert rest == ["-q", "hello"]


def test_value_flags_do_not_swallow_the_description():
    """--effort takes exactly one value; the prompt must survive."""
    rest, flags = _pop_global_flags(["--effort", "low", "fix the bug"])
    assert rest == ["fix the bug"]
    assert flags["effort"] == "low"


@pytest.mark.parametrize("flag", ["--effort", "--max-budget-usd"])
def test_value_flag_without_a_value_exits(flag):
    with pytest.raises(SystemExit):
        _pop_global_flags([flag])
