"""Which Node.js an npm-installed agent is launched with, and whether it is new enough."""

from __future__ import annotations

import json
from pathlib import Path

from raven.agent.subagent.node_runtime import NodeTooOld, _declared_floor, _short, _upgrade_command, node_too_old


def _installed(
    tmp_path: Path,
    node_version: str,
    *,
    node_dir: Path | None = None,
    engines: object = ">=22.0.0",
    shebang: str = "#!/usr/bin/env node",
) -> str:
    """An `npm i -g` layout -- `bin/qwen` linked to the package's entry script -- and a `node`; the PATH to both.

    The `node` is a stand-in that answers `--version`, which is all the check
    asks of it.
    """
    package = tmp_path / "lib" / "node_modules" / "@qwen-code" / "qwen-code"
    package.mkdir(parents=True)
    manifest: dict[str, object] = {"name": "@qwen-code/qwen-code"}
    if engines is not None:
        manifest["engines"] = {"node": engines}
    (package / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    entry = package / "cli-entry.js"
    entry.write_text(f"{shebang}\nconsole.log('hi')\n", encoding="utf-8")
    entry.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "qwen").symlink_to(entry)
    nodedir = node_dir or tmp_path / "node" / "bin"
    nodedir.mkdir(parents=True, exist_ok=True)
    node = nodedir / "node"
    node.write_text(f"#!/bin/sh\necho v{node_version}\n", encoding="utf-8")
    node.chmod(0o755)
    return f"{nodedir}:{bindir}:/usr/bin:/bin"


def test_an_agent_launched_on_a_node_below_its_floor_is_named_with_both_versions(tmp_path: Path) -> None:
    """Measured on Qwen Code 0.24.4: on Node 18.20.8 it dies at import, and its package asks for >=22.

    The versions are measured, not read from the crash: the crash names a
    missing `node:fs` export, which says nothing about which Node.js ran it.
    """
    path = _installed(tmp_path, "18.20.8")
    stale = node_too_old("qwen", path)
    assert stale == NodeTooOld(needs="22", found="18.20.8", node=str(tmp_path / "node" / "bin" / "node"), upgrade=None)


def test_a_node_at_or_past_the_floor_is_not_named(tmp_path: Path) -> None:
    assert node_too_old("qwen", _installed(tmp_path, "22.1.0")) is None


def test_the_upgrade_names_the_installer_the_node_came_from(tmp_path: Path) -> None:
    """nvm and Homebrew show in the path; anything else gets no command rather than a wrong one.

    The nvm default is what the login shell -- which Raven launches agents
    from -- resolves, so switching it is the fix, not only installing. Read off
    the path alone, so no stand-in `node` is run here.
    """
    nvm = tmp_path / ".nvm" / "versions" / "node" / "v18.20.8" / "bin" / "node"
    assert _upgrade_command(str(nvm), 22) == "nvm install 22 && nvm alias default 22"
    brew = tmp_path / "Cellar" / "node" / "18.20.8" / "bin" / "node"
    assert _upgrade_command(str(brew), 22) == "brew upgrade node"
    pinned = tmp_path / "Cellar" / "node@18" / "18.20.8" / "bin" / "node"
    assert _upgrade_command(str(pinned), 22) is None, "`brew upgrade node` does not move a pinned node@18"
    assert _upgrade_command(str(tmp_path / "usr" / "local" / "bin" / "node"), 22) is None


def test_the_floor_is_said_the_way_it_is_declared(tmp_path: Path) -> None:
    assert node_too_old("qwen", _installed(tmp_path, "20.11.0", engines=">=20.19.0")).needs == "20.19"


