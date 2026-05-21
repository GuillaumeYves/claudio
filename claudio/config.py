"""Claudio configuration management."""

import json
from pathlib import Path

# How much claudio lets Claude do on its own. This is the user-facing knob set
# by the setup wizard (`claudio setup` / `/setup`). Because claudio drives
# `claude --print` headless — where there is no human to answer a mid-run
# permission popup — each posture maps to the closest honest CLI behaviour:
#
#   autonomous -> bypassPermissions : auto-apply edits AND run shell commands
#   edits      -> acceptEdits        : auto-apply edits, but no shell commands
#   confirm    -> acceptEdits + gate : claudio asks Y/n once before a build
#                                       applies, then auto-applies the edits
#   preview    -> None (default mode): never apply; build just prints the diff
#
# "confirm" is a coarse, per-invocation gate (claudio's own prompt), not a
# per-tool popup — the latter needs the deferred stream-input control protocol.
PERMISSION_POSTURES = {
    "autonomous": "bypassPermissions",
    "edits": "acceptEdits",
    "confirm": "acceptEdits",
    "preview": None,
}
DEFAULT_POSTURE = "edits"

DEFAULT_CONFIG = {
    "claude_binary": "claude",
    "default_model": "sonnet",
    "max_input_tokens": 32000,
    "compression_threshold": 4000,
    "output_format": "text",
    "verbose": False,
    # See PERMISSION_POSTURES above. Set by the setup wizard; governs what
    # `build` is allowed to do on disk.
    "permission_posture": DEFAULT_POSTURE,
}

CONFIG_DIR = Path.home() / ".config" / "claudio"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _raw_config() -> dict:
    """Return only what's actually written to disk (no defaults merged in).

    Used to tell an explicitly-set key from a default — needed for the legacy
    `build_permission_mode` -> `permission_posture` migration below.
    """
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def load_config() -> dict:
    """Load config from ~/.config/claudio/config.json, falling back to defaults."""
    config = dict(DEFAULT_CONFIG)
    config.update(_raw_config())
    return config


def config_exists() -> bool:
    """True if a config file has been written (used to detect first run)."""
    return CONFIG_FILE.exists()


def save_config(updates: dict) -> Path:
    """Merge `updates` into the on-disk config and persist it.

    Reads the current file (ignoring a corrupt one), applies the updates, and
    writes pretty JSON back. Creates the config dir if missing. Returns the
    path written so callers can show it to the user.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    current: dict = {}
    if CONFIG_FILE.exists():
        try:
            current = json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            current = {}
    current.update(updates)
    CONFIG_FILE.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return CONFIG_FILE


# Reverse of PERMISSION_POSTURES, for migrating configs written before the
# posture model existed (they carried a raw `build_permission_mode`).
_LEGACY_MODE_TO_POSTURE = {
    "bypassPermissions": "autonomous",
    "acceptEdits": "edits",
    "default": "preview",
    "": "preview",
}


def permission_posture() -> str:
    """Return the configured posture, falling back to the default if unknown.

    Reads the raw file so a pre-posture config (one with only the legacy
    `build_permission_mode` key) is migrated to the matching posture instead
    of silently reverting to the default.
    """
    raw = _raw_config()
    posture = raw.get("permission_posture")
    if posture in PERMISSION_POSTURES:
        return posture
    legacy = raw.get("build_permission_mode")
    if legacy is not None:
        return _LEGACY_MODE_TO_POSTURE.get(str(legacy).strip(), DEFAULT_POSTURE)
    return DEFAULT_POSTURE


def posture_permission_mode(posture: str | None = None) -> str | None:
    """Map a posture to the `--permission-mode` value claudio passes `claude`.

    None means "no mutating mode" (preview-only). Defaults to the configured
    posture when called with no argument.
    """
    if posture is None:
        posture = permission_posture()
    return PERMISSION_POSTURES.get(posture, PERMISSION_POSTURES[DEFAULT_POSTURE])
