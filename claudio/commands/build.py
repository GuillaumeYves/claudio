"""cld build -- create or modify code.

Modes:
    -refactor   Refactor existing code (preserve behavior, improve structure)
    -generate   Generate new code from a description

Usage:
    cld build -refactor @main.py -10-25 "reduce complexity"
    cld build -generate @models/user.py "REST endpoint for users"
"""

from claudio.pipeline.process import process
from claudio.commands.run_prompt import execute_with_tracking, parse_need_context
from claudio.utils.args import (
    parse_command_args,
    resolve_file_attachments,
    format_file_context,
)
from claudio.utils.output import Output
from claudio.utils.tokens import format_token_info

BUILD_MODES = {
    "refactor": "refactor",
    "generate": "generate",
    "r": "refactor",
    "g": "generate",
}

MODE_CONFIG = {
    "refactor": {
        "intent": "refactor",
        "constraints": [
            "Preserve behavior",
            "Output unified diff",
            "One-line reason per change",
        ],
        "output_format": "diff with explanation",
        "task_prefix": "Refactor",
    },
    "generate": {
        "intent": "generate",
        "constraints": [
            "Production-ready",
            "Match conventions in context",
            "Minimal imports",
        ],
        "output_format": "complete code block",
        "task_prefix": "Generate",
    },
}


def execute(raw_args: list[str], ctx: dict) -> int:
    out = Output(json_mode=ctx["json_output"], verbose=ctx["verbose"])

    if not raw_args:
        out.error("Usage: cld build -refactor|-generate @file ... \"description\"")
        return 1

    parsed = parse_command_args(raw_args, BUILD_MODES)

    for err in parsed.errors:
        out.warn(err)

    if not parsed.prompt and not parsed.files:
        out.error("Provide a description and/or @file attachments")
        return 1

    if parsed.mode not in MODE_CONFIG:
        out.error(f"Unknown build mode: {parsed.mode}. Use -refactor or -generate")
        return 1

    if parsed.files:
        _, file_errors = resolve_file_attachments(parsed.files)
        for err in file_errors:
            out.error(err)
            return 1

    config = MODE_CONFIG[parsed.mode]
    task = f"{config['task_prefix']}: {parsed.prompt}" if parsed.prompt else config["task_prefix"]

    allow_feedback = bool(ctx.get("feedback") and parsed.files)

    response = _process_and_execute(
        files=parsed.files,
        task=task,
        mode=parsed.mode,
        config=config,
        ctx=ctx,
        out=out,
        allow_feedback=allow_feedback,
    )

    if allow_feedback and response:
        need = parse_need_context(response)
        if need:
            path, lines, reason = need
            if _expand_file_range(parsed.files, path, lines):
                out.info(f"[claudio] Claude requested more context: {path} lines {lines}"
                         f"{' — ' + reason if reason else ''}. Retrying.")
                _process_and_execute(
                    files=parsed.files,
                    task=task,
                    mode=parsed.mode,
                    config=config,
                    ctx={**ctx, "no_cache": True},
                    out=out,
                    allow_feedback=False,
                )

    return 0


def _process_and_execute(files, task, mode, config, ctx, out, allow_feedback):
    file_context = format_file_context(files)

    result = process(
        raw_input=file_context,
        task=task,
        intent=config["intent"],
        filename=files[0].path if files else "",
        constraints=config["constraints"],
        output_format=config["output_format"],
        allow_context_request=allow_feedback,
    )

    if ctx["verbose"]:
        out.info(format_token_info(result.compressed_tokens))
        if result.tokens_saved > 0:
            out.info(f"Saved ~{result.tokens_saved:,} tokens via compression")

    return execute_with_tracking(
        prompt=result.prompt,
        ctx=ctx,
        out=out,
        cmd="build",
        mode=mode,
        intent=config["intent"],
        metadata=result.metadata,
    )


def _expand_file_range(files, requested_path: str, requested_lines: str) -> bool:
    from claudio.utils.args import resolve_file_attachments

    for fa in files:
        if fa.path == requested_path or fa.path.endswith(requested_path):
            fa.lines = requested_lines
            fa.content = ""
            resolve_file_attachments([fa])
            return bool(fa.content)
    return False
