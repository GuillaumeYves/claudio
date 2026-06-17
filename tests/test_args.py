"""Tests for argument parsing, including the -f/--file PowerShell-safe alias."""

from __future__ import annotations

from claudio.utils.args import (
    _normalize_file_flags,
    _suggest_order,
    parse_command_args,
    resolve_file_attachments,
    FileAttachment,
)


BUILD_MODES = {"r": "refactor", "refactor": "refactor",
               "g": "generate", "generate": "generate"}


# ---- _normalize_file_flags ----------------------------------------------

def test_normalize_dash_f_becomes_at():
    assert _normalize_file_flags(["-f", "main.py"]) == ["@main.py"]


def test_normalize_double_dash_file_becomes_at():
    assert _normalize_file_flags(["--file", "src/app.py"]) == ["@src/app.py"]


def test_normalize_passes_through_unrelated_args():
    args = ["-r", "@existing.py", "-10-25", "msg"]
    assert _normalize_file_flags(args) == args


def test_normalize_handles_trailing_f_with_no_value():
    # If -f is the last token, it can't be paired -- leave it for the parser
    # to ignore or complain about, don't index out of bounds.
    assert _normalize_file_flags(["-r", "-f"]) == ["-r", "-f"]


def test_normalize_multiple_f_flags():
    result = _normalize_file_flags(["-f", "a.py", "-f", "b.py"])
    assert result == ["@a.py", "@b.py"]


def test_normalize_f_preserves_line_range_position():
    # -f path must be collapsed to @path BEFORE the -N-N range so the range
    # still attaches to the file.
    result = _normalize_file_flags(["-r", "-f", "main.py", "-10-25", "msg"])
    assert result == ["-r", "@main.py", "-10-25", "msg"]


# ---- parse_command_args with -f / --file --------------------------------

def test_parse_f_flag_attaches_line_range():
    p = parse_command_args(
        ["-r", "-f", "main.py", "-10-25", "reduce complexity"],
        BUILD_MODES,
    )
    assert p.mode == "refactor"
    assert p.errors == []
    assert len(p.files) == 1
    assert p.files[0].path == "main.py"
    assert p.files[0].lines == "10-25"
    assert p.prompt == "reduce complexity"


def test_parse_double_dash_file():
    p = parse_command_args(
        ["-g", "--file", "models/user.py", "add endpoint"],
        BUILD_MODES,
    )
    assert p.mode == "generate"
    assert p.files[0].path == "models/user.py"
    assert p.files[0].lines is None


def test_parse_at_file_still_works():
    p = parse_command_args(
        ["-r", "@main.py", "-1-5", "trim"],
        BUILD_MODES,
    )
    assert p.files[0].path == "main.py"
    assert p.files[0].lines == "1-5"


def test_parse_order_violation_file_after_description():
    p = parse_command_args(
        ["-r", "some description", "@file.py"],
        BUILD_MODES,
    )
    assert any("must come before the description" in e for e in p.errors)


def test_parse_default_mode_applied_when_missing():
    p = parse_command_args(["@file.py", "msg"], BUILD_MODES)
    # First value in dict is "refactor"
    assert p.mode == "refactor"


def test_parse_max_files_enforced():
    args: list[str] = []
    for i in range(15):
        args += ["-f", f"file_{i}.py"]
    args.append("msg")
    p = parse_command_args(args, BUILD_MODES)
    assert len(p.files) == 10
    assert any("Maximum" in e for e in p.errors)


# ---- corrected-order suggestion -----------------------------------------

def test_clean_parse_has_no_suggestion():
    p = parse_command_args(["-r", "@main.py", "-1-5", "trim"], BUILD_MODES)
    assert p.errors == []
    assert p.suggestion is None


def test_order_violation_suggests_canonical_form():
    # description before the @file -> rejected, with the fixed order offered.
    p = parse_command_args(["-r", "some description", "@file.py"], BUILD_MODES)
    assert p.errors
    assert p.suggestion == '-r @file.py "some description"'


def test_suggestion_keeps_line_range_with_its_file():
    p = parse_command_args(["fix it", "-r", "@main.py", "-10-25"], BUILD_MODES)
    assert p.suggestion == '-r @main.py -10-25 "fix it"'


def test_suggest_order_reorders_mode_files_text():
    # Direct helper check: scrambled input -> canonical order.
    out = _suggest_order(["text here", "@a.py", "-g"], BUILD_MODES)
    assert out == '-g @a.py "text here"'


def test_suggest_order_returns_none_for_empty():
    assert _suggest_order([], BUILD_MODES) is None


# ---- resolve_file_attachments directory error ---------------------------

def test_resolve_directory_reports_error(tmp_path):
    d = tmp_path / "assets"
    d.mkdir()
    fa = FileAttachment(path=str(d))
    _, errors = resolve_file_attachments([fa])
    assert errors, "expected error when path is a directory"
    assert "Not a file" in errors[0]


def test_resolve_missing_file_reports_error(tmp_path):
    fa = FileAttachment(path=str(tmp_path / "does_not_exist.py"))
    _, errors = resolve_file_attachments([fa])
    assert errors
    assert "not found" in errors[0].lower()


def test_resolve_reads_real_file(tmp_path):
    f = tmp_path / "hello.py"
    f.write_text("print('hi')\n", encoding="utf-8")
    fa = FileAttachment(path=str(f))
    files, errors = resolve_file_attachments([fa])
    assert errors == []
    assert files[0].content == "print('hi')\n"


def test_resolve_binary_file_reports_error(tmp_path):
    """A binary blob (NUL bytes) is rejected, not fed as garbage to the prompt."""
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    fa = FileAttachment(path=str(f))
    _, errors = resolve_file_attachments([fa])
    assert errors
    assert "cannot read" in errors[0].lower()
    assert "binary" in errors[0].lower()
    # Path is named once (wrapper), not duplicated by read_file's reason.
    assert errors[0].lower().count("blob.bin") == 1


def test_resolve_crlf_file_is_normalized(tmp_path):
    """CRLF files decode without leaking \\r into the attachment content."""
    f = tmp_path / "crlf.py"
    f.write_bytes(b"a = 1\r\nb = 2\r\n")
    fa = FileAttachment(path=str(f))
    files, errors = resolve_file_attachments([fa])
    assert errors == []
    assert "\r" not in files[0].content
    assert files[0].content == "a = 1\nb = 2\n"
