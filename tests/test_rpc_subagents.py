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
    assert by_name["opencode"]["configured"] is False


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
    # preset never gets one: `_test_acp` records only for `source == "config"`.
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
    assert by_name["hermes"]["probe_status"] == "attention"
    assert by_name["hermes"]["group"] == "installed"
    # Not an unconditional "installed": this one's executable really is absent,
    # which is the only thing NOT INSTALLED is meant to say.
    assert by_name["openclaw"]["probe_status"] == "missing"
    assert by_name["openclaw"]["group"] == "uninstalled"


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
        "codex",
        "openclaw",
        "opencode",
        "hermes",
    } | set(ACP_REGISTRY_PRESETS)
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


async def test_add_writes_the_preset_template_under_a_chosen_name(config_path: Path) -> None:
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


async def test_add_defaults_name_and_description_to_the_preset(config_path: Path) -> None:
    await subagents_add({"preset": "opencode"})
    entry = next(e for e in _stored(config_path) if e["name"] == "opencode")
    assert entry["description"]  # the preset's shipped text, not blank


async def test_add_preserves_empty_mcps_and_false_secret_policy(config_path: Path) -> None:
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


async def test_add_disables_an_openai_preset_that_has_no_key(config_path: Path) -> None:
    # mirothinker ships an empty apiKey. Added enabled, it would be advertised to
    # the model and fail on first dispatch.
    await subagents_add({"preset": "mirothinker", "name": "Deep"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Deep")
    assert entry["enabled"] is False


async def test_add_keeps_an_openai_preset_enabled_when_a_key_is_supplied(config_path: Path) -> None:
    await subagents_add({"preset": "mirothinker", "name": "Deep", "api_key": "sk-live"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Deep")
    assert entry["enabled"] is True


async def test_add_keeps_a_cli_preset_enabled(config_path: Path) -> None:
    await subagents_add({"preset": "opencode"})
    entry = next(e for e in _stored(config_path) if e["name"] == "opencode")
    assert entry["enabled"] is True


async def test_add_rejects_a_duplicate_name_without_writing(config_path: Path) -> None:
    before = _stored(config_path)
    # Surfaced as ConfigValidationError (-32011), not the -32603 internal_error
    # the dispatcher would otherwise turn a bare ValueError into.
    with pytest.raises(ConfigValidationError, match="Coder"):
        await subagents_add({"preset": "opencode", "name": "Coder"})
    assert _stored(config_path) == before


async def test_add_trims_a_padded_name(config_path: Path) -> None:
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


async def test_toggle_flips_enabled(config_path: Path) -> None:
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

    assert await subagents_toggle({"name": "Raven-Probe", "enabled": True}) == {"enabled": True}

    assert [e for e in _stored(config_path) if e["name"] == "Raven-Probe"] == []


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

    assert await subagents_toggle({"name": "Raven-Probe", "enabled": True}) == {"enabled": True}

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
    assert await subagents_toggle({"name": "Raven-Probe", "enabled": True}) == {"enabled": True}
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


async def test_toggle_still_refuses_a_name_nothing_knows(config_path: Path) -> None:
    """The materializing branch must not turn an unknown name into a success."""
    with pytest.raises(SubagentNotFoundError):
        await subagents_toggle({"name": "Nobody", "enabled": True})


async def test_remove_deletes_the_entry(config_path: Path) -> None:
    assert await subagents_remove({"name": "Coder"}) == {"removed": True}
    assert all(e["name"] != "Coder" for e in _stored(config_path))


async def test_remove_reports_false_for_an_unknown_name(config_path: Path) -> None:
    assert await subagents_remove({"name": "nope"}) == {"removed": False}


async def test_a_mutation_hot_applies_to_the_live_loop(config_path: Path) -> None:
    applied: list[list] = []

    class _Loop:
        def apply_agents(self, configs: list) -> None:
            applied.append(configs)

    await subagents_toggle({"name": "Researcher", "enabled": True}, agent_loop_factory=lambda: _Loop())
    assert len(applied) == 1
    assert [c.name for c in applied[0]] == ["Coder", "Researcher"]


async def test_a_mutation_without_a_live_loop_still_writes(config_path: Path) -> None:
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
    out = await subagents_test({"name": "opencode", "source": "preset"})
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
    # A preset is a template no config claims, so `_test_acp` records no snapshot
    # for it and the table has nothing to re-derive.
    from raven.agent.subagent.probe import TestResult

    applied: list[list] = []

    class _Loop:
        def apply_agents(self, configs: list) -> None:
            applied.append(configs)

    async def fake_run_test(cfg, *, source):
        return TestResult(cfg.name, source, "acp", True, "connected", None, 5)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)
    await subagents_test({"name": "opencode", "source": "preset"}, agent_loop_factory=lambda: _Loop())
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


def _product_tree(tmp_path: Path, *, launcher: bool = True, engine: dict | None = None) -> Path:
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
        "kind": "cli",
        "description": "d",
        "command": "{PYTHON} {SUBAGENT_DIR}/run.py {prompt}",
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
