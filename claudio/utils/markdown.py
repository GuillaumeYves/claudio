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
import shutil
import sys
from dataclasses import dataclass

from claudio.utils.colors import (
    BOLD, CLAUDIO_BLUE, DIM, GREEN, GREEN_BG, GREY, RED, RED_BG, RESET,
    colors_enabled,
)

# Strip ANSI escape sequences when measuring visible width — `\x1b[36m`
# eats 5 bytes but renders zero columns on screen.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(s: str) -> int:
    """Length of `s` in screen columns (ignores ANSI escapes)."""
    return len(_ANSI_RE.sub("", s))


def _wrap_with_indent(line: str, width: int, indent: str) -> list[str]:
    """Word-wrap an ANSI-styled line so every visual row carries `indent`.

    Returns the list of indented rows. When the line is empty, returns a
    single row containing just the indent (preserves blank-line spacing).
    Lines that fit return a single-element list.

    Wraps on word boundaries (split by ` `). ANSI escapes stay attached to
    the word they decorate, so styling never bleeds across rows.
    """
    if width <= len(indent) + 1:
        # Nonsense terminal width -- bail to "no wrap" so we never loop.
        return [indent + line]

    usable = width - len(indent)
    if _visible_len(line) <= usable:
        return [indent + line]

    words = line.split(" ")
    rows: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
            continue
        # +1 for the joining space
        if _visible_len(current) + 1 + _visible_len(word) <= usable:
            current += " " + word
        else:
            rows.append(current)
            current = word
    if current:
        rows.append(current)
    return [indent + row for row in rows]


def _term_width(default: int = 80) -> int:
    """Detect the terminal width, defaulting when stdout is piped."""
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except (OSError, ValueError):
        return default

# Italic: not all terminals render it, but the ones that don't will just
# show the text plain — same effect as the source markdown.
ITALIC = "\x1b[3m"

# Header styles -- every level uses plain `CLAUDIO_BLUE`, the exact same
# byte sequence (`\x1b[36m`) the logo, prompt, and breadcrumbs use, so a
# `## Strengths` header reads as the same brand blue as `claudio>` and
# `↳ claudio is reading ...`. We deliberately do NOT add BOLD to H1/H2:
# many terminal palettes (notably Windows Terminal) render `BOLD + CYAN`
# as a lighter teal that visually drifts away from the plain-cyan brand
# colour. Hierarchy across levels comes from DIM on the lower three,
# nothing else.
_HEADER_STYLES = (
    f"{CLAUDIO_BLUE}",         # H1 #
    f"{CLAUDIO_BLUE}",         # H2 ##
    f"{CLAUDIO_BLUE}",         # H3 ###
    f"{DIM}{CLAUDIO_BLUE}",    # H4 ####
    f"{DIM}{CLAUDIO_BLUE}",    # H5 #####
    f"{DIM}{CLAUDIO_BLUE}",    # H6 ######
)

# Semantic markers — colour success / failure glyphs so LLM check-lists
# read at a glance without manual scanning. Applied after inline styling.
_GLYPH_GREEN = ("✓", "✔", "✅")
_GLYPH_RED = ("✗", "✘", "❌")

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
    state = _FenceState()
    for raw_line in text.splitlines():
        prev_in_fence = state.in_fence
        styled = _render_line(raw_line, state)
        # Swallow fence open/close markers (empty styled + transition).
        if not styled and prev_in_fence != state.in_fence:
            continue
        out_lines.append(styled)
    return "\n".join(out_lines)


