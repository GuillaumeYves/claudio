"""execute_with_tracking must not cache file-mutating runs.

A build that applies edits is side-effecting: replaying a cached response on
a later identical call would print "Done!" without re-applying the edit. So a
mutating permission_mode bypasses the cache entirely, while read-only runs
(ask/review) keep caching. These tests pin both halves of that contract.
"""

from __future__ import annotations

import pytest

from claudio import cache
from claudio.commands import run_prompt
from claudio.utils.output import Output


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / ".claudio" / "cache")
    # Don't touch the real usage log.
    monkeypatch.setattr(run_prompt, "log_request", lambda *a, **k: None)
    yield


def _ctx(**over):
    base = {
        "dry_run": False, "no_cache": False, "verbose": False,
        "json_output": False, "model": None, "session_id": None, "resume": None,
    }
    base.update(over)
    return base


def test_mutating_run_bypasses_cache(monkeypatch):
    calls: list[dict] = []

    def fake_execute(prompt, **kwargs):
        calls.append(kwargs)
        return ("applied the edit", True)

    monkeypatch.setattr(run_prompt, "execute_prompt", fake_execute)

    out = Output()
    run_prompt.execute_with_tracking(
        "PROMPT", _ctx(), out, "build", "refactor",
        intent="refactor", permission_mode="acceptEdits",
    )

    # Nothing was stored, so a follow-up lookup is a clean miss.
    assert cache.cache_get("PROMPT") is None
    # And the mode was actually handed to the executor.
    assert calls[0]["permission_mode"] == "acceptEdits"


def test_mutating_run_ignores_existing_cache_entry(monkeypatch):
    # Pre-seed the cache with a stale response for this prompt.
    cache.cache_put("PROMPT", "STALE CACHED TEXT", input_tokens=1)

    calls: list[dict] = []

    def fake_execute(prompt, **kwargs):
        calls.append(kwargs)
        return ("freshly applied", True)

    monkeypatch.setattr(run_prompt, "execute_prompt", fake_execute)

    out = Output()
    run_prompt.execute_with_tracking(
        "PROMPT", _ctx(), out, "build", "refactor",
        intent="refactor", permission_mode="acceptEdits",
    )

    # The executor ran instead of short-circuiting on the stale entry.
    assert len(calls) == 1


def test_readonly_run_still_caches(monkeypatch):
    calls: list[dict] = []

    def fake_execute(prompt, **kwargs):
        calls.append(kwargs)
        return ("the answer", True)

    monkeypatch.setattr(run_prompt, "execute_prompt", fake_execute)

    out = Output()
    # No permission_mode -> read-only ask. First call executes and stores.
    run_prompt.execute_with_tracking("Q", _ctx(), out, "ask", "question")
    assert len(calls) == 1
    assert cache.cache_get("Q") == "the answer"
