"""Noise filtering -- remove low-signal content from inputs.

Every byte removed here is a token saved. This stage runs before compression
and targets content that is never useful to Claude regardless of intent:
  - Trailing whitespace (free savings, ~2-5% on most files)
  - License/copyright headers (legal boilerplate, not code)
  - Consecutive blank lines (visual spacing, no semantic value)
  - Log noise (health checks, separators, duplicates)
  - Comment blocks when intent is behavior-focused (refactor/review/debug)
"""

import re


def filter_noise(text: str, mode: str = "auto", intent: str = "") -> str:
    """Remove noise from text based on content type.

    Args:
        text: Raw input text.
        mode: "auto", "logs", or "code".
        intent: The task intent -- affects how aggressively we filter.
                For "refactor"/"review"/"debug", comments are stripped
                since Claude needs the logic, not the docs.
    """
    if mode == "auto":
        mode = _detect_mode(text)

    if mode == "logs":
        return filter_logs(text)
    elif mode == "code":
        return filter_code(text, intent)
    return _strip_trailing_whitespace(text)


def filter_logs(text: str) -> str:
    """Filter log content: deduplicate, remove low-signal lines."""
    lines = text.splitlines()

    # Remove blank lines
    lines = [l for l in lines if l.strip()]

    # Deduplicate consecutive identical messages (keep first + count)
    deduped = []
    prev = None
    count = 0
    for line in lines:
        normalized = _normalize_log_line(line)
        if normalized == prev:
            count += 1
        else:
            if prev is not None and count > 0:
                deduped.append(f"  [x{count + 1}]")
            deduped.append(line)
            prev = normalized
            count = 0
    if count > 0:
        deduped.append(f"  [x{count + 1}]")

    # Remove low-signal log lines
    filtered = [l for l in deduped if not _is_low_signal_log(l)]

    return "\n".join(filtered)


def filter_code(text: str, intent: str = "") -> str:
    """Filter code: strip waste, optionally strip comments.

    Always:
      - Strip trailing whitespace from every line
      - Collapse 2+ blank lines into 1
      - Remove license/copyright headers
      - Remove shebang lines

    When intent is refactor/review/debug (behavior-focused):
      - Strip inline comments (# ... at end of line)
      - Strip full-line comments (lines that are only comments)
      - Strip docstrings (triple-quote blocks)
      - Collapse resulting blank lines again
    """
    # Phase 1: Universal cleanup
    lines = text.splitlines()
    lines = [l.rstrip() for l in lines]  # Strip trailing whitespace

    # Remove shebang
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]

    # Remove license/copyright header block (common at top of file)
    lines = _strip_license_header(lines)

    # Phase 2: Intent-based comment stripping
    behavior_intents = {"refactor", "review", "debug"}
    if intent in behavior_intents:
        lines = _strip_comments(lines)
        lines = _strip_docstrings(lines)

    # Phase 3: Collapse blank lines
    result = []
    blank_count = 0
    for line in lines:
        if not line.strip():
            blank_count += 1
            if blank_count <= 1:
                result.append(line)
            continue
        blank_count = 0
        result.append(line)

    # Strip leading/trailing blank lines
    while result and not result[0].strip():
        result.pop(0)
    while result and not result[-1].strip():
        result.pop()

    return "\n".join(result)


