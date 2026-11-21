"""A bounded tool output is the replay record; the full body is filed beside it."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from raven.security.trust import fence_span, wrap_untrusted
from raven.session.refs import TOOL_RESULT_TRUNCATION_MARKER, is_truncated
from raven.session.tool_output import (
    ToolOutputLimits,
    bound_tool_result,
    direction_for,
    prune_tool_outputs,
    tool_output_dir,
)

BYTES = ToolOutputLimits(max_lines=2_000, max_bytes=1_000, max_chars=0)
LONG_BODY = "\n".join(["x" * 100] * 50)


def _files(workspace: Path) -> list[Path]:
    return sorted(tool_output_dir(workspace).glob("tool_*.txt"))


def _body(content: str) -> str:
    span = fence_span(content)
    return span[1] if span is not None else content


def test_an_over_cap_result_is_previewed_and_the_full_body_is_filed(tmp_path: Path) -> None:
    fenced = wrap_untrusted(LONG_BODY, source="exec") + "\n[note: keep this]"

    bounded = bound_tool_result(fenced, tmp_path, limits=BYTES)

    assert isinstance(bounded, str)
    assert bounded.startswith("[BEGIN UNTRUSTED exec #")
    assert "[END UNTRUSTED" in bounded
    assert TOOL_RESULT_TRUNCATION_MARKER in bounded
    assert is_truncated(bounded)
    hint_at = bounded.index("Full output saved to:")
    assert hint_at > bounded.index("[END UNTRUSTED")
    assert "bytes omitted" in bounded
    assert "x" * 100 in bounded
    assert LONG_BODY not in bounded

    files = _files(tmp_path)
    assert len(files) == 1
    assert str(files[0].resolve()) in bounded
    assert files[0].read_text(encoding="utf-8") == LONG_BODY


def test_a_result_under_the_cap_is_unchanged_and_files_nothing(tmp_path: Path) -> None:
    under = wrap_untrusted("short", source="exec")
    at_cap = wrap_untrusted("x" * 1_000, source="exec")

    assert bound_tool_result(under, tmp_path, limits=BYTES) == under
    assert bound_tool_result(at_cap, tmp_path, limits=BYTES) == at_cap
    assert _files(tmp_path) == []


def test_bounding_an_already_bounded_result_is_a_no_op(tmp_path: Path) -> None:
    bounded = bound_tool_result(wrap_untrusted(LONG_BODY, source="exec"), tmp_path, limits=BYTES)

    assert bound_tool_result(bounded, tmp_path, limits=BYTES) == bounded
    assert len(_files(tmp_path)) == 1


def test_a_result_with_its_own_marker_is_still_filed(tmp_path: Path) -> None:
    content = wrap_untrusted(LONG_BODY + TOOL_RESULT_TRUNCATION_MARKER, source="exec")

    bounded = bound_tool_result(content, tmp_path, limits=BYTES)

    assert "Full output saved to:" in bounded
    assert len(_files(tmp_path)) == 1


def test_an_unfenced_result_keeps_the_same_contract(tmp_path: Path) -> None:
    bounded = bound_tool_result(LONG_BODY, tmp_path, limits=BYTES)

    assert isinstance(bounded, str)
    assert bounded.startswith("x" * 100)
    assert TOOL_RESULT_TRUNCATION_MARKER in bounded
    assert "Full output saved to:" in bounded
    assert _files(tmp_path)[0].read_text(encoding="utf-8") == LONG_BODY


def test_a_write_failure_still_bounds_the_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import raven.session.tool_output as store

    monkeypatch.setattr(store, "_file_full_output", lambda *a, **kw: None)
    fenced = wrap_untrusted(LONG_BODY, source="exec")

    bounded = bound_tool_result(fenced, tmp_path, limits=BYTES)

    assert is_truncated(bounded)
    assert TOOL_RESULT_TRUNCATION_MARKER in bounded
    assert "Full output saved to:" not in bounded
    assert "x" * 100 in bounded
    assert _files(tmp_path) == []


def test_the_line_cap_bounds_before_the_byte_cap(tmp_path: Path) -> None:
    body = "\n".join(f"line {i}" for i in range(3_000))
    limits = ToolOutputLimits(max_lines=2_000, max_bytes=10_000_000, max_chars=0)

    bounded = bound_tool_result(wrap_untrusted(body, source="exec"), tmp_path, limits=limits)

    visible = _body(bounded)
    preview = visible.removesuffix(TOOL_RESULT_TRUNCATION_MARKER)
    assert len(preview.split("\n")) == 2_000
    assert "1,000 lines omitted" in bounded
    assert _files(tmp_path)[0].read_text(encoding="utf-8") == body


def test_a_tail_preview_keeps_the_end_of_a_shell_result(tmp_path: Path) -> None:
    body = "\n".join(f"line {i}" for i in range(10))
    limits = ToolOutputLimits(max_lines=3, max_bytes=10_000, max_chars=0)

    bounded = bound_tool_result(wrap_untrusted(body, source="exec"), tmp_path, limits=limits, direction="tail")

    visible = _body(bounded)
    assert visible.startswith(TOOL_RESULT_TRUNCATION_MARKER)
    preview = visible.removeprefix(TOOL_RESULT_TRUNCATION_MARKER).lstrip("\n")
    assert preview.split("\n") == ["line 7", "line 8", "line 9"]
    assert "7 lines omitted" in bounded
    assert "the preview shows the end" in bounded


def test_the_legacy_char_cap_still_applies(tmp_path: Path) -> None:
    limits = ToolOutputLimits(max_lines=0, max_bytes=0, max_chars=100)

    bounded = bound_tool_result(wrap_untrusted("x" * 500, source="exec"), tmp_path, limits=limits)

    preview = _body(bounded).removesuffix(TOOL_RESULT_TRUNCATION_MARKER)
    assert preview == "x" * 100
    assert "400 characters omitted" in bounded
    assert is_truncated(bounded)


def test_a_single_line_over_the_byte_budget_keeps_its_head(tmp_path: Path) -> None:
    body = "x" * (60 * 1024)
    limits = ToolOutputLimits(max_lines=2_000, max_bytes=50 * 1024, max_chars=0)

    bounded = bound_tool_result(wrap_untrusted(body, source="exec"), tmp_path, limits=limits)

    preview = _body(bounded).removesuffix(TOOL_RESULT_TRUNCATION_MARKER)
    assert preview
    assert len(preview.encode("utf-8")) <= 50 * 1024
    assert preview.startswith("x")
    assert "bytes omitted" in bounded


def test_a_single_line_over_the_byte_budget_keeps_its_tail_for_exec(tmp_path: Path) -> None:
    body = "y" * (60 * 1024)
    limits = ToolOutputLimits(max_lines=2_000, max_bytes=50 * 1024, max_chars=0)

    bounded = bound_tool_result(wrap_untrusted(body, source="exec"), tmp_path, limits=limits, direction="tail")

    visible = _body(bounded)
    assert visible.startswith(TOOL_RESULT_TRUNCATION_MARKER)
    preview = visible.removeprefix(TOOL_RESULT_TRUNCATION_MARKER).lstrip("\n")
    assert preview
    assert len(preview.encode("utf-8")) <= 50 * 1024
    assert preview.endswith("y")
    assert "the preview shows the end" in bounded


def test_a_single_cjk_line_is_cut_on_a_utf8_boundary(tmp_path: Path) -> None:
    # U+6C49 is a 3-byte CJK character; the source stays ASCII for the gate.
    body = chr(0x6C49) * 20_000
    limits = ToolOutputLimits(max_lines=0, max_bytes=1_000, max_chars=0)

    bounded = bound_tool_result(wrap_untrusted(body, source="exec"), tmp_path, limits=limits)

    preview = _body(bounded).removesuffix(TOOL_RESULT_TRUNCATION_MARKER)
    assert preview
    assert len(preview.encode("utf-8")) <= 1_000
    assert "�" not in preview


def test_the_filed_body_preserves_its_line_endings(tmp_path: Path) -> None:
    body = "\r\n".join(["x" * 100] * 50)
    limits = ToolOutputLimits(max_lines=2_000, max_bytes=1_000, max_chars=0)

    bound_tool_result(wrap_untrusted(body, source="exec"), tmp_path, limits=limits)

    assert _files(tmp_path)[0].read_bytes() == body.encode("utf-8")


def test_the_hint_names_only_the_reader_tools_that_exist(tmp_path: Path) -> None:
    both = bound_tool_result(wrap_untrusted(LONG_BODY, source="exec"), tmp_path, limits=BYTES)
    read_only = bound_tool_result(
        wrap_untrusted(LONG_BODY, source="exec"), tmp_path, limits=BYTES, readers=("read_file",)
    )
    neither = bound_tool_result(wrap_untrusted(LONG_BODY, source="exec"), tmp_path, limits=BYTES, readers=())

    assert "grep" in both
    assert "grep" not in read_only
    assert "The full output is at the path above." in neither


def test_shell_tools_keep_the_tail_and_others_the_head() -> None:
    assert direction_for("exec") == "tail"
    assert direction_for("read_file") == "head"
    assert direction_for(None) == "head"


def test_managed_files_older_than_the_retention_are_pruned(tmp_path: Path) -> None:
    directory = tool_output_dir(tmp_path)
    directory.mkdir()
    old = directory / "tool_old.txt"
    fresh = directory / "tool_fresh.txt"
    old.write_text("old", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")
    old_time = (datetime.now() - timedelta(days=8)).timestamp()
    os.utime(old, (old_time, old_time))

    assert prune_tool_outputs(directory) == 1
    assert not old.exists()
    assert fresh.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_filed_body_is_owner_only(tmp_path: Path) -> None:
    bound_tool_result(wrap_untrusted(LONG_BODY, source="exec"), tmp_path, limits=BYTES)

    mode = _files(tmp_path)[0].stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_filed_body_is_owner_only_without_chmod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mode is set at creation, so a create-then-chmod race cannot widen it."""

    def _refuse(self: Path, mode: int, **kwargs: object) -> None:
        raise OSError("chmod disabled")

    monkeypatch.setattr(Path, "chmod", _refuse)
    bound_tool_result(wrap_untrusted(LONG_BODY, source="exec"), tmp_path, limits=BYTES)

    mode = _files(tmp_path)[0].stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_symlinked_managed_directory_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    victim = outside / "tool_victim.txt"
    victim.write_text("keep", encoding="utf-8")
    old = (datetime.now() - timedelta(days=8)).timestamp()
    os.utime(victim, (old, old))
    (workspace / "tool-output").symlink_to(outside, target_is_directory=True)
    before_mode = outside.stat().st_mode & 0o777

    bounded = bound_tool_result(wrap_untrusted(LONG_BODY, source="exec"), workspace, limits=BYTES)

    assert is_truncated(bounded)
    assert "Full output saved to:" not in bounded
    assert sorted(p.name for p in outside.iterdir()) == ["tool_victim.txt"]
    assert victim.exists()
    assert outside.stat().st_mode & 0o777 == before_mode
    assert (workspace / "tool-output").is_symlink()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_symlink_inside_the_workspace_is_also_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "elsewhere"
    target.mkdir(parents=True)
    (workspace / "tool-output").symlink_to(target, target_is_directory=True)

    bounded = bound_tool_result(wrap_untrusted(LONG_BODY, source="exec"), workspace, limits=BYTES)

    assert is_truncated(bounded)
    assert "Full output saved to:" not in bounded
    assert list(target.iterdir()) == []
