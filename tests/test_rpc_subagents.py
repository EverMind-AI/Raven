"""Tests for the ``subagents.*`` RPC handlers."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from raven.agent.subagent.acp_registry_presets import ACP_REGISTRY_PRESETS
from raven.config.schema import ThirdPartyCliSubagentConfig
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.errors import ConfigFieldReadonlyError, ConfigValidationError
from raven.rpc.methods.subagents import (
    register_subagents_methods,
    subagents_list,
    subagents_probe,
    subagents_test,
    subagents_test_cancel,
)


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config file the handlers write to instead of the real ~/.raven."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "subagents": {
                    "agents": [
                        {
                            "name": "Coder",
                            "preset": "claude_code",
                            "kind": "cli",
                            "command": "claude -p {prompt} --mcp-config {mcp_file}",
                            "description": "coding",
                            "enabled": True,
                            "mcps": ["github"],
                            "allowMcpSecrets": True,
                        },
                        {
                            "name": "Researcher",
                            "preset": "mirothinker",
                            "kind": "openai",
                            "baseUrl": "https://api.miromind.ai/v1",
                            "model": "m",
                            "apiKey": "sk-secret-value",
                            "enabled": False,
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
    monkeypatch.setattr("raven.rpc.methods.subagents.get_config_path", lambda: path)
    return path


async def _probe_openai_without_network(cfg, *, source):
    """An ``_probe_openai`` stand-in for tests about row shape, not live probing."""
    from raven.agent.subagent.probe import ProbeResult

    return ProbeResult(cfg.name, source, "openai", "unknown", "not probed in this test", "", 0)


@pytest.fixture(autouse=True)
def _skip_live_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``_rows(probe=True)`` off both the login shell and the network.

    ``subagents_list`` probes by default, and the fixture config here holds an
    openai-kind agent, so every call reached a real ``/models`` round trip
    against the address in that config and a real login shell spawned to read
    its PATH. Neither carries a signal these assertions read, and a unit test
    that answers differently when a vendor is down is not one. A test about the
    PATH capture itself sets ``_login_path`` again, which lands after this.
    """
    monkeypatch.setattr("raven.agent.subagent.probe._login_path", lambda: "")
    monkeypatch.setattr("raven.agent.subagent.probe._probe_openai", _probe_openai_without_network)


async def test_list_returns_configured_entries_and_unconfigured_presets(config_path: Path) -> None:
    result = await subagents_list({})
    by_name = {row["name"]: row for row in result["rows"]}

    assert by_name["Coder"]["configured"] is True
    assert by_name["Coder"]["preset"] == "claude_code"
    assert by_name["Coder"]["enabled"] is True
    assert by_name["Coder"]["mcps"] == ["github"]
    assert by_name["Coder"]["allow_mcp_secrets"] is True
    assert by_name["Researcher"]["enabled"] is False
    assert by_name["Researcher"]["mcps"] == []
    assert by_name["Researcher"]["allow_mcp_secrets"] is False
    # A preset with no configured entry still appears, so the overlay can offer it.
    assert by_name["OpenCode"]["configured"] is False


