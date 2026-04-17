"""File reading and ingestion utilities."""

import sys
from pathlib import Path


def read_file(path: str, lines: str | None = None) -> str:
    """Read a file, optionally extracting a line range.

    Args:
        path: File path to read.
        lines: Optional line range like "10-25" or "10".
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not p.is_file():
        raise ValueError(f"Not a file: {path}")

    content = p.read_text(encoding="utf-8", errors="replace")

    if lines:
        content = extract_lines(content, lines)

    return content


def extract_lines(content: str, line_spec: str) -> str:
    """Extract specific lines from content.

    Supports: "10" (single line), "10-25" (range), "10-" (from line to end).
    """
    all_lines = content.splitlines(keepends=True)

    if "-" in line_spec:
        parts = line_spec.split("-", 1)
        start = int(parts[0]) - 1
        end = int(parts[1]) if parts[1] else len(all_lines)
    else:
        start = int(line_spec) - 1
        end = start + 1

    start = max(0, start)
    end = min(len(all_lines), end)
    return "".join(all_lines[start:end])


def read_stdin() -> str:
    """Read all content from stdin (non-blocking check)."""
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def collect_files(directory: str, extensions: set[str] | None = None, max_files: int = 50) -> list[Path]:
    """Collect relevant files from a directory."""
    p = Path(directory)
    if not p.is_dir():
        raise ValueError(f"Not a directory: {directory}")

    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".tox"}

    files = []
    for item in sorted(p.rglob("*")):
        if any(skip in item.parts for skip in skip_dirs):
            continue
        if not item.is_file():
            continue
        if extensions and item.suffix.lower() not in extensions:
            continue
        files.append(item)
        if len(files) >= max_files:
            break

    return files
