"""What the host tells a child about itself survives the login-shell boundary.

Both variables here are captured from raven's own environment and re-applied over
a login shell's, which is the step that drops them if it is missed: the capture
runs `$SHELL -lic` from a minimal base precisely so raven's own variables do not
leak, so anything the host means the child to see has to be overlaid back.

Windows has no login shell, so the same boundary is crossed there by a registry
read; the sections at the end cover that capture and the launch that consumes it.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path
from typing import Any, NoReturn

import pytest

import raven.agent.subagent.probe as probe_mod
from raven.acp_client.client import AcpClient
from raven.acp_client.protocol import AcpConnectionError
from raven.agent.subagent.backends import env as backend_env
from raven.agent.subagent.probe import probe_all
from raven.agent.subagent.role import SUBAGENT_ENV_VAR, is_subagent_process
from raven.config.schema import ThirdPartyAcpSubagentConfig


def _reporting_child(script: Path, variable: str) -> str:
    """Write a child that reports one variable, and return the command that runs it.

    The write is staged and renamed so that the observed path existing means its
    content is whole. ``write_text`` creates the file before it writes into it, so a
    parent polling on existence can read an empty string from a child that is about
    to report correctly -- which is what a loaded CI runner saw as
    ``assert '' == '/tmp/.../custom-home'`` while the same test passed every local
    run. ``os.replace`` is atomic within a filesystem, so there is no window to lose.

    The fallback is a word rather than an empty string for the same reason: a
    variable that genuinely did not arrive must fail loudly, not look like the race.
    """
    script.write_text(
        "import os, pathlib, time\n"
        "target = pathlib.Path(os.environ['OUTPUT'])\n"
        "partial = target.with_name(target.name + '.partial')\n"
        f"partial.write_text(os.environ.get({variable!r}, '<missing>'))\n"
        "os.replace(partial, target)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


async def test_acp_child_inherits_custom_raven_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host_home = tmp_path / "custom-home"
    host_home.mkdir()
    observed = tmp_path / "observed.txt"
    command = _reporting_child(tmp_path / "child.py", "RAVEN_HOME")
    monkeypatch.setenv("RAVEN_HOME", str(host_home))
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: {"PATH": os.environ["PATH"], "OUTPUT": str(observed)})

    client = await AcpClient.launch(name="env-probe", command=command)
    try:
        for _ in range(100):
            if observed.exists():
                break
            await asyncio.sleep(0.01)
        assert observed.read_text(encoding="utf-8") == str(host_home)
    finally:
        await client.close()


async def test_acp_child_is_told_it_serves_as_a_subagent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the gate in ``raven.agent.subagent.role`` never fires: a child
    ``raven acp`` builds a full registry because nothing ever tells it otherwise,
    which is how a DAG node's sub-agent came to hold ``spawn`` and delegate the
    node's whole task to a background receipt."""
    observed = tmp_path / "observed.txt"
    command = _reporting_child(tmp_path / "child.py", SUBAGENT_ENV_VAR)
    # The capture the launch overlays onto, standing in for the user's login
    # shell: it carries no role variable, so a passing read proves the overlay
    # rather than a value inherited from the test runner.
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: {"PATH": os.environ["PATH"], "OUTPUT": str(observed)})

    client = await AcpClient.launch(name="role-probe", command=command)
    try:
        for _ in range(100):
            if observed.exists():
                break
            await asyncio.sleep(0.01)
        arrived = observed.read_text(encoding="utf-8")
    finally:
        await client.close()

    # Judged by the reader rather than against a literal: what the host writes and
    # what the child accepts are two halves of one contract, and a test holding
    # the written form alone would pass while the child ignored it.
    monkeypatch.setenv(SUBAGENT_ENV_VAR, arrived)
    assert is_subagent_process(), f"the child was handed {arrived!r}, which it does not read as a sub-agent role"


# --- Windows: the capture --------------------------------------------------
#
# Windows has no login shell. The capture rebuilds PATH from the two registry
# stores a new terminal's Path is assembled from, and keeps every other value
# raven already has. The fixtures are shaped the way Windows hands them over:
# the stores' REG_EXPAND_SZ values unexpanded, and raven's own environment with
# every name upper-cased, which is how CPython's ``os.environ`` holds it there.


