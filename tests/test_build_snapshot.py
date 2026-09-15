"""Tests for pre-build snapshots and /undo restore."""


import pytest

from claudio import build_snapshot


@pytest.fixture(autouse=True)
def _in_tmp_cwd(tmp_path, monkeypatch):
    """Run each test in an isolated cwd so .claudio/undo/ is scoped to it."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def test_no_snapshot_returns_none():
    assert build_snapshot.has_snapshot() is False
    assert build_snapshot.undo() is None


def test_snapshot_then_undo_restores_modified_file(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("original\n", encoding="utf-8")

    build_snapshot.snapshot(["code.py"])
    assert build_snapshot.has_snapshot() is True

    f.write_text("clobbered by build\n", encoding="utf-8")
    restored, deleted, errors = build_snapshot.undo()

    assert restored == ["code.py"]
    assert deleted == []
    assert errors == []
    assert f.read_text(encoding="utf-8") == "original\n"


def test_undo_deletes_build_created_file(tmp_path):
    # Target does not exist at snapshot time -> generate creates it -> undo removes it.
    build_snapshot.snapshot(["new_module.py"])
    created = tmp_path / "new_module.py"
    created.write_text("def f(): ...\n", encoding="utf-8")

    restored, deleted, errors = build_snapshot.undo()

    assert restored == []
    assert deleted == ["new_module.py"]
    assert not created.exists()


def test_undo_is_consumed_after_apply(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x", encoding="utf-8")
    build_snapshot.snapshot(["a.py"])

    assert build_snapshot.undo() is not None
    # Second undo finds nothing — the manifest was consumed.
    assert build_snapshot.has_snapshot() is False
    assert build_snapshot.undo() is None


def test_snapshot_overwrites_previous(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v1", encoding="utf-8")
    build_snapshot.snapshot(["a.py"])

    f.write_text("v2", encoding="utf-8")
    build_snapshot.snapshot(["a.py"])  # newer snapshot wins

    f.write_text("v3", encoding="utf-8")
    build_snapshot.undo()
    assert f.read_text(encoding="utf-8") == "v2"


def test_empty_paths_writes_no_snapshot():
    build_snapshot.snapshot([])
    assert build_snapshot.has_snapshot() is False


def test_binary_content_roundtrips(tmp_path):
    f = tmp_path / "blob.bin"
    original = bytes(range(256))
    f.write_bytes(original)
    build_snapshot.snapshot(["blob.bin"])

    f.write_bytes(b"corrupted")
    build_snapshot.undo()
    assert f.read_bytes() == original
