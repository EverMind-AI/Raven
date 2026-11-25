"""Unit tests for the read_skill / use_skill agent tools."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.tools.skill_hub import (
    FindSkillTool,
    ReadSkillTool,
    UseSkillTool,
    router_held_hit,
    split_qualified_id,
)
from raven.context_engine.scent import ScentMenu
from raven.contracts.memory import Memory
from raven.memory_engine import ProjectSkills
from raven.memory_engine.skill_forge import RouterHit
from raven.memory_engine.skill_forge.backend_source import BackendSkillSource
from raven.memory_engine.skill_forge.project_skills import ProjectSkillCache
from raven.memory_engine.skill_forge.router import SkillForgeRouter


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
        return [(Path("/fake/home/skills"), "workspace")]


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


def _project_repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path.resolve()


def _project_skill(repo: Path, name: str, body: str) -> Path:
    skill_dir = repo / ".agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n\n{body}\n", encoding="utf-8"
    )
    return skill_dir


def _project(tmp_path: Path, repo: Path | None, *, enabled: bool = True) -> ProjectSkills:
    return ProjectSkills(
        agent_home=tmp_path / "agent-home",
        workdir_reader=lambda: repo,
        enabled_reader=lambda: enabled,
        cache=ProjectSkillCache(),
    )


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


class TestProjectSkills:
    """``project/<name>`` ids resolve against the working directory bound to the turn.

    A project id is never served from the router's held hits: the router is
    shared by every session, so a held body may belong to another session's
    project.
    """

    async def test_read_skill_reads_a_project_skill_from_the_bound_project(self, tmp_path: Path) -> None:
        repo = _project_repo(tmp_path / "repo")
        skill_dir = _project_skill(
            repo,
            "pdf-tools",
            "Extract text from PDF files.\n\nRead [guide](references/guide.md) before placing a page.",
        )
        guide = skill_dir / "references" / "guide.md"
        guide.parent.mkdir()
        guide.write_text("shapes", encoding="utf-8")

        out = await ReadSkillTool(project=_project(tmp_path, repo)).execute(skill_id="project/pdf-tools")

        assert "Extract" in out
        assert f"({skill_dir}/references/guide.md)" in out

    async def test_read_skill_project_miss_does_not_fall_back_to_held_hits(self, tmp_path: Path) -> None:
        repo = _project_repo(tmp_path / "repo")
        (repo / ".agents" / "skills").mkdir(parents=True)
        held = RouterHit(qualified_id="project/ghost", name="ghost", content="HELD BODY", score=1.0)

        out = await ReadSkillTool(project=_project(tmp_path, repo), held_hit=lambda _q: held).execute(
            skill_id="project/ghost"
        )

        assert out.startswith("Error: no project skill 'ghost'")
        assert "HELD BODY" not in out

    async def test_read_skill_project_flags_foreign_home_paths_as_a_note(self, tmp_path: Path) -> None:
        repo = _project_repo(tmp_path / "repo")
        _project_skill(repo, "pdf-tools", "Write the output to ~/.config/opencode/results.json.")

        out = await ReadSkillTool(project=_project(tmp_path, repo)).execute(skill_id="project/pdf-tools")

        assert "note:" in out
        assert "~/.config/opencode" in out
        assert "Error" not in out

    async def test_read_skill_project_respects_the_blocklist(self, tmp_path: Path) -> None:
        repo = _project_repo(tmp_path / "repo")
        _project_skill(repo, "pdf-tools", "Extract text from PDF files.")

        out = await ReadSkillTool(project=_project(tmp_path, repo), blocklist=["pdf-tools"]).execute(
            skill_id="project/pdf-tools"
        )

        assert "blocklist" in out

    async def test_read_skill_project_when_switched_off(self, tmp_path: Path) -> None:
        repo = _project_repo(tmp_path / "repo")
        _project_skill(repo, "pdf-tools", "Extract text from PDF files.")

        out = await ReadSkillTool(project=_project(tmp_path, repo, enabled=False)).execute(skill_id="project/pdf-tools")

        assert out.startswith("Error")
        assert "skillForge.projectSkills.enabled" in out

    async def test_read_skill_project_skill_deleted_after_indexing(self, tmp_path: Path) -> None:
        repo = _project_repo(tmp_path / "repo")
        skill_dir = _project_skill(repo, "pdf-tools", "Extract text from PDF files.")
        tool = ReadSkillTool(project=_project(tmp_path, repo))

        assert "Extract" in await tool.execute(skill_id="project/pdf-tools")
        shutil.rmtree(skill_dir)

        second = await tool.execute(skill_id="project/pdf-tools")

        assert second.startswith("Error: no project skill 'pdf-tools'")

    async def test_read_skill_reads_both_ids_of_a_shared_name(self, tmp_path: Path) -> None:
        repo = _project_repo(tmp_path / "repo")
        _project_skill(repo, "fmt", "PROJECT BODY")
        registry = _FakeRegistry({("builtin", "fmt"): _meta(tmp_path, "fmt", "LOCAL BODY", with_scripts=False)})
        tool = ReadSkillTool(registry=registry, project=_project(tmp_path, repo))

        local_out = await tool.execute(skill_id="local/fmt")
        project_out = await tool.execute(skill_id="project/fmt")

        assert "LOCAL BODY" in local_out
        assert "PROJECT BODY" in project_out

    async def test_a_tool_without_project_access_refuses_project_ids(self) -> None:
        out = await ReadSkillTool().execute(skill_id="project/x")

        assert out.startswith("Error: project skills are not available")

    async def test_use_skill_project_returns_the_scripts_dir(self, tmp_path: Path) -> None:
        repo = _project_repo(tmp_path / "repo")
        skill_dir = _project_skill(repo, "pdf-tools", "Extract text from PDF files.")
        (skill_dir / "scripts").mkdir()

        out = await UseSkillTool(project=_project(tmp_path, repo)).execute(skill_id="project/pdf-tools")

        assert f"scripts_dir: {repo / '.agents/skills/pdf-tools/scripts'}" in out


@pytest.mark.parametrize("tool_cls", [ReadSkillTool, UseSkillTool])
class TestLocalMiss:
    """What a ``local/`` miss tells the model, from either tool.

    The scent menu and ``# Active Skills`` read the registry these tools
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
        assert out.startswith("Error: no local skill 'confirming-pr-review' found")
        assert "# Skills" not in out
        assert "already in your context" not in out

    async def test_it_offers_no_ids_when_none_is_close(self, tool_cls, tmp_path: Path) -> None:
        registry = _registry_with(tmp_path, workspace=["raven-dev-conventions"])
        out = await tool_cls(registry=registry).execute(skill_id="local/confirming-pr-review")
        assert out.startswith("Error: no local skill 'confirming-pr-review' found")
        assert "Closest local ids" not in out

    async def test_a_tool_without_a_registry_still_answers_a_local_miss(self, tool_cls) -> None:
        out = await tool_cls().execute(skill_id="local/confirming-pr-review")
        assert out.startswith("Error: no local skill 'confirming-pr-review' found")
        assert "read_file" in out
        assert "Local ids cover" not in out

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

    async def test_it_still_searches_a_library_at_the_limit(self, tool_cls) -> None:
        from raven.agent.tools.skill_hub import _NEAR_ID_SEARCH_LIMIT

        metas = {("builtin", f"deck-{i:05d}"): object() for i in range(_NEAR_ID_SEARCH_LIMIT)}
        out = await tool_cls(registry=_FakeRegistry(metas)).execute(skill_id="local/deck-000000")
        assert "Closest local ids: local/deck-" in out

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


