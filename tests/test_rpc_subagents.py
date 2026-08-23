"""Tests for the ``subagents.*`` RPC handlers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

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
    }
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


# --------------------------------------------------------------- building a vendored folder


@pytest.fixture(autouse=True)
def _no_build_state_between_tests():
    """Empty the module-level build maps around each test.

    ``_BUILDING`` and ``_BUILD_ERROR`` are process-wide by design -- a build
    outlives the request that started it, and the row's flag is read from a later
    one -- so without this a task left behind by one test is a task the next one
    finds under the same name, and the pair pass or hang depending on order.

    Cancelled, never awaited: each task belongs to its own test's event loop,
    which is already closed by the time the next test runs, and awaiting one from
    a different loop is what turned this cleanup into the hang it exists to
    prevent.
    """
    from raven.rpc.methods.subagents import _BUILD_ERROR, _BUILDING

    def clear() -> None:
        for task in list(_BUILDING.values()):
            task.cancel()
        _BUILDING.clear()
        _BUILD_ERROR.clear()

    clear()
    yield
    clear()


def _vendored_tree(tmp_path: Path, *, script: str) -> Path:
    """A `subagents/` tree with one folder and a stand-in installer.

    The real `install.sh` runs `uv sync` -- minutes and hundreds of MB -- so what
    is exercised here is everything around it: the lookup, the one-at-a-time
    guard, and that the verdict is read off the filesystem rather than taken from
    the exit status.
    """
    root = tmp_path / "subagents"
    folder = root / "raven-probe"
    (folder / "Build").mkdir(parents=True)
    (folder / "subagent.json").write_text(
        json.dumps(
            {
                "name": "Raven-Probe",
                "kind": "cli",
                "description": "d",
                "command": "{PYTHON} {SUBAGENT_DIR}/run.py {prompt}",
            }
        ),
        encoding="utf-8",
    )
    (folder / "config.json").write_text("{}", encoding="utf-8")
    (folder / "install.py").write_text("", encoding="utf-8")
    (folder / "Build" / "pyproject.toml").write_text("", encoding="utf-8")
    (root / "install.sh").write_text(script, encoding="utf-8")
    return root


def _installer(monkeypatch: pytest.MonkeyPatch, *, code: int = 0, output: str = "", builds: Path | None = None):
    """Stand in for `_run_installer`, optionally producing the launcher.

    Never a real subprocess: every async test gets a fresh event loop, and the
    second fork in one process waits forever on the child watcher the first loop
    left behind -- which turned a wrong verdict into a hang instead of a failure.
    """

    async def fake(_installer_path: Path, _folder_name: str) -> tuple[int, str]:
        if builds is not None:
            launcher = builds / ".venv" / "bin" / "raven"
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o755)
        return code, output

    monkeypatch.setattr("raven.rpc.methods.subagents._run_installer", fake)


async def test_build_starts_and_the_row_reports_it_ready_afterwards(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import vendored_agents as va
    from raven.rpc.methods.subagents import _BUILDING, subagents_build

    root = _vendored_tree(tmp_path, script="")
    monkeypatch.setattr(va, "subagents_root", lambda: root)
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: True)
    _installer(monkeypatch, builds=root / "raven-probe" / "Build")

    before = await subagents_list({"probe": False})
    assert [r for r in before["rows"] if r["name"] == "Raven-Probe"][0]["enabled"] is False

    result = await subagents_build({"name": "Raven-Probe"})
    assert result == {"building": True, "detail": ""}
    await _BUILDING["Raven-Probe"]

    after = await subagents_list({"probe": False})
    row = [r for r in after["rows"] if r["name"] == "Raven-Probe"][0]
    assert row["enabled"] is True, "the readiness verdict is re-read, not remembered"
    assert row["building"] is False


async def test_a_build_that_leaves_no_launcher_is_a_failure_whatever_it_exited(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`exit 0` is not the claim that matters.

    The installer does several things per folder; the one this feature needs is a
    launcher raven can start. Trusting the status would report an agent ready that
    the dispatching model then fails on.
    """
    from raven.agent.subagent import vendored_agents as va
    from raven.rpc.methods.subagents import _BUILD_ERROR, _BUILDING, subagents_build

    root = _vendored_tree(tmp_path, script="")
    monkeypatch.setattr(va, "subagents_root", lambda: root)
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: True)
    _installer(monkeypatch, code=0, output="resolved 0 packages\n")

    await subagents_build({"name": "Raven-Probe"})
    await _BUILDING["Raven-Probe"]

    assert "resolved 0 packages" in _BUILD_ERROR["Raven-Probe"]
    row = [r for r in (await subagents_list({"probe": False}))["rows"] if r["name"] == "Raven-Probe"][0]
    assert row["enabled"] is False
    assert row["probe_detail"] == _BUILD_ERROR["Raven-Probe"], "the page shows why, not just that"


