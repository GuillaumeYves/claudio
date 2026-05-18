"""Markdown -> ANSI rendering for terminal output.

LLMs reply in markdown. Showing the raw `**bold**` / `## Header` /
fenced-code source in a terminal is ugly. This module converts markdown
to ANSI styling using the existing colors helper -- no new dependency,
no heavy framework.

Two entry points:

  render(text)
      One-shot: render a full string. Used in the buffered output path.

  MarkdownStream(out_stream)
      Stateful: feed it text chunks as they arrive (streaming path). It
      buffers partial lines until a newline, tracks code-fence state, and
      writes styled output to out_stream as each line completes.

Both paths degrade to plain text when colors are disabled (no TTY,
NO_COLOR, CLAUDIO_NO_COLOR) -- callers don't need to gate themselves.
"""

from __future__ import annotations

import re
import sys

from claudio.utils.colors import (
    BOLD, CYAN, DIM, GREEN, RESET, YELLOW, colors_enabled,
)

# Italic: not all terminals render it, but the ones that don't will just
# show the text plain — same effect as the source markdown.
ITALIC = "\x1b[3m"

# Header styles ranked by depth -- H1 most prominent, H6 dimmer.
_HEADER_STYLES = (
    f"{BOLD}{CYAN}",      # #
    f"{BOLD}{CYAN}",      # ##
    f"{BOLD}",            # ###
    f"{BOLD}{YELLOW}",    # ####
    f"{YELLOW}",          # #####
    f"{DIM}",             # ######
)

# Inline patterns. Order matters: process **bold** before *italic* so the
# italic regex doesn't eat half a bold marker.
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_BOLD_ALT_RE = re.compile(r"__(.+?)__")
_ITALIC_RE = re.compile(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?![\*\w])")
_ITALIC_ALT_RE = re.compile(r"(?<![_\w])_(?!\s)(.+?)(?<!\s)_(?![_\w])")
_CODE_RE = re.compile(r"`([^`]+)`")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# Block-level patterns (anchored to line start).
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUM_BULLET_RE = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_FENCE_RE = re.compile(r"^\s*```(\w*)\s*$")
_HRULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def render(text: str, *, stream=None) -> str:
    """Render a full markdown string to ANSI.

    When colors are disabled (piped/redirected/NO_COLOR), returns the
    source markdown unchanged -- raw markdown is the right plain-text
    fallback since it's still readable.
    """
    if not colors_enabled(stream or sys.stdout):
        return text

    out_lines: list[str] = []
    in_fence = False
    fence_lang = ""
    for raw_line in text.splitlines():
        styled, in_fence, fence_lang = _render_line(raw_line, in_fence, fence_lang)
        out_lines.append(styled)
    return "\n".join(out_lines)


class MarkdownStream:
    """Stateful streaming markdown renderer.

    Feed it text chunks via `feed(chunk)` as they arrive from the model.
    It buffers partial lines (so styled spans like `**word**` aren't split
    across chunk boundaries) and emits styled lines to the output stream
    as soon as a newline closes one. Call `close()` at end-of-stream to
    flush any remaining partial line.

    When colors are disabled, all writes are pass-through plain text.
    """

    def __init__(self, out_stream=None):
        self.out = out_stream if out_stream is not None else sys.stdout
        self._buffer = ""
        self._in_fence = False
        self._fence_lang = ""
        self._enabled = colors_enabled(self.out)

    def feed(self, chunk: str) -> None:
        """Append a chunk; emit any complete lines that result."""
        if not chunk:
            return
        if not self._enabled:
            self._write(chunk)
            return

        self._buffer += chunk
        # Split off complete lines, keep last partial in buffer.
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            styled, self._in_fence, self._fence_lang = _render_line(
                line, self._in_fence, self._fence_lang
            )
            self._write(styled + "\n")

    def close(self) -> None:
        """Flush any partial line still in the buffer."""
        if self._buffer:
            if self._enabled:
                styled, self._in_fence, self._fence_lang = _render_line(
                    self._buffer, self._in_fence, self._fence_lang
                )
                self._write(styled)
            else:
                self._write(self._buffer)
            self._buffer = ""

    def _write(self, text: str) -> None:
        try:
            self.out.write(text)
            self.out.flush()
        except (OSError, UnicodeEncodeError):
            pass


def _render_line(
    line: str, in_fence: bool, fence_lang: str
) -> tuple[str, bool, str]:
    """Render one complete line. Returns (styled_line, new_in_fence, new_lang)."""
    # Code-fence transitions
    fence_match = _FENCE_RE.match(line)
    if fence_match:
        new_fence = not in_fence
        lang = fence_match.group(1) if new_fence else ""
        # Render the fence line itself as a dim separator
        marker = "```" + (fence_match.group(1) if new_fence else "")
        return f"{DIM}{marker}{RESET}", new_fence, lang

    # Inside a code fence: render whole line dim, skip inline styling
    if in_fence:
        return f"{DIM}{line}{RESET}", in_fence, fence_lang

    # Horizontal rule
    if _HRULE_RE.match(line):
        return f"{DIM}{'─' * 40}{RESET}", in_fence, fence_lang

    # Header
    m = _HEADER_RE.match(line)
    if m:
        level = min(len(m.group(1)), 6) - 1
        body = _render_inline(m.group(2))
        return f"{_HEADER_STYLES[level]}{body}{RESET}", in_fence, fence_lang

    # Block quote
    m = _QUOTE_RE.match(line)
    if m:
        body = _render_inline(m.group(1))
        return f"{DIM}│ {RESET}{body}", in_fence, fence_lang

    # Bullet list
    m = _BULLET_RE.match(line)
    if m:
        indent, body = m.group(1), m.group(2)
        return f"{indent}{CYAN}•{RESET} {_render_inline(body)}", in_fence, fence_lang

    # Numbered list
    m = _NUM_BULLET_RE.match(line)
    if m:
        indent, num, body = m.group(1), m.group(2), m.group(3)
        return f"{indent}{CYAN}{num}.{RESET} {_render_inline(body)}", in_fence, fence_lang

    # Plain paragraph line: just inline styling
    return _render_inline(line), in_fence, fence_lang


def _render_inline(text: str) -> str:
    """Apply inline styles (bold/italic/code/strike/links) to a line."""
    if not text:
        return text
    # Order matters: ** before *, __ before _.
    text = _BOLD_RE.sub(lambda m: f"{BOLD}{m.group(1)}{RESET}", text)
    text = _BOLD_ALT_RE.sub(lambda m: f"{BOLD}{m.group(1)}{RESET}", text)
    text = _STRIKE_RE.sub(lambda m: f"{DIM}{m.group(1)}{RESET}", text)
    text = _CODE_RE.sub(lambda m: f"{GREEN}{m.group(1)}{RESET}", text)
    text = _ITALIC_RE.sub(lambda m: f"{ITALIC}{m.group(1)}{RESET}", text)
    text = _ITALIC_ALT_RE.sub(lambda m: f"{ITALIC}{m.group(1)}{RESET}", text)
    # Links: render label as cyan, URL dim in parens
    text = _LINK_RE.sub(
        lambda m: f"{CYAN}{m.group(1)}{RESET} {DIM}({m.group(2)}){RESET}",
        text,
    )
    return text
