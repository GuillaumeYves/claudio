"""Semantic compression -- reduce large inputs to structured summaries.

Compression thresholds:
  - Code: 300+ lines -> structural map + imports (with target body preserved)
  - Logs: 150+ lines -> errors/warnings + tail
  - Import blocks: always collapsed to one-liner

The goal is maximum information density. Claude doesn't need to see
every line to understand code structure -- a map of classes, functions,
and their line numbers is often more useful than raw code.

Symbol-aware compression: when the caller passes `task_text`, any symbol
named in the task that also appears in the structural map gets its body
preserved verbatim. Reviewing `validate_token` without seeing
`validate_token`'s code is useless; this avoids that failure mode.
"""

import re
from pathlib import Path


# Was 50, which was way too aggressive -- a 60-line file lost all its code
# and became a 6-line table-of-contents. 300 lines covers most real source
# files; only genuinely large modules get compressed.
CODE_COMPRESS_THRESHOLD = 300
LOG_COMPRESS_THRESHOLD = 150

# How many leading non-target structures to enumerate before
# truncating with "...". Keeps the map readable on monster files.
_MAX_STRUCTURE_ROWS = 60

_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]+)\b")


def compress_code(content: str, filename: str = "", task_text: str = "") -> str:
    """Compress code by extracting structural elements.

    For files above threshold, produces:
      1. File header (filename, line count)
      2. Full import lines (preserved verbatim — aliases matter)
      3. Structural map (classes, functions with line numbers)
      4. Verbatim bodies of any symbol named in `task_text`
    """
    lines = content.splitlines()
    if len(lines) <= CODE_COMPRESS_THRESHOLD:
        # Still collapse imports even for small files
        return _collapse_imports(content)

    ext = Path(filename).suffix.lower() if filename else ""
    structures = extract_structures(content, ext)

    if not structures:
        return _collapse_imports(content)

    parts = []
    if filename:
        parts.append(f"[{filename} | {len(lines)} lines]")

    # Preserve full import lines -- module names alone lose `as` aliases.
    import_lines = _extract_import_lines(lines, ext)
    if import_lines:
        parts.append("imports:")
        parts.extend(import_lines)

    # Structural map -- compact format, capped for huge files
    parts.append("")
    parts.append("structure:")
    for s in structures[:_MAX_STRUCTURE_ROWS]:
        indent = "  " * s.get("depth", 0)
        parts.append(f"{indent}{s['type']} {s['name']} @{s['line']}")
    if len(structures) > _MAX_STRUCTURE_ROWS:
        parts.append(f"... +{len(structures) - _MAX_STRUCTURE_ROWS} more symbols")

    # Symbol-aware preservation: any identifier in <task> that matches a
    # structure becomes a kept body. Claude reviewing `validate_token` needs
    # to see `validate_token`, not just its line number.
    if task_text:
        targets = _resolve_target_symbols(task_text, structures)
        if targets:
            parts.append("")
            parts.append("target bodies:")
            for t in targets:
                body = _extract_body(lines, t, structures)
                if body:
                    parts.append(
                        f"<{t['type']} name=\"{t['name']}\" "
                        f"lines=\"{t['line']}-{t['end']}\">"
                    )
                    parts.append(body)
                    parts.append(f"</{t['type']}>")

    return "\n".join(parts)


def _resolve_target_symbols(task_text: str, structures: list[dict]) -> list[dict]:
    """Return structures whose names are mentioned in the task text.

    Matches whole identifiers only -- "user" doesn't match "user_id". Keeps
    the structural list ordering so output is stable.
    """
    if not task_text or not structures:
        return []
    mentioned = set(_IDENT_RE.findall(task_text))
    if not mentioned:
        return []
    return [s for s in structures if s["name"] in mentioned]


def _extract_body(
    lines: list[str],
    target: dict,
    structures: list[dict],
) -> str:
    """Return the verbatim source of one target symbol.

    Body runs from the target's start line until the next sibling structure
    (same or shallower depth) or end of file. The `end` line is recorded
    on the target dict for the caller to display.
    """
    start_idx = target["line"] - 1
    depth = target.get("depth", 0)
    end_idx = len(lines)

    # Find next structure at same-or-shallower depth
    for s in structures:
        if s["line"] <= target["line"]:
            continue
        if s.get("depth", 0) <= depth:
            end_idx = s["line"] - 1
            break

    # For indentation-based languages (Python), trim trailing blank lines
    snippet = lines[start_idx:end_idx]
    while snippet and not snippet[-1].strip():
        snippet.pop()

    target["end"] = start_idx + len(snippet)
    return "\n".join(snippet)


