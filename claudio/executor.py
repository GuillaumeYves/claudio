"""Claude CLI executor — sends optimized prompts to Claude.

Two output paths:

  - **Streaming** (default for human-readable output): subprocess.Popen with
    `--output-format stream-json --verbose`. We parse JSONL events as they
    arrive. Text deltas are fed through `MarkdownStream` so headers, bold,
    code spans, etc. render with ANSI styling on a TTY in real time. Tool
    events (Read / Edit / Grep / Bash …) are split off from the text
    stream and surface either as a live spinner-label update (`claudio is
    reading auth.py`) before the response starts, or as a dim stderr
    breadcrumb (`↳ claudio is reading auth.py`) once text is in flight —
    so they never pollute the response on stdout.

  - **Buffered**: subprocess.run with `--output-format json` (or no format).
    Used when `--json` is requested (machine-parsed output), when streaming
    is disabled via config/env, or as a fallback if the user's claude CLI
    doesn't emit stream-json events.

Retry policy: the buffered path retries transient failures (5xx, ECONNRESET,
timeouts) with exponential backoff. The streaming path only retries when no
text has been emitted yet — once a delta hits the terminal we can't unprint
it, so a retry would duplicate output.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from claudio.config import load_config
from claudio.utils.colors import (
    BOLD, CLAUDIO_BLUE, DIM, GREEN, RED, RESET, colors_enabled,
)
from claudio.utils.markdown import MarkdownStream
from claudio.utils.spinner import Spinner


def _print_error(message: str) -> None:
    """Write an [claudio:error] line to stderr with the full message in red.

    Matches the styling used by `claudio.utils.output.Output.error()`:
    bold-red label, red body, so the whole line reads as one error block
    instead of a coloured prefix followed by a default-colour message that
    blends into surrounding output.
    """
    if colors_enabled(sys.stderr):
        prefix = f"{BOLD}{RED}[claudio:error]{RESET}"
        body = f"{RED}{message}{RESET}"
        line = f"{prefix} {body}"
    else:
        line = f"[claudio:error] {message}"
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except (OSError, UnicodeEncodeError):
        pass


# stderr / exit signatures we treat as transient and worth retrying.
_TRANSIENT_PATTERNS = re.compile(
    r"("
    r"ECONN(?:RESET|REFUSED|ABORTED)|"
    r"ETIMEDOUT|EPIPE|EAI_AGAIN|"
    r"socket hang up|"
    r"network|fetch failed|getaddrinfo|"
    r"\b5\d{2}\b|"               # any 5xx
    r"overloaded|"
    r"rate.?limit|"
    r"too many requests|"
    r"temporarily unavailable|"
    r"timeout"
    r")",
    re.IGNORECASE,
)

# Defaults — overridable via config.json or env.
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 2.0  # 2s, 4s, 8s


def find_claude_cli() -> str | None:
    """Find the Claude CLI binary."""
    config = load_config()
    configured = config.get("claude_binary", "claude")

    path = shutil.which(configured)
    if path:
        return path
    for name in ("claude", "claude.exe"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _retry_settings() -> tuple[int, float]:
    """Resolve (max_retries, backoff_base) from env > config > defaults."""
    cfg = load_config()
    env_retries = os.environ.get("CLAUDIO_MAX_RETRIES")
    env_backoff = os.environ.get("CLAUDIO_BACKOFF_BASE")
    try:
        max_retries = int(env_retries) if env_retries else int(cfg.get("max_retries", _DEFAULT_MAX_RETRIES))
    except ValueError:
        max_retries = _DEFAULT_MAX_RETRIES
    try:
        backoff = float(env_backoff) if env_backoff else float(cfg.get("backoff_base", _DEFAULT_BACKOFF_BASE))
    except ValueError:
        backoff = _DEFAULT_BACKOFF_BASE
    return max(0, max_retries), max(0.1, backoff)


def _streaming_enabled() -> bool:
    """Streaming is on by default; CLAUDIO_NO_STREAM=1 or config disables."""
    if os.environ.get("CLAUDIO_NO_STREAM"):
        return False
    cfg = load_config()
    return bool(cfg.get("streaming", True))


def _cache_friendly_enabled() -> bool:
    """Pass --exclude-dynamic-system-prompt-sections by default.

    This moves per-machine sections (cwd, env info, git status, memory paths)
    out of Claude's *system* prompt and into the first *user* message. The
    system prompt then becomes byte-identical across users and across calls
    on the same machine over time, dramatically improving Anthropic's
    automatic prompt-cache reuse — and a cache hit is ~90% cheaper on the
    cached input tokens.

    Disable with CLAUDIO_NO_CACHE_FRIENDLY=1 or `cache_friendly: false` in
    config.json.
    """
    if os.environ.get("CLAUDIO_NO_CACHE_FRIENDLY"):
        return False
    cfg = load_config()
    return bool(cfg.get("cache_friendly", True))


def _is_transient(stderr: str, returncode: int) -> bool:
    """Decide whether a non-zero exit looks worth retrying."""
    if returncode in (124, 137, 143):
        return True
    if not stderr:
        return True
    return bool(_TRANSIENT_PATTERNS.search(stderr))


def execute_prompt(
    prompt: str,
    json_output: bool = False,
    model: str | None = None,
    session_id: str | None = None,
    resume: str | None = None,
    allowed_tools: list[str] | None = None,
    permission_mode: str | None = None,
) -> tuple[str, bool]:
    """Send a prompt to Claude CLI.

    Args:
        permission_mode: Maps to the CLI's `--permission-mode`. Headless
            `claude --print` denies file-mutating tools by default (there's
            no human to approve them), so a build that should actually edit
            files must pass e.g. `acceptEdits` — otherwise Claude emits the
            Edit/Write tool_use events but the writes are auto-denied and
            nothing reaches disk.

    Returns:
        (response_text, was_streamed) — `was_streamed` tells the caller
        whether the response has already been written to stdout (streaming
        path) or still needs to be printed (buffered path).
    """
    claude_bin = find_claude_cli()
    if not claude_bin:
        _print_error(
            "Claude CLI not found. Install it from https://claude.ai/code\n"
            "                 In the meantime, use --dry-run to see the optimized prompt."
        )
        sys.exit(1)

    config = load_config()
    timeout = config.get("timeout", 300)
    max_retries, backoff_base = _retry_settings()
    use_stream = _streaming_enabled() and not json_output

    cmd = [claude_bin, "--print"]
    if _cache_friendly_enabled():
        cmd.append("--exclude-dynamic-system-prompt-sections")
    if json_output:
        cmd.extend(["--output-format", "json"])
    elif use_stream:
        # `--verbose` is the documented partner of `stream-json` — it tells
        # the CLI to emit per-delta events instead of one big result event.
        cmd.extend(["--output-format", "stream-json", "--verbose"])
    if model:
        cmd.extend(["--model", model])
    # Pass --fallback-model if configured. The CLI uses this to retry once
    # against a cheaper/different model when the primary is overloaded —
    # complementary to our own retry loop (which handles network blips).
    fallback = config.get("fallback_model")
    if fallback and not os.environ.get("CLAUDIO_NO_FALLBACK_MODEL"):
        cmd.extend(["--fallback-model", fallback])
    if resume:
        cmd.extend(["--resume", resume])
    elif session_id:
        cmd.extend(["--session-id", session_id])
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    if permission_mode:
        cmd.extend(["--permission-mode", permission_mode])

    spinner_label = f"asking {model}" if model else "asking claude"

    if use_stream:
        return _execute_streaming(cmd, prompt, timeout, spinner_label, claude_bin)
    return _execute_buffered(cmd, prompt, timeout, spinner_label, claude_bin,
                              max_retries, backoff_base)


def _execute_buffered(
    cmd: list[str],
    prompt: str,
    timeout: int,
    spinner_label: str,
    claude_bin: str,
    max_retries: int,
    backoff_base: float,
) -> tuple[str, bool]:
    """Original retry-with-backoff path. Returns (text, was_streamed=False)."""
    last_error: str | None = None
    with Spinner(spinner_label) as spin:
        for attempt in range(max_retries + 1):
            try:
                result = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                last_error = f"timed out after {timeout}s"
                if attempt < max_retries:
                    delay = backoff_base * (2 ** attempt)
                    spin.update(f"{spinner_label} (timed out — retry {attempt + 1}/{max_retries} in {delay:.0f}s)")
                    time.sleep(delay)
                    continue
                _print_error(f"Claude CLI {last_error} after {max_retries + 1} attempts.")
                sys.exit(1)
            except FileNotFoundError:
                _print_error(f"Could not execute: {claude_bin}")
                sys.exit(1)

            if result.returncode == 0:
                return result.stdout.strip(), False

            stderr = (result.stderr or "").strip()
            last_error = stderr or f"exit {result.returncode}"

            if attempt < max_retries and _is_transient(stderr, result.returncode):
                delay = backoff_base * (2 ** attempt)
                short = stderr.splitlines()[-1] if stderr else f"exit {result.returncode}"
                spin.update(
                    f"{spinner_label} (connection blip — retry "
                    f"{attempt + 1}/{max_retries} in {delay:.0f}s)"
                )
                print(
                    f"[claudio:warn] transient failure ({short[:140]}); "
                    f"retrying in {delay:.0f}s ({attempt + 1}/{max_retries})",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue

            if stderr:
                _print_error(f"Claude CLI error: {stderr}")
            else:
                _print_error(f"Claude CLI exited with code {result.returncode}")
            sys.exit(result.returncode or 1)

    _print_error(f"Claude CLI failed after retries: {last_error}")
    sys.exit(1)


def _execute_streaming(
    cmd: list[str],
    prompt: str,
    timeout: int,
    spinner_label: str,
    claude_bin: str,
) -> tuple[str, bool]:
    """Stream JSONL events from claude --output-format stream-json.

    Writes text deltas live to stdout, returns the full aggregated text so
    callers can cache it or log token usage.

    Retries only when no delta has been printed yet — once tokens hit the
    terminal we can't unprint them, so partial-failure cases bubble up as
    errors rather than risk duplicating output on a retry.
    """
    max_retries, backoff_base = _retry_settings()
    last_error: str | None = None

    with Spinner(spinner_label) as spin:
        for attempt in range(max_retries + 1):
            try:
                rc, full_text, stderr, saw_delta = _stream_once(
                    cmd, prompt, timeout, spin, spinner_label
                )
            except FileNotFoundError:
                _print_error(f"Could not execute: {claude_bin}")
                sys.exit(1)
            except subprocess.TimeoutExpired:
                last_error = f"timed out after {timeout}s"
                if attempt < max_retries:
                    delay = backoff_base * (2 ** attempt)
                    spin.update(f"{spinner_label} (timed out — retry {attempt + 1}/{max_retries} in {delay:.0f}s)")
                    time.sleep(delay)
                    continue
                _print_error(f"Claude CLI {last_error} after {max_retries + 1} attempts.")
                sys.exit(1)

            if rc == 0:
                return full_text.strip(), True

            last_error = (stderr or "").strip() or f"exit {rc}"

            # Retry only when we haven't already printed anything.
            if not saw_delta and attempt < max_retries and _is_transient(stderr, rc):
                delay = backoff_base * (2 ** attempt)
                short = stderr.splitlines()[-1] if stderr else f"exit {rc}"
                spin.update(
                    f"{spinner_label} (connection blip — retry "
                    f"{attempt + 1}/{max_retries} in {delay:.0f}s)"
                )
                print(
                    f"[claudio:warn] transient failure ({short[:140]}); "
                    f"retrying in {delay:.0f}s ({attempt + 1}/{max_retries})",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue

            if stderr:
                print(f"\n[claudio:error] Claude CLI error: {stderr}", file=sys.stderr)
            else:
                print(f"\n[claudio:error] Claude CLI exited with code {rc}", file=sys.stderr)
            sys.exit(rc or 1)

    _print_error(f"Claude CLI failed after retries: {last_error}")
    sys.exit(1)


def _stream_once(
    cmd: list[str],
    prompt: str,
    timeout: int,
    spinner: Spinner,
    spinner_label: str,
) -> tuple[int, str, str, bool]:
    """One pass at streaming. Returns (returncode, full_text, stderr, saw_delta).

    Text events stream through MarkdownStream (renders bold/headers/etc as
    ANSI in a TTY, plain otherwise). Tool events surface as:
      - a spinner label update if no text has streamed yet
      - a dim stderr breadcrumb if text is already mid-flight
    Neither pollutes the stdout response.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,  # line-buffered so stdout iter yields per-event
    )
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass

    full_text: list[str] = []
    raw_buffer: list[str] = []
    saw_delta = False
    saw_tool = False  # any tool_use event during this turn -> emit the handoff
    md_stream = MarkdownStream(out_stream=sys.stdout)
    # Post-text tool tracking: the currently-animating breadcrumb (if any)
    # plus a flag set whenever a tool fires after text has started, so we
    # know to print the handoff line before text resumes.
    active_breadcrumb: _BreadcrumbAnimator | None = None
    post_text_tool_seen = False

    started = time.monotonic()
    if proc.stdout is None:
        # Defensive: subprocess.Popen with stdout=PIPE should always give
        # us a readable handle, but `assert` is stripped under `python -O`
        # so we use an explicit guard. Treat as a hard failure -- there's
        # nothing meaningful to stream from a closed/missing pipe.
        return 1, "", "stdout pipe is None", False
    try:
        for raw_line in proc.stdout:
            if timeout and (time.monotonic() - started) > timeout:
                proc.terminate()
                raise subprocess.TimeoutExpired(cmd, timeout)
            line = raw_line.rstrip("\n")
            if not line:
                continue
            kind, payload = _parse_stream_event(line)

            if kind == "text" and payload:
                if not saw_delta:
                    spinner.stop()
                    # Pre-text handoff: any tools that fired before the
                    # first delta land the "✓ claudio has enough context"
                    # line here, on transition from gather → answer.
                    if saw_tool:
                        _emit_context_handoff()
                    saw_delta = True
                else:
                    # Mid-response handoff: if a tool ran after text had
                    # already started (Claude paused to grab more context),
                    # finalize the active breadcrumb and emit the same
                    # marker before the answer continues.
                    if active_breadcrumb is not None:
                        active_breadcrumb.stop()
                        active_breadcrumb = None
                    if post_text_tool_seen:
                        _emit_context_handoff()
                        post_text_tool_seen = False
                full_text.append(payload)
                md_stream.feed(payload)
                continue

            if kind == "tool" and payload:
                saw_tool = True
                if not saw_delta:
                    # Pre-text: reuse the main spinner with the three-dot
                    # animation so "thinking" reads clearly.
                    spinner.use_dots()
                    spinner.update(f"claudio is {payload}")
                else:
                    # Post-text: each tool gets its own animated
                    # breadcrumb. The previous one (if any) finalizes
                    # into a static line so the activity log builds up.
                    post_text_tool_seen = True
                    if active_breadcrumb is not None:
                        active_breadcrumb.stop()
                    active_breadcrumb = _BreadcrumbAnimator(payload)
                    active_breadcrumb.start()
                continue

            if not kind:
                # Non-text/non-tool event (system, message_start, ...) or
                # plain text from a CLI that didn't honour stream-json.
                # Save in case we need the fallback.
                raw_buffer.append(line)

        rc = proc.wait(timeout=10)
    except KeyboardInterrupt:
        if active_breadcrumb is not None:
            active_breadcrumb.stop()
            active_breadcrumb = None
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise

    # Stream ended (normal or error): make sure any in-flight breadcrumb
    # gets committed so its line isn't left half-painted.
    if active_breadcrumb is not None:
        active_breadcrumb.stop()
        active_breadcrumb = None

    md_stream.close()

    stderr = ""
    try:
        if proc.stderr is not None:
            stderr = proc.stderr.read() or ""
    except OSError:
        pass

    text = "".join(full_text)

    # Fallback: stream-json wasn't honoured and we have plain text in the
    # raw buffer. Print and use it as the response so the user still gets
    # the answer.
    if not saw_delta and rc == 0 and raw_buffer:
        joined = "\n".join(raw_buffer).strip()
        if joined:
            spinner.stop()
            try:
                sys.stdout.write(joined + "\n")
                sys.stdout.flush()
            except (UnicodeEncodeError, OSError):
                pass
            text = joined

    if saw_delta:
        try:
            sys.stdout.write("\n")
            sys.stdout.flush()
        except OSError:
            pass

    return rc, text, stderr, saw_delta


