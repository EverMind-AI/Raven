"""Mirasim prompt staging and file attachments across native path conventions."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.acp.mirasim import uicli
from raven.acp.mirasim.server import _prompt_input


@pytest.mark.parametrize(
    "name", ["ordinary space.txt", "caf\u00e9.txt", "literal%20name.txt", "literal%25name.txt", "literal%23name.txt"]
)
def test_a_file_uri_preserves_the_literal_filename(tmp_path: Path, name: str) -> None:
    attachment = tmp_path / name
    attachment.write_text("attachment", encoding="utf-8")
    text, files = _prompt_input([{"type": "resource_link", "uri": attachment.as_uri(), "name": name}])
    assert files == [str(attachment)]
    assert text == "See the attached files."


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
