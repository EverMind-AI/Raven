"""Robot RPC contracts, profile isolation, and refusal of device control."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods import robots
from raven.rpc.models import METHOD_MODELS, RobotDraft


def draft(**changes):
    return {"domain": "personal", "confirm_personal": True, "name": "Fixture robot", "type": "sparkbot", **changes}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(robots, "get_config_path", lambda: tmp_path / "config.json")
    return tmp_path


def checked(method, result):
    METHOD_MODELS[method][1].model_validate(result)
    schema = json.loads((Path(__file__).parents[1] / "rpc-schema/openrpc.json").read_text())
    wire = next(item["result"]["schema"] for item in schema["methods"] if item["name"] == method)
    jsonschema.validate(result, {"components": schema["components"], **wire})
    return result


@pytest.mark.asyncio
async def test_rpc_crud_persistence_and_recoverable_removal(home):
    row = checked("robots.save", await robots.robots_save({"device": draft()}))["device"]
    assert row["status"] == "unknown"
    updated = await robots.robots_save(
        {"device": draft(name="Edited fixture"), "id": row["id"], "revision": row["revision"]}
    )
    checked("robots.save", updated)
    with pytest.raises(ConfigValidationError, match="edited by another client"):
        await robots.robots_save({"device": draft(), "id": row["id"], "revision": row["revision"]})
    checked("robots.remove", await robots.robots_remove({"id": row["id"]}))
    assert checked("robots.list", await robots.robots_list({}))["devices"] == []
    trash = checked("robots.list", await robots.robots_list({"trash": True}))["devices"]
    assert trash[0]["name"] == "Edited fixture"
    restored = checked("robots.restore", await robots.robots_restore({"id": row["id"]}))["device"]
    assert restored["observation"] is None
    assert restored["name"] == "Edited fixture"
    assert len(robots._store().list()) == 1


@pytest.mark.asyncio
async def test_metadata_check_and_invalid_inputs_never_probe(home, monkeypatch):
    calls = []

    async def forbidden_probe(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Unexpected network access")

    monkeypatch.setattr(robots, "probe_status", forbidden_probe)
    row = (await robots.robots_save({"device": draft(type="agv")}))["device"]
    with pytest.raises(ConfigValidationError, match="Metadata-only"):
        await robots.robots_check({"id": row["id"]})
    for change in (
        {"domain": "company"},
        {"confirm_personal": False},
        {"confirm_personal": "true"},
        {"command": "move"},
        {"api_key": "fixture"},
        {"status_url": "http://127.0.0.1:8080/health", "connection_mode": "http_status"},
    ):
        with pytest.raises(ConfigValidationError):
            await robots.robots_save({"device": draft(**change)})
    with pytest.raises(ConfigValidationError):
        await robots.robots_check({"id": "not-an-id"})
    assert not calls
    assert len(robots._store().list()) == 1


@pytest.mark.asyncio
async def test_missing_records_and_foreign_stored_domains_are_refused(home):
    for operation in [robots.robots_remove, robots.robots_restore]:
        with pytest.raises(ConfigValidationError):
            await operation({"id": "0" * 32})
    row = (await robots.robots_save({"device": draft()}))["device"]
    with robots._store().connect() as db:
        db.execute("UPDATE devices SET data=? WHERE id=?", (json.dumps({"domain": "company"}), row["id"]))
    with pytest.raises(ConfigValidationError, match="security domain"):
        await robots.robots_list({})


@pytest.mark.asyncio
async def test_explicit_check_uses_registered_address_and_stale_result_is_refused(home, monkeypatch):
    row = (
        await robots.robots_save(
            {"device": draft(connection_mode="http_status", status_url="http://10.0.0.2:8080/health")}
        )
    )["device"]
    observation = {
        "status": "not_connected",
        "checked_at": "2026-01-01T00:00:00+00:00",
        "checked_epoch": 0.0,
        "http_status": None,
        "detail": "Fixture disconnected",
        "observed": {},
    }
    calls = []

    async def probe(device):
        calls.append(device["status_url"])
        return observation

    monkeypatch.setattr(robots, "probe_status", probe)
    result = checked("robots.check", await robots.robots_check({"id": row["id"]}))
    assert calls == ["http://10.0.0.2:8080/health"]
    assert result["device"]["status"] == "unknown"

    async def overlapping_remove(device):
        robots._store().delete(device["id"])
        return observation

    monkeypatch.setattr(robots, "probe_status", overlapping_remove)
    with pytest.raises(ConfigValidationError, match="stale result"):
        await robots.robots_check({"id": row["id"]})
    assert not robots._store().list()


@pytest.mark.asyncio
async def test_config_profiles_are_separate_and_have_no_motion_method(home, monkeypatch):
    await robots.robots_save({"device": draft()})
    monkeypatch.setattr(robots, "get_config_path", lambda: home / "other.json")
    assert (await robots.robots_list({}))["devices"] == []
    dispatcher = Dispatcher()
    robots.register_robots_methods(dispatcher)
    assert set(dispatcher.methods()) == {
        "robots.list",
        "robots.save",
        "robots.check",
        "robots.remove",
        "robots.restore",
    }
    reply = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": "robots.move", "params": {}})
    assert reply["error"]["code"] == -32601


def test_draft_does_not_accept_credentials():
    with pytest.raises(ValidationError):
        RobotDraft.model_validate(draft(password="fixture"))
