"""Tests for ``skills.manage`` -- the ``/skills`` slash surface and the skills hub.

The split that matters: ``list`` / ``inspect`` are local-registry reads, while
``browse`` / ``install`` need a configured hub. ``search`` spans both and must
still answer from the local half when the hub is unreachable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.rpc.errors import InternalError
from raven.rpc.methods.skills import skills_manage


class _Meta:
    def __init__(self, name: str, description: str, source: str, path: str) -> None:
        self.name = name
        self.description = description
        self.source = source
        self.path = Path(path)


class _Registry:
    def __init__(self, metas: list[_Meta]) -> None:
        self._metas = metas
        self.invalidated = 0

    def list_all(self) -> list[_Meta]:
        return list(self._metas)

    def get(self, name: str) -> _Meta | None:
        return next((m for m in self._metas if m.name == name), None)

    def invalidate_cache(self) -> None:
        self.invalidated += 1


class _Hub:
    def __init__(self, items: list[dict] | None = None, raises: Exception | None = None) -> None:
        self._items = items or []
        self._raises = raises
        self.installed: list[str] = []

    async def search(self, q: str, limit: int = 20) -> list[dict]:
        if self._raises is not None:
            raise self._raises
        return self._items[:limit]

    async def install(self, skill_id: str) -> dict:
        if self._raises is not None:
            raise self._raises
        self.installed.append(skill_id)
        return {"slug": skill_id, "version": "v1", "dir": "/tmp/x"}


_METAS = [
    _Meta("deploy", "ship a release", "workspace", "/w/deploy/SKILL.md"),
    _Meta("triage", "sort incoming bugs", "builtin", "/b/triage/SKILL.md"),
    _Meta("archive", "box up old runs", "builtin", "/b/archive/SKILL.md"),
]


def _factory(registry: object | None = None, hub: object | None = None):
    skills = type("_S", (), {"registry": registry})()
    context = type("_C", (), {"skills": skills})()
    loop = type("_Loop", (), {"context": context, "_skill_hub_client": hub})()
    return lambda: loop


async def test_list_groups_names_by_origin() -> None:
    result = await skills_manage({"action": "list"}, agent_loop_factory=_factory(_Registry(_METAS)))
    assert result == {"skills": {"builtin": ["archive", "triage"], "workspace": ["deploy"]}}


async def test_inspect_returns_the_fields_the_panel_renders() -> None:
    result = await skills_manage(
        {"action": "inspect", "query": "deploy"},
        agent_loop_factory=_factory(_Registry(_METAS)),
    )
    assert result["info"] == {
        "name": "deploy",
        "description": "ship a release",
        "category": "workspace",
        "path": "/w/deploy/SKILL.md",
    }


async def test_inspect_tolerates_the_wrong_case() -> None:
    result = await skills_manage(
        {"action": "inspect", "query": "DePloY"},
        agent_loop_factory=_factory(_Registry(_METAS)),
    )
    assert result["info"]["name"] == "deploy"


async def test_inspect_of_an_unknown_skill_returns_empty_info_not_an_error() -> None:
    result = await skills_manage(
        {"action": "inspect", "query": "nope"},
        agent_loop_factory=_factory(_Registry(_METAS)),
    )
    assert result == {"info": {}}


async def test_search_matches_name_and_description() -> None:
    factory = _factory(_Registry(_METAS))
    by_name = await skills_manage({"action": "search", "query": "arch"}, agent_loop_factory=factory)
    by_desc = await skills_manage({"action": "search", "query": "bugs"}, agent_loop_factory=factory)
    assert [r["name"] for r in by_name["results"]] == ["archive"]
    assert [r["name"] for r in by_desc["results"]] == ["triage"]


async def test_search_adds_hub_hits_that_are_not_installed() -> None:
    hub = _Hub([{"slug": "deploy", "description": "dup"}, {"slug": "canary", "description": "staged rollout"}])
    result = await skills_manage(
        {"action": "search", "query": "deploy"},
        agent_loop_factory=_factory(_Registry(_METAS), hub),
    )
    names = [r["name"] for r in result["results"]]
    assert names.count("deploy") == 1, "an installed skill must not be listed twice"
    assert "canary" in names


async def test_a_dead_hub_does_not_take_the_local_results_with_it() -> None:
    hub = _Hub(raises=RuntimeError("connection refused"))
    result = await skills_manage(
        {"action": "search", "query": "deploy"},
        agent_loop_factory=_factory(_Registry(_METAS), hub),
    )
    assert [r["name"] for r in result["results"]] == ["deploy"]


async def test_browse_pages_the_catalogue() -> None:
    hub = _Hub([{"slug": f"skill-{i}", "description": str(i)} for i in range(45)])
    factory = _factory(_Registry(_METAS), hub)

    first = await skills_manage({"action": "browse", "page": 1}, agent_loop_factory=factory)
    assert first["page"] == 1
    assert first["total"] == 45
    assert first["total_pages"] == 3
    assert len(first["items"]) == 20
    assert first["items"][0]["name"] == "skill-0"

    last = await skills_manage({"action": "browse", "page": 3}, agent_loop_factory=factory)
    assert len(last["items"]) == 5


async def test_browse_clamps_a_page_past_the_end() -> None:
    hub = _Hub([{"slug": "only", "description": ""}])
    result = await skills_manage(
        {"action": "browse", "page": 99},
        agent_loop_factory=_factory(_Registry(_METAS), hub),
    )
    assert result["page"] == 1


async def test_browse_without_a_hub_says_so() -> None:
    with pytest.raises(InternalError, match="hub"):
        await skills_manage({"action": "browse"}, agent_loop_factory=_factory(_Registry(_METAS)))


async def test_install_drops_the_registry_cache_so_the_skill_is_loadable() -> None:
    registry = _Registry(_METAS)
    hub = _Hub()
    result = await skills_manage(
        {"action": "install", "query": "canary"},
        agent_loop_factory=_factory(registry, hub),
    )
    assert result == {"installed": True, "name": "canary"}
    assert hub.installed == ["canary"]
    assert registry.invalidated == 1


async def test_an_unknown_action_is_a_typed_error() -> None:
    with pytest.raises(InternalError, match="unknown skills action"):
        await skills_manage({"action": "explode"}, agent_loop_factory=_factory(_Registry(_METAS)))


async def test_no_live_registry_is_a_typed_error() -> None:
    with pytest.raises(InternalError, match="registry"):
        await skills_manage({"action": "list"}, agent_loop_factory=_factory(None))
