"""Semantic compression -- reduce large inputs to structured summaries.

Compression thresholds:
  - Code: 50+ lines -> structural map + imports
  - Logs: 150+ lines -> errors/warnings + tail
  - Import blocks: always collapsed to one-liner

The goal is maximum information density. Claude doesn't need to see
every line to understand code structure -- a map of classes, functions,
and their line numbers is often more useful than raw code.
"""

import re
from pathlib import Path


# Lowered from 100 -- this is an optimization tool, optimize early
CODE_COMPRESS_THRESHOLD = 50
LOG_COMPRESS_THRESHOLD = 150


def compress_code(content: str, filename: str = "") -> str:
    """Compress code by extracting structural elements.

    For files above threshold, produces:
      1. File header (filename, line count)
      2. Import summary (collapsed to one line)
      3. Structural map (classes, functions with line numbers)
      4. First meaningful code section (after imports)
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

    # Collapse imports into a summary
    import_summary = _summarize_imports(lines, ext)
    if import_summary:
        parts.append(f"imports: {import_summary}")

    # Structural map -- compact format
    parts.append("")
    for s in structures:
        indent = "  " * s.get("depth", 0)
        parts.append(f"{indent}{s['type']} {s['name']} @{s['line']}")

    return "\n".join(parts)


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


def _summarize_imports(lines: list[str], ext: str) -> str:
    """Extract import module names as a comma-separated string."""
    modules = []
    for line in lines:
        stripped = line.strip()
        if ext in (".py", ""):
            m = re.match(r"(?:from|import)\s+([\w.]+)", stripped)
            if m:
                modules.append(m.group(1))
        elif ext in (".js", ".ts", ".jsx", ".tsx"):
            m = re.match(r"import\s+.*from\s+['\"]([^'\"]+)", stripped)
            if m:
                modules.append(m.group(1))
        elif ext == ".go":
            m = re.match(r'\s*"([^"]+)"', stripped)
            if m and any(l.strip().startswith("import") for l in lines[:5]):
                modules.append(m.group(1))
    return ", ".join(modules) if modules else ""


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
