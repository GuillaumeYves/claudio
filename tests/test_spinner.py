"""Tests for the stderr spinner used during Claude CLI calls."""

from __future__ import annotations

import time

from claudio.utils.spinner import Spinner


class FakeStream:
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


def test_spinner_silent_when_stream_not_tty():
    stream = FakeStream(tty=False)
    with Spinner("work", stream=stream):
        time.sleep(0.25)
    assert stream.writes == []


def test_spinner_animates_when_stream_is_tty():
    stream = FakeStream(tty=True)
    with Spinner("work", stream=stream):
        time.sleep(0.35)
    # At 100ms tick we expect multiple frames + the final clear
    assert len(stream.writes) >= 3
    assert any("work" in w for w in stream.writes)


def test_spinner_clears_line_on_exit():
    stream = FakeStream(tty=True)
    with Spinner("x", stream=stream):
        time.sleep(0.15)
    # Last write should be the clear sequence (\r + spaces + \r)
    last = stream.writes[-1]
    assert last.startswith("\r"), f"expected clear sequence, got {last!r}"
    assert last.endswith("\r")
    assert last.strip("\r ").strip() == ""


def test_spinner_stop_is_idempotent():
    stream = FakeStream(tty=True)
    spinner = Spinner("x", stream=stream)
    spinner.start()
    time.sleep(0.12)
    spinner.stop()
    spinner.stop()  # must not raise


def test_spinner_without_start_does_not_crash():
    # If the context manager path wasn't used and start() never ran,
    # stop() must still be a no-op.
    Spinner("x").stop()


def test_spinner_survives_stream_errors():
    class BrokenStream(FakeStream):
        def write(self, s: str) -> int:
            raise OSError("stream closed")

    stream = BrokenStream(tty=True)
    # Must not raise -- spinner runs in a daemon thread and catches OSError.
    with Spinner("x", stream=stream):
        time.sleep(0.15)
