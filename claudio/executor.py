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

Billed usage: both paths surface a `CallUsage` when the CLI reports one. The
`result` event (stream-json) and the `--output-format json` envelope carry
`usage` + `total_cost_usd` — what Anthropic *actually charged*, including the
system prompt, CLAUDE.md, tool definitions and prompt-cache traffic that a
local token estimate cannot see. claudio prefers these over its own estimate
wherever it reports tokens or dollars.

Retry policy: the buffered path retries transient failures (5xx, ECONNRESET,
timeouts) with exponential backoff. The streaming path only retries when no
text has been emitted yet — once a delta hits the terminal we can't unprint
it, so a retry would duplicate output. Separately, if the installed CLI
rejects one of claudio's *optional* flags, that flag is dropped and the call
is retried once — an older `claude` degrades instead of hard-failing.
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
from typing import NamedTuple

from claudio.config import load_config
from claudio.utils.colors import (
    BOLD,
    CLAUDIO_BLUE,
    DIM,
    GREEN,
    RED,
    RESET,
    colors_enabled,
)
from claudio.utils.markdown import MarkdownStream
from claudio.utils.spinner import Spinner


class CallUsage(NamedTuple):
    """What one `claude` invocation actually cost, as reported by the CLI.

    claudio's own `estimate_tokens` only ever sees the prompt claudio itself
    composed. The real request also carries Claude Code's system prompt, the
    project's CLAUDE.md, every tool definition, and prompt-cache reads/writes
    — routinely tens of thousands of tokens. Estimating around that is how a
    trivial call gets reported as "~9 tokens, $0.000003" when it billed
    23,870 tokens and $0.0172. When the CLI hands us these numbers we use
    them and stop guessing.

    `cost_usd` is the CLI's own `total_cost_usd` (list-price basis).
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    model: str | None

    @property
    def billed_input_tokens(self) -> int:
        """All input tokens charged for, cache reads and writes included."""
        return (self.input_tokens
                + self.cache_read_tokens
                + self.cache_creation_tokens)


class ExecResult(NamedTuple):
    """Return value of `execute_prompt`.

    `streamed` tells the caller whether the text is already on the user's
    screen (streaming path) or still needs printing (buffered path).
    `usage` is None when the CLI reported none — e.g. plain-text buffered
    output — and callers fall back to estimates.
    """

    text: str
    streamed: bool
    usage: CallUsage | None


def _coerce_int(value) -> int:
    """Non-negative int from untrusted JSON, else 0."""
    return value if isinstance(value, int) and value >= 0 else 0


def _parse_result_usage(obj: dict) -> CallUsage | None:
    """Extract billed usage from a `result` event / `--output-format json` body.

    Returns None when the object carries no usage block at all (so a bare
    `{"type": "result", "result": "..."}` stays noise).

    The top-level `usage` block can come back zeroed in some terminal states
    (a budget-exhausted run, for one) while `modelUsage` still holds the real
    per-model figures — so we fall back to `modelUsage` rather than silently
    logging a free call.
    """
    usage = obj.get("usage")
    model_usage = obj.get("modelUsage")
    if not isinstance(usage, dict) and not isinstance(model_usage, dict):
        return None
    usage = usage if isinstance(usage, dict) else {}

    model: str | None = None
    entry: dict = {}
    if isinstance(model_usage, dict) and model_usage:
        # One entry per model that served the call (a --fallback-model hop
        # yields two). Attribute to whichever did the most generating, and
        # prefer its canonical id over the alias claudio asked for.
        name, raw = max(
            model_usage.items(),
            key=lambda kv: _coerce_int((kv[1] or {}).get("outputTokens")
                                       if isinstance(kv[1], dict) else 0),
        )
        entry = raw if isinstance(raw, dict) else {}
        model = entry.get("canonicalModel") or name

    tokens = {
        "input": _coerce_int(usage.get("input_tokens")),
        "output": _coerce_int(usage.get("output_tokens")),
        "cache_read": _coerce_int(usage.get("cache_read_input_tokens")),
        "cache_creation": _coerce_int(usage.get("cache_creation_input_tokens")),
    }
    if not any(tokens.values()) and entry:
        tokens = {
            "input": _coerce_int(entry.get("inputTokens")),
            "output": _coerce_int(entry.get("outputTokens")),
            "cache_read": _coerce_int(entry.get("cacheReadInputTokens")),
            "cache_creation": _coerce_int(entry.get("cacheCreationInputTokens")),
        }

    cost = obj.get("total_cost_usd")
    if not isinstance(cost, (int, float)):
        cost = entry.get("costUSD")
    cost = float(cost) if isinstance(cost, (int, float)) else 0.0

    return CallUsage(
        input_tokens=tokens["input"],
        output_tokens=tokens["output"],
        cache_read_tokens=tokens["cache_read"],
        cache_creation_tokens=tokens["cache_creation"],
        cost_usd=cost,
        model=model,
    )


def _usage_from_json_text(text: str) -> CallUsage | None:
    """Best-effort usage from an `--output-format json` response body.

    Returns None for plain-text output or anything unparseable — reporting
    no usage is correct there, and callers estimate instead.
    """
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    return _parse_result_usage(obj)


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
# Cap on Claude's agentic tool-use loop. Without it, an ambiguous prompt sends
# Claude Read/Grep/Glob-ing around the repo turn after turn until the wall-clock
# timeout — the "searching… thinking…" runaway. 12 turns is enough to gather
# context for a focused claudio task without wandering. 0 disables the cap.
_DEFAULT_MAX_TURNS = 12

# Effort levels `claude --effort` accepts. Effort is a cost/quality lever that
# is orthogonal to model tier: dropping effort on the same model is often a
# better trade than dropping to a weaker model.
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Optional flags claudio adds for quality-of-life. An older `claude` that
# doesn't know one of these would hard-fail the whole call, so on an
# "unknown option" error we drop the offending flag and retry once.
_OPTIONAL_FLAGS = frozenset({
    "--include-partial-messages",
    "--exclude-dynamic-system-prompt-sections",
    "--effort",
    "--max-budget-usd",
    "--fallback-model",
})
# Of those, the ones whose *next* argv is a value that must be dropped too.
_VALUE_TAKING_FLAGS = frozenset({"--effort", "--max-budget-usd", "--fallback-model"})
_UNKNOWN_OPTION_RE = re.compile(
    r"unknown (?:option|argument|flag)s?[:\s'\"]*(--[a-z0-9][a-z0-9-]*)", re.IGNORECASE
)


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


def _max_turns() -> int:
    """Resolve the agentic turn cap from env > config > default.

    Returns 0 when the cap is disabled (CLAUDIO_MAX_TURNS=0 or max_turns: 0),
    in which case no --max-turns flag is passed and Claude runs unbounded.
    """
    env = os.environ.get("CLAUDIO_MAX_TURNS")
    cfg = load_config()
    try:
        turns = int(env) if env else int(cfg.get("max_turns", _DEFAULT_MAX_TURNS))
    except ValueError:
        turns = _DEFAULT_MAX_TURNS
    return max(0, turns)


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


def _partial_messages_enabled() -> bool:
    """Ask the CLI for true token-by-token deltas. On by default.

    Without `--include-partial-messages`, `stream-json` emits one complete
    message snapshot per content block — so "streaming" arrives in chunks,
    not tokens. With it, the CLI also emits `stream_event` wrappers carrying
    Anthropic `content_block_delta` events, which is what actually streams.

    Disable with CLAUDIO_NO_PARTIAL=1 or `partial_messages: false` in config.
    """
    if os.environ.get("CLAUDIO_NO_PARTIAL"):
        return False
    cfg = load_config()
    return bool(cfg.get("partial_messages", True))


def _resolve_effort(override: str | None = None) -> str | None:
    """Resolve --effort from override > env > config. None means don't pass it.

    An unrecognised level is dropped with a warning rather than forwarded —
    the CLI would reject it and fail the whole call.
    """
    value = override or os.environ.get("CLAUDIO_EFFORT") or load_config().get("effort")
    if not value:
        return None
    value = str(value).strip().lower()
    if value not in _EFFORT_LEVELS:
        print(f"[claudio:warn] ignoring unknown effort level {value!r} "
              f"(expected one of: {', '.join(_EFFORT_LEVELS)})", file=sys.stderr)
        return None
    return value


def _resolve_max_budget(override: float | None = None) -> float | None:
    """Resolve --max-budget-usd from override > env > config.

    A hard dollar ceiling on the call: the CLI stops and reports
    `terminal_reason: budget_exhausted` rather than running past it. None
    means no cap. Non-positive or unparseable values are ignored.
    """
    raw = override
    if raw is None:
        raw = os.environ.get("CLAUDIO_MAX_BUDGET_USD") or load_config().get("max_budget_usd")
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        print(f"[claudio:warn] ignoring unparseable max_budget_usd {raw!r}", file=sys.stderr)
        return None
    return value if value > 0 else None


def _strip_unsupported_flag(cmd: list[str], stderr: str) -> list[str] | None:
    """Drop an optional flag the installed CLI rejected, for a degraded retry.

    Returns the shortened command, or None when the error isn't an unknown
    *optional* flag (so genuine failures still surface). Each call removes at
    most one flag, and a removed flag can't match again — the retry loop is
    bounded by the number of optional flags.
    """
    if not stderr:
        return None
    match = _UNKNOWN_OPTION_RE.search(stderr)
    if not match:
        return None
    flag = match.group(1)
    if flag not in _OPTIONAL_FLAGS or flag not in cmd:
        return None
    i = cmd.index(flag)
    end = i + (2 if flag in _VALUE_TAKING_FLAGS else 1)
    return cmd[:i] + cmd[end:]


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
    effort: str | None = None,
    max_budget_usd: float | None = None,
) -> ExecResult:
    """Send a prompt to Claude CLI.

    Args:
        permission_mode: Maps to the CLI's `--permission-mode`. Headless
            `claude --print` denies file-mutating tools by default (there's
            no human to approve them), so a build that should actually edit
            files must pass e.g. `acceptEdits` — otherwise Claude emits the
            Edit/Write tool_use events but the writes are auto-denied and
            nothing reaches disk.
        effort: Maps to `--effort` (low|medium|high|xhigh|max). Falls back to
            CLAUDIO_EFFORT / config when omitted.
        max_budget_usd: Maps to `--max-budget-usd`, a hard spend ceiling for
            this call. Falls back to CLAUDIO_MAX_BUDGET_USD / config.

    Returns:
        ExecResult(text, streamed, usage). `usage` carries the CLI's billed
        figures when it reported them, and is None otherwise.
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
        # the CLI to emit per-event output instead of one big result event.
        cmd.extend(["--output-format", "stream-json", "--verbose"])
        # ...and `--include-partial-messages` is what makes it stream *tokens*.
        # Without it each content block arrives as one finished snapshot.
        if _partial_messages_enabled():
            cmd.append("--include-partial-messages")
    if model:
        cmd.extend(["--model", model])
    # Bound the agentic loop so an ambiguous prompt can't spin on tool calls
    # until the wall-clock timeout. Skipped when disabled (0).
    max_turns = _max_turns()
    if max_turns:
        cmd.extend(["--max-turns", str(max_turns)])
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
    # Effort tunes thinking depth / token spend within the chosen model — a
    # finer-grained cost lever than swapping tiers.
    resolved_effort = _resolve_effort(effort)
    if resolved_effort:
        cmd.extend(["--effort", resolved_effort])
    # Hard dollar ceiling. The CLI stops the run itself rather than letting an
    # ambiguous prompt spend without bound — a truer guard than --max-turns,
    # which caps turns as a proxy for spend.
    budget = _resolve_max_budget(max_budget_usd)
    if budget is not None:
        cmd.extend(["--max-budget-usd", str(budget)])

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
) -> ExecResult:
    """Original retry-with-backoff path. Returns ExecResult(streamed=False).

    Usage is available only when `--output-format json` was requested; a
    plain-text buffered run carries no usage envelope, so `usage` is None and
    callers fall back to estimating.
    """
    last_error: str | None = None
    cmd = list(cmd)
    degraded_retries = len(_OPTIONAL_FLAGS)
    with Spinner(spinner_label) as spin:
        attempt = 0
        while attempt <= max_retries:
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
                    attempt += 1
                    continue
                _print_error(f"Claude CLI {last_error} after {max_retries + 1} attempts.")
                sys.exit(1)
            except FileNotFoundError:
                _print_error(f"Could not execute: {claude_bin}")
                sys.exit(1)

            if result.returncode == 0:
                text = result.stdout.strip()
                return ExecResult(text, False, _usage_from_json_text(text))

            stderr = (result.stderr or "").strip()
            last_error = stderr or f"exit {result.returncode}"

            # An older CLI that doesn't know one of our optional flags: drop
            # it and try again rather than failing the user's call outright.
            if degraded_retries > 0:
                reduced = _strip_unsupported_flag(cmd, stderr)
                if reduced is not None:
                    degraded_retries -= 1
                    dropped = [f for f in cmd if f not in reduced]
                    cmd = reduced
                    print(f"[claudio:warn] your claude CLI does not support "
                          f"{dropped[0]}; retrying without it", file=sys.stderr)
                    continue

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
                attempt += 1
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
) -> ExecResult:
    """Stream JSONL events from claude --output-format stream-json.

    Writes text deltas live to stdout, returns the full aggregated text so
    callers can cache it or log token usage.

    Retries only when no delta has been printed yet — once tokens hit the
    terminal we can't unprint them, so partial-failure cases bubble up as
    errors rather than risk duplicating output on a retry.
    """
    max_retries, backoff_base = _retry_settings()
    last_error: str | None = None
    cmd = list(cmd)
    degraded_retries = len(_OPTIONAL_FLAGS)

    with Spinner(spinner_label) as spin:
        attempt = 0
        while attempt <= max_retries:
            try:
                rc, full_text, stderr, saw_delta, usage = _stream_once(
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
                    attempt += 1
                    continue
                _print_error(f"Claude CLI {last_error} after {max_retries + 1} attempts.")
                sys.exit(1)

            if rc == 0:
                return ExecResult(full_text.strip(), True, usage)

            last_error = (stderr or "").strip() or f"exit {rc}"

            # Unknown optional flag on an older CLI: drop it and retry. Safe
            # even here, because a rejected flag fails before any text exists.
            if not saw_delta and degraded_retries > 0:
                reduced = _strip_unsupported_flag(cmd, stderr)
                if reduced is not None:
                    degraded_retries -= 1
                    dropped = [f for f in cmd if f not in reduced]
                    cmd = reduced
                    print(f"[claudio:warn] your claude CLI does not support "
                          f"{dropped[0]}; retrying without it", file=sys.stderr)
                    continue

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
                attempt += 1
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
) -> tuple[int, str, str, bool, CallUsage | None]:
    """One pass at streaming.

    Returns (returncode, full_text, stderr, saw_delta, usage).

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
    usage: CallUsage | None = None
    # True once a real token delta arrives. With --include-partial-messages
    # the CLI emits BOTH per-token deltas and, when each block finishes, a
    # complete `assistant` snapshot of the same text. Rendering both would
    # print the response twice, so once deltas are flowing the snapshots are
    # used only for their tool_use blocks.
    saw_partial = False
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

            if kind == "usage":
                usage = payload
                continue

            if kind in ("text", "delta"):
                if kind == "delta":
                    saw_partial = True
                elif saw_partial:
                    # Snapshot of text already rendered delta-by-delta.
                    continue
                if not payload:
                    continue
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

    return rc, text, stderr, saw_delta, usage


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


def _parse_stream_event(line: str) -> tuple[str, object]:
    """Parse one stream-json line into (kind, payload).

    Returns:
      ('delta', text)  - a single streamed token run, from a partial-message
                          `stream_event`; render it immediately
      ('text', text)   - a complete `assistant` content-block snapshot
      ('tool', label)  - tool_use event; `label` is e.g. "reading main.py"
                          for display in the spinner or as a breadcrumb
      ('usage', usage) - a CallUsage carrying the CLI's billed figures
      ('', '')         - event not relevant to the user (system, thinking,
                          message_start, etc.)

    Two shapes arrive together. `--output-format stream-json` emits *complete
    message snapshots*: each `"assistant"` event carries `message.content[]`
    with text + tool_use + thinking blocks. `--include-partial-messages`
    additionally emits `"stream_event"` wrappers carrying raw Anthropic
    events, which is where real per-token text deltas live.

    Both cover the same text, so the caller renders deltas when it sees them
    and falls back to snapshots when it doesn't (see `saw_partial` in
    `_stream_once`). Tool labels always come from the snapshot, which is the
    first place a tool_use block is complete enough to summarise.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return ("", "")
    if not isinstance(obj, dict):
        return ("", "")

    t = obj.get("type", "")

    # Partial-message wrapper: the real token stream.
    if t == "stream_event":
        event = obj.get("event")
        return _parse_stream_event_inner(event) if isinstance(event, dict) else ("", "")

    # Terminal event: the only place the CLI reports what the call billed.
    if t == "result":
        usage = _parse_result_usage(obj)
        return ("usage", usage) if usage is not None else ("", "")

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

    # Bare Anthropic delta (some integrations emit these unwrapped).
    if t == "content_block_delta":
        return _parse_stream_event_inner(obj)

    # Light-weight wrapper some integrations use.
    if t == "text":
        text = obj.get("text", "") or ""
        return ("text", text) if text else ("", "")

    return ("", "")


def _parse_stream_event_inner(event: dict) -> tuple[str, object]:
    """Parse a raw Anthropic streaming event into (kind, payload).

    Only text deltas matter to us: `thinking_delta` and `signature_delta`
    are internal reasoning that must not reach stdout, and `input_json_delta`
    is a partially-built tool input we'd rather summarise once it's whole.
    """
    if event.get("type") != "content_block_delta":
        return ("", "")
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ("", "")
    text = delta.get("text", "") or ""
    return ("delta", text) if text else ("", "")


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
