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
FIFTY_KIB = ToolOutputLimits(max_lines=2_000, max_bytes=50 * 1024, max_chars=0)
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
    assert len(preview.encode("utf-8")) == 50 * 1024
    assert preview == "x" * (50 * 1024)
    assert "bytes omitted" in bounded


def test_a_single_line_over_the_byte_budget_keeps_its_tail_for_exec(tmp_path: Path) -> None:
    body = "y" * (60 * 1024)
    limits = ToolOutputLimits(max_lines=2_000, max_bytes=50 * 1024, max_chars=0)

    bounded = bound_tool_result(wrap_untrusted(body, source="exec"), tmp_path, limits=limits, direction="tail")

    visible = _body(bounded)
    assert visible.startswith(TOOL_RESULT_TRUNCATION_MARKER)
    preview = visible.removeprefix(TOOL_RESULT_TRUNCATION_MARKER).lstrip("\n")
    assert len(preview.encode("utf-8")) == 50 * 1024
    assert preview == "y" * (50 * 1024)
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


def test_a_blank_first_line_still_keeps_the_head_of_the_over_budget_line(tmp_path: Path) -> None:
    """``kept == [""]`` is not a preview: the blank line fits, the content does not."""
    body = "\n" + "x" * (60 * 1024)

    bounded = bound_tool_result(wrap_untrusted(body, source="web_fetch"), tmp_path, limits=FIFTY_KIB)

    preview = _body(bounded).removesuffix(TOOL_RESULT_TRUNCATION_MARKER)
    assert len(preview) == 50 * 1024
    assert preview == "x" * (50 * 1024)
    # The whole body is 61,441 bytes; 50 KiB of it is shown.
    assert "10,241 bytes omitted" in bounded
    assert _files(tmp_path)[0].read_text(encoding="utf-8") == body


def test_a_blank_last_line_still_keeps_the_tail_of_the_over_budget_line(tmp_path: Path) -> None:
    """The same shape at the cut end exec reads from."""
    body = "y" * (60 * 1024) + "\n"

    bounded = bound_tool_result(wrap_untrusted(body, source="exec"), tmp_path, limits=FIFTY_KIB, direction="tail")

    visible = _body(bounded)
    assert visible.startswith(TOOL_RESULT_TRUNCATION_MARKER)
    preview = visible.removeprefix(TOOL_RESULT_TRUNCATION_MARKER).lstrip("\n")
    assert len(preview) == 50 * 1024
    assert preview == "y" * (50 * 1024)
    assert "10,241 bytes omitted" in bounded
    assert "the preview shows the end" in bounded
    assert _files(tmp_path)[0].read_text(encoding="utf-8") == body


def test_a_kept_short_line_does_not_invent_a_preview_of_the_next_one(tmp_path: Path) -> None:
    """The line that did not fit is cut only when no line at all was kept."""
    body = "header\n" + "x" * (60 * 1024)

    bounded = bound_tool_result(wrap_untrusted(body, source="web_fetch"), tmp_path, limits=FIFTY_KIB)

    preview = _body(bounded).removesuffix(TOOL_RESULT_TRUNCATION_MARKER)
    assert preview == "header"
    assert len(preview) == len("header")
    # 61,447 bytes in the body; the header line is the only whole line kept.
    assert "61,441 bytes omitted" in bounded


def test_a_body_with_a_lone_surrogate_is_bounded_instead_of_raising(tmp_path: Path) -> None:
    body = "\ud800" * 20_000

    bounded = bound_tool_result(wrap_untrusted(body, source="web_fetch"), tmp_path, limits=FIFTY_KIB)

    assert is_truncated(bounded)
    assert "bytes omitted" in bounded


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


