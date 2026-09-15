"""Pre-build file snapshots for `/undo`.

`build` applies edits directly to disk (and, under the Autonomous posture, runs
shell commands too). Before a mutating build touches anything, we record the
current bytes of every target file — or note that a target does not exist yet.
`/undo` in the REPL then restores that state: modified files revert to their
saved content, and files the build created (targets that did not exist before)
are deleted.

The snapshot is persisted under `.claudio/undo/last.json` (cwd-relative, like
`.claudio/cache/`), so `/undo` works even across separate invocations — a
one-shot `claudio build …` followed by `claudio` → `/undo` — not just within a
single REPL session. Each build overwrites the previous snapshot: undo covers
the *last* build only, and is consumed once applied.

Scope: targets are the build's attached `@file` paths. This fully covers
`refactor` (overwriting existing files — the dangerous case) and any `generate`
that writes to an attached path. A `generate` that creates brand-new files not
named on the command line is out of scope; `/undo` can't revert what it was
never told to watch.
"""

import base64
import json
import time
from pathlib import Path

_MANIFEST = Path(".claudio") / "undo" / "last.json"


def snapshot(paths: list[str]) -> None:
    """Record the pre-build state of `paths`, replacing any prior snapshot.

    Existing files are stored with their current bytes; missing targets are
    recorded as `existed: false` so `/undo` can delete them if the build
    creates them. A no-op when `paths` is empty.
    """
    entries = []
    for p in paths:
        fp = Path(p)
        if fp.is_file():
            try:
                data = fp.read_bytes()
            except OSError:
                continue
            entries.append({
                "path": str(p),
                "existed": True,
                "content": base64.b64encode(data).decode("ascii"),
            })
        else:
            entries.append({"path": str(p), "existed": False, "content": None})

    if not entries:
        return

    _MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    _MANIFEST.write_text(
        json.dumps({"created_at": time.time(), "files": entries}),
        encoding="utf-8",
    )


def has_snapshot() -> bool:
    """True when a build snapshot is available to undo."""
    return _MANIFEST.is_file()


def undo() -> tuple[list[str], list[str], list[str]] | None:
    """Restore the last snapshot.

    Returns `(restored, deleted, errors)` — lists of file paths reverted, files
    deleted (build-created), and human-readable failures — or `None` when there
    is no snapshot to apply. The manifest is consumed on success so a second
    `/undo` doesn't re-restore stale state.
    """
    if not _MANIFEST.is_file():
        return None
    try:
        manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    restored: list[str] = []
    deleted: list[str] = []
    errors: list[str] = []

    for entry in manifest.get("files", []):
        fp = Path(entry["path"])
        try:
            if entry.get("existed"):
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_bytes(base64.b64decode(entry["content"]))
                restored.append(entry["path"])
            elif fp.is_file():
                fp.unlink()
                deleted.append(entry["path"])
        except OSError as ex:
            errors.append(f"{entry['path']}: {ex}")

    try:
        _MANIFEST.unlink()
    except OSError:
        pass

    return restored, deleted, errors