class MarkdownStream:
    """Stateful streaming markdown renderer.

    Feed it text chunks via `feed(chunk)` as they arrive from the model.
    It buffers partial lines (so styled spans like `**word**` aren't split
    across chunk boundaries) and emits styled lines to the output stream
    as soon as a newline closes one. Call `close()` at end-of-stream to
    flush any remaining partial line.

    Each emitted line is prefixed with `line_prefix` (default `"  "`, a
    two-space indent) so the response visually stands out from the user's
    prompt. The prefix is suppressed when colors are disabled (piped
    output) so machine-parsed text stays clean.
    """

    def __init__(self, out_stream=None, line_prefix: str = "  "):
        self.out = out_stream if out_stream is not None else sys.stdout
        self._buffer = ""
        self._state = _FenceState()
        self._enabled = colors_enabled(self.out)
        # Only indent when we're styling — piped/redirected output stays flush
        # left so consumers parsing it don't trip on leading whitespace.
        self._prefix = line_prefix if self._enabled else ""

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
            was_in_fence = self._state.in_fence
            styled = _render_line(line, self._state)
            # Fence transitions (open / close) come back as empty
            # styled strings and toggle the in_fence flag. They are not
            # emitted at all -- the code block has no visible border.
            if not styled and was_in_fence != self._state.in_fence:
                continue
            self._emit_line(styled, fenced=was_in_fence)

    def close(self) -> None:
        """Flush any partial line still in the buffer."""
        if self._buffer:
            if self._enabled:
                was_in_fence = self._state.in_fence
                styled = _render_line(self._buffer, self._state)
                # Trailing partial line, no closing newline
                if fenced := was_in_fence:
                    self._write(self._prefix + styled)
                else:
                    rows = _wrap_with_indent(styled, _term_width(), self._prefix)
                    self._write("\n".join(rows))
            else:
                self._write(self._buffer)
            self._buffer = ""

    def _emit_line(self, styled: str, *, fenced: bool) -> None:
        """Write one completed line with indent + (optional) hard-wrap.

        Fence open/close markers come through as empty strings (Codex-
        style code blocks have no border) -- swallow them so we don't
        emit a stray blank row. Real empty lines inside code are still
        rendered (they go through _render_code_line first and arrive
        non-empty with the gutter populated).

        Fenced lines may carry pre-wrapped multi-row output (joined by
        \\n) so the line-number gutter only paints on the first row of
        each logical code line, continuations stay aligned under the
        code position. We split and emit each visual row with the global
        indent prepended.

        Non-fenced (prose) lines pass through the word-wrap helper so the
        2-space gutter persists across soft-wrapped visual rows.
        """
        if fenced:
            if not styled:
                return  # swallowed fence marker (open/close)
            for row in styled.split("\n"):
                self._write(self._prefix + row + "\n")
            return
        if not styled:
            # Empty prose line: just write the indent + newline so the
            # blank line preserves the gutter alignment.
            self._write(self._prefix + "\n")
            return
        rows = _wrap_with_indent(styled, _term_width(), self._prefix)
        self._write("\n".join(rows) + "\n")

    def _write(self, text: str) -> None:
        try:
            self.out.write(text)
            self.out.flush()
        except (OSError, UnicodeEncodeError):
            pass


# Code-block layout (Codex-style):
#   - No outer border. Code lines stand on their own; the line-number
#     gutter is what visually separates them from prose.
#   - 4-char right-aligned line number, single space, then the line body.
#     No `│` gutter character — looks cleaner at terminal density.
#   - Diff rows: `-` lines render in red with a dark-red background tint,
#     `+` lines in green with a dark-green background tint, so they read
#     as discrete "removed" / "added" blocks at a glance.
#   - `@@` hunk headers render in claudio-blue.
#   - Plain code rows render in grey.
#   - Long lines are char-wrapped; continuation rows skip the gutter and
#     align under the code position.

_CODE_GUTTER_WIDTH = 4   # right-aligned line-number column width
_CODE_PREFIX_LEN = _CODE_GUTTER_WIDTH + 1  # gutter + one trailing space
_CODE_MIN_INNER = 30     # minimum chars available for code body

# Fence languages that opt the block into diff colouring on open.
_DIFF_LANGS = {"diff", "patch"}

# git/unified-diff hunk header: `@@ -<old_start>[,<old_n>] +<new_start>[,<new_n>] @@`
_HUNK_RE = re.compile(
    r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@"
)