class _FakeKey:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def __enter__(self) -> "_FakeKey":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _FakeWinreg:
    """``winreg`` over two stores a test edits; a store set to ``None`` refuses the read."""

    HKEY_LOCAL_MACHINE = 0x80000002
    HKEY_CURRENT_USER = 0x80000001
    REG_EXPAND_SZ = 2

    def __init__(self, machine: dict[str, str] | None, user: dict[str, str] | None) -> None:
        self.machine = machine
        self.user = user

    def OpenKey(self, hive: int, _subkey: str) -> _FakeKey:
        store = self.machine if hive == self.HKEY_LOCAL_MACHINE else self.user
        if store is None:
            raise PermissionError(5, "Access is denied")
        return _FakeKey(dict(store))

    def QueryInfoKey(self, key: _FakeKey) -> tuple[int, int, int]:
        return (0, len(key.values), 0)

    def EnumValue(self, key: _FakeKey, index: int) -> tuple[str, str, int]:
        name = list(key.values)[index]
        return (name, key.values[name], self.REG_EXPAND_SZ)


# What `reg query` shows in each store on a default install.
_STOCK_MACHINE = {
    "ComSpec": r"%SystemRoot%\system32\cmd.exe",
    "Path": r"%SystemRoot%\system32;%SystemRoot%",
    "PSModulePath": r"%ProgramFiles%\WindowsPowerShell\Modules",
    "TEMP": r"%SystemRoot%\TEMP",
    "TMP": r"%SystemRoot%\TEMP",
    "USERNAME": "SYSTEM",
    "windir": r"%SystemRoot%",
}
_STOCK_USER = {
    "Path": r"%USERPROFILE%\AppData\Local\Microsoft\WindowsApps",
    "TEMP": r"%USERPROFILE%\AppData\Local\Temp",
    "TMP": r"%USERPROFILE%\AppData\Local\Temp",
}
# Raven's own environment, as the logon built it from those stores.
_LOGON = {
    "COMSPEC": r"C:\Windows\system32\cmd.exe",
    "PATH": r"C:\Windows\system32;C:\Windows;C:\Users\me\AppData\Local\Microsoft\WindowsApps",
    "SYSTEMROOT": r"C:\Windows",
    "TEMP": r"C:\Users\me\AppData\Local\Temp",
    "TMP": r"C:\Users\me\AppData\Local\Temp",
    "USERNAME": "me",
    "USERPROFILE": r"C:\Users\me",
    "WINDIR": r"C:\Windows",
}


@pytest.fixture
def windows_host(monkeypatch: pytest.MonkeyPatch) -> _FakeWinreg:
    """Raven started on Windows over the stock stores, with no capture taken yet.

    The ambient environment goes first: on Linux it can hold one variable under two
    spellings (``HTTP_PROXY`` beside ``http_proxy``), which Windows never can, and a
    test that read it would measure the machine it ran on.
    """
    for name in list(os.environ):
        if not name.startswith("PYTEST_"):
            monkeypatch.delenv(name)
    for name, value in _LOGON.items():
        monkeypatch.setenv(name, value)
    registry = _FakeWinreg(dict(_STOCK_MACHINE), dict(_STOCK_USER))
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(backend_env, "_on_windows", lambda: True)
    monkeypatch.setattr(backend_env, "_LOGIN_ENV", None)
    monkeypatch.setattr(backend_env, "_LOGIN_ENV_FAILED", False)
    return registry


def _no_shell(argv: list[str], **_: Any) -> NoReturn:
    raise AssertionError(f"no shell is started on Windows, got {argv}")


