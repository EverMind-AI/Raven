"""``skillhub.*`` -- the RPC face of :mod:`raven.skill_hub.hub`.

Parses the wire params, calls the engine, and translates its two failure
kinds into this surface's errors: a rejected request is a
``ConfigValidationError``, an engine or hub failure an ``InternalError``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from raven.rpc.errors import ConfigValidationError, InternalError
from raven.rpc.models import (
    SkillhubDetailParams,
    SkillhubInstallParams,
    SkillhubRemoveParams,
    SkillhubSearchParams,
)
from raven.skill_hub import hub
from raven.skill_hub.hub import SkillHubFailed, SkillHubRejected

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


def _parse(model_cls: type, params: dict) -> Any:
    try:
        return model_cls.model_validate(params)
    except ValidationError as exc:
        raise ConfigValidationError(
            f"invalid params for {model_cls.__name__}",
            data={"errors": exc.errors(include_url=False)},
        ) from exc


@contextmanager
def _translated():
    try:
        yield
    except SkillHubRejected as e:
        raise ConfigValidationError(str(e), data=e.data or None) from e
    except SkillHubFailed as e:
        raise InternalError(str(e), data=e.data or None) from e


async def skillhub_search(params: dict) -> dict:
    p = _parse(SkillhubSearchParams, params)
    with _translated():
        return await hub.search(
            text=p.query, category=p.category, tags=p.tags, min_score=p.min_score, page=p.page, limit=p.limit
        )


async def skillhub_detail(params: dict) -> dict:
    p = _parse(SkillhubDetailParams, params)
    with _translated():
        return await hub.detail(p.id)


async def skillhub_install(params: dict, *, agent_loop_factory=None, if_absent: bool = False) -> dict:
    """``if_absent`` is the plugin transaction's flag: see :func:`raven.skill_hub.hub.install`."""
    p = _parse(SkillhubInstallParams, params)
    with _translated():
        return await hub.install(p.id, agent_loop_factory=agent_loop_factory, if_absent=if_absent)


async def skillhub_remove(params: dict, *, agent_loop_factory=None) -> dict:
    p = _parse(SkillhubRemoveParams, params)
    with _translated():
        return await hub.remove(p.name, agent_loop_factory=agent_loop_factory)


def register_skillhub_methods(dispatcher: "Dispatcher", *, agent_loop_factory=None) -> None:
    """Register the four ``skillhub.*`` handlers on a dispatcher instance."""

    def bind(fn):
        async def _h(params: dict) -> dict:
            return await fn(params, agent_loop_factory=agent_loop_factory)

        return _h

    dispatcher.register("skillhub.search", skillhub_search)
    dispatcher.register("skillhub.detail", skillhub_detail)
    dispatcher.register("skillhub.install", bind(skillhub_install))
    dispatcher.register("skillhub.remove", bind(skillhub_remove))


__all__ = [
    "skillhub_search",
    "skillhub_detail",
    "skillhub_install",
    "skillhub_remove",
    "register_skillhub_methods",
]
