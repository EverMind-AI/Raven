"""Mirasim prompt content and file attachments across native path conventions."""

from __future__ import annotations

import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from raven.acp.mirasim import server as server_mod
from raven.acp.mirasim.server import read_prompt
from raven.agent.subagent import attachments
from raven.spine.message import Media


def _prompt_input(blocks: list[dict[str, str]]) -> tuple[str, list[str]]:
    """The prompt's text and the local files it hands over, for an agent that takes no inline images."""
    content = read_prompt(blocks, images=False)
    return content.text, list(content.paths)


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
    """Under Windows conversion any other host becomes a UNC server, so each must be refused before any lookup."""
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
    """Python 3.12's Windows converter raises ``OSError("Bad URL")`` for a path such as ``/1:/notes.md``."""

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
