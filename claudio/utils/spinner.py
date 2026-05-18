"""Lightweight stderr spinner for long-running subprocess calls."""

from __future__ import annotations

import itertools
import sys
import threading
import time

from claudio.utils.colors import CYAN, DIM, RESET, colors_enabled


# Braille frames look smoother than the rotating slash and stay aligned
# in monospaced terminals. Two-character ASCII fallback for legacy consoles.
_FRAMES_UNICODE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FRAMES_ASCII = "|/-\\"


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

    def update(self, message: str) -> None:
        """Swap the displayed message mid-spin (e.g. on retry)."""
        self.message = message

    def _pick_frames(self) -> str:
        # cp1252 (legacy Windows consoles) cannot render braille — sniff
        # the stream encoding and fall back to ASCII frames if needed.
        encoding = getattr(self.stream, "encoding", None) or ""
        if encoding.lower().startswith(("utf", "cp65001")):
            return _FRAMES_UNICODE
        return _FRAMES_ASCII

    def _run(self) -> None:
        frames = itertools.cycle(self._pick_frames())
        start = time.monotonic()
        last_len = 0
        use_color = colors_enabled(self.stream)
        while not self._stop.is_set():
            elapsed = time.monotonic() - start
            frame = next(frames)
            if use_color:
                line = (
                    f"\r{CYAN}{frame}{RESET} {self.message} "
                    f"{DIM}({elapsed:4.1f}s){RESET}"
                )
                # length excluding ANSI for the clear pass
                visible_len = 1 + 1 + len(self.message) + 1 + len(f"({elapsed:4.1f}s)") + 1
            else:
                line = f"\r{frame} {self.message} ({elapsed:4.1f}s)"
                visible_len = len(line) - 1  # minus the leading \r
            try:
                self.stream.write(line)
                self.stream.flush()
            except (OSError, ValueError):
                return
            last_len = max(last_len, visible_len)
            if self._stop.wait(0.1):
                break
        try:
            # Plain clear sequence (no ANSI) so callers parsing the stream
            # see a clean blank — and so test_spinner_clears_line_on_exit
            # keeps matching r"\r +\r".
            self.stream.write("\r" + " " * max(last_len, 1) + "\r")
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
