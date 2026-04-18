"""Lightweight stderr spinner for long-running subprocess calls."""

from __future__ import annotations

import itertools
import sys
import threading
import time


class Spinner:
    """Spinner that writes to stderr while a blocking call is in flight.

    Silent when stderr is not a TTY (piped/redirected), so captured output
    stays clean. Writes a single carriage-return line and clears it on stop.
    """

    def __init__(self, message: str = "thinking", stream=None):
        self.message = message
        self.stream = stream if stream is not None else sys.stderr
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "Spinner":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        if not getattr(self.stream, "isatty", lambda: False)():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        frames = itertools.cycle("|/-\\")
        start = time.monotonic()
        last_len = 0
        while not self._stop.is_set():
            elapsed = time.monotonic() - start
            line = f"\r{next(frames)} {self.message} ({elapsed:4.1f}s)"
            try:
                self.stream.write(line)
                self.stream.flush()
            except (OSError, ValueError):
                return
            last_len = len(line)
            if self._stop.wait(0.1):
                break
        try:
            self.stream.write("\r" + " " * max(last_len, 1) + "\r")
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
