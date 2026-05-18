"""Unified `claudio` entry point.

Running bare `claudio` drops you *inside* the tool — type commands
(build / ask / run / stats) directly, no prefix needed. `@<prefix>`
triggers live file/folder completion, and slash commands handle meta
operations (/help, /model, /mode, /cwd, /fresh, /session, /clear).

The REPL is mode-sticky: after an explicit `ask -review @auth.py "..."`,
the prompt becomes `claudio [ask -review]>` and subsequent bare lines
inherit the command, mode, and @file set. Use /mode to pin without
sending, /fresh to wipe.

Running `claudio <subcommand> …` is the single-shot form, for scripts
and one-liners. Both paths share the same command router (`cli.main`).

`prompt_toolkit` ships as a required dependency of `claudio-cli`.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path

from claudio import __version__, session_files
from claudio.utils.colors import (
    BOLD, CYAN, DIM, GREY, RESET, colors_enabled,
)
from claudio.utils.update_check import pending_notice, start_background_check


_LOGO_LINES = (
    "█▀▀ █   ▄▀█ █ █ █▀▄ █ █▀█",
    "█▄▄ █▄▄ █▀█ █▄█ █▄▀ █ █▄█",
)

HELP_TEXT = """\
Commands:
    build -r|-g [@file...] description    Create or modify code
    ask   -q|-rv|-d [@file...] question   Ask, review, or debug
    run [--agentic]                       Execute claudio-task.json
    stats [--reset]                       Show usage & cost tracking

Quoting in the REPL:
  Quotes are optional. The structural part of the line is read first
  (command, flags, @files, line ranges); everything after that is your
  prompt, taken verbatim to end-of-line — apostrophes, @ symbols, even
  --flag-looking text all pass through.

    ask -q i'm not sure if A or B is better, what do you think?
    build -r @main.py reduce nesting where it's hard to read
    ask -q what does the @ symbol mean in python?
    ask -q how do you decide between --verbose and --debug logging?

  Use quotes only when you want to *emphasise* something inside the prompt:

    ask -q do you think "ruby vs php" is still relevant in 2026?

  If the whole prompt happens to be wrapped in matching outer quotes, those
  are stripped — the traditional shell form still works, but it isn't
  required for @, --, or apostrophes.

Slash commands:
    /help            Show this help
    /model NAME      Set model for this session (haiku|sonnet|opus)
    /mode CMD MODE   Pin a sticky mode (e.g. `/mode ask -review`); /mode
                     alone shows current, /mode none clears it
    /cwd [PATH]      Show or change working directory
    /clear           Clear the screen
    /fresh           Drop the current conversation and start a new session
    /session         Show the current session id
    /exit | /quit    Exit (Ctrl-D also works)

Sticky mode + files:
  After your first `ask -review @auth.py "..."`, the prompt becomes
  `claudio [ask -review]>` and follow-up lines that don't start with a
  command inherit both the mode and the @file set:

    claudio [ask -review]> what about input validation?
    claudio [ask -review]> any token-replay risk?

  Switch modes with a fresh command (`ask -q ...`, `build -r ...`), or
  /mode to pin without sending. New @files in a line replace the prior
  set; bare prompts re-attach the previous ones.