class _AgentTrack:
    """A memory backend whose agent track holds one evolved skill and one case."""

    async def recall(self, query, *, agent_id=None, top_k=5, **_):
        return [
            Memory(
                text="EVOLVED-STEP-1 rotate pg_dump files weekly",
                score=0.9,
                metadata={"id": "sk_42", "name": "pg-backup-rotation"},
            ),
            Memory(text="CASE-APPROACH last time the WAL was pruned first", score=0.8, metadata={"id": "case_7"}),
        ]


def _agent_track_router() -> tuple[SkillForgeRouter, BackendSkillSource]:
    source = BackendSkillSource(_AgentTrack(), agent_id="agent-1", name="everos")
    return SkillForgeRouter([source]), source


@pytest.mark.parametrize("tool_cls", [ReadSkillTool, UseSkillTool])
class TestMemoryBackendHits:
    """An evolved skill or a case has no file: it exists only as the hit the
    router returned, so that hit is where its body is read from."""

    @staticmethod
    def _tool(tool_cls, tmp_path: Path, router: SkillForgeRouter, **kwargs):
        return tool_cls(registry=_registry_with(tmp_path), held_hit=router_held_hit(lambda: router), **kwargs)

    async def test_an_id_find_skill_listed_reads_back_verbatim(self, tool_cls, tmp_path: Path) -> None:
        router, _ = _agent_track_router()
        listed = await FindSkillTool(lambda: router).execute(query="postgres backup rotation")
        assert "everos/sk_42" in listed
        assert "everos/case_7" in listed

        tool = self._tool(tool_cls, tmp_path, router)
        skill = await tool.execute(skill_id="everos/sk_42")
        case = await tool.execute(skill_id="everos/case_7")

        assert not skill.startswith("Error")
        assert "EVOLVED-STEP-1 rotate pg_dump files weekly" in skill
        assert not case.startswith("Error")
        assert "CASE-APPROACH last time the WAL was pruned first" in case

    async def test_an_id_the_scent_menu_offered_reads_back(self, tool_cls, tmp_path: Path) -> None:
        router, _ = _agent_track_router()
        menu = await ScentMenu(router).build("rotate the postgres backups weekly and prune old dumps", [])
        assert "everos/sk_42" in menu.skill_ids

        out = await self._tool(tool_cls, tmp_path, router).execute(skill_id="everos/sk_42")

        assert "EVOLVED-STEP-1 rotate pg_dump files weekly" in out

    async def test_a_miss_names_the_search_that_brings_the_id_back(self, tool_cls, tmp_path: Path) -> None:
        """Nothing is held after a restart. The miss sends the model to
        find_skill, and that holds: an id the search returns reads right after."""
        router, _ = _agent_track_router()
        tool = self._tool(tool_cls, tmp_path, router)

        miss = await tool.execute(skill_id="everos/sk_42")
        assert miss.startswith("Error: no everos skill 'sk_42'")
        assert "find_skill" in miss
        assert "context" not in miss

        await FindSkillTool(lambda: router).execute(query="postgres backup rotation")
        assert "EVOLVED-STEP-1 rotate pg_dump files weekly" in await tool.execute(skill_id="everos/sk_42")

    async def test_turning_memory_off_forgets_its_hits(self, tool_cls, tmp_path: Path) -> None:
        router, source = _agent_track_router()
        await FindSkillTool(lambda: router).execute(query="postgres backup rotation")

        router.remove_source(source)

        assert (await self._tool(tool_cls, tmp_path, router).execute(skill_id="everos/sk_42")).startswith("Error")

    async def test_a_held_hit_on_the_blocklist_is_refused_by_name(self, tool_cls, tmp_path: Path) -> None:
        router, _ = _agent_track_router()
        await FindSkillTool(lambda: router).execute(query="postgres backup rotation")

        out = await self._tool(tool_cls, tmp_path, router, blocklist=["pg-backup-rotation"]).execute(
            skill_id="everos/sk_42"
        )

        assert "blocklist" in out
        assert "EVOLVED-STEP-1" not in out


