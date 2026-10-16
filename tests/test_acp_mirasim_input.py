"""Mirasim prompt staging and file attachments across native path conventions."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from raven.acp.mirasim import server as server_mod
from raven.acp.mirasim import uicli
from raven.acp.mirasim.server import _prompt_input
from raven.agent.subagent import attachments
from raven.spine.message import Media


@pytest.mark.parametrize(
    "name", ["ordinary space.txt", "caf\u00e9.txt", "literal%20name.txt", "literal%25name.txt", "literal%23name.txt"]
)
def test_a_file_uri_preserves_the_literal_filename(tmp_path: Path, name: str) -> None:
    attachment = tmp_path / name
    attachment.write_text("attachment", encoding="utf-8")
    text, files = _prompt_input([{"type": "resource_link", "uri": attachment.as_uri(), "name": name}])
    assert files == [str(attachment)]
    assert text == "See the attached files."


@pytest.mark.parametrize("authority", ["", "localhost", "LOCALHOST"])
def test_a_local_attachment_round_trips_through_its_caller(tmp_path: Path, authority: str) -> None:
    path = tmp_path / "literal%20name.txt"
    path.write_text("attachment", encoding="utf-8")
    blocks = attachments.attachment_blocks([Media(path=str(path), mime="text/plain", kind="file")])
    blocks[0]["uri"] = blocks[0]["uri"].replace("file://", f"file://{authority}", 1)
    text, files = _prompt_input(blocks)
    assert files == [str(path)]
    assert text == "See the attached files."


@pytest.mark.parametrize("host", ["review-server", "server name", "localhost"])
@pytest.mark.parametrize("present", [True, False], ids=["present", "missing"])
def test_a_unc_attachment_retains_its_authority_from_caller_to_parser(monkeypatch, host: str, present: bool) -> None:
    """Use Windows path conversion while standing in for share resolution and existence."""
    from nturl2path import url2pathname

    checked = []
    available = True
    path_type = type(Path()) if sys.platform == "win32" else PureWindowsPath

    class SharePath(path_type):
        def is_file(self):
            checked.append(str(self))
            return available and self == source

        def resolve(self, *args, **kwargs):
            return self

    source = SharePath(f"\\\\{host}\\review-share\\literal%20name.txt")
    monkeypatch.setattr(attachments, "Path", SharePath)
    monkeypatch.setattr(server_mod, "Path", SharePath)
    monkeypatch.setattr(server_mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(server_mod, "url2pathname", url2pathname)
    blocks = attachments.attachment_blocks([Media(path=str(source), mime="text/plain", kind="file")])
    assert blocks[0]["uri"].endswith("/review-share/literal%2520name.txt")
    available = present
    text, files = _prompt_input(blocks)
    assert checked == [str(source), str(source)]
    assert files == ([str(source)] if present else [])
    if present:
        assert text == "See the attached files."
    else:
        assert blocks[0]["uri"] in text


@pytest.mark.parametrize(
    "authority",
    [
        "user@server",
        "server:445",
        "server%2fother",
        "server%5cother",
        "server%3a445",
        "user%40server",
        "server%00",
        "server%0a",
        "server%7f",
        "[invalid",
    ],
)
def test_an_invalid_file_authority_is_not_looked_up(monkeypatch, authority: str) -> None:
    """Under Windows conversion any other host becomes a UNC server, so only the authority check stops these."""
    from nturl2path import url2pathname

    monkeypatch.setattr(server_mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(server_mod, "url2pathname", url2pathname)
    monkeypatch.setattr(server_mod, "Path", lambda _: pytest.fail("invalid authority reached the filesystem"))
    uri = f"file://{authority}/share/file.txt"
    text, files = _prompt_input([{"type": "resource_link", "uri": uri}])
    assert files == [] and uri in text


@pytest.mark.parametrize(
    "uri",
    ["file://server/C:/local.txt", "file://server//other/share/file.txt", "file://localhost//other/share/file.txt"],
)
def test_a_windows_authority_cannot_be_replaced_by_a_drive_or_another_host(monkeypatch, uri: str) -> None:
    from nturl2path import url2pathname

    monkeypatch.setattr(server_mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(server_mod, "url2pathname", url2pathname)
    monkeypatch.setattr(server_mod, "Path", lambda _: pytest.fail("a different filesystem target was probed"))
    assert server_mod._local_file(uri) is None


def test_a_windows_drive_under_localhost_is_this_machine(monkeypatch) -> None:
    from nturl2path import url2pathname

    class LocalPath(PureWindowsPath):
        def is_file(self) -> bool:
            return True

    monkeypatch.setattr(server_mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(server_mod, "url2pathname", url2pathname)
    monkeypatch.setattr(server_mod, "Path", LocalPath)
    assert server_mod._local_file("file://LOCALHOST/C:/literal%2520name.txt") == "C:\\literal%20name.txt"


def test_a_path_the_converter_refuses_stays_a_link(monkeypatch) -> None:
    """The Windows converter of Python 3.12 and 3.13 raises ``OSError("Bad URL")`` for a path such as ``/1:/notes.md``."""

    def refuse(path: str) -> str:
        raise OSError(f"Bad URL: {path}")

    monkeypatch.setattr(server_mod, "url2pathname", refuse)
    monkeypatch.setattr(server_mod, "Path", lambda _: pytest.fail("a path the converter refused was probed"))
    uri = "file:///1:/notes.md"
    text, files = _prompt_input([{"type": "resource_link", "uri": uri}])
    assert files == [] and uri in text


def test_a_remote_file_authority_is_not_a_local_posix_path(monkeypatch) -> None:
    monkeypatch.setattr(server_mod, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(server_mod, "Path", lambda _: pytest.fail("a remote URI became a local path"))
    assert server_mod._local_file("file://review-server/etc/local.txt") is None


@pytest.fixture
def windows_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uicli, "sys", SimpleNamespace(platform="win32", executable=sys.executable))


@pytest.mark.parametrize("text", ["x" * 40000, "\U0001f680" * 17000, '\\"' * 12000], ids=["ascii", "utf16", "quoted"])
def test_an_oversized_windows_prompt_is_staged_without_changing_its_bytes(windows_command, text: str) -> None:
    argv = ["mirasim.exe", "ui-cli", "prompt", f"--text={text}", "--model=chosen", "--file=existing.txt"]
    with uicli._staged_command(argv) as staged:
        path = Path(staged[-1].removeprefix("--file="))
        assert path.read_bytes() == text.encode("utf-8")
        assert staged[4:6] == ["--model=chosen", "--file=existing.txt"]
        assert staged[3] == "--text=" + uicli.PROMPT_FILE_NOTE.format(name=path.name)
        command = subprocess.list2cmdline([sys.executable, *uicli._GUARD, *staged])
        assert len(command.encode("utf-16-le")) // 2 + 1 <= 32767
    assert not path.exists()
    assert argv[3] == f"--text={text}"


def test_windows_staging_includes_flags_and_the_guard_in_its_budget(windows_command) -> None:
    argv = ["mirasim.exe", "prompt", "--text=" + "x" * 20000, "--model=" + "m" * 13000]
    with uicli._staged_command(argv) as staged:
        path = Path(staged[-1].removeprefix("--file="))
        assert path.read_text(encoding="utf-8") == "x" * 20000
        assert staged[3] == argv[3]
    assert not path.exists()


def test_the_windows_budget_includes_the_terminating_nul(windows_command, monkeypatch) -> None:
    argv = ["mirasim.exe", "prompt", "--text=" + "x" * 1000]
    command = subprocess.list2cmdline([sys.executable, *uicli._GUARD, *argv])
    length = len(command.encode("utf-16-le")) // 2
    monkeypatch.setattr(uicli, "_WINDOWS_COMMAND_LINE_LIMIT", length + 1)
    with uicli._staged_command(argv) as staged:
        assert staged is argv
    monkeypatch.setattr(uicli, "_WINDOWS_COMMAND_LINE_LIMIT", length)
    with uicli._staged_command(argv) as staged:
        assert staged is not argv
        assert staged[-1].startswith("--file=")


@pytest.mark.parametrize("prompt", [[], ["--text=short"]])
def test_an_oversized_non_prompt_argument_is_refused_and_leaves_no_staging_file(
    windows_command, monkeypatch, tmp_path: Path, prompt: list[str]
) -> None:
    monkeypatch.setattr(uicli.tempfile, "tempdir", str(tmp_path))
    with pytest.raises(uicli.UiCliError, match="Windows command-line limit"):
        with uicli._staged_command(["mirasim.exe", "--model=" + "m" * 40000, *prompt]):
            pytest.fail("an oversized command was accepted")
    assert list(tmp_path.iterdir()) == []


def test_a_failed_invocation_cleans_up_its_staged_prompt(windows_command) -> None:
    with pytest.raises(RuntimeError, match="launch failed"):
        with uicli._staged_command(["mirasim.exe", "--text=" + "x" * 40000]) as staged:
            path = Path(staged[-1].removeprefix("--file="))
            assert path.exists()
            raise RuntimeError("launch failed")
    assert not path.exists()


def test_posix_keeps_the_existing_prompt_staging_policy(monkeypatch) -> None:
    monkeypatch.setattr(uicli, "sys", SimpleNamespace(platform="linux", executable=sys.executable))
    argv = ["mirasim", "--text=" + "x" * 40000]
    with uicli._staged_command(argv) as staged:
        assert staged is argv
