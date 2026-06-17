"""Shared prompt execution with caching, usage tracking, and model routing.

All commands (build, ask, run) go through this to ensure consistent cache,
stats, model-selection, and session behavior.
"""

import re
import sys

from claudio import session_files
from claudio.cache import cache_get, cache_put
from claudio.config import load_config
from claudio.executor import execute_prompt
from claudio.usage import log_request
from claudio.utils.model_router import pick_model
from claudio.utils.output import Output
from claudio.utils.tokens import estimate_tokens, format_estimate


# Signal pattern for the two-way context channel. Claude is instructed (via
# prompt.py's <context-protocol>) to emit one or more of these when more
# context is needed. We accept either a single <need-context …/> tag or a
# series of them inside an outer <need-context>…</need-context> wrapper.
_NEED_CONTEXT_RE = re.compile(
    r'<need-context\s+'
    r'file=["\']([^"\']+)["\']\s+'
    r'lines=["\']([^"\']+)["\']'
    r'(?:\s+reason=["\']([^"\']*)["\'])?'
    r'\s*/?>',
    re.IGNORECASE,
)

# Signal pattern for the clarification channel. Claude asks a focused
# question when the TASK itself is ambiguous (not the data).
_NEED_CLARIFICATION_RE = re.compile(
    r'<need-clarification\s+question=["\']([^"\']+)["\']\s*/?>',
    re.IGNORECASE,
)

# Signal pattern for the read-only -> build escalation channel. Claude emits
# this from a read-only command (ask/question/debug) when satisfying the
# request would require mutating files or running a build step — work that
# only build/run are allowed to do. `mode` is "generate" (new files) or
# "refactor" (changes to existing code); both attributes are optional so a
# bare <needs-build/> still parses (we default mode to "generate").
_NEEDS_BUILD_RE = re.compile(
    r'<needs-build'
    r'(?:\s+mode=["\']([^"\']*)["\'])?'
    r'(?:\s+reason=["\']([^"\']*)["\'])?'
    r'\s*/?>',
    re.IGNORECASE,
)


def parse_need_clarification(response: str) -> str | None:
    """If `response` is a clarification request, return the question text.

    Returns None when the response is a normal answer. Recognised only at
    the very start of the response, same as parse_need_context.
    """
    stripped = response.strip()
    if not stripped.lower().startswith("<need-clarification"):
        return None
    m = _NEED_CLARIFICATION_RE.search(stripped)
    if not m:
        return None
    return m.group(1).strip() or None


def collect_clarification_answer(question: str, out: Output) -> str | None:
    """Surface a clarification question to the dev and collect their answer.

    Interactive (stdin is a TTY): print the question to stderr and read a
    one-line answer from stdin. Empty answer means "abort, no retry".

    Non-interactive (piped/CI): print to stderr and return None -- the
    caller must skip the retry and bubble the unanswered question up to
    the user, who can re-run with the clarifying info appended.
    """
    out.info(f"[claudio] Claude needs clarification: {question}")
    if not sys.stdin.isatty():
        out.info("[claudio] (non-interactive shell — re-run with the answer "
                 "appended to your prompt)")
        return None
    try:
        sys.stderr.write("clarify > ")
        sys.stderr.flush()
        answer = sys.stdin.readline().rstrip("\r\n")
    except (EOFError, KeyboardInterrupt):
        return None
    answer = answer.strip()
    return answer or None


def parse_needs_build(response: str) -> tuple[str, str] | None:
    """If `response` is a read-only -> build escalation, return (mode, reason).

    `mode` is normalised to "generate" or "refactor" (anything unrecognised,
    or a bare tag with no mode, falls back to "generate"). `reason` may be an
    empty string. Returns None for a normal answer. Recognised only when the
    response *starts* with the tag, mirroring the other signals — so Claude
    quoting `<needs-build/>` mid-explanation stays prose.
    """
    stripped = response.strip()
    if not stripped.lower().startswith("<needs-build"):
        return None
    m = _NEEDS_BUILD_RE.search(stripped)
    if not m:
        return None
    mode = (m.group(1) or "").strip().lower()
    if mode not in ("generate", "refactor"):
        mode = "generate"
    reason = (m.group(2) or "").strip()
    return mode, reason


def parse_need_context(response: str) -> list[tuple[str, str, str]] | None:
    """Parse a need-context signal. Returns a list of (file, lines, reason).

    Returns None when the response isn't a signal. Treating a response as a
    signal requires it to *start* with <need-context — mid-answer mentions
    are left as prose so Claude can still quote the tag in normal output.

    Multiple ranges per call are supported -- Claude can ask for two files
    at once instead of being forced into serial round-trips.
    """
    stripped = response.strip()
    if not stripped.lower().startswith("<need-context"):
        return None
    matches = _NEED_CONTEXT_RE.findall(stripped)
    if not matches:
        return None
    return [(file, lines, reason or "") for file, lines, reason in matches]


