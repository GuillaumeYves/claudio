"""Tests for the markdown -> ANSI renderer."""

from __future__ import annotations

import io

import pytest

from claudio.utils.markdown import (
    MarkdownStream,
    _visible_len,
    _wrap_with_indent,
    render,
)


@pytest.fixture(autouse=True)
def _force_color(monkeypatch):
    """Force color-enabled output regardless of test environment."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLAUDIO_NO_COLOR", raising=False)


class _FakeTTY(io.StringIO):
    """StringIO that pretends to be a TTY so colors_enabled() returns True."""
    def isatty(self) -> bool:
        return True


# ---- render() one-shot -------------------------------------------------

def test_render_bold():
    out = render("**hello**", stream=_FakeTTY())
    assert "\x1b[1m" in out
    assert "hello" in out


def test_render_inline_code_uses_grey():
    """Inline code is grey (file paths / identifiers), not green. Green is
    reserved for ✓/✔/✅ — actual success markers."""
    out = render("use `foo()` here", stream=_FakeTTY())
    assert "\x1b[90m" in out  # GREY
    assert "\x1b[32m" not in out  # not green
    assert "foo()" in out


def test_render_semantic_glyph_check_is_green():
    out = render("Result: ✓ all good", stream=_FakeTTY())
    assert "\x1b[32m" in out  # GREEN
    assert "✓" in out


def test_render_semantic_glyph_cross_is_red():
    out = render("Result: ✗ failed", stream=_FakeTTY())
    assert "\x1b[31m" in out  # RED
    assert "✗" in out


def test_all_headers_use_cyan_not_yellow():
    """Every header level should land on the same CYAN hue as the logo.
    Yellow (#### / #####) was a 1.3-era mistake."""
    for level in range(1, 7):
        out = render("#" * level + " Heading", stream=_FakeTTY())
        assert "\x1b[36m" in out, f"H{level} missing cyan"
        assert "\x1b[33m" not in out, f"H{level} still has yellow"


def test_render_header_h1_is_plain_claudio_blue():
    """H1 uses plain CLAUDIO_BLUE — no BOLD — to match the prompt and
    breadcrumb shade exactly. BOLD + CYAN drifts to teal on Windows
    Terminal and breaks the brand palette."""
    out = render("# Title", stream=_FakeTTY())
    assert "Title" in out
    assert "\x1b[36m" in out  # CLAUDIO_BLUE
    assert "\x1b[1m" not in out  # NOT bold


def test_render_header_h3():
    out = render("### Subsection", stream=_FakeTTY())
    assert "Subsection" in out
    assert "\x1b[36m" in out  # same blue, no weight change


def test_render_bullet_list():
    out = render("- first\n- second", stream=_FakeTTY())
    assert out.count("•") == 2
    assert "first" in out
    assert "second" in out


def test_render_numbered_list():
    out = render("1. first\n2. second", stream=_FakeTTY())
    assert "1." in out
    assert "2." in out


def test_render_code_fence_has_no_borders():
    """Codex-style: code blocks have no ┌─/└─ borders. The line-number
    gutter is what visually separates code from prose."""
    src = "before\n```python\ndef foo():\n    pass\n```\nafter"
    out = render(src, stream=_FakeTTY())
    assert "┌" not in out
    assert "└" not in out
    # The body lines still come through
    assert "def foo():" in out
    # The fence markers themselves are swallowed -- no `\`\`\`` artifacts
    assert "```" not in out


def test_render_code_fence_emits_line_numbers():
    """Each code row inside a fence gets a right-aligned line number.
    No │ gutter — Codex style uses a clean space-separated layout."""
    src = "```\nfirst\nsecond\nthird\n```"
    out = render(src, stream=_FakeTTY())
    # Line numbers 1, 2, 3 all appear (right-aligned in a 4-wide column)
    assert "   1" in out
    assert "   2" in out
    assert "   3" in out
    # No │ gutter character anymore
    assert "│" not in out


def test_render_code_fence_diff_colours():
    """Lines starting with `-` go red, `+` go green, hunk `@@` go blue."""
    src = "```diff\n-       raise ValueError\n+       raise RuntimeError\n@@ -10,5 +10,6 @@\n```"
    out = render(src, stream=_FakeTTY())
    assert "\x1b[31m" in out  # RED for the removal
    assert "\x1b[32m" in out  # GREEN for the addition
    assert "\x1b[36m" in out  # CLAUDIO_BLUE for the hunk header


def test_render_code_fence_diff_rows_get_background_tint():
    """`-` and `+` rows get a subtle background colour so they read as
    distinct removed/added blocks, not just coloured text."""
    src = "```diff\n-       raise ValueError\n+       raise RuntimeError\n```"
    out = render(src, stream=_FakeTTY())
    # Dark red bg (256-colour 52) on the - row
    assert "\x1b[48;5;52m" in out
    # Dark green bg (256-colour 22) on the + row
    assert "\x1b[48;5;22m" in out


def test_render_code_fence_normal_line_is_grey():
    """Non-diff code rows render in dark grey (file-path / identifier colour)."""
    src = "```\ndef foo(): return 1\n```"
    out = render(src, stream=_FakeTTY())
    assert "\x1b[90m" in out  # GREY


def test_render_code_fence_metadata_lines_not_coloured():
    """`---` and `+++` (diff metadata, not real removals/additions) must
    stay neutral grey -- so they don't read as a giant red/green line."""
    src = "```diff\n--- a/file.py\n+++ b/file.py\n```"
    out = render(src, stream=_FakeTTY())
    # The metadata lines should not carry the diff foreground colours
    # (we still allow grey on them, but never red/green)
    assert "\x1b[31m--- a/file.py" not in out
    assert "\x1b[32m+++ b/file.py" not in out
    assert "--- a/file.py" in out
    assert "+++ b/file.py" in out


# ---- 1.3.1: gated diff coloring + hunk-aware line numbers -----------

def test_plain_fence_does_not_color_dashed_lines():
    """A bash/python fence with `-v` flags or hyphenated text must NOT
    paint them red. Diff coloring is gated on lang or @@ presence."""
    src = "```bash\nclaudio ask -q hello\nclaudio --verbose\n```"
    out = render(src, stream=_FakeTTY())
    # No red foreground / red background anywhere
    assert "\x1b[31m" not in out
    assert "\x1b[48;5;52m" not in out
    # All rows should be grey
    assert out.count("\x1b[90m") >= 2


def test_diff_lang_opens_diff_mode():
    """Opening as ```diff enables +/- coloring even without a @@ header."""
    src = "```diff\n-removed\n+added\n```"
    out = render(src, stream=_FakeTTY())
    assert "\x1b[31m" in out  # red FG
    assert "\x1b[32m" in out  # green FG
    assert "\x1b[48;5;52m" in out  # red BG
    assert "\x1b[48;5;22m" in out  # green BG


def test_hunk_header_in_plain_fence_upgrades_to_diff():
    """A fence that opens without a lang but contains @@ promotes to
    diff mode mid-fence so subsequent +/- rows get coloured."""
    src = "```\n@@ -1,3 +1,3 @@\n-old\n+new\n```"
    out = render(src, stream=_FakeTTY())
    assert "\x1b[31m" in out
    assert "\x1b[32m" in out


def test_hunk_header_drives_real_line_numbers():
    """After `@@ -1044,3 +1044,4 @@` the next - row labels as 1044, the
    next + row labels as 1044, the next context row as 1045 etc."""
    src = (
        "```diff\n"
        "@@ -1044,3 +1044,4 @@\n"
        "-removed line\n"
        "+added line\n"
        " context line\n"
        "```"
    )
    out = render(src, stream=_FakeTTY())
    # Real file line numbers should appear (right-aligned in 4 cols)
    assert "1044" in out
    # The context line shows the new-file number (which advanced to 1045)
    assert "1045" in out


def test_horizontal_rule_uses_terminal_width(monkeypatch):
    """`---` horizontal rule spans the response block, not a fixed 40."""
    monkeypatch.setattr(
        "claudio.utils.markdown._term_width", lambda default=80: 60
    )
    out = render("---", stream=_FakeTTY())
    # Rule width = term_width - 4 (2 indent + small margin)
    rule = "─" * 56
    assert rule in out


def test_empty_fence_emits_no_orphan_rows():
    """An immediately-closed fence (no body lines) should produce no
    visible code rows -- not an orphan blank line with no number."""
    src = "before\n```\n```\nafter"
    out = render(src, stream=_FakeTTY())
    # No line-number gutters should appear
    assert "   1" not in out
    # Both surrounding text lines are preserved
    assert "before" in out
    assert "after" in out


def test_render_blockquote():
    out = render("> a quote", stream=_FakeTTY())
    assert "a quote" in out
    assert "│" in out


def test_render_link():
    out = render("see [docs](https://example.com)", stream=_FakeTTY())
    assert "docs" in out
    assert "example.com" in out


def test_render_no_color_returns_plain():
    """When colors are disabled (no TTY), markdown is returned unchanged."""
    out = render("**bold**", stream=io.StringIO())  # not a TTY
    assert out == "**bold**"


def test_render_horizontal_rule():
    out = render("---", stream=_FakeTTY())
    assert "─" in out  # the unicode hr glyph


# ---- MarkdownStream streaming ----------------------------------------

def test_stream_emits_complete_lines_only():
    sink = _FakeTTY()
    ms = MarkdownStream(out_stream=sink)
    # Partial line: nothing emitted yet
    ms.feed("**bo")
    assert sink.getvalue() == ""
    # Complete line: now we should see it
    ms.feed("ld**\n")
    out = sink.getvalue()
    assert "bold" in out
    assert "\x1b[1m" in out


def test_stream_handles_styling_split_across_chunks():
    sink = _FakeTTY()
    ms = MarkdownStream(out_stream=sink)
    # Bold marker split across feeds
    for chunk in ["**", "hello", " world", "**\n"]:
        ms.feed(chunk)
    out = sink.getvalue()
    assert "hello world" in out
    assert "\x1b[1m" in out


def test_stream_close_flushes_partial_line():
    sink = _FakeTTY()
    ms = MarkdownStream(out_stream=sink)
    ms.feed("**trailing**")  # no newline
    assert sink.getvalue() == ""
    ms.close()
    out = sink.getvalue()
    assert "trailing" in out


def test_stream_code_fence_state_carries_across_chunks():
    sink = _FakeTTY()
    ms = MarkdownStream(out_stream=sink)
    ms.feed("```\n")
    ms.feed("inside the fence\n")
    ms.feed("```\n")
    ms.feed("**outside bold**\n")
    out = sink.getvalue()
    # Inside-fence line was dim-wrapped (no bold styling)
    assert "inside the fence" in out
    # Outside-fence content has bold treatment
    assert "outside bold" in out
    assert "\x1b[1m" in out


def test_stream_disabled_when_not_tty():
    sink = io.StringIO()  # plain StringIO -> isatty() False
    ms = MarkdownStream(out_stream=sink)
    ms.feed("**hello**\n")
    # No styling AND no left-pad: piped output stays flush left
    assert sink.getvalue() == "**hello**\n"


def test_stream_indents_each_line_when_styled():
    """On a TTY, every emitted line is prefixed with `  ` (2-space indent)
    so the response visually stands apart from the user's prompt."""
    sink = _FakeTTY()
    ms = MarkdownStream(out_stream=sink)
    ms.feed("alpha\nbeta\n")
    out = sink.getvalue()
    assert out.startswith("  ")
    # Second line also indented
    assert "\n  " in out


def test_stream_indent_suppressed_when_not_tty():
    """Plain stdout stays flush-left so machine parsers don't choke on
    leading whitespace."""
    sink = io.StringIO()
    ms = MarkdownStream(out_stream=sink)
    ms.feed("alpha\nbeta\n")
    assert not sink.getvalue().startswith("  ")


# ---- wrap-aware indenting --------------------------------------------

def test_visible_len_strips_ansi():
    assert _visible_len("\x1b[36mhello\x1b[0m") == 5
    assert _visible_len("plain") == 5
    assert _visible_len("\x1b[1m\x1b[36mbold cyan\x1b[0m") == 9


def test_wrap_short_line_returns_one_row():
    rows = _wrap_with_indent("hello world", width=80, indent="  ")
    assert rows == ["  hello world"]


def test_wrap_long_line_splits_on_word_boundary():
    """Long line wraps on spaces; every visual row carries the indent."""
    long = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    rows = _wrap_with_indent(long, width=20, indent="  ")
    # Every row must start with the indent
    assert all(row.startswith("  ") for row in rows)
    # And every row must fit
    assert all(_visible_len(row) <= 20 for row in rows)
    # And the original words are all preserved
    assert " ".join(row.lstrip() for row in rows) == long


def test_wrap_preserves_ansi_styling():
    """ANSI codes don't count toward width and stay attached to words."""
    styled = (
        "\x1b[36mword1\x1b[0m word2 word3 word4 \x1b[1mword5\x1b[0m word6 word7"
    )
    rows = _wrap_with_indent(styled, width=30, indent="  ")
    # Each visible row fits even though raw bytes are longer
    for row in rows:
        assert _visible_len(row) <= 30


def test_stream_hard_wraps_long_lines_with_indent(monkeypatch):
    """A line longer than the terminal must be split, with every visual
    row carrying the 2-space gutter — not just the first."""
    monkeypatch.setattr(
        "claudio.utils.markdown._term_width", lambda default=80: 24
    )
    sink = _FakeTTY()
    ms = MarkdownStream(out_stream=sink)
    ms.feed("alpha beta gamma delta epsilon zeta eta theta iota\n")
    out = sink.getvalue()
    # Multiple lines emitted
    lines = out.rstrip("\n").split("\n")
    assert len(lines) >= 2
    # EVERY line indented, including continuations
    assert all(line.startswith("  ") for line in lines)


def test_stream_char_wraps_inside_code_fence(monkeypatch):
    """Long fenced lines char-wrap with continuation rows aligned under
    the code position (Codex-style). The gutter prints only on the first
    visual row; continuations get blank spaces of the same width."""
    monkeypatch.setattr(
        "claudio.utils.markdown._term_width", lambda default=80: 30
    )
    sink = _FakeTTY()
    ms = MarkdownStream(out_stream=sink)
    long_code = "x = some_very_long_function_name(a, b, c)"
    ms.feed(f"```\n{long_code}\n```\n")
    out = sink.getvalue()
    # The full source text is present even after wrapping (when ANSI
    # is stripped). Char-wrap may split mid-token, so check pieces.
    import re as _re
    plain = _re.sub(r"\x1b\[[0-9;]*m", "", out)
    plain = plain.replace("\n", "")  # rejoin wrapped pieces
    assert "some_very_long_function_name" in plain or long_code[:20] in plain
    # Multi-row output (line + at least one continuation)
    assert out.count("\n") >= 2


def test_stream_multiple_lines_in_one_chunk():
    sink = _FakeTTY()
    ms = MarkdownStream(out_stream=sink)
    ms.feed("# Header\n- item one\n- item two\n")
    out = sink.getvalue()
    assert "Header" in out
    assert out.count("•") == 2


# ---- regression: tricky inline cases ---------------------------------

def test_italic_does_not_eat_bold():
    """`**foo**` must not be parsed as italic-italic."""
    out = render("**not italic**", stream=_FakeTTY())
    # Should be bold, not italic-italic
    assert "\x1b[1m" in out
    # And italic shouldn't show up around a bare word inside
    assert out.count("\x1b[3m") == 0


def test_underscore_inside_word_not_italic():
    """`foo_bar_baz` must stay plain (Python identifiers etc.)."""
    out = render("call foo_bar_baz now", stream=_FakeTTY())
    # No italic styling applied
    assert "\x1b[3m" not in out
    assert "foo_bar_baz" in out
