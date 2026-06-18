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

# Single source of truth for every dollar figure claudio prints (--estimate,
# stats, --verbose). USD per 1M tokens, by model tier. When Anthropic changes
# pricing, edit this table and bump PRICING_LAST_UPDATED — nothing else hardcodes
# a rate. Tiers map from a --model value via _tier_for().
#   Opus 4.8: $5 / $25    Sonnet 4.6: $3 / $15    Haiku 4.5: $1 / $5
PRICING_LAST_UPDATED = "2026-06-04"
_PRICING_PER_M = {
    "haiku":  {"input": 1.00, "output": 5.00},
    "sonnet": {"input": 3.00, "output": 15.00},
    "opus":   {"input": 5.00, "output": 25.00},
}
DEFAULT_TIER = "sonnet"  # used when no model is known / unrecognised

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


def _tier_for(model: str | None) -> str:
    """Map a --model value (alias or full id) to a pricing tier.

    Defaults to DEFAULT_TIER for anything unrecognised, matching the router's
    fallthrough tier.
    """
    m = (model or "").lower()
    if "haiku" in m:
        return "haiku"
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    return DEFAULT_TIER


def estimate_cost(input_tokens: int, output_tokens: int = 500,
                  model: str | None = None) -> float:
    """Estimate cost in USD for a request, priced at `model`'s tier.

    `model` may be an alias ('opus') or a full id ('claude-opus-4-8'); when
    omitted, prices at DEFAULT_TIER so existing callers keep their old
    Sonnet-rate behaviour. Output defaults to a 500-token guess for callers
    that don't yet know the real length.
    """
    rates = _PRICING_PER_M[_tier_for(model)]
    input_cost = (input_tokens / 1_000_000) * rates["input"]
    output_cost = (output_tokens / 1_000_000) * rates["output"]
    return input_cost + output_cost


def counting_method() -> str:
    """How token counts are being produced: 'tiktoken' or 'heuristic'.

    Lets callers tell the user whether a count is a real BPE count (tiktoken
    installed) or the chars-per-token approximation, so dollar figures come
    with their accuracy caveat instead of looking authoritative.
    """
    return "tiktoken" if _get_bpe_encoder() is not None else "heuristic"


def format_estimate(input_tokens: int, model: str | None) -> str:
    """One-line pre-flight cost estimate for --estimate.

    Prices input tokens at the resolved model's tier from the shared pricing
    table. Output tokens are unknown before the call, so they're excluded and
    the line says so. Names the counting method so the figure reads as the
    estimate it is, not a quote.
    """
    rates = _PRICING_PER_M[_tier_for(model)]
    cost = (input_tokens / 1_000_000) * rates["input"]
    label = model or _tier_for(model)
    note = "" if counting_method() == "tiktoken" else ", rough count"
    return (f"~{input_tokens:,} input tokens ({counting_method()}{note}) | "
            f"model: {label} | est. input cost ${cost:.4f} "
            f"(output billed separately, prices as of {PRICING_LAST_UPDATED})")


def format_token_info(token_count: int, is_code: bool = False) -> str:
    """Format token estimate with cost and warnings.

    Cost is priced at DEFAULT_TIER here (this helper has no model in hand);
    the per-call estimate in --verbose / --estimate prices at the resolved
    model. The counting method is named so the figure reads as an estimate.
    """
    cost = estimate_cost(token_count)
    parts = [f"~{token_count:,} tokens ({counting_method()}, est. ${cost:.4f})"]

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
