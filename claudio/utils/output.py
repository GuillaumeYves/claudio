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

    def _print_metadata(self, metadata: dict) -> None:
        for key, value in metadata.items():
            print(f"  {key}: {value}", file=sys.stderr)
        print("---", file=sys.stderr)
