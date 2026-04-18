"""Claude CLI executor -- sends optimized prompts to Claude."""

import subprocess
import shutil
import sys

from claudio.config import load_config
from claudio.utils.spinner import Spinner


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


def execute_prompt(
    prompt: str,
    json_output: bool = False,
    model: str | None = None,
    session_id: str | None = None,
    resume: str | None = None,
    allowed_tools: list[str] | None = None,
) -> str:
    """Send a prompt to Claude CLI and return the response.

    Args:
        prompt: The optimized prompt to send.
        json_output: If True, request JSON output from Claude.
        model: Model alias (haiku/sonnet/opus) or full ID. Passed via --model.
        session_id: Fixed session ID (UUID). Passed via --session-id.
        resume: Existing session ID to resume. Passed via --resume. Mutually
            exclusive with session_id at the CLI level.
        allowed_tools: List of tool names to allow (e.g. ["Read", "Grep", "Glob"]).
            Passed via --allowedTools. Empty/None means no tools (single-shot).
    """
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
    if model:
        cmd.extend(["--model", model])
    if resume:
        cmd.extend(["--resume", resume])
    elif session_id:
        cmd.extend(["--session-id", session_id])
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    config = load_config()
    timeout = config.get("timeout", 300)

    spinner_label = f"asking {model}" if model else "asking claude"
    try:
        with Spinner(spinner_label):
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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