def test_a_junction_tool_output_directory_is_refused(tmp_path: Path) -> None:
    """A junction is refused like a symlink: Path.is_symlink() is False for one."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path_type = type(tool_output_dir(workspace))
    real_is_junction = path_type.is_junction
    path_type.is_junction = lambda self: True
    try:
        bounded = bound_tool_result(wrap_untrusted(LONG_BODY, source="exec"), workspace, limits=BYTES)
    finally:
        path_type.is_junction = real_is_junction

    assert is_truncated(bounded)
    assert "Full output saved to:" not in bounded
    assert not tool_output_dir(workspace).exists()


def test_blank_lines_before_an_over_budget_line_keep_that_line(tmp_path: Path) -> None:
    """With the line limit off, the budget stop can itself be a blank line."""
    body = "\n" * (60 * 1024) + "x" * 100
    limits = ToolOutputLimits(max_lines=0, max_bytes=50 * 1024, max_chars=0)

    bounded = bound_tool_result(wrap_untrusted(body, source="web_fetch"), tmp_path, limits=limits)

    preview = _body(bounded).removesuffix(TOOL_RESULT_TRUNCATION_MARKER)
    assert preview == "x" * 100
    assert "bytes omitted" in bounded


def test_blank_lines_after_an_over_budget_line_keep_that_line_for_exec(tmp_path: Path) -> None:
    """The same stop at the end: the tail keeps the line before the blank run."""
    body = "y" * 100 + "\n" * (60 * 1024)
    limits = ToolOutputLimits(max_lines=0, max_bytes=50 * 1024, max_chars=0)

    bounded = bound_tool_result(wrap_untrusted(body, source="exec"), tmp_path, limits=limits, direction="tail")

    visible = _body(bounded)
    assert visible.startswith(TOOL_RESULT_TRUNCATION_MARKER)
    preview = visible.removeprefix(TOOL_RESULT_TRUNCATION_MARKER).lstrip("\n")
    assert preview == "y" * 100
    assert "the preview shows the end" in bounded


def test_windows_branch_uses_owner_only_dacl_helpers_without_os_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows, directory and file creation dispatch through owner-only DACL helpers."""
    import raven.session.tool_output as store

    events: list[tuple[str, Path]] = []

    class _WindowsOS:
        name = "nt"

        def __getattr__(self, item: str) -> object:
            return getattr(os, item)

        def open(self, *args: object, **kwargs: object) -> int:
            raise AssertionError("os.open must not create Windows managed tool output")

    def fake_mkdir(directory: Path) -> None:
        events.append(("mkdir", directory))
        directory.mkdir(parents=True, exist_ok=True)

    def fake_protect(directory: Path) -> None:
        events.append(("protect", directory))

    def fake_create(path: Path) -> int:
        events.append(("create", path))
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    monkeypatch.setattr(store, "os", _WindowsOS())
    monkeypatch.setattr(store, "_mkdir_owner_only_windows", fake_mkdir)
    monkeypatch.setattr(store, "_protect_owner_only_windows", fake_protect)
    monkeypatch.setattr(store, "_create_owner_only_windows", fake_create)

    bounded = bound_tool_result(wrap_untrusted(LONG_BODY, source="exec"), tmp_path, limits=BYTES)

    files = _files(tmp_path)
    assert len(files) == 1
    assert str(files[0].resolve()) in bounded
    assert events == [
        ("mkdir", tool_output_dir(tmp_path)),
        ("protect", tool_output_dir(tmp_path)),
        ("create", files[0]),
    ]