def compress_logs(content: str, max_lines: int = LOG_COMPRESS_THRESHOLD) -> str:
    """Compress logs: keep errors/warnings, summarize info lines."""
    lines = content.splitlines()
    if len(lines) <= max_lines:
        return content

    errors = []
    warnings = []
    other_count = 0

    for line in lines:
        if re.search(r"\b(ERROR|FATAL|CRITICAL|Exception|Traceback|panic)\b", line, re.IGNORECASE):
            errors.append(line)
        elif re.search(r"\b(WARN|WARNING)\b", line, re.IGNORECASE):
            warnings.append(line)
        else:
            other_count += 1

    parts = [f"[{len(lines)} lines total]"]

    if errors:
        parts.append(f"\nERRORS ({len(errors)}):")
        for e in errors[:50]:
            parts.append(e)

    if warnings:
        parts.append(f"\nWARNINGS ({len(warnings)}):")
        for w in warnings[:20]:
            parts.append(w)

    if other_count:
        parts.append(f"\n{other_count} info/debug lines omitted")

    # Last 15 lines for recency
    parts.append("\nTAIL:")
    parts.extend(lines[-15:])

    return "\n".join(parts)


def _collapse_imports(content: str) -> str:
    """Collapse import blocks into a compact summary within code."""
    lines = content.splitlines()
    result = []
    import_names = []
    in_import_block = False

    for line in lines:
        stripped = line.strip()
        is_import = (
            stripped.startswith("import ")
            or stripped.startswith("from ")
            or (in_import_block and (stripped.startswith(",") or stripped.endswith(",")))
            or stripped == ")"
        )

        if is_import:
            if not in_import_block:
                in_import_block = True
            # Extract module names
            m = re.match(r"(?:from|import)\s+([\w.]+)", stripped)
            if m:
                import_names.append(m.group(1))
        else:
            if in_import_block:
                # Flush collected imports as one line
                if import_names:
                    result.append(f"# imports: {', '.join(import_names)}")
                    import_names = []
                in_import_block = False
            result.append(line)

    # Handle trailing imports
    if import_names:
        result.append(f"# imports: {', '.join(import_names)}")

    return "\n".join(result)


def _extract_import_lines(lines: list[str], ext: str) -> list[str]:
    """Return full import-statement lines verbatim.

    Preserves `as` aliases, multi-name imports, and re-exports — all
    information that bare module names throw away. Capped so a file with
    200 imports doesn't dominate the compressed view.
    """
    imports: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if ext in (".py", ""):
            if stripped.startswith("import ") or stripped.startswith("from "):
                imports.append(stripped)
        elif ext in (".js", ".ts", ".jsx", ".tsx"):
            if stripped.startswith("import ") or stripped.startswith("export "):
                imports.append(stripped)
        elif ext == ".go":
            if stripped.startswith("import ") or stripped.startswith('"'):
                imports.append(stripped)
        if len(imports) >= 40:
            imports.append("... (more imports omitted)")
            break
    return imports


def extract_structures(content: str, ext: str = "") -> list[dict]:
    """Extract structural elements (classes, functions, etc.) from code."""
    structures = []
    lines = content.splitlines()

    for i, line in enumerate(lines, 1):
        # Python
        if ext in (".py", ""):
            if m := re.match(r"^(\s*)class\s+(\w+)", line):
                depth = len(m.group(1)) // 4
                structures.append({"type": "class", "name": m.group(2), "line": i, "depth": depth})
            elif m := re.match(r"^(\s*)def\s+(\w+)", line):
                depth = len(m.group(1)) // 4
                structures.append({"type": "fn", "name": m.group(2), "line": i, "depth": depth})

        # JavaScript/TypeScript
        if ext in (".js", ".ts", ".jsx", ".tsx", ""):
            if m := re.match(r"^(\s*)(?:export\s+)?(?:async\s+)?function\s+(\w+)", line):
                structures.append({"type": "fn", "name": m.group(2), "line": i, "depth": 0})
            elif m := re.match(r"^(\s*)(?:export\s+)?class\s+(\w+)", line):
                structures.append({"type": "class", "name": m.group(2), "line": i, "depth": 0})
            elif m := re.match(r"^(\s*)(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s+)?\(", line):
                structures.append({"type": "fn", "name": m.group(2), "line": i, "depth": 0})

        # Go
        if ext == ".go":
            if m := re.match(r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)", line):
                structures.append({"type": "fn", "name": m.group(1), "line": i, "depth": 0})
            elif m := re.match(r"^type\s+(\w+)\s+struct", line):
                structures.append({"type": "struct", "name": m.group(1), "line": i, "depth": 0})

    return structures
