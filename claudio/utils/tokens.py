"""Token estimation and cost-awareness utilities.

Counting strategy (best-available, cached at module level):
  1. tiktoken with cl100k_base — OpenAI's BPE, but the closest *offline*
     approximation to Anthropic's tokenizer. Typically within ~10% of
     Anthropic's actual count; close enough for model routing and cost
     display, and a lot tighter than the char-density heuristic.
  2. Character-density heuristic — len(text)/4 for prose, len(text)/3 for
     code. The original fallback; ships with stdlib only, no install.

tiktoken is an *optional* dependency. If it isn't installed (or fails to
load its data files in this environment) we silently degrade to the
heuristic — no errors, same call signature.
"""

# Approximate token ratios (chars per token) for Claude models.
# Claude averages ~3.5-4 chars per token for English text, ~3 for code.
CHARS_PER_TOKEN_TEXT = 4.0
CHARS_PER_TOKEN_CODE = 3.0

# Pricing per 1M tokens (Claude Sonnet 4, as of 2025)
COST_INPUT_PER_M = 3.00   # $3 per 1M input tokens
COST_OUTPUT_PER_M = 15.00  # $15 per 1M output tokens

# Thresholds
WARN_TOKEN_THRESHOLD = 8_000   # Warn if input exceeds this
LARGE_TOKEN_THRESHOLD = 32_000  # Strongly suggest compression


_BPE_ENCODER = None
_BPE_ATTEMPTED = False


def _get_bpe_encoder():
    """Lazy-load tiktoken's cl100k_base encoder. Returns None if unavailable."""
    global _BPE_ENCODER, _BPE_ATTEMPTED
    if _BPE_ATTEMPTED:
        return _BPE_ENCODER
    _BPE_ATTEMPTED = True
    try:
        import tiktoken  # type: ignore
        _BPE_ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:
        # ImportError (not installed), network errors fetching the BPE,
        # data-file load failures — all treated the same: fall back.
        _BPE_ENCODER = None
    return _BPE_ENCODER


def estimate_tokens(text: str, is_code: bool = False) -> int:
    """Estimate token count for `text`.

    Uses tiktoken when available, the char-density heuristic otherwise.
    Returns at least 1 even for empty strings so callers can safely divide.
    """
    if not text:
        return 1
    enc = _get_bpe_encoder()
    if enc is not None:
        try:
            n = len(enc.encode(text))
            return max(1, n)
        except Exception:
            # Encoder corruption / OOM / unusual input: stop using it and
            # drop through to the heuristic instead of bubbling the error.
            pass
    ratio = CHARS_PER_TOKEN_CODE if is_code else CHARS_PER_TOKEN_TEXT
    return max(1, int(len(text) / ratio))


def estimate_cost(input_tokens: int, output_tokens: int = 500) -> float:
    """Estimate cost in USD for a request."""
    input_cost = (input_tokens / 1_000_000) * COST_INPUT_PER_M
    output_cost = (output_tokens / 1_000_000) * COST_OUTPUT_PER_M
    return input_cost + output_cost


# Approximate input pricing per 1M tokens by model tier (USD). Output is
# billed separately and varies with response length, so --estimate reports
# input cost only. Kept coarse on purpose — this is a pre-flight gut-check,
# not a billing statement.
_INPUT_PRICE_PER_M = {
    "haiku": 1.00,
    "sonnet": 3.00,
    "opus": 15.00,
}


def _tier_for(model: str | None) -> str:
    """Map a --model value (alias or full id) to a pricing tier.

    Defaults to 'sonnet' for anything unrecognised, matching the router's
    fallthrough tier.
    """
    m = (model or "").lower()
    if "haiku" in m:
        return "haiku"
    if "opus" in m:
        return "opus"
    return "sonnet"


def format_estimate(input_tokens: int, model: str | None) -> str:
    """One-line pre-flight cost estimate for --estimate.

    Prices input tokens at the resolved model's tier. Output tokens are
    unknown before the call, so they're excluded and the line says so.
    """
    tier = _tier_for(model)
    cost = (input_tokens / 1_000_000) * _INPUT_PRICE_PER_M[tier]
    label = model or tier
    return (f"~{input_tokens:,} input tokens | model: {label} | "
            f"est. input cost ${cost:.4f} (output billed separately)")


def format_token_info(token_count: int, is_code: bool = False) -> str:
    """Format token estimate with cost and warnings."""
    cost = estimate_cost(token_count)
    parts = [f"~{token_count:,} tokens (est. ${cost:.4f})"]

    if token_count > LARGE_TOKEN_THRESHOLD:
        parts.append("WARNING: Large input — consider using --lines or --scope to reduce context")
    elif token_count > WARN_TOKEN_THRESHOLD:
        parts.append("Note: Moderately large input — compression applied")

    return " | ".join(parts)


def is_code_file(path: str) -> bool:
    """Heuristic: does this file path look like code?"""
    code_extensions = {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
        ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
        ".kt", ".scala", ".sh", ".bash", ".zsh", ".sql", ".yaml",
        ".yml", ".toml", ".json", ".xml", ".html", ".css", ".scss",
    }
    from pathlib import Path
    return Path(path).suffix.lower() in code_extensions