async def test_a_silent_failure_still_names_the_exit_status(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installer that fails without saying anything still has to leave the
    reader something to act on."""
    from raven.agent.subagent import vendored_agents as va
    from raven.rpc.methods.subagents import _BUILD_ERROR, _BUILDING, subagents_build

    root = _vendored_tree(tmp_path, script="")
    monkeypatch.setattr(va, "subagents_root", lambda: root)
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: True)
    _installer(monkeypatch, code=2, output="   \n")

    await subagents_build({"name": "Raven-Probe"})
    await _BUILDING["Raven-Probe"]

    assert "exited 2" in _BUILD_ERROR["Raven-Probe"]


async def test_a_second_build_reports_the_running_one_rather_than_starting_another(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two `uv sync` runs against one directory is how a venv ends up half-written."""
    import asyncio as aio

    from raven.agent.subagent import vendored_agents as va
    from raven.rpc.methods.subagents import _BUILDING, subagents_build

    root = _vendored_tree(tmp_path, script="")
    monkeypatch.setattr(va, "subagents_root", lambda: root)
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: True)
    gate: aio.Event = aio.Event()
    calls: list[int] = []

    async def held(_installer_path: Path, _folder_name: str) -> tuple[int, str]:
        calls.append(1)
        await gate.wait()
        return 0, ""

    monkeypatch.setattr("raven.rpc.methods.subagents._run_installer", held)

    first = await subagents_build({"name": "Raven-Probe"})
    await aio.sleep(0)  # let the task reach the installer before asking again
    second = await subagents_build({"name": "Raven-Probe"})

    assert first["detail"] == ""
    assert second["building"] is True and "already running" in second["detail"]
    assert len(calls) == 1, "the second call must not start a second build"

    gate.set()
    await _BUILDING["Raven-Probe"]


async def test_a_row_being_built_reads_as_working_not_as_broken(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving "venv not built -- run install.sh" up for the minutes a build takes
    reads as nothing having happened."""
    import asyncio as aio

    from raven.agent.subagent import vendored_agents as va
    from raven.rpc.methods.subagents import _BUILDING, subagents_build

    root = _vendored_tree(tmp_path, script="")
    monkeypatch.setattr(va, "subagents_root", lambda: root)
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: True)
    gate: aio.Event = aio.Event()

    async def held(_installer_path: Path, _folder_name: str) -> tuple[int, str]:
        await gate.wait()
        return 0, ""

    monkeypatch.setattr("raven.rpc.methods.subagents._run_installer", held)

    await subagents_build({"name": "Raven-Probe"})
    await aio.sleep(0)
    row = [r for r in (await subagents_list({"probe": False}))["rows"] if r["name"] == "Raven-Probe"][0]

    assert row["building"] is True
    assert row["probe_status"] != "missing"
    assert "install.sh" not in row["probe_detail"]

    gate.set()
    await _BUILDING["Raven-Probe"]


async def test_an_unbuilt_folder_reports_missing_so_the_page_offers_install(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pairing test for the one below: `missing` is what the page reads as
    "offer Install", and the unbuilt venv is the state that button fixes."""
    from raven.agent.subagent import vendored_agents as va

    root = _vendored_tree(tmp_path, script="")
    monkeypatch.setattr(va, "subagents_root", lambda: root)
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: True)

    row = [r for r in (await subagents_list({"probe": False}))["rows"] if r["name"] == "Raven-Probe"][0]

    assert row["probe_status"] == "missing"
    assert "venv not built" in row["probe_detail"]


