"""Unit tests for the read_skill / use_skill agent tools."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from raven.agent.tools.skill_hub import (
    ReadSkillTool,
    UseSkillTool,
    _split_qualified_id,
)


class _FakeRegistry:
    """Minimal SkillRegistry stand-in, keyed the way the real one is.

    Entries are keyed by *physical layer* — ``workspace`` / ``builtin`` /
    ``external`` / ``everos`` — never by the ``local`` router namespace, which
    SkillRegistry does not know. ``source=None`` resolves to the layer-priority
    winner. Keying a fixture under ``("local", name)`` would make a broken
    namespace passthrough look green.
    """

    _LAYER_PRIORITY = ("workspace", "external", "builtin", "everos")

    def __init__(self, metas: dict[tuple[str, str], object]) -> None:
        assert not any(layer == "local" for layer, _ in metas), "'local' is a router namespace, not a registry layer"
        self._metas = metas
        self.invalidated: list[str] = []

    def get(self, name: str, source: str | None = None):
        if source is not None:
            return self._metas.get((source, name))
        candidates = [(layer, m) for (layer, key), m in self._metas.items() if key == name]
        if not candidates:
            return None
        candidates.sort(key=lambda c: self._LAYER_PRIORITY.index(c[0]))
        return candidates[0][1]

    def invalidate_source(self, source: str) -> None:
        self.invalidated.append(source)


class _FakeClient:
    """SkillHubClient stand-in capturing calls and returning canned data."""

    def __init__(self, *, get_result=None, install_result=None, raises=False):
        self._get_result = get_result or {}
        self._install_result = install_result or {}
        self._raises = raises
        self.get_calls: list[str] = []
        self.install_calls: list[str] = []

    async def get(self, skill_id: str):
        self.get_calls.append(skill_id)
        if self._raises:
            raise RuntimeError("boom")
        return self._get_result

    async def install(self, skill_id: str):
        self.install_calls.append(skill_id)
        if self._raises:
            raise RuntimeError("boom")
        return self._install_result


def _meta(tmp: Path, name: str, content: str, *, with_scripts: bool) -> object:
    skill_dir = tmp / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    if with_scripts:
        (skill_dir / "scripts").mkdir(exist_ok=True)
    return SimpleNamespace(name=name, content=content, path=skill_dir / "SKILL.md")


class TestSplitQualifiedId:
    def test_source_prefixed(self) -> None:
        assert _split_qualified_id("hub/my-skill") == ("hub", "my-skill")
        assert _split_qualified_id("local/x") == ("local", "x")
        assert _split_qualified_id("everos/abc123") == ("everos", "abc123")

    def test_bare_id_assumed_hub(self) -> None:
        assert _split_qualified_id("abc123") == ("hub", "abc123")


class TestReadSkill:
    async def test_missing_skill_id_errors(self) -> None:
        assert (await ReadSkillTool().execute(skill_id=None)).startswith("Error")
        assert (await ReadSkillTool().execute(skill_id="")).startswith("Error")

    async def test_hub_body_fetched(self) -> None:
        client = _FakeClient(
            get_result={
                "skill_md": "do the thing",
                "name": "Foo",
                "version": "v2",
                "scenario_tags": ["a", "b"],
            }
        )
        out = await ReadSkillTool(client=client).execute(skill_id="hub/foo")
        assert client.get_calls == ["foo"]
        assert "Foo" in out and "v2" in out and "do the thing" in out
        assert "a, b" in out

    async def test_hub_without_client_errors(self) -> None:
        out = await ReadSkillTool(client=None).execute(skill_id="hub/foo")
        assert out.startswith("Error") and "not configured" in out

    async def test_hub_fetch_failure_is_isolated(self) -> None:
        client = _FakeClient(raises=True)
        out = await ReadSkillTool(client=client).execute(skill_id="hub/foo")
        assert out.startswith("Error") and "failed to read" in out

    async def test_local_body_from_registry(self, tmp_path: Path) -> None:
        meta = _meta(tmp_path, "x", "local body", with_scripts=False)
        reg = _FakeRegistry({("builtin", "x"): meta})
        out = await ReadSkillTool(registry=reg).execute(skill_id="local/x")
        assert "local body" in out

    async def test_local_missing_errors(self) -> None:
        out = await ReadSkillTool(registry=_FakeRegistry({})).execute(
            skill_id="local/nope",
        )
        assert out.startswith("Error")


class TestUseSkill:
    async def test_missing_skill_id_errors(self) -> None:
        assert (await UseSkillTool().execute(skill_id=None)).startswith("Error")

    async def test_unknown_source_errors(self) -> None:
        out = await UseSkillTool().execute(skill_id="weird/x")
        assert out.startswith("Error") and "unknown skill source" in out

    async def test_local_with_scripts(self, tmp_path: Path) -> None:
        meta = _meta(tmp_path, "x", "body here", with_scripts=True)
        reg = _FakeRegistry({("builtin", "x"): meta})
        out = await UseSkillTool(registry=reg).execute(skill_id="local/x")
        assert "scripts_dir:" in out
        assert str(tmp_path / "x" / "scripts") in out
        assert "body here" in out

    async def test_local_without_scripts_is_instruction_only(self, tmp_path: Path) -> None:
        meta = _meta(tmp_path, "x", "just instructions", with_scripts=False)
        reg = _FakeRegistry({("builtin", "x"): meta})
        out = await UseSkillTool(registry=reg).execute(skill_id="local/x")
        assert "no bundled scripts" in out
        assert "scripts_dir:" not in out

    async def test_everos_resolves_on_disk(self, tmp_path: Path) -> None:
        meta = _meta(tmp_path, "sql123", "evolved skill", with_scripts=True)
        reg = _FakeRegistry({("everos", "sql123"): meta})
        out = await UseSkillTool(registry=reg).execute(skill_id="everos/sql123")
        assert "scripts_dir:" in out and "evolved skill" in out

    async def test_local_missing_errors(self) -> None:
        out = await UseSkillTool(registry=_FakeRegistry({})).execute(
            skill_id="local/nope",
        )
        assert out.startswith("Error")

    async def test_local_id_reaches_a_builtin_via_the_real_registry(self, tmp_path: Path) -> None:
        """Pins the namespace translation against a real SkillRegistry.

        No fake can catch this one: the bug was handing ``source="local"``
        straight to ``SkillRegistry.get``, whose compound key only ever holds
        physical layers (workspace / builtin / external), so every ``local/*``
        id — the exact form the router prints in the ``# Skills`` brackets and
        both tool descriptions tell the agent to copy — resolved to None.
        """
        from raven.memory_engine.skill_local import SkillRegistry

        workspace = tmp_path / "ws"
        workspace.mkdir()
        builtin = tmp_path / "builtin" / "demo"
        builtin.mkdir(parents=True)
        (builtin / "SKILL.md").write_text(
            "---\nname: demo\ndescription: d\n---\n\nDEMO BODY\n",
            encoding="utf-8",
        )
        registry = SkillRegistry(workspace, builtin_skills_dir=tmp_path / "builtin")
        assert registry.get("demo").source == "builtin"  # never "local"

        for tool in (UseSkillTool(registry=registry), ReadSkillTool(registry=registry)):
            out = await tool.execute(skill_id="local/demo")
            assert not out.startswith("Error"), out
            assert "DEMO BODY" in out

    async def test_hub_install_and_registry_invalidation(self) -> None:
        client = _FakeClient(
            install_result={
                "slug": "foo",
                "version": "v1",
                "scripts_dir": "/cache/foo/scripts",
                "skill_md": "hub body",
            }
        )
        reg = _FakeRegistry({})
        out = await UseSkillTool(client=client, registry=reg).execute(
            skill_id="hub/foo",
        )
        assert client.install_calls == ["foo"]
        assert "scripts_dir: /cache/foo/scripts" in out
        assert "hub body" in out
        assert reg.invalidated == ["hub"]

    async def test_hub_without_scripts(self) -> None:
        client = _FakeClient(
            install_result={
                "slug": "foo",
                "version": "v1",
                "scripts_dir": None,
                "skill_md": "pure instructions",
            }
        )
        out = await UseSkillTool(client=client).execute(skill_id="hub/foo")
        assert "no bundled scripts" in out and "pure instructions" in out

    async def test_hub_without_client_errors(self) -> None:
        out = await UseSkillTool(client=None).execute(skill_id="hub/foo")
        assert out.startswith("Error") and "not configured" in out

    async def test_hub_install_failure_is_isolated(self) -> None:
        client = _FakeClient(raises=True)
        out = await UseSkillTool(client=client).execute(skill_id="hub/foo")
        assert out.startswith("Error") and "failed to install" in out