def _strip_license_header(lines: list[str]) -> list[str]:
    """Remove license/copyright comment block from the top of a file."""
    if not lines:
        return lines

    # Detect block comment style (/* ... */ or # ... block at top)
    i = 0

    # Skip initial blank lines
    while i < len(lines) and not lines[i].strip():
        i += 1

    if i >= len(lines):
        return lines

    start = i
    first = lines[i].strip()

    # Multi-line /* */ style
    if first.startswith("/*"):
        while i < len(lines):
            if "*/" in lines[i]:
                # Check if this block mentions license/copyright
                block = "\n".join(lines[start:i + 1]).lower()
                if any(kw in block for kw in ("license", "copyright", "(c)", "permission", "warranty", "redistribute")):
                    return lines[i + 1:]
                break
            i += 1
        return lines

    # Hash-comment style (Python, shell, etc.)
    if first.startswith("#"):
        while i < len(lines) and (lines[i].strip().startswith("#") or not lines[i].strip()):
            i += 1
            if i - start > 30:  # Safety: don't strip more than 30 lines
                return lines
        block = "\n".join(lines[start:i]).lower()
        if any(kw in block for kw in ("license", "copyright", "(c)", "permission", "warranty", "redistribute")):
            return lines[i:]

    return lines


def _strip_comments(lines: list[str]) -> list[str]:
    """Remove comment-only lines and inline comments from Python-style code."""
    result = []
    for line in lines:
        stripped = line.strip()

        # Full-line comment
        if stripped.startswith("#"):
            continue

        # Inline comment -- careful not to strip inside strings
        pos = line.find(" #")
        if pos != -1 and not _in_string(line, pos):
            line = line[:pos].rstrip()

        result.append(line)
    return result


def _strip_docstrings(lines: list[str]) -> list[str]:
    """Remove triple-quote docstrings."""
    result = []
    in_docstring = False
    docstring_char = None

    for line in lines:
        stripped = line.strip()

        if in_docstring:
            if docstring_char in stripped:
                in_docstring = False
            continue

        # Detect docstring start
        if stripped.startswith('"""') or stripped.startswith("'''"):
            docstring_char = stripped[:3]
            # Single-line docstring: """text"""
            if stripped.count(docstring_char) >= 2 and len(stripped) > 3:
                continue
            # Multi-line docstring start
            in_docstring = True
            continue

        result.append(line)
    return result


def _in_string(line: str, pos: int) -> bool:
    """Check if position in line is inside a string literal.

    Handles escaped quotes (\\\" and \\') correctly.
    """
    in_single = False
    in_double = False
    i = 0
    while i < pos:
        ch = line[i]
        if ch == "\\" and i + 1 < pos:
            i += 2  # Skip escaped character entirely
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        i += 1
    return in_single or in_double


def _detect_mode(text: str) -> str:
    """Heuristic to detect if text is logs or code."""
    log_indicators = 0
    code_indicators = 0
    lines = text.splitlines()[:50]

    for line in lines:
        if re.search(r"\d{4}[-/]\d{2}[-/]\d{2}", line):
            log_indicators += 1
        if re.search(r"\b(INFO|WARN|ERROR|DEBUG|TRACE)\b", line):
            log_indicators += 1
        if re.search(r"(def |class |function |import |from |const |let |var )", line):
            code_indicators += 1
        if re.search(r"[{}\[\]();]$", line.strip()):
            code_indicators += 1

    if log_indicators > code_indicators:
        return "logs"
    return "code"


def _normalize_log_line(line: str) -> str:
    """Normalize a log line for dedup comparison."""
    normalized = re.sub(r"^\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}[.\d]*\s*", "", line)
    normalized = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<ID>", normalized)
    normalized = re.sub(r"\b\d{5,}\b", "<ID>", normalized)
    return normalized


def _is_low_signal_log(line: str) -> bool:
    """Detect log lines that carry little debugging information."""
    low_signal = [
        r"^\s*$",
        r"^-+$",
        r"^=+$",
        r"^\s*\.\.\.\s*$",
        r"^(Starting|Started|Stopping|Stopped)\s+(server|service|process)",
        r"^Health check (passed|OK)",
        r"^\s*\[x\d+\]\s*$",  # Our own dedup markers alone on a line
    ]
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in low_signal)


def _strip_trailing_whitespace(text: str) -> str:
    """Strip trailing whitespace from every line."""
    return "\n".join(l.rstrip() for l in text.splitlines())
