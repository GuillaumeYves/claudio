"""Main processing pipeline -- orchestrates filter and prompt stages.

As of 2.0.0 claudio sends attached content *faithfully*. The old lossy
compression stage (structural maps for large files, log summaries) was
removed: it counteracted the point of the tool — you'd think you attached a
file in full and Claude would silently receive a table of contents. What
remains is lossless: noise filtering (trailing whitespace, blank-line runs,
license headers, log dedup) and Claude-native prompt framing.
"""

from claudio.pipeline.filter import filter_noise
from claudio.pipeline.prompt import build_prompt
from claudio.utils.git_context import discover_git_changes
from claudio.utils.project_context import discover_project_preamble
from claudio.utils.tokens import estimate_tokens, is_code_file


class PipelineResult:
    """Result of processing an input through the pipeline."""

    __slots__ = ("prompt", "input_tokens", "sent_tokens", "metadata")

    def __init__(self, prompt: str, input_tokens: int, sent_tokens: int, metadata: dict):
        self.prompt = prompt
        self.input_tokens = input_tokens
        # Tokens of the content actually sent (post noise-filter). No longer a
        # "compressed" figure — filtering only removes whitespace/boilerplate.
        self.sent_tokens = sent_tokens
        self.metadata = metadata

    @property
    def tokens_saved(self) -> int:
        """Tokens shaved by noise filtering (usually small; never lossy)."""
        return self.input_tokens - self.sent_tokens


def process(
    raw_input: str,
    task: str,
    intent: str = "general",
    filename: str = "",
    constraints: list[str] | None = None,
    output_format: str | None = None,
    allow_context_request: bool = False,
    readonly_escalation: bool = False,
) -> PipelineResult:
    """Run the processing pipeline on raw input.

    Pipeline:
      1. Estimate raw tokens
      2. Filter noise (intent-aware: strips comments for refactor)
      3. Build minimal XML-tagged prompt (faithful content, no compression)
      4. Return result with metadata
    """
    is_code = is_code_file(filename) if filename else False
    input_tokens = estimate_tokens(raw_input, is_code)

    # Stage 1: Filter noise -- pass intent so behavior-focused modes strip comments
    filtered = filter_noise(
        raw_input,
        mode="code" if is_code else "auto",
        intent=intent,
    )

    # The filtered content is what Claude sees — faithfully, in full.
    sent_tokens = estimate_tokens(filtered, is_code)

    # Stage 2: Build prompt (XML tags, no duplication)
    # Discover the project preamble (CLAUDE.md + .claudio/project.md +
    # auto-detected stack) so codebase context lands in the cacheable prefix.
    preamble = discover_project_preamble()

    # For behavior-focused intents in a git repo, auto-include the diff
    # of work-in-progress. Usually the single most relevant context.
    git_changes = discover_git_changes(intent=intent)

    prompt = build_prompt(
        task=task,
        context=filtered,
        constraints=constraints,
        output_format=output_format,
        intent=intent,
        allow_context_request=allow_context_request,
        readonly_escalation=readonly_escalation,
        project_preamble=preamble,
        git_changes=git_changes,
    )

    final_tokens = estimate_tokens(prompt, is_code=False)

    savings_pct = (1 - sent_tokens / max(input_tokens, 1)) * 100
    metadata = {
        "filename": filename,
        "intent": intent,
        "input_tokens": input_tokens,
        "sent_tokens": sent_tokens,
        "final_tokens": final_tokens,
        "saved": f"{savings_pct:.0f}%",
    }

    return PipelineResult(
        prompt=prompt,
        input_tokens=input_tokens,
        sent_tokens=sent_tokens,
        metadata=metadata,
    )