@dataclass
class _FenceState:
    """All the state a code-fence renderer needs across successive lines.

    Diff fences (opened as ```diff/```patch, or any fence in which a
    `@@ ... @@` hunk header appears) are coloured: `-` rows red on dark
    red, `+` rows green on dark green, context grey. Real file line
    numbers come out of the @@ header. Plain fences number sequentially
    from 1 and never apply diff colouring -- so a `--verbose` shell flag
    or a hyphen-led English sentence inside a normal ```bash block won't
    get painted red.
    """

    in_fence: bool = False
    lang: str = ""
    line_no: int = 0    # sequential counter for non-diff fences
    is_diff: bool = False
    old_no: int = 0     # next line number for `-` rows in a diff fence
    new_no: int = 0     # next line number for `+` and context rows

    def open_fence(self, lang: str) -> None:
        self.in_fence = True
        self.lang = lang
        self.line_no = 0
        self.is_diff = lang.lower() in _DIFF_LANGS
        self.old_no = 0
        self.new_no = 0

    def close_fence(self) -> None:
        self.in_fence = False
        self.lang = ""
        self.line_no = 0
        self.is_diff = False
        self.old_no = 0
        self.new_no = 0

    def upgrade_to_diff(self) -> None:
        """Promote a non-diff fence to diff mode (called on @@ header)."""
        self.is_diff = True


def _hard_wrap_chars(text: str, width: int) -> list[str]:
    """Wrap `text` into rows of at most `width` chars each. Char-based, so
    long identifiers / strings are split mid-token if necessary -- matches
    how diff viewers handle code that can't be safely word-wrapped."""
    if not text:
        return [""]
    if len(text) <= width:
        return [text]
    return [text[i : i + width] for i in range(0, len(text), width)]


def _render_code_line(content: str, state: _FenceState) -> str:
    """Render one fence-internal line, returning one or more rows joined
    by \\n. Mutates `state` to advance line counters.

    Diff colouring is only applied when `state.is_diff` is True; in plain
    code fences a leading `-` or `+` is just code and renders grey. Line
    numbers come from the hunk-parsed old/new counters in diff mode,
    sequential from 1 otherwise.
    """
    fg, bg = GREY, ""
    line_label = ""

    if state.is_diff:
        # Hunk header: parse, update old/new counters, render in blue.
        hunk = _HUNK_RE.match(content)
        if hunk:
            state.old_no = int(hunk.group(1))
            state.new_no = int(hunk.group(2))
            fg = CLAUDIO_BLUE
            # No line number on the hunk header row -- gutter stays blank.
        elif content.startswith("-") and not content.startswith("---"):
            fg, bg = RED, RED_BG
            line_label = f"{state.old_no}"
            state.old_no += 1
        elif content.startswith("+") and not content.startswith("+++"):
            fg, bg = GREEN, GREEN_BG
            line_label = f"{state.new_no}"
            state.new_no += 1
        elif content.startswith("---") or content.startswith("+++"):
            # File-header metadata in unified diffs: leave neutral.
            fg = GREY
        else:
            # Context row: both counters advance, show new-file number.
            line_label = f"{state.new_no}"
            if state.old_no:
                state.old_no += 1
            if state.new_no:
                state.new_no += 1
    else:
        # Plain code fence: sequential numbering, no diff colouring.
        # Promote to diff mode if we belatedly see a hunk header.
        if _HUNK_RE.match(content):
            state.upgrade_to_diff()
            state.old_no = int(_HUNK_RE.match(content).group(1))
            state.new_no = int(_HUNK_RE.match(content).group(2))
            fg = CLAUDIO_BLUE
        else:
            state.line_no += 1
            line_label = f"{state.line_no}"

    # Build the gutter: line label right-aligned, or blank space when
    # the row has no number (hunk header).
    if line_label:
        num = f"{line_label:>{_CODE_GUTTER_WIDTH}}"
    else:
        num = " " * _CODE_GUTTER_WIDTH

    # Char-wrap the body. 2-space global indent + gutter eat into width.
    inner_width = max(_CODE_MIN_INNER, _term_width() - 2 - _CODE_PREFIX_LEN)
    rows = _hard_wrap_chars(content, inner_width)

    out: list[str] = []
    for i, row in enumerate(rows):
        if i == 0:
            prefix = f"{DIM}{num}{RESET} "
        else:
            prefix = " " * _CODE_PREFIX_LEN
        out.append(f"{prefix}{bg}{fg}{row}{RESET}")
    return "\n".join(out)


