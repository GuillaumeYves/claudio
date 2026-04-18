"""Unified `claudio` entry point.

Running bare `claudio` drops you *inside* the tool — type commands
(build / ask / run / stats) directly, no prefix needed.
`@<prefix>` triggers live file/folder completion, and slash commands
handle meta operations like /model, /cwd, /stats.

Running `claudio <subcommand> …` is the single-shot form, for scripts
and one-liners. Both paths share the same command router (`cli.main`).

`prompt_toolkit` ships as a required dependency of `claudio-cli`.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

from claudio import __version__


BANNER = f"""\

    ███████╗██║      █████╗ ██╗   ██╗██████╗ ██╗ ██████╗
    ██╔════╝██║     ██╔══██╗██║   ██║██╔══██╗██║██╔═══██╗
    ██║     ██║     ███████║██║   ██║██║  ██║██║██║   ██║
    ██║     ██║     ██╔══██║██║   ██║██║  ██║██║██║   ██║
    ╚██████╗███████╗██║  ██║╚██████╔╝██████╔╝██║╚██████╔╝
    ╚═════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═╝ ╚═════╝

    Claude Intelligence Optimizer  v{__version__}

    ─────────────────────────────────────────────────────
        /help     View commands
        @file     Reference files
        Ctrl+D    Exit
    ─────────────────────────────────────────────────────

"""

HELP_TEXT = """\
Commands:
    build -r|-g [@file...] "description"   Create or modify code
    ask   -q|-rv|-d [@file...] "question"  Ask, review, or debug
    run [--agentic]                        Execute claudio-task.json
    stats [--reset]                        Show usage & cost tracking

Slash commands:
    /help            Show this help
    /model NAME      Set model for this session (haiku|sonnet|opus)
    /cwd [PATH]      Show or change working directory
    /clear           Clear the screen
    /exit | /quit    Exit (Ctrl-D also works)

File references:
    @path/to/file    Tab-completes from current directory
    -N or -N-N       Line range for the preceding @file

Global flags: 
    --dry-run --no-cache --verbose --json
    --model NAME --feedback --agentic
"""

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
             "dist", "build", ".tox", ".mypy_cache", ".pytest_cache"}

_AT_PATTERN = re.compile(r"@([^\s@]*)$")


def iter_at_completions(text_before_cursor: str):
    """Yield (text, display, meta) triples for an @<prefix> in the input.

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


# Subcommands handled by cli.main in single-shot mode.
_CLI_SUBCOMMANDS = {"build", "ask", "run", "stats", "setup"}


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

    class AtFileCompleter(Completer):
        def get_completions(self, document, complete_event):
            for text, display, meta, token_len in iter_at_completions(
                document.text_before_cursor
            ):
                yield Completion(
                    text=text,
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
        completer=AtFileCompleter(),
        complete_while_typing=True,
        complete_style=CompleteStyle.MULTI_COLUMN,
    )

    _print_banner()

    session_model: str | None = None

    while True:
        try:
            line = session.prompt(ANSI("\x1b[36mclaudio>\x1b[0m "))
        except KeyboardInterrupt:
            continue
        except EOFError:
            sys.stdout.write("\n")
            break

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            action = _handle_slash(line, session_model)
            if action == "exit":
                break
            if isinstance(action, tuple) and action[0] == "model":
                session_model = action[1]
                print(f"[claudio] model set to: {session_model or '(auto-route)'}")
            continue

        try:
            argv = _tokenize(line)
        except ValueError as e:
            print(f"[claudio:error] parse error: {e}", file=sys.stderr)
            continue

        if session_model and "--model" not in argv:
            argv = argv + ["--model", session_model]

        _dispatch(argv)

    return 0


def _tokenize(line: str) -> list[str]:
    """Split a REPL line into argv.

    Uses posix=False so backslashes survive (Windows paths like `src\\file.py`
    stay intact), then strips matching outer quotes from each token so the
    description in `build -r @f "a b"` becomes `a b`, not `"a b"`.
    """
    tokens = shlex.split(line, posix=False)
    out: list[str] = []
    for t in tokens:
        if len(t) >= 2 and t[0] == t[-1] and t[0] in ('"', "'"):
            out.append(t[1:-1])
        else:
            out.append(t)
    return out


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


def _handle_slash(line: str, current_model: str | None):
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
    if cmd == "/stats":
        _dispatch(["stats"] + (_tokenize(arg) if arg else []))
        return None

    print(f"[claudio] unknown command: {cmd}. Try /help.", file=sys.stderr)
    return None


def _print_banner() -> None:
    """Print the banner, falling back to ASCII if the console can't render it.

    Old Windows consoles default to cp1252 and choke on the box-drawing chars
    in BANNER. Detect and degrade gracefully instead of crashing on launch.
    """
    ascii_banner = (
        f"  Claudio v{__version__} -- Claude Intelligence Optimizer\n"
        "  Type /help for commands  .  @ to reference files  .  Ctrl-D to exit\n"
    )
    try:
        sys.stdout.write(BANNER + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        try:
            sys.stdout.write(ascii_banner + "\n")
            sys.stdout.flush()
        except OSError:
            pass
    except OSError:
        # stdout closed or broken -- swallow, we still want the REPL to run
        pass


def _history_file() -> Path:
    base = os.environ.get("CLAUDIO_HOME")
    if base:
        return Path(base) / "repl_history"
    home = Path.home()
    return home / ".claudio" / "repl_history"


if __name__ == "__main__":
    sys.exit(main())
