"""Per-session memory of which files Claude has already been shown.

With REPL auto-chain (and explicit --session-id / --resume in one-shot mode),
the same Anthropic conversation can span many `ask`/`build` turns. Re-sending
the full contents of `@main.py` on every turn is wasteful — Claude already
has it from the first turn. This module tracks (path, line-range, content
hash) tuples per session_id so the prompt builder can substitute a compact
marker for files Claude has already seen verbatim.

Storage: one JSON file per session at ~/.claudio/sessions/<uuid>.json.
Schema:
  {
    "files": {
      "main.py":      "abc123...",        # sha256-16 of content
      "utils.py@1-50": "def456..."         # path@lines for partial views
    }
  }

If the content hash changes (user edited the file between turns), the
session forgets the old version and treats the new one as a fresh attach.
All I/O is best-effort: failures degrade to "treat everything as fresh".
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def _root() -> Path:
    base = os.environ.get("CLAUDIO_HOME")
    return Path(base) if base else (Path.home() / ".claudio")


def _path(session_id: str) -> Path:
    return _root() / "sessions" / f"{session_id}.json"


def _content_hash(content: str) -> str:
    """Short content fingerprint; 16 hex chars is plenty for collision-avoid."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


def _key(path: str, lines: str | None) -> str:
    """Compose the lookup key — distinct line ranges of the same file are
    treated as different views."""
    return f"{path}@{lines}" if lines else path


def _load(session_id: str) -> dict:
    p = _path(session_id)
    if not p.exists():
        return {"files": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"files": {}}
        data.setdefault("files", {})
        return data
    except (OSError, json.JSONDecodeError):
        return {"files": {}}


def _save(session_id: str, record: dict) -> None:
    p = _path(session_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        pass


def mark_files_seen(session_id: str | None, file_attachments) -> set[tuple[str, str | None]]:
    """Update the session's file-hash record and return which attachments
    Claude already has.

    Returns a set of (path, lines) keys whose hashes matched what we'd
    already recorded for this session. The caller uses that set to swap
    in an `unchanged="true"` marker instead of the full file body when
    building the prompt context.

    Files that aren't yet recorded (or whose content changed) are stored
    with their new hash and excluded from the returned set.
    """
    if not session_id:
        return set()

    record = _load(session_id)
    files_record = record["files"]
    unchanged: set[tuple[str, str | None]] = set()
    changed = False

    for fa in file_attachments:
        content = getattr(fa, "content", "") or ""
        if not content:
            continue
        path = getattr(fa, "path", "")
        lines = getattr(fa, "lines", None)
        h = _content_hash(content)
        k = _key(path, lines)
        if files_record.get(k) == h:
            unchanged.add((path, lines))
        else:
            files_record[k] = h
            changed = True

    if changed:
        _save(session_id, record)
    return unchanged


def clear(session_id: str | None) -> None:
    """Forget every file the session has been shown. Used by /fresh."""
    if not session_id:
        return
    try:
        _path(session_id).unlink(missing_ok=True)
    except OSError:
        pass
