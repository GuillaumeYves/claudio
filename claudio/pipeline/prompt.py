"""Prompt construction -- build minimal, Claude-native prompts.

Design principles:
  1. XML tags over markdown headers -- Claude parses XML natively (same
     format used for tool use). Tags cost fewer tokens than `## Header`.
  2. No duplication -- the user's description appears exactly once in <task>.
  3. Intent drives structure -- each mode gets only the instructions it
     needs. No generic "be concise" padding.
  4. Context is pre-compressed -- the prompt builder trusts the pipeline
     already stripped noise. No re-explaining what was removed.
  5. Cache-aligned order -- the stable prefix (`<project>` then `<rules>`
     / `<format>` / `<intent>` / `<context-protocol>` / `<context>`) sits
     first; the volatile tail (`<changes>` then `<task>`) sits last. The
     Anthropic prompt cache keys on the prefix, so this layout maximises
     warm-cache reuse across back-to-back calls.
  6. Whitespace is normalised before tagging -- trailing spaces and runs of
     blank lines are collapsed. They cost tokens without adding signal.
"""

import re

_BLANK_RUN_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+(?=\n)")


def _tighten(text: str) -> str:
    """Strip trailing whitespace and collapse 3+ blank lines into 2.

    Cheap pre-pass that shaves ~5-15% off most pasted-in code blocks
    without altering meaning. Run on context only; the user's task
    string is left untouched so quoted whitespace survives.
    """
    if not text:
        return text
    text = _TRAILING_WS_RE.sub("", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip("\n")


def build_prompt(
    task: str,
    context: str = "",
    constraints: list[str] | None = None,
    output_format: str | None = None,
    intent: str = "general",
    allow_context_request: bool = False,
    project_preamble: str = "",
    git_changes: str = "",
) -> str:
    """Build a minimal structured prompt using XML tags.

    Order (cache-friendly):
      <project> <rules> <format> <intent> <context-protocol>
        <context> <changes> <task>
                                   stable prefix                         | variable

    Keeping <task> last means two back-to-back calls on the same file + mode
    share a long cacheable prefix, so the second one can hit the 5-minute
    prompt cache instead of paying full input cost. <project> goes first
    because it is the most stable element of all — it only changes when
    CLAUDE.md / .claudio/project.md change. <changes> sits right before
    <task> because it shifts whenever the dev edits a file — keeping it
    out of the cached prefix.
    """
    parts: list[str] = []

    # --- Stable prefix (cacheable) ---

    # Project preamble -- codebase-specific context from CLAUDE.md /
    # .claudio/project.md. Sits at the very front so it caches forever.
    if project_preamble:
        parts.append(f"<project>\n{project_preamble}\n</project>")

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

    # Context -- file contents, pre-compressed. Tighten whitespace one more
    # time before tagging: the compressor preserves source formatting, but
    # blank-line runs and trailing spaces are pure token waste here.
    if context:
        tight = _tighten(context)
        if tight:
            parts.append(f"<context>\n{tight}\n</context>")

    # --- Variable (not cached) ---

    # Git changes -- shifts with every edit, so it lives in the volatile
    # tail. Only present for review/debug/refactor in a git repo.
    if git_changes:
        tight = _tighten(git_changes)
        if tight:
            parts.append(f"<changes>\n{tight}\n</changes>")

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


# Instruction that opens a two-way channel for context. Two signals are
# supported -- both parsed by run_prompt.py and honored once per call.
#
#   <need-context/>      missing CODE (claudio compressed too aggressively)
#   <need-clarification/> ambiguous TASK (the request itself is unclear)
#
# Use exactly one type per response. For context, multiple ranges in one
# response are allowed (they expand in a single retry). For clarification,
# one focused question is best -- claudio surfaces it to the dev.
_CONTEXT_PROTOCOL = (
    "<context-protocol>\n"
    "If <context> is insufficient, respond with ONLY one or more:\n"
    '<need-context file="PATH" lines="START-END" reason="..."/>\n'
    "tags back-to-back (multiple ranges allowed) and stop.\n"
    "If the task itself is ambiguous (not the data), respond with ONLY:\n"
    '<need-clarification question="..."/>\n'
    "and stop. Otherwise proceed normally.\n"
    "</context-protocol>"
)
