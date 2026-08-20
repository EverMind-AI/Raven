"""Tests for the ``subagents.*`` RPC handlers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.errors import ConfigValidationError
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
                            "command": "claude -p {prompt}",
                            "description": "coding",
                            "enabled": True,
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
    assert by_name["Researcher"]["enabled"] is False
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
    # those two are excluded from the unconfigured-preset rows. The built-in rows
    # lead the list: they are on the agent table whether config mentions them or
    # not, so an overlay that omitted them would be hiding half the roster the
    # model dispatches to.
    assert set(by_name) == {
        "raven",
        "research-raven",
        "code-raven",
        "data-raven",
        "content-raven",
        "Coder",
        "Researcher",
        "codex",
        "openclaw",
        "opencode",
        "hermes",
    }
    # Their own group, not installed/uninstalled: there is nothing to install, and
    # a row that could only ever read "uninstalled" would say the opposite.
    assert by_name["research-raven"]["group"] == "builtin"
    assert by_name["research-raven"]["builtin"] is True
    assert by_name["research-raven"]["configured"] is False
    assert by_name["research-raven"]["enabled"] is True
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


async def test_toggling_a_builtin_row_creates_its_override(config_path: Path) -> None:
    """``enabled`` is the only way to take a built-in agent off the roster.

    The row exists on the table without existing in config, so the first toggle
    has to write the override rather than report the name unknown -- and it must
    write nothing but the switch, so the description and skills keep coming from
    the package's own row.
    """
    result = await subagents_toggle({"name": "research-raven", "enabled": False})
    assert result == {"enabled": False}

    stored = [e for e in _stored(config_path) if e["name"] == "research-raven"]
    assert len(stored) == 1
    assert stored[0]["kind"] == "builtin"
    assert stored[0]["enabled"] is False

    rows = {r["name"]: r for r in (await subagents_list({"probe": False}))["rows"]}
    assert rows["research-raven"]["enabled"] is False
    # And the switch is all it overrode: the description still comes from the
    # package's row. A write dumps the whole model, so applying the stored row
    # field-for-field would blank the only line the model reads about this agent.
    assert "Deep retrieval" in rows["research-raven"]["description"]
    # Still on the list -- switched off is not deleted, and it has to stay
    # reachable to be switched back on.
    assert rows["research-raven"]["group"] == "builtin"

    await subagents_toggle({"name": "research-raven", "enabled": True})
    assert (await subagents_list({"probe": False}))["rows"] != []


async def test_a_builtin_name_cannot_be_added_as_another_transport(config_path: Path) -> None:
    """The write primitive refuses it, so every RPC that writes inherits the guard."""
    from raven.rpc.errors import ConfigValidationError

    with pytest.raises(ConfigValidationError):
        await subagents_update({"name": "Coder", "new_name": "research-raven"})
