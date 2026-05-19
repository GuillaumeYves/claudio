"""Tests for the post-text tool-activity breadcrumb animator."""

from __future__ import annotations

import os
import time

from claudio.executor import _BreadcrumbAnimator


class FakeStream:
    """Minimal stderr stand-in. `tty` controls whether animation runs."""

    encoding = "utf-8"

    def __init__(self, tty: bool):
        self._tty = tty
        self.writes: list[str] = []

    def write(self, s: str) -> int:
        self.writes.append(s)
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return self._tty


# ---- non-TTY path (piped / captured stderr) ---------------------------

def test_non_tty_writes_static_line_once():
    """Without a TTY, no thread is spawned -- just one static breadcrumb."""
    stream = FakeStream(tty=False)
    anim = _BreadcrumbAnimator("reading auth.py", stream=stream)
    anim.start()
    # No animation spawned; line committed immediately.
    joined = "".join(stream.writes)
    assert "claudio is reading auth.py" in joined
    assert joined.endswith("\n")
    # stop() on a non-TTY animator must be a no-op (no thread to join)
    anim.stop()


# ---- TTY path: dots animate, label survives stop ----------------------

def test_tty_animation_writes_multiple_frames():
    """The animator should repaint several times before being stopped."""
    stream = FakeStream(tty=True)
    anim = _BreadcrumbAnimator("globbing *.md", stream=stream)
    anim.start()
    # Let several ticks elapse (TICK_SECONDS = 0.25 → ~3 frames in 0.7s)
    time.sleep(0.7)
    anim.stop()
    # We expect multiple \r repaints
    repaints = [w for w in stream.writes if w.startswith("\r")]
    assert len(repaints) >= 2


def test_tty_commit_writes_label_without_dots():
    """After stop(), the final committed line has the bare label + \\n,
    no animated dots, so it stays as a permanent activity-log entry."""
    stream = FakeStream(tty=True)
    anim = _BreadcrumbAnimator("reading README.md", stream=stream)
    anim.start()
    time.sleep(0.3)
    anim.stop()
    # Last write must be the commit: starts with \r, includes the label,
    # ends with \n, and does NOT contain trailing dots.
    last = stream.writes[-1]
    assert "claudio is reading README.md" in last
    assert last.endswith("\n")
    # The cycling frames (".  ", ".. ", "...") shouldn't be in the final commit
    final_after_label = last.split("README.md", 1)[1]
    assert "..." not in final_after_label
    assert ".. " not in final_after_label


def test_tty_double_stop_is_safe():
    """Calling stop() twice must not raise (idempotent like Spinner.stop)."""
    stream = FakeStream(tty=True)
    anim = _BreadcrumbAnimator("x", stream=stream)
    anim.start()
    time.sleep(0.15)
    anim.stop()
    anim.stop()  # must not raise


def test_tty_animation_includes_dot_frame():
    """At least one frame written during animation must show animated dots."""
    stream = FakeStream(tty=True)
    anim = _BreadcrumbAnimator("reading X", stream=stream)
    anim.start()
    time.sleep(0.4)
    anim.stop()
    # Skip the final commit (last write); look at intermediate frames
    animated = "".join(stream.writes[:-1])
    assert "." in animated  # at least one dot was painted


def test_breadcrumb_uses_claudio_blue_not_dim():
    """The activity line should match the prompt/logo brand colour
    (\\x1b[36m), not the dim grey that read as 'background noise'."""
    stream = FakeStream(tty=True)
    anim = _BreadcrumbAnimator("reading auth.py", stream=stream)
    anim.start()
    time.sleep(0.3)
    anim.stop()
    joined = "".join(stream.writes)
    # CLAUDIO_BLUE (= CYAN) escape must appear at least once
    assert "\x1b[36m" in joined
    # And the DIM-only styling should not be the only colour
    assert "claudio is reading auth.py" in joined


def test_long_breadcrumb_label_is_truncated(monkeypatch):
    """A path longer than the terminal width is shortened from the head
    with a leading … so the line fits and the dot animation isn't
    corrupted by terminal soft-wrap.

    We patch the executor module's shutil import directly so pytest's
    own terminal-width probe (which calls the real shutil) keeps working.
    """
    from claudio import executor
    fake = os.terminal_size((40, 24))
    monkeypatch.setattr(
        executor.shutil,
        "get_terminal_size",
        lambda fallback=(80, 24): fake,
    )
    long_path = "C:/Users/YvesGUILLAUME(A1)/Documents/Perso/claudio"
    anim = _BreadcrumbAnimator(f"reading {long_path}", stream=FakeStream(tty=True))
    # Truncation happens in __init__ via _truncate_breadcrumb_label.
    assert len(anim.label) <= 40
    # The tail (recognisable filename) is preserved with a leading …
    assert anim.label.startswith("…")
    assert anim.label.endswith("claudio")
