"""Claudio configuration management."""

import json
from pathlib import Path

DEFAULT_CONFIG = {
    "claude_binary": "claude",
    "default_model": "sonnet",
    "max_input_tokens": 32000,
    "compression_threshold": 4000,
    "output_format": "text",
    "verbose": False,
    # Permission mode claudio hands to `claude` for build (refactor/generate).
    # "acceptEdits" lets Claude apply file edits without an interactive prompt
    # (required for headless `--print` runs to actually write to disk). Set to
    # "default" to make build preview-only again, or "bypassPermissions" to
    # also auto-approve shell/other tools.
    "build_permission_mode": "acceptEdits",
}

CONFIG_DIR = Path.home() / ".config" / "claudio"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_config() -> dict:
    """Load config from ~/.config/claudio/config.json, falling back to defaults."""
    config = dict(DEFAULT_CONFIG)

    if CONFIG_FILE.exists():
        try:
            user_config = json.loads(CONFIG_FILE.read_text())
            config.update(user_config)
        except (json.JSONDecodeError, OSError):
            pass

    return config
