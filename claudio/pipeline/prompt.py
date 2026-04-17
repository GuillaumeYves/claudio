"""Prompt construction -- build minimal, Claude-native prompts.

Design principles:
  1. XML tags over markdown headers -- Claude parses XML natively (tool use format).
     Tags cost fewer tokens than ## Header + newlines.
  2. No duplication -- the user's description appears exactly once.
  3. Intent drives structure -- each mode gets only the instructions it needs.
     No generic "be concise" padding.
  4. Context is pre-compressed -- the prompt builder trusts the pipeline
     already stripped noise. No re-explaining what was removed.
  5. Cache-aligned order -- stable sections (rules, format, intent, context)
     come first, variable <task> is last. The Anthropic prompt cache keys on
     the prefix, so a stable prefix keeps warm caches across calls that share
     the same file context and mode.
"""


def build_prompt(
    task: str,
    context: str = "",
    constraints: list[str] | None = None,
    output_format: str | None = None,
    intent: str = "general",
    allow_context_request: bool = False,
) -> str:
    """Build a minimal structured prompt using XML tags.

    Order (cache-friendly):
      <rules> <format> <intent> <context-protocol> <context> <task>
                                   stable prefix               | variable

    Keeping <task> last means two back-to-back calls on the same file + mode
    share a long cacheable prefix, so the second one can hit the 5-minute
    prompt cache instead of paying full input cost.
    """
    parts: list[str] = []

    # --- Stable prefix (cacheable) ---

    # Constraints -- only if provided (no defaults)
    if constraints:
        rules = "\n".join(f"- {c}" for c in constraints)
        parts.append(f"<rules>\n{rules}\n</rules>")

    # Output format -- only if explicitly requested
    if output_format:
        parts.append(f"<format>{output_format}</format>")

    # Intent instruction -- only when there are no explicit constraints
    # (constraints already encode the intent more precisely)
    if not constraints:
        hint = _INTENT_HINTS.get(intent)
        if hint:
            parts.append(f"<intent>{hint}</intent>")

    # Two-way context protocol -- lets Claude request more context instead
    # of hallucinating when compression was too aggressive.
    if allow_context_request:
        parts.append(_CONTEXT_PROTOCOL)

    # Context -- file contents, pre-compressed
    if context:
        parts.append(f"<context>\n{context}\n</context>")

    # --- Variable (not cached) ---

    # Task -- always last so the prefix stays stable across calls
    parts.append(f"<task>{task}</task>")

    return "\n".join(parts)


# Compact intent hints -- only used when no constraints are provided.
# When constraints exist, they already express intent more precisely.
_INTENT_HINTS = {
    "debug": "Find root cause. Give fix.",
    "refactor": "Preserve behavior. Output diff.",
    "generate": "Production-ready code.",
    "review": "Flag bugs and security issues by severity.",
    "general": "",  # No hint needed for questions
}


# Instruction that opens a two-way channel for context. When the pipeline
# compresses a large file to a structural map, Claude can signal it needs
# specific lines back instead of guessing. Parsed by run_prompt.py.
_CONTEXT_PROTOCOL = (
    "<context-protocol>\n"
    "If <context> is insufficient to answer well, respond with ONLY:\n"
    '<need-context file="PATH" lines="START-END" reason="..."/>\n'
    "and stop. Otherwise proceed normally.\n"
    "</context-protocol>"
)
