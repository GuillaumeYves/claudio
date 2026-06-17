"""Claudio CLI -- main entry point and command router.

Strict argument order: COMMAND > MODE > @FILES [-lines] > DESCRIPTION
"""

import sys

from claudio import __version__

USAGE = """\
claudio -- Claude Intelligence Optimizer

Usage:
    claudio                                  Launch the interactive session
    claudio <command> <mode> [@file [-lines]] ... [description]   One-shot

Commands:
    build   Create or modify code
    ask     Ask Claude a question
    run     Execute a plan from claudio-task.json
    stats   View token usage and cost tracking
    setup   Configure PATH, completions, and verify installation

Build modes:
    -refactor | -r      Refactor existing code
    -generate | -g      Generate new code

Ask modes:
    -review   | -rv     Code review
    -question | -q      General question
    -debug    | -d      Debug an issue

File attachments (max 10):
    @path/to/file       Attach a workspace file as context
    -f path | --file    Same as @path (PowerShell-safe — see note below)
    -N                  Single line from preceding @file
    -N-N                Line range from preceding @file

  PowerShell note:
    PowerShell treats `@name` as the splatting operator and silently drops
    it when `$name` is unset. On PowerShell, either quote: '@path', or use
    `-f path` instead. Bash/zsh/cmd are unaffected.

Global flags:
    --dry-run           Show optimized prompt without sending to Claude
    --estimate          Show token count + projected input cost, then exit
    --no-cache          Bypass response cache for this request
    --verbose           Show token estimates and pipeline details
    --json              Output in JSON format
    --model NAME        Override model (haiku|sonnet|opus or full ID)
    --session-id UUID   Start a session with a fixed ID (reusable via --resume)
    --resume UUID       Resume an existing Claude session (warm prompt cache)
    --feedback          Let Claude request missing context (auto-retry once)
    --agentic           (claudio run only) Execute the plan in one agentic session
    --completions SHELL Output shell completion script (bash/zsh/powershell)
    -v, --version       Show version
    -h, --help          Show this help

Tip: inside the interactive session you drop the `claudio` prefix — just
type `build -r @file.py "..."`, `ask -q "..."`, `/model haiku`, etc.

Argument order is enforced:
    claudio build -refactor @main.py -10-25 "reduce complexity"     OK
    claudio ask -debug @server.log -100-200 "timeout error"         OK
    claudio build "text" -refactor @file.py                         ERROR
"""


# Flags that take a value (the next argv is the value, not a positional arg).
_VALUE_FLAGS = {"--model", "--session-id", "--resume"}


def _pop_global_flags(argv: list[str]) -> tuple[list[str], dict]:
    """Extract global flags from argv, return (remaining, flags dict)."""
    flags = {
        "dry_run": False,
        "estimate": False,
        "no_cache": False,
        "verbose": False,
        "json_output": False,
        "model": None,
        "session_id": None,
        "resume": None,
        "feedback": False,
        "agentic": False,
    }
    remaining: list[str] = []

    i = 0
    while i < len(argv):
        arg = argv[i]

        if arg == "--dry-run":
            flags["dry_run"] = True
        elif arg == "--estimate":
            flags["estimate"] = True
        elif arg == "--no-cache":
            flags["no_cache"] = True
        elif arg == "--verbose":
            flags["verbose"] = True
        elif arg == "--json":
            flags["json_output"] = True
        elif arg == "--feedback":
            flags["feedback"] = True
        elif arg == "--agentic":
            flags["agentic"] = True
        elif arg in _VALUE_FLAGS:
            if i + 1 >= len(argv):
                print(f"[claudio:error] {arg} requires a value", file=sys.stderr)
                sys.exit(2)
            value = argv[i + 1]
            if arg == "--model":
                flags["model"] = value
            elif arg == "--session-id":
                flags["session_id"] = value
            elif arg == "--resume":
                flags["resume"] = value
            i += 2
            continue
        else:
            remaining.append(arg)
        i += 1

    return remaining, flags


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    if argv[0] in ("-v", "--version"):
        print(f"claudio {__version__}")
        return 0

    # Shell completions: claudio --completions bash|zsh|powershell
    if argv[0] == "--completions" and len(argv) >= 2:
        return _output_completions(argv[1])

    command = argv[0]
    rest, ctx = _pop_global_flags(argv[1:])

    # Refresh the PyPI version cache opportunistically. Daemon thread, no-op
    # if the cache is already fresh or if the user opted out.
    from claudio.utils.update_check import start_background_check
    start_background_check()

    if command == "build":
        from claudio.commands.build import execute
        rc = execute(rest, ctx)
    elif command == "ask":
        from claudio.commands.ask import execute
        rc = execute(rest, ctx)
    elif command == "run":
        from claudio.commands.run import execute
        rc = execute(rest, ctx)
    elif command == "stats":
        from claudio.commands.stats import execute
        rc = execute(rest, ctx)
    elif command == "setup":
        from claudio.commands.setup import execute
        rc = execute(rest, ctx)
    else:
        print(f"Unknown command: {command}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 1

    # Trailing pip-style notice. Suppressed in --json mode so machine-parsed
    # output stays clean.
    if not ctx.get("json_output"):
        _maybe_print_update_notice()
    return rc


def _maybe_print_update_notice() -> None:
    from claudio.utils.update_check import pending_notice
    notice = pending_notice(stream=sys.stderr)
    if notice:
        try:
            print("\n" + notice, file=sys.stderr)
        except (UnicodeEncodeError, OSError):
            pass


def _output_completions(shell: str) -> int:
    """Print shell completion script to stdout."""
    shell = shell.lower().strip()
    if shell in ("bash", "sh"):
        from claudio.completions.bash import generate
    elif shell in ("zsh",):
        from claudio.completions.zsh import generate
    elif shell in ("powershell", "pwsh", "ps", "ps1"):
        from claudio.completions.powershell import generate
    else:
        print(f"Unknown shell: {shell}. Supported: bash, zsh, powershell", file=sys.stderr)
        return 1
    print(generate())
    return 0


if __name__ == "__main__":
    sys.exit(main())