def _render_line(line: str, state: _FenceState) -> str:
    """Render one complete line, mutating fence `state` as a side-effect.

    Returns the styled text for the line (empty string when the line was
    a fence open/close marker -- the caller is responsible for not
    emitting a blank row for swallowed transitions). All fence-related
    bookkeeping (open/close, line counter, diff mode, hunk-derived
    line numbers) lives on `state` so the caller threads one object
    through successive lines instead of a tuple of fields.
    """
    # Code-fence transitions: no visible output (Codex-style).
    fence_match = _FENCE_RE.match(line)
    if fence_match:
        if state.in_fence:
            state.close_fence()
        else:
            state.open_fence(fence_match.group(1) or "")
        return ""

    # Inside a code fence: line-numbered code with conditional diff colour
    if state.in_fence:
        return _render_code_line(line, state)

    # Horizontal rule -- spans the response block, minus the gutter.
    if _HRULE_RE.match(line):
        rule_width = max(20, _term_width() - 4)
        return f"{DIM}{'─' * rule_width}{RESET}"

    # Header
    m = _HEADER_RE.match(line)
    if m:
        level = min(len(m.group(1)), 6) - 1
        body = _render_inline(m.group(2))
        return f"{_HEADER_STYLES[level]}{body}{RESET}"

    # Block quote
    m = _QUOTE_RE.match(line)
    if m:
        body = _render_inline(m.group(1))
        return f"{DIM}│ {RESET}{body}"

    # Bullet list
    m = _BULLET_RE.match(line)
    if m:
        indent, body = m.group(1), m.group(2)
        return f"{indent}{CLAUDIO_BLUE}•{RESET} {_render_inline(body)}"

    # Numbered list
    m = _NUM_BULLET_RE.match(line)
    if m:
        indent, num, body = m.group(1), m.group(2), m.group(3)
        return f"{indent}{CLAUDIO_BLUE}{num}.{RESET} {_render_inline(body)}"

    # Plain paragraph line: just inline styling
    return _render_inline(line)


def _render_inline(text: str) -> str:
    """Apply inline styles (bold/italic/code/strike/links/semantic) to a line.

    Inline code uses dim grey, not green: most `code spans` in LLM output
    are file paths or identifiers, not success indicators. Reserving green
    for ✓/✔/✅ and red for ✗/✘/❌ keeps a clean signal: green means "this
    is right", red means "this is wrong", grey means "this is a name".
    """
    if not text:
        return text
    # Order matters: ** before *, __ before _.
    text = _BOLD_RE.sub(lambda m: f"{BOLD}{m.group(1)}{RESET}", text)
    text = _BOLD_ALT_RE.sub(lambda m: f"{BOLD}{m.group(1)}{RESET}", text)
    text = _STRIKE_RE.sub(lambda m: f"{DIM}{m.group(1)}{RESET}", text)
    text = _CODE_RE.sub(lambda m: f"{GREY}{m.group(1)}{RESET}", text)
    text = _ITALIC_RE.sub(lambda m: f"{ITALIC}{m.group(1)}{RESET}", text)
    text = _ITALIC_ALT_RE.sub(lambda m: f"{ITALIC}{m.group(1)}{RESET}", text)
    # Links: render label as cyan, URL dim in parens
    text = _LINK_RE.sub(
        lambda m: f"{CLAUDIO_BLUE}{m.group(1)}{RESET} {DIM}({m.group(2)}){RESET}",
        text,
    )
    # Semantic glyph pass: colour the common success/failure markers.
    # Done last so it overrides any earlier styling that wrapped them.
    for g in _GLYPH_GREEN:
        text = text.replace(g, f"{GREEN}{g}{RESET}")
    for g in _GLYPH_RED:
        text = text.replace(g, f"{RED}{g}{RESET}")
    return text
