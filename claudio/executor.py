"""Claude CLI executor -- sends optimized prompts to Claude."""

import subprocess
import shutil
import sys

from claudio.config import load_config


def find_claude_cli() -> str | None:
    """Find the Claude CLI binary."""
    config = load_config()
    configured = config.get("claude_binary", "claude")

    # Check configured name first
    path = shutil.which(configured)
    if path:
        return path

    # Fallback to common names
    for name in ("claude", "claude.exe"):
        path = shutil.which(name)
        if path:
            return path
    return None


def execute_prompt(prompt: str, json_output: bool = False) -> str:
    """Send a prompt to Claude CLI and return the response."""
    claude_bin = find_claude_cli()
    if not claude_bin:
        print(
            "[claudio:error] Claude CLI not found. Install it from https://claude.ai/code\n"
            "In the meantime, use --dry-run to see the optimized prompt.",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = [claude_bin, "--print"]
    if json_output:
        cmd.extend(["--output-format", "json"])

    config = load_config()
    timeout = config.get("timeout", 300)

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"[claudio:error] Claude CLI timed out after {timeout}s.", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"[claudio:error] Could not execute: {claude_bin}", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr:
            print(f"[claudio:error] Claude CLI error: {stderr}", file=sys.stderr)
        sys.exit(result.returncode)

    return result.stdout.strip()
