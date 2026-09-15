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
#   preview    -> plan               : never apply; Claude reports what it
#                                       would do instead of doing it
#
# "confirm" is a coarse, per-invocation gate (claudio's own prompt), not a
# per-tool popup — the latter needs the deferred stream-input control protocol.
#
# `preview` used to map to None, leaning on the fact that headless `claude
# --print` auto-denies mutating tools when nobody can approve them. That
# worked, but only as a side effect: Claude would still *attempt* edits and
# have them silently denied, so you paid for tool calls that could never
# land. `plan` is the mode built for this — Claude plans rather than edits.
PERMISSION_POSTURES = {
    "autonomous": "bypassPermissions",
    "edits": "acceptEdits",
    "confirm": "acceptEdits",
    "preview": "plan",
}
DEFAULT_POSTURE = "edits"

DEFAULT_CONFIG = {
    "claude_binary": "claude",
    "default_model": "sonnet",
    "max_input_tokens": 32000,
    # Cap on Claude's agentic tool-use loop per call (see executor._max_turns).
    # Stops an ambiguous prompt from searching the repo until the timeout.
    "max_turns": 12,
    "output_format": "text",
    "verbose": False,
    # Effort level passed to `claude --effort` (low|medium|high|xhigh|max).
    # None means "don't pass the flag" and lets the CLI use its own default.
    # A cost/quality lever within one model — often a better trade than
    # dropping to a weaker tier.
    "effort": None,
    # Hard ceiling in USD for a single call (`claude --max-budget-usd`). None
    # disables the cap. Bounds spend directly instead of via max_turns, which
    # only caps turns as a proxy.
    "max_budget_usd": None,
    # Ask the CLI for token-by-token deltas rather than one snapshot per
    # content block. Purely cosmetic; set false if your terminal struggles.
    "partial_messages": True,
    # Cumulative ceiling across ALL calls in a day, in USD. None disables.
    # Unlike max_budget_usd (one call), this is what people actually worry
    # about; the remainder is passed to each call so the CLI stops mid-run
    # rather than overshooting. Only honest because the ledger now records
    # billed figures — see budget.py.
    "daily_budget_usd": None,
    # Pre-flight specification check: name what a request leaves unpinned,
    # locally and for free, before paying for an answer built on a guess.
    # Advisory by default; `strict_spec` turns findings into a refusal.
    "spec_check": True,
    "strict_spec": False,
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
    "plan": "preview",
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
