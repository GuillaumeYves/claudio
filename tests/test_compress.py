"""Tests for compression with symbol-aware preservation."""

from __future__ import annotations

from claudio.pipeline.compress import (
    CODE_COMPRESS_THRESHOLD,
    compress_code,
    _extract_import_lines,
    _resolve_target_symbols,
)


def _big_py(num_funcs: int = 50, body_per_func: int = 8) -> str:
    """Build a Python source file with `num_funcs` functions, enough lines
    to clear CODE_COMPRESS_THRESHOLD."""
    out = ["import os", "import sys", "from pathlib import Path", ""]
    for i in range(num_funcs):
        out.append(f"def func_{i}(x):")
        for j in range(body_per_func):
            out.append(f"    step_{j} = x + {j}")
        out.append("")
    return "\n".join(out)


def test_small_file_skips_structural_compression():
    src = "def hello():\n    return 1\n"
    out = compress_code(src, "tiny.py")
    # Small file -- structure is not built, body is preserved
    assert "def hello():" in out
    assert "structure:" not in out


def test_large_file_triggers_compression():
    src = _big_py(num_funcs=50)
    assert len(src.splitlines()) > CODE_COMPRESS_THRESHOLD
    out = compress_code(src, "big.py")
    assert "structure:" in out
    assert "fn func_0 @" in out


def test_imports_preserve_full_lines_with_aliases():
    src = "import pandas as pd\nimport numpy as np\nfrom typing import List\n" + _big_py(num_funcs=50)
    out = compress_code(src, "big.py")
    assert "import pandas as pd" in out
    assert "import numpy as np" in out
    # The pre-existing summary form `imports: pandas, numpy` must NOT appear
    assert "imports: pandas" not in out


def test_target_symbol_body_preserved():
    """When task names a function, its body must come through verbatim."""
    src = _big_py(num_funcs=50)
    out = compress_code(src, "big.py", task_text="Refactor func_7")
    # Structural map still present
    assert "fn func_7 @" in out
    # And the actual body is preserved
    assert "def func_7(x):" in out
    assert "target bodies:" in out


def test_unrelated_task_does_not_inflate_output():
    """No matching symbol -> no target-bodies section."""
    src = _big_py(num_funcs=50)
    out = compress_code(src, "big.py", task_text="Generate a CSV writer")
    assert "target bodies:" not in out


def test_target_match_uses_whole_words():
    """Substring matches must not trigger -- `func` shouldn't pull `func_1`."""
    src = _big_py(num_funcs=10)
    structures = [{"type": "fn", "name": "validate_token", "line": 1, "depth": 0}]
    # The word "validate" alone must NOT pull validate_token
    assert _resolve_target_symbols("validate the input", structures) == []
    # But the full name does
    assert len(_resolve_target_symbols("review validate_token", structures)) == 1


def test_extract_import_lines_python():
    lines = [
        "import os",
        "from typing import List, Dict",
        "import pandas as pd",
        "",
        "def foo(): pass",
    ]
    out = _extract_import_lines(lines, ".py")
    assert out == ["import os", "from typing import List, Dict", "import pandas as pd"]
