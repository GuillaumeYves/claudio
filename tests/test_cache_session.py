"""Tests for the cache-hit / session-turn interaction.

When the REPL serves a response from the local cache, Claude itself
isn't called, so the conversation never exists on Claude's side. The
next REPL turn must therefore still use `--session-id <id>` (a fresh
session) rather than `--resume <id>` (which would fail with "No
conversation found"). The cache module signals hits via `consume_last_hit`.
"""

from __future__ import annotations

import pytest

from claudio import cache


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Point cache.CACHE_DIR at a clean tmp dir so tests don't leak."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / ".claudio" / "cache")
    # Reset the module flag at the start of every test.
    cache._last_was_hit = False
    yield


def test_consume_last_hit_is_false_when_nothing_cached():
    """A fresh cache has no hits to report."""
    cache.cache_get("some prompt")
    assert cache.consume_last_hit() is False


def test_cache_hit_flips_last_was_hit():
    """Storing then retrieving the same prompt should register a hit."""
    cache.cache_put("hello", "world", input_tokens=1)
    # cache_put resets the flag (it's a real call, not a hit)
    assert cache.consume_last_hit() is False

    # Re-fetch -- should be a hit
    out = cache.cache_get("hello")
    assert out == "world"
    assert cache.consume_last_hit() is True


def test_consume_last_hit_is_read_and_clear():
    """The hit signal is consumed once; a second consume returns False."""
    cache.cache_put("hello", "world", input_tokens=1)
    cache.cache_get("hello")  # hit
    assert cache.consume_last_hit() is True
    # Already consumed -- second read returns False.
    assert cache.consume_last_hit() is False


def test_cache_miss_does_not_set_hit_flag():
    """Looking up an unstored prompt must NOT register as a hit."""
    cache.cache_get("never-stored")
    assert cache.consume_last_hit() is False


def test_cache_put_resets_hit_flag():
    """A real call (cache_put) must clear any stale hit signal so the
    REPL's session_turn logic stays in sync after a miss+store."""
    cache.cache_put("a", "1")
    cache.cache_get("a")  # hit
    assert cache._last_was_hit is True
    cache.cache_put("b", "2")  # real call after a hit
    assert cache._last_was_hit is False