def test_a_failed_windows_tighten_is_logged_and_keeps_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A directory left wide by a failed tighten is logged, and the file still lands."""
    from loguru import logger

    import raven.session.tool_output as store

    class _WindowsOS:
        name = "nt"

        def __getattr__(self, item: str) -> object:
            return getattr(os, item)

    def fake_mkdir(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)

    def fail_protect(directory: Path) -> None:
        raise OSError("access denied")

    def fake_create(path: Path) -> int:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    monkeypatch.setattr(store, "os", _WindowsOS())
    monkeypatch.setattr(store, "_mkdir_owner_only_windows", fake_mkdir)
    monkeypatch.setattr(store, "_protect_owner_only_windows", fail_protect)
    monkeypatch.setattr(store, "_create_owner_only_windows", fake_create)

    messages: list[str] = []
    handler = logger.add(messages.append, format="{message}", level="WARNING")
    try:
        bounded = bound_tool_result(wrap_untrusted(LONG_BODY, source="exec"), tmp_path, limits=BYTES)
    finally:
        logger.remove(handler)

    files = _files(tmp_path)
    assert len(files) == 1
    assert str(files[0].resolve()) in bounded
    directory = str(tool_output_dir(tmp_path))
    assert any(directory in message and "access denied" in message for message in messages)


def _windows_assert_owner_only_dacl(targets: list[Path]) -> None:
    """Assert every target grants the owner read and denies non-owner SIDs."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    authz = ctypes.WinDLL("authz", use_last_error=True)

    class _SidAttr(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("User", _SidAttr)]

    class _Luid(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class _AuthzRequest(ctypes.Structure):
        _fields_ = [
            ("DesiredAccess", wintypes.DWORD),
            ("PrincipalSelfSid", ctypes.c_void_p),
            ("ObjectTypeList", ctypes.c_void_p),
            ("ObjectTypeListLength", wintypes.DWORD),
            ("OptionalArguments", ctypes.c_void_p),
        ]

    class _AuthzReply(ctypes.Structure):
        _fields_ = [
            ("ResultListLength", wintypes.DWORD),
            ("GrantedAccessMask", ctypes.POINTER(wintypes.DWORD)),
            ("SaclEvaluationResults", ctypes.POINTER(wintypes.DWORD)),
            ("Error", ctypes.POINTER(wintypes.DWORD)),
        ]

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    kernel32.OpenProcessToken.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetFileSecurityW.restype = wintypes.BOOL
    authz.AuthzInitializeResourceManager.argtypes = [
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    authz.AuthzInitializeResourceManager.restype = wintypes.BOOL
    authz.AuthzInitializeContextFromSid.argtypes = [
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.HANDLE,
        ctypes.c_void_p,
        _Luid,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    authz.AuthzInitializeContextFromSid.restype = wintypes.BOOL
    authz.AuthzAccessCheck.argtypes = [
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(_AuthzRequest),
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_AuthzReply),
        ctypes.c_void_p,
    ]
    authz.AuthzAccessCheck.restype = wintypes.BOOL
    authz.AuthzFreeContext.argtypes = [wintypes.HANDLE]
    authz.AuthzFreeResourceManager.argtypes = [wintypes.HANDLE]

    token = wintypes.HANDLE()
    assert kernel32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token))
    needed = wintypes.DWORD(0)
    advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
    tbuf = ctypes.create_string_buffer(needed.value)
    assert advapi32.GetTokenInformation(token, 1, tbuf, needed.value, ctypes.byref(needed))
    owner_sid = ctypes.cast(tbuf, ctypes.POINTER(_TokenUser)).contents.User.Sid
    kernel32.CloseHandle(token)

    rm = wintypes.HANDLE()
    assert authz.AuthzInitializeResourceManager(1, None, None, None, None, ctypes.byref(rm))
    file_generic_read = 0x00120089
    error_access_denied = 5
    try:
        for target in targets:
            need = wintypes.DWORD(0)
            advapi32.GetFileSecurityW(str(target), 7, None, 0, ctypes.byref(need))
            sd_buf = ctypes.create_string_buffer(need.value)
            assert advapi32.GetFileSecurityW(str(target), 7, sd_buf, need.value, ctypes.byref(need))

            # Owner can read.
            owner_ctx = wintypes.HANDLE()
            assert authz.AuthzInitializeContextFromSid(
                2, owner_sid, rm, None, _Luid(0, 0), None, ctypes.byref(owner_ctx)
            )
            granted = wintypes.DWORD(0)
            err = wintypes.DWORD(999)
            req = _AuthzRequest(file_generic_read, None, None, 0, None)
            reply = _AuthzReply(1, ctypes.pointer(granted), None, ctypes.pointer(err))
            assert authz.AuthzAccessCheck(
                0, owner_ctx, ctypes.byref(req), None, sd_buf, None, 0, ctypes.pointer(reply), None
            )
            authz.AuthzFreeContext(owner_ctx)
            assert granted.value == file_generic_read and err.value == 0

            # Non-owner user, Everyone, Authenticated Users, Users, and Admins are all denied.
            for non_owner in ("S-1-5-21-0-0-0-1999", "S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-32-544"):
                csid = ctypes.c_void_p()
                assert advapi32.ConvertStringSidToSidW(non_owner, ctypes.byref(csid))
                ctx = wintypes.HANDLE()
                assert authz.AuthzInitializeContextFromSid(
                    2, csid.value, rm, None, _Luid(0, 0), None, ctypes.byref(ctx)
                )
                g_other = wintypes.DWORD(1)
                e_other = wintypes.DWORD(0)
                r_other = _AuthzReply(1, ctypes.pointer(g_other), None, ctypes.pointer(e_other))
                assert authz.AuthzAccessCheck(
                    0, ctx, ctypes.byref(req), None, sd_buf, None, 0, ctypes.pointer(r_other), None
                )
                authz.AuthzFreeContext(ctx)
                kernel32.LocalFree(csid)
                assert g_other.value == 0 and e_other.value == error_access_denied
    finally:
        authz.AuthzFreeResourceManager(rm)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows NTFS DACLs")
