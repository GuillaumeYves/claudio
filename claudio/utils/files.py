"""File reading and ingestion utilities."""

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
