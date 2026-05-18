"""Tests for stack auto-detection from manifest files."""

from __future__ import annotations

import json

import pytest

from claudio.utils.stack_detect import detect_stack


def test_no_manifests_returns_empty(tmp_path):
    assert detect_stack(cwd=tmp_path) == ""


def test_pyproject_pep621(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\n'
        'name = "myapp"\n'
        'requires-python = ">=3.10"\n'
        'dependencies = ["django>=4.2", "psycopg[binary]>=3", "celery"]\n',
        encoding="utf-8",
    )
    out = detect_stack(cwd=tmp_path)
    assert "Python" in out
    assert ">=3.10" in out
    assert "myapp" in out
    assert "Django" in out
    assert "django" in out
    assert "celery" in out


def test_pyproject_poetry(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "x"\n'
        '[tool.poetry.dependencies]\n'
        'python = "^3.11"\n'
        'fastapi = "^0.110"\n'
        'sqlalchemy = "^2.0"\n',
        encoding="utf-8",
    )
    out = detect_stack(cwd=tmp_path)
    assert "Python" in out
    assert "FastAPI" in out
    assert "fastapi" in out
    assert "sqlalchemy" in out
    # python = "^3.11" must NOT be reported as a dep
    assert "python" not in out.replace("Python", "")


def test_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "flask==3.0\n# comment\nrequests>=2.31\n\n-r dev.txt\n",
        encoding="utf-8",
    )
    out = detect_stack(cwd=tmp_path)
    assert "Python" in out
    assert "requirements.txt" in out
    assert "Flask" in out
    assert "flask" in out
    assert "requests" in out


def test_package_json_typescript(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "name": "myapp",
            "engines": {"node": ">=20"},
            "dependencies": {"next": "14", "react": "18"},
            "devDependencies": {"typescript": "5"},
        }),
        encoding="utf-8",
    )
    out = detect_stack(cwd=tmp_path)
    assert "TypeScript" in out
    assert "node >=20" in out
    assert "Next.js" in out
    assert "React" in out


def test_package_json_javascript_without_typescript(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"express": "4"}}),
        encoding="utf-8",
    )
    out = detect_stack(cwd=tmp_path)
    assert "JavaScript" in out
    assert "Express" in out


def test_cargo_toml(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "engine"\nedition = "2021"\n',
        encoding="utf-8",
    )
    out = detect_stack(cwd=tmp_path)
    assert "Rust" in out
    assert "edition 2021" in out
    assert "engine" in out


def test_go_mod(tmp_path):
    (tmp_path / "go.mod").write_text(
        "module github.com/foo/bar\n\ngo 1.22\n",
        encoding="utf-8",
    )
    out = detect_stack(cwd=tmp_path)
    assert "Go" in out
    assert "1.22" in out
    assert "github.com/foo/bar" in out


def test_multi_manifest_combined(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "api"\ndependencies = ["fastapi"]\n', encoding="utf-8"
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "ui", "dependencies": {"react": "18"}}), encoding="utf-8"
    )
    out = detect_stack(cwd=tmp_path)
    assert "Python" in out
    assert "JavaScript" in out
    assert "FastAPI" in out
    assert "React" in out


def test_env_disables_detection(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDIO_NO_STACK_DETECT", "1")
    assert detect_stack(cwd=tmp_path) == ""


def test_malformed_package_json_returns_empty_for_that_source(tmp_path):
    (tmp_path / "package.json").write_text("{ not json", encoding="utf-8")
    # Should not raise; the python-side may still produce output if present
    out = detect_stack(cwd=tmp_path)
    assert out == ""
