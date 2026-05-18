"""Tests for the PyPI update checker — version compare, cache, opt-out."""

from __future__ import annotations

import json
import time

import pytest

from claudio.utils import update_check


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch, tmp_path):
    """Point CLAUDIO_HOME at a temp dir so tests never touch the real cache."""
    monkeypatch.setenv("CLAUDIO_HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDIO_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("CI", raising=False)
    yield


# ---- parse_version / is_newer ------------------------------------------

def test_parse_version_basic():
    assert update_check.parse_version("1.2.0") == (1, 2, 0)
    assert update_check.parse_version("2.10.0") == (2, 10, 0)


def test_parse_version_handles_prerelease_suffix():
    assert update_check.parse_version("1.2.0rc1") == (1, 2, 0)
    assert update_check.parse_version("1.0.0a2") == (1, 0, 0)


def test_parse_version_short_form():
    assert update_check.parse_version("1.2") == (1, 2)


def test_is_newer_simple():
    assert update_check.is_newer("1.3.0", "1.2.0") is True
    assert update_check.is_newer("1.2.0", "1.3.0") is False
    assert update_check.is_newer("1.2.0", "1.2.0") is False


def test_is_newer_handles_two_digit_minor():
    # "1.10.0" must beat "1.2.0" — string compare would get this wrong.
    assert update_check.is_newer("1.10.0", "1.2.0") is True


def test_is_newer_garbage_input_is_false():
    assert update_check.is_newer("not-a-version", "1.0.0") is False


# ---- cache --------------------------------------------------------------

def test_write_then_read_cache():
    update_check.write_cache("9.9.9", checked_at=12345.0)
    cache = update_check.read_cache()
    assert cache == {"latest": "9.9.9", "checked_at": 12345.0}


def test_read_cache_missing_file_returns_none():
    assert update_check.read_cache() is None


def test_read_cache_corrupt_returns_none(tmp_path):
    path = update_check._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert update_check.read_cache() is None


def test_cache_freshness_window():
    now = 1_000_000.0
    fresh = {"checked_at": now - 60}
    stale = {"checked_at": now - update_check.CHECK_INTERVAL_SECONDS - 1}
    assert update_check._cache_is_fresh(fresh, now=now) is True
    assert update_check._cache_is_fresh(stale, now=now) is False


# ---- pending_notice ----------------------------------------------------

def test_pending_notice_when_newer_in_cache():
    update_check.write_cache("99.0.0")
    notice = update_check.pending_notice(current="1.0.0")
    assert notice is not None
    assert "99.0.0" in notice
    assert "1.0.0" in notice
    assert "claudio-cli" in notice


def test_pending_notice_when_no_cache():
    assert update_check.pending_notice(current="1.0.0") is None


def test_pending_notice_when_local_already_latest():
    update_check.write_cache("1.0.0")
    assert update_check.pending_notice(current="1.0.0") is None


def test_pending_notice_when_local_is_newer_than_cache():
    update_check.write_cache("0.5.0")
    assert update_check.pending_notice(current="1.2.0") is None


# ---- opt-out -----------------------------------------------------------

def test_disabled_via_env_var(monkeypatch):
    monkeypatch.setenv("CLAUDIO_NO_UPDATE_CHECK", "1")
    update_check.write_cache("99.0.0")
    assert update_check.pending_notice(current="1.0.0") is None
    assert update_check.start_background_check() is None


def test_disabled_in_ci(monkeypatch):
    monkeypatch.setenv("CI", "true")
    update_check.write_cache("99.0.0")
    assert update_check.pending_notice(current="1.0.0") is None
    assert update_check.start_background_check() is None


# ---- background check --------------------------------------------------

def test_background_check_skipped_when_cache_is_fresh(monkeypatch):
    update_check.write_cache("1.0.0", checked_at=time.time())
    called = []
    monkeypatch.setattr(update_check, "fetch_latest_pypi", lambda timeout=3: called.append(1))
    t = update_check.start_background_check()
    assert t is None
    assert called == []


def test_background_check_runs_when_cache_is_stale(monkeypatch):
    update_check.write_cache("1.0.0", checked_at=0)  # ancient
    monkeypatch.setattr(update_check, "fetch_latest_pypi", lambda timeout=3: "42.0.0")
    t = update_check.start_background_check()
    assert t is not None
    t.join(timeout=2.0)
    cache = update_check.read_cache()
    assert cache["latest"] == "42.0.0"


def test_background_check_silent_when_fetch_fails(monkeypatch):
    monkeypatch.setattr(update_check, "fetch_latest_pypi", lambda timeout=3: None)
    t = update_check.start_background_check()
    assert t is not None
    t.join(timeout=2.0)
    # No cache written when fetch returns None
    assert update_check.read_cache() is None


def test_fetch_handles_network_errors(monkeypatch):
    def boom(req, timeout=3):
        raise OSError("no network")
    monkeypatch.setattr(update_check.urllib.request, "urlopen", boom)
    assert update_check.fetch_latest_pypi(timeout=0.1) is None
