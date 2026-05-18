"""Shared prompt execution with caching, usage tracking, and model routing.

All commands (build, ask, run) go through this to ensure consistent cache,
stats, model-selection, and session behavior.
"""

import re
import sys

from claudio.cache import cache_get, cache_put
from claudio.config import load_config
from claudio.executor import execute_prompt
from claudio.usage import log_request
from claudio.utils.model_router import pick_model
from claudio.utils.output import Output
from claudio.utils.tokens import estimate_tokens


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


def resolve_model(ctx: dict, intent: str, input_tokens: int) -> str | None:
    """Resolve the model to use for this call.

    Precedence:
      1. --model flag (ctx['model'])
      2. config.json 'default_model' (only honored if not the legacy 'sonnet'
         default, which we treat as "no preference" so routing kicks in)
      3. pick_model(intent, input_tokens)
    """
    if ctx.get("model"):
        return ctx["model"]
    config = load_config()
    configured = config.get("default_model")
    # If user explicitly set something non-default, respect it. Otherwise route.
    if configured and configured not in ("sonnet", None, ""):
        return configured
    return pick_model(intent, input_tokens)


def execute_with_tracking(
    prompt: str,
    ctx: dict,
    out: Output,
    cmd: str,
    mode: str,
    intent: str = "general",
    metadata: dict | None = None,
    allowed_tools: list[str] | None = None,
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

    Returns:
        Response string, or None if dry-run.
    """
    input_tokens = estimate_tokens(prompt)
    model = resolve_model(ctx, intent, input_tokens)

    if ctx.get("verbose") and model:
        out.info(f"[claudio] model: {model}")

    # Dry run -- show prompt, log nothing
    if ctx["dry_run"]:
        out.result(prompt, metadata=metadata)
        return None

    # Cache check (unless --no-cache). Skip cache when resuming a session —
    # the user explicitly wants a fresh Claude turn, not a stored echo.
    use_cache = not ctx.get("no_cache") and not ctx.get("resume")
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
