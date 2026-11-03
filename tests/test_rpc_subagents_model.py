"""Tests for ``subagents.update``'s model and factory-description handling.

Split out of ``test_rpc_subagents.py`` (per the plan) rather than added to it:
these exercise the acp-menu / host-catalogue validation and the broadened
description reset, none of which the existing file's fixture or helpers touch.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.acp_client.capabilities import (
    AcpEffortChoice,
    AcpModelChoice,
    CapabilitySnapshot,
    SnapshotStore,
    snapshot_fingerprint,
)
from raven.config.agent_names import GENERIC_AGENT
from raven.rpc.errors import ConfigFieldReadonlyError, ConfigValidationError, SubagentNotReadyError
from raven.rpc.methods.subagents import _as_configs, subagents_list, subagents_update


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config file the handlers write to instead of the real ``~/.raven``."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "subagents": {
                    "agents": [
                        {
                            "name": "Hermes Agent",
                            "preset": "hermes",
                            "kind": "acp",
                            "command": "hermes acp --accept-hooks",
                            "description": "custom desc",
                            "enabled": True,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
    monkeypatch.setattr("raven.rpc.methods.subagents.get_config_path", lambda: path)
    return path


def _stored(path: Path) -> list[dict]:
    return json.loads(path.read_text())["subagents"]["agents"]


def _gate_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the agent the re-key gate asks before the write lands.

    Every acp row in this file is enabled, and changing a live row's model now
    sends it one real prompt -- so a test about what this handler *validates*
    would otherwise launch `hermes acp` for real. A test about the gate itself
    belongs in `test_rpc_subagents.py`, which installs its own stub.

    Both halves are stood in for, not just the ping: an acp row that answers is
    then measured, and leaving that live spent sixteen seconds per test waiting
    on a handshake with an agent that is not installed. The gate swallows that
    failure by design, so the only sign was the idle ceiling. So is the login
    shell the gate reads again before it asks (`_read_the_shell_again`), which
    is the machine's rather than the test's.
    """
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    async def _ok(cfg):
        return PingResult(True, "it ran and replied")

    async def _measured(cfg):
        return None

    async def _shell_not_read(cfg):
        return None

    monkeypatch.setattr(subagents_mod, "ping_agent", _ok)
    monkeypatch.setattr(subagents_mod, "record_capabilities", _measured)
    monkeypatch.setattr(subagents_mod, "_read_the_shell_again", _shell_not_read)


@pytest.fixture(autouse=True)
def _no_live_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model pick on an unmeasured acp row measures it first; here nothing is launched."""

    async def _unmeasured(cfg: object) -> None:
        return None

    monkeypatch.setattr("raven.rpc.methods.subagents.record_capabilities", _unmeasured)


def _fake_agent_meta(choices: tuple[str, ...], efforts: tuple[str, ...] = ()):
    """A stand-in for ``agent_meta`` reporting a fixed acp model menu.

    Real ``agent_meta`` reads a capability snapshot off disk; these tests are
    about the update handler's own validation, not about the snapshot store,
    so the menu it reads from is nailed down instead of measured.
    """

    def fake(cfg, *, snapshot=None):
        if getattr(cfg, "kind", None) != "acp":
            return SimpleNamespace(
                name=getattr(cfg, "name", ""),
                description=getattr(cfg, "description", ""),
                model_choices=(),
                effort_choices=(),
                stateful=False,
                reads_local_files=True,
                live_progress=False,
                owns="",
                modes=(),
                owns_watched_work=False,
                machine="",
            )
        return SimpleNamespace(
            name=getattr(cfg, "name", ""),
            description=getattr(cfg, "description", ""),
            model_choices=tuple(SimpleNamespace(value=v, name=v, group="") for v in choices),
            effort_choices=tuple(SimpleNamespace(value=v, name=v) for v in efforts),
            stateful=False,
            reads_local_files=True,
            live_progress=False,
            owns="",
            modes=(),
            owns_watched_work=False,
            machine="",
        )

    return fake


async def test_update_accepts_a_model_the_acp_row_advertises(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate_answers(monkeypatch)
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(("vendor/a", "vendor/b")))
    out = await subagents_update({"name": "Hermes Agent", "model": "vendor/a"})
    assert out == {"updated": True, "name": "Hermes Agent"}
    entry = next(e for e in _stored(config_path) if e["name"] == "Hermes Agent")
    assert entry["model"] == "vendor/a"


async def test_update_rejects_a_model_the_acp_row_does_not_advertise(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(("vendor/a",)))
    with pytest.raises(ConfigValidationError, match="offers 1"):
        await subagents_update({"name": "Hermes Agent", "model": "vendor/bogus"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Hermes Agent")
    assert "model" not in entry or entry["model"] is None


async def test_update_rejects_any_model_when_a_third_party_acp_row_advertises_none(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermes is nobody's own -- no snapshot names raven -- so an empty menu is
    the whole vocabulary: raven's own ids would be refused by the agent."""
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(()))
    with pytest.raises(ConfigValidationError, match="offers none"):
        await subagents_update({"name": "Hermes Agent", "model": "vendor/a"})


async def test_list_reports_the_effort_and_the_menu_that_offers_it(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate_answers(monkeypatch)
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(("vendor/a",), ("low", "high")))
    rows = (await subagents_list({"probe": False}))["rows"]
    row = next(r for r in rows if r["name"] == "Hermes Agent")
    assert row["effort"] is None
    assert row["effort_choices"] == [
        {"value": "low", "name": "low"},
        {"value": "high", "name": "high"},
    ]