class _BreadcrumbAnimator:
    """Animated tool-activity breadcrumb on stderr (post-text path).

    While a tool runs, this class repaints `↳ claudio is <label> .  /
    ..  / ...  ` on a single stderr line using carriage return, so the
    user can see something is alive between tool announcements. When
    `stop()` is called (next tool starts, or text resumes), the dots are
    cleared and the bare label is committed with a newline -- so the
    breadcrumb persists as part of the activity log instead of vanishing.

    On non-TTY stderr (piped, captured) the animation is skipped and the
    static breadcrumb is written once. Same end result, no thread cost.

    The label is truncated to fit the terminal width so long paths don't
    overflow into wrapped rows that mangle the dot animation.
    """

    _FRAMES = (".  ", ".. ", "...")
    _TICK_SECONDS = 0.25
    _PREFIX_OVERHEAD = len("  ↳ claudio is ") + len(" ...")  # gutter + dots

    def __init__(self, label: str, stream=None):
        self.stream = stream if stream is not None else sys.stderr
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._use_color = colors_enabled(self.stream)
        self._is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.label = _truncate_breadcrumb_label(label, self.stream)

    def start(self) -> None:
        if not self._is_tty:
            self._commit_static()
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Commit the breadcrumb as a permanent line and end the animation."""
        if not self._is_tty:
            return  # static line already written by start()
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=0.6)
        self._thread = None

    def _styled(self, text: str) -> str:
        # Breadcrumbs share the brand blue with the prompt + logo so the
        # whole tool reads as one colour family. The arrow stays bold-ish
        # via the blue itself rather than DIM grey (which read as "noise"
        # instead of "claudio is doing something").
        return f"{CLAUDIO_BLUE}{text}{RESET}" if self._use_color else text

    def _commit_static(self) -> None:
        """Non-TTY (or fallback) path: write the bare breadcrumb once + \\n."""
        try:
            self.stream.write(self._styled(f"  ↳ claudio is {self.label}") + "\n")
            self.stream.flush()
        except (OSError, UnicodeEncodeError):
            pass

    def _run(self) -> None:
        i = 0
        max_len = 0
        while not self._stop.is_set():
            frame = self._FRAMES[i % len(self._FRAMES)]
            i += 1
            text = f"  ↳ claudio is {self.label} {frame}"
            max_len = max(max_len, len(text))
            try:
                self.stream.write("\r" + self._styled(text))
                self.stream.flush()
            except (OSError, ValueError):
                return
            if self._stop.wait(self._TICK_SECONDS):
                break
        # Commit: clear the animated frame, write the bare label, newline.
        final_text = f"  ↳ claudio is {self.label}"
        pad = " " * max(0, max_len - len(final_text))
        try:
            self.stream.write("\r" + self._styled(final_text) + pad + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            pass


def _truncate_breadcrumb_label(label: str, stream) -> str:
    """Shorten an overlong breadcrumb label so it fits the terminal.

    Long paths like `C:\\Users\\Foo\\Documents\\Perso\\claudio` would
    otherwise wrap into a second visual row, breaking the carriage-return
    based dot animation. We trim with a `…` prefix preserving the *tail*
    (the part the user usually recognises -- the file/dir name) rather
    than the head.
    """
    try:
        width = shutil.get_terminal_size((80, 24)).columns
    except (OSError, ValueError):
        width = 80
    # Leave room for the `↳ claudio is ` prefix and trailing `...` frame.
    budget = max(20, width - _BreadcrumbAnimator._PREFIX_OVERHEAD)
    if len(label) <= budget:
        return label
    # Keep the trailing chars (filename / last path segment).
    return "…" + label[-(budget - 1):]


def _emit_context_handoff() -> None:
    """Print the 'context gathered, here's the answer' transition line.

    Called once per turn, only when at least one tool_use event fired
    before the first text delta. Marks the moment claudio finishes
    gathering context and the actual response begins. Green ✓ on stdout
    (so it's part of the response area, not the stderr noise stream),
    then a blank line for visual breathing room.
    """
    use_color = colors_enabled(sys.stdout)
    if use_color:
        line = f"  {GREEN}✓{RESET} {DIM}claudio has enough context{RESET}\n\n"
    else:
        line = "  ✓ claudio has enough context\n\n"
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except (OSError, UnicodeEncodeError):
        pass


def _parse_stream_event(line: str) -> tuple[str, str]:
    """Parse one stream-json line into (kind, payload).

    Returns:
      ('text', text)   - user-visible response text to render
      ('tool', label)  - tool_use event; `label` is e.g. "reading main.py"
                          for display in the spinner or as a breadcrumb
      ('', '')         - event not relevant to the user (system, thinking,
                          message_start, result, etc.)

    The claude CLI's `--output-format stream-json` emits *complete message
    snapshots* per event. Each `"assistant"` event carries `message.content[]`
    which may contain text + tool_use + thinking blocks. We split them by
    kind so the caller can route text to stdout (with markdown rendering)
    and tool events to the spinner / a stderr breadcrumb.

    Falls back to Anthropic API-style `content_block_delta` events for
    forward-compat with future CLI versions.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return ("", "")
    if not isinstance(obj, dict):
        return ("", "")

    t = obj.get("type", "")

    if t == "assistant":
        message = obj.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return ("", "")
        text_parts: list[str] = []
        tool_label = ""
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "") or ""
                if text:
                    text_parts.append(text)
            elif btype == "tool_use":
                name = block.get("name", "tool")
                inp = block.get("input") or {}
                tool_label = _tool_status_label(name, inp)
            # thinking / image / anything else: silent
        if text_parts:
            return ("text", "".join(text_parts))
        if tool_label:
            return ("tool", tool_label)
        return ("", "")

    # Future-proofing: real Anthropic streaming deltas, if the CLI ever
    # switches modes.
    if t == "content_block_delta":
        delta = obj.get("delta") or {}
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            text = delta.get("text", "") or ""
            return ("text", text) if text else ("", "")

    # Light-weight wrapper some integrations use.
    if t == "text":
        text = obj.get("text", "") or ""
        return ("text", text) if text else ("", "")

    return ("", "")


