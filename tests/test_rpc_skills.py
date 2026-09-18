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
        "body": "",
        "files": [],
        "always": False,
        "hub": False,
        "hub_id": "",
        "install": None,
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


# ---------------------------------------------------------------------------
# inspect carries the detail page's fields; open stays inside the skill
# ---------------------------------------------------------------------------


def _real_skill(tmp_path: Path, name: str, *, hub: bool = False, meta: dict | None = None) -> _Meta:
    import json

    d = tmp_path / name
    d.mkdir()
    (d / "SKILL.md").write_text(f"# {name}\n\nBody.\n", encoding="utf-8")
    (d / "helper.py").write_text("print(1)\n", encoding="utf-8")
    if hub:
        (d / ".skillhub.json").write_text(json.dumps({"id": f"hub/{name}@v2"}), encoding="utf-8")
    if meta is not None:
        (d / ".install-meta.json").write_text(json.dumps(meta), encoding="utf-8")
    m = _Meta(name, f"{name} does things", "workspace", str(d / "SKILL.md"))
    m.always = name == "always_one"
    return m


async def test_inspect_carries_body_files_and_install_meta(tmp_path: Path) -> None:
    stamp = {
        "slug": "weather",
        "version": "v2",
        "source": "hub",
        "trigger": "use_skill",
        "score_safety": 0.9,
        "installed_at": "2026-09-01T00:00:00+00:00",
    }
    meta = _real_skill(tmp_path, "weather", hub=True, meta=stamp)
    info = (
        await skills_manage({"action": "inspect", "query": "weather"}, agent_loop_factory=_factory(_Registry([meta])))
    )["info"]
    assert info["body"].startswith("# weather")
    assert info["files"] == ["SKILL.md", "helper.py"]
    assert (info["hub"], info["hub_id"]) == (True, "hub/weather@v2")
    assert info["install"] == {
        "installed_at": "2026-09-01T00:00:00+00:00",
        "version": "v2",
        "trigger": "use_skill",
        "source": "hub",
        "score_safety": 0.9,
    }
    assert info["always"] is False


async def test_inspect_without_a_stamp_reports_null_install_and_always(tmp_path: Path) -> None:
    meta = _real_skill(tmp_path, "always_one")
    info = (
        await skills_manage(
            {"action": "inspect", "query": "always_one"}, agent_loop_factory=_factory(_Registry([meta]))
        )
    )["info"]
    assert info["install"] is None
    assert info["hub"] is False
    assert info["always"] is True


async def test_open_launches_a_file_inside_the_skill_and_refuses_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.rpc.errors import ConfigValidationError
    from raven.rpc.methods import console as console_module

    meta = _real_skill(tmp_path, "codeword")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    opened: list[Path] = []
    monkeypatch.setattr(console_module, "_open_with_system", lambda target, app="": opened.append(target))
    factory = _factory(_Registry([meta]))

    assert await skills_manage(
        {"action": "open", "query": "codeword", "file": "helper.py"}, agent_loop_factory=factory
    ) == {"opened": True}
    assert opened == [(tmp_path / "codeword" / "helper.py").resolve()]
    with pytest.raises(ConfigValidationError):
        await skills_manage(
            {"action": "open", "query": "codeword", "file": "../config.json"}, agent_loop_factory=factory
        )
    with pytest.raises(ConfigValidationError):
        await skills_manage({"action": "open", "query": "codeword", "file": "missing.txt"}, agent_loop_factory=factory)
    assert len(opened) == 1


def test_hub_marker_and_install_meta_tolerate_a_corrupt_stamp(tmp_path: Path) -> None:
    from raven.rpc.methods.skills import _hub_marker, _install_meta
    from raven.skill_hub.hub import MARKER

    skill = tmp_path / "corrupt"
    skill.mkdir()
    assert _hub_marker(skill) == (False, "")
    assert _install_meta(skill) is None
    (skill / MARKER).write_text("{not json", encoding="utf-8")
    (skill / ".install-meta.json").write_text("{not json", encoding="utf-8")
    assert _hub_marker(skill) == (True, "")
    assert _install_meta(skill) is None


async def test_open_of_an_unknown_skill_is_a_typed_error() -> None:
    from raven.rpc.errors import ConfigValidationError

    with pytest.raises(ConfigValidationError, match="unknown skill"):
        await skills_manage(
            {"action": "open", "query": "nobody", "file": "SKILL.md"}, agent_loop_factory=_factory(_Registry([]))
        )
