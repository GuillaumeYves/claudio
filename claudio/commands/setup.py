"""cld setup -- post-install configuration.

Detects if cld is on PATH, offers to add it, verifies Claude CLI.
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def execute(raw_args: list[str], ctx: dict) -> int:
    print("Claudio -- Post-install setup\n")

    # Step 1: Check if cld is already on PATH
    cld_on_path = shutil.which("cld") is not None

    if cld_on_path:
        print("[OK] cld is on PATH")
    else:
        print("[!!] cld is NOT on PATH")
        scripts_dir = _find_scripts_dir()

        if scripts_dir:
            print(f"\n  Recommended: add cld to PATH for fast access from any terminal.")
            print(f"  Scripts directory: {scripts_dir}\n")

            try:
                answer = input("  Add to PATH now? (recommended) [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                answer = "n"

            if answer in ("", "y", "yes"):
                success = _add_to_path(scripts_dir)
                if success:
                    print(f"\n[OK] Added {scripts_dir} to PATH")
                    print("     Restart your terminal for changes to take effect.")
                else:
                    print(f"\n[!!] Could not add automatically.")
                    _print_manual_instructions(scripts_dir)
            else:
                print()
                _print_manual_instructions(scripts_dir)
        else:
            print("  Could not detect scripts directory.")
            print("  Make sure you installed with: pip install claudio-cli")
            print("  Then ensure Python's Scripts directory is on PATH.")

    # Step 2: Check Claude CLI
    print()
    claude_bin = shutil.which("claude") or shutil.which("claude.exe")
    if claude_bin:
        print(f"[OK] Claude CLI found: {claude_bin}")
    else:
        print("[!!] Claude CLI not found")
        print("     Install from: https://docs.anthropic.com/en/docs/claude-code")
        print("     You can still use --dry-run to preview optimized prompts.")

    # Step 3: Create config dir
    config_dir = Path.home() / ".config" / "claudio"
    if not config_dir.exists():
        config_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n[OK] Created config directory: {config_dir}")
    else:
        print(f"\n[OK] Config directory exists: {config_dir}")

    # Step 4: Shell completions
    print()
    _setup_completions()

    print("\nSetup complete. Run: cld --help")
    return 0


def _setup_completions() -> None:
    """Detect shell and offer to install completions."""
    system = platform.system()
    shell = os.environ.get("SHELL", "")

    if system == "Windows":
        # Windows: PowerShell is the primary shell
        profile_cmd = "$PROFILE"
        eval_cmd = 'cld --completions powershell | Invoke-Expression'
        shell_name = "PowerShell"
        profile_path = _find_powershell_profile()
        append_line = f'\n# Claudio CLI completions\n{eval_cmd}\n'
    elif "zsh" in shell:
        shell_name = "Zsh"
        profile_path = Path.home() / ".zshrc"
        eval_cmd = 'eval "$(cld --completions zsh)"'
        append_line = f'\n# Claudio CLI completions\n{eval_cmd}\n'
    else:
        shell_name = "Bash"
        profile_path = Path.home() / ".bashrc"
        eval_cmd = 'eval "$(cld --completions bash)"'
        append_line = f'\n# Claudio CLI completions\n{eval_cmd}\n'

    # Check if already installed
    if profile_path and profile_path.exists():
        existing = profile_path.read_text(errors="replace")
        if "cld --completions" in existing:
            print(f"[OK] {shell_name} completions already installed")
            return

    print(f"  Tab completion for {shell_name} enables:")
    print(f"    cld <TAB>          -> build, ask, run, setup")
    print(f"    cld build -<TAB>   -> -refactor, -generate, ...")
    print(f"    cld ask -d @<TAB>  -> @file paths from workspace\n")

    try:
        answer = input(f"  Install {shell_name} completions? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = "n"

    if answer in ("", "y", "yes"):
        if profile_path:
            try:
                with open(profile_path, "a", encoding="utf-8") as f:
                    f.write(append_line)
                print(f"\n[OK] Completions added to {profile_path}")
                print("     Restart your terminal to activate.")
            except OSError as e:
                print(f"\n[!!] Could not write to {profile_path}: {e}")
                print(f"     Add manually: {eval_cmd}")
        else:
            print(f"\n[!!] Could not find {shell_name} profile.")
            print(f"     Add manually: {eval_cmd}")
    else:
        print(f"\n     To install later: {eval_cmd}")


def _find_scripts_dir() -> str | None:
    """Find the pip scripts directory where cld would be installed."""
    # Check user scripts first (pip install --user)
    user_scripts = Path(sys.prefix) / "Scripts"
    if user_scripts.exists() and (user_scripts / "cld.exe").exists():
        return str(user_scripts)
    if user_scripts.exists() and (user_scripts / "cld").exists():
        return str(user_scripts)

    # Check site-packages bin
    for p in sys.path:
        scripts = Path(p).parent / "Scripts"
        if scripts.exists() and any(scripts.glob("cld*")):
            return str(scripts)
        scripts = Path(p).parent / "bin"
        if scripts.exists() and any(scripts.glob("cld*")):
            return str(scripts)

    # Fallback: derive from sys.executable
    exe_dir = Path(sys.executable).parent
    if platform.system() == "Windows":
        candidate = exe_dir / "Scripts"
    else:
        candidate = exe_dir
    if candidate.exists():
        return str(candidate)

    return None


def _add_to_path(scripts_dir: str) -> bool:
    """Attempt to add scripts_dir to system PATH."""
    system = platform.system()

    if system == "Windows":
        return _add_to_path_windows(scripts_dir)
    else:
        return _add_to_path_unix(scripts_dir)


def _add_to_path_windows(scripts_dir: str) -> bool:
    """Add to PATH on Windows via registry (user-level)."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0,
            winreg.KEY_ALL_ACCESS,
        )
        try:
            current_path, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current_path = ""

        # Check if already there
        paths = [p.strip() for p in current_path.split(";") if p.strip()]
        if scripts_dir.lower() in [p.lower() for p in paths]:
            winreg.CloseKey(key)
            return True

        new_path = current_path.rstrip(";") + ";" + scripts_dir
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
        winreg.CloseKey(key)

        # Broadcast change to other processes
        try:
            import ctypes
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x1A
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", 0x0002, 5000, None
            )
        except Exception:
            pass

        return True
    except Exception:
        return False