def test_windows_capture_takes_path_from_the_stores_and_every_other_value_from_raven(
    windows_host: _FakeWinreg,
) -> None:
    r"""The reported failure: an installer ran after the gateway started. Only PATH moves.

    A terminal opened after ``uv tool install`` builds its Path from the store the
    installer wrote, and so does this capture. The stores' other values are what
    Windows builds an environment *from* -- ``ComSpec`` and ``TEMP`` unexpanded, the
    machine's own ``USERNAME`` -- so copied over raven's they would hand a child a
    ``ComSpec`` that ``Popen(shell=True)`` cannot start, a ``TEMP`` that is no
    directory, and a second spelling of names raven already has.
    """
    windows_host.user["Path"] += r";C:\Users\me\.local\bin"

    captured = backend_env._capture_windows(consequence="subagents inherit raven's environment")

    assert captured is not None
    assert captured["PATH"] == (
        r"C:\Windows\system32;C:\Windows;C:\Users\me\AppData\Local\Microsoft\WindowsApps;C:\Users\me\.local\bin;"
        r"C:\Windows\system32;C:\Windows;C:\Users\me\AppData\Local\Microsoft\WindowsApps"
    )
    assert {name: value for name, value in captured.items() if name != "PATH"} == {
        name: value for name, value in os.environ.items() if name != "PATH"
    }


def test_windows_capture_expands_stored_references_with_ravens_own_values_first(
    windows_host: _FakeWinreg,
) -> None:
    r"""A reference in a stored Path resolves to what a terminal of this logon would get.

    Raven's own values win because they are the logon's -- the machine store's
    ``USERNAME`` is ``SYSTEM`` -- and the child carries them beside this PATH. A
    variable an installer added after raven started is only in the stores, where the
    user's shadows the machine's and may hold a reference of its own.
    """
    windows_host.machine.update({"Path": r"C:\Users\%USERNAME%\bin;%NVM_HOME%", "NVM_HOME": r"C:\nvm\machine"})
    windows_host.user.update({"Path": "%NVM_SYMLINK%", "NVM_HOME": r"C:\nvm\user", "NVM_SYMLINK": r"%NVM_HOME%\nodejs"})

    captured = backend_env._capture_windows(consequence="subagents inherit raven's environment")

    assert captured is not None
    assert captured["PATH"].split(";")[:3] == [r"C:\Users\me\bin", r"C:\nvm\user", r"C:\nvm\user\nodejs"]


def test_windows_capture_keeps_the_users_store_when_the_machines_refuses(windows_host: _FakeWinreg) -> None:
    """HKLM can refuse a read; the user's entries must not be lost with it."""
    windows_host.machine = None

    captured = backend_env._capture_windows(consequence="subagents inherit raven's environment")

    assert captured is not None
    assert captured["PATH"] == r"C:\Users\me\AppData\Local\Microsoft\WindowsApps;" + _LOGON["PATH"]


def test_windows_capture_reports_failure_when_no_store_reads(windows_host: _FakeWinreg) -> None:
    """Both stores refusing is the one case the caller must fall back on ``os.environ``."""
    windows_host.machine = None
    windows_host.user = None

    assert backend_env._capture_windows(consequence="subagents inherit raven's environment") is None