def test_windows_managed_file_and_dir_deny_non_owner_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real Windows NTFS DACL check: owner reads cleanly while non-owner SIDs are denied."""
    import subprocess

    import raven.session.tool_output as store

    # Widen the parent workspace ACL explicitly so an unprotected file would
    # inherit Everyone:F; the managed output must ignore that inheritance.
    subprocess.run(
        ["icacls", str(tmp_path), "/grant", "*S-1-1-0:(OI)(CI)F"],
        check=True,
        capture_output=True,
        text=True,
    )
    real_open = os.open

    def forbid_os_open(*args: object, **kwargs: object) -> int:
        raise AssertionError("Windows path must create the file via CreateFileW DACL, not os.open")

    monkeypatch.setattr(store.os, "open", forbid_os_open)
    try:
        bounded = bound_tool_result(wrap_untrusted(LONG_BODY, source="exec"), tmp_path, limits=BYTES)
    finally:
        monkeypatch.setattr(store.os, "open", real_open)

    files = _files(tmp_path)
    assert len(files) == 1
    assert str(files[0].resolve()) in bounded
    assert files[0].read_text(encoding="utf-8") == LONG_BODY

    _windows_assert_owner_only_dacl([tool_output_dir(tmp_path), files[0]])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows NTFS DACLs")
def test_windows_existing_wide_tool_output_directory_is_tightened(tmp_path: Path) -> None:
    """A directory that already exists wide is tightened by SetNamedSecurityInfoW, not left alone."""
    import subprocess

    directory = tool_output_dir(tmp_path)
    directory.mkdir()
    subprocess.run(
        ["icacls", str(directory), "/grant", "*S-1-1-0:F"],
        check=True,
        capture_output=True,
        text=True,
    )

    bounded = bound_tool_result(wrap_untrusted(LONG_BODY, source="exec"), tmp_path, limits=BYTES)

    files = _files(tmp_path)
    assert len(files) == 1
    assert str(files[0].resolve()) in bounded
    _windows_assert_owner_only_dacl([directory])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows CreateFileW CREATE_NEW")
def test_windows_owner_only_create_refuses_existing_target(tmp_path: Path) -> None:
    import raven.session.tool_output as store

    existing = tmp_path / "existing.txt"
    existing.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        store._create_owner_only_windows(existing)
