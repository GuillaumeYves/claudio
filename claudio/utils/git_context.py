"""Auto-discover git changes relevant to the current task.

When the dev runs `claudio ask -review @auth.py "anything broken"`, the
single most relevant context is usually NOT the whole file — it's the
diff they just wrote and haven't committed. Same story for `-debug`:
the bug is almost always in the lines that changed last.

This module produces a compact `<changes>` block when:

  - the cwd is inside a git repo, and
  - either `git diff HEAD` has output (uncommitted work), or
  - the current branch is ahead of an upstream branch (committed-but-
    unmerged work).

Output is capped (default 6,000 chars) and ringfenced behind
CLAUDIO_NO_GIT_CONTEXT for opt-out. Failures of any git command degrade
to '' silently — git context is a bonus, never load-bearing.
"""

from __future__ import annotations

import os
import subprocess

_MAX_DIFF_CHARS = 6_000
_TRUNC_MARKER = "\n... [diff truncated]"

# Branches we try as the "main" reference, in order. First one that
# resolves wins.
_BASE_CANDIDATES = ("origin/main", "origin/master", "main", "master")

# Intents that actually benefit from a diff. For `generate`, the file
# context doesn't usually exist yet; for general questions, the diff
# would be noise.
_DIFF_INTENTS = {"review", "debug", "refactor"}


def discover_git_changes(
    intent: str = "",
    cwd: str | os.PathLike | None = None,
) -> str:
    """Return a compact diff string, or '' if none applies.

    Args:
        intent: pipeline intent. Diff context is only produced for
                review/debug/refactor.
        cwd: workspace root (defaults to current working directory).
    """
    if os.environ.get("CLAUDIO_NO_GIT_CONTEXT"):
        return ""
    if intent not in _DIFF_INTENTS:
        return ""
    if not _is_git_repo(cwd):
        return ""

    sections: list[str] = []

    # 1) Uncommitted work (staged + unstaged)
    uncommitted = _run_git(["diff", "HEAD", "--stat", "--patch"], cwd)
    if uncommitted:
        sections.append("[uncommitted (git diff HEAD)]\n" + uncommitted)

    # 2) Committed work on the current branch vs. the base
    base = _find_base_branch(cwd)
    if base:
        branch_diff = _run_git(["diff", f"{base}...HEAD", "--stat", "--patch"], cwd)
        if branch_diff:
            sections.append(f"[branch vs {base}]\n{branch_diff}")

    if not sections:
        return ""

    combined = "\n\n".join(sections).strip()
    if len(combined) > _MAX_DIFF_CHARS:
        combined = combined[: _MAX_DIFF_CHARS - len(_TRUNC_MARKER)] + _TRUNC_MARKER
    return combined


def _is_git_repo(cwd: str | os.PathLike | None) -> bool:
    out = _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    return out.strip() == "true"


def _find_base_branch(cwd: str | os.PathLike | None) -> str:
    """Return the first base-branch ref that exists, or ''."""
    # If we're already on main/master, no branch diff makes sense.
    current = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).strip()
    if current in ("main", "master"):
        return ""

    for ref in _BASE_CANDIDATES:
        if _ref_exists(ref, cwd):
            # Don't compare a branch to itself
            if ref.endswith("/" + current) or ref == current:
                continue
            return ref
    return ""


def _ref_exists(ref: str, cwd: str | os.PathLike | None) -> bool:
    out = _run_git(["rev-parse", "--verify", "--quiet", ref], cwd)
    return bool(out.strip())


def _run_git(args: list[str], cwd: str | os.PathLike | None) -> str:
    """Run a git command and return stdout, '' on failure or non-zero exit."""
    workdir = str(cwd) if cwd else None
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""
