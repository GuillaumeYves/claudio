"""cld ask -- ask Claude a question.

Modes:
    -review     Code review (security, quality, bugs)
    -question   General question (explain, how-to, architecture)
    -debug      Debug an issue (root cause, fix, explanation)

Usage:
    cld ask -review @auth.py "check for security issues"
    cld ask -question "how does the auth middleware work"
    cld ask -debug @server.log -100-200 "why is it timing out"
"""

from claudio.pipeline.process import process
from claudio.commands.run_prompt import execute_with_tracking
from claudio.utils.args import (
    parse_command_args,
    resolve_file_attachments,
    format_file_context,
)
from claudio.utils.output import Output
from claudio.utils.tokens import format_token_info

ASK_MODES = {
    "review": "review",
    "question": "question",
    "debug": "debug",
    "rv": "review",
    "q": "question",
    "d": "debug",
}

MODE_CONFIG = {
    "review": {
        "intent": "review",
        "constraints": [
            "Flag bugs, security, code smells",
            "Rank by severity",
            "Give fixes",
        ],
        "output_format": "[severity] issue + fix, then one-line verdict",
        "task_prefix": "Review",
    },
    "question": {
        "intent": "general",
        "constraints": None,
        "output_format": None,
        "task_prefix": "",
    },
    "debug": {
        "intent": "debug",
        "constraints": [
            "Root cause first",
            "Concrete fix",
            "Rank if ambiguous",
        ],
        "output_format": "root cause, fix (diff), explanation",
        "task_prefix": "Debug",
    },
}


def execute(raw_args: list[str], ctx: dict) -> int:
    out = Output(json_mode=ctx["json_output"], verbose=ctx["verbose"])

    if not raw_args:
        out.error("Usage: cld ask -review|-question|-debug @file ... \"query\"")
        return 1

    parsed = parse_command_args(raw_args, ASK_MODES)

    for err in parsed.errors:
        out.warn(err)

    if not parsed.prompt and not parsed.files:
        out.error("Provide a question and/or @file attachments")
        return 1

    if parsed.mode not in MODE_CONFIG:
        out.error(f"Unknown ask mode: {parsed.mode}. Use -review, -question, or -debug")
        return 1

    if parsed.files:
        _, file_errors = resolve_file_attachments(parsed.files)
        for err in file_errors:
            out.error(err)
            return 1

    config = MODE_CONFIG[parsed.mode]

    if config["task_prefix"]:
        task = f"{config['task_prefix']}: {parsed.prompt}" if parsed.prompt else config["task_prefix"]
    else:
        task = parsed.prompt

    file_context = format_file_context(parsed.files)

    result = process(
        raw_input=file_context,
        task=task,
        intent=config["intent"],
        filename=parsed.files[0].path if parsed.files else "",
        constraints=config["constraints"],
        output_format=config["output_format"],
    )

    if ctx["verbose"]:
        out.info(format_token_info(result.compressed_tokens))
        if result.tokens_saved > 0:
            out.info(f"Saved ~{result.tokens_saved:,} tokens via compression")

    execute_with_tracking(
        prompt=result.prompt,
        ctx=ctx,
        out=out,
        cmd="ask",
        mode=parsed.mode,
        metadata=result.metadata,
    )
    return 0
