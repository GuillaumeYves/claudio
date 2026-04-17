"""Shared prompt execution with caching, usage tracking, and model routing.

All commands (build, ask, run) go through this to ensure consistent cache,
stats, model-selection, and session behavior.
"""

import re

from claudio.cache import cache_get, cache_put
from claudio.config import load_config
from claudio.executor import execute_prompt
from claudio.usage import log_request
from claudio.utils.model_router import pick_model
from claudio.utils.output import Output
from claudio.utils.tokens import estimate_tokens


# Signal pattern for the two-way context channel. Claude is instructed (via
# prompt.py's <context-protocol>) to emit this when it needs more context.
_NEED_CONTEXT_RE = re.compile(
    r'<need-context\s+'
    r'file=["\']([^"\']+)["\']\s+'
    r'lines=["\']([^"\']+)["\']'
    r'(?:\s+reason=["\']([^"\']*)["\'])?'
    r'\s*/?>',
    re.IGNORECASE,
)


def parse_need_context(response: str) -> tuple[str, str, str] | None:
    """If `response` is a need-context signal, return (file, lines, reason).

    Returns None when the response is a normal answer. We only treat a
    response as a signal when it *starts* with <need-context — mid-answer
    occurrences are ignored so Claude can still quote the tag in prose.
    """
    stripped = response.strip()
    if not stripped.lower().startswith("<need-context"):
        return None
    m = _NEED_CONTEXT_RE.search(stripped)
    if not m:
        return None
    return m.group(1), m.group(2), (m.group(3) or "")


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
            model, session_id, resume, ...).
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
    response = execute_prompt(
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

    out.result(response, metadata=metadata if ctx["verbose"] else None)
    return response
