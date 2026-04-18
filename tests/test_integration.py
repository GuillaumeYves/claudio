"""End-to-end integration tests that exercise the real CLI dispatch path.

These use --dry-run so no actual Claude API call is made, and a fake Claude
binary for tests that need to verify the executor/spinner path without
installing a real CLI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


CLAUDIO_ROOT = Path(__file__).resolve().parent.parent


def _run_claudio(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run the `claudio` CLI via `python -m claudio` so tests work without
    the entry-point scripts being on PATH."""
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [sys.executable, "-m", "claudio", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=full_env,
        timeout=30,
    )


def test_cli_help_runs():
    result = _run_claudio(["--help"], cwd=CLAUDIO_ROOT)
    assert result.returncode == 0
    assert "claudio" in result.stdout
    assert "PowerShell note" in result.stdout
    assert "-f path" in result.stdout


def test_cli_version():
    result = _run_claudio(["--version"], cwd=CLAUDIO_ROOT)
    assert result.returncode == 0
    assert "claudio" in result.stdout


def test_cli_unknown_command_errors():
    result = _run_claudio(["bluid"], cwd=CLAUDIO_ROOT)
    assert result.returncode != 0
    # "bluid" isn't a subcommand, so repl.main treats it as an unknown arg
    # and cli.main never sees it.
    assert "unknown" in result.stderr.lower()


def test_build_with_dash_f_dry_run(tmp_path):
    """The main bug report: `claudio build -r -f path "prompt"` should not
    silently no-op. With --dry-run, we get the compiled prompt back."""
    target = tmp_path / "hello.py"
    target.write_text("def hello():\n    return 'hi'\n", encoding="utf-8")

    result = _run_claudio(
        ["build", "-r", "-f", "hello.py", "--dry-run", "rename function"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    # Dry-run prints the compiled prompt; must contain the task and the file
    assert "rename function" in result.stdout.lower() or "Refactor" in result.stdout
    assert "hello" in result.stdout  # file content was included


def test_build_with_missing_file_reports_error(tmp_path):
    result = _run_claudio(
        ["build", "-r", "-f", "nonexistent.py", "msg"],
        cwd=tmp_path,
    )
    assert result.returncode != 0
    assert "not found" in result.stderr.lower() or "error" in result.stderr.lower()


def test_build_with_directory_reports_error(tmp_path):
    """The original user's bug: @assets pointed at a directory. Must not
    silently hang -- it should error cleanly."""
    (tmp_path / "assets").mkdir()
    result = _run_claudio(
        ["build", "-r", "-f", "assets", "describe it"],
        cwd=tmp_path,
    )
    assert result.returncode != 0
    assert "not a file" in result.stderr.lower() or "error" in result.stderr.lower()


def test_build_with_at_file_dry_run(tmp_path):
    """Original @file syntax still works (bash/zsh/cmd users)."""
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result = _run_claudio(
        ["build", "-r", "@a.py", "--dry-run", "rename"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_completions_powershell_emits_script():
    result = _run_claudio(["--completions", "powershell"], cwd=CLAUDIO_ROOT)
    assert result.returncode == 0
    assert "Register-ArgumentCompleter" in result.stdout


def test_claudio_help_flag():
    result = subprocess.run(
        [sys.executable, "-m", "claudio.repl", "--help"],
        cwd=str(CLAUDIO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    assert result.returncode == 0
    assert "claudio" in result.stdout.lower()
    assert "REPL" in result.stdout or "interactive" in result.stdout.lower()


def test_claudio_version_flag():
    result = subprocess.run(
        [sys.executable, "-m", "claudio.repl", "--version"],
        cwd=str(CLAUDIO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    assert result.returncode == 0
    assert "claudio" in result.stdout.lower()


def test_claudio_non_tty_exits_with_hint(tmp_path):
    """Running `claudio` with piped stdin must bail fast with a helpful
    message, not crash inside prompt_toolkit or hang waiting for input."""
    result = subprocess.run(
        [sys.executable, "-m", "claudio.repl"],
        cwd=str(CLAUDIO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        input="",  # piped stdin -- not a TTY
    )
    assert result.returncode == 1
    # Either the TTY hint or the prompt_toolkit hint is acceptable -- both
    # mean we exited cleanly instead of hanging.
    combined = result.stderr.lower()
    assert "tty" in combined or "prompt_toolkit" in combined


def test_claudio_repl_missing_prompt_toolkit(tmp_path):
    """Broken installs with prompt_toolkit missing must fail fast with a
    reinstall hint -- not a bare ImportError traceback. prompt_toolkit is
    now a required dep, so this path only trips on a broken environment."""
    script = tmp_path / "block_pt.py"
    script.write_text(
        "import sys\n"
        "sys.modules['prompt_toolkit'] = None\n"
        "from claudio.repl import main\n"
        "sys.exit(main())\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(CLAUDIO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    assert result.returncode == 1
    assert "prompt_toolkit" in result.stderr
    assert "pip install" in result.stderr
