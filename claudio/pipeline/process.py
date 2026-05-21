"""Main processing pipeline -- orchestrates filter, compress, prompt stages."""

from claudio.pipeline.filter import filter_noise
from claudio.pipeline.compress import compress_code, compress_logs
from claudio.pipeline.prompt import build_prompt
from claudio.utils.git_context import discover_git_changes
from claudio.utils.project_context import discover_project_preamble
from claudio.utils.tokens import estimate_tokens, is_code_file


class PipelineResult:
    """Result of processing an input through the pipeline."""

    __slots__ = ("prompt", "input_tokens", "compressed_tokens", "metadata")

    def __init__(self, prompt: str, input_tokens: int, compressed_tokens: int, metadata: dict):
        self.prompt = prompt
        self.input_tokens = input_tokens
        self.compressed_tokens = compressed_tokens
        self.metadata = metadata

    @property
    def tokens_saved(self) -> int:
        return self.input_tokens - self.compressed_tokens


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
    """Run the full processing pipeline on raw input.

    Pipeline:
      1. Estimate raw tokens
      2. Filter noise (intent-aware: strips comments for refactor/review/debug)
      3. Compress (structural map for large files, log summary for logs)
      4. Build minimal XML-tagged prompt
      5. Return result with metadata
    """
    is_code = is_code_file(filename) if filename else False
    input_tokens = estimate_tokens(raw_input, is_code)

    # Stage 1: Filter noise -- pass intent so behavior-focused modes strip comments
    filtered = filter_noise(
        raw_input,
        mode="code" if is_code else "auto",
        intent=intent,
    )

    # Stage 2: Compress
    # Passing `task` lets the compressor keep the body of any symbol the
    # user names verbatim instead of leaving Claude with just a line number.
    if is_code or _looks_like_code(filtered):
        compressed = compress_code(filtered, filename, task_text=task)
    elif _looks_like_logs(filtered):
        compressed = compress_logs(filtered)
    else:
        compressed = filtered

    compressed_tokens = estimate_tokens(compressed, is_code)

    # Stage 3: Build prompt (XML tags, no duplication)
    # Discover the project preamble (CLAUDE.md + .claudio/project.md +
    # auto-detected stack) so codebase context lands in the cacheable prefix.
    preamble = discover_project_preamble()

    # For behavior-focused intents in a git repo, auto-include the diff
    # of work-in-progress. Usually the single most relevant context.
    git_changes = discover_git_changes(intent=intent)

    prompt = build_prompt(
        task=task,
        context=compressed,
        constraints=constraints,
        output_format=output_format,
        intent=intent,
        allow_context_request=allow_context_request,
        readonly_escalation=readonly_escalation,
        project_preamble=preamble,
        git_changes=git_changes,
    )

    final_tokens = estimate_tokens(prompt, is_code=False)

    savings_pct = (1 - compressed_tokens / max(input_tokens, 1)) * 100
    metadata = {
        "filename": filename,
        "intent": intent,
        "input_tokens": input_tokens,
        "compressed_tokens": compressed_tokens,
        "final_tokens": final_tokens,
        "saved": f"{savings_pct:.0f}%",
    }

    return PipelineResult(
        prompt=prompt,
        input_tokens=input_tokens,
        compressed_tokens=compressed_tokens,
        metadata=metadata,
    )


def _looks_like_code(text: str) -> bool:
    lines = text.splitlines()[:30]
    code_score = sum(
        1 for l in lines
        if any(kw in l for kw in ("def ", "class ", "function ", "import ", "const ", "let ", "var ", "func "))
    )
    return code_score >= 3


def _looks_like_logs(text: str) -> bool:
    import re
    lines = text.splitlines()[:30]
    log_score = sum(
        1 for l in lines
        if re.search(r"\b(INFO|WARN|ERROR|DEBUG)\b", l)
    )
    return log_score >= 3