async def test_a_folder_missing_only_a_credential_does_not_offer_install(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its venv is built and its problem is a key, which the installer cannot mint
    -- it scaffolds `.env` from `.env.example`, whose bare `NAME=` lines
    `api_key_present` correctly reads as absent. So the button would return in
    seconds having changed nothing, and the row says what is wrong instead.

    `attention` rather than a new wire field: the page already renders that status
    as the detail line, and already gates Install on `missing`, so the fact lands
    where it is needed without a fifth place to declare the row's shape.
    """
    from raven.agent.subagent import vendored_agents as va

    root = _vendored_tree(tmp_path, script="")
    launcher = root / "raven-probe" / "Build" / ".venv" / "bin" / "raven"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setattr(va, "subagents_root", lambda: root)
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: False)

    row = [r for r in (await subagents_list({"probe": False}))["rows"] if r["name"] == "Raven-Probe"][0]

    assert row["enabled"] is False
    assert row["probe_status"] == "attention"
    assert "no LLM credential" in row["probe_detail"]


async def test_a_tree_with_no_installer_says_so_where_the_client_can_read_it(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason has to travel, not just be raised.

    The dispatcher fills `data` from a handler's `detail` only when the handler
    passed no `data` of its own, so naming the folder in `data` silently dropped
    the sentence: the client received `message` (the error's code name,
    "subagent_not_found") and nothing else. Measured on a real install -- a tree
    copied without its `install.sh` offered Install and answered with two words
    that named neither the tree nor the missing script.
    """
    from raven.agent.subagent import vendored_agents as va
    from raven.rpc.methods.subagents import subagents_build

    root = _vendored_tree(tmp_path, script="")
    (root / "install.sh").unlink()
    monkeypatch.setattr(va, "subagents_root", lambda: root)

    with pytest.raises(SubagentNotFoundError) as caught:
        await subagents_build({"name": "Raven-Probe"})

    assert caught.value.data is not None
    assert "install.sh" in caught.value.data["detail"]
    assert caught.value.data["name"] == "Raven-Probe"


async def test_a_finished_build_refreshes_the_agent_table(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the agent is installed and the model still cannot name it.

    A build changes readiness, which lives on the filesystem, while the roster the
    dispatching model reads is a snapshot taken the last time `apply` ran -- and a
    build triggers no config write, so nothing re-ran it. The page showed the row
    ready (it re-discovers per request) and the model's own list did not have the
    name in it, which from the outside is indistinguishable from a failed install.
    """
    from raven.agent.subagent import vendored_agents as va
    from raven.rpc.methods.subagents import _BUILDING, subagents_build

    root = _vendored_tree(tmp_path, script="")
    monkeypatch.setattr(va, "subagents_root", lambda: root)
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: True)
    _installer(monkeypatch, builds=root / "raven-probe" / "Build")

    applied: list[object] = []

    class _Loop:
        def apply_agents(self, configs: object) -> None:
            applied.append(configs)

    await subagents_build({"name": "Raven-Probe"}, agent_loop_factory=lambda: _Loop())
    await _BUILDING["Raven-Probe"]

    assert applied, "a finished build left the loop's table as it was"


async def test_a_failed_build_does_not_refresh_the_table(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pairing case, so the assertion above cannot pass by refreshing always:
    nothing became callable, and re-composing would claim something did."""
    from raven.agent.subagent import vendored_agents as va
    from raven.rpc.methods.subagents import _BUILDING, subagents_build

    root = _vendored_tree(tmp_path, script="")
    monkeypatch.setattr(va, "subagents_root", lambda: root)
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: True)
    _installer(monkeypatch, code=1, output="uv sync failed")

    applied: list[object] = []

    class _Loop:
        def apply_agents(self, configs: object) -> None:
            applied.append(configs)

    await subagents_build({"name": "Raven-Probe"}, agent_loop_factory=lambda: _Loop())
    await _BUILDING["Raven-Probe"]

    assert applied == []


async def test_building_a_name_no_folder_carries_is_refused(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import vendored_agents as va
    from raven.rpc.methods.subagents import subagents_build

    root = _vendored_tree(tmp_path, script="")
    monkeypatch.setattr(va, "subagents_root", lambda: root)

    with pytest.raises(SubagentNotFoundError):
        await subagents_build({"name": "claude_code"})


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
