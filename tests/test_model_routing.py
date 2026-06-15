"""Tests for command-aware model resolution (resolve_model)."""

from __future__ import annotations

from claudio.commands import run_prompt


def _no_config(monkeypatch, **overrides):
    """Force resolve_model onto the routing path (default 'sonnet' config)."""
    cfg = {"default_model": "sonnet"}
    cfg.update(overrides)
    monkeypatch.setattr(run_prompt, "load_config", lambda: cfg)


def test_build_floors_to_opus_regardless_of_size(monkeypatch):
    """`build` writes code, so it gets Opus even on a tiny prompt — size-based
    routing would otherwise drop most builds to Sonnet."""
    _no_config(monkeypatch)
    assert run_prompt.resolve_model({}, "generate", 50, cmd="build") == "opus"
    assert run_prompt.resolve_model({}, "refactor", 50, cmd="build") == "opus"


def test_non_build_still_routes_by_size(monkeypatch):
    """ask/run keep size-based routing — a tiny light prompt stays cheap."""
    _no_config(monkeypatch)
    assert run_prompt.resolve_model({}, "question", 50, cmd="ask") == "haiku"


def test_explicit_model_flag_wins_over_build_floor(monkeypatch):
    _no_config(monkeypatch)
    assert run_prompt.resolve_model({"model": "haiku"}, "generate", 50, cmd="build") == "haiku"


def test_explicit_config_model_wins_over_build_floor(monkeypatch):
    """A user who pinned a non-default default_model keeps it, even for build."""
    _no_config(monkeypatch, default_model="haiku")
    assert run_prompt.resolve_model({}, "generate", 50, cmd="build") == "haiku"
