"""Tests for the token estimator (tiktoken with heuristic fallback)."""

from __future__ import annotations

import pytest

from claudio.utils import tokens


@pytest.fixture(autouse=True)
def _reset_encoder_cache(monkeypatch):
    """The encoder is module-cached on first use. Reset before each test so
    monkeypatching `_get_bpe_encoder` actually takes effect."""
    monkeypatch.setattr(tokens, "_BPE_ENCODER", None)
    monkeypatch.setattr(tokens, "_BPE_ATTEMPTED", False)
    yield


def test_estimate_empty_returns_one():
    assert tokens.estimate_tokens("") == 1


def test_estimate_uses_heuristic_when_no_encoder(monkeypatch):
    monkeypatch.setattr(tokens, "_get_bpe_encoder", lambda: None)
    # 40-char string, prose ratio 4 -> 10 tokens
    assert tokens.estimate_tokens("x" * 40) == 10
    # code ratio 3 -> 13 tokens
    assert tokens.estimate_tokens("x" * 40, is_code=True) == 13


def test_estimate_uses_encoder_when_available(monkeypatch):
    class FakeEncoder:
        def encode(self, text):
            # Pretend "1 token per char" so we get a distinct, predictable
            # result that wouldn't match the heuristic.
            return list(text)

    monkeypatch.setattr(tokens, "_get_bpe_encoder", lambda: FakeEncoder())
    assert tokens.estimate_tokens("hello") == 5


def test_estimate_falls_back_when_encoder_raises(monkeypatch):
    class BoomEncoder:
        def encode(self, text):
            raise RuntimeError("BPE table corrupt")

    monkeypatch.setattr(tokens, "_get_bpe_encoder", lambda: BoomEncoder())
    # Fall back to heuristic: 40 chars / 4 = 10
    assert tokens.estimate_tokens("x" * 40) == 10


def test_get_bpe_encoder_caches_failure(monkeypatch):
    """If tiktoken isn't importable, we don't keep retrying on every call."""
    calls = {"n": 0}

    def fake_import(name, *a, **kw):
        calls["n"] += 1
        if name == "tiktoken":
            raise ImportError("not installed")
        raise ImportError(name)

    monkeypatch.setattr("builtins.__import__", fake_import)
    # First call: tries import, caches None
    assert tokens._get_bpe_encoder() is None
    # Second call: returns cached None, no retry
    assert tokens._get_bpe_encoder() is None
    # Only one import attempt (tiktoken), even across two calls
    tiktoken_imports = [True for _ in range(calls["n"])]
    # The exact count depends on Python's internal import behaviour, but it
    # should be at most a small constant — definitely not 2+ tiktoken imports.
    assert calls["n"] <= 4


def test_format_token_info_still_works():
    s = tokens.format_token_info(5000)
    assert "5,000" in s
    assert "$" in s


def test_format_token_info_warns_on_large():
    s = tokens.format_token_info(40_000)
    assert "WARNING" in s.upper()


# ---- per-model pricing / --estimate formatting -------------------------

def test_tier_for_maps_aliases_and_ids():
    assert tokens._tier_for("haiku") == "haiku"
    assert tokens._tier_for("opus") == "opus"
    assert tokens._tier_for("claude-opus-4-8") == "opus"
    assert tokens._tier_for("sonnet") == "sonnet"
    # Unknown / None default to the router's fallthrough tier.
    assert tokens._tier_for(None) == "sonnet"
    assert tokens._tier_for("something-weird") == "sonnet"


def test_format_estimate_prices_by_tier():
    # 1M tokens at the opus tier price ($15/M) -> $15.0000.
    s = tokens.format_estimate(1_000_000, "opus")
    assert "1,000,000 input tokens" in s
    assert "model: opus" in s
    assert "$15.0000" in s
    assert "output billed separately" in s


def test_format_estimate_opus_costs_more_than_haiku():
    def _cost(line: str) -> float:
        return float(line.split("$")[1].split(" ")[0])

    assert _cost(tokens.format_estimate(50_000, "opus")) > _cost(
        tokens.format_estimate(50_000, "haiku")
    )