def _add_to_path_unix(scripts_dir: str) -> bool:
    """Add to PATH on Linux/macOS via shell profile."""
    shell = os.environ.get("SHELL", "/bin/bash")
    if "zsh" in shell:
        profile = Path.home() / ".zshrc"
    elif "fish" in shell:
        # Fish uses a different syntax
        profile = Path.home() / ".config" / "fish" / "config.fish"
    else:
        profile = Path.home() / ".bashrc"

    line = f'\nexport PATH="{scripts_dir}:$PATH"  # Added by claudio\n'

    try:
        existing = profile.read_text() if profile.exists() else ""
        if scripts_dir in existing:
            return True  # Already there
        with open(profile, "a") as f:
            f.write(line)
        return True
    except Exception:
        return False


def _print_manual_instructions(scripts_dir: str) -> None:
    """Print manual PATH setup instructions."""
    system = platform.system()

    if system == "Windows":
        print(f"  To add cld to PATH manually:\n")
        print(f"    1. Open Settings > System > About > Advanced system settings")
        print(f"    2. Click 'Environment Variables'")
        print(f"    3. Edit 'Path' under User variables")
        print(f"    4. Add: {scripts_dir}")
        print(f"\n  Or run in PowerShell (admin):")
        print(f'    [Environment]::SetEnvironmentVariable("Path", $env:Path + ";{scripts_dir}", "User")')
    else:
        shell = os.environ.get("SHELL", "/bin/bash")
        if "zsh" in shell:
            rc = "~/.zshrc"
        else:
            rc = "~/.bashrc"
        print(f"  To add cld to PATH manually:\n")
        print(f'    echo \'export PATH="{scripts_dir}:$PATH"\' >> {rc}')
        print(f"    source {rc}")


def _find_powershell_profile() -> Path | None:
    """Find the PowerShell profile path on Windows."""
    # Common profile locations
    docs = Path.home() / "Documents"
    candidates = [
        docs / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
        docs / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
    ]
    for c in candidates:
        if c.exists():
            return c
    # Return first candidate (will be created)
    if candidates:
        candidates[0].parent.mkdir(parents=True, exist_ok=True)
        return candidates[0]
    return None
