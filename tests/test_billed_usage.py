"""Billed-usage capture: the CLI's own numbers beat claudio's estimate.

claudio's local estimate only ever sees the prompt claudio composed. The real
request also carries Claude Code's system prompt, CLAUDE.md, tool definitions
and prompt-cache traffic. These tests pin the shapes claudio must read so a
trivial call is never again logged as "~9 tokens, $0.000003" when it billed
23,870 tokens and $0.0172.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from claudio import executor
from claudio import usage as usage_mod

# A real `result` event, trimmed to the fields claudio reads.
REAL_RESULT = {
    "type": "result",
    "subtype": "success",
    "total_cost_usd": 0.0172027,
    "usage": {
        "input_tokens": 9,
        "cache_creation_input_tokens": 7604,
        "cache_read_input_tokens": 16257,
        "output_tokens": 72,
    },
    "modelUsage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 9,
            "outputTokens": 72,
            "cacheReadInputTokens": 16257,
            "cacheCreationInputTokens": 7604,
            "costUSD": 0.0172027,
            "canonicalModel": "claude-haiku-4-5",
        }
    },
}


# ---- _parse_result_usage ------------------------------------------------

def test_parses_real_result_event():
    u = executor._parse_result_usage(REAL_RESULT)
    assert u is not None
    assert u.input_tokens == 9
    assert u.output_tokens == 72
    assert u.cache_read_tokens == 16257
    assert u.cache_creation_tokens == 7604
    assert u.cost_usd == pytest.approx(0.0172027)
    # Canonical id, not the alias claudio asked for.
    assert u.model == "claude-haiku-4-5"


def test_billed_input_tokens_includes_cache_traffic():
    """The headline number: cache reads and writes are billed input."""
    u = executor._parse_result_usage(REAL_RESULT)
    assert u.billed_input_tokens == 23870


def test_falls_back_to_model_usage_when_usage_block_is_zeroed():
    """A budget-exhausted run zeroes the top-level usage block but still
    reports real per-model figures. Logging that as a free call would be a
    lie in the user's favour, which is still a lie."""
    event = {
        "type": "result",
        "terminal_reason": "budget_exhausted",
        "total_cost_usd": 0.135016,
        "usage": {"input_tokens": 0, "output_tokens": 0,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        "modelUsage": {
            "claude-sonnet-5": {
                "inputTokens": 2, "outputTokens": 50,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 33628,
                "costUSD": 0.135016, "canonicalModel": "claude-sonnet-5",
            }
        },
    }
    u = executor._parse_result_usage(event)
    assert u.billed_input_tokens == 33630
    assert u.output_tokens == 50
    assert u.cost_usd == pytest.approx(0.135016)


def test_attributes_to_the_model_that_did_the_generating():
    """After a --fallback-model hop two models appear; the one that actually
    generated the answer is the honest attribution."""
    event = {
        "type": "result",
        "total_cost_usd": 1.0,
        "usage": {"input_tokens": 10, "output_tokens": 100},
        "modelUsage": {
            "claude-opus-5": {"outputTokens": 0, "canonicalModel": "claude-opus-5"},
            "claude-sonnet-5": {"outputTokens": 100, "canonicalModel": "claude-sonnet-5"},
        },
    }
    assert executor._parse_result_usage(event).model == "claude-sonnet-5"


def test_result_without_usage_is_not_usage():
    assert executor._parse_result_usage({"type": "result", "result": "text"}) is None


def test_ignores_junk_token_values():
    event = {"type": "result", "usage": {"input_tokens": "lots", "output_tokens": -5}}
    u = executor._parse_result_usage(event)
    assert u.input_tokens == 0
    assert u.output_tokens == 0


# ---- stream + buffered plumbing ----------------------------------------

def test_result_event_surfaces_as_usage_kind():
    kind, payload = executor._parse_stream_event(json.dumps(REAL_RESULT))
    assert kind == "usage"
    assert payload.cost_usd == pytest.approx(0.0172027)


def test_partial_message_wrapper_yields_a_delta():
    event = json.dumps({
        "type": "stream_event",
        "event": {"type": "content_block_delta",
                  "delta": {"type": "text_delta", "text": "Hi! "}},
    })
    assert executor._parse_stream_event(event) == ("delta", "Hi! ")


@pytest.mark.parametrize("delta_type", ["thinking_delta", "signature_delta",
                                        "input_json_delta"])
def test_non_text_deltas_never_reach_stdout(delta_type):
    """Thinking and partial tool-input deltas must not be rendered as answer
    text: they are internal, and input_json_delta is an incomplete fragment."""
    event = json.dumps({
        "type": "stream_event",
        "event": {"type": "content_block_delta",
                  "delta": {"type": delta_type, "text": "leak"}},
    })
    assert executor._parse_stream_event(event) == ("", "")


def test_usage_from_json_output_body():
    u = executor._usage_from_json_text(json.dumps(REAL_RESULT))
    assert u.output_tokens == 72


@pytest.mark.parametrize("body", ["", "plain text answer", "{broken", "[1,2]"])
def test_usage_from_non_json_body_is_none(body):
    assert executor._usage_from_json_text(body) is None


# ---- log_request: billed wins over estimated ---------------------------

@pytest.fixture
def tmp_usage(tmp_path, monkeypatch):
    path = tmp_path / "usage.json"
    monkeypatch.setattr(usage_mod, "USAGE_FILE", path)
    return path


def test_billed_usage_supersedes_the_estimate(tmp_usage):
    u = executor._parse_result_usage(REAL_RESULT)
    # The estimate claudio would have logged on its own: wildly low.
    usage_mod.log_request("ask", "question", input_tokens=9, output_tokens=500,
                          model="haiku", usage=u)
    entry = json.loads(tmp_usage.read_text())["entries"][0]
    assert entry["basis"] == "billed"
    assert entry["input_tokens"] == 23870      # not 9
    assert entry["output_tokens"] == 72        # not the 500-token guess
    assert entry["cost"] == pytest.approx(0.0172027)
    assert entry["model"] == "claude-haiku-4-5"
    assert entry["cache_read_tokens"] == 16257


def test_without_billed_usage_entries_are_labelled_estimated(tmp_usage):
    usage_mod.log_request("ask", "question", input_tokens=1000, model="sonnet")
    entry = json.loads(tmp_usage.read_text())["entries"][0]
    assert entry["basis"] == "estimated"


def test_cache_hits_stay_free_even_with_usage_present(tmp_usage):
    """A claudio-cache hit never called Claude, so it costs nothing:
    a stale CallUsage must not be attributed to it."""
    u = executor._parse_result_usage(REAL_RESULT)
    usage_mod.log_request("ask", "question", 10, cached=True, model="haiku", usage=u)
    entry = json.loads(tmp_usage.read_text())["entries"][0]
    assert entry["cached"] is True
    assert entry["cost"] == 0.0


def test_stats_separate_billed_from_estimated(tmp_usage):
    u = executor._parse_result_usage(REAL_RESULT)
    usage_mod.log_request("ask", "question", 9, usage=u)
    usage_mod.log_request("ask", "question", 1000, model="sonnet")
    stats = usage_mod.get_stats()["all_time"]
    assert stats["billed_requests"] == 1
    assert stats["estimated_requests"] == 1
    assert stats["cache_read_tokens"] == 16257


def test_legacy_entries_without_basis_count_as_estimated(tmp_usage):
    """Ledgers written before 2.0.0 have no basis key."""
    tmp_usage.write_text(json.dumps({"entries": [
        {"ts": 0, "cmd": "ask", "mode": "question", "input_tokens": 5,
         "output_tokens": 5, "cost": 0.01, "cached": False},
    ]}))
    assert usage_mod.get_stats()["all_time"]["estimated_requests"] == 1


# ---- executor: usage reaches the caller --------------------------------

def test_buffered_json_run_returns_usage(monkeypatch):
    monkeypatch.setenv("CLAUDIO_NO_STREAM", "1")
    monkeypatch.setattr(executor, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(["claude"], 0,
                                                    json.dumps(REAL_RESULT), ""))
    result = executor.execute_prompt("hi", json_output=True)
    assert result.usage is not None
    assert result.usage.cost_usd == pytest.approx(0.0172027)


def test_plain_text_run_reports_no_usage(monkeypatch):
    """No usage envelope means None: callers then fall back to estimating,
    rather than inventing a number."""
    monkeypatch.setenv("CLAUDIO_NO_STREAM", "1")
    monkeypatch.setattr(executor, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(["claude"], 0, "the answer", ""))
    result = executor.execute_prompt("hi")
    assert result.text == "the answer"
    assert result.usage is None
