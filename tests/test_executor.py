"""Tests for executor retry behaviour."""

from __future__ import annotations

import subprocess

import pytest

from claudio import executor


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Keep tests fast by zeroing the backoff and skipping real sleeps.

    Also forces buffered mode — these tests mock subprocess.run, which the
    buffered path uses. The streaming path uses subprocess.Popen and has
    its own tests.
    """
    monkeypatch.setattr(executor, "_DEFAULT_BACKOFF_BASE", 0.0)
    monkeypatch.setenv("CLAUDIO_BACKOFF_BASE", "0")
    monkeypatch.setenv("CLAUDIO_NO_STREAM", "1")
    monkeypatch.setattr(executor.time, "sleep", lambda _: None)
    monkeypatch.setattr(executor, "find_claude_cli", lambda: "claude")


def _completed(stdout: str = "", stderr: str = "", code: int = 0):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=code, stdout=stdout, stderr=stderr
    )


def test_is_transient_classification():
    assert executor._is_transient("ECONNRESET", 1) is True
    assert executor._is_transient("503 Service Unavailable", 1) is True
    assert executor._is_transient("Overloaded", 1) is True
    assert executor._is_transient("rate limited", 1) is True
    assert executor._is_transient("socket hang up", 1) is True
    assert executor._is_transient("Invalid API key", 1) is False
    assert executor._is_transient("ENOENT", 1) is False
    # Empty stderr -> assume transient (the loop bounds it)
    assert executor._is_transient("", 1) is True
    # SIGKILL / timeout exit codes -> transient
    assert executor._is_transient("anything", 124) is True


def test_executor_retries_transient_then_succeeds(monkeypatch):
    calls: list[int] = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            return _completed(stderr="ECONNRESET", code=1)
        return _completed(stdout="ok\n")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDIO_MAX_RETRIES", "3")

    text, was_streamed = executor.execute_prompt("hello")
    assert text == "ok"
    assert was_streamed is False
    assert len(calls) == 3


def test_executor_does_not_retry_hard_errors(monkeypatch):
    calls: list[int] = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        return _completed(stderr="Authentication failed: invalid API key", code=2)

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDIO_MAX_RETRIES", "3")

    with pytest.raises(SystemExit):
        executor.execute_prompt("hello")
    assert len(calls) == 1


def test_executor_gives_up_after_max_retries(monkeypatch):
    calls: list[int] = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        return _completed(stderr="503 overloaded", code=1)

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDIO_MAX_RETRIES", "2")

    with pytest.raises(SystemExit):
        executor.execute_prompt("hello")
    # 1 initial + 2 retries
    assert len(calls) == 3


def test_executor_retries_on_subprocess_timeout(monkeypatch):
    calls: list[int] = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
        return _completed(stdout="recovered")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDIO_MAX_RETRIES", "2")

    text, was_streamed = executor.execute_prompt("hello")
    assert text == "recovered"
    assert was_streamed is False
    assert len(calls) == 2


def test_executor_zero_retries_means_single_attempt(monkeypatch):
    calls: list[int] = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        return _completed(stderr="ECONNRESET", code=1)

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDIO_MAX_RETRIES", "0")

    with pytest.raises(SystemExit):
        executor.execute_prompt("hello")
    assert len(calls) == 1


# ---- cache-friendly flag passthrough ----------------------------------

def test_executor_passes_exclude_dynamic_sections_by_default(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _completed(stdout="ok")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.delenv("CLAUDIO_NO_CACHE_FRIENDLY", raising=False)
    executor.execute_prompt("hello")
    assert "--exclude-dynamic-system-prompt-sections" in captured["cmd"]


def test_executor_omits_exclude_dynamic_when_disabled(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _completed(stdout="ok")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDIO_NO_CACHE_FRIENDLY", "1")
    executor.execute_prompt("hello")
    assert "--exclude-dynamic-system-prompt-sections" not in captured["cmd"]


def test_executor_passes_fallback_model_from_config(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _completed(stdout="ok")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        executor, "load_config",
        lambda: {"timeout": 300, "fallback_model": "haiku", "streaming": False},
    )
    executor.execute_prompt("hello")
    assert "--fallback-model" in captured["cmd"]
    fb_idx = captured["cmd"].index("--fallback-model")
    assert captured["cmd"][fb_idx + 1] == "haiku"


def test_executor_skips_fallback_model_when_unconfigured(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _completed(stdout="ok")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        executor, "load_config",
        lambda: {"timeout": 300, "streaming": False},
    )
    executor.execute_prompt("hello")
    assert "--fallback-model" not in captured["cmd"]
