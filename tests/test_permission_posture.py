"""Tests for the permission-posture config and how build resolves it.

The setup wizard writes a `permission_posture`; claudio maps it to the
`--permission-mode` it hands the headless `claude` CLI. These cover the
mapping, config persistence, build's resolution precedence, and the
'confirm' posture's pre-apply gate.
"""

from __future__ import annotations

import json

import pytest

import claudio.config as config
from claudio.commands import build
from claudio.utils.output import Output


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point config at a temp file so tests never touch the real ~/.config."""
    cfgfile = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", cfgfile)
    monkeypatch.delenv("CLAUDIO_BUILD_PERMISSION_MODE", raising=False)
    return cfgfile


# ---- posture -> permission-mode mapping --------------------------------

def test_posture_mapping():
    assert config.posture_permission_mode("autonomous") == "bypassPermissions"
    assert config.posture_permission_mode("edits") == "acceptEdits"
    assert config.posture_permission_mode("confirm") == "acceptEdits"
    assert config.posture_permission_mode("preview") is None


def test_unknown_posture_maps_to_default():
    assert config.posture_permission_mode("bogus") == config.posture_permission_mode(
        config.DEFAULT_POSTURE
    )


# ---- save / load roundtrip --------------------------------------------

def test_config_exists_reflects_file(isolated_config):
    assert config.config_exists() is False
    config.save_config({"permission_posture": "autonomous"})
    assert config.config_exists() is True


def test_save_merges_without_clobbering(isolated_config):
    config.save_config({"permission_posture": "autonomous"})
    config.save_config({"default_model": "opus"})
    data = json.loads(isolated_config.read_text())
    assert data["permission_posture"] == "autonomous"
    assert data["default_model"] == "opus"


def test_permission_posture_falls_back_on_unknown(isolated_config):
    config.save_config({"permission_posture": "bogus"})
    assert config.permission_posture() == config.DEFAULT_POSTURE


@pytest.mark.parametrize("legacy,expected", [
    ("bypassPermissions", "autonomous"),
    ("acceptEdits", "edits"),
    ("default", "preview"),
])
def test_legacy_build_permission_mode_migrates(isolated_config, legacy, expected):
    # A pre-posture config carried only build_permission_mode.
    config.save_config({"build_permission_mode": legacy})
    assert config.permission_posture() == expected


# ---- build's permission-mode resolution -------------------------------

def test_build_mode_from_posture(isolated_config):
    config.save_config({"permission_posture": "autonomous"})
    assert build._build_permission_mode({"dry_run": False}) == "bypassPermissions"
    config.save_config({"permission_posture": "preview"})
    assert build._build_permission_mode({"dry_run": False}) is None


def test_build_dry_run_never_mutates(isolated_config):
    config.save_config({"permission_posture": "autonomous"})
    assert build._build_permission_mode({"dry_run": True}) is None


def test_env_override_wins_over_posture(isolated_config, monkeypatch):
    config.save_config({"permission_posture": "preview"})
    monkeypatch.setenv("CLAUDIO_BUILD_PERMISSION_MODE", "bypassPermissions")
    assert build._build_permission_mode({"dry_run": False}) == "bypassPermissions"
    monkeypatch.setenv("CLAUDIO_BUILD_PERMISSION_MODE", "default")
    assert build._build_permission_mode({"dry_run": False}) is None


# ---- 'confirm' posture gate -------------------------------------------

class _FakeStdin:
    def __init__(self, line: str):
        self._line = line

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        return self._line


def _gate(answer: str, monkeypatch, posture: str = "confirm") -> bool:
    config.save_config({"permission_posture": posture})
    monkeypatch.setattr("sys.stdin", _FakeStdin(answer))
    out = Output(json_mode=False, verbose=False)
    ctx = {"dry_run": False, "json_output": False}
    return build._confirm_build_if_needed([], "do x", ctx, out)


def test_confirm_gate_yes(isolated_config, monkeypatch):
    assert _gate("y\n", monkeypatch) is True


def test_confirm_gate_enter_defaults_to_yes(isolated_config, monkeypatch):
    assert _gate("\n", monkeypatch) is True


def test_confirm_gate_no_aborts(isolated_config, monkeypatch):
    assert _gate("n\n", monkeypatch) is False


def test_no_gate_under_other_postures(isolated_config, monkeypatch):
    # 'edits' must not prompt even on a TTY — gate returns True untouched.
    assert _gate("n\n", monkeypatch, posture="edits") is True
