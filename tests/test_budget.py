"""Cumulative spend enforcement.

A per-call cap cannot answer "how much has today cost me". These tests pin
the two mechanisms that together make a daily cap real: refusing when it is
already spent, and *clamping* the per-call ceiling to whatever is left so a
single call cannot overshoot it.
"""

from __future__ import annotations

import json

import pytest

from claudio import budget
from claudio import usage as usage_mod


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    """Point the ledger at a temp file and clear budget env/config."""
    path = tmp_path / "usage.json"
    monkeypatch.setattr(usage_mod, "USAGE_FILE", path)
    monkeypatch.delenv("CLAUDIO_DAILY_BUDGET_USD", raising=False)
    monkeypatch.setattr(budget, "load_config", lambda: {})
    return path


def spend(amount: float, ledger, ts: float | None = None) -> None:
    """Write a billed entry worth `amount` dollars."""
    import time
    data = {"entries": []}
    if ledger.exists():
        data = json.loads(ledger.read_text())
    data["entries"].append({
        "ts": ts if ts is not None else time.time(),
        "cmd": "ask", "mode": "question", "model": "claude-opus-5",
        "input_tokens": 1000, "output_tokens": 100,
        "cost": amount, "cached": False, "basis": "billed",
    })
    ledger.write_text(json.dumps(data))


# ---- resolving the cap --------------------------------------------------

def test_no_cap_configured_means_no_constraint():
    allowed, effective, note = budget.check()
    assert allowed is True
    assert effective is None
    assert note is None


def test_cap_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDIO_DAILY_BUDGET_USD", "5")
    assert budget.daily_cap() == 5.0


def test_cap_from_config(monkeypatch):
    monkeypatch.setattr(budget, "load_config", lambda: {"daily_budget_usd": 2.5})
    assert budget.daily_cap() == 2.5


def test_env_overrides_config(monkeypatch):
    monkeypatch.setattr(budget, "load_config", lambda: {"daily_budget_usd": 2.5})
    monkeypatch.setenv("CLAUDIO_DAILY_BUDGET_USD", "9")
    assert budget.daily_cap() == 9.0


@pytest.mark.parametrize("bad", ["", "abc", "0", "-3", None])
def test_nonsense_caps_disable_rather_than_block(monkeypatch, bad):
    """A typo in config must not brick the tool by blocking every call."""
    monkeypatch.setattr(budget, "load_config", lambda: {"daily_budget_usd": bad})
    assert budget.daily_cap() is None
    assert budget.check()[0] is True


# ---- refusing -----------------------------------------------------------

def test_refuses_once_the_cap_is_spent(monkeypatch, isolated_ledger):
    monkeypatch.setenv("CLAUDIO_DAILY_BUDGET_USD", "1.00")
    spend(1.50, isolated_ledger)
    allowed, effective, note = budget.check()
    assert allowed is False
    assert effective is None
    assert "daily budget reached" in note
    assert "$1.00" in note


def test_yesterdays_spend_does_not_count(monkeypatch, isolated_ledger):
    monkeypatch.setenv("CLAUDIO_DAILY_BUDGET_USD", "1.00")
    spend(5.00, isolated_ledger, ts=0)  # epoch: definitely not today
    assert budget.check()[0] is True


# ---- clamping -----------------------------------------------------------

def test_clamps_the_call_to_what_is_left(monkeypatch, isolated_ledger):
    """The mechanism that makes the cap real: without it, a call started a
    cent under the cap could still bill many dollars."""
    monkeypatch.setenv("CLAUDIO_DAILY_BUDGET_USD", "1.00")
    spend(0.75, isolated_ledger)
    allowed, effective, note = budget.check()
    assert allowed is True
    assert effective == pytest.approx(0.25)
    assert "0.25" in note


def test_a_tighter_per_call_cap_is_respected(monkeypatch, isolated_ledger):
    monkeypatch.setenv("CLAUDIO_DAILY_BUDGET_USD", "10.00")
    allowed, effective, note = budget.check(requested_max=0.05)
    assert effective == 0.05
    assert note is None          # nothing to say; the user's own cap binds


def test_a_per_call_cap_can_never_raise_the_daily_ceiling(monkeypatch,
                                                          isolated_ledger):
    """Asking for --max-budget-usd 50 with $0.25 of daily budget left must
    not grant $50."""
    monkeypatch.setenv("CLAUDIO_DAILY_BUDGET_USD", "1.00")
    spend(0.75, isolated_ledger)
    allowed, effective, _ = budget.check(requested_max=50.0)
    assert allowed is True
    assert effective == pytest.approx(0.25)


def test_remaining_never_goes_negative(monkeypatch, isolated_ledger):
    monkeypatch.setenv("CLAUDIO_DAILY_BUDGET_USD", "1.00")
    spend(3.00, isolated_ledger)
    assert budget.remaining_today() == 0.0


def test_remaining_is_none_without_a_cap(isolated_ledger):
    assert budget.remaining_today() is None


def test_spend_accumulates_across_entries(monkeypatch, isolated_ledger):
    spend(0.10, isolated_ledger)
    spend(0.25, isolated_ledger)
    assert budget.spent_today() == pytest.approx(0.35)
