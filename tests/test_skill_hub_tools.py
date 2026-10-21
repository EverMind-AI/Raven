"""Unit tests for the read_skill / use_skill agent tools."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.tools.skill_hub import (
    ReadSkillTool,
    UseSkillTool,
    split_qualified_id,
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

    def names(self) -> list[str]:
        return [name for _, name in self._metas]

    def layer_roots(self) -> list[tuple[Path, str]]:
        return []


class _FakeClient:
    """SkillHubClient stand-in capturing calls and returning canned data."""

    def __init__(self, *, get_result=None, install_result=None, raises=False):
        self._get_result = get_result or {}
        self._install_result = install_result or {}
        self._raises = raises
        self.get_calls: list[str] = []
        self.install_calls: list[str] = []
        self.install_meta: list[dict | None] = []

    async def get(self, skill_id: str):
        self.get_calls.append(skill_id)
        if self._raises:
            raise RuntimeError("boom")
        return self._get_result

    async def install(self, skill_id: str, *, prefetched_meta=None):
        self.install_calls.append(skill_id)
        self.install_meta.append(prefetched_meta)
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


def _registry_with(tmp_path: Path, *, workspace: Iterable[str] = (), builtin: Iterable[str] = ()):
    """A real SkillRegistry over a temp home, so a miss reads real layer roots."""
    from raven.memory_engine.skill_local import SkillRegistry

    home = tmp_path / "home"
    for root, names in ((home / "skills", workspace), (tmp_path / "builtin", builtin)):
        root.mkdir(parents=True, exist_ok=True)
        for name in names:
            (root / name).mkdir()
            (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\nbody\n", encoding="utf-8")
    return SkillRegistry(home, builtin_skills_dir=tmp_path / "builtin")


class TestSplitQualifiedId:
    def test_source_prefixed(self) -> None:
        assert split_qualified_id("hub/my-skill") == ("hub", "my-skill")
        assert split_qualified_id("local/x") == ("local", "x")
        assert split_qualified_id("everos/abc123") == ("everos", "abc123")

    def test_bare_id_assumed_hub(self) -> None:
        assert split_qualified_id("abc123") == ("hub", "abc123")


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

    async def test_a_local_body_names_its_directory_and_links_its_references_absolute(self, tmp_path: Path) -> None:
        """The skill menu sends the model here, and a body only says `references/x.md`.

        A mounted skill lives nowhere near the agent home, so a raw body left the
        model to guess its directory: deck runs on two models each searched the
        filesystem for the reference files, one reading another checkout's copy.
        The fetched body reads the way the injected one does.
        """
        from raven.memory_engine.skill_forge.catalog import render_skill_body

        body = "Read [layouts](references/layouts.md) before placing a page."
        meta = _meta(tmp_path, "deck", body, with_scripts=False)
        skill_dir = tmp_path / "deck"
        (skill_dir / "references").mkdir()
        (skill_dir / "references" / "layouts.md").write_text("shapes", encoding="utf-8")
        reg = _FakeRegistry({("builtin", "deck"): meta})

        out = await ReadSkillTool(registry=reg).execute(skill_id="local/deck")

        assert f"**Skill directory**: `{skill_dir}`" in out
        assert f"[layouts]({skill_dir}/references/layouts.md)" in out
        assert out == render_skill_body(meta), "one renderer for the fetched and the injected body"

    async def test_the_blocklist_is_read_per_call(self, tmp_path: Path) -> None:
        """Enabling a skill stops the refusal without restarting the loop.

        The tool's policy is built once when the loop wires its tools, so a
        screen reading the construction-time list keeps refusing a skill the
        operator has since switched back on -- with nothing in the product to
        undo it.
        """
        meta = _meta(tmp_path, "x", "local body", with_scripts=False)
        reg = _FakeRegistry({("builtin", "x"): meta})
        live: set[str] = {"x"}
        tool = ReadSkillTool(registry=reg, blocklist=["x"], blocklist_reader=lambda: frozenset(live))

        out = await tool.execute(skill_id="local/x")
        assert out.startswith("Error") and "blocklist" in out

        live.discard("x")
        assert "local body" in await tool.execute(skill_id="local/x")

        live.add("x")
        assert (await tool.execute(skill_id="local/x")).startswith("Error")

    async def test_local_missing_errors(self) -> None:
        out = await ReadSkillTool(registry=_FakeRegistry({})).execute(
            skill_id="local/nope",
        )
        assert out.startswith("Error")


@pytest.mark.parametrize("tool_cls", [ReadSkillTool, UseSkillTool])
class TestLocalMiss:
    """What a ``local/`` miss tells the model, from either tool.

    ``# Skills`` and ``# Active Skills`` render from the registry these tools
    read, so a local id it lacks has no body in context to fall back on. When
    another tool's skill folder was listed beside Raven's, the model tried each
    name there as ``local/<name>``, and every read failed until it opened the
    file itself.
    """

    async def test_it_names_the_directories_local_ids_come_from(self, tool_cls, tmp_path: Path) -> None:
        registry = _registry_with(tmp_path, workspace=["raven-dev-conventions"])
        out = await tool_cls(registry=registry).execute(skill_id="local/confirming-pr-review")
        assert out.startswith("Error: no local skill 'confirming-pr-review' found")
        assert str(tmp_path / "home" / "skills") in out
        assert str(tmp_path / "builtin") in out

    async def test_it_says_a_skill_file_outside_them_opens_with_read_file(self, tool_cls, tmp_path: Path) -> None:
        out = await tool_cls(registry=_registry_with(tmp_path)).execute(skill_id="local/confirming-pr-review")
        assert "read_file" in out

    async def test_it_does_not_send_the_model_to_the_skills_context(self, tool_cls, tmp_path: Path) -> None:
        out = await tool_cls(registry=_registry_with(tmp_path)).execute(skill_id="local/confirming-pr-review")
        assert "# Skills" not in out
        assert "already in your context" not in out

    async def test_it_offers_the_closest_local_ids(self, tool_cls, tmp_path: Path) -> None:
        registry = _registry_with(tmp_path, workspace=["raven-dev-conventions"], builtin=["weather"])
        out = await tool_cls(registry=registry).execute(skill_id="local/raven-dev-convention")
        assert "local/raven-dev-conventions" in out
        assert "local/weather" not in out

    async def test_it_offers_no_id_the_operator_blocked(self, tool_cls, tmp_path: Path) -> None:
        """The same tool refuses a blocklisted id, so offering one sends the
        model from one error straight into the next."""
        registry = _registry_with(tmp_path, workspace=["raven-dev-conventions"], builtin=["raven-dev-guide"])
        tool = tool_cls(registry=registry, blocklist=["Raven-Dev-Conventions"])
        out = await tool.execute(skill_id="local/raven-dev-convention")
        assert "local/raven-dev-guide" in out
        assert "local/raven-dev-conventions" not in out

    async def test_it_offers_at_most_three_ids(self, tool_cls, tmp_path: Path) -> None:
        registry = _registry_with(tmp_path, builtin=[f"deck-{i:03d}" for i in range(300)])
        out = await tool_cls(registry=registry).execute(skill_id="local/deck-0000")
        assert len(re.findall(r"local/deck-\d{3}\b", out)) == 3

    async def test_it_offers_no_ids_from_a_library_too_large_to_search_cheaply(self, tool_cls, tmp_path: Path) -> None:
        from raven.agent.tools.skill_hub import _NEAR_ID_SEARCH_LIMIT

        metas = {("builtin", f"deck-{i:05d}"): object() for i in range(_NEAR_ID_SEARCH_LIMIT + 1)}
        out = await tool_cls(registry=_FakeRegistry(metas)).execute(skill_id="local/deck-000000")
        assert out.startswith("Error: no local skill 'deck-000000' found")
        assert "Closest local ids" not in out
        assert "read_file" in out

    async def test_it_does_not_rescan_a_source_the_watcher_marked_dirty(
        self, tool_cls, tmp_path: Path, monkeypatch
    ) -> None:
        """The miss reads the index the lookup just used. A rebuild here ran
        inside the async tool call: about a second at 5000 skills."""
        registry = _registry_with(tmp_path, builtin=["weather"])
        assert registry.get("weather") is not None
        registry.invalidate_source("builtin")
        scanned: list[Path] = []
        walk = registry._iter_skill_dirs
        monkeypatch.setattr(registry, "_iter_skill_dirs", lambda root, **kw: scanned.append(root) or walk(root, **kw))
        out = await tool_cls(registry=registry).execute(skill_id="local/weathr")
        assert "local/weather" in out
        assert scanned == []


@pytest.mark.parametrize(
    ("tool_cls", "hint"),
    [(ReadSkillTool, "'# Skills' context"), (UseSkillTool, "already in your context")],
)
async def test_a_memory_backend_miss_still_points_at_the_injected_body(tool_cls, hint: str, tmp_path: Path) -> None:
    """The router injects a memory-backend hit with its body, and that skill
    need not be on disk, so for that source the context hint holds."""
    out = await tool_cls(registry=_registry_with(tmp_path)).execute(skill_id="everos/abc123")
    assert out.startswith("Error: no everos skill 'abc123' found")
    assert hint in out


class TestUseSkill:
    async def test_missing_skill_id_errors(self) -> None:
        assert (await UseSkillTool().execute(skill_id=None)).startswith("Error")

    async def test_unknown_source_falls_back_to_an_on_disk_lookup(self) -> None:
        """The on-disk source's name is whatever ``memory.backend`` says, so an
        unqualified prefix can no longer be checked against a hardcoded list:
        anything but ``hub`` is looked up on disk and misses there."""
        out = await UseSkillTool().execute(skill_id="weird/x")
        assert out.startswith("Error") and "no weird skill" in out

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

        workspace = tmp_path / "chanwork"
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


class TestUseSkillSafetyPolicy:
    """Install-time policy: min_safety and blocklist must gate use_skill."""

    async def test_low_safety_hub_skill_refused(self) -> None:
        client = _FakeClient(
            get_result={"score_safety": 0.2, "slug": "tag-memory", "skill_md": "evil"},
            install_result={"slug": "tag-memory", "version": "v1", "skill_md": "evil"},
        )
        out = await UseSkillTool(client=client, min_safety=0.7).execute(
            skill_id="hub/tag-memory",
        )
        assert out.startswith("Error")
        assert "score_safety" in out
        assert client.install_calls == []

    async def test_score_missing_allows_install(self) -> None:
        client = _FakeClient(
            get_result={"slug": "foo"},
            install_result={"slug": "foo", "version": "v1", "skill_md": "ok"},
        )
        out = await UseSkillTool(client=client, min_safety=0.7).execute(
            skill_id="hub/foo",
        )
        assert not out.startswith("Error")
        assert client.install_calls == ["foo"]

    async def test_blocklisted_hub_skill_refused_before_network(self) -> None:
        client = _FakeClient(get_result={"slug": "tag-memory"})
        out = await UseSkillTool(client=client, blocklist=["Tag-Memory"]).execute(
            skill_id="hub/tag-memory",
        )
        assert out.startswith("Error") and "blocklist" in out
        assert client.get_calls == []
        assert client.install_calls == []

    async def test_blocklisted_local_skill_refused(self, tmp_path: Path) -> None:
        meta = _meta(tmp_path, "x", "body", with_scripts=True)
        # Keyed under a real registry layer, not the "local" router namespace:
        # `local/x` is a namespace-qualified id the router strips, and a fixture
        # keyed under ("local", ...) would let a passthrough bug read as green.
        reg = _FakeRegistry({("workspace", "x"): meta})
        out = await UseSkillTool(registry=reg, blocklist=["x"]).execute(
            skill_id="local/x",
        )
        assert out.startswith("Error") and "blocklist" in out

    async def test_policy_pass_reuses_prefetched_meta(self) -> None:
        client = _FakeClient(
            get_result={"score_safety": 0.9, "slug": "foo"},
            install_result={"slug": "foo", "version": "v1", "skill_md": "ok"},
        )
        out = await UseSkillTool(client=client, min_safety=0.7).execute(
            skill_id="hub/foo",
        )
        assert not out.startswith("Error")
        assert client.get_calls == ["foo"]
        assert client.install_meta == [{"score_safety": 0.9, "slug": "foo"}]

    async def test_external_path_in_body_refused(self) -> None:
        client = _FakeClient(
            get_result={
                "score_safety": 0.9,
                "slug": "tag-memory",
                "skill_md": "python scripts/db.py --db ~/.openclaw/db.sqlite",
            },
        )
        out = await UseSkillTool(client=client).execute(skill_id="hub/tag-memory")
        assert out.startswith("Error") and "~/.openclaw" in out
        assert client.install_calls == []

    async def test_install_appends_audit_record(self, tmp_path: Path) -> None:
        import json

        audit = tmp_path / "installs.jsonl"
        client = _FakeClient(
            get_result={"score_safety": 0.9, "slug": "foo", "version": "v2"},
            install_result={"slug": "foo", "version": "v2", "skill_md": "ok", "dir": "/cache/foo"},
        )
        out = await UseSkillTool(
            client=client,
            min_safety=0.7,
            install_audit_path=audit,
        ).execute(skill_id="hub/foo")
        assert not out.startswith("Error")
        rec = json.loads(audit.read_text(encoding="utf-8").strip())
        assert rec["slug"] == "foo"
        assert rec["trigger"] == "use_skill"

    async def test_install_writes_install_meta(self, tmp_path: Path) -> None:
        import json

        skill_dir = tmp_path / "foo@v2"
        skill_dir.mkdir()
        client = _FakeClient(
            get_result={"score_safety": 0.9, "slug": "foo", "version": "v2"},
            install_result={"slug": "foo", "version": "v2", "skill_md": "ok", "dir": str(skill_dir)},
        )
        out = await UseSkillTool(client=client).execute(skill_id="hub/foo")
        assert not out.startswith("Error")
        rec = json.loads((skill_dir / ".install-meta.json").read_text(encoding="utf-8"))
        assert rec["slug"] == "foo"
        assert rec["version"] == "v2"
        assert rec["source"] == "hub"
        assert rec["trigger"] == "use_skill"
        assert rec["installed_at"]


class _FakeStdin:
    def __init__(self, *, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class TestUseSkillAutoInstall:
    """skillForge.autoInstall gates the Hub bundle download in use_skill."""

    def _client(self) -> _FakeClient:
        return _FakeClient(
            get_result={"score_safety": 0.9, "slug": "foo", "skill_md": "ok"},
            install_result={"slug": "foo", "version": "v1", "skill_md": "ok"},
        )

    async def test_off_skips_install_with_notice(self) -> None:
        client = self._client()
        out = await UseSkillTool(client=client, auto_install="off").execute(
            skill_id="hub/foo",
        )
        assert out.startswith("Skill install skipped")
        assert "'off'" in out
        assert client.install_calls == []

    async def test_prompt_without_tty_skips(self, monkeypatch) -> None:
        import sys

        monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=False))
        client = self._client()
        out = await UseSkillTool(client=client, auto_install="prompt").execute(
            skill_id="hub/foo",
        )
        assert out.startswith("Skill install skipped")
        assert client.install_calls == []

    async def test_prompt_tty_confirmation_installs(self, monkeypatch) -> None:
        import sys

        monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=True))
        monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
        client = self._client()
        out = await UseSkillTool(client=client, auto_install="prompt").execute(
            skill_id="hub/foo",
        )
        assert not out.startswith("Error") and not out.startswith("Skill install skipped")
        assert client.install_calls == ["foo"]
