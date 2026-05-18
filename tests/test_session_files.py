"""Tests for per-session file-tracking + unchanged-file marker."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from claudio import session_files
from claudio.utils.args import FileAttachment, format_file_context


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDIO_HOME", str(tmp_path))
    yield


def _fa(path: str, content: str, lines: str | None = None) -> FileAttachment:
    return FileAttachment(path=path, lines=lines, content=content)


# ---- mark_files_seen ---------------------------------------------------

def test_first_call_records_and_returns_empty():
    files = [_fa("main.py", "x = 1\n")]
    unchanged = session_files.mark_files_seen("sess-1", files)
    assert unchanged == set()


def test_second_call_with_same_content_returns_unchanged():
    files = [_fa("main.py", "x = 1\n")]
    session_files.mark_files_seen("sess-1", files)
    unchanged = session_files.mark_files_seen("sess-1", files)
    assert ("main.py", None) in unchanged


def test_changed_content_is_not_unchanged():
    session_files.mark_files_seen("sess-1", [_fa("main.py", "x = 1\n")])
    unchanged = session_files.mark_files_seen("sess-1", [_fa("main.py", "x = 2\n")])
    assert unchanged == set()
    # And the third call with the *new* content is now unchanged
    unchanged = session_files.mark_files_seen("sess-1", [_fa("main.py", "x = 2\n")])
    assert ("main.py", None) in unchanged


def test_different_line_ranges_track_separately():
    f_full = _fa("a.py", "full content")
    f_slice = _fa("a.py", "slice content", lines="1-10")
    session_files.mark_files_seen("sess-1", [f_full, f_slice])
    # Both already seen now; second pass returns both
    unchanged = session_files.mark_files_seen("sess-1", [f_full, f_slice])
    assert ("a.py", None) in unchanged
    assert ("a.py", "1-10") in unchanged


def test_different_sessions_dont_share_memory():
    session_files.mark_files_seen("sess-A", [_fa("main.py", "data")])
    unchanged = session_files.mark_files_seen("sess-B", [_fa("main.py", "data")])
    assert unchanged == set()  # sess-B has never seen it


def test_empty_session_id_is_a_no_op():
    files = [_fa("main.py", "x")]
    assert session_files.mark_files_seen(None, files) == set()
    assert session_files.mark_files_seen("", files) == set()


def test_empty_content_is_skipped():
    files = [_fa("main.py", "")]
    session_files.mark_files_seen("sess-1", files)
    # No record written because the file had no content
    assert session_files._load("sess-1")["files"] == {}


# ---- clear -------------------------------------------------------------

def test_clear_removes_session_file():
    session_files.mark_files_seen("sess-1", [_fa("main.py", "x")])
    assert session_files._path("sess-1").exists()
    session_files.clear("sess-1")
    assert not session_files._path("sess-1").exists()


def test_clear_with_no_record_is_silent():
    session_files.clear("never-existed")  # must not raise


def test_clear_with_no_session_id_is_silent():
    session_files.clear(None)
    session_files.clear("")


# ---- format_file_context with unchanged flag --------------------------

def test_format_emits_self_closing_marker_for_unchanged():
    fa = FileAttachment(path="main.py", content="x = 1", unchanged=True)
    out = format_file_context([fa])
    assert 'path="main.py"' in out
    assert 'unchanged="true"' in out
    assert out.endswith("/>")
    # The body is NOT inlined
    assert "x = 1" not in out


def test_format_inlines_full_body_for_fresh_files():
    fa = FileAttachment(path="main.py", content="x = 1", unchanged=False)
    out = format_file_context([fa])
    assert 'path="main.py"' in out
    assert "x = 1" in out
    assert "unchanged" not in out


def test_format_preserves_line_attr_on_marker():
    fa = FileAttachment(path="main.py", content="x", lines="1-50", unchanged=True)
    out = format_file_context([fa])
    assert 'lines="1-50"' in out
    assert 'unchanged="true"' in out


def test_format_assigns_target_role_to_first_file_only():
    a = FileAttachment(path="a.py", content="x")
    b = FileAttachment(path="b.py", content="y")
    c = FileAttachment(path="c.py", content="z")
    out = format_file_context([a, b, c])
    assert out.count('role="target"') == 1
    assert out.count('role="context"') == 2
    # Target must be the first file in the output
    target_pos = out.find('role="target"')
    context_pos = out.find('role="context"')
    assert target_pos < context_pos


# ---- corrupt session file recovers gracefully ------------------------

def test_load_returns_empty_on_corrupt_session_file():
    p = session_files._path("broken")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json", encoding="utf-8")
    # Should not raise; treats as empty session
    assert session_files._load("broken") == {"files": {}}