def test_an_absolute_shebang_is_the_node_that_runs(tmp_path: Path) -> None:
    """`#!/path/to/node` bypasses PATH, so the check must too."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    node = elsewhere / "node"
    node.write_text("#!/bin/sh\necho v16.20.2\n", encoding="utf-8")
    node.chmod(0o755)
    path = _installed(tmp_path, "22.1.0", shebang=f"#!{node}")
    stale = node_too_old("qwen", path)
    assert stale is not None and (stale.found, stale.node) == ("16.20.2", str(node))


def test_a_missing_fact_names_nothing(tmp_path: Path) -> None:
    """Every fact is required, so Node.js is never named on a guess."""
    for case, kwargs in {
        "no floor declared": {"engines": None},
        "a floor nobody can read": {"engines": "^22"},
        "not a Node.js script": {"shebang": "#!/bin/sh"},
    }.items():
        root = tmp_path / case.replace(" ", "_")
        root.mkdir()
        assert node_too_old("qwen", _installed(root, "18.20.8", **kwargs)) is None, case

    silent = tmp_path / "silent"
    silent.mkdir()
    path = _installed(silent, "18.20.8")
    (silent / "node" / "bin" / "node").write_text("#!/bin/sh\necho not-a-version\n", encoding="utf-8")
    assert node_too_old("qwen", path) is None, "a node that does not answer with a version"

    assert node_too_old("qwen", "/nonexistent") is None, "no agent on PATH"


def test_what_cannot_be_read_names_nothing(tmp_path: Path) -> None:
    """An unreadable script, a native binary, a node that will not start: each leaves Node.js unnamed."""
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    path = _installed(unreadable, "18.20.8")
    (unreadable / "lib" / "node_modules" / "@qwen-code" / "qwen-code" / "cli-entry.js").chmod(0o111)
    assert node_too_old("qwen", path) is None, "a script it may run but not read"

    native = tmp_path / "native"
    native.mkdir()
    path = _installed(native, "18.20.8")
    (native / "lib" / "node_modules" / "@qwen-code" / "qwen-code" / "cli-entry.js").write_bytes(b"\xcf\xfa\xed\xfe")
    assert node_too_old("qwen", path) is None, "a native binary has no interpreter to name"

    gone = tmp_path / "gone"
    gone.mkdir()
    path = _installed(gone, "22.1.0", shebang=f"#!{gone / 'no' / 'node'}")
    assert node_too_old("qwen", path) is None, "a shebang naming a node that is not there"


def test_the_floor_is_the_nearest_manifest_s_and_only_one_that_reads(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    (package / "dist").mkdir(parents=True)
    (package / "package.json").write_text('{"engines": {"node": ">=20.19.1"}}', encoding="utf-8")
    assert _declared_floor(str(package / "dist" / "cli.js")) == (20, 19, 1), "one level up from a dist/ entry"
    assert _short((20, 19, 1)) == "20.19.1"

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "package.json").write_text("{not json", encoding="utf-8")
    assert _declared_floor(str(broken / "cli.js")) is None

    assert _declared_floor(str(tmp_path / "a" / "b" / "c" / "d" / "e" / "cli.js")) is None, "no manifest near it"


def test_a_launch_command_that_does_not_parse_names_nothing() -> None:
    from types import SimpleNamespace

    from raven.agent.subagent import probe as probe_mod

    for command in ('qwen "--acp', ""):
        assert probe_mod._stale_node(SimpleNamespace(preset="qwen_code", command=command, env={})) is None, command


_DIED = (
    "acp agent 'Qwen Code': connection ended (exit 1); stderr tail: SyntaxError: The requested module "
    "'node:fs' does not provide an export named 'openAsBlob'"
)


def _qwen(path: str, preset: str = "qwen_code") -> object:
    from types import SimpleNamespace

    return SimpleNamespace(name="Qwen Code", preset=preset, kind="acp", command="qwen --acp", env={"PATH": path})


def test_a_launch_that_quit_on_an_old_node_is_named_with_both_versions(tmp_path: Path) -> None:
    """The fix is the Node.js, so the reader is told which one ran and which one is needed.

    Only for a preset measured to die this way: the same exit from an agent
    that does not run on Node.js stays a launch that quit.
    """
    from raven.acp_client.protocol import AcpConnectionError
    from raven.agent.subagent import probe as probe_mod
    from raven.agent.subagent.probe_state import Remedy

    path = _installed(tmp_path, "18.20.8")
    text, remedy = probe_mod._ping_refusal(_qwen(path), AcpConnectionError(_DIED))
    assert remedy == Remedy("runtime", None, needs="22", found="18.20.8")
    assert "it needs Node.js 22 or newer and was launched with 18.20.8" in text
    assert text.endswith(f"It said: {_DIED}"), "the agent's own words stay in the record"

    assert probe_mod._ping_refusal(_qwen(path, preset="hermes"), AcpConnectionError(_DIED))[1] == Remedy("exited")
    assert probe_mod._ping_refusal(_qwen(path, preset="grok"), AcpConnectionError(_DIED))[1] == Remedy("exited")


def test_a_launch_that_quit_on_a_new_enough_node_is_only_an_exit(tmp_path: Path) -> None:
    from raven.acp_client.protocol import AcpConnectionError
    from raven.agent.subagent import probe as probe_mod
    from raven.agent.subagent.probe_state import Remedy

    path = _installed(tmp_path, "22.1.0")
    assert probe_mod._ping_refusal(_qwen(path), AcpConnectionError(_DIED))[1] == Remedy("exited")


async def test_the_connect_names_an_old_node_off_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    """The check runs `node --version`, so the connect reaches it through a thread."""
    from raven.acp_client.protocol import AcpConnectionError
    from raven.agent.subagent import probe as probe_mod
    from raven.agent.subagent.probe_state import Remedy

    def _died(*args: object, **kwargs: object) -> object:
        raise AcpConnectionError(_DIED)

    monkeypatch.setattr(probe_mod, "build_third_party_backend", _died)
    result = await probe_mod.ping_agent(_qwen(_installed(tmp_path, "18.20.8")))
    assert result.remedy == Remedy("runtime", None, needs="22", found="18.20.8")


async def test_the_test_button_names_an_old_node_the_way_the_connect_does(tmp_path: Path, monkeypatch) -> None:
    """Test fails on the handshake first; that verdict is read by the same rule as the connect's."""
    from types import SimpleNamespace

    from raven.agent.subagent import probe as probe_mod
    from raven.agent.subagent.probe_state import Remedy

    async def _refused(cfg: object) -> object:
        detail = f"handshake failed: {_DIED}; stderr: at async ModuleJob.run"
        return SimpleNamespace(usable=False, needs_auth=False, unfetched=False, detail=detail, available_models=[])

    monkeypatch.setattr(probe_mod, "record_capabilities", _refused)
    result = await probe_mod.run_test(_qwen(_installed(tmp_path, "18.20.8")), source="config")
    assert not result.ok
    assert result.remedy == Remedy("runtime", None, needs="22", found="18.20.8")