def _tool_status_label(name: str, inp: dict) -> str:
    """Friendly tool status line: 'reading main.py', 'searching pipeline'."""
    verb = _TOOL_VERBS.get(name, "running")
    hint = _summarise_tool_input(name, inp)
    if hint:
        return f"{verb} {hint}"
    return f"{verb} {name}"


# Map raw tool names to plain-English verbs for the status display.
_TOOL_VERBS = {
    "Read": "reading",
    "Edit": "editing",
    "Write": "writing",
    "NotebookEdit": "editing notebook",
    "Bash": "running shell",
    "PowerShell": "running powershell",
    "Grep": "searching",
    "Glob": "globbing",
    "WebFetch": "fetching",
    "WebSearch": "searching web",
}


def _summarise_tool_input(name: str, inp: dict) -> str:
    """Short, user-readable hint for a tool_use block.

    Keep this tiny — the goal is "what is Claude doing right now" at a
    glance, not full reproduction of the call. We don't want to print
    long paths or whole bash commands.
    """
    if not isinstance(inp, dict):
        return ""
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("path") or ""
        if path:
            return _shorten_path(path)
    if name in ("Bash", "PowerShell"):
        cmd = inp.get("command") or ""
        first = cmd.strip().splitlines()[0] if cmd.strip() else ""
        return (first[:60] + "...") if len(first) > 60 else first
    if name == "Grep":
        return inp.get("pattern", "")
    if name == "Glob":
        return inp.get("pattern", "")
    if name in ("WebFetch", "WebSearch"):
        return inp.get("url") or inp.get("query", "")
    return ""


def _shorten_path(path: str) -> str:
    """Drop the cwd prefix from absolute paths for readability."""
    try:
        cwd = os.getcwd()
        if path.startswith(cwd):
            return path[len(cwd):].lstrip("\\/") or path
    except OSError:
        pass
    return path