@pytest.mark.parametrize("tool_cls", [ReadSkillTool, UseSkillTool])
async def test_a_tool_with_no_router_names_no_search_on_a_miss(tool_cls, tmp_path: Path) -> None:
    """A built-in sub-agent's read_skill holds no hits and has no find_skill, so
    its miss must not send the model to a tool it cannot call."""
    out = await tool_cls(registry=_registry_with(tmp_path)).execute(skill_id="everos/sk_42")

    assert out.startswith("Error: no everos skill 'sk_42' found")
    assert "find_skill" not in out


@pytest.mark.parametrize("tool_cls", [ReadSkillTool, UseSkillTool])
async def test_a_switch_off_tool_names_no_search_it_cannot_run(tool_cls, tmp_path: Path) -> None:
    """The master switch leaves the router absent while the tool keeps the
    held-hit wiring, so a miss must not send the model to a find_skill that
    answers "not available"."""
    tool = tool_cls(registry=_registry_with(tmp_path), held_hit=router_held_hit(lambda: None))

    out = await tool.execute(skill_id="everos/sk_42")

    assert out.startswith("Error: no everos skill 'sk_42' found")
    assert "find_skill" not in out


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
        id — the exact form the scent menu prints and both tool descriptions
        tell the agent to copy — resolved to None.
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


class TestTheHubSwitch:
    """Off is checked per call, before any request leaves for the Hub; the
    local sources are not affected."""

    async def test_a_hub_read_is_refused_when_the_switch_is_off(self) -> None:
        client = _FakeClient(get_result={"skill_md": "x", "name": "Foo"})

        out = await ReadSkillTool(client=client, enabled_reader=lambda: False).execute(skill_id="hub/foo")

        assert out.startswith("Error") and "turned off" in out
        assert client.get_calls == []

    async def test_the_switch_is_read_per_read(self) -> None:
        on = {"value": False}
        client = _FakeClient(get_result={"skill_md": "do the thing", "name": "Foo"})
        tool = ReadSkillTool(client=client, enabled_reader=lambda: on["value"])

        assert "turned off" in await tool.execute(skill_id="hub/foo")
        on["value"] = True
        out = await tool.execute(skill_id="hub/foo")

        assert "Foo" in out and client.get_calls == ["foo"]

    async def test_a_local_read_still_works_when_the_switch_is_off(self, tmp_path: Path) -> None:
        reg = _registry_with(tmp_path, workspace=["x"])

        out = await ReadSkillTool(registry=reg, enabled_reader=lambda: False).execute(skill_id="local/x")

        assert "body" in out

    async def test_a_hub_install_is_refused_when_the_switch_is_off(self) -> None:
        client = _FakeClient(install_result={"slug": "foo", "version": "v1", "skill_md": "x"})

        out = await UseSkillTool(client=client, enabled_reader=lambda: False).execute(skill_id="hub/foo")

        assert out.startswith("Error") and "turned off" in out
        assert client.get_calls == [] and client.install_calls == []

    async def test_a_local_use_still_works_when_the_switch_is_off(self, tmp_path: Path) -> None:
        reg = _registry_with(tmp_path, workspace=["x"])

        out = await UseSkillTool(registry=reg, enabled_reader=lambda: False).execute(skill_id="local/x")

        assert "body" in out
