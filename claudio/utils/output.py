"""Output formatting for CLI results.

`Output.result()` renders the response through the markdown -> ANSI
converter on a colour-capable TTY (headers, bold, code spans, fences,
lists, blockquotes). On `--json` mode or piped stdout the content is
emitted verbatim so machine parsers see what they expect.

`info` / `success` / `warn` / `error` write labelled, colour-tagged
messages to stderr so the response on stdout stays clean for piping.
"""

import json
import sys

from claudio.utils.colors import CYAN, GREEN, RED, YELLOW, colored
from claudio.utils.markdown import (
    _term_width,
    _wrap_with_indent,
    render as render_markdown,
)


class Output:
    """Structured output formatter for Claudio."""

    def __init__(self, json_mode: bool = False, verbose: bool = False):
        self.json_mode = json_mode
        self.verbose = verbose

    def result(self, content: str, metadata: dict | None = None) -> None:
        """Print the main result.

        In TTY mode the response is rendered through the markdown -> ANSI
        converter (bold/headers/code spans become styled text) and each
        line is left-padded so the answer visually stands apart from the
        user's prompt. In JSON mode or piped output, content is emitted
        verbatim so machine parsers see what they expect.
        """
        if self.json_mode:
            payload = {"result": content}
            if metadata:
                payload["metadata"] = metadata
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            if self.verbose and metadata:
                self._print_metadata(metadata)
            rendered = render_markdown(content, stream=sys.stdout)
            # Wrap-aware indent: each *visual* row carries the 2-space
            # gutter, including soft-wrapped continuations. Only kicks in
            # when we styled (TTY); piped output stays flush left.
            if rendered is not content:
                width = _term_width()
                wrapped_rows: list[str] = []
                for line in rendered.splitlines():
                    wrapped_rows.extend(_wrap_with_indent(line, width, "  "))
                rendered = "\n".join(wrapped_rows)
            try:
                print(rendered)
            except UnicodeEncodeError:
                print(rendered.encode("utf-8", errors="replace").decode("ascii", errors="replace"))

    def info(self, message: str) -> None:
        """Print an informational message to stderr."""
        if not self.json_mode:
            label = colored("[claudio]", CYAN, stream=sys.stderr)
            print(f"{label} {message}", file=sys.stderr)

    def success(self, message: str) -> None:
        """Print a success message to stderr."""
        if not self.json_mode:
            label = colored("[claudio]", GREEN, stream=sys.stderr, bold=True)
            print(f"{label} {message}", file=sys.stderr)

    def warn(self, message: str) -> None:
        """Print a warning to stderr."""
        label = colored("[claudio:warn]", YELLOW, stream=sys.stderr, bold=True)
        print(f"{label} {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        """Print an error to stderr -- entire line in red.

        The label `[claudio:error]` is bold red and the message after it is
        also red (not the default fg) so the whole line reads as one error
        block at a glance. Previously only the label was coloured, which
        let long error messages blend into surrounding output.
        """
        label = colored("[claudio:error]", RED, stream=sys.stderr, bold=True)
        body = colored(message, RED, stream=sys.stderr)
        print(f"{label} {body}", file=sys.stderr)

    def _print_metadata(self, metadata: dict) -> None:
        for key, value in metadata.items():
            print(f"  {key}: {value}", file=sys.stderr)
        print("---", file=sys.stderr)
