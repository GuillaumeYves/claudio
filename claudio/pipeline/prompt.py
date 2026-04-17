"""Prompt construction -- build minimal, Claude-native prompts.

Design principles:
  1. XML tags over markdown headers -- Claude parses XML natively (tool use format).
     Tags cost fewer tokens than ## Header + newlines.
  2. No duplication -- the user's description appears exactly once.
  3. Intent drives structure -- each mode gets only the instructions it needs.
     No generic "be concise" padding.
  4. Context is pre-compressed -- the prompt builder trusts the pipeline
     already stripped noise. No re-explaining what was removed.
"""


def build_prompt(
    task: str,
    context: str = "",
    constraints: list[str] | None = None,
    output_format: str | None = None,
    intent: str = "general",
) -> str:
    """Build a minimal structured prompt using XML tags.

    Token savings vs markdown format:
      - XML tags: ~2 tokens each vs ~4-6 for ## Header + newline
      - No approach section when constraints exist (was always redundant)
      - No default output_format boilerplate for general intent
    """
    parts: list[str] = []

    # Task -- always first, always present
    parts.append(f"<task>{task}</task>")

    # Intent instruction -- only when there are no explicit constraints
    # (constraints already encode the intent more precisely)
    if not constraints:
        hint = _INTENT_HINTS.get(intent)
        if hint:
            parts.append(f"<intent>{hint}</intent>")

    # Context -- file contents, pre-compressed
    if context:
        parts.append(f"<context>\n{context}\n</context>")

    # Constraints -- only if provided (no defaults)
    if constraints:
        rules = "\n".join(f"- {c}" for c in constraints)
        parts.append(f"<rules>\n{rules}\n</rules>")

    # Output format -- only if explicitly requested
    if output_format:
        parts.append(f"<format>{output_format}</format>")

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
