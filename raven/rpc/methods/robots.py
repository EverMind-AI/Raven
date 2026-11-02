"""Personal robot registration, separate from the Machine registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from raven.home import get_config_path
from raven.ops.robots import RegistryError, RobotStore, probe_status
from raven.rpc.errors import ConfigValidationError
from raven.rpc.models import RobotsIdParams, RobotsListParams, RobotsSaveParams

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


def _store() -> RobotStore:
    config = get_config_path().resolve()
    return RobotStore(config.parent / "robots" / config.stem / "personal")


def _parse(model, params):
    try:
        return model.model_validate(params)
    except ValidationError:
        raise ConfigValidationError("Invalid robot registration parameters") from None


async def robots_list(params: dict) -> dict:
    request = _parse(RobotsListParams, params)
    try:
        return {"devices": _store().list(trash=request.trash), "status_ttl_seconds": 60}
    except RegistryError as exc:
        raise ConfigValidationError(str(exc)) from None


async def robots_save(params: dict) -> dict:
    request = _parse(RobotsSaveParams, params)
    try:
        payload = request.device.model_dump()
        payload["revision"] = request.revision
        return {"device": _store().save(payload, request.id)}
    except RegistryError as exc:
        raise ConfigValidationError(str(exc)) from None


async def robots_remove(params: dict) -> dict:
    request = _parse(RobotsIdParams, params)
    try:
        _store().delete(request.id)
        return {"ok": True}
    except RegistryError as exc:
        raise ConfigValidationError(str(exc)) from None


async def robots_restore(params: dict) -> dict:
    request = _parse(RobotsIdParams, params)
    try:
        return {"device": _store().restore(request.id)}
    except RegistryError as exc:
        raise ConfigValidationError(str(exc)) from None


async def robots_check(params: dict) -> dict:
    request = _parse(RobotsIdParams, params)
    try:
        store = _store()
        device = store.get(request.id)
        if device["connection_mode"] != "http_status":
            raise RegistryError("Metadata-only robots cannot be connected or controlled")
        observation = await probe_status(device)
        return {"device": store.observe(device["id"], device["revision"], observation)}
    except RegistryError as exc:
        raise ConfigValidationError(str(exc)) from None


def register_robots_methods(dispatcher: Dispatcher) -> None:
    dispatcher.register("robots.list", robots_list)
    dispatcher.register("robots.save", robots_save)
    dispatcher.register("robots.remove", robots_remove)
    dispatcher.register("robots.restore", robots_restore)
    dispatcher.register("robots.check", robots_check)
