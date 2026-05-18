"""Auto-detect language/framework/tooling from manifest files.

Reads the usual suspects -- pyproject.toml, package.json, Cargo.toml,
go.mod, requirements.txt -- and produces a short, factual stack
description. The output is plain prose, capped, and intended to be
concatenated into the <project> preamble so it lands in the cached
prompt prefix.

Detection is conservative: we only report what is provable from a file
we can read. No guessing from directory names.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_MAX_PACKAGES_LISTED = 12
_MAX_OUTPUT_CHARS = 1_200


def detect_stack(cwd: str | os.PathLike | None = None) -> str:
    """Return a short stack description, or '' if nothing detected."""
    if os.environ.get("CLAUDIO_NO_STACK_DETECT"):
        return ""

    root = Path(cwd) if cwd else Path.cwd()
    lines: list[str] = []

    for detector in (
        _detect_python,
        _detect_node,
        _detect_rust,
        _detect_go,
    ):
        line = detector(root)
        if line:
            lines.append(line)

    if not lines:
        return ""

    out = "\n".join(lines).strip()
    if len(out) > _MAX_OUTPUT_CHARS:
        out = out[: _MAX_OUTPUT_CHARS - 15] + "\n... [truncated]"
    return out


def _detect_python(root: Path) -> str:
    pyproject = root / "pyproject.toml"
    requirements = root / "requirements.txt"
    setup_py = root / "setup.py"

    if pyproject.is_file():
        text = _read(pyproject)
        return _python_from_pyproject(text)
    if requirements.is_file():
        text = _read(requirements)
        return _python_from_requirements(text)
    if setup_py.is_file():
        return "Python (setup.py)"
    return ""


def _python_from_pyproject(text: str) -> str:
    if not text:
        return ""
    # python version
    py_version = ""
    m = re.search(r'(?:requires-python|python)\s*=\s*["\']([^"\']+)["\']', text)
    if m:
        py_version = m.group(1)

    # project name
    name = ""
    m = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if m:
        name = m.group(1)

    # dependencies: grab from [project.dependencies] or tool.poetry.dependencies
    deps = _extract_dep_names(text)

    # Detect well-known frameworks from deps
    framework = _identify_framework(deps)

    parts = ["Python"]
    if py_version:
        parts.append(py_version)
    parts.append("(pyproject.toml)")
    line = " ".join(parts)
    if name:
        line += f" — project: {name}"
    if framework:
        line += f"; framework: {framework}"
    if deps:
        line += f"; deps: {', '.join(deps[:_MAX_PACKAGES_LISTED])}"
        if len(deps) > _MAX_PACKAGES_LISTED:
            line += f" (+{len(deps) - _MAX_PACKAGES_LISTED} more)"
    return line


def _python_from_requirements(text: str) -> str:
    deps = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        # Strip extras + version specifier
        name = re.split(r"[<>=!~\[]", line, maxsplit=1)[0].strip()
        if name:
            deps.append(name)
    line = "Python (requirements.txt)"
    framework = _identify_framework(deps)
    if framework:
        line += f"; framework: {framework}"
    if deps:
        line += f"; deps: {', '.join(deps[:_MAX_PACKAGES_LISTED])}"
        if len(deps) > _MAX_PACKAGES_LISTED:
            line += f" (+{len(deps) - _MAX_PACKAGES_LISTED} more)"
    return line


def _extract_dep_names(pyproject_text: str) -> list[str]:
    """Yank dependency names from a pyproject.toml without parsing TOML.

    We accept either PEP 621 `dependencies = ["foo", "bar>=1"]` form or
    Poetry's `[tool.poetry.dependencies]` table. A regex scan is good
    enough -- we only need the names.
    """
    names: list[str] = []

    # PEP 621 array form: dependencies = [ "foo>=1", "bar[extra]>=2" ]
    # Bracket-aware scan so deps with extras like `psycopg[binary]` don't
    # truncate the array prematurely.
    array_body = _slice_balanced_array(pyproject_text, "dependencies")
    if array_body:
        for item in re.findall(r'["\']([^"\']+)["\']', array_body):
            name = re.split(r"[<>=!~\[]", item, maxsplit=1)[0].strip()
            if name:
                names.append(name)

    # Poetry table: keys before `=` (skip python = "...")
    poetry_block = re.search(
        r"\[tool\.poetry\.dependencies\]([^\[]*)", pyproject_text, re.DOTALL
    )
    if poetry_block:
        for key in re.findall(r"^([A-Za-z0-9_\-]+)\s*=", poetry_block.group(1), re.MULTILINE):
            if key.lower() != "python":
                names.append(key)

    # Dedup but preserve order
    seen = set()
    out: list[str] = []
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def _slice_balanced_array(text: str, key: str) -> str:
    """Return the content between `[` and matching `]` for `key = [...]`.

    Handles bracket nesting inside quoted strings (e.g. `psycopg[binary]`).
    Returns '' when the key isn't found or the brackets don't balance.
    """
    m = re.search(rf"\b{re.escape(key)}\s*=\s*\[", text)
    if not m:
        return ""
    start = m.end()  # position just after the opening [
    depth = 1
    in_quote = ""
    i = start
    while i < len(text):
        ch = text[i]
        if in_quote:
            if ch == in_quote and text[i - 1] != "\\":
                in_quote = ""
        elif ch in ("'", '"'):
            in_quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i]
        i += 1
    return ""


def _identify_framework(deps: list[str]) -> str:
    """Tag the well-known Python/JS frameworks from a dep list."""
    lower = {d.lower() for d in deps}
    candidates = [
        ("Django", "django"),
        ("Flask", "flask"),
        ("FastAPI", "fastapi"),
        ("Starlette", "starlette"),
        ("Pyramid", "pyramid"),
        ("Tornado", "tornado"),
        ("Next.js", "next"),
        ("React", "react"),
        ("Vue", "vue"),
        ("Svelte", "svelte"),
        ("Express", "express"),
        ("NestJS", "@nestjs/core"),
        ("Remix", "@remix-run/react"),
    ]
    hits = [label for label, key in candidates if key in lower]
    return ", ".join(hits) if hits else ""


def _detect_node(root: Path) -> str:
    pkg = root / "package.json"
    if not pkg.is_file():
        return ""
    try:
        data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return ""
    if not isinstance(data, dict):
        return ""

    name = data.get("name", "") if isinstance(data.get("name"), str) else ""
    deps_dict = {}
    for key in ("dependencies", "devDependencies"):
        d = data.get(key)
        if isinstance(d, dict):
            deps_dict.update(d)
    deps = list(deps_dict.keys())

    engines = data.get("engines") if isinstance(data.get("engines"), dict) else {}
    node_ver = engines.get("node", "") if isinstance(engines, dict) else ""

    # TypeScript heuristic
    is_ts = "typescript" in {d.lower() for d in deps} or (root / "tsconfig.json").is_file()
    lang = "TypeScript" if is_ts else "JavaScript"

    line = f"{lang} (package.json)"
    if node_ver:
        line += f"; node {node_ver}"
    if name:
        line += f" — project: {name}"
    framework = _identify_framework(deps)
    if framework:
        line += f"; framework: {framework}"
    if deps:
        line += f"; deps: {', '.join(deps[:_MAX_PACKAGES_LISTED])}"
        if len(deps) > _MAX_PACKAGES_LISTED:
            line += f" (+{len(deps) - _MAX_PACKAGES_LISTED} more)"
    return line


def _detect_rust(root: Path) -> str:
    cargo = root / "Cargo.toml"
    if not cargo.is_file():
        return ""
    text = _read(cargo)
    name = ""
    m = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if m:
        name = m.group(1)
    edition = ""
    m = re.search(r'^\s*edition\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if m:
        edition = m.group(1)
    line = "Rust (Cargo.toml)"
    if edition:
        line += f"; edition {edition}"
    if name:
        line += f" — crate: {name}"
    return line


def _detect_go(root: Path) -> str:
    gomod = root / "go.mod"
    if not gomod.is_file():
        return ""
    text = _read(gomod)
    module = ""
    m = re.search(r"^\s*module\s+(\S+)", text, re.MULTILINE)
    if m:
        module = m.group(1)
    go_ver = ""
    m = re.search(r"^\s*go\s+(\S+)", text, re.MULTILINE)
    if m:
        go_ver = m.group(1)
    line = "Go (go.mod)"
    if go_ver:
        line += f" {go_ver}"
    if module:
        line += f" — module: {module}"
    return line


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
