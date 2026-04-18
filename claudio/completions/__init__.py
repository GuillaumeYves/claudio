"""Shell completion generators for claudio."""

# Command tree -- single source of truth for completions and help.
COMMANDS = {
    "build": {
        "modes": {
            "-refactor": "Refactor existing code",
            "-r": "Refactor (short)",
            "-generate": "Generate new code",
            "-g": "Generate (short)",
        },
    },
    "ask": {
        "modes": {
            "-review": "Code review",
            "-rv": "Review (short)",
            "-question": "General question",
            "-q": "Question (short)",
            "-debug": "Debug an issue",
            "-d": "Debug (short)",
        },
    },
    "run": {
        "modes": {},
    },
    "stats": {
        "modes": {
            "--reset": "Clear all usage data",
        },
    },
    "setup": {
        "modes": {},
    },
}

GLOBAL_FLAGS = ["--dry-run", "--no-cache", "--verbose", "--json", "--help", "--version"]