async def test_update_accepts_an_effort_the_acp_row_advertises(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate_answers(monkeypatch)
    monkeypatch.setattr(
        "raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(("vendor/a",), ("low", "high", "default"))
    )
    out = await subagents_update({"name": "Hermes Agent", "effort": "high"})
    assert out == {"updated": True, "name": "Hermes Agent"}
    entry = next(e for e in _stored(config_path) if e["name"] == "Hermes Agent")
    assert entry["effort"] == "high"


async def test_update_rejects_an_effort_the_acp_row_does_not_advertise(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(("vendor/a",), ("low", "high")))
    with pytest.raises(ConfigValidationError, match="reasoning effort"):
        await subagents_update({"name": "Hermes Agent", "effort": "max"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Hermes Agent")
    assert "effort" not in entry or entry["effort"] is None


async def test_update_refuses_effort_for_a_non_acp_row(config_path: Path) -> None:
    with pytest.raises(ConfigFieldReadonlyError, match="only an acp agent"):
        await subagents_update({"name": GENERIC_AGENT, "effort": "high"})


async def test_update_refuses_effort_when_the_menu_cannot_be_read(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("raven.rpc.methods.subagents.acp_snapshot_for", lambda cfg: None)

    async def fail(cfg):
        raise RuntimeError("no handshake")

    monkeypatch.setattr("raven.rpc.methods.subagents.record_capabilities", fail)
    with pytest.raises(ConfigValidationError, match="offers none"):
        await subagents_update({"name": "Hermes Agent", "effort": "high"})


async def test_update_clears_an_effort_without_needing_a_menu(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing must work after an agent upgrade removed the option; the stale
    value would otherwise be unsendable and unremovable."""
    _gate_answers(monkeypatch)
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(("vendor/a",), ("high",)))
    await subagents_update({"name": "Hermes Agent", "effort": "high"})
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(("vendor/a",)))
    out = await subagents_update({"name": "Hermes Agent", "clear_effort": True})
    assert out == {"updated": True, "name": "Hermes Agent"}
    entry = next(e for e in _stored(config_path) if e["name"] == "Hermes Agent")
    assert entry["effort"] is None


_MENUS: dict[str, tuple[str, ...]] = {
    "vendor/reasoner": ("low", "high", "max"),
    "vendor/flash": (),
}


def _measured_snapshot(cfg: object, efforts: tuple[str, ...]) -> CapabilitySnapshot:
    """A handshake measured on the model ``cfg`` is on, and the menu it reported."""
    return CapabilitySnapshot(
        agent=getattr(cfg, "name", ""),
        fingerprint=snapshot_fingerprint(cfg),
        status="ready",
        detail="",
        measured_at_ms=1,
        model_choices=(
            AcpModelChoice(value="vendor/reasoner", name="Reasoner", group="vendor"),
            AcpModelChoice(value="vendor/flash", name="Flash", group="vendor"),
        ),
        effort_choices=tuple(AcpEffortChoice(value=value, name=value.title()) for value in efforts),
        effort_option="effort" if efforts else "",
    )


def _measures_per_model(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """A ``record_capabilities`` stand-in that records the menu of ``cfg.model``.

    Written to the real store, so the row the handler reads back is the one this
    measurement left: that read is what the picker draws.
    """
    measured: list[str | None] = []

    async def fake_record(cfg):
        measured.append(getattr(cfg, "model", None))
        snapshot = _measured_snapshot(cfg, _MENUS.get(cfg.model or "", ()))
        SnapshotStore().record(snapshot)
        return snapshot

    monkeypatch.setattr("raven.rpc.methods.subagents.record_capabilities", fake_record)
    return measured


async def test_a_model_only_update_re_measures_the_effort_menu_on_the_new_model(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row's effort menu is measured on the model the row is on.

    A model-only update moved the row without re-measuring anything: the model
    is not a launch field, so nothing marked the snapshot stale. The picker then
    offered rungs the new model does not take, `update {effort}` accepted them,
    and it refused rungs the new model does take.
    """
    _gate_answers(monkeypatch)
    row = next(entry for entry in _stored(config_path) if entry["name"] == "Hermes Agent")
    SnapshotStore().record(_measured_snapshot(_as_configs([row])[0], _MENUS["vendor/reasoner"]))
    measured = _measures_per_model(monkeypatch)

    await subagents_update({"name": "Hermes Agent", "model": "vendor/flash"})

    assert measured[-1] == "vendor/flash", "the move is measured on the model it moves to"
    rows = (await subagents_list({"probe": False}))["rows"]
    row = next(entry for entry in rows if entry["name"] == "Hermes Agent")
    assert row["model"] == "vendor/flash"
    assert row["effort_choices"] == [], "flash takes no rung, and the row must not say it does"
    with pytest.raises(ConfigValidationError, match="offers none"):
        await subagents_update({"name": "Hermes Agent", "effort": "high"})

    await subagents_update({"name": "Hermes Agent", "model": "vendor/reasoner"})
    rows = (await subagents_list({"probe": False}))["rows"]
    row = next(entry for entry in rows if entry["name"] == "Hermes Agent")
    assert [choice["value"] for choice in row["effort_choices"]] == ["low", "high", "max"]
    out = await subagents_update({"name": "Hermes Agent", "effort": "high"})
    assert out == {"updated": True, "name": "Hermes Agent"}


async def test_a_model_and_effort_in_one_call_check_the_effort_on_the_new_model(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same move carrying an effort must not be judged on the old menu.

    On a never-measured row the model branch measures before ``target["model"]``
    is assigned, and the effort branch then trusts that measurement. The pick is
    checked against the model the row is leaving.
    """
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"][0]["model"] = "vendor/reasoner"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    _gate_answers(monkeypatch)
    measured = _measures_per_model(monkeypatch)

    with pytest.raises(ConfigValidationError, match="offers none"):
        await subagents_update({"name": "Hermes Agent", "model": "vendor/flash", "effort": "high"})

    assert measured == ["vendor/reasoner", "vendor/flash"], "the move measured the new model and stopped there"
    entry = next(entry for entry in _stored(config_path) if entry["name"] == "Hermes Agent")
    assert entry["model"] == "vendor/reasoner", "the refused call writes nothing"
    rows = (await subagents_list({"probe": False}))["rows"]
    row = next(entry for entry in rows if entry["name"] == "Hermes Agent")
    assert [choice["value"] for choice in row["effort_choices"]] == ["low", "high", "max"], (
        "the store again describes the model the row kept"
    )


async def test_a_same_call_effort_is_judged_on_the_new_model_when_the_move_read_fails(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The never-measured branch measures the model the row is leaving, so its
    answer cannot stand for the model the same call moves to. When the move's
    own read fails, nothing measured the new model: the effort is refused as
    unchecked rather than judged on the old menu, the failing launch is not
    tried a second time, and the refusal puts the old record back."""
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"][0]["model"] = "vendor/reasoner"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    _gate_answers(monkeypatch)

    measured: list[str | None] = []
    calls = {"n": 0}

    async def fake_record(cfg):
        calls["n"] += 1
        measured.append(getattr(cfg, "model", None))
        if calls["n"] == 2:
            raise RuntimeError("the new model's handshake did not answer")
        snapshot = _measured_snapshot(cfg, _MENUS.get(cfg.model or "", ()))
        SnapshotStore().record(snapshot)
        return snapshot

    monkeypatch.setattr("raven.rpc.methods.subagents.record_capabilities", fake_record)

    with pytest.raises(ConfigValidationError, match="could not read"):
        await subagents_update({"name": "Hermes Agent", "model": "vendor/flash", "effort": "high"})

    assert measured == ["vendor/reasoner", "vendor/flash"], "the old model's read is not reused for the new one"
    entry = next(entry for entry in _stored(config_path) if entry["name"] == "Hermes Agent")
    assert entry["model"] == "vendor/reasoner", "the refused call writes nothing"
    rows = (await subagents_list({"probe": False}))["rows"]
    row = next(entry for entry in rows if entry["name"] == "Hermes Agent")
    assert [choice["value"] for choice in row["effort_choices"]] == ["low", "high", "max"], (
        "the store again describes the model the row kept"
    )


async def test_a_refused_move_drops_a_menu_the_row_never_adopted(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row measured for the first time by this call has no earlier record to
    restore, so a refusal must leave none behind either: the row keeps its
    model, and the store holds no menu for a model it never moved to."""
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"][0]["model"] = "vendor/reasoner"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    measured = _measures_per_model(monkeypatch)

    with pytest.raises(ConfigValidationError, match="offers none"):
        await subagents_update({"name": "Hermes Agent", "clear_model": True, "effort": "high"})

    assert measured == [None], "the clear is measured on the agent's own model"
    entry = next(entry for entry in _stored(config_path) if entry["name"] == "Hermes Agent")
    assert entry["model"] == "vendor/reasoner"
    assert SnapshotStore().load([_as_configs([entry])[0]], allow_stale=True) == {}


def _set_row(config_path: Path, **fields: object) -> dict:
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"][0].update(fields)
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    return raw["subagents"]["agents"][0]


def _failing_handshake(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """The real ``record_capabilities`` over a handshake that never gets as far as
    ``session/new``: ``verify_agent`` does not raise, it answers an unusable
    snapshot, and ``_test_record`` keeps the previous record's menus under it."""
    from raven.agent.subagent.probe import record_capabilities

    tried: list[str | None] = []

    async def verify(cfg, **_kw):
        tried.append(getattr(cfg, "model", None))
        return replace(
            _measured_snapshot(cfg, ()), status="attention", detail="the agent did not start", measured_at_ms=2
        )

    monkeypatch.setattr("raven.rpc.methods.subagents.record_capabilities", record_capabilities)
    monkeypatch.setattr("raven.acp_client.capabilities.verify_agent", verify)
    return tried


async def test_a_move_whose_handshake_fails_does_not_judge_the_effort_on_the_old_menu(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed handshake is an answer, not an exception, and the record it leaves
    still carries the old model's rungs. Reading that as the new model's menu let
    `high` through onto a model that takes none. Nothing measured the new model,
    so the pick is refused as unchecked, once, and the store is put back."""
    row = _set_row(config_path, model="vendor/reasoner")
    SnapshotStore().record(_measured_snapshot(_as_configs([row])[0], _MENUS["vendor/reasoner"]))
    _gate_answers(monkeypatch)
    tried = _failing_handshake(monkeypatch)

    with pytest.raises(ConfigValidationError, match="could not read") as refused:
        await subagents_update({"name": "Hermes Agent", "model": "vendor/flash", "effort": "high"})

    assert refused.value.data == {"field": "effort", "name": "Hermes Agent"}
    assert tried == ["vendor/flash"], "one handshake, not a second launch of the one that failed"
    entry = next(entry for entry in _stored(config_path) if entry["name"] == "Hermes Agent")
    assert (entry["model"], entry.get("effort")) == ("vendor/reasoner", None), "the refused call writes nothing"
    restored = SnapshotStore().load([_as_configs([entry])[0]], allow_stale=True)["Hermes Agent"]
    assert (restored.status, restored.measured_at_ms) == ("ready", 1), "the pre-move record is back"


async def test_a_model_only_move_drops_a_stored_effort_the_new_model_does_not_take(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move re-measures the menu, so the stored pick is judged against it. A
    rung the new model lacks would be refused by the agent on every turn, and
    with no menu the page hides the picker, leaving a value nobody can see or
    clear. One the new model does take is kept."""
    row = _set_row(config_path, model="vendor/reasoner", effort="high")
    SnapshotStore().record(_measured_snapshot(_as_configs([row])[0], _MENUS["vendor/reasoner"]))
    _gate_answers(monkeypatch)
    _measures_per_model(monkeypatch)

    await subagents_update({"name": "Hermes Agent", "model": "vendor/flash"})
    entry = next(entry for entry in _stored(config_path) if entry["name"] == "Hermes Agent")
    assert (entry["model"], entry.get("effort")) == ("vendor/flash", None)

    _set_row(config_path, effort="high")
    await subagents_update({"name": "Hermes Agent", "model": "vendor/reasoner"})
    entry = next(entry for entry in _stored(config_path) if entry["name"] == "Hermes Agent")
    assert (entry["model"], entry.get("effort")) == ("vendor/reasoner", "high"), "a rung the new model takes stays"


async def test_a_model_only_move_whose_handshake_fails_keeps_the_stored_effort(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a measurement of the new model there is nothing to judge the pick
    against, so it is left as it was rather than dropped on a guess."""
    row = _set_row(config_path, model="vendor/reasoner", effort="high")
    SnapshotStore().record(_measured_snapshot(_as_configs([row])[0], _MENUS["vendor/reasoner"]))
    _gate_answers(monkeypatch)
    _failing_handshake(monkeypatch)

    await subagents_update({"name": "Hermes Agent", "model": "vendor/flash"})

    entry = next(entry for entry in _stored(config_path) if entry["name"] == "Hermes Agent")
    assert (entry["model"], entry.get("effort")) == ("vendor/flash", "high")


async def test_an_enabled_rows_model_only_move_asks_the_agent_before_measuring_it(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connect gate launches the agent too. Measuring first meant an agent that
    does not start was waited out twice -- the handshake, then the prompt -- before
    the move was refused. A move that carries no effort needs no menu before the
    gate, so the gate goes first and a refusal spends one wait. A move that does
    carry one is judged before the gate spends a prompt on it."""
    import raven.rpc.methods.subagents as subagents_mod
    from raven.agent.subagent.probe import PingResult

    row = _set_row(config_path, model="vendor/reasoner")
    SnapshotStore().record(_measured_snapshot(_as_configs([row])[0], _MENUS["vendor/reasoner"]))
    _gate_answers(monkeypatch)
    order: list[str] = []
    answers = {"ok": True}

    async def ping(cfg):
        order.append("ping")
        return PingResult(answers["ok"], "it ran and replied" if answers["ok"] else "it did not start")

    async def record(cfg):
        order.append(f"measure {cfg.model}")
        snapshot = _measured_snapshot(cfg, _MENUS.get(cfg.model or "", ()))
        SnapshotStore().record(snapshot)
        return snapshot

    monkeypatch.setattr(subagents_mod, "ping_agent", ping)
    monkeypatch.setattr(subagents_mod, "record_capabilities", record)

    await subagents_update({"name": "Hermes Agent", "model": "vendor/flash"})
    assert order == ["ping", "measure vendor/flash"]

    order.clear()
    await subagents_update({"name": "Hermes Agent", "model": "vendor/reasoner", "effort": "high"})
    assert order == ["measure vendor/reasoner", "ping"]

    order.clear()
    answers["ok"] = False
    with pytest.raises(SubagentNotReadyError):
        await subagents_update({"name": "Hermes Agent", "model": "vendor/flash"})
    assert order == ["ping"], "a move the agent cannot answer is refused without a handshake"


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("written", ["Other", "Hermes Agent"])
async def test_a_write_that_lands_while_a_move_is_measured_is_kept(
    config_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool, written: str
) -> None:
    """The move's handshake is an await bounded only by the agent's readiness
    timeout, and other ``subagents.*`` writes land during it -- the page runs
    them side by side. Writing back the list read before the handshake reverted
    every one of them after it had answered success. The list is read again after
    the last await and only this call's own fields are replayed onto it, on the
    row it edits as on any other."""
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"][0].update(model="vendor/reasoner", enabled=enabled)
    raw["subagents"]["agents"].append(
        {"name": "Other", "kind": "acp", "command": "other acp", "description": "old", "enabled": False}
    )
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    row = raw["subagents"]["agents"][0]
    SnapshotStore().record(_measured_snapshot(_as_configs([row])[0], _MENUS["vendor/reasoner"]))
    _gate_answers(monkeypatch)

    async def record(cfg):
        if cfg.name == "Hermes Agent" and cfg.model == "vendor/flash":
            await subagents_update({"name": written, "description": "written meanwhile"})
        snapshot = _measured_snapshot(cfg, _MENUS.get(cfg.model or "", ()))
        SnapshotStore().record(snapshot)
        return snapshot

    monkeypatch.setattr("raven.rpc.methods.subagents.record_capabilities", record)

    await subagents_update({"name": "Hermes Agent", "model": "vendor/flash"})

    stored = {entry["name"]: entry for entry in _stored(config_path)}
    assert stored["Hermes Agent"]["model"] == "vendor/flash"
    assert stored[written]["description"] == "written meanwhile", "the other write answered success and stays"


async def test_two_model_moves_measured_at_once_both_land(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two picks on two rows, each waiting on its own handshake: both answer
    success, so both moves have to be on disk afterwards."""
    import asyncio

    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"][0]["model"] = "vendor/reasoner"
    raw["subagents"]["agents"].append(
        {"name": "Second", "kind": "acp", "command": "second acp", "model": "vendor/reasoner", "enabled": True}
    )
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    for row in raw["subagents"]["agents"]:
        SnapshotStore().record(_measured_snapshot(_as_configs([row])[0], _MENUS["vendor/reasoner"]))
    _gate_answers(monkeypatch)
    both_measuring = asyncio.Event()
    measuring: list[str] = []

    async def record(cfg):
        if cfg.model == "vendor/flash":
            measuring.append(cfg.name)
            if len(measuring) == 2:
                both_measuring.set()
            await both_measuring.wait()
        snapshot = _measured_snapshot(cfg, _MENUS.get(cfg.model or "", ()))
        SnapshotStore().record(snapshot)
        return snapshot

    monkeypatch.setattr("raven.rpc.methods.subagents.record_capabilities", record)

    await asyncio.gather(
        subagents_update({"name": "Hermes Agent", "model": "vendor/flash"}),
        subagents_update({"name": "Second", "model": "vendor/flash"}),
    )

    assert sorted(measuring) == ["Hermes Agent", "Second"], "the two handshakes overlapped"
    assert {entry["name"]: entry.get("model") for entry in _stored(config_path)} == {
        "Hermes Agent": "vendor/flash",
        "Second": "vendor/flash",
    }


async def test_a_rename_onto_a_taken_name_is_refused_before_anything_is_measured(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store keys a capability record by agent name, so a measurement taken
    under the new name before the duplicate check wrote over the other row's
    record, and the rollback then deleted it. The duplicate is refused first."""
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"][0]["model"] = "vendor/reasoner"
    raw["subagents"]["agents"].append({"name": "Other", "kind": "acp", "command": "other acp", "enabled": True})
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    other = next(c for c in _as_configs(raw["subagents"]["agents"]) if c.name == "Other")
    SnapshotStore().record(replace(_measured_snapshot(other, ("low",)), measured_at_ms=5))
    _gate_answers(monkeypatch)
    measured = _measures_per_model(monkeypatch)

    with pytest.raises(ConfigValidationError, match="duplicate"):
        await subagents_update({"name": "Hermes Agent", "new_name": "Other", "model": "vendor/flash"})

    assert measured == [], "nothing was launched for a write that could never land"
    kept = SnapshotStore().load([other], allow_stale=True)["Other"]
    assert (kept.measured_at_ms, [c.value for c in kept.effort_choices]) == (5, ["low"])


async def test_a_refused_move_under_an_alias_spelling_puts_the_rows_record_back(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A built-in row is addressed under any name it has had, so the request's
    name need not be the key its record is stored under. The rollback goes by the
    key the measurement wrote: the pre-move record is restored, not dropped."""
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"] = [
        {"name": GENERIC_AGENT, "kind": "acp", "command": "raven acp", "model": "vendor/reasoner", "enabled": True}
    ]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    cfg = _as_configs(raw["subagents"]["agents"])[0]
    SnapshotStore().record(replace(_measured_snapshot(cfg, _MENUS["vendor/reasoner"]), measured_at_ms=5))
    _gate_answers(monkeypatch)
    _measures_per_model(monkeypatch)

    with pytest.raises(ConfigValidationError, match="offers none"):
        await subagents_update({"name": "Raven", "model": "vendor/flash", "effort": "high"})

    restored = SnapshotStore().load([cfg], allow_stale=True).get(GENERIC_AGENT)
    assert restored is not None and restored.measured_at_ms == 5
    assert [c.value for c in restored.effort_choices] == ["low", "high", "max"]


def _with_providers(config_path: Path, providers: dict) -> None:
    raw = json.loads(config_path.read_text())
    raw["providers"] = providers
    config_path.write_text(json.dumps(raw), encoding="utf-8")


async def test_update_materializes_a_builtin_override_and_checks_its_model_against_ravens_providers(
    config_path: Path,
) -> None:
    """``Raven`` (the generic built-in agent) has no config row until its first
    edit; setting its model must both create the override row and validate the
    value against raven's own providers, not against any agent menu: the id
    names a provider raven knows, and that provider holds a credential."""
    _with_providers(config_path, {"openai": {"apiKey": "sk-test"}})

    out = await subagents_update({"name": GENERIC_AGENT, "model": "openai/gpt-5"})
    assert out == {"updated": True, "name": GENERIC_AGENT}
    entry = next(e for e in _stored(config_path) if e["name"] == GENERIC_AGENT)
    assert entry["kind"] == "builtin"
    assert entry["model"] == "openai/gpt-5"


async def test_update_accepts_a_builtin_model_a_configured_section_lists_by_hand(config_path: Path) -> None:
    """A passthrough vendor no spec matches is reachable only through the model
    list its section carries, so that list is the other thing the check reads --
    and the id is stored naming the section, which is what the pool binds."""
    _with_providers(
        config_path, {"custom": {"apiBase": "http://127.0.0.1:8000/v1", "apiKey": "k", "models": ["my-local-model"]}}
    )

    await subagents_update({"name": GENERIC_AGENT, "model": "my-local-model"})
    entry = next(e for e in _stored(config_path) if e["name"] == GENERIC_AGENT)
    assert entry["model"] == "custom/my-local-model"


async def test_update_accepts_a_pick_under_a_section_no_spec_matches_when_sent_with_its_provider(
    config_path: Path,
) -> None:
    """The picker offers a passthrough section's own list (``model.options``
    adds config extras on purpose), so the pick arrives with ``provider`` and
    is prefixed before the check. The prefixed id names no spec; it names the
    section, and the tail is what the section's list holds."""
    _with_providers(
        config_path, {"mylocal": {"apiBase": "http://127.0.0.1:8000/v1", "apiKey": "k", "models": ["my-local-model"]}}
    )

    await subagents_update({"name": GENERIC_AGENT, "model": "my-local-model", "provider": "mylocal"})
    entry = next(e for e in _stored(config_path) if e["name"] == GENERIC_AGENT)
    assert entry["model"] == "mylocal/my-local-model"


async def test_update_rejects_an_id_a_section_no_spec_matches_does_not_list(config_path: Path) -> None:
    """A section raven has no spec for serves only what it lists: there is no
    catalogue to say a typed id exists there."""
    _with_providers(
        config_path, {"mylocal": {"apiBase": "http://127.0.0.1:8000/v1", "apiKey": "k", "models": ["my-local-model"]}}
    )

    with pytest.raises(ConfigValidationError, match="none of them can serve 'mylocal/other-model'"):
        await subagents_update({"name": GENERIC_AGENT, "model": "other-model", "provider": "mylocal"})


async def test_update_stores_a_bare_id_claimed_by_keyword_naming_its_provider(config_path: Path) -> None:
    """A bare id sent without a provider still lands prefixed: the pin reads
    the provider off the stored id, and a bare one would be re-claimed by
    keyword on every dispatch instead of naming the pair once."""
    _with_providers(config_path, {"openai": {"apiKey": "sk-test"}})

    await subagents_update({"name": GENERIC_AGENT, "model": "gpt-5"})
    entry = next(e for e in _stored(config_path) if e["name"] == GENERIC_AGENT)
    assert entry["model"] == "openai/gpt-5"


def test_host_pair_lets_an_unreadable_config_raise_rather_than_blame_the_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``PermissionError`` on the config file is the server's failure. Swallowed
    into ``None`` it came out as "your model or key is wrong", which asserts a
    fact about the reader's pick that nothing checked."""
    import raven.config.loader as loader_mod
    from raven.rpc.methods.subagents import _host_pair

    def unreadable() -> None:
        raise PermissionError("config.json")

    monkeypatch.setattr(loader_mod, "load_config", unreadable)
    with pytest.raises(PermissionError):
        _host_pair("openai/gpt-5")


async def test_update_rejects_a_builtin_model_no_provider_of_ravens_serves(config_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="none of them can serve 'nonsense-model-xyz'"):
        await subagents_update({"name": GENERIC_AGENT, "model": "nonsense-model-xyz"})
    assert not any(e["name"] == GENERIC_AGENT for e in _stored(config_path)), (
        "a rejected write must not leave a half-made override behind"
    )


async def test_update_rejects_a_builtin_model_whose_provider_holds_no_credential(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recognising the id is not enough: the dispatch pairs it with that
    provider's credential (``ProviderPool.bind_pin``) and drops the pin when
    there is none, so a write that passed here would run on the conversation's
    model with one log line to say so. Refused instead."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ConfigValidationError, match="no usable credentials"):
        await subagents_update({"name": GENERIC_AGENT, "model": "openai/gpt-5"})


async def test_update_stores_a_builtin_model_naming_the_provider_it_was_picked_under(config_path: Path) -> None:
    """A bare id is claimed by keyword matching at dispatch, which sends it
    wherever those rules land; the pair the reader picked is kept by spelling
    the provider into the stored id, the way ``config.set model`` stores the
    host's."""
    _with_providers(config_path, {"openrouter": {"apiKey": "sk-or-test"}})

    await subagents_update({"name": GENERIC_AGENT, "model": "gpt-5", "provider": "openrouter"})
    entry = next(e for e in _stored(config_path) if e["name"] == GENERIC_AGENT)
    assert entry["model"] == "openrouter/gpt-5"


@pytest.fixture
def store_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The capability store the handlers read, instead of the real ``~/.raven``."""
    path = tmp_path / "caps.json"
    monkeypatch.setattr("raven.acp_client.capabilities.default_snapshot_path", lambda: path)
    return path


def _acp_row_with_snapshot(
    config_path: Path, store_path: Path, name: str, *, agent_name: str, menu: tuple = ()
) -> None:
    """Add an acp row and record the handshake it would have measured.

    Written through the real store under the real fingerprint rather than by
    stubbing ``agent_meta``: the rule under test reads the snapshot the way the
    roster reads it, and a stub would take that reading out of the test.
    """
    from raven.acp_client.capabilities import AcpModelChoice, CapabilitySnapshot, SnapshotStore, snapshot_fingerprint
    from raven.config.schema import SubagentsConfig

    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append(
        {"name": name, "kind": "acp", "command": f"{name.lower()} acp", "description": "d", "enabled": True}
    )
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    cfg = next(c for c in SubagentsConfig(agents=raw["subagents"]["agents"]).agents if c.name == name)
    SnapshotStore(path=store_path).record(
        CapabilitySnapshot(
            agent=name,
            fingerprint=snapshot_fingerprint(cfg),
            status="ready",
            detail="",
            measured_at_ms=1,
            agent_name=agent_name,
            model_choices=tuple(AcpModelChoice(value=v, name=v, group="V") for v in menu),
        )
    )


async def test_update_lets_one_of_ravens_own_acp_rows_with_no_menu_pick_from_ravens_providers(
    config_path: Path, store_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A product raven installed beside itself inherits raven's providers, so a
    handshake that advertised nothing leaves the row on raven's own catalogue --
    the alternative is a row nothing can ever set a model on."""
    _gate_answers(monkeypatch)
    _with_providers(config_path, {"openai": {"apiKey": "sk-test"}})
    _acp_row_with_snapshot(config_path, store_path, "Raven-PPT", agent_name="raven")

    await subagents_update({"name": "Raven-PPT", "model": "gpt-5", "provider": "openai"})

    entry = next(e for e in _stored(config_path) if e["name"] == "Raven-PPT")
    assert entry["model"] == "openai/gpt-5"


async def test_update_refuses_an_own_acp_row_a_model_no_provider_of_ravens_serves(
    config_path: Path, store_path: Path
) -> None:
    """Falling back to raven's catalogue means its refusal too: the row is
    checked against the pairing the dispatch would make, not against a menu."""
    _acp_row_with_snapshot(config_path, store_path, "Raven-PPT", agent_name="raven")

    with pytest.raises(ConfigValidationError, match="runs on raven's own providers"):
        await subagents_update({"name": "Raven-PPT", "model": "nonsense-model-xyz"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Raven-PPT")
    assert entry.get("model") is None


async def test_update_takes_either_vocabulary_for_an_own_acp_row_that_did_advertise_a_menu(
    config_path: Path, store_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One of raven's own runs on this host's providers whatever it advertised,
    so both vocabularies land: the menu is what the page draws, not a gate. It
    is also what keeps a pick honest across a re-measure -- the listing a reader
    picked from is one rule behind the moment the boot backfill writes a menu.
    """
    _gate_answers(monkeypatch)
    _with_providers(config_path, {"openai": {"apiKey": "sk-test"}})
    _acp_row_with_snapshot(config_path, store_path, "Raven-Code", agent_name="raven", menu=("vendor/a",))

    await subagents_update({"name": "Raven-Code", "model": "gpt-5", "provider": "openai"})
    assert next(e for e in _stored(config_path) if e["name"] == "Raven-Code")["model"] == "openai/gpt-5"

    await subagents_update({"name": "Raven-Code", "model": "vendor/a"})
    assert next(e for e in _stored(config_path) if e["name"] == "Raven-Code")["model"] == "vendor/a"


async def test_update_refuses_an_own_acp_row_an_id_neither_vocabulary_holds(
    config_path: Path, store_path: Path
) -> None:
    """Taking both means naming both: a reader of the refusal is looking at one
    of the two menus and has to be told the other one turned the id down too."""
    _with_providers(config_path, {"openai": {"apiKey": "sk-test"}})
    _acp_row_with_snapshot(config_path, store_path, "Raven-Code", agent_name="raven", menu=("vendor/a",))

    with pytest.raises(ConfigValidationError, match="none of the 1 its handshake advertised"):
        await subagents_update({"name": "Raven-Code", "model": "nonsense/xyz"})
    with pytest.raises(ConfigValidationError, match="runs on raven's own providers"):
        await subagents_update({"name": "Raven-Code", "model": "nonsense/xyz"})
    assert next(e for e in _stored(config_path) if e["name"] == "Raven-Code").get("model") is None


async def test_update_refuses_a_host_id_for_a_third_party_acp_row_with_a_menu(
    config_path: Path, store_path: Path
) -> None:
    """The second vocabulary is ownership's, not every acp row's: a third party
    runs on its own credentials, and raven's ids would be refused by the agent
    itself once the dispatch got there."""
    _with_providers(config_path, {"openai": {"apiKey": "sk-test"}})
    _acp_row_with_snapshot(config_path, store_path, "Outsider", agent_name="other-agent", menu=("vendor/a",))

    with pytest.raises(ConfigValidationError, match="offers 1"):
        await subagents_update({"name": "Outsider", "model": "gpt-5", "provider": "openai"})
    assert next(e for e in _stored(config_path) if e["name"] == "Outsider").get("model") is None


async def test_update_edits_a_builtin_override_stored_under_the_legacy_spelling_in_place(config_path: Path) -> None:
    """An install that wrote its override before the generic row was renamed
    holds it as ``raven``; the page addresses the merged row as ``Raven``. The
    edit has to land on that row -- a second one would be dropped by the merge,
    taking the reader's model with it."""
    _with_providers(config_path, {"openai": {"apiKey": "sk-test"}})
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append({"name": "raven", "kind": "builtin", "skills": ["one", "two"]})
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    await subagents_update({"name": GENERIC_AGENT, "model": "openai/gpt-5"})

    builtin = [e for e in _stored(config_path) if e.get("kind") == "builtin"]
    assert [e["name"] for e in builtin] == ["raven"]
    assert builtin[0]["skills"] == ["one", "two"]
    assert builtin[0]["model"] == "openai/gpt-5"


async def test_update_edits_a_legacy_spelled_acp_transport_override_in_place(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An acp row of a seed's name is the supported transport switch, and the
    table lists it under the canonical name; the edit must land on it, by its
    own rule (its advertised menu), not materialize a builtin row beside it that
    the merge would then drop the transport for."""
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append({"name": "raven", "kind": "acp", "command": ""})
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(("vendor/a",)))

    await subagents_update({"name": GENERIC_AGENT, "model": "vendor/a"})

    of_name = [e for e in _stored(config_path) if e["name"].lower() == "raven"]
    assert [(e["name"], e["kind"], e.get("model")) for e in of_name] == [("raven", "acp", "vendor/a")]


async def test_update_refuses_a_builtin_name_config_holds_an_ignored_row_for(config_path: Path) -> None:
    """A cli row of a seed's name is one the table ignores and the list leaves
    out, showing the seed instead. Editing the seed would write a second row of
    the name; editing the cli row would change nothing anyone sees. Refused,
    naming the row to remove."""
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append({"name": GENERIC_AGENT, "kind": "cli", "command": "echo {prompt}"})
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigFieldReadonlyError, match="'cli' row of that name"):
        await subagents_update({"name": GENERIC_AGENT, "model": "openai/gpt-5"})
    assert [e["kind"] for e in _stored(config_path) if e["name"] == GENERIC_AGENT] == ["cli"]


async def test_update_refuses_to_rename_a_materialized_builtin_override(config_path: Path) -> None:
    """The row is bound to the seed by name; renamed, it would be a second
    built-in agent with nothing behind it."""
    _with_providers(config_path, {"openai": {"apiKey": "sk-test"}})
    with pytest.raises(ConfigFieldReadonlyError):
        await subagents_update({"name": GENERIC_AGENT, "new_name": "Scribe", "model": "openai/gpt-5"})
    assert not any(e.get("kind") == "builtin" for e in _stored(config_path))


async def test_update_clear_model_wins_over_a_model_sent_beside_it(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate_answers(monkeypatch)
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _fake_agent_meta(("vendor/a",)))
    await subagents_update({"name": "Hermes Agent", "model": "vendor/a"})
    await subagents_update({"name": "Hermes Agent", "model": "vendor/a", "clear_model": True})
    entry = next(e for e in _stored(config_path) if e["name"] == "Hermes Agent")
    assert entry.get("model") is None


async def test_update_refuses_to_clear_the_model_of_a_row_with_no_menu(config_path: Path) -> None:
    """An openai row's model is a required config value and a cli row has none:
    clearing meets the same refusal setting does, not a validation error from a
    field nulled underneath the schema."""
    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append(
        {"name": "Researcher", "kind": "openai", "baseUrl": "https://api.example.test/v1", "model": "m", "apiKey": "k"}
    )
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigFieldReadonlyError):
        await subagents_update({"name": "Researcher", "clear_model": True})
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["model"] == "m"


async def test_update_clearing_the_description_with_explicit_null_reverts_to_the_preset_default(
    config_path: Path,
) -> None:
    """``description: null`` (an explicit key, not merely absent) must reset to
    the factory text on the same terms a blank string already does."""
    from raven.agent.subagent.presets import third_party_subagent_preset

    out = await subagents_update({"name": "Hermes Agent", "description": None})
    assert out == {"updated": True, "name": "Hermes Agent"}
    entry = next(e for e in _stored(config_path) if e["name"] == "Hermes Agent")
    assert entry["description"] == third_party_subagent_preset("hermes")["description"]
    assert entry["description"] != "custom desc"


async def test_update_clearing_the_description_with_explicit_null_puts_a_builtin_override_back_on_the_seed(
    config_path: Path,
) -> None:
    """A built-in row's ``description: null`` clears the override rather than
    copying today's seed text in: ``""`` is the schema's own "the seed's", so
    the row reads the package text now and keeps following it when a release
    changes it -- a copy would have frozen this one."""
    from raven.agent.subagent.builtin_agents import builtin_agent_seeds

    raw = json.loads(config_path.read_text())
    raw["subagents"]["agents"].append(
        {"name": GENERIC_AGENT, "kind": "builtin", "description": "a retuned blurb", "enabled": True}
    )
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    await subagents_update({"name": GENERIC_AGENT, "description": None})

    entry = next(e for e in _stored(config_path) if e["name"] == GENERIC_AGENT)
    assert entry["description"] == ""
    seed = next(s for s in builtin_agent_seeds() if s.name == GENERIC_AGENT)
    listed = next(r for r in (await subagents_list({"probe": False}))["rows"] if r["name"] == GENERIC_AGENT)
    assert listed["description"] == seed.description


async def test_a_model_pick_on_a_row_never_measured_measures_it_first(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seen live: a connected agent never tested was told
    "it offers none", and the caller launched a whole run to get a menu recorded.
    Never measured is not an empty menu; the handshake that holds it spends nothing."""
    measured = SimpleNamespace(agent_name="hermes")
    handshakes: list[object] = []

    async def _handshake(cfg: object) -> object:
        handshakes.append(cfg)
        return measured

    def _meta(cfg, *, snapshot=None):
        menu = ("vendor/a", "vendor/b") if snapshot is measured else ()
        return SimpleNamespace(model_choices=tuple(SimpleNamespace(value=v) for v in menu))

    _gate_answers(monkeypatch)
    monkeypatch.setattr("raven.rpc.methods.subagents.record_capabilities", _handshake)
    monkeypatch.setattr("raven.rpc.methods.subagents.acp_snapshot_for", lambda cfg: None)
    monkeypatch.setattr("raven.rpc.methods.subagents.agent_meta", _meta)

    await subagents_update({"name": "Hermes Agent", "model": "vendor/b"})

    assert handshakes, "the menu was measured rather than taken as empty"
    assert next(e for e in _stored(config_path) if e["name"] == "Hermes Agent")["model"] == "vendor/b"
