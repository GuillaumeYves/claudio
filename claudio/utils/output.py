"""Output formatting for CLI results."""

import json
import sys


class Output:
    """Structured output formatter for Claudio."""

    def __init__(self, json_mode: bool = False, verbose: bool = False):
        self.json_mode = json_mode
        self.verbose = verbose

    def result(self, content: str, metadata: dict | None = None) -> None:
        """Print the main result."""
        if self.json_mode:
            payload = {"result": content}
            if metadata:
                payload["metadata"] = metadata
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            if self.verbose and metadata:
                self._print_metadata(metadata)
            # Handle Windows encoding issues gracefully
            try:
                print(content)
            except UnicodeEncodeError:
                print(content.encode("utf-8", errors="replace").decode("ascii", errors="replace"))

    def info(self, message: str) -> None:
        """Print an informational message to stderr."""
        if not self.json_mode:
            print(f"[claudio] {message}", file=sys.stderr)

    def warn(self, message: str) -> None:
        """Print a warning to stderr."""
        print(f"[claudio:warn] {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        """Print an error to stderr."""
        print(f"[claudio:error] {message}", file=sys.stderr)

    def debug(self, message: str) -> None:
        """Print debug info (only in verbose mode)."""
        if self.verbose and not self.json_mode:
            print(f"[claudio:debug] {message}", file=sys.stderr)

    def diff(self, original: str, modified: str, filename: str = "") -> None:
        """Output a unified diff."""
        import difflib
        diff_lines = difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{filename}" if filename else "a/original",
            tofile=f"b/{filename}" if filename else "b/modified",
        )
        sys.stdout.writelines(diff_lines)

    def _print_metadata(self, metadata: dict) -> None:
        for key, value in metadata.items():
            print(f"  {key}: {value}", file=sys.stderr)
        print("---", file=sys.stderr)
