"""claudio ask -- ask Claude a question.

Modes:
    -review     Code review (security, quality, bugs)
    -question   General question (explain, how-to, architecture)
    -debug      Debug an issue (root cause, fix, explanation)

Usage:
    claudio ask -review @auth.py "check for security issues"
    claudio ask -question "how does the auth middleware work"
    claudio ask -debug @server.log -100-200 "why is it timing out"
"""

import sys

from claudio import session_files
from claudio.pipeline.process import process
from claudio.commands.run_prompt import (
    collect_clarification_answer,
    execute_with_tracking,
    parse_need_clarification,
    parse_need_context,
    parse_needs_build,
)
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
            "One issue per bullet, <=2 lines: `[severity] file:line - issue -> fix`",
            "Ranked highest severity first",
            "Skip nits unless asked",
        ],
        "output_format": "bullets only; one-line verdict at the end",
        "task_prefix": "Review",
    },
    "question": {
        # Exploratory mode — no constraints. Free-form answers are the point.
        "intent": "general",
        "constraints": None,
        "output_format": None,
        "task_prefix": "",
    },
    "debug": {
        "intent": "debug",
        "constraints": [
            "Cause: <=1 sentence",
            "Fix: minimal diff",
            "Why: <=2 sentences",
        ],
        "output_format": "three labeled blocks: Cause / Fix / Why",
        "task_prefix": "Debug",
    },
}

# Terseness rules added to every mode except `question`, which is meant to
# be expansive. -question users want explanations; -review and -debug users
# want artifacts.
_TERSE_RULES = ["No preamble", "Stop after the artifact"]


def _with_terseness(intent: str, constraints: list[str] | None) -> list[str] | None:
    if intent == "general":
        return constraints  # leave -question alone
    base = list(constraints) if constraints else []
    return base + _TERSE_RULES


def execute(raw_args: list[str], ctx: dict) -> int:
    out = Output(json_mode=ctx["json_output"], verbose=ctx["verbose"])

    if not raw_args:
        out.error("Usage: claudio ask -review|-question|-debug @file ... \"query\"")
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

    # Feedback channel: only meaningful when files are attached (context may
    # have been compressed). Opt-in via --feedback.
    allow_feedback = bool(ctx.get("feedback") and parsed.files)

    response = _process_and_execute(
        files=parsed.files,
        task=task,
        config=config,
        ctx=ctx,
        out=out,
        allow_feedback=allow_feedback,
    )

    # Read-only -> build escalation. Not gated by --feedback: ask can never
    # mutate, so if Claude judged the request needs a write/build step it
    # emits <needs-build/> instead of silently attempting (and being denied).
    # We offer to re-run in build mode, resuming this same session so the
    # analysis just produced carries straight into the build.
    if response:
        needs = parse_needs_build(response)
        if needs:
            _offer_build_switch(needs[0], needs[1], parsed.files, parsed.prompt, ctx, out)
            return 0

    # Two-way feedback signals (gated by --feedback). Mutually exclusive:
    #   <need-clarification>  task is ambiguous -> ask the dev
    #   <need-context>        data is missing   -> expand file ranges
    # Both honor a single retry; no loops.
    if allow_feedback and response:
        question = parse_need_clarification(response)
        if question:
            answer = collect_clarification_answer(question, out)
            if answer:
                enriched_task = f"{task}\n\n[clarification] {question}\n[answer] {answer}"
                _process_and_execute(
                    files=parsed.files,
                    task=enriched_task,
                    config=config,
                    ctx={**ctx, "no_cache": True},
                    out=out,
                    allow_feedback=False,
                )
            return 0

        needs = parse_need_context(response)
        if needs:
            expanded = []
            for path, lines, reason in needs:
                if _expand_file_range(parsed.files, path, lines):
                    expanded.append(f"{path} lines {lines}" + (f" — {reason}" if reason else ""))
            if expanded:
                out.info("[claudio] Claude requested more context: "
                         + "; ".join(expanded) + ". Retrying.")
                _process_and_execute(
                    files=parsed.files,
                    task=task,
                    config=config,
                    ctx={**ctx, "no_cache": True},
                    out=out,
                    allow_feedback=False,
                )

    return 0