def test_a_refresh_on_windows_reads_the_stores_again(
    windows_host: _FakeWinreg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check again, Connect and Test each refresh the capture; on Windows that is another registry read.

    Routed to the login-shell capture instead, a refresh found no shell, kept the
    first capture, and an agent installed after it stayed missing until a restart --
    the case the Windows capture exists for. Git for Windows exports ``SHELL`` to
    what it starts, and driving that bash would have been worse: an MSYS
    environment, a ``:``-joined PATH of ``/c/...`` entries and no ``SystemRoot``,
    swapped in for every probe and spawn after it.
    """
    monkeypatch.setenv("SHELL", r"C:\Program Files\Git\usr\bin\bash.exe")
    monkeypatch.setattr(backend_env.subprocess, "run", _no_shell)
    assert r"C:\Users\me\.local\bin" not in backend_env.login_shell_env()["PATH"]

    windows_host.user["Path"] += r";C:\Users\me\.local\bin"

    assert backend_env.refresh_login_shell_env() is True
    assert r"C:\Users\me\.local\bin" in backend_env.login_shell_env()["PATH"]


async def test_windows_probe_finds_agents_on_the_captured_path(
    tmp_path: Path, windows_host: _FakeWinreg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capture is read by name -- ``probe_all`` asks it for ``PATH`` -- so it is checked through the probe.

    One agent sits on the PATH raven started with, one only in a directory the
    user's store gained afterwards. Stored under the registry's ``Path`` spelling,
    the capture left the probe an empty PATH and both read as missing, the first of
    them found before the capture existed.
    """
    boot_bin, fresh_bin = tmp_path / "boot-bin", tmp_path / "fresh-bin"
    for directory, name in ((boot_bin, "on-boot-agent"), (fresh_bin, "fresh-agent")):
        directory.mkdir()
        (directory / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (directory / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(boot_bin))
    windows_host.user["Path"] = str(fresh_bin)
    monkeypatch.setattr(os, "pathsep", ";")
    monkeypatch.setattr(probe_mod, "acp_snapshot_for", lambda cfg: None)
    rows = [
        ThirdPartyAcpSubagentConfig(name=name, command=f"{name} --acp") for name in ("on-boot-agent", "fresh-agent")
    ]

    results = await probe_all([(cfg, "config") for cfg in rows])

    assert [(r.name, r.status, r.target) for r in results] == [
        ("on-boot-agent", "attention", str(boot_bin / "on-boot-agent")),
        ("fresh-agent", "attention", str(fresh_bin / "fresh-agent")),
    ]


# --- Windows: the launch ---------------------------------------------------


def _recording_refusal(started: list[tuple[str, ...]]) -> Any:
    async def refuse(*argv: str, **_: Any) -> NoReturn:
        started.append(argv)
        raise FileNotFoundError(2, "not started in a test")

    return refuse


async def test_on_windows_a_bare_program_starts_from_the_childs_own_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CreateProcess finds a bare name on the gateway's own PATH, never on the env block it is handed.

    So an agent installed after raven started, which the refreshed capture and the
    probe both find, would still not start. The launch looks the name up on the
    child's PATH itself, by CreateProcess's own rule that a name with no extension
    means ``.exe``.
    """
    fresh_bin = tmp_path / "fresh-bin"
    fresh_bin.mkdir()
    (fresh_bin / "fresh-agent.exe").write_text("", encoding="utf-8")
    (fresh_bin / "fresh-agent.exe").chmod(0o755)
    monkeypatch.setattr(backend_env, "_on_windows", lambda: True)
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: {"PATH": str(fresh_bin)})
    started: list[tuple[str, ...]] = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _recording_refusal(started))

    with pytest.raises(AcpConnectionError):
        await AcpClient.launch(name="fresh", command="fresh-agent acp")

    assert started == [(str(fresh_bin / "fresh-agent.exe"), "acp")]


async def test_off_windows_the_launch_leaves_the_lookup_to_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX ``exec`` already resolves a bare name on the PATH of the env it is handed."""
    fresh_bin = tmp_path / "fresh-bin"
    fresh_bin.mkdir()
    (fresh_bin / "fresh-agent.exe").write_text("", encoding="utf-8")
    (fresh_bin / "fresh-agent.exe").chmod(0o755)
    monkeypatch.setattr(backend_env, "_on_windows", lambda: False)
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: {"PATH": str(fresh_bin)})
    started: list[tuple[str, ...]] = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _recording_refusal(started))

    with pytest.raises(AcpConnectionError):
        await AcpClient.launch(name="fresh", command="fresh-agent acp")

    assert started == [("fresh-agent", "acp")]


def test_on_windows_a_shim_on_the_childs_path_is_not_made_the_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.cmd`` shim runs through cmd.exe, whose parser would get the arguments -- a cli agent's prompt among them."""
    npm_bin = tmp_path / "npm"
    npm_bin.mkdir()
    (npm_bin / "codex.cmd").write_text("", encoding="utf-8")
    (npm_bin / "codex.cmd").chmod(0o755)
    monkeypatch.setattr(backend_env, "_on_windows", lambda: True)
    child_env = {"PATH": str(npm_bin)}

    assert backend_env.resolve_program(["codex", "exec", "a & b"], child_env) == ["codex", "exec", "a & b"]
    assert backend_env.resolve_program(["codex.cmd", "exec"], child_env) == ["codex.cmd", "exec"]
