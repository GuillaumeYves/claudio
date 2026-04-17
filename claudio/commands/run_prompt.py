"""Shared prompt execution with caching and usage tracking.

All commands (build, ask, run) go through this to ensure
consistent cache and stats behavior.
"""

from claudio.cache import cache_get, cache_put
from claudio.executor import execute_prompt
from claudio.usage import log_request
from claudio.utils.output import Output
from claudio.utils.tokens import estimate_tokens


def execute_with_tracking(
    prompt: str,
    ctx: dict,
    out: Output,
    cmd: str,
    mode: str,
    metadata: dict | None = None,
) -> str | None:
    """Execute a prompt with cache check and usage tracking.

    Args:
        prompt: The final optimized prompt.
        ctx: Global flags dict (dry_run, no_cache, verbose, json_output).
        out: Output formatter.
        cmd: Command name for stats (build, ask, run).
        mode: Submode for stats (refactor, generate, review, etc).
        metadata: Pipeline metadata to display in verbose mode.

    Returns:
        Response string, or None if dry-run.
    """
    input_tokens = estimate_tokens(prompt)

    # Dry run -- show prompt, log nothing
    if ctx["dry_run"]:
        out.result(prompt, metadata=metadata)
        return None

    # Cache check (unless --no-cache)
    if not ctx.get("no_cache"):
        cached = cache_get(prompt)
        if cached is not None:
            out.info("[cache hit] Returning cached response")
            log_request(cmd, mode, input_tokens, cached=True)
            out.result(cached, metadata=metadata if ctx["verbose"] else None)
            return cached

    # Execute
    response = execute_prompt(prompt, json_output=ctx["json_output"])

    # Cache store
    if not ctx.get("no_cache"):
        cache_put(prompt, response, input_tokens)

    # Log usage
    output_tokens = estimate_tokens(response)
    log_request(cmd, mode, input_tokens, output_tokens=output_tokens, cached=False)

    out.result(response, metadata=metadata if ctx["verbose"] else None)
    return response