async def test_a_preset_is_withheld_when_a_configured_row_holds_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-written row wearing a preset's shipped name hides that preset.

    Rows carry their agents' official names, so a row someone wrote by hand may
    be called ``OpenCode`` while launching something else. Offering the opencode
    preset beside it would put two rows of one name on the overlay, and every
    verb here addresses a row by name, so the second would be unreachable.

    Withheld here rather than by inferring `preset` from the name: that field is
    read at runtime, so a guess would hand this row opencode's measured MCP
    policy. The row's own provenance therefore stays empty, which
    ``test_a_shipped_name_on_a_hand_written_row_infers_no_provenance`` pins.
    """
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "subagents": {
                    "agents": [
                        {"name": "OpenCode", "kind": "acp", "command": "custom-agent --acp", "enabled": True},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
    monkeypatch.setattr("raven.rpc.methods.subagents.get_config_path", lambda: path)

    result = await subagents_list({"probe": False})
    rows = [r for r in result["rows"] if r["name"] == "OpenCode"]
    assert len(rows) == 1, "the opencode preset must not be offered beside the row holding its name"
    # The wire shape carries booleans rather than the internal source label.
    assert rows[0]["configured"] is True
    assert rows[0]["builtin"] is False
    assert rows[0]["preset"] is None, "a hand-written row is not given provenance it did not declare"
    # Every other preset is still offered: the rule withholds one name, not the group.
    assert "OpenClaw" in {r["name"] for r in result["rows"]}


async def test_list_leaves_out_a_hidden_row(tmp_path: Path, monkeypatch) -> None:
    """The page shows the roster the model reads; a row reached only through
    another row's routes is not on it, so it has no line and no switch here."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "subagents": {
                    "agents": [
                        {"name": "Design", "kind": "acp", "command": "design --acp", "routes": [{"to": "Deck"}]},
                        {"name": "Deck", "kind": "acp", "command": "deck --acp", "hidden": True},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
    monkeypatch.setattr("raven.rpc.methods.subagents.get_config_path", lambda: path)

    names = {r["name"] for r in (await subagents_list({"probe": False}))["rows"]}
    assert "Design" in names
    assert "Deck" not in names


async def test_list_never_returns_an_api_key(config_path: Path) -> None:
    result = await subagents_list({})
    blob = json.dumps(result)
    assert "sk-secret-value" not in blob
    by_name = {row["name"]: row for row in result["rows"]}
    assert by_name["Researcher"]["has_api_key"] is True
    assert "api_key" not in by_name["Researcher"]
    assert "apiKey" not in by_name["Researcher"]


async def test_list_groups_an_openai_entry_by_whether_a_key_is_set(config_path: Path) -> None:
    result = await subagents_list({})
    by_name = {row["name"]: row for row in result["rows"]}
    # The key is present, so the entry is usable regardless of any network probe.
    assert by_name["Researcher"]["group"] == "installed"


async def test_list_groups_a_cli_entry_by_the_probe(config_path: Path, monkeypatch) -> None:
    # `claude` is not on PATH in CI, so the row must land in uninstalled rather
    # than defaulting to installed and offering an agent that cannot run.
    monkeypatch.setattr("raven.agent.subagent.probe._login_path", lambda: "")
    result = await subagents_list({})
    by_name = {row["name"]: row for row in result["rows"]}
    assert by_name["Coder"]["group"] == "uninstalled"
    assert by_name["Coder"]["probe_status"] in {"missing", "unknown"}


async def test_list_groups_an_installed_but_untested_acp_preset_as_installed(
    config_path: Path, tmp_path: Path, monkeypatch
) -> None:
    # An acp row reaches "ready" only from a recorded capability snapshot, and a
    # preset never gets one: `_test_acp` records for every source but `preset`.
    # Grouping acp on "ready" therefore pinned every acp preset to NOT INSTALLED,
    # where the overlay makes an unconfigured row view-only -- so the one action
    # that could have freed it was the one action unavailable there.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "hermes"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    monkeypatch.setattr("raven.agent.subagent.probe._login_path", lambda: str(bindir))
    result = await subagents_list({})
    by_name = {row["name"]: row for row in result["rows"]}
    assert by_name["Hermes Agent"]["probe_status"] == "attention"
    assert by_name["Hermes Agent"]["group"] == "installed"
    # Not an unconditional "installed": this one's executable really is absent,
    # which is the only thing NOT INSTALLED is meant to say.
    assert by_name["OpenClaw"]["probe_status"] == "missing"
    assert by_name["OpenClaw"]["group"] == "uninstalled"


async def test_a_missing_row_names_the_executable_to_install(config_path: Path, tmp_path: Path, monkeypatch) -> None:
    """The page reads what to install off the row, and for a shim preset that is npx.

    Codex launches through `npx`, so on a machine with neither npx nor any agent
    the row is missing for want of Node.js -- which the agent's own installer
    does not bring. A row that is not `missing` names nothing, and neither does
    a listing that measured nothing.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "hermes"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    monkeypatch.setattr("raven.agent.subagent.probe._login_path", lambda: str(bindir))
    by_name = {row["name"]: row for row in (await subagents_list({}))["rows"]}
    assert by_name["Codex"]["probe_status"] == "missing"
    assert by_name["Codex"]["probe_missing"] == "npx"
    assert by_name["OpenClaw"]["probe_missing"] == "openclaw"
    assert by_name["Hermes Agent"]["probe_missing"] is None

    unmeasured = (await subagents_list({"probe": False}))["rows"]
    assert all(row["probe_missing"] is None for row in unmeasured)


async def test_list_surfaces_a_malformed_config_section_instead_of_an_empty_list(
    config_path: Path,
) -> None:
    # An empty overlay reads as "no sub-agents configured", which would send the
    # user off to add ones they already have. The validation error has to reach
    # them as ConfigValidationError, not the -32603 internal_error the dispatcher
    # would otherwise turn a bare ValidationError into.
    config_path.write_text(
        json.dumps({"subagents": {"agents": [{"name": "Broken", "kind": "cli"}]}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError) as excinfo:
        await subagents_list({})
    assert "command" in str(excinfo.value).lower()


async def test_list_with_probe_false_skips_the_network_probe(config_path: Path, monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise AssertionError("probe_all must not be called when probe=False")

    monkeypatch.setattr("raven.rpc.methods.subagents.probe_all", boom)
    result = await subagents_list({"probe": False})
    by_name = {row["name"]: row for row in result["rows"]}

    # Coder/Researcher already claim the claude_code/mirothinker presets, so
    # those two are excluded from the unconfigured-preset rows. The built-in row
    # leads the list: it is on the agent table whether config mentions it or not,
    # so an overlay that omitted it would be hiding the agent every unnamed spawn
    # already dispatches to.
    assert set(by_name) == {
        "Raven",
        "Coder",
        "Researcher",
        "Codex",
        "OpenClaw",
        "OpenCode",
        "Hermes Agent",
    } | {p["name"] for p in ACP_REGISTRY_PRESETS.values()}
    # Their own group, not installed/uninstalled: there is nothing to install, and
    # a row that could only ever read "uninstalled" would say the opposite.
    assert by_name["Raven"]["group"] == "builtin"
    assert by_name["Raven"]["builtin"] is True
    assert by_name["Raven"]["configured"] is False
    assert by_name["Raven"]["enabled"] is True
    assert all(row["probe_status"] == "unknown" for row in result["rows"])
    assert all(row["probe_detail"] == "" for row in result["rows"])
    # group must still resolve correctly without a probe: openai keyed by its
    # api key, cli falls to uninstalled because nothing confirmed it.
    assert by_name["Researcher"]["group"] == "installed"
    assert by_name["Coder"]["group"] == "uninstalled"
    # Fields that need no probe are unaffected.
    assert by_name["Coder"]["enabled"] is True
    assert by_name["Coder"]["configured"] is True
    assert by_name["Coder"]["description"] == "coding"


async def test_list_defaults_to_probing(config_path: Path, monkeypatch) -> None:
    called = False

    async def fake_probe_all(entries, *, verdicts=None):
        nonlocal called
        called = True
        from raven.agent.subagent.probe import probe_all as real_probe_all

        return await real_probe_all(entries, verdicts=verdicts)

    monkeypatch.setattr("raven.rpc.methods.subagents.probe_all", fake_probe_all)
    await subagents_list({})
    assert called is True


async def test_probe_returns_the_same_row_shape_as_list(config_path: Path) -> None:
    listed = await subagents_list({})
    probed = await subagents_probe({})
    assert {r["name"] for r in listed["rows"]} == {r["name"] for r in probed["rows"]}
    assert set(listed["rows"][0]) == set(probed["rows"][0])


def test_register_subagents_methods_registers_list_and_probe() -> None:
    dispatcher = Dispatcher()
    register_subagents_methods(dispatcher)
    assert {"subagents.list", "subagents.probe"} <= set(dispatcher.methods())


from raven.rpc.errors import SubagentNotFoundError
from raven.rpc.methods.subagents import (
    subagents_add,
    subagents_remove,
    subagents_toggle,
    subagents_update,
)


def _stored(path: Path) -> list[dict]:
    return json.loads(path.read_text())["subagents"]["agents"]


async def _pings_ok(cfg):
    """A `ping_agent` stand-in for a toggle test whose fixture command cannot
    really answer one -- a fake path, or a real but empty script. Those tests
    are about bookkeeping (does the right row survive, get dropped, get
    written), not about readiness, so the gate should still run for real
    against the row it composes; only the live model call is stubbed."""
    from raven.agent.subagent.probe import PingResult

    return PingResult(True, "it ran and replied")


async def test_add_writes_the_preset_template_under_a_chosen_name(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("raven.rpc.methods.subagents.ping_agent", _pings_ok)
    out = await subagents_add({"preset": "opencode", "name": "Builder", "description": "builds"})
    assert out == {"added": True, "name": "Builder"}

    entry = next(e for e in _stored(config_path) if e["name"] == "Builder")
    assert entry["preset"] == "opencode"
    # The template's execution fields come from the preset, not the caller -- and
    # they carry the transport the preset fixes, so this is an acp entry with a
    # launch command rather than a task template.
    assert entry["kind"] == "acp"
    assert entry["command"].endswith("acp")
    assert "{prompt}" not in entry["command"]
    assert entry["description"] == "builds"


async def test_add_defaults_name_and_description_to_the_preset(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("raven.rpc.methods.subagents.ping_agent", _pings_ok)
    await subagents_add({"preset": "opencode"})
    entry = next(e for e in _stored(config_path) if e["name"] == "OpenCode")
    assert entry["description"]  # the preset's shipped text, not blank


async def test_add_preserves_empty_mcps_and_false_secret_policy(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("raven.rpc.methods.subagents.ping_agent", _pings_ok)
    await subagents_add(
        {
            "preset": "opencode",
            "name": "Builder",
            "mcps": [],
            "allow_mcp_secrets": False,
        }
    )
    entry = next(e for e in _stored(config_path) if e["name"] == "Builder")
    assert entry["mcps"] == []
    assert entry["allowMcpSecrets"] is False


def _gate_answers(monkeypatch: pytest.MonkeyPatch) -> list:
    """Let the enable gate pass without reaching the agent, and say who it asked.

    Every kind is pinged now, so a test about what a write *stores* has to stand
    in for the agent or it reaches a real process or a real endpoint -- the
    openai rows in this file point at a live base URL, and one of them answered
    a unit test with HTTP 401 before this existed. A test about the gate itself
    installs its own stub instead.
    """
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    asked: list = []

    async def _ok(cfg):
        asked.append(cfg)
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _ok)
    return asked


async def test_add_disables_an_openai_preset_that_has_no_key(config_path: Path) -> None:
    # mirothinker ships an empty apiKey. Added enabled, it would be advertised to
    # the model and fail on first dispatch.
    await subagents_add({"preset": "mirothinker", "name": "Deep"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Deep")
    assert entry["enabled"] is False


async def test_add_keeps_an_openai_preset_enabled_when_a_key_is_supplied(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate_answers(monkeypatch)
    await subagents_add({"preset": "mirothinker", "name": "Deep", "api_key": "sk-live"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Deep")
    assert entry["enabled"] is True


def _as_a_cli_preset(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Make ``subagents_add`` see a ``kind: "cli"`` template, and return it.

    The gate reads ``_PINGED_KINDS``, not the ``acp`` literal, and no shipped
    preset is ``kind: "cli"`` any more -- so without a synthesized template the
    ``cli`` half of that tuple would be unpinned on the add path.
    """
    import raven.rpc.methods.subagents as subagents_mod

    template = {
        "name": "Local",
        "kind": "cli",
        "command": "local -p {prompt}",
        "description": "a local cli agent",
        "enabled": True,
    }
    monkeypatch.setattr(subagents_mod, "third_party_subagent_preset", lambda _name: dict(template))
    return template


async def test_add_proves_a_preset_of_a_pinged_kind_and_lands_it_enabled(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One call, and the row is dispatchable when it returns.

    This test asserted the opposite for one commit (`arrives disabled`, so the
    switch would be the only gated enable path), which turned the page's single
    Connect into add-then-enable: `stageOf` read the added row as configured and
    off, offered Connect again, and only the second click reached the ping. The
    gate belongs on the add for that reason -- the roster's entry criterion for
    these kinds is that the agent answers now, and the add is where the caller
    asked for it.

    `opencode` is an `acp` preset, which is the kind every shipped local preset
    carries today. The `cfg` the gate hands `ping_agent` is captured, because
    what must be proved is the entry this call assembled, not whatever a re-read
    of config would have found under that name.
    """
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    seen = []

    async def _capture(cfg):
        seen.append(cfg)
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _capture)

    assert await subagents_add({"preset": "opencode"}) == {"added": True, "name": "OpenCode"}
    entry = next(e for e in _stored(config_path) if e["name"] == "OpenCode")
    assert entry["kind"] in subagents_mod._PINGED_KINDS
    assert entry["enabled"] is True, "one Connect must leave the row dispatchable"
    assert len(seen) == 1
    assert seen[0].command == entry["command"], "the gate must prove the entry this add assembled"


async def test_add_records_the_capabilities_of_an_acp_row_that_answered(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that answered the ping can be measured, so it is, on the spot.

    Before this the connect proved the agent and recorded nothing: the row it
    landed read "capabilities not recorded -- run a test", stateless (no
    instance, no direct chat) and menuless (no model pill) until someone
    pressed Test or the gateway restarted into the boot backfill.
    """
    import raven.rpc.methods.subagents as subagents_mod

    recorded = []

    async def _record(cfg):
        recorded.append(cfg)
        return None

    monkeypatch.setattr(subagents_mod, "ping_agent", _pings_ok)
    monkeypatch.setattr(subagents_mod, "capabilities_wanted", lambda cfg: True)
    monkeypatch.setattr(subagents_mod, "record_capabilities", _record)

    assert await subagents_add({"preset": "opencode"}) == {"added": True, "name": "OpenCode"}
    assert [c.name for c in recorded] == ["OpenCode"]


async def test_add_does_not_re_measure_a_row_whose_record_is_complete(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import raven.rpc.methods.subagents as subagents_mod

    recorded = []

    async def _record(cfg):
        recorded.append(cfg)

    monkeypatch.setattr(subagents_mod, "ping_agent", _pings_ok)
    monkeypatch.setattr(subagents_mod, "capabilities_wanted", lambda cfg: False)
    monkeypatch.setattr(subagents_mod, "record_capabilities", _record)

    await subagents_add({"preset": "opencode"})
    assert recorded == []


async def test_add_stands_on_the_ping_when_the_capability_record_fails(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent already proved itself; a handshake that fails afterwards is
    logged, not a reason to refuse the connect."""
    import raven.rpc.methods.subagents as subagents_mod

    async def _boom(cfg):
        raise RuntimeError("handshake fell over")

    monkeypatch.setattr(subagents_mod, "ping_agent", _pings_ok)
    monkeypatch.setattr(subagents_mod, "capabilities_wanted", lambda cfg: True)
    monkeypatch.setattr(subagents_mod, "record_capabilities", _boom)

    assert await subagents_add({"preset": "opencode"}) == {"added": True, "name": "OpenCode"}
    assert next(e for e in _stored(config_path) if e["name"] == "OpenCode")["enabled"] is True


async def test_add_proves_a_cli_preset_the_same_way(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    template = _as_a_cli_preset(monkeypatch)

    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    seen = []

    async def _capture(cfg):
        seen.append(cfg)
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _capture)

    await subagents_add({"preset": "opencode"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Local")
    assert entry["kind"] == "cli"
    assert entry["enabled"] is True
    assert [c.command for c in seen] == [template["command"]]


async def test_a_refused_add_stores_nothing_at_all(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not the row disabled -- no row.

    This is the half of the contract that keeps Connect one verb on the failure
    path too: a stored-but-off row reads as `stageOf` stage `off`, which offers
    Connect a second time for an agent that just refused to answer, and the
    second click would take the *other* branch (`subagents.toggle`). With
    nothing stored the row stays at stage `add`, the reason is toasted, and the
    one thing to do about it is still Connect.
    """
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    before = _stored(config_path)

    async def _refuses(cfg):
        return PingResult(False, "Internal error: You need to sign in to use this model.")

    monkeypatch.setattr(subagents_mod, "ping_agent", _refuses)

    with pytest.raises(subagents_mod.SubagentNotReadyError) as caught:
        await subagents_add({"preset": "opencode"})

    assert "sign in" in str(caught.value), "the refusal must carry the agent's own reason"
    assert caught.value.data is not None
    assert "sign in" in caught.value.data["detail"], "the UI reads data.detail, not just the message"
    assert [e for e in _stored(config_path) if e["name"] == "OpenCode"] == [], "a refused add must store no row"
    assert _stored(config_path) == before, "and must leave the rest of the list alone"


async def test_a_refused_add_carries_the_fix_it_names(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The page draws the fix from `data.remedy`; `data.detail` stays the sentence.

    Only when the ping named one: a refusal it could not classify must not grow
    a fix the page would then draw in place of the agent's own words.
    """
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult
    from raven.agent.subagent.probe_state import Remedy

    async def _named(cfg):
        return PingResult(False, "it is installed but has no usable credential", Remedy("sign_in", "claude auth login"))

    monkeypatch.setattr(subagents_mod, "ping_agent", _named)
    with pytest.raises(subagents_mod.SubagentNotReadyError) as caught:
        await subagents_add({"preset": "opencode"})
    assert caught.value.data["remedy"] == {"kind": "sign_in", "command": "claude auth login"}
    assert "no usable credential" in caught.value.data["detail"]

    async def _unnamed(cfg):
        return PingResult(False, "connection ended (exit 127)")

    monkeypatch.setattr(subagents_mod, "ping_agent", _unnamed)
    with pytest.raises(subagents_mod.SubagentNotReadyError) as caught:
        await subagents_add({"preset": "opencode"})
    assert "remedy" not in caught.value.data


async def test_a_refused_add_that_npx_could_not_fetch_carries_the_download_fix(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult
    from raven.agent.subagent.probe_state import Remedy

    command = "npx -y @agentclientprotocol/codex-acp@1.1.14"

    async def _unfetched(cfg):
        return PingResult(False, "npx could not download it (ENOTFOUND)", Remedy("download", command))

    monkeypatch.setattr(subagents_mod, "ping_agent", _unfetched)
    with pytest.raises(subagents_mod.SubagentNotReadyError) as caught:
        await subagents_add({"preset": "codex"})
    assert caught.value.data["remedy"] == {"kind": "download", "command": command}


async def test_a_refused_add_of_a_cli_preset_stores_nothing_either(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_a_cli_preset(monkeypatch)

    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    before = _stored(config_path)

    async def _refuses(cfg):
        return PingResult(False, "Internal error: You need to sign in to use this model.")

    monkeypatch.setattr(subagents_mod, "ping_agent", _refuses)

    with pytest.raises(subagents_mod.SubagentNotReadyError):
        await subagents_add({"preset": "opencode"})

    assert _stored(config_path) == before


async def test_force_adds_a_preset_of_a_pinged_kind_without_a_ping(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same escape hatch as the switch's, and the same strictness: a working
    agent whose provider is briefly down must not be un-addable."""
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    seen = []

    async def _capture(cfg):
        seen.append(cfg)
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _capture)

    await subagents_add({"preset": "opencode", "force": True})
    entry = next(e for e in _stored(config_path) if e["name"] == "OpenCode")
    assert entry["enabled"] is True
    assert seen == [], "force must skip the ping entirely, not just tolerate its answer"


async def test_a_truthy_non_boolean_force_does_not_skip_the_add_gate(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`force` is declared a boolean here too, and `"no"` is truthy in Python
    while reading as a refusal to whoever sent it."""
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    seen = []

    async def _refuses(cfg):
        seen.append(cfg)
        return PingResult(False, "Internal error: You need to sign in to use this model.")

    monkeypatch.setattr(subagents_mod, "ping_agent", _refuses)

    with pytest.raises(subagents_mod.SubagentNotReadyError):
        await subagents_add({"preset": "opencode", "force": "no"})

    assert len(seen) == 1, "a non-boolean force must not skip the ping"
    assert [e for e in _stored(config_path) if e["name"] == "OpenCode"] == []


async def test_add_pings_an_openai_preset_it_is_about_to_enable(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An endpoint answers the same question every other kind does, by answering.

    The free models probe settles whether the endpoint knows the credential, and
    it runs nowhere near this call -- the key being added here has never been
    probed, because it did not exist when the listing last ran. So the gate asks
    the endpoint, like every other kind. The keyless rule is unchanged and is
    what keeps this affordable: that row lands disabled, and a row that is not
    being enabled is never pinged."""
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    seen = []

    async def _capture(cfg):
        seen.append(cfg)
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _capture)

    await subagents_add({"preset": "mirothinker", "name": "Keyed", "api_key": "sk-live"})
    await subagents_add({"preset": "mirothinker", "name": "Keyless"})

    stored = {e["name"]: e for e in _stored(config_path)}
    assert stored["Keyed"]["enabled"] is True
    assert stored["Keyless"]["enabled"] is False
    assert [c.name for c in seen] == ["Keyed"], "the keyed add is gated; the keyless one lands disabled unasked"


async def test_a_write_that_lands_during_an_adds_ping_is_not_reverted(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The add's gate has the same await between its read and its write that the
    switch's has, and `set_agents` replaces the whole list -- so writing the
    list read before a ping of up to a minute would silently revert every other
    `subagents.*` write that landed during it, each of which has already
    answered success to its own client."""
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def _hangs_until_released(cfg):
        in_flight.set()
        await release.wait()
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _hangs_until_released)

    adding = asyncio.ensure_future(subagents_add({"preset": "opencode"}))
    await in_flight.wait()
    # Another client, on the ordinary path. It has to be a write that cannot
    # reach the gate itself, or it would block on the very stub this test is
    # holding open: a description changes nothing the agent could answer, so it
    # is never asked. A switch-on would be, now that every kind is.
    assert await subagents_update({"name": "Researcher", "description": "mid-flight"}) == {
        "updated": True,
        "name": "Researcher",
    }
    assert {e["name"]: e.get("description") for e in _stored(config_path)}["Researcher"] == "mid-flight"

    release.set()
    assert await adding == {"added": True, "name": "OpenCode"}

    after = {e["name"]: e for e in _stored(config_path)}
    assert after["OpenCode"].get("enabled") is True, "the added row must survive"
    assert after["Researcher"].get("description") == "mid-flight", (
        "the write made during the ping must survive the add's write"
    )


async def test_add_rejects_a_duplicate_name_without_writing_or_pinging(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collision is not a readiness question, and the schema keeps the first of
    two rows carrying one name -- so a gate that ran ahead of this refusal would
    ping the *stored* `Coder` and report "did not answer" for what is really a
    name already in use. The write's own refusals come first; only then is the
    agent's quota spent."""
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    seen = []

    async def _capture(cfg):
        seen.append(cfg)
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _capture)

    before = _stored(config_path)
    # Surfaced as ConfigValidationError (-32011), not the -32603 internal_error
    # the dispatcher would otherwise turn a bare ValueError into.
    with pytest.raises(ConfigValidationError, match="Coder"):
        await subagents_add({"preset": "opencode", "name": "Coder"})
    assert _stored(config_path) == before
    assert seen == [], "an add the write was always going to refuse must spend nothing"


async def test_add_trims_a_padded_name(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("raven.rpc.methods.subagents.ping_agent", _pings_ok)
    await subagents_add({"preset": "opencode", "name": "  Builder  "})
    names = [e["name"] for e in _stored(config_path)]
    assert "Builder" in names
    assert "  Builder  " not in names


async def test_add_rejects_a_whitespace_only_name(config_path: Path) -> None:
    before = _stored(config_path)
    with pytest.raises(ConfigValidationError):
        await subagents_add({"preset": "opencode", "name": "   "})
    assert _stored(config_path) == before


async def test_add_rejects_an_unknown_preset(config_path: Path) -> None:
    with pytest.raises(SubagentNotFoundError):
        await subagents_add({"preset": "no_such_preset"})


async def test_update_renames_and_keeps_the_stored_key_when_blank(config_path: Path) -> None:
    out = await subagents_update({"name": "Researcher", "new_name": "Deep", "api_key": None})
    assert out == {"updated": True, "name": "Deep"}
    entry = next(e for e in _stored(config_path) if e["name"] == "Deep")
    # Blank means keep: the caller never sees the stored key, so it cannot resend it.
    assert entry["apiKey"] == "sk-secret-value"


async def test_update_replaces_the_key_when_one_is_given(config_path: Path) -> None:
    await subagents_update({"name": "Researcher", "api_key": "sk-new"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["apiKey"] == "sk-new"


async def test_update_keeps_the_stored_key_when_it_is_an_empty_string(config_path: Path) -> None:
    await subagents_update({"name": "Researcher", "api_key": ""})
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["apiKey"] == "sk-secret-value"


async def test_update_keeps_the_stored_key_when_it_is_whitespace_only(config_path: Path) -> None:
    await subagents_update({"name": "Researcher", "api_key": "   "})
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["apiKey"] == "sk-secret-value"


async def test_update_keeps_the_stored_key_when_it_is_a_tab(config_path: Path) -> None:
    await subagents_update({"name": "Researcher", "api_key": "\t"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["apiKey"] == "sk-secret-value"


async def test_update_leaves_execution_fields_alone(config_path: Path) -> None:
    before = next(e for e in _stored(config_path) if e["name"] == "Coder")
    await subagents_update({"name": "Coder", "description": "new text"})
    after = next(e for e in _stored(config_path) if e["name"] == "Coder")
    assert after["command"] == before["command"]
    assert after["description"] == "new text"
    assert after["mcps"] == ["github"]
    assert after["allowMcpSecrets"] is True


async def test_update_can_clear_mcps_and_disable_secret_forwarding(config_path: Path) -> None:
    await subagents_update({"name": "Coder", "mcps": [], "allow_mcp_secrets": False})
    entry = next(e for e in _stored(config_path) if e["name"] == "Coder")
    assert entry["mcps"] == []
    assert entry["allowMcpSecrets"] is False


async def test_update_rejects_mcp_fields_for_an_openai_agent(config_path: Path) -> None:
    before = _stored(config_path)
    with pytest.raises(ConfigValidationError, match="no tool loop"):
        await subagents_update({"name": "Researcher", "mcps": ["github"]})
    with pytest.raises(ConfigValidationError, match="no tool loop"):
        await subagents_update({"name": "Researcher", "allow_mcp_secrets": True})
    assert _stored(config_path) == before


async def test_update_materializes_a_vendored_mcp_override(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendored = ThirdPartyCliSubagentConfig(
        name="Raven-Probe",
        command="raven-probe {prompt_file} {mcp_file}",
        description="shipped",
    )
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda: [vendored],
    )

    out = await subagents_update(
        {
            "name": "Raven-Probe",
            "new_name": "Raven-Probe",
            "mcps": ["github"],
            "allow_mcp_secrets": True,
        }
    )

    assert out == {"updated": True, "name": "Raven-Probe"}
    entry = next(e for e in _stored(config_path) if e["name"] == "Raven-Probe")
    assert entry["command"] == "raven-probe {prompt_file} {mcp_file}"
    assert entry["mcps"] == ["github"]
    assert entry["allowMcpSecrets"] is True


async def test_a_vendored_override_cannot_be_renamed(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendored = ThirdPartyCliSubagentConfig(
        name="Raven-Probe",
        command="raven-probe {prompt_file} {mcp_file}",
    )
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda: [vendored],
    )

    with pytest.raises(ConfigFieldReadonlyError, match="cannot be renamed"):
        await subagents_update({"name": "Raven-Probe", "new_name": "Renamed"})


async def test_update_rejects_an_unknown_name(config_path: Path) -> None:
    with pytest.raises(SubagentNotFoundError):
        await subagents_update({"name": "nope", "description": "x"})


async def test_update_rejects_a_rename_onto_an_existing_name(config_path: Path) -> None:
    # Surfaced as ConfigValidationError (-32011) with the real reason, not the
    # -32603 internal_error a bare duplicate-name ValueError would become.
    with pytest.raises(ConfigValidationError, match="Coder"):
        await subagents_update({"name": "Researcher", "new_name": "Coder"})


async def test_update_trims_a_padded_new_name(config_path: Path) -> None:
    await subagents_update({"name": "Coder", "new_name": "  Renamed  "})
    names = [e["name"] for e in _stored(config_path)]
    assert "Renamed" in names
    assert "  Renamed  " not in names


async def test_update_rejects_a_whitespace_only_new_name(config_path: Path) -> None:
    with pytest.raises(ConfigValidationError):
        await subagents_update({"name": "Coder", "new_name": "   "})
    names = [e["name"] for e in _stored(config_path)]
    assert "Coder" in names


async def test_update_clearing_the_description_reverts_to_the_preset_default(config_path: Path) -> None:
    # Coder is a "claude_code" preset entry whose stored description ("coding")
    # was overridden at add-time; blanking it must fall back to the preset's
    # shipped text, exactly like add does, not store an empty string.
    from raven.agent.subagent.presets import third_party_subagent_preset

    await subagents_update({"name": "Coder", "description": "   "})
    entry = next(e for e in _stored(config_path) if e["name"] == "Coder")
    assert entry["description"] == third_party_subagent_preset("claude_code")["description"]
    assert entry["description"]


async def test_update_clearing_the_description_with_no_preset_blanks_it(config_path: Path) -> None:
    # A hand-written entry (no `preset` provenance) has no default to fall back
    # to, so clearing it must actually clear it.
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append(
        {"name": "Handwritten", "kind": "cli", "command": "cat", "description": "custom text", "enabled": True}
    )
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    await subagents_update({"name": "Handwritten", "description": ""})
    entry = next(e for e in _stored(config_path) if e["name"] == "Handwritten")
    assert entry["description"] == ""


async def test_update_whole_entry_validation_failure_does_not_leak_the_api_key(config_path: Path, monkeypatch) -> None:
    # A corrupt on-disk section that fails schema validation as a whole entry
    # (not just a rejected field) makes pydantic's str(ValidationError) embed the
    # entire offending dict via its `input_value=...` diagnostic - including a
    # plaintext apiKey. Neither the raised error's message nor its `data` may
    # contain the key or the substring "input_value".
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append({"name": "Y", "kind": "openai", "apiKey": "sk-CANARY-B"})
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigValidationError) as excinfo:
        await subagents_list({})

    exc = excinfo.value
    blob = f"{exc.message} {exc.detail} {json.dumps(exc.data or {})}"
    assert "sk-CANARY-B" not in blob
    assert "input_value" not in blob


async def test_update_reproves_a_live_row_whose_credential_changed(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key swapped under a row that is already on is a connect nobody gated.

    The row goes on serving dispatches with a credential nothing has tried, so
    the first real task is what discovers a typo. The update asks the same
    question the switch asks, and a refusal leaves the stored key alone --
    otherwise the refusal would still have taken the working key away.
    """
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    asked = _gate_answers(monkeypatch)
    await subagents_toggle({"name": "Researcher", "enabled": True})
    asked.clear()

    async def _refuse(cfg):
        asked.append(cfg)
        return PingResult(False, 'HTTP 401: {"error":"invalid api key"}')

    monkeypatch.setattr(subagents_mod, "ping_agent", _refuse)

    with pytest.raises(subagents_mod.SubagentNotReadyError):
        await subagents_update({"name": "Researcher", "api_key": "sk-wrong"})

    assert [c.name for c in asked] == ["Researcher"]
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["apiKey"] == "sk-secret-value", "a refused update must leave the working key on disk"


async def test_update_reproves_a_live_row_whose_model_changed(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same question: an agent need not still serve the
    model it is asked for, and the row would carry the new name until something
    ran. Only an acp row reaches this -- an openai row's model is fixed, and a
    built-in one is this process, which the gate never asks.
    """
    from raven.acp_client.capabilities import AcpModelChoice, CapabilitySnapshot, SnapshotStore, snapshot_fingerprint
    from raven.config.schema import SubagentsConfig

    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append(
        {"name": "Coded", "kind": "acp", "command": "coded acp", "description": "d", "enabled": True, "model": "v/m"}
    )
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    store_path = tmp_path / "caps.json"
    monkeypatch.setattr("raven.acp_client.capabilities.default_snapshot_path", lambda: store_path)
    cfg = next(c for c in SubagentsConfig(agents=raw["subagents"]["agents"]).agents if c.name == "Coded")
    SnapshotStore(path=store_path).record(
        CapabilitySnapshot(
            agent="Coded",
            fingerprint=snapshot_fingerprint(cfg),
            status="ready",
            detail="",
            measured_at_ms=1,
            agent_name="other-agent",
            model_choices=(
                AcpModelChoice(value="v/m", name="M", group="V"),
                AcpModelChoice(value="v/m2", name="M2", group="V"),
            ),
        )
    )

    asked = _gate_answers(monkeypatch)
    await subagents_update({"name": "Coded", "model": "v/m2"})

    assert [c.name for c in asked] == ["Coded"]
    entry = next(e for e in _stored(config_path) if e["name"] == "Coded")
    assert entry["model"] == "v/m2"


async def test_update_asks_nothing_when_neither_credential_nor_model_moved(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit that cannot change what the agent answers is not worth a call.

    Re-sending the same key counts as not moving: the sheet posts what is in the
    field, so an unchanged form would otherwise spend one on every save.
    """
    asked = _gate_answers(monkeypatch)
    await subagents_toggle({"name": "Researcher", "enabled": True})
    asked.clear()

    await subagents_update({"name": "Researcher", "description": "new words"})
    await subagents_update({"name": "Researcher", "api_key": "sk-secret-value"})

    assert asked == [], "neither a description nor an unchanged key reaches the agent"


async def test_update_asks_nothing_of_a_row_that_is_switched_off(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is serving dispatches, and switching it on is already gated, so
    asking here would spend a second call to learn the same thing."""
    asked = _gate_answers(monkeypatch)

    await subagents_update({"name": "Researcher", "api_key": "sk-new"})

    assert asked == [], "an off row is proved by the switch that turns it on"
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["apiKey"] == "sk-new"


async def test_toggle_flips_enabled(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _gate_answers(monkeypatch)
    assert await subagents_toggle({"name": "Researcher", "enabled": True}) == {"enabled": True}
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["enabled"] is True


async def test_toggling_a_discovered_folder_writes_it_into_the_registry(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discovered row has no config entry, so the switch has nowhere to live.

    It gets one: the same list, the same ``enabled`` field every other agent's
    switch is written to. Before this, the one verb the page offers for these
    rows answered "no configured sub-agent named ...".
    """
    from raven.config.schema import ThirdPartyCliSubagentConfig

    discovered = ThirdPartyCliSubagentConfig(
        name="Raven-Probe", kind="cli", command="/tmp/py /tmp/raven-probe/run.py", enabled=True
    )
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [discovered],
    )

    assert await subagents_toggle({"name": "Raven-Probe", "enabled": False}) == {"enabled": False}

    stored = next(e for e in _stored(config_path) if e["name"] == "Raven-Probe")
    assert stored["enabled"] is False
    # And it is a switch, not a definition: no launcher copied out of the
    # manifest, because a copy outlives the manifest it was taken from. The
    # folder still defines the agent; this row only says "not this one".
    assert stored["command"] == ""
    assert stored["switchOnly"] is True


async def test_switching_a_discovered_folder_back_on_drops_its_switch_row(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On removes the row rather than storing ``enabled: true``.

    For a discovered folder, no row IS the answer: the folder governs, and a
    stored row wins whole over the discovered one. A row saying true would
    outlive its folder and override a later readiness failure, which is a name
    that cannot start back on the dispatch roster.
    """
    from raven.config.schema import ThirdPartyCliSubagentConfig

    discovered = ThirdPartyCliSubagentConfig(
        name="Raven-Probe", kind="cli", command="/tmp/py /tmp/raven-probe/run.py", enabled=True
    )
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [discovered],
    )
    await subagents_toggle({"name": "Raven-Probe", "enabled": False})
    assert any(e["name"] == "Raven-Probe" for e in _stored(config_path))

    # The fixture's command cannot really answer one; the gate must still run
    # for real against the row it composes, so only the live call is stubbed.
    monkeypatch.setattr("raven.rpc.methods.subagents.ping_agent", _pings_ok)
    assert await subagents_toggle({"name": "Raven-Probe", "enabled": True}) == {"enabled": True}

    assert [e for e in _stored(config_path) if e["name"] == "Raven-Probe"] == []


async def test_switching_a_discovered_folder_on_with_no_row_writes_nothing(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to remove and nothing to write: the folder already says yes, or
    says no through its readiness, and either way that is the answer."""
    from raven.config.schema import ThirdPartyCliSubagentConfig

    discovered = ThirdPartyCliSubagentConfig(
        name="Raven-Probe", kind="cli", command="/tmp/py /tmp/raven-probe/run.py", enabled=True
    )
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [discovered],
    )

    monkeypatch.setattr("raven.rpc.methods.subagents.ping_agent", _pings_ok)
    assert await subagents_toggle({"name": "Raven-Probe", "enabled": True}) == {"enabled": True}

    assert [e for e in _stored(config_path) if e["name"] == "Raven-Probe"] == []


async def test_enabling_a_discovered_folder_for_the_first_time_pings_its_own_command(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No stored row exists yet -- the first-time-enable shape the feature
    exists for. Composing the gate from a fresh disk read instead of this
    call's own ``entries`` found nothing and raised ``SubagentNotFoundError``
    before a ping ever ran; this asserts the ping runs, and runs against the
    folder's own command, by capturing the ``cfg`` the gate actually hands it."""
    from raven.config.schema import ThirdPartyCliSubagentConfig

    discovered = ThirdPartyCliSubagentConfig(
        name="Raven-Probe", kind="cli", command="/tmp/py /tmp/raven-probe/run.py", enabled=True
    )
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [discovered],
    )

    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    seen = []

    async def _capture(cfg):
        seen.append(cfg)
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _capture)

    assert await subagents_toggle({"name": "Raven-Probe", "enabled": True}) == {"enabled": True}
    assert len(seen) == 1
    assert seen[0].command == "/tmp/py /tmp/raven-probe/run.py"


async def test_re_enabling_a_switched_off_discovered_folder_pings_its_real_command(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off writes a stub with ``command: ""``. Composing the gate from a fresh
    disk read instead of this call's own ``entries`` still saw that stub after
    the toggle-on had already dropped it in memory, so the gate pinged an empty
    command and could never pass. This asserts the ping receives the folder's
    real command instead, by capturing the ``cfg`` the gate actually hands it."""
    from raven.config.schema import ThirdPartyCliSubagentConfig

    discovered = ThirdPartyCliSubagentConfig(
        name="Raven-Probe", kind="cli", command="/tmp/py /tmp/raven-probe/run.py", enabled=True
    )
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [discovered],
    )
    await subagents_toggle({"name": "Raven-Probe", "enabled": False})
    assert any(e["name"] == "Raven-Probe" for e in _stored(config_path)), "off must write the stub this test relies on"

    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    seen = []

    async def _capture(cfg):
        seen.append(cfg)
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _capture)

    assert await subagents_toggle({"name": "Raven-Probe", "enabled": True}) == {"enabled": True}
    assert len(seen) == 1
    assert seen[0].command == "/tmp/py /tmp/raven-probe/run.py"


async def test_switching_on_keeps_a_row_nobody_marked_as_a_switch(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored row that the switch did not write -- an ``install.py`` entry, a
    hand edit -- is somebody's real override, and the switch may not delete it.

    Provenance decides, not a comparison with the discovered entry: a manifest
    that has moved on leaves a switch row differing in fields nobody chose.
    """
    from raven.config.schema import ThirdPartyCliSubagentConfig
    from raven.config.update_subagents import set_agents

    discovered = ThirdPartyCliSubagentConfig(
        name="Raven-Probe", kind="cli", command="/tmp/py /tmp/raven-probe/run.py", enabled=True
    )
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [discovered],
    )
    # What install.py writes: a complete entry, and no switch marker on it.
    set_agents(
        [
            *_stored(config_path),
            {"name": "Raven-Probe", "kind": "cli", "command": "/opt/mine/run.py", "enabled": False},
        ],
        config_path=config_path,
    )

    assert await subagents_toggle({"name": "Raven-Probe", "enabled": True, "force": True}) == {"enabled": True}

    rows = [e for e in _stored(config_path) if e["name"] == "Raven-Probe"]
    assert len(rows) == 1
    assert rows[0]["enabled"] is True
    assert rows[0]["command"] == "/opt/mine/run.py"


async def test_a_drifted_switch_row_cannot_override_the_folder_it_names(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switch a folder off at v1, upgrade the folder to v2, and the stored copy
    no longer matches what was discovered.

    Two things hold it. The row carries provenance, so switching back on still
    drops it; and while it is stored, the merge reads only its flag, so v2's own
    fields are what the roster sees.
    """
    from raven.agent.subagent.vendored_agents import merge_product_seeds
    from raven.config.schema import ThirdPartyCliSubagentConfig

    launcher = config_path.parent / "run.py"
    launcher.write_text("")
    v1 = ThirdPartyCliSubagentConfig(
        name="Raven-Probe", kind="cli", command=f"{sys.executable} {launcher}", description="v1", enabled=True
    )
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [v1],
    )
    await subagents_toggle({"name": "Raven-Probe", "enabled": False})

    # The folder upgrades: a new description, and it is not ready this time.
    v2 = v1.model_copy(update={"description": "v2", "enabled": False})
    stored = [ThirdPartyCliSubagentConfig.model_validate(e) for e in _stored(config_path) if e["name"] == "Raven-Probe"]
    merged = merge_product_seeds(stored, [v2])
    assert [(r.name, r.description, r.enabled) for r in merged] == [("Raven-Probe", "v2", False)]

    # And the switch still knows the row is its own, drift or no drift.
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [v2],
    )
    monkeypatch.setattr("raven.rpc.methods.subagents.ping_agent", _pings_ok)
    assert await subagents_toggle({"name": "Raven-Probe", "enabled": True}) == {"enabled": True}
    assert [e for e in _stored(config_path) if e["name"] == "Raven-Probe"] == []


async def test_a_switch_row_that_lost_its_marker_is_still_only_a_flag(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raven too old to know the marker accepts the row, drops the field, and
    persists it without one on its next rewrite of this list -- so provenance
    cannot be the only thing holding the switch apart from a real override.

    A row that is the discovered entry with nothing but its flag changed carries
    no information beyond that flag whoever wrote it, so it is read as a switch
    too: the merge takes only the flag, and switching the folder back on removes
    the row.
    """
    from raven.agent.subagent.vendored_agents import merge_product_seeds
    from raven.config.schema import ThirdPartyCliSubagentConfig
    from raven.config.update_subagents import set_agents

    launcher = config_path.parent / "run.py"
    launcher.write_text("")
    discovered = ThirdPartyCliSubagentConfig(
        name="Raven-Probe", kind="cli", command=f"{sys.executable} {launcher}", enabled=True
    )
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [discovered],
    )
    # What the downgrade leaves behind: the copy, no marker, and `enabled: true`
    # -- which is what an older raven's own switch wrote into the row. The flag
    # has to be true for this to test anything: with the copy off, an override
    # and an overlay both answer "disabled" and the assertion cannot fail.
    set_agents(
        [
            *_stored(config_path),
            {
                "name": "Raven-Probe",
                "kind": "cli",
                "command": f"{sys.executable} {launcher}",
                "enabled": True,
            },
        ],
        config_path=config_path,
    )
    stored = [ThirdPartyCliSubagentConfig.model_validate(e) for e in _stored(config_path) if e["name"] == "Raven-Probe"]
    assert stored and stored[0].switch_only is False, "the row under test must carry no marker"

    # The folder stops being ready. An override would carry its own true through;
    # a switch may only take a row out, never put an unstartable one back.
    not_ready = discovered.model_copy(update={"enabled": False})
    merged = merge_product_seeds(stored, [not_ready])
    assert [(r.name, r.enabled) for r in merged] == [("Raven-Probe", False)]

    # And the switch still knows the row for what it is, marker or no marker.
    assert await subagents_toggle({"name": "Raven-Probe", "enabled": True, "force": True}) == {"enabled": True}
    assert [e for e in _stored(config_path) if e["name"] == "Raven-Probe"] == []


async def test_a_switch_row_survives_an_older_rewrite_and_a_folder_upgrade(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape a rollback actually has. A bundled folder travels with the raven
    that ships it, so going back a version and forward again drops the row's
    marker (an older build ignores the field and rewrites the list without it)
    *and* moves the manifest on. A row that had copied the manifest then looks
    exactly like somebody's override of a folder that has changed.

    The switch row declares no launcher, which is a field every version keeps and
    which says nothing about the folder, so neither half of that can disguise it.
    """
    from raven.agent.subagent.vendored_agents import merge_product_seeds
    from raven.config.schema import ThirdPartyCliSubagentConfig
    from raven.config.update_subagents import set_agents

    launcher = config_path.parent / "run.py"
    launcher.write_text("")
    v1 = ThirdPartyCliSubagentConfig(
        name="Raven-Probe", kind="cli", command=f"{sys.executable} {launcher}", description="v1", enabled=True
    )
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [v1],
    )
    await subagents_toggle({"name": "Raven-Probe", "enabled": False})

    # The older build's rewrite: every field it knows, and the marker gone. Its
    # own switch also wrote `enabled: true` before the upgrade.
    kept = [e for e in _stored(config_path) if e["name"] != "Raven-Probe"]
    set_agents(
        [*kept, {"name": "Raven-Probe", "kind": "cli", "command": "", "enabled": True}],
        config_path=config_path,
    )
    stored = [ThirdPartyCliSubagentConfig.model_validate(e) for e in _stored(config_path) if e["name"] == "Raven-Probe"]
    assert stored and stored[0].switch_only is False, "the row under test must carry no marker"

    # And the folder upgrades: new description, and not ready this time.
    v2 = v1.model_copy(update={"description": "v2", "enabled": False})
    merged = merge_product_seeds(stored, [v2])
    assert [(r.name, r.description, r.enabled) for r in merged] == [("Raven-Probe", "v2", False)]

    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [v2],
    )
    monkeypatch.setattr("raven.rpc.methods.subagents.ping_agent", _pings_ok)
    assert await subagents_toggle({"name": "Raven-Probe", "enabled": True}) == {"enabled": True}
    assert [e for e in _stored(config_path) if e["name"] == "Raven-Probe"] == []


async def test_an_openai_row_is_not_mistaken_for_a_switch(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An openai agent declares no command at all. Reading that as an empty one
    made every such row look like a switch for a folder, and the merge dropped
    it: the fixture roster lost `Researcher` entirely."""
    from raven.agent.subagent.vendored_agents import merge_product_seeds
    from raven.config.schema import ThirdPartyOpenAISubagentConfig

    row = ThirdPartyOpenAISubagentConfig(
        name="Researcher", kind="openai", base_url="https://api.example/v1", model="m", enabled=True
    )
    assert not hasattr(row, "command")

    assert [r.name for r in merge_product_seeds([row], [])] == ["Researcher"]


async def test_an_orphaned_stub_can_never_be_an_agent(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The last way round: an older rewrite takes the marker off a switch stub,
    and then the folder it named goes. Nothing left says "switch", and the row
    names no launcher -- so it must not be dispatchable, and asking to enable it
    must not write a yes the roster would have to overrule.
    """
    from raven.agent.subagent.vendored_agents import merge_product_seeds
    from raven.config.schema import ThirdPartyCliSubagentConfig
    from raven.config.update_subagents import set_agents

    orphan = {"name": "Raven-Probe", "kind": "cli", "command": "", "enabled": True}
    set_agents(
        [*[e for e in _stored(config_path) if e["name"] != "Raven-Probe"], orphan],
        config_path=config_path,
    )
    stored = [ThirdPartyCliSubagentConfig.model_validate(e) for e in _stored(config_path) if e["name"] == "Raven-Probe"]
    assert stored and stored[0].switch_only is False and stored[0].enabled is True

    # Carried through, because deleting a row nobody asked to delete is not the
    # merge's business -- but never enabled, because there is nothing to run.
    merged = merge_product_seeds(stored, [])
    assert [(r.name, r.enabled) for r in merged] == [("Raven-Probe", False)]

    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [],
    )
    assert await subagents_toggle({"name": "Raven-Probe", "enabled": True}) == {"enabled": False}
    row = next(e for e in _stored(config_path) if e["name"] == "Raven-Probe")
    assert row["enabled"] is False


async def test_an_empty_command_alone_is_not_a_switch(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An acp row is allowed to carry an empty command, and one may exist for a
    name no folder has anything to do with. Reading that as a switch dropped a
    configured agent from the roster."""
    from raven.agent.subagent.vendored_agents import merge_product_seeds
    from raven.config.schema import ThirdPartyAcpSubagentConfig

    row = ThirdPartyAcpSubagentConfig.model_validate({"name": "startup", "kind": "acp", "command": ""})

    # Nothing discovered under that name: the row is the only thing there is.
    assert [r.name for r in merge_product_seeds([row], [])] == ["startup"]


async def test_a_switch_row_for_a_folder_that_is_gone_drops_out(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing discovered under that name means the switch is a switch for
    nothing: it leaves the roster rather than standing in as an agent."""
    from raven.agent.subagent.vendored_agents import merge_product_seeds
    from raven.config.schema import ThirdPartyCliSubagentConfig

    launcher = config_path.parent / "run.py"
    launcher.write_text("")
    row = ThirdPartyCliSubagentConfig(
        name="Raven-Probe", kind="cli", command=f"{sys.executable} {launcher}", enabled=True
    )
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [row],
    )
    await subagents_toggle({"name": "Raven-Probe", "enabled": False})
    stored = [ThirdPartyCliSubagentConfig.model_validate(e) for e in _stored(config_path) if e["name"] == "Raven-Probe"]

    assert merge_product_seeds(stored, []) == []


async def test_a_folder_switched_on_still_obeys_its_own_readiness(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Driven through the merge that decides the roster.

    A stored ``enabled: true`` for a discovered folder wins whole over the
    discovered row, so it overrode a later readiness failure and put a name that
    cannot start back where the dispatching model reads. With the switch row
    dropped instead, the folder's verdict is the only one there is.
    """
    import sys

    from raven.agent.subagent.vendored_agents import merge_product_seeds
    from raven.config.schema import ThirdPartyCliSubagentConfig

    # A command whose absolute tokens all exist. With a made-up path the merge
    # skips the stored row as a stale launcher and the assertion below holds
    # whatever the switch wrote -- an assertion that cannot fail.
    launcher = config_path.parent / "run.py"
    launcher.write_text("")
    ready = ThirdPartyCliSubagentConfig(
        name="Raven-Probe", kind="cli", command=f"{sys.executable} {launcher}", enabled=True
    )
    monkeypatch.setattr(
        "raven.agent.subagent.vendored_agents.discover_product_rows",
        lambda root=None: [ready],
    )
    await subagents_toggle({"name": "Raven-Probe", "enabled": False})
    monkeypatch.setattr("raven.rpc.methods.subagents.ping_agent", _pings_ok)
    await subagents_toggle({"name": "Raven-Probe", "enabled": True})

    # The venv breaks, or the launcher goes: discovery reports the row disabled.
    not_ready = ready.model_copy(update={"enabled": False})
    stored = [
        ThirdPartyCliSubagentConfig.model_validate(e)
        for e in _stored(config_path)
        if e.get("kind", "cli") == "cli" and e.get("name") == "Raven-Probe"
    ]
    merged = merge_product_seeds(stored, [not_ready])

    assert [(row.name, row.enabled) for row in merged] == [("Raven-Probe", False)]


async def test_a_fork_era_off_survives_the_folders_move_to_acp(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape every upgraded install's real "no" is in.

    A fork-era ``install.py`` wrote a complete cli row, so the toggle's off
    landed on that full definition, not on a stub. When the folder then declares
    acp, the merge calls the row stale -- and dropping it whole took the
    operator's off with it, turning the toggle into a silent no-op on exactly
    the installs that had one.
    """
    from raven.agent.subagent.vendored_agents import merge_product_seeds
    from raven.config.schema import ThirdPartyAcpSubagentConfig
    from raven.config.update_subagents import set_agents

    launcher = config_path.parent / "run.py"
    launcher.write_text("")
    set_agents(
        [
            *_stored(config_path),
            {
                "name": "Raven-Probe",
                "kind": "cli",
                "command": f"{sys.executable} {launcher} --prompt-file {{prompt_file}}",
                "description": "the complete row a fork-era install.py wrote",
                "enabled": True,
            },
        ],
        config_path=config_path,
    )

    assert await subagents_toggle({"name": "Raven-Probe", "enabled": False}) == {"enabled": False}

    # The upgrade moves the folder to acp; discovery now declares the new kind.
    v2 = ThirdPartyAcpSubagentConfig(name="Raven-Probe", command=f"{sys.executable} {launcher}", enabled=True)
    stored = [ThirdPartyCliSubagentConfig.model_validate(e) for e in _stored(config_path) if e["name"] == "Raven-Probe"]
    merged = merge_product_seeds(stored, [v2])

    assert [(r.name, r.kind, r.enabled) for r in merged] == [("Raven-Probe", "acp", False)]


async def test_enabling_a_local_agent_runs_a_ping_and_refuses_a_failure(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch answers what is true, the way it already does for a row with
    no launcher: a yes the roster would have to overrule is not written."""
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    # The fixture's Coder starts enabled; disable it first so a refusal that
    # left the flag untouched is distinguishable from one that never fired.
    await subagents_toggle({"name": "Coder", "enabled": False})

    async def _refuses(cfg):
        return PingResult(False, "Internal error: You need to sign in to use this model.")

    monkeypatch.setattr(subagents_mod, "ping_agent", _refuses)

    with pytest.raises(subagents_mod.SubagentNotReadyError) as caught:
        await subagents_toggle({"name": "Coder", "enabled": True})

    assert "sign in" in str(caught.value), "the refusal must carry the agent's own reason"
    entry = next(e for e in _stored(config_path) if e["name"] == "Coder")
    assert entry["enabled"] is not True, "a refused enable must not be written"


async def test_a_ping_refusal_carries_its_reason_in_data_detail(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatcher only synthesizes `data={"detail": ...}` when a handler
    passes no `data` of its own, so a handler that passes a custom `data`
    without a `detail` key drops the human-readable reason from the wire --
    the same trap `subagents_build`'s `SubagentNotFoundError` raise documents
    and avoids a few lines below this one. `str(caught.value)` is not enough
    to prove this: the message argument survives either way, only `data` is
    at risk."""
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    await subagents_toggle({"name": "Coder", "enabled": False})

    async def _refuses(cfg):
        return PingResult(False, "Internal error: You need to sign in to use this model.")

    monkeypatch.setattr(subagents_mod, "ping_agent", _refuses)

    with pytest.raises(subagents_mod.SubagentNotReadyError) as caught:
        await subagents_toggle({"name": "Coder", "enabled": True})

    assert caught.value.data is not None
    assert "sign in" in caught.value.data["detail"], "the UI reads data.detail, not just the message"


async def test_enabling_a_local_agent_succeeds_when_the_ping_answers(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    async def _answers(cfg):
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _answers)

    assert await subagents_toggle({"name": "Coder", "enabled": True}) == {"enabled": True}
    entry = next(e for e in _stored(config_path) if e["name"] == "Coder")
    assert entry["enabled"] is True


async def test_an_openai_agent_is_asked_to_answer_before_it_switches_on(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An endpoint is asked the same question every other kind is, by answering.

    It was exempt once, on the grounds that the free models probe had settled
    its credential; that probe runs on the listing and on an explicit test, and
    never on this path, so the key a switch is about to put to work may never
    have been tried. The fixture's Researcher row is `kind: "openai"`."""
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    seen = []

    async def _capture(cfg):
        seen.append(cfg)
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _capture)

    assert await subagents_toggle({"name": "Researcher", "enabled": True}) == {"enabled": True}
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["enabled"] is True
    assert [c.name for c in seen] == ["Researcher"], "switching an endpoint on asks it to answer, like every other kind"


async def test_force_switches_a_local_agent_on_despite_no_test(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A working agent whose provider is briefly down must not be un-switchable."""
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    seen = []

    async def _capture(cfg):
        seen.append(cfg)
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _capture)

    assert await subagents_toggle({"name": "Coder", "enabled": True, "force": True}) == {"enabled": True}
    entry = next(e for e in _stored(config_path) if e["name"] == "Coder")
    assert entry["enabled"] is True
    assert seen == [], "force must skip the ping entirely, not just tolerate its answer"


async def test_a_truthy_non_boolean_force_does_not_skip_the_gate(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`force` is declared a boolean and is the one documented way past the gate.
    `"no"` -- which a client could plausibly send -- is truthy in Python while
    reading as a refusal to whoever sent it, so only a real boolean opens it."""
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    await subagents_toggle({"name": "Coder", "enabled": False})
    seen = []

    async def _refuses(cfg):
        seen.append(cfg)
        return PingResult(False, "Internal error: You need to sign in to use this model.")

    monkeypatch.setattr(subagents_mod, "ping_agent", _refuses)

    with pytest.raises(subagents_mod.SubagentNotReadyError):
        await subagents_toggle({"name": "Coder", "enabled": True, "force": "no"})

    assert len(seen) == 1, "a non-boolean force must not skip the ping"
    entry = next(e for e in _stored(config_path) if e["name"] == "Coder")
    assert entry["enabled"] is not True


async def test_switching_a_local_agent_off_needs_no_test(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is on the yes. Off is always allowed, or a broken agent could
    not be switched off."""
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    seen = []

    async def _capture(cfg):
        seen.append(cfg)
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _capture)

    assert await subagents_toggle({"name": "Coder", "enabled": False}) == {"enabled": False}
    entry = next(e for e in _stored(config_path) if e["name"] == "Coder")
    assert entry["enabled"] is False
    assert seen == [], "switching off must never ping"


async def test_a_write_that_lands_during_the_ping_window_is_not_reverted(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's await sits between reading the agent list and writing it back.

    `set_agents` replaces the whole list, so writing the list that was read
    before a ping of up to a minute silently reverts every other
    `subagents.*` write that landed during it -- and the clobbered call has
    already answered success to its own client. The server dispatches each
    frame in its own task, so a second write really does run inside that window.
    """
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    await subagents_toggle({"name": "Coder", "enabled": False})
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def _hangs_until_released(cfg):
        in_flight.set()
        await release.wait()
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _hangs_until_released)

    pinged = asyncio.ensure_future(subagents_toggle({"name": "Coder", "enabled": True}))
    await in_flight.wait()
    # Another client, on the ordinary path. It has to be a write that cannot
    # reach the gate itself, or it would block on the very stub this test is
    # holding open: a description changes nothing the agent could answer, so it
    # is never asked. A switch-on would be, now that every kind is.
    assert await subagents_update({"name": "Researcher", "description": "mid-flight"}) == {
        "updated": True,
        "name": "Researcher",
    }
    mid = {e["name"]: e.get("description") for e in _stored(config_path)}
    assert mid["Researcher"] == "mid-flight", "the concurrent write must reach disk"

    release.set()
    assert await pinged == {"enabled": True}

    after = {e["name"]: e for e in _stored(config_path)}
    assert after["Coder"].get("enabled") is True, "the pinged toggle's own write must survive"
    assert after["Researcher"].get("description") == "mid-flight", (
        "the write made during the ping must survive the toggle's write"
    )


async def test_a_write_that_lands_during_an_updates_ping_is_not_reverted(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-key gate opens the window the switch and the add both close.

    `subagents.update` reads the agent list, mutates one row inside it, and
    writes the whole list back. A changed key on a live row now sends a prompt
    in between, for up to a minute, which puts this handler in exactly the
    position the other two re-read to escape: the list it writes is the list it
    read, so every other `subagents.*` write that landed during the ping is
    reverted, after that call already answered success to its own client.
    """
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    _gate_answers(monkeypatch)
    assert await subagents_toggle({"name": "Researcher", "enabled": True}) == {"enabled": True}

    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def _hangs_until_released(cfg):
        in_flight.set()
        await release.wait()
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _hangs_until_released)

    pinged = asyncio.ensure_future(subagents_update({"name": "Researcher", "api_key": "sk-rotated"}))
    await in_flight.wait()
    # A description carries nothing the agent could answer, so this write is
    # never gated -- which is what lets it run while the gate is held open.
    assert await subagents_update({"name": "Coder", "description": "mid-flight"}) == {
        "updated": True,
        "name": "Coder",
    }
    mid = {e["name"]: e.get("description") for e in _stored(config_path)}
    assert mid["Coder"] == "mid-flight", "the concurrent write must reach disk"

    release.set()
    assert await pinged == {"updated": True, "name": "Researcher"}

    after = {e["name"]: e for e in _stored(config_path)}
    assert after["Researcher"].get("apiKey") == "sk-rotated", "the pinged update's own write must survive"
    assert after["Coder"].get("description") == "mid-flight", (
        "the write made during the ping must survive the update's write"
    )


async def test_a_write_to_the_same_row_during_an_updates_ping_is_not_reverted(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-reading the list is not enough: the row itself has to be re-read too.

    Carrying this call's own row across the ping is what keeps its change,
    and carrying it *whole* is what loses everybody else's -- the copy was
    taken before the await, so every field another call wrote to this row in
    between is restored to what it was. The neighbouring test cannot see it:
    it edits a different row, which is the half a fresh list read already
    fixes.
    """
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    _gate_answers(monkeypatch)
    assert await subagents_toggle({"name": "Researcher", "enabled": True}) == {"enabled": True}

    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def _hangs_until_released(cfg):
        in_flight.set()
        await release.wait()
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _hangs_until_released)

    pinged = asyncio.ensure_future(subagents_update({"name": "Researcher", "api_key": "sk-rotated"}))
    await in_flight.wait()
    assert await subagents_update({"name": "Researcher", "description": "mid-flight"}) == {
        "updated": True,
        "name": "Researcher",
    }

    release.set()
    assert await pinged == {"updated": True, "name": "Researcher"}

    after = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert after.get("apiKey") == "sk-rotated", "the pinged update's own field must survive"
    assert after.get("description") == "mid-flight", "a field written to this same row during the ping must survive too"


async def test_an_update_refuses_a_row_removed_while_it_was_being_proved(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row deleted during the ping has nothing left to merge onto.

    Re-reading the list is what makes this reachable: the row this call read
    is no longer in it. Appending the pre-await copy would resurrect an entry
    somebody removed, and answering success would say a change landed on a row
    that is gone -- so the call refuses instead, under the error its published
    contract already declares.
    """
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    _gate_answers(monkeypatch)
    assert await subagents_toggle({"name": "Researcher", "enabled": True}) == {"enabled": True}

    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def _hangs_until_released(cfg):
        in_flight.set()
        await release.wait()
        return PingResult(True, "it ran and replied")

    monkeypatch.setattr(subagents_mod, "ping_agent", _hangs_until_released)

    pinged = asyncio.ensure_future(subagents_update({"name": "Researcher", "api_key": "sk-rotated"}))
    await in_flight.wait()
    assert await subagents_remove({"name": "Researcher"}) == {"removed": True}

    release.set()
    with pytest.raises(SubagentNotFoundError, match="renamed or removed"):
        await pinged
    assert all(e["name"] != "Researcher" for e in _stored(config_path)), "the removal must stand"


async def test_toggle_still_refuses_a_name_nothing_knows(config_path: Path) -> None:
    """The materializing branch must not turn an unknown name into a success."""
    with pytest.raises(SubagentNotFoundError):
        await subagents_toggle({"name": "Nobody", "enabled": True})


async def test_remove_deletes_the_entry(config_path: Path) -> None:
    assert await subagents_remove({"name": "Coder"}) == {"removed": True}
    assert all(e["name"] != "Coder" for e in _stored(config_path))


async def test_remove_reports_false_for_an_unknown_name(config_path: Path) -> None:
    assert await subagents_remove({"name": "nope"}) == {"removed": False}


async def test_a_mutation_hot_applies_to_the_live_loop(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _gate_answers(monkeypatch)
    applied: list[list] = []

    class _Loop:
        def apply_agents(self, configs: list) -> None:
            applied.append(configs)

    await subagents_toggle({"name": "Researcher", "enabled": True}, agent_loop_factory=lambda: _Loop())
    assert len(applied) == 1
    assert [c.name for c in applied[0]] == ["Coder", "Researcher"]


async def test_a_mutation_without_a_live_loop_still_writes(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _gate_answers(monkeypatch)
    # The demo runner has no loop; a missing loop is not an error.
    await subagents_toggle({"name": "Researcher", "enabled": True}, agent_loop_factory=lambda: None)
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["enabled"] is True


async def test_a_write_re_reads_the_file_first(config_path: Path) -> None:
    # The gateway may have written between the overlay's list call and this
    # mutation; the mutation must not resurrect the stale list it was rendered
    # from. Simulate a concurrent add, then toggle an unrelated agent.
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append({"name": "Sneaky", "kind": "cli", "command": "cat", "enabled": True})
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    await subagents_toggle({"name": "Coder", "enabled": False})
    names = [e["name"] for e in _stored(config_path)]
    assert "Sneaky" in names, "a concurrent write was clobbered"


async def test_test_records_a_verdict_for_a_configured_agent(config_path: Path, monkeypatch) -> None:
    from raven.agent.subagent.probe import TestResult

    async def fake_run_test(cfg, *, source):
        return TestResult(cfg.name, source, "cli", True, "the agent ran and replied", "PONG", 42)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)
    out = await subagents_test({"name": "Coder", "source": "config"})
    assert out["ok"] is True
    assert out["reply"] == "PONG"
    assert out["elapsed_ms"] == 42
    assert out["cancelled"] is False

    # The verdict is persisted, so it survives closing the overlay.
    rows = (await subagents_list({}))["rows"]
    coder = next(r for r in rows if r["name"] == "Coder")
    assert coder["last_test_ok"] is True


async def test_a_failed_test_s_fix_reaches_the_row(config_path: Path, monkeypatch) -> None:
    """The row carries the fix the last failed test named, and nothing when it named none."""
    from raven.agent.subagent.probe import TestResult
    from raven.agent.subagent.probe_state import Remedy

    verdicts = iter(
        [
            TestResult(
                "Coder", "config", "acp", False, "no usable credential", None, 5, Remedy("setup", "hermes model")
            ),
            TestResult("Coder", "config", "acp", False, "exited 1", None, 5),
        ]
    )

    async def fake_run_test(cfg, *, source):
        return next(verdicts)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)

    await subagents_test({"name": "Coder", "source": "config"})
    coder = next(r for r in (await subagents_list({}))["rows"] if r["name"] == "Coder")
    assert coder["last_test_remedy"] == {"kind": "setup", "command": "hermes model"}

    await subagents_test({"name": "Coder", "source": "config"})
    coder = next(r for r in (await subagents_list({}))["rows"] if r["name"] == "Coder")
    assert coder.get("last_test_remedy") is None, "a failure with no fix leaves none behind"


async def test_test_rejects_an_unknown_name(config_path: Path) -> None:
    with pytest.raises(SubagentNotFoundError):
        await subagents_test({"name": "nope", "source": "config"})


async def test_test_on_a_healthy_agent_does_not_leak_another_entrys_api_key(config_path: Path) -> None:
    # The config is shared with other clients (web UI, hand edits): `_find`
    # re-validates the *whole* on-disk section, so one malformed entry written by
    # anyone must not turn a `t` on a completely unrelated, healthy row into a key
    # disclosure via pydantic's `input_value=...` diagnostic.
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append({"name": "Bad", "kind": "openai", "apiKey": "sk-CANARY-C"})
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigValidationError) as excinfo:
        await subagents_test({"name": "Coder", "source": "config"})

    exc = excinfo.value
    blob = f"{exc.message} {exc.detail} {json.dumps(exc.data or {})}"
    assert "sk-CANARY-C" not in blob
    assert "input_value" not in blob


async def test_test_can_target_an_unconfigured_preset(config_path: Path, monkeypatch) -> None:
    from raven.agent.subagent.probe import TestResult

    async def fake_run_test(cfg, *, source):
        return TestResult(cfg.name, source, "cli", True, "ok", "PONG", 1)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)
    out = await subagents_test({"name": "OpenCode", "source": "preset"})
    assert out["ok"] is True


async def test_an_acp_test_recomposes_the_live_agent_table(config_path: Path, monkeypatch) -> None:
    # An acp test is a measurement, and `_test_acp` records the snapshot the
    # roster reads `stateful` from. Without re-composing, the live table keeps the
    # measurement it was built with, and an agent that just proved it can resume
    # is still refused by `create_instance`.
    from raven.agent.subagent.probe import TestResult

    applied: list[list] = []

    class _Loop:
        def apply_agents(self, configs: list) -> None:
            applied.append(configs)

    async def fake_run_test(cfg, *, source):
        return TestResult(cfg.name, source, "acp", True, "connected", None, 5)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)
    out = await subagents_test({"name": "Coder", "source": "config"}, agent_loop_factory=lambda: _Loop())
    assert out["ok"] is True
    assert len(applied) == 1
    assert "Coder" in [c.name for c in applied[0]]


async def test_the_dispatcher_reaches_the_wrapper_that_carries_the_loop(config_path: Path, monkeypatch) -> None:
    # The re-compose lives in the registration wrapper, not in `subagents_test`,
    # which takes the loop as a keyword the tests above hand it directly. So
    # registering the bare function instead leaves every real caller on
    # `agent_loop_factory=None`, `_hot_apply` returns on its first line, and the
    # stale-table bug is back in full with the rest of this file still green.
    # Dispatching through the registered name is what pins the wiring;
    # `test_rpc_registration.py` asserts only that the name is registered.
    from raven.agent.subagent.probe import TestResult

    applied: list[list] = []

    class _Loop:
        def apply_agents(self, configs: list) -> None:
            applied.append(configs)

    async def fake_run_test(cfg, *, source):
        return TestResult(cfg.name, source, "acp", True, "connected", None, 5)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)
    dispatcher = Dispatcher()
    register_subagents_methods(dispatcher, agent_loop_factory=lambda: _Loop())

    frame = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "subagents.test",
            "params": {"name": "Coder", "source": "config"},
        }
    )
    assert "error" not in frame, frame
    assert frame["result"]["ok"] is True
    assert len(applied) == 1
    assert "Coder" in [c.name for c in applied[0]]


async def test_a_cli_test_leaves_the_agent_table_alone(config_path: Path, monkeypatch) -> None:
    # A cli test writes no capability snapshot, so there is nothing new for the
    # table to pick up, and the door re-reads config for nothing.
    from raven.agent.subagent.probe import TestResult

    applied: list[list] = []

    class _Loop:
        def apply_agents(self, configs: list) -> None:
            applied.append(configs)

    async def fake_run_test(cfg, *, source):
        return TestResult(cfg.name, source, "cli", True, "ok", "PONG", 5)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)
    await subagents_test({"name": "Coder", "source": "config"}, agent_loop_factory=lambda: _Loop())
    assert applied == []


async def test_testing_a_preset_leaves_the_agent_table_alone(config_path: Path, monkeypatch) -> None:
    # A Test writes a capability snapshot, never a roster entry -- for a preset as
    # much as for a configured row -- so there is nothing here for the agent table
    # to be rebuilt from. (It did once hold for the reason that `_test_acp` skipped
    # recording a preset's snapshot entirely; it no longer skips it, and the
    # assertion below is unmoved, because recording was never what applied.)
    from raven.agent.subagent.probe import TestResult

    applied: list[list] = []

    class _Loop:
        def apply_agents(self, configs: list) -> None:
            applied.append(configs)

    async def fake_run_test(cfg, *, source):
        return TestResult(cfg.name, source, "acp", True, "connected", None, 5)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)
    await subagents_test({"name": "OpenCode", "source": "preset"}, agent_loop_factory=lambda: _Loop())
    assert applied == []


async def test_a_failing_recompose_does_not_mask_the_verdict(config_path: Path, monkeypatch) -> None:
    # The snapshot is already on disk by the time the door runs, so the verdict
    # the caller asked for is real whatever the table does with it. Raising here
    # would report a successful measurement as a failed test.
    from raven.agent.subagent.probe import TestResult

    class _Loop:
        def apply_agents(self, configs: list) -> None:
            raise RuntimeError("table rebuild failed")

    async def fake_run_test(cfg, *, source):
        return TestResult(cfg.name, source, "acp", True, "connected", None, 5)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)
    out = await subagents_test({"name": "Coder", "source": "config"}, agent_loop_factory=lambda: _Loop())
    assert out["ok"] is True
    assert out["detail"] == "connected"


async def test_an_acp_test_without_a_live_loop_still_reports(config_path: Path, monkeypatch) -> None:
    # The demo runner has no loop, and a test is a read: a missing loop must not
    # turn a completed measurement into an error.
    from raven.agent.subagent.probe import TestResult

    async def fake_run_test(cfg, *, source):
        return TestResult(cfg.name, source, "acp", True, "connected", None, 5)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)
    assert (await subagents_test({"name": "Coder", "source": "config"}))["ok"] is True
    out = await subagents_test({"name": "Coder", "source": "config"}, agent_loop_factory=lambda: None)
    assert out["ok"] is True


async def test_cancel_stops_a_running_test(config_path: Path, monkeypatch) -> None:
    started = asyncio.Event()

    async def slow_run_test(cfg, *, source):
        started.set()
        await asyncio.sleep(60)
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", slow_run_test)
    task = asyncio.create_task(subagents_test({"name": "Coder", "source": "config"}))
    await asyncio.wait_for(started.wait(), timeout=5)

    assert await subagents_test_cancel({"name": "Coder"}) == {"cancelled": True}
    out = await asyncio.wait_for(task, timeout=5)
    assert out["cancelled"] is True
    assert out["ok"] is False


async def test_cancel_reports_false_when_nothing_is_running(config_path: Path) -> None:
    assert await subagents_test_cancel({"name": "Coder"}) == {"cancelled": False}


async def test_list_reports_test_running_while_a_test_is_in_flight(config_path: Path, monkeypatch) -> None:
    from raven.agent.subagent.probe import TestResult

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run_test(cfg, *, source):
        started.set()
        await release.wait()
        return TestResult(cfg.name, source, "cli", True, "ok", "PONG", 1)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", slow_run_test)
    task = asyncio.create_task(subagents_test({"name": "Coder", "source": "config"}))
    await asyncio.wait_for(started.wait(), timeout=5)

    rows = (await subagents_list({}))["rows"]
    coder = next(r for r in rows if r["name"] == "Coder")
    researcher = next(r for r in rows if r["name"] == "Researcher")
    assert coder["test_running"] is True
    assert researcher["test_running"] is False

    release.set()
    await asyncio.wait_for(task, timeout=5)

    rows_after = (await subagents_list({}))["rows"]
    coder_after = next(r for r in rows_after if r["name"] == "Coder")
    assert coder_after["test_running"] is False


async def test_a_finished_test_is_not_left_in_the_running_map(config_path: Path, monkeypatch) -> None:
    from raven.agent.subagent.probe import TestResult
    from raven.rpc.methods.subagents import _RUNNING

    async def fake_run_test(cfg, *, source):
        return TestResult(cfg.name, source, "cli", True, "ok", "PONG", 1)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)
    await subagents_test({"name": "Coder", "source": "config"})
    assert "Coder" not in _RUNNING


async def test_a_second_concurrent_test_for_the_same_name_is_refused(config_path: Path, monkeypatch) -> None:
    from raven.agent.subagent.probe import TestResult

    calls = 0
    started = asyncio.Event()

    async def counting_run_test(cfg, *, source):
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(60)
        return TestResult(cfg.name, source, "cli", True, "ok", "PONG", 1)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", counting_run_test)
    first = asyncio.create_task(subagents_test({"name": "Coder", "source": "config"}))
    await asyncio.wait_for(started.wait(), timeout=5)

    second = await subagents_test({"name": "Coder", "source": "config"})
    assert second["ok"] is False
    assert second["cancelled"] is False
    assert "already running" in second["detail"]
    assert calls == 1, "the refused call must not dispatch a second real test"

    # Clean up the still-running first call through the real cancel path.
    assert await subagents_test_cancel({"name": "Coder"}) == {"cancelled": True}
    out = await asyncio.wait_for(first, timeout=5)
    assert out["cancelled"] is True


async def test_cancel_still_reaches_the_first_call_while_a_second_is_refused(config_path: Path, monkeypatch) -> None:
    from raven.agent.subagent.probe import TestResult
    from raven.rpc.methods.subagents import _RUNNING

    started = asyncio.Event()

    async def slow_run_test(cfg, *, source):
        started.set()
        await asyncio.sleep(60)
        return TestResult(cfg.name, source, "cli", True, "ok", "PONG", 1)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", slow_run_test)
    first = asyncio.create_task(subagents_test({"name": "Coder", "source": "config"}))
    await asyncio.wait_for(started.wait(), timeout=5)

    refusal = await subagents_test({"name": "Coder", "source": "config"})
    assert refusal["ok"] is False

    assert await subagents_test_cancel({"name": "Coder"}) == {"cancelled": True}
    out = await asyncio.wait_for(first, timeout=5)
    assert out["cancelled"] is True

    assert _RUNNING == {}


async def test_a_builtin_row_cannot_be_switched_off(config_path: Path) -> None:
    """The switch is not the caller's to throw, so the RPC refuses rather than writes.

    An unnamed ``spawn`` and a DAG node with no ``subagent`` both normalize to the
    generic built-in row, so taking it off the roster leaves the default pointing
    at nothing. Refused rather than silently ignored: a caller that asked for a
    state change is owed the reason it did not happen.
    """
    with pytest.raises(ConfigFieldReadonlyError):
        await subagents_toggle({"name": "Raven", "enabled": False})

    # And the name it used to be written under is refused too: a guard that missed
    # it would write a config row under the old name, which lands on the table as a
    # second agent rather than as the override it was taken for.
    with pytest.raises(ConfigFieldReadonlyError):
        await subagents_toggle({"name": "raven", "enabled": False})

    # Nothing was written on the way out either time: the override row that used to
    # be created to carry the switch has no other reason to exist.
    assert [e for e in _stored(config_path) if e["name"] in ("Raven", "raven")] == []

    rows = {r["name"]: r for r in (await subagents_list({"probe": False}))["rows"]}
    assert rows["Raven"]["enabled"] is True
    assert rows["Raven"]["group"] == "builtin"
    # The description still comes from the package's row, which is the only line
    # the model reads about this agent.
    assert "in-process sub-agent" in rows["Raven"]["description"]


async def test_a_switch_off_hand_written_into_config_does_not_take_it_off_the_roster(
    config_path: Path,
) -> None:
    """No UI guard can reach a hand-edited config file, so the merge has to hold.

    Written under the name the row used to carry, which is what a config edited
    before the rename holds: it is still the seed's override, so it must neither
    switch the seed off nor appear beside it as a second agent.
    """
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append({"name": "raven", "kind": "builtin", "enabled": False})
    config_path.write_text(json.dumps(raw))

    rows = {r["name"]: r for r in (await subagents_list({"probe": False}))["rows"]}
    assert rows["Raven"]["enabled"] is True
    assert "raven" not in rows


async def test_a_builtin_name_cannot_be_added_as_another_transport(config_path: Path) -> None:
    """The write primitive refuses it, so every RPC that writes inherits the guard."""
    from raven.rpc.errors import ConfigValidationError

    with pytest.raises(ConfigValidationError):
        await subagents_update({"name": "Coder", "new_name": "raven"})


# --------------------------------------------------------------- product readiness on the page


def _product_tree(tmp_path: Path, *, launcher: bool = True, engine: dict | None = None, kind: str = "cli") -> Path:
    """An `agents/` tree with one product folder.

    The manifest command names the interpreter and the folder's `run.py`, the
    same two absolute paths every shipped product command carries -- which is
    what the launcher probe reads.
    """
    root = tmp_path / "agents"
    folder = root / "raven-probe"
    folder.mkdir(parents=True)
    manifest = {
        "name": "Raven-Probe",
        "kind": kind,
        "description": "d",
        # An acp folder is served, not spawned per task, so it takes no prompt
        # placeholder -- the shape the five shipped products ship with.
        "command": "{PYTHON} {SUBAGENT_DIR}/run.py" + (" {prompt}" if kind == "cli" else ""),
    }
    if engine is not None:
        manifest["engine"] = engine
    (folder / "subagent.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "config.json").write_text("{}", encoding="utf-8")
    if launcher:
        (folder / "run.py").write_text("", encoding="utf-8")
    return root


async def test_a_ready_product_is_enabled_and_never_building(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is buildable in the product tree, so the flag the page watches
    during a fork-era venv build is now constant."""
    from raven.agent.subagent import vendored_agents as va

    root = _product_tree(tmp_path)
    monkeypatch.setattr(va, "agents_root", lambda: root)

    row = [r for r in (await subagents_list({"probe": False}))["rows"] if r["name"] == "Raven-Probe"][0]

    assert row["enabled"] is True
    assert row["vendored"] is True
    assert row["building"] is False


async def test_a_missing_launcher_reports_attention_with_the_reason(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe checks the command's first token, which is the interpreter and
    always exists -- so without the readiness override the page would call a
    broken folder ready while the roster refused to advertise it. `attention`,
    never `missing`: `missing` is what the page reads as "offer Install", and
    no readiness reason here is installable from this server.
    """
    from raven.agent.subagent import vendored_agents as va

    root = _product_tree(tmp_path, launcher=False)
    monkeypatch.setattr(va, "agents_root", lambda: root)

    row = [r for r in (await subagents_list({"probe": False}))["rows"] if r["name"] == "Raven-Probe"][0]

    assert row["enabled"] is False
    assert row["probe_status"] == "attention"
    assert "run.py" in row["probe_detail"]


async def test_a_missing_engine_reports_attention_and_names_the_wheel(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one action that fixes this state is installing a wheel, which only
    the reader can do -- so the wheel's name has to reach them on the row."""
    from raven.agent.subagent import vendored_agents as va

    engine = {"package": "raven_probe_engine_that_is_not_installed", "wheel": "probe-engine"}
    root = _product_tree(tmp_path, engine=engine)
    monkeypatch.setattr(va, "agents_root", lambda: root)

    row = [r for r in (await subagents_list({"probe": False}))["rows"] if r["name"] == "Raven-Probe"][0]

    assert row["enabled"] is False
    assert row["probe_status"] == "attention"
    assert "probe-engine" in row["probe_detail"]


async def test_a_probe_miss_on_a_ready_discovered_row_reads_as_attention_not_missing(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness and the probe read different facts -- absolute-path existence
    vs `which` on the first token -- so a relative `SUBAGENT_PYTHON` override
    can be ready by one rule and missing by the other. `missing` is the one
    status the page renders an Install button for, and for a discovered row
    that click can do nothing; the row says `attention` instead and keeps the
    probe's own detail for the reader."""
    from raven.agent.subagent import vendored_agents as va
    from raven.agent.subagent.probe import ProbeResult

    root = _product_tree(tmp_path)
    monkeypatch.setattr(va, "agents_root", lambda: root)

    async def all_missing(entries, *, verdicts=None):
        return [
            ProbeResult(cfg.name, source, cfg.kind, "missing", f"{cfg.name}: not on the login shell PATH", "", 0)
            for cfg, source in entries
        ]

    monkeypatch.setattr("raven.rpc.methods.subagents.probe_all", all_missing)

    rows = (await subagents_list({"probe": True}))["rows"]
    row = [r for r in rows if r["name"] == "Raven-Probe"][0]

    assert row["vendored"] is True and row["enabled"] is True
    assert row["probe_status"] == "attention"
    assert "not on the login shell PATH" in row["probe_detail"]


async def test_build_answers_nothing_to_build_for_a_ready_product(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The method stays on the wire for old clients; what it reports is the
    truth of the product tree: launchers ship with the wheel."""
    from raven.agent.subagent import vendored_agents as va
    from raven.rpc.methods.subagents import subagents_build

    root = _product_tree(tmp_path)
    monkeypatch.setattr(va, "agents_root", lambda: root)

    result = await subagents_build({"name": "Raven-Probe"})

    assert result["building"] is False
    assert "nothing to build" in result["detail"]


async def test_build_hands_back_the_readiness_reason_for_an_unready_product(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that still offers Install learns why the button cannot help:
    the reason names the wheel, and installing that is not this server's job."""
    from raven.agent.subagent import vendored_agents as va
    from raven.rpc.methods.subagents import subagents_build

    engine = {"package": "raven_probe_engine_that_is_not_installed", "wheel": "probe-engine"}
    root = _product_tree(tmp_path, engine=engine)
    monkeypatch.setattr(va, "agents_root", lambda: root)

    result = await subagents_build({"name": "Raven-Probe"})

    assert result["building"] is False
    assert "probe-engine" in result["detail"]


async def test_building_a_name_no_folder_carries_is_refused(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason has to travel, not just be raised: the dispatcher fills
    `data` from a handler's `detail` only when the handler passed no `data` of
    its own, so the name is repeated inside `data` deliberately."""
    from raven.agent.subagent import vendored_agents as va
    from raven.rpc.methods.subagents import subagents_build

    root = _product_tree(tmp_path)
    monkeypatch.setattr(va, "agents_root", lambda: root)

    with pytest.raises(SubagentNotFoundError) as caught:
        await subagents_build({"name": "claude_code"})

    assert caught.value.data is not None
    assert caught.value.data["name"] == "claude_code"
    assert "claude_code" in caught.value.data["detail"]


async def test_a_reserved_name_entry_is_reported_once_and_as_ignored(config_path: Path) -> None:
    """The roster is where a user learns their entry is dead, not the server log.

    The capitalised spelling was accepted before the generic row was renamed to it,
    so config can hold a cli entry of that name. It is inert at runtime, and the
    list used to carry it beside the built-in row as a second enabled-looking agent
    of the same name -- a view that keys by name opens whichever comes first.
    """
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append(
        {"name": "Raven", "kind": "cli", "enabled": True, "command": "x {prompt}", "description": "legacy"}
    )
    config_path.write_text(json.dumps(raw))

    rows = [r for r in (await subagents_list({"probe": False}))["rows"] if r["name"] == "Raven"]

    assert len(rows) == 1
    assert rows[0]["kind"] == "builtin"
    assert rows[0]["enabled"] is True


async def test_a_row_carries_the_credential_verdict_its_snapshot_measured(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agents page draws `Unauthorized` off this and nothing else.

    It cannot be derived client-side: what survives on the wire otherwise is
    ``probe_status``, which is ``attention`` for "wants signing in" and equally
    ``attention`` for "installed and never verified" -- two rows that need
    opposite things from the reader. The snapshot measured the difference; this
    is the field that carries it out.
    """
    from raven.acp_client.capabilities import CapabilitySnapshot
    from raven.rpc.methods import subagents as mod

    def snapshot_for(cfg: object) -> CapabilitySnapshot | None:
        if getattr(cfg, "name", "") != "Codex":
            return None
        return CapabilitySnapshot(
            agent="Codex", fingerprint="f", status="attention", detail="no session", measured_at_ms=0, needs_auth=True
        )

    monkeypatch.setattr(mod, "acp_snapshot_for", snapshot_for, raising=False)
    monkeypatch.setattr("raven.agent.subagent.probe.acp_snapshot_for", snapshot_for)

    rows = {row["name"]: row for row in (await subagents_list({"probe": False}))["rows"]}
    assert rows["Codex"]["needs_auth"] is True
    # Every other row says so too, rather than leaving the key out: a missing key
    # and a false one read the same to a client, right up until one of them means
    # "this server is too old to know". A cli row and an openai row have no
    # handshake to be refused in, so both answer false rather than nothing.
    assert rows["Coder"]["needs_auth"] is False
    assert rows["Researcher"]["needs_auth"] is False
    assert all("needs_auth" in row for row in rows.values())


# ---- own / model_source -----------------------------------------------------


async def test_list_marks_the_built_in_row_and_a_discovered_product_as_ravens_own(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import vendored_agents as va

    root = _product_tree(tmp_path)
    monkeypatch.setattr(va, "agents_root", lambda: root)
    rows = {r["name"]: r for r in (await subagents_list({"probe": False}))["rows"]}

    assert rows["Raven"]["own"] is True and rows["Raven"]["model_source"] == "raven"
    # Ownership and the model rule are two facts: a discovered product is
    # raven's, and as a cli row it has no menu -- `update` refuses a model on it.
    assert rows["Raven-Probe"]["own"] is True and rows["Raven-Probe"]["model_source"] == "fixed"
    assert rows["Coder"]["own"] is False and rows["Coder"]["model_source"] == "fixed"
    assert rows["Researcher"]["own"] is False and rows["Researcher"]["model_source"] == "fixed"


async def test_list_takes_a_product_on_its_own_key_off_the_hosts_catalogue(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder carrying its own `<PREFIX>_API_KEY` is what each launcher
    branches on: it takes that credential with the provider and model beside it
    and never calls `inherit_llm`. So the row does not follow the main Raven,
    its model is not one this host can name, and the page must not offer raven's
    ids for it -- which is what the `fixed` rule says, the same answer an openai
    row gets for the same reason.

    The same folder either way, so the key is the only thing that moved: without
    it the row reads its menu off raven's own catalogue, the way every shipped
    product does today.
    """
    from raven.acp_client.capabilities import CapabilitySnapshot, SnapshotStore, snapshot_fingerprint
    from raven.agent.subagent import vendored_agents as va

    root = _product_tree(tmp_path, kind="acp")
    monkeypatch.setattr(va, "agents_root", lambda: root)
    monkeypatch.setattr("raven.acp_client.capabilities.default_snapshot_path", lambda: tmp_path / "caps.json")
    monkeypatch.delenv("PROBE_API_KEY", raising=False)
    cfg = next(c for c in va.discover_product_rows(root) if c.name == "Raven-Probe")
    SnapshotStore(path=tmp_path / "caps.json").record(
        CapabilitySnapshot(
            agent="Raven-Probe",
            fingerprint=snapshot_fingerprint(cfg),
            status="ready",
            detail="",
            measured_at_ms=1,
            agent_name="raven",
        )
    )

    rows = {r["name"]: r for r in (await subagents_list({"probe": False}))["rows"]}
    assert rows["Raven-Probe"]["own"] is True and rows["Raven-Probe"]["model_source"] == "raven"

    (root / "raven-probe" / ".env").write_text("PROBE_API_KEY=sk-its-own\n", encoding="utf-8")

    rows = {r["name"]: r for r in (await subagents_list({"probe": False}))["rows"]}
    assert rows["Raven-Probe"]["own"] is True and rows["Raven-Probe"]["model_source"] == "fixed"


async def test_update_refuses_a_model_on_a_product_that_runs_on_its_own_key(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is the point: the pick would be pushed at a session whose own
    config was rendered from the folder's credential and has never heard of this
    host's ids."""
    from raven.agent.subagent import vendored_agents as va

    root = _product_tree(tmp_path, kind="acp")
    (root / "raven-probe" / ".env").write_text("PROBE_API_KEY=sk-its-own\n", encoding="utf-8")
    monkeypatch.setattr(va, "agents_root", lambda: root)
    monkeypatch.delenv("PROBE_API_KEY", raising=False)

    with pytest.raises(ConfigFieldReadonlyError, match="no menu this call can pick from"):
        await subagents_update({"name": "Raven-Probe", "model": "gpt-5"})


async def test_list_marks_a_config_row_whose_handshake_named_raven_as_ravens_own(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped installer registers a product as a plain config row, with
    neither flag; the handshake it recorded is what still says it is raven's."""
    from raven.acp_client.capabilities import AcpModelChoice, CapabilitySnapshot, SnapshotStore, snapshot_fingerprint
    from raven.config.schema import SubagentsConfig

    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"] += [
        {"name": "Raven-Code", "kind": "acp", "command": "raven acp", "description": "d", "enabled": True},
        {"name": "Raven-PPT", "kind": "acp", "command": "raven-ppt acp", "description": "d", "enabled": True},
        {"name": "Other", "kind": "acp", "command": "other acp", "description": "d", "enabled": True},
        {"name": "Other-Quiet", "kind": "acp", "command": "quiet acp", "description": "d", "enabled": True},
    ]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    store_path = tmp_path / "caps.json"
    monkeypatch.setattr("raven.acp_client.capabilities.default_snapshot_path", lambda: store_path)
    cfgs = {c.name: c for c in SubagentsConfig(agents=raw["subagents"]["agents"]).agents}
    menu = (AcpModelChoice(value="v/m", name="M", group="V"),)
    for name, agent_name, choices in (
        ("Raven-Code", "raven", menu),
        ("Raven-PPT", "raven", ()),
        ("Other", "other-agent", menu),
        ("Other-Quiet", "other-agent", ()),
    ):
        SnapshotStore(path=store_path).record(
            CapabilitySnapshot(
                agent=name,
                fingerprint=snapshot_fingerprint(cfgs[name]),
                status="ready",
                detail="",
                measured_at_ms=1,
                agent_name=agent_name,
                model_choices=choices,
            )
        )

    rows = {r["name"]: r for r in (await subagents_list({"probe": False}))["rows"]}

    # Raven's own, and still an acp row: it picks from raven's live catalogue,
    # not from the launch-time capture its handshake advertised -- the menu the
    # composer draws, so the two never disagree. The capture still travels, for
    # a reader that wants to know what the probe saw.
    assert rows["Raven-Code"]["own"] is True and rows["Raven-Code"]["model_source"] == "raven"
    assert rows["Raven-Code"]["model_choices"] == [{"value": "v/m", "name": "M", "group": "V"}]
    assert rows["Other"]["own"] is False and rows["Other"]["model_source"] == "agent"
    assert rows["Other"]["model_choices"] == [{"value": "v/m", "name": "M", "group": "V"}]
    # The same agent with nothing to advertise: raven's own is on the same
    # catalogue either way, a third party is taken at its word.
    assert rows["Raven-PPT"]["own"] is True and rows["Raven-PPT"]["model_source"] == "raven"
    assert rows["Raven-PPT"]["model_choices"] == []
    assert rows["Other-Quiet"]["own"] is False and rows["Other-Quiet"]["model_source"] == "agent"


async def test_test_can_target_a_discovered_product(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A product row has no config entry and is not a preset either: its folder
    is the pool it is found in, and the verdict is recorded under that source so
    the roster reads it back on the same row."""
    from raven.agent.subagent import vendored_agents as va
    from raven.agent.subagent.probe import TestResult
    from raven.rpc.errors import SubagentNotFoundError

    root = _product_tree(tmp_path)
    monkeypatch.setattr(va, "agents_root", lambda: root)
    seen: list[tuple[str, str]] = []

    async def fake_run_test(cfg, *, source):
        seen.append((cfg.name, source))
        return TestResult(cfg.name, source, "cli", True, "ok", "PONG", 1)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)

    out = await subagents_test({"name": "Raven-Probe", "source": "vendored"})

    assert out["ok"] is True
    assert seen == [("Raven-Probe", "vendored")]
    row = next(r for r in (await subagents_list({"probe": False}))["rows"] if r["name"] == "Raven-Probe")
    assert row["last_test_ok"] is True
    with pytest.raises(SubagentNotFoundError):
        await subagents_test({"name": "Coder", "source": "vendored"})
