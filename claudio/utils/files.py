"""File reading and ingestion utilities."""

from pathlib import Path


def read_file(path: str, lines: str | None = None) -> str:
    """Read a file, optionally extracting a line range.

    Args:
        path: File path to read.
        lines: Optional line range like "10-25" or "10".
    """
    # Exception messages are the bare reason (no path) — callers such as
    # resolve_file_attachments prepend "cannot read <path>:" so the path
    # isn't duplicated.
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError("File not found")
    if not p.is_file():
        raise ValueError("Not a file")

    try:
        raw = p.read_bytes()
    except PermissionError as e:
        raise PermissionError("Permission denied") from e
    except OSError as e:
        raise OSError(e.strerror or str(e)) from e

    # Reject binary blobs before they pollute the prompt with replacement
    # characters. A NUL byte in the first chunk is the cheap, reliable signal.
    if b"\x00" in raw[:8000]:
        raise ValueError("Binary file, not text")

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Not UTF-8 — fall back to a lossy decode so a stray byte in an
        # otherwise-text file doesn't hard-fail the whole attachment.
        content = raw.decode("utf-8", errors="replace")

    # Normalize newlines (read_bytes skips the universal-newline translation
    # read_text did) so CRLF files don't leak \r into prompts or token counts.
    content = content.replace("\r\n", "\n").replace("\r", "\n")

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
