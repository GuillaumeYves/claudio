"""Project-level preamble discovery.

When claudio runs in a workspace, three sources inject codebase-specific
context into every call's cacheable prefix:

  - .claudio/project.md (claudio-specific tighter preamble)
  - CLAUDE.md (Claude Code's project memory file)
  - Auto-detected stack from manifest files (pyproject.toml, etc.)

All are optional. When present, their content is wrapped in a <project>
tag and prepended to the prompt (before <rules>). Because it sits in the
stable prefix, Anthropic's prompt cache reuses it across every call in
the same session.

The combined preamble is capped (default 2,000 chars) to keep cache
overhead bounded — preamble files that grow huge will be truncated with
a marker rather than blow the prefix budget.
"""

from __future__ import annotations

import os
from pathlib import Path

from claudio.utils.stack_detect import detect_stack

_MAX_PREAMBLE_CHARS = 2_000
_TRUNC_MARKER = "\n... [truncated]"


def discover_project_preamble(cwd: str | os.PathLike | None = None) -> str:
    """Return the project preamble text for `cwd`, or '' if none found.

    Order: .claudio/project.md, CLAUDE.md, then auto-detected stack. The
    narrower the source, the higher in the preamble — author-written
    docs override anything we inferred from manifests.
    """
    if os.environ.get("CLAUDIO_NO_PREAMBLE"):
        return ""

    root = Path(cwd) if cwd else Path.cwd()
    parts: list[str] = []

    project_md = root / ".claudio" / "project.md"
    claude_md = root / "CLAUDE.md"

    for label, path in (("project.md", project_md), ("CLAUDE.md", claude_md)):
        text = _read(path)
        if text:
            parts.append(f"[from {label}]\n{text}")

    stack = detect_stack(root)
    if stack:
        parts.append(f"[stack]\n{stack}")

    if not parts:
        return ""

    combined = "\n\n".join(parts).strip()
    if len(combined) > _MAX_PREAMBLE_CHARS:
        combined = combined[: _MAX_PREAMBLE_CHARS - len(_TRUNC_MARKER)] + _TRUNC_MARKER
    return combined


def _read(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
