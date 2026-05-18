"""Tests for the markdown -> ANSI renderer."""

from __future__ import annotations

import io

import pytest

from claudio.utils.markdown import MarkdownStream, render


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


def test_render_inline_code():
    out = render("use `foo()` here", stream=_FakeTTY())
    assert "\x1b[32m" in out  # green for inline code
    assert "foo()" in out


def test_render_header_h1():
    out = render("# Title", stream=_FakeTTY())
    assert "Title" in out
    assert "\x1b[1m" in out  # bold


def test_render_header_h3():
    out = render("### Subsection", stream=_FakeTTY())
    assert "Subsection" in out


def test_render_bullet_list():
    out = render("- first\n- second", stream=_FakeTTY())
    assert out.count("•") == 2
    assert "first" in out
    assert "second" in out


def test_render_numbered_list():
    out = render("1. first\n2. second", stream=_FakeTTY())
    assert "1." in out
    assert "2." in out


def test_render_code_fence_dims_content():
    src = "before\n```python\ndef foo():\n    pass\n```\nafter"
    out = render(src, stream=_FakeTTY())
    assert "def foo():" in out
    # Code inside fence is wrapped in DIM
    assert "\x1b[2m" in out


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
    # No styling: pass-through
    assert sink.getvalue() == "**hello**\n"


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