def _process_and_execute(files, task, config, ctx, out, allow_feedback):
    """Single process+execute pass. Extracted so retry can call it again."""
    # Per-session file dedup — Claude already has unchanged @files from
    # prior turns; substitute a marker instead of re-sending bytes.
    session_id = ctx.get("session_id") or ctx.get("resume")
    if files and session_id:
        unchanged = session_files.mark_files_seen(session_id, files)
        for fa in files:
            if (fa.path, fa.lines) in unchanged:
                fa.unchanged = True

    file_context = format_file_context(files)

    result = process(
        raw_input=file_context,
        task=task,
        intent=config["intent"],
        filename=files[0].path if files else "",
        constraints=_with_terseness(config["intent"], config["constraints"]),
        output_format=config["output_format"],
        allow_context_request=allow_feedback,
        # ask is always read-only — let Claude escalate to build mode instead
        # of attempting a write the headless CLI would silently deny.
        readonly_escalation=True,
    )

    if ctx["verbose"]:
        out.info(format_token_info(result.compressed_tokens))
        if result.tokens_saved > 0:
            out.info(f"Saved ~{result.tokens_saved:,} tokens via compression")

    return execute_with_tracking(
        prompt=result.prompt,
        ctx=ctx,
        out=out,
        cmd="ask",
        mode=_mode_for(config),
        intent=config["intent"],
        metadata=result.metadata,
    )


def _offer_build_switch(mode, reason, files, user_prompt, ctx, out):
    """Claude (read-only) signalled the request needs build mode — mediate it.

    Interactive TTY: explain why, then prompt to re-run in build mode now. On
    yes we call build's own execute path with the *same* FileAttachment objects
    (line ranges intact) and resume the session ask just used, so the analysis
    carries over and the edit actually lands. build's acceptEdits already
    bypasses the response cache, so the write isn't replayed from a stored echo.

    Non-interactive or --json: print the equivalent `build` command and stop —
    there's no human to confirm an auto-switch in a pipe or CI run.
    """
    label = "generate" if mode == "generate" else "refactor"
    flag = "-g" if label == "generate" else "-r"

    if reason:
        out.warn(f"This needs build mode: {reason}")
    else:
        out.warn("This needs build mode (it would create or modify files).")

    if ctx.get("json_output") or not sys.stdin.isatty():
        files_part = (" ".join(fa.path for fa in files) + " ") if files else ""
        out.info(f'[claudio] Re-run in build mode:  build {flag} {files_part}"{user_prompt}"')
        return

    try:
        sys.stderr.write(f"Re-run in build mode ({label}) now? [Y/n] ")
        sys.stderr.flush()
        answer = sys.stdin.readline().strip().lower()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return
    if answer not in ("", "y", "yes"):
        out.info("[claudio] Staying in ask mode.")
        return

    # Lazy import avoids a build <-> ask import cycle at module load.
    from claudio.commands import build as build_cmd

    config = build_cmd.MODE_CONFIG[label]
    build_task = (
        f"{config['task_prefix']}: {user_prompt}" if user_prompt else config["task_prefix"]
    )
    # Resume whatever session ask used (it created/continued one on the Claude
    # side), so the build turn inherits the analysis. session_id is cleared so
    # we --resume rather than try to re-create an existing session.
    build_ctx = {
        **ctx,
        "resume": ctx.get("resume") or ctx.get("session_id"),
        "session_id": None,
    }
    permission_mode = build_cmd._build_permission_mode(build_ctx)
    build_cmd._process_and_execute(
        files=files,
        task=build_task,
        mode=label,
        config=config,
        ctx=build_ctx,
        out=out,
        allow_feedback=False,
        permission_mode=permission_mode,
    )


def _mode_for(config: dict) -> str:
    """Map MODE_CONFIG entry back to the short mode name used for stats."""
    for short, full in ASK_MODES.items():
        if short == full and MODE_CONFIG[full] is config:
            return full
    return config.get("intent", "question")


def _expand_file_range(files, requested_path: str, requested_lines: str) -> bool:
    """Find the matching FileAttachment and expand its line range.

    Returns True if the attachment was found and re-read.
    """
    from claudio.utils.args import resolve_file_attachments

    for fa in files:
        if fa.path == requested_path or fa.path.endswith(requested_path):
            fa.lines = requested_lines
            fa.content = ""  # force re-read
            resolve_file_attachments([fa])
            return bool(fa.content)
    return False
