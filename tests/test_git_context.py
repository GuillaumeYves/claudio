"""Tests for auto git-diff context discovery."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from claudio.utils.git_context import discover_git_changes


def _git_available() -> bool:
    return shutil.which("git") is not None


requires_git = pytest.mark.skipif(not _git_available(), reason="git not on PATH")


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )


def _init_repo(path):
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@test")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")


def test_returns_empty_outside_git_repo(tmp_path):
    assert discover_git_changes(intent="review", cwd=tmp_path) == ""


def test_returns_empty_when_intent_excluded(tmp_path):
    # Even in a fresh repo, intent=general must skip
    (tmp_path / "x").write_text("x", encoding="utf-8")
    assert discover_git_changes(intent="general", cwd=tmp_path) == ""
    assert discover_git_changes(intent="generate", cwd=tmp_path) == ""


@requires_git
def test_uncommitted_changes_appear(tmp_path):
    _init_repo(tmp_path)
    f = tmp_path / "app.py"
    f.write_text("def foo(): return 1\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-q", "-m", "init")

    # Modify the file -- now there's an uncommitted diff
    f.write_text("def foo(): return 2\n", encoding="utf-8")

    out = discover_git_changes(intent="review", cwd=tmp_path)
    assert out != ""
    assert "uncommitted" in out
    assert "app.py" in out
    assert "return 2" in out


@requires_git
def test_clean_repo_returns_empty(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a").write_text("a", encoding="utf-8")
    _git(tmp_path, "add", "a")
    _git(tmp_path, "commit", "-q", "-m", "init")
    out = discover_git_changes(intent="review", cwd=tmp_path)
    assert out == ""


@requires_git
def test_env_disables_git_context(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    f = tmp_path / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "x.py")
    _git(tmp_path, "commit", "-q", "-m", "init")
    f.write_text("x = 2\n", encoding="utf-8")

    monkeypatch.setenv("CLAUDIO_NO_GIT_CONTEXT", "1")
    assert discover_git_changes(intent="review", cwd=tmp_path) == ""


@requires_git
def test_branch_diff_against_main(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "x.py").write_text("a = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "x.py")
    _git(tmp_path, "commit", "-q", "-m", "init")

    # Branch off, commit a change
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "x.py").write_text("a = 2\n", encoding="utf-8")
    _git(tmp_path, "commit", "-aq", "-m", "tweak")

    out = discover_git_changes(intent="debug", cwd=tmp_path)
    assert "branch vs main" in out
    assert "a = 2" in out