def mark_session_files(ctx: dict, files, out: Output) -> None:
    """Per-session @file dedup + stale-file warning, shared by ask/build/run.

    For each attached file the current session has already seen:
      - content unchanged -> flag it so format_file_context collapses it to a
        compact marker instead of re-sending the body;
      - content changed since last seen -> warn the user (the new body IS
        re-sent, so Claude never reasons on stale code — the warning is
        informational, addressing the "resume can serve stale code" gap).

    No-op without files or a session key: one-shot calls with no --session-id
    / --resume keep re-sending everything, exactly as before.
    """
    session_id = ctx.get("session_id") or ctx.get("resume")
    if not (files and session_id):
        return
    for path, lines in session_files.changed_since_seen(session_id, files):
        loc = f"{path} lines {lines}" if lines else path
        out.warn(f"[claudio] {loc} changed since this session last saw it "
                 f"— re-sending the new version")
    unchanged = session_files.mark_files_seen(session_id, files)
    for fa in files:
        if (fa.path, fa.lines) in unchanged:
            fa.unchanged = True


def resolve_model(ctx: dict, intent: str, input_tokens: int, cmd: str = "") -> str | None:
    """Resolve the model to use for this call.

    Precedence:
      1. --model flag (ctx['model'])
      2. config.json 'default_model' (only honored if not the legacy 'sonnet'
         default, which we treat as "no preference" so routing kicks in)
      3. build floor: `build` writes code, so absent an explicit preference it
         gets Opus regardless of input size — size-based routing would drop
         most builds to Sonnet, which is too weak for the verb that mutates
         the user's files.
      4. pick_model(intent, input_tokens)
    """
    if ctx.get("model"):
        return ctx["model"]
    config = load_config()
    configured = config.get("default_model")
    # If user explicitly set something non-default, respect it. Otherwise route.
    if configured and configured not in ("sonnet", None, ""):
        return configured
    if cmd == "build":
        return "opus"
    return pick_model(intent, input_tokens)


# Permission modes that let Claude mutate the filesystem. Runs in these
# modes are side-effecting, so their output must NOT be cached — a cache hit
# would replay the stored narration ("Done!") without re-applying the edit.
_MUTATING_PERMISSION_MODES = {"acceptEdits", "bypassPermissions"}


def execute_with_tracking(
    prompt: str,
    ctx: dict,
    out: Output,
    cmd: str,
    mode: str,
    intent: str = "general",
    metadata: dict | None = None,
    allowed_tools: list[str] | None = None,
    permission_mode: str | None = None,
) -> str | None:
    """Execute a prompt with cache check, model routing, and usage tracking.

    Args:
        prompt: The final optimized prompt.
        ctx: Global flags dict (dry_run, no_cache, verbose, json_output,
            model, session_id, resume, ...). The REPL sets session_id once
            per REPL session so successive commands keep Anthropic's prompt
            cache warm.
        out: Output formatter.
        cmd: Command name for stats (build, ask, run).
        mode: Submode for stats (refactor, generate, review, etc).
        intent: Pipeline intent — drives model routing when --model not set.
        metadata: Pipeline metadata to display in verbose mode.
        allowed_tools: Optional tool allowlist (used by agentic run).
        permission_mode: Optional CLI permission mode. When it permits file
            mutation (see _MUTATING_PERMISSION_MODES), caching is bypassed so
            the edit actually runs every time instead of replaying a stored
            response.

    Returns:
        Response string, or None if dry-run.
    """
    input_tokens = estimate_tokens(prompt)
    model = resolve_model(ctx, intent, input_tokens, cmd=cmd)

    if ctx.get("verbose") and model:
        out.info(f"[claudio] model: {model}")

    if ctx.get("verbose"):
        from claudio.config import permission_posture
        mode_label = permission_mode or "default"
        out.info(f"[claudio] permission mode: {mode_label} (posture: {permission_posture()})")

    # --estimate -- price the request and stop before calling Claude. Like
    # --dry-run, but reports token count + projected input cost instead of the
    # raw prompt. Takes precedence over --dry-run when both are set: the user
    # asked for the number, not the text.
    if ctx.get("estimate"):
        out.info(f"[claudio] estimate: {format_estimate(input_tokens, model)}")
        return None

    # Dry run -- show prompt, log nothing
    if ctx["dry_run"]:
        out.result(prompt, metadata=metadata)
        return None

    # Cache check (unless --no-cache). Skip cache when resuming a session —
    # the user explicitly wants a fresh Claude turn, not a stored echo — and
    # whenever this run can mutate files, since edits are side effects that a
    # cached response wouldn't reproduce.
    mutating = permission_mode in _MUTATING_PERMISSION_MODES
    use_cache = not ctx.get("no_cache") and not ctx.get("resume") and not mutating
    if use_cache:
        cached = cache_get(prompt)
        if cached is not None:
            out.info("[cache hit] Returning cached response")
            log_request(cmd, mode, input_tokens, cached=True)
            out.result(cached, metadata=metadata if ctx["verbose"] else None)
            return cached

    # Execute
    response, was_streamed = execute_prompt(
        prompt,
        json_output=ctx["json_output"],
        model=model,
        session_id=ctx.get("session_id"),
        resume=ctx.get("resume"),
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
    )

    # Cache store
    if use_cache:
        cache_put(prompt, response, input_tokens)

    # Log usage
    output_tokens = estimate_tokens(response)
    log_request(cmd, mode, input_tokens, output_tokens=output_tokens, cached=False)

    # When the executor streamed text live, the response is already on the
    # user's screen — don't print it again. We still want verbose metadata
    # though (token counts, model, savings), so emit that separately.
    if was_streamed:
        if ctx["verbose"] and metadata:
            for key, value in metadata.items():
                out.info(f"  {key}: {value}")
    else:
        out.result(response, metadata=metadata if ctx["verbose"] else None)
    return response
