"""Pre-flight specification check — local, free, and non-rewriting.

What actually degrades an answer is rarely poor wording; Claude handles
vague phrasing well. It is missing *specification*: which file, which
lines, what must not change, what "done" looks like. That gap is not
visible in the response — you get a confident answer built on a guess,
and only find out later.

This module names the gap before the call, for free. Three rules keep it
honest:

  1. **It never rewrites the prompt.** It reports what is missing and
     leaves the words alone. Silently reshaping a request is the same
     mistake as the compression stage removed in 2.0.0 — a well-meant
     transformation the user cannot see.
  2. **It costs nothing.** Pure local inspection, no model call. A check
     that itself costs a call cannot pay for itself on a cheap request.
  3. **It advises by default.** Findings print as warnings and the call
     proceeds. `--strict-spec` promotes them to a refusal for people who
     want the gate; nobody gets blocked by surprise.

Deliberately *not* included: anything requiring the model's judgment.
These are mechanical signals with low false-positive rates, because a
check that cries wolf gets ignored and then it protects nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Verbs that name an action without naming its target. "fix" tells claudio
# nothing about what is broken; "fix the timeout in auth.py" does.
_VAGUE_OPENERS = re.compile(
    r"^\s*(?:please\s+)?"
    r"(fix|clean\s*up|improve|optimi[sz]e|refactor|update|change|make\s+it\s+better|"
    r"tidy|polish|sort\s+out|handle|deal\s+with)\b",
    re.IGNORECASE,
)

# Signals that the prompt already carries concrete evidence to work from.
_HAS_EVIDENCE = re.compile(
    r"(error|exception|traceback|stack\s*trace|fail|assert|"
    r"expected|actual|returns?|line\s+\d+|\bbug\b.{0,40}\bwhen\b|"
    r"```|\bTypeError\b|\bValueError\b|\d{3}\s+(?:error|status))",
    re.IGNORECASE,
)

# Signals that an outcome / acceptance criterion is stated.
_HAS_CRITERION = re.compile(
    r"(so\s+that|so\s+it|must\s+|should\s+|without\s+|keep\s+|preserve|"
    r"don'?t\s+|do\s+not\s+|instead\s+of|rather\s+than|while\s+still|"
    r"ensure|make\s+sure|tests?\s+pass)",
    re.IGNORECASE,
)

# A prompt this short cannot carry a target and a criterion.
_TERSE_PROMPT_WORDS = 4

# An attachment this large without a line range means Claude reads (and you
# pay for) far more than the task needs.
_LARGE_ATTACHMENT_TOKENS = 6_000


@dataclass(frozen=True)
class Finding:
    """One thing the request does not pin down.

    `hint` is phrased as the concrete addition that would close the gap,
    because "be more specific" is not actionable and "attach the failing
    line range" is.
    """

    code: str
    message: str
    hint: str


def check(prompt: str, files, cmd: str, mode: str,
          input_tokens: int = 0) -> list[Finding]:
    """Inspect a request for missing specification. Returns findings.

    Args:
        prompt: the user's own description, before prompt assembly.
        files: resolved FileAttachment objects (may be empty).
        cmd: "build" | "ask" | "run".
        mode: the submode (refactor, generate, review, question, debug).
        input_tokens: estimated size of the assembled prompt, used only to
            flag oversized attachments.

    An empty list means nothing mechanical is missing — not that the
    request is good. This check has no opinion on whether the task is
    worth doing.
    """
    findings: list[Finding] = []
    text = (prompt or "").strip()
    words = text.split()

    if not text:
        # A bare `build -r @file.py` with no description at all.
        if files:
            findings.append(Finding(
                "no-task",
                "no description — only files were attached",
                "say what to do with them, e.g. "
                '`build -r @auth.py "extract the retry loop into a helper"`',
            ))
        return findings

    vague_opener = bool(_VAGUE_OPENERS.match(text))
    has_evidence = bool(_HAS_EVIDENCE.search(text))
    has_criterion = bool(_HAS_CRITERION.search(text))

    # A vague verb with nothing to act on: the highest-value catch, because
    # Claude will pick a target and may well pick the wrong one.
    if vague_opener and not files and not has_evidence:
        findings.append(Finding(
            "no-target",
            f'"{words[0].lower()}" without a file or an error to work from',
            "attach the file (`@path/to/file.py`) or paste the error text",
        ))

    # Debugging with no symptom is guesswork dressed as analysis.
    if mode == "debug" and not has_evidence:
        findings.append(Finding(
            "no-symptom",
            "debug mode without an error message, traceback, or failing case",
            "paste the traceback, or say what you expected vs. what happened",
        ))

    # A mutation with no stated constraint invites collateral change.
    if cmd == "build" and not has_criterion and len(words) < 12:
        findings.append(Finding(
            "no-criterion",
            "no constraint on what the change must preserve",
            'add what must not change, e.g. "...without altering the public API"',
        ))

    if len(words) < _TERSE_PROMPT_WORDS and not has_evidence:
        findings.append(Finding(
            "terse",
            f"only {len(words)} word(s) of description",
            "one more clause about the goal usually beats a longer file",
        ))

    # Paying to send a whole large file when a range would do.
    if input_tokens > _LARGE_ATTACHMENT_TOKENS:
        unranged = [f for f in files if not getattr(f, "lines", None)]
        if unranged:
            names = ", ".join(f.path for f in unranged[:3])
            findings.append(Finding(
                "unranged-large",
                f"~{input_tokens:,} tokens sent in full ({names})",
                "attach a line range instead: `@file.py -120-180`",
            ))

    return findings


def render(findings: list[Finding], strict: bool) -> str:
    """Format findings for the user. Empty string when there are none."""
    if not findings:
        return ""
    verb = "refusing" if strict else "sending anyway"
    lines = [f"under-specified request ({verb}):"]
    for f in findings:
        lines.append(f"  - {f.message}")
        lines.append(f"    try: {f.hint}")
    if not strict:
        lines.append("  (--strict-spec turns these into a refusal; "
                     "--no-spec-check silences them)")
    return "\n".join(lines)
