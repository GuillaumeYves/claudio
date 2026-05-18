"""Tiny ANSI color helper.

Centralised so callers don't have to repeat the TTY/NO_COLOR dance. All
helpers degrade to plain text when:
  - the target stream is not a TTY (piped, redirected, captured)
  - $NO_COLOR is set (https://no-color.org)
  - $CLAUDIO_NO_COLOR is set (project-specific override)
"""

from __future__ import annotations

import os
import sys

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"

RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
GREY = "\x1b[90m"


def colors_enabled(stream=None) -> bool:
    """Should we emit ANSI codes for `stream` (default sys.stderr)?"""
    if os.environ.get("NO_COLOR") or os.environ.get("CLAUDIO_NO_COLOR"):
        return False
    if stream is None:
        stream = sys.stderr
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        return False


def colored(text: str, color: str, *, stream=None, bold: bool = False) -> str:
    """Wrap text in `color` (and optionally bold) when `stream` supports it."""
    if not colors_enabled(stream):
        return text
    prefix = (BOLD if bold else "") + color
    return f"{prefix}{text}{RESET}"
