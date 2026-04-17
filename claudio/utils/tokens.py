"""Token estimation and cost-awareness utilities."""

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


def estimate_tokens(text: str, is_code: bool = False) -> int:
    """Estimate token count from text length."""
    ratio = CHARS_PER_TOKEN_CODE if is_code else CHARS_PER_TOKEN_TEXT
    return max(1, int(len(text) / ratio))


def estimate_cost(input_tokens: int, output_tokens: int = 500) -> float:
    """Estimate cost in USD for a request."""
    input_cost = (input_tokens / 1_000_000) * COST_INPUT_PER_M
    output_cost = (output_tokens / 1_000_000) * COST_OUTPUT_PER_M
    return input_cost + output_cost


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
