"""claudio build -- create or modify code.

Modes:
    -refactor   Refactor existing code (preserve behavior, improve structure)
    -generate   Generate new code from a description

Usage:
    claudio build -refactor @main.py -10-25 "reduce complexity"
    claudio build -generate @models/user.py "REST endpoint for users"
"""

import os
import subprocess
import sys

from claudio import session_files
from claudio.config import permission_posture, posture_permission_mode
from claudio.pipeline.process import process
from claudio.commands.run_prompt import (
    _MUTATING_PERMISSION_MODES,
    collect_clarification_answer,
    execute_with_tracking,
    parse_need_clarification,
    parse_need_context,
)
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

# Mode constraints are deliberately terse. Two recurring rules pay off in
# every mode and are appended automatically below:
#   - "No preamble" — kills "Sure, here's...", "I'll help you...", etc.
#   - "Stop after the artifact" — no trailing summary / restatement of what
#     was changed. The user can `ask -q` for elaboration; in REPL auto-chain
#     mode that follow-up is essentially free.
# Build is the "actually change the code" verb: unlike ask (read-only), these
# modes instruct Claude to apply edits to disk with its Edit/Write tools rather
# than print a diff. The write only lands when claudio also grants a mutating
# permission mode (see _build_permission_mode); without it, headless `claude`
# auto-denies the edit and you'd get narration but no change.
MODE_CONFIG = {
    "refactor": {
        "intent": "refactor",
        "constraints": [
            "Preserve behavior",
            "Apply the changes directly to the file(s) with your Edit tool",
            "Keep edits minimal and focused",
        ],
        "output_format": "after editing, a <=2 line summary of what changed",
        "task_prefix": "Refactor",
    },
    "generate": {
        "intent": "generate",
        "constraints": [
            "Production-ready code only",
            "Match conventions in context",
            "Write the code to the appropriate file(s); create them if missing",
            "Minimal imports",
        ],
        "output_format": "after writing, a <=2 line summary of the files created/changed",
        "task_prefix": "Generate",
    },
}

# Universal terseness rules — applied to every build mode.
_TERSE_RULES = ["No preamble", "Stop after the artifact"]


def _with_terseness(constraints: list[str] | None) -> list[str]:
    base = list(constraints) if constraints else []
    return base + _TERSE_RULES


def execute(raw_args: list[str], ctx: dict) -> int:
    out = Output(json_mode=ctx["json_output"], verbose=ctx["verbose"])

    if not raw_args:
        out.error("Usage: claudio build -refactor|-generate @file ... \"description\"")
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
    permission_mode = _build_permission_mode(ctx)

    # "confirm" posture: one Y/n gate before we apply anything.
    if not _confirm_build_if_needed(parsed.files, parsed.prompt, ctx, out):
        return 0

    response = _process_and_execute(
        files=parsed.files,
        task=task,
        mode=parsed.mode,
        config=config,
        ctx=ctx,
        out=out,
        allow_feedback=allow_feedback,
        permission_mode=permission_mode,
    )

    # Two-way feedback signals (gated by --feedback). Mutually exclusive:
    #   <need-clarification>  task is ambiguous -> ask the dev
    #   <need-context>        data is missing   -> expand file ranges
    if allow_feedback and response:
        question = parse_need_clarification(response)
        if question:
            answer = collect_clarification_answer(question, out)
            if answer:
                enriched_task = f"{task}\n\n[clarification] {question}\n[answer] {answer}"
                _process_and_execute(
                    files=parsed.files,
                    task=enriched_task,
                    mode=parsed.mode,
                    config=config,
                    ctx={**ctx, "no_cache": True},
                    out=out,
                    allow_feedback=False,
                    permission_mode=permission_mode,
                )
            _show_applied_diff(parsed.files, ctx, out, permission_mode)
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
                    mode=parsed.mode,
                    config=config,
                    ctx={**ctx, "no_cache": True},
                    out=out,
                    allow_feedback=False,
                    permission_mode=permission_mode,
                )

    _show_applied_diff(parsed.files, ctx, out, permission_mode)
    return 0


def _build_permission_mode(ctx: dict) -> str | None:
    """Resolve the CLI permission mode for this build.

    Precedence: dry-run (None — nothing to apply) > CLAUDIO_BUILD_PERMISSION_MODE
    env (power-user / test override; "default"/"" means preview-only) > the
    configured permission posture (see config.PERMISSION_POSTURES, set by the
    setup wizard).
    """
    if ctx.get("dry_run"):
        return None
    mode = os.environ.get("CLAUDIO_BUILD_PERMISSION_MODE")
    if mode is not None:
        mode = mode.strip()
        if not mode or mode == "default":
            return None
        return mode
    return posture_permission_mode()


def _confirm_build_if_needed(files, user_prompt, ctx, out) -> bool:
    """Honor the 'confirm' posture: ask once before a build applies edits.

    Returns True to proceed, False to abort. Only gates interactive, non-dry,
    non-json builds under the 'confirm' posture; every other posture (and any
    non-TTY / piped / dry-run invocation) proceeds untouched. This is a coarse
    per-invocation gate, not a per-edit popup — the user opted into it knowing
    headless claudio can't prompt mid-run.
    """
    if ctx.get("dry_run") or ctx.get("json_output") or not sys.stdin.isatty():
        return True
    if permission_posture() != "confirm":
        return True
    targets = ", ".join(fa.path for fa in files) if files else "the relevant file(s)"
    out.info(f"Build will edit {targets} to: {user_prompt or '(see task)'}")
    try:
        sys.stderr.write("Proceed? [Y/n] ")
        sys.stderr.flush()
        answer = sys.stdin.readline().strip().lower()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return False
    if answer in ("", "y", "yes"):
        return True
    out.info("[claudio] Build cancelled.")
    return False


def _process_and_execute(files, task, mode, config, ctx, out, allow_feedback,
                         permission_mode=None):
    # Mark which files Claude has already seen this session so we can swap
    # in a compact <file unchanged="true"/> marker instead of re-sending
    # the full body. Uses --session-id or --resume from ctx as the key.
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
        constraints=_with_terseness(config["constraints"]),
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
        permission_mode=permission_mode,
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


def _show_applied_diff(files, ctx: dict, out, permission_mode) -> None:
    """Print the git diff of the attached files after Claude edits them.

    Build now applies edits via tools instead of printing a diff, so the
    stream only shows `editing foo.py` breadcrumbs. To keep the old "here's
    what changed" transparency we replay the actual on-disk diff once the run
    finishes. Best-effort: skipped in dry-run / JSON / preview modes and when
    git isn't available. Written to stderr so piped stdout stays clean.
    """
    from claudio.utils.colors import colors_enabled

    if permission_mode not in _MUTATING_PERMISSION_MODES:
        return
    if ctx.get("dry_run") or ctx.get("json_output"):
        return
    paths = [fa.path for fa in files] if files else []
    if not paths:
        return

    use_color = colors_enabled(sys.stderr)
    args = ["git"]
    if use_color:
        args += ["-c", "color.ui=always"]
    args += ["diff", "--", *paths]
    try:
        result = subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return
    diff = (result.stdout or "").strip() if result.returncode == 0 else ""
    if not diff:
        return
    out.success("applied changes:")
    try:
        print(diff, file=sys.stderr)
    except UnicodeEncodeError:
        print(diff.encode("utf-8", "replace").decode("ascii", "replace"), file=sys.stderr)
