"""Model selection by intent and input size.

Rationale: the smallest model that can handle the task wins on latency and
cost. Map intent + input size -> model tier, then let config or --model
override the pick.

Tiers (aliases are accepted by `claude --model`):
  haiku   -- fast/cheap; small questions, trivial asks
  sonnet  -- default balanced tier
  opus    -- heavy analysis, large context, high-stakes review/refactor
"""

# Thresholds in estimated input tokens.
_HAIKU_CEILING = 2_000       # tiny prompts: go cheap
_OPUS_FLOOR_GENERAL = 20_000  # huge prompts: go smart regardless of intent
_OPUS_FLOOR_ANALYSIS = 8_000  # review/refactor on non-trivial code: go smart

_HEAVY_INTENTS = {"review", "refactor", "debug"}
_LIGHT_INTENTS = {"general", "question"}


def pick_model(intent: str, input_tokens: int) -> str:
    """Pick a model tier based on intent and input size.

    Returns an alias ('haiku' | 'sonnet' | 'opus') that `claude --model`
    accepts directly.
    """
    if intent in _LIGHT_INTENTS and input_tokens < _HAIKU_CEILING:
        return "haiku"

    if input_tokens > _OPUS_FLOOR_GENERAL:
        return "opus"

    if intent in _HEAVY_INTENTS and input_tokens > _OPUS_FLOOR_ANALYSIS:
        return "opus"

    return "sonnet"
