"""Claudio CLI -- main entry point and command router.

Strict argument order: COMMAND > MODE > @FILES [-lines] > DESCRIPTION
"""

import sys

from claudio import __version__

USAGE = """\
cld -- Claude Intelligence Optimizer

Syntax (strict order):
    cld <command> <mode> [@file [-lines]] ... [description]

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
    -N                  Single line from preceding @file
    -N-N                Line range from preceding @file

Global flags:
    --dry-run           Show optimized prompt without sending to Claude
    --no-cache          Bypass response cache for this request
    --verbose           Show token estimates and pipeline details
    --json              Output in JSON format
    --completions SHELL Output shell completion script (bash/zsh/powershell)
    -v, --version       Show version
    -h, --help          Show this help

Argument order is enforced:
    cld build -refactor @main.py -10-25 "reduce complexity"     OK
    cld ask -debug @server.log -100-200 "timeout error"         OK
    cld build "text" -refactor @file.py                         ERROR
"""


def _pop_global_flags(argv: list[str]) -> dict:
    """Extract global flags from argv, return (remaining, flags dict)."""
    flags = {
        "dry_run": False,
        "no_cache": False,
        "verbose": False,
        "json_output": False,
    }
    remaining = []

    for arg in argv:
        if arg == "--dry-run":
            flags["dry_run"] = True
        elif arg == "--no-cache":
            flags["no_cache"] = True
        elif arg == "--verbose":
            flags["verbose"] = True
        elif arg == "--json":
            flags["json_output"] = True
        else:
            remaining.append(arg)

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

    # Shell completions: cld --completions bash|zsh|powershell
    if argv[0] == "--completions" and len(argv) >= 2:
        return _output_completions(argv[1])

    command = argv[0]
    rest, ctx = _pop_global_flags(argv[1:])

    if command == "build":
        from claudio.commands.build import execute
        return execute(rest, ctx)
    elif command == "ask":
        from claudio.commands.ask import execute
        return execute(rest, ctx)
    elif command == "run":
        from claudio.commands.run import execute
        return execute(rest, ctx)
    elif command == "stats":
        from claudio.commands.stats import execute
        return execute(rest, ctx)
    elif command == "setup":
        from claudio.commands.setup import execute
        return execute(rest, ctx)
    else:
        print(f"Unknown command: {command}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 1


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
