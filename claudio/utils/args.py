"""Argument parsing for @file references with line ranges.

Strict argument order: MODE -> @FILES (with -lines) -> DESCRIPTION
This order is enforced — violations produce clear errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto

from claudio.utils.files import read_file

MAX_FILE_ATTACHMENTS = 10

# Matches -XXX or -XXX-XXX (line or line range)
LINE_RANGE_RE = re.compile(r"^-(\d+)(?:-(\d+))?$")


class _Phase(Enum):
    """Parsing phases — strictly sequential."""
    MODE = auto()
    FILES = auto()
    DESCRIPTION = auto()


@dataclass
class FileAttachment:
    """A file reference with optional line range."""
    path: str
    lines: str | None = None
    content: str = ""
    # Set to True by commands/build|ask|run when claudio.session_files reports
    # that Claude has already seen this exact (path, lines, hash) tuple in
    # the current session. format_file_context emits a compact marker instead
    # of re-sending the full body.
    unchanged: bool = False


@dataclass
class ParsedArgs:
    """Result of parsing command arguments."""
    mode: str  # subtype: refactor, generate, review, question, debug
    prompt: str  # the user's description/question
    files: list[FileAttachment] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _normalize_file_flags(raw_args: list[str]) -> list[str]:
    """Rewrite `-f path` / `--file path` to `@path`.

    Why: PowerShell treats a bare `@name` as the splatting operator and
    silently drops it when `$name` is unset. `-f`/`--file` is a safe
    alternative that survives PowerShell parsing.
    """
    out: list[str] = []
    i = 0
    while i < len(raw_args):
        if raw_args[i] in ("-f", "--file") and i + 1 < len(raw_args):
            out.append("@" + raw_args[i + 1])
            i += 2
        else:
            out.append(raw_args[i])
            i += 1
    return out


def parse_command_args(raw_args: list[str], valid_modes: dict[str, str]) -> ParsedArgs:
    """Parse args after the subcommand (build/ask/run).

    Enforces strict order:  MODE -> @FILES [-lines] -> DESCRIPTION

    Examples (correct order):
        -refactor @main.py -10-25 "reduce complexity"
        -debug @server.log -100-200 "timeout error"
        -generate @models/user.py "REST endpoint"
        -question "how does auth work"
        -refactor -f main.py -10-25 "reduce complexity"    # PowerShell-safe

    Order violations produce errors:
        "some text" @file.py        -> error: @file after description
        @file.py -refactor          -> error: mode flag after @file
        "text" -refactor @file.py   -> error: mode flag after description
    """
    raw_args = _normalize_file_flags(raw_args)
    mode: str | None = None
    prompt_parts: list[str] = []
    files: list[FileAttachment] = []
    errors: list[str] = []
    last_file: FileAttachment | None = None
    phase = _Phase.MODE

    for arg in raw_args:
        # --- Mode flag: -refactor, -generate, etc. ---
        if arg.startswith("-") and arg.lstrip("-") in valid_modes:
            if phase == _Phase.FILES:
                errors.append(f"Mode flag '{arg}' must come before @file attachments. Order: mode > @files > description")
            elif phase == _Phase.DESCRIPTION:
                errors.append(f"Mode flag '{arg}' must come first. Order: mode > @files > description")
            elif mode is not None:
                errors.append(f"Multiple modes specified: -{mode} and {arg}")
            else:
                mode = valid_modes[arg.lstrip("-")]
            continue

        # --- @file reference ---
        if arg.startswith("@"):
            if phase == _Phase.DESCRIPTION:
                errors.append(f"File '{arg}' must come before the description. Order: mode > @files > description")
                continue
            phase = _Phase.FILES
            if len(files) >= MAX_FILE_ATTACHMENTS:
                errors.append(f"Maximum {MAX_FILE_ATTACHMENTS} file attachments exceeded, ignoring: {arg}")
                continue
            path = arg[1:]  # strip the @
            last_file = FileAttachment(path=path)
            files.append(last_file)
            continue

        # --- Line range: -N or -N-N (only valid right after @file) ---
        m = LINE_RANGE_RE.match(arg)
        if m and phase == _Phase.FILES and last_file is not None:
            if last_file.lines is not None:
                errors.append(f"Multiple line ranges for {last_file.path}: {last_file.lines} and {arg[1:]}")
            else:
                start = m.group(1)
                end = m.group(2)
                last_file.lines = f"{start}-{end}" if end else start
            continue

        # --- Everything else is description text ---
        if phase != _Phase.DESCRIPTION:
            phase = _Phase.DESCRIPTION
            last_file = None
        prompt_parts.append(arg)

    # Default mode if none specified
    if mode is None and valid_modes:
        default = next(iter(valid_modes.values()))
        mode = default

    prompt = " ".join(prompt_parts)

    return ParsedArgs(
        mode=mode or "",
        prompt=prompt,
        files=files,
        errors=errors,
    )


def resolve_file_attachments(files: list[FileAttachment]) -> tuple[list[FileAttachment], list[str]]:
    """Read file contents for all attachments.

    Returns:
        Tuple of (resolved files, errors).
    """
    errors: list[str] = []
    for fa in files:
        try:
            fa.content = read_file(fa.path, lines=fa.lines)
        except (FileNotFoundError, ValueError) as e:
            errors.append(str(e))
    return files, errors


def format_file_context(files: list[FileAttachment]) -> str:
    """Format file attachments as XML file tags.

    Uses <file> tags instead of markdown fences -- saves ~10 tokens per file
    and matches the format Claude parses natively for tool results.

    Each tag carries a role attribute: the first attachment is the
    `target` (what the task is about) and the rest are `context`
    (reference material). This lets Claude direct attention without
    guessing from filenames.

    Files marked `unchanged=True` (Claude has them from a prior turn in this
    session, per claudio.session_files) collapse to a self-closing marker
    so we don't pay for retransmitting bytes Claude already has.
    """
    if not files:
        return ""
    parts = []
    role_assigned = False
    for fa in files:
        if not fa.content:
            continue
        role = "target" if not role_assigned else "context"
        role_assigned = True
        lines_attr = f' lines="{fa.lines}"' if fa.lines else ""
        if fa.unchanged:
            parts.append(
                f'<file path="{fa.path}" role="{role}"{lines_attr} unchanged="true"/>'
            )
        else:
            parts.append(
                f'<file path="{fa.path}" role="{role}"{lines_attr}>\n{fa.content}\n</file>'
            )
    return "\n".join(parts)
