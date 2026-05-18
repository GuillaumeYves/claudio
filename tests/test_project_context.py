"""Tests for project preamble discovery (CLAUDE.md + .claudio/project.md)."""

from __future__ import annotations

import pytest

from claudio.utils.project_context import discover_project_preamble


def test_returns_empty_when_no_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert discover_project_preamble() == ""


def test_reads_claude_md(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("Django 4.2, pytest.", encoding="utf-8")
    out = discover_project_preamble()
    assert "Django 4.2, pytest." in out
    assert "CLAUDE.md" in out


def test_reads_claudio_project_md(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".claudio").mkdir()
    (tmp_path / ".claudio" / "project.md").write_text("Use black.", encoding="utf-8")
    out = discover_project_preamble()
    assert "Use black." in out
    assert "project.md" in out


def test_project_md_before_claude_md_when_both_present(tmp_path, monkeypatch):
    """The narrower .claudio/project.md takes precedence in ordering -- it
    is the file the user wrote specifically for claudio."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("CLAUDE-CONTENT", encoding="utf-8")
    (tmp_path / ".claudio").mkdir()
    (tmp_path / ".claudio" / "project.md").write_text("PROJECT-CONTENT", encoding="utf-8")
    out = discover_project_preamble()
    assert out.index("PROJECT-CONTENT") < out.index("CLAUDE-CONTENT")


def test_truncates_huge_preamble(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("A" * 5_000, encoding="utf-8")
    out = discover_project_preamble()
    assert "[truncated]" in out
    assert len(out) <= 2_100  # cap + tiny header overhead


def test_env_disables_preamble(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("content", encoding="utf-8")
    monkeypatch.setenv("CLAUDIO_NO_PREAMBLE", "1")
    assert discover_project_preamble() == ""


def test_accepts_explicit_cwd(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("hi", encoding="utf-8")
    out = discover_project_preamble(cwd=tmp_path)
    assert "hi" in out


def test_unreadable_path_returns_empty(tmp_path, monkeypatch):
    """If the file doesn't exist or can't be read, return ''."""
    monkeypatch.chdir(tmp_path)
    # Make sure neither file exists -- function must return empty cleanly.
    assert not (tmp_path / "CLAUDE.md").exists()
    assert discover_project_preamble() == ""