The REPL auto-chains: every ask/build/run in this session resumes the
same Claude conversation, so context (and Anthropic's prompt cache) is
preserved across turns. Use /fresh to wipe and start over.

File references:
    @path/to/file    Tab-completes from current directory
    -N or -N-N       Line range for the preceding @file

Global flags:
    --dry-run --no-cache --verbose --json
    --model NAME --feedback --agentic
"""

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
             "dist", "build", ".tox", ".mypy_cache", ".pytest_cache"}

# Static completion vocabularies — duplicated from cli.py / commands intentionally
# so the completer doesn't pull in the dispatcher just to introspect modes.
_COMMANDS = {
    "build": "Create or modify code",
    "ask": "Ask Claude a question",
    "run": "Execute claudio-task.json",
    "stats": "Token usage and cost tracking",
    "setup": "Configure PATH and verify installation",
}

_BUILD_MODES = {
    "-refactor": "Refactor existing code",
    "-r": "Refactor (short)",
    "-generate": "Generate new code",
    "-g": "Generate (short)",
}

_ASK_MODES = {
    "-review": "Code review",
    "-rv": "Review (short)",
    "-question": "General question",
    "-q": "Question (short)",
    "-debug": "Debug an issue",
    "-d": "Debug (short)",
}

_GLOBAL_FLAGS = {
    "--dry-run": "Show optimized prompt without sending",
    "--no-cache": "Bypass response cache",
    "--verbose": "Show token estimates and details",
    "--json": "Output in JSON format",
    "--model": "Override model",
    "--feedback": "Allow Claude to request more context",
    "--agentic": "Run plan in one agentic session",
}

_SLASH_COMMANDS = {
    "/help": "Show commands",
    "/model": "Set session model",
    "/mode": "Show / pin / clear the sticky command+mode",
    "/cwd": "Show or change directory",
    "/clear": "Clear the screen",
    "/stats": "Token usage and cost",
    "/fresh": "Start a new conversation (drops Claude's memory of this one)",
    "/session": "Print the current session id",
    "/exit": "Exit the REPL",
    "/quit": "Exit the REPL",
}

_MODEL_NAMES = {
    "haiku": "Fast, cheap",
    "sonnet": "Balanced (default)",
    "opus": "Most capable",
    "auto": "Reset to auto-route",
}

_AT_PATTERN = re.compile(r"@([^\s@]*)$")


def iter_at_completions(text_before_cursor: str):
    """Yield (text, display, meta, token_len) for an @<prefix> in the input.

    This is the core of the REPL's file completer, extracted so it can be
    unit-tested without instantiating prompt_toolkit. Returns an empty
    iterator if the text does not end in an `@<prefix>` token.
    """
    m = _AT_PATTERN.search(text_before_cursor)
    if not m:
        return
    token = m.group(0)
    prefix = m.group(1)

    try:
        cwd = Path.cwd()
        if prefix.endswith("/") or prefix.endswith("\\"):
            # `@src/` means "show me what's inside src/", not "filter cwd by
            # names starting with src". Descend into the directory.
            search_dir = cwd / prefix.rstrip("/\\")
            base = ""
        elif "/" in prefix or "\\" in prefix:
            prefix_path = Path(prefix)
            search_dir = cwd / prefix_path.parent
            base = prefix_path.name
        else:
            search_dir = cwd
            base = prefix

        if not search_dir.exists() or not search_dir.is_dir():
            return

        base_lower = base.lower()
        entries = sorted(search_dir.iterdir(),
                         key=lambda p: (not p.is_dir(), p.name.lower()))
        for item in entries:
            if item.name in SKIP_DIRS:
                continue
            if not base and item.name.startswith("."):
                continue
            if base_lower and not item.name.lower().startswith(base_lower):
                continue
            try:
                rel = item.relative_to(cwd).as_posix()
            except ValueError:
                rel = item.name
            display = rel + ("/" if item.is_dir() else "")
            yield (f"@{display}", display, "dir" if item.is_dir() else "file", len(token))
    except (OSError, PermissionError):
        return


def iter_command_completions(text_before_cursor: str):
    """Yield (text, display, meta, token_len) for non-@ tokens.

    Handles four cases (in order):
      1. Slash commands at line start (/he -> /help)
      2. Model names after `/model `
      3. Top-level commands at line start (bui -> build)
      4. Modes after a recognized command (build -r ...)
    """
    text = text_before_cursor
    stripped_left = text.lstrip()
    leading_ws = len(text) - len(stripped_left)

    # 1. Slash command at line start.
    if stripped_left.startswith("/") and " " not in stripped_left:
        prefix = stripped_left  # includes the leading `/`
        for cmd, meta in _SLASH_COMMANDS.items():
            if cmd.startswith(prefix):
                yield (cmd, cmd, meta, len(prefix))
        return

    # 2. /model <NAME>
    if stripped_left.startswith("/model "):
        last_token = _last_token(text)
        for name, meta in _MODEL_NAMES.items():
            if name.startswith(last_token):
                yield (name, name, meta, len(last_token))
        return

    tokens = stripped_left.split()
    last_token = _last_token(text)
    cursor_in_token = bool(text) and not text[-1].isspace()

    # 3. Top-level command at start of line.
    if not tokens or (len(tokens) == 1 and cursor_in_token):
        for cmd, meta in _COMMANDS.items():
            if cmd.startswith(last_token):
                yield (cmd, cmd, meta, len(last_token))
        return

    # 4. Modes / global flags after a recognized command.
    cmd = tokens[0].lower()
    if cmd not in _COMMANDS:
        return
    if not last_token.startswith("-"):
        return

    candidates: dict[str, str] = {}
    if cmd == "build":
        candidates.update(_BUILD_MODES)
    elif cmd == "ask":
        candidates.update(_ASK_MODES)
    candidates.update(_GLOBAL_FLAGS)

    for flag, meta in candidates.items():
        if flag.startswith(last_token):
            yield (flag, flag, meta, len(last_token))


def _last_token(text: str) -> str:
    """Return the partial token under the cursor (text after the last space)."""
    if not text:
        return ""
    if text[-1].isspace():
        return ""
    return text.rsplit(None, 1)[-1] if " " in text else text.lstrip()


# Subcommands handled by cli.main in single-shot mode.
_CLI_SUBCOMMANDS = {"build", "ask", "run", "stats", "setup"}

# Commands whose mode flag is worth remembering as a sticky default. `run`
# reads from claudio-task.json and `stats`/`setup` are one-shots, so only
# `ask` and `build` participate in sticky-mode behavior.
_STICKY_COMMANDS = {"ask", "build"}


def apply_sticky(
    line: str,
    sticky_mode: tuple[str, str] | None,
    sticky_files: list[str],
) -> tuple[str, bool]:
    """Prefix a bare REPL line with the current sticky command + mode + files.

    Returns (adjusted_line, used_default). `used_default` is True when no
    sticky mode existed and we fell back to `ask -q` — the caller uses that
    to emit a one-time onboarding hint.

    A line whose first whitespace-delimited token is itself a known command
    is returned unchanged: explicit commands always win and *update* sticky
    state via `extract_sticky` after dispatch.
    """
    stripped = line.strip()
    if not stripped:
        return line, False

    # Slash commands are meta-operations; sticky never applies.
    if stripped.startswith("/"):
        return line, False

    first_word = stripped.split(None, 1)[0].lower()
    if first_word in _COMMANDS:
        return line, False

    # Bare prompt: synthesise an implicit command line.
    used_default = sticky_mode is None
    if sticky_mode is None:
        cmd, mode_flag = "ask", "-q"
    else:
        cmd, mode_flag = sticky_mode

    # Re-attach the previous turn's files only when the user didn't include
    # any new @-tokens this turn. Mixing the two would inflate context with
    # files that may no longer be relevant.
    has_new_at = any(
        tok.startswith("@") for tok in stripped.split()
    )
    if sticky_files and not has_new_at:
        files_part = " ".join(sticky_files) + " "
    else:
        files_part = ""

    adjusted = f"{cmd} {mode_flag} {files_part}{stripped}"
    return adjusted, used_default


def extract_sticky(argv: list[str]) -> tuple[tuple[str, str] | None, list[str]]:
    """Pull (command, mode_flag) + file tokens from a parsed argv.

    Returns (None, []) when argv isn't an ask/build invocation (other
    commands don't participate in stickiness). Line ranges (`-10-25`) are
    kept attached to their preceding @file in the returned list.
    """
    if not argv:
        return None, []
    cmd = argv[0].lower()
    if cmd not in _STICKY_COMMANDS:
        return None, []

    mode_flag: str | None = None
    files: list[str] = []
    prev_was_at = False
    for tok in argv[1:]:
        if tok in _MODES:
            mode_flag = tok
            prev_was_at = False
        elif tok.startswith("@"):
            files.append(tok)
            prev_was_at = True
        elif _LINE_RANGE_RE.match(tok) and prev_was_at:
            files.append(tok)
            prev_was_at = False
        else:
            prev_was_at = False

    if mode_flag is None:
        return None, files
    return (cmd, mode_flag), files


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Version flag short-circuits before any dispatch.
    if argv and argv[0] in ("-v", "--version"):
        print(f"claudio {__version__}")
        return 0

    # Anything that looks like a single-shot invocation (subcommand,
    # --completions, --help) routes straight to the cli dispatcher.
    if argv and (
        argv[0] in _CLI_SUBCOMMANDS
        or argv[0] in ("-h", "--help", "help", "--completions")
    ):
        from claudio.cli import main as cli_main
        return cli_main(argv)

    # No args -> interactive REPL.
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.shortcuts import CompleteStyle
    except ImportError:
        # prompt_toolkit is a required dependency; this only trips in
        # broken installs (manual removal, stale venv).
        sys.stderr.write(
            "[claudio] prompt_toolkit is missing from this environment.\n"
            "Reinstall:  pip install --force-reinstall claudio-cli\n"
        )
        return 1

    if argv:
        sys.stderr.write(
            f"[claudio] unknown argument: {argv[0]!r}. "
            "Run `claudio --help` for usage.\n"
        )
        return 2

    if not sys.stdin.isatty():
        sys.stderr.write(
            "[claudio] Interactive mode needs a TTY. "
            "Pass a subcommand for non-interactive use, e.g. "
            "`claudio build -r @file.py \"...\"`.\n"
        )
        return 1

    class ClaudioCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor

            # @file completions take precedence — if we're inside an @ token
            # the user explicitly opted in to file mode.
            had_at = False
            for entry in iter_at_completions(text):
                had_at = True
                comp_text, display, meta, token_len = entry
                yield Completion(
                    text=comp_text,
                    start_position=-token_len,
                    display=display,
                    display_meta=meta,
                )
            if had_at:
                return

            for comp_text, display, meta, token_len in iter_command_completions(text):
                yield Completion(
                    text=comp_text,
                    start_position=-token_len,
                    display=display,
                    display_meta=meta,
                )

    history_path = _history_file()
    history: FileHistory | None
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(history_path))
    except (OSError, PermissionError) as e:
        sys.stderr.write(f"[claudio] warning: history disabled ({e})\n")
        history = None

    session = PromptSession(
        history=history,
        completer=ClaudioCompleter(),
        complete_while_typing=True,
        complete_style=CompleteStyle.MULTI_COLUMN,
    )

    # Kick off the PyPI version check before we print anything; the daemon
    # thread races the banner so the cache is usually warm by the second run.
    start_background_check()

    _print_banner()
    _print_update_notice()

    session_model: str | None = None
    # Auto-chain: every command in this REPL run shares one Anthropic session
    # so context accumulates naturally and the prompt cache stays warm. The
    # first dispatch uses --session-id (creates the session with our UUID);
    # all subsequent dispatches use --resume (continues it).
    session_id = str(uuid.uuid4())
    session_turn = 0

    # Sticky mode / files: a bare follow-up prompt inherits the previous
    # turn's (command, mode_flag) and @-file set. Explicit commands replace
    # the sticky state; /fresh wipes it. First bare prompt with no sticky
    # falls back to `ask -q` plus a one-time onboarding hint.
    sticky_mode: tuple[str, str] | None = None
    sticky_files: list[str] = []
    sticky_hint_shown = False

    while True:
        try:
            line = session.prompt(ANSI(_render_prompt(sticky_mode)))
        except KeyboardInterrupt:
            continue
        except EOFError:
            sys.stdout.write("\n")
            break

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            action = _handle_slash(line, session_model, session_id, sticky_mode)
            if action == "exit":
                break
            if isinstance(action, tuple) and action[0] == "model":
                session_model = action[1]
                print(f"[claudio] model set to: {session_model or '(auto-route)'}")
            elif isinstance(action, tuple) and action[0] == "mode":
                # /mode set/clear from the user — overrides sticky directly.
                sticky_mode = action[1]
                sticky_files = []
                if sticky_mode is None:
                    print("[claudio] mode cleared")
                else:
                    print(f"[claudio] mode set to: {sticky_mode[0]} {sticky_mode[1]}")
            elif action == "fresh":
                # Drop the file-memory for the old session too — keeps disk
                # tidy and avoids leaking the previous conversation's hash
                # set into any subsequent /session display.
                session_files.clear(session_id)
                session_id = str(uuid.uuid4())
                session_turn = 0
                sticky_mode = None
                sticky_files = []
                sticky_hint_shown = False
                print(f"[claudio] new session: {session_id}")
            continue

        # Apply sticky preprocessing: bare follow-ups inherit the previous
        # command + mode + files. Explicit commands pass through unchanged.
        adjusted_line, used_default = apply_sticky(line, sticky_mode, sticky_files)

        if used_default and not sticky_hint_shown:
            print(
                "[claudio:hint] No mode set — defaulting to `ask -question`. "
                "Pin a mode with `ask -review`, `build -r`, `ask -debug`, etc. "
                "so future bare prompts inherit the right intent.",
                file=sys.stderr,
            )
            sticky_hint_shown = True

        try:
            argv = _tokenize(adjusted_line)
        except ValueError as e:
            print(f"[claudio:error] parse error: {e}", file=sys.stderr)
            continue

        # Refresh sticky state from this turn's argv (no-op for run/stats).
        new_mode, new_files = extract_sticky(argv)
        if new_mode is not None:
            sticky_mode = new_mode
            sticky_files = new_files

        argv = _augment_argv(argv, session_model, session_id, session_turn)
        _dispatch(argv)
        # Successful or not, the session is now "in flight" — claude allocates
        # the session on first invocation, so subsequent turns should --resume
        # rather than --session-id (which would conflict).
        session_turn += 1

    return 0


def _render_prompt(sticky_mode: tuple[str, str] | None) -> str:
    """Build the ANSI prompt string showing the current sticky mode."""
    if sticky_mode is None:
        return f"{CYAN}claudio>{RESET} "
    cmd, mode_flag = sticky_mode
    return f"{CYAN}claudio{RESET} {GREY}[{cmd} {mode_flag}]{RESET}{CYAN}>{RESET} "


def _augment_argv(
    argv: list[str],
    session_model: str | None,
    session_id: str,
    session_turn: int,
) -> list[str]:
    """Inject the REPL's session_model and auto-chain session_id into argv.

    Skipped when the user passed their own equivalent on the line so explicit
    overrides win.
    """
    out = list(argv)
    if session_model and "--model" not in out:
        out += ["--model", session_model]
    if "--session-id" not in out and "--resume" not in out:
        flag = "--session-id" if session_turn == 0 else "--resume"
        out += [flag, session_id]
    return out


# Structural-token vocabulary used by the tokenizer. These sets define what
# we'll recognise *before* the prose blob; everything after the last structural
# token is the user's prompt, taken verbatim to end-of-line.
_BARE_FLAGS = {
    "--dry-run", "--no-cache", "--verbose", "--json",
    "--feedback", "--agentic", "--help", "-h", "-v", "--version",
}
_VALUE_FLAGS = {"--model", "--session-id", "--resume", "-f", "--file"}
_MODES = {
    "-r", "-g", "-refactor", "-generate",
    "-q", "-rv", "-d", "-review", "-question", "-debug",
}
_LINE_RANGE_RE = re.compile(r"^-\d+(?:-\d+)?$")


def _tokenize(line: str) -> list[str]:
    """Split a REPL line into argv.

    Single-pass left-to-right walker:

      1. Consume *structural* tokens by whitespace boundary — the command
         word in slot 0, then any combination of mode flags (-r), bare flags
         (--verbose), value flags + their arg (--model haiku), @file refs
         (@src/x.py), and line ranges (-10-25).

      2. The first token that doesn't classify as structural marks the start
         of the prose blob. Everything from there to end-of-line becomes a
         single argv entry — verbatim, including any embedded quotes,
         apostrophes, @ symbols, or `--`-flag-looking sequences.

      3. If the entire prose blob is wrapped in matching outer quotes (no
         inner quote of the same type), strip them. This keeps the
         traditional `"whole prompt in quotes"` form working without
         clobbering inline quoted phrases like `do you think "ruby vs php"
         is still relevant?`.

    Consequence: once prose begins, `--verbose` / `@file.py` etc. become
    part of the message. Put them before your prompt if you want them
    interpreted structurally.
    """
    out: list[str] = []
    n = len(line)
    i = 0
    while i < n:
        while i < n and line[i].isspace():
            i += 1
        if i >= n:
            break
        j = i
        while j < n and not line[j].isspace():
            j += 1
        token = line[i:j]
        is_first = not out

        # Slot 0: command word (anything not starting with - or @). Validation
        # of which command names are real happens in cli.main.
        if is_first and not token.startswith(("-", "@")):
            out.append(token)
            i = j
            continue

        # @file reference, line range, bare flag, or known mode — keep
        # consuming structurals.
        if (token.startswith("@")
                or _LINE_RANGE_RE.match(token)
                or token in _BARE_FLAGS
                or token in _MODES):
            out.append(token)
            i = j
            continue

        # Value flag: pair it with the next whitespace-delimited token.
        if token in _VALUE_FLAGS:
            out.append(token)
            i = j
            while i < n and line[i].isspace():
                i += 1
            if i < n:
                k = i
                while k < n and not line[k].isspace():
                    k += 1
                out.append(line[i:k])
                i = k
            continue

        # Prose: take the rest of the line verbatim (preserving whatever
        # internal whitespace the user typed), then strip outer quotes if
        # the whole blob is one quoted string.
        prose = line[i:].rstrip()
        out.append(_strip_outer_quotes(prose))
        return out

    return out


def _strip_outer_quotes(s: str) -> str:
    """Strip matching outer quotes only when the entire string is wrapped.

    `"reduce complexity"` -> `reduce complexity`     (whole prompt quoted)
    `'what does this do'` -> `what does this do`     (single-quote variant)
    `"it's broken"`       -> `it's broken`           (different inner quote ok)
    `do you think "X" is relevant?` -> unchanged     (inline emphasis quote)
    `"a" and "b"`         -> unchanged               (two pairs, not one wrap)
    """
    if len(s) < 2:
        return s
    first, last = s[0], s[-1]
    if first != last or first not in ('"', "'"):
        return s
    if first in s[1:-1]:
        return s
    return s[1:-1]


def _dispatch(argv: list[str]) -> None:
    """Run a cli.main invocation inside the REPL, catching SystemExit."""
    from claudio.cli import main as cli_main
    try:
        cli_main(argv)
    except SystemExit:
        pass
    except KeyboardInterrupt:
        print("\n[claudio] interrupted", file=sys.stderr)
    except Exception as e:
        print(f"[claudio:error] {type(e).__name__}: {e}", file=sys.stderr)


def _handle_slash(
    line: str,
    current_model: str | None,
    current_session: str | None = None,
    current_mode: tuple[str, str] | None = None,
):
    parts = line.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/exit", "/quit", "/q"):
        return "exit"
    if cmd in ("/help", "/h", "/?"):
        print(HELP_TEXT)
        return None
    if cmd == "/clear":
        os.system("cls" if os.name == "nt" else "clear")
        return None
    if cmd == "/fresh":
        return "fresh"
    if cmd == "/session":
        print(f"[claudio] session: {current_session or '(none)'}")
        return None
    if cmd == "/cwd":
        if arg:
            try:
                os.chdir(os.path.expanduser(arg))
            except OSError as e:
                print(f"[claudio:error] {e}", file=sys.stderr)
                return None
        print(f"[claudio] cwd: {Path.cwd()}")
        return None
    if cmd == "/model":
        if not arg:
            print(f"[claudio] current model: {current_model or '(auto-route)'}")
            return None
        if arg.lower() in ("auto", "none", "clear", "reset"):
            return ("model", None)
        return ("model", arg)
    if cmd == "/mode":
        # /mode                -> show current
        # /mode none|clear     -> unset, future bare prompts get the default
        # /mode ask -review    -> pin (ask, -review)
        if not arg:
            if current_mode is None:
                print("[claudio] no sticky mode (bare prompts default to ask -q)")
            else:
                print(f"[claudio] sticky mode: {current_mode[0]} {current_mode[1]}")
            return None
        if arg.lower() in ("none", "clear", "reset", "off"):
            return ("mode", None)
        new_mode = _parse_mode_arg(arg)
        if new_mode is None:
            print(
                "[claudio:error] /mode expects e.g. `ask -review`, `build -r`, "
                "`ask -debug`. Use `/mode none` to clear.",
                file=sys.stderr,
            )
            return None
        return ("mode", new_mode)
    if cmd == "/stats":
        _dispatch(["stats"] + (_tokenize(arg) if arg else []))
        return None

    print(f"[claudio] unknown command: {cmd}. Try /help.", file=sys.stderr)
    return None


def _parse_mode_arg(arg: str) -> tuple[str, str] | None:
    """Parse a /mode argument like 'ask -review' or 'build -r' into the tuple.

    Returns None when the argument isn't a recognised (command, mode_flag)
    pair. Only `ask` and `build` are valid sticky commands.
    """
    tokens = arg.split()
    if len(tokens) != 2:
        return None
    cmd, flag = tokens[0].lower(), tokens[1]
    if cmd not in _STICKY_COMMANDS:
        return None
    if flag not in _MODES:
        return None
    return (cmd, flag)


def _print_banner() -> None:
    """Print the minimal startup banner.

    Layout (color-capable utf-8 TTY):

        █▀▀ █   ▄▀█ █ █ █▀▄ █ █▀█
        █▄▄ █▄▄ █▀█ █▄█ █▄▀ █ █▄█

        ✻ Claudio v1.3.0

          /help for commands  ·  @ to reference files  ·  Ctrl-D to exit
          cwd: ~/Documents/Perso/claudio

    The small CLAUDIO logo and ✻ glyph share the same cyan as the
    `claudio>` prompt for one consistent palette; the product name is
    bold; version, tips, and cwd are dim. On legacy cp1252 Windows
    consoles the logo is skipped and the glyph falls back to `*` — the
    rest of the banner still appears.
    """
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    can_unicode = encoding.startswith(("utf", "cp65001"))
    glyph = "✻" if can_unicode else "*"
    cwd = _format_cwd()
    use_color = colors_enabled(sys.stdout)

    parts: list[str] = [""]  # leading blank line

    # Logo block (only when the encoding can carry the block chars).
    # Uses plain CYAN — same shade as the ✻ glyph below and the `claudio>`
    # prompt — so the whole tool reads as one consistent colour palette.
    if can_unicode:
        if use_color:
            for row in _LOGO_LINES:
                parts.append(f"  {CYAN}{row}{RESET}")
        else:
            for row in _LOGO_LINES:
                parts.append(f"  {row}")
        parts.append("")  # blank line under the logo

    # Title line: glyph + product + dim version.
    if use_color:
        title = (
            f"  {CYAN}{glyph}{RESET} {BOLD}Claudio{RESET} "
            f"{DIM}v{__version__}{RESET}"
        )
    else:
        title = f"  {glyph} Claudio v{__version__}"
    parts.append(title)
    parts.append("")  # blank line under the title

    # Tips + cwd, both dim.
    if use_color:
        parts.append(
            f"  {DIM}/help for commands  ·  @ to reference files  "
            f"·  Ctrl-D to exit{RESET}"
        )
        parts.append(f"  {DIM}cwd: {cwd}{RESET}")
    else:
        parts.append(
            "  /help for commands  ·  @ to reference files  ·  Ctrl-D to exit"
        )
        parts.append(f"  cwd: {cwd}")
    parts.append("")  # trailing blank

    output = "\n".join(parts) + "\n"
    try:
        sys.stdout.write(output)
        sys.stdout.flush()
    except UnicodeEncodeError:
        # Last-ditch ASCII fallback for terminals that can't render `·` etc.
        try:
            sys.stdout.write(
                f"\n  * Claudio v{__version__}\n\n"
                f"  /help for commands, @ to reference files, Ctrl-D to exit\n"
                f"  cwd: {cwd}\n\n"
            )
            sys.stdout.flush()
        except OSError:
            pass
    except OSError:
        # stdout closed/broken: swallow so the REPL still launches.
        pass


def _format_cwd() -> str:
    """Return the cwd, collapsed to ~ when it sits under the user's home.

    Examples:
      /Users/yves/Documents/Perso/claudio  ->  ~/Documents/Perso/claudio
      C:\\Users\\yves\\Documents\\claudio   ->  ~/Documents/claudio
      /opt/work                            ->  /opt/work
    """
    try:
        cwd = Path.cwd()
        home = Path.home()
        try:
            rel = cwd.relative_to(home)
            return f"~/{rel.as_posix()}" if rel.parts else "~"
        except ValueError:
            return cwd.as_posix()
    except OSError:
        return "(unknown)"


def _print_update_notice() -> None:
    """Print the cached "new release available" notice, if any."""
    notice = pending_notice(stream=sys.stdout)
    if not notice:
        return
    try:
        sys.stdout.write(notice + "\n\n")
        sys.stdout.flush()
    except (UnicodeEncodeError, OSError):
        pass


def _history_file() -> Path:
    base = os.environ.get("CLAUDIO_HOME")
    if base:
        return Path(base) / "repl_history"
    home = Path.home()
    return home / ".claudio" / "repl_history"


if __name__ == "__main__":
    sys.exit(main())
