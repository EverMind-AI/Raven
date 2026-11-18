"""Project-skill discovery: the walk from a working directory to the git root,
the flat ``.agents/skills`` reader over it, and the BM25 index and shared cache
that serve it."""

from __future__ import annotations

import asyncio
import gc
import shutil
import threading
from pathlib import Path

import pytest

from raven.memory_engine.skill_forge.project_skills import (
    ProjectRoots,
    ProjectSkillCache,
    ProjectSkillIndex,
    project_fingerprint,
    project_skill_roots,
    read_project_skills,
    shared_project_skill_cache,
)
from raven.memory_engine.skill_local.registry import build_skill_meta
from raven.memory_engine.skill_local.types import SkillMeta


def _symlink_to(link: Path, target: Path) -> None:
    """Create a symlink, or skip where the platform refuses one.

    Unelevated Windows without Developer Mode raises WinError 1314 for every
    symlink, and none of these fixtures can express the resolved-target rule
    another way.
    """
    try:
        link.symlink_to(target)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink creation requires an unavailable privilege")
        raise


def _skill(
    root: Path,
    name: str,
    body: str = "...",
    front: str = "",
    dir_name: str | None = None,
) -> Path:
    skill_dir = root / (dir_name or name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = f"name: {name}\ndescription: {name} skill"
    if front:
        frontmatter += "\n" + front.strip()
    (skill_dir / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_walk_collects_from_workdir_up_to_git_root_nearest_first(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _skill(repo / ".agents/skills", "outer")
    _skill(repo / "pkg/.agents/skills", "inner")
    workdir = repo / "pkg" / "sub"
    workdir.mkdir(parents=True)

    roots = project_skill_roots(
        workdir,
        agent_home=tmp_path / "home" / "workspace",
        home=tmp_path / "home",
    )

    assert roots is not None
    assert roots.dirs == (
        (repo / "pkg/.agents/skills").resolve(),
        (repo / ".agents/skills").resolve(),
    )
    assert roots.boundary == repo.resolve()


def test_a_git_file_marks_a_linked_worktree_root(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    repo = parent / "repo"
    repo.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
    _skill(repo / ".agents/skills", "one")
    _skill(parent / ".agents/skills", "two")

    roots = project_skill_roots(
        repo,
        agent_home=tmp_path / "home" / "workspace",
        home=tmp_path / "home",
    )

    assert roots is not None
    assert roots.dirs == ((repo / ".agents/skills").resolve(),)


def test_without_git_only_the_workdir_is_read(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    work = parent / "work"
    _skill(parent / ".agents/skills", "outer")
    _skill(work / ".agents/skills", "inner")

    roots = project_skill_roots(
        work,
        agent_home=tmp_path / "home" / "workspace",
        home=tmp_path / "home",
    )

    assert roots is not None
    assert roots.dirs == ((work / ".agents/skills").resolve(),)
    assert roots.boundary == work.resolve()


def test_home_and_agent_home_ancestors_are_never_project_dirs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    _skill(home / ".agents/skills", "root-level")
    _skill(home / ".raven/.agents/skills", "agent-level")
    agent_home = home / ".raven" / "workspace"
    agent_home.mkdir(parents=True)
    workdir = home / ".raven" / "tmp" / "web"
    workdir.mkdir(parents=True)

    assert project_skill_roots(workdir, agent_home=agent_home, home=home) is None


def test_a_git_root_at_home_does_not_bound_a_gitless_workdir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    config = home / ".raven" / "config.json"
    config.parent.mkdir()
    config.write_text(
        "---\nname: setup\ndescription: setup skill\n---\n\nsk-SECRET-VALUE\n",
        encoding="utf-8",
    )
    workdir = home / "Downloads" / "evil"
    skill_dir = workdir / ".agents" / "skills" / "setup"
    skill_dir.mkdir(parents=True)
    _symlink_to(skill_dir / "SKILL.md", config)
    agent_home = home / ".raven" / "workspace"
    agent_home.mkdir(parents=True)

    roots = project_skill_roots(workdir, agent_home=agent_home, home=home)

    assert roots is not None
    assert roots.boundary == workdir.resolve()
    assert read_project_skills(roots) == []


def test_a_git_root_above_home_does_not_bound_a_gitless_workdir(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)
    home = outer / "home"
    config = home / ".raven" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        "---\nname: setup\ndescription: setup skill\n---\n\nsk-SECRET-VALUE\n",
        encoding="utf-8",
    )
    workdir = home / "Downloads" / "evil"
    skill_dir = workdir / ".agents" / "skills" / "setup"
    skill_dir.mkdir(parents=True)
    _symlink_to(skill_dir / "SKILL.md", config)
    agent_home = tmp_path / "raven-data" / "workspace"
    agent_home.mkdir(parents=True)

    roots = project_skill_roots(workdir, agent_home=agent_home, home=home)

    assert roots is not None
    assert roots.boundary == workdir.resolve()
    assert read_project_skills(roots) == []


def test_home_is_a_containment_root_with_agent_home_outside_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    config = home / ".raven" / "config.json"
    config.parent.mkdir()
    config.write_text(
        "---\nname: setup\ndescription: setup skill\n---\n\nsk-SECRET-VALUE\n",
        encoding="utf-8",
    )
    workdir = home / "evil"
    skill_dir = workdir / ".agents" / "skills" / "setup"
    skill_dir.mkdir(parents=True)
    _symlink_to(skill_dir / "SKILL.md", config)
    agent_home = tmp_path / "elsewhere" / "workspace"
    agent_home.mkdir(parents=True)

    roots = project_skill_roots(workdir, agent_home=agent_home, home=home)

    assert roots is not None
    assert roots.boundary == workdir.resolve()
    assert read_project_skills(roots) == []


def test_an_agents_symlink_into_agent_home_is_skipped_inside_the_boundary(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    agent_home = repo / "nested" / "agent-home"
    _skill(agent_home / ".agents" / "skills", "leaked")
    workdir = repo / "pkg"
    workdir.mkdir(parents=True)
    _symlink_to(workdir / ".agents", agent_home / ".agents")
    home = tmp_path / "home"

    assert project_skill_roots(workdir, agent_home=agent_home, home=home) is None

    own = repo / "own"
    _skill(own / ".agents" / "skills", "kept")
    roots = project_skill_roots(own, agent_home=agent_home, home=home)
    assert roots is not None
    assert roots.guarded == (home.resolve() / ".agents", agent_home.resolve())

    inside = ProjectRoots(
        ((workdir / ".agents" / "skills").resolve(),),
        repo.resolve(),
        roots.guarded,
    )
    assert read_project_skills(inside) == []


def test_a_checkout_under_home_with_its_own_git_root_is_collected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = home / "code" / "foo"
    (repo / ".git").mkdir(parents=True)
    _skill(repo / ".agents/skills", "pdf-tools")
    workdir = repo / "pkg"
    workdir.mkdir(parents=True)
    agent_home = home / ".raven" / "workspace"
    agent_home.mkdir(parents=True)

    roots = project_skill_roots(workdir, agent_home=agent_home, home=home)

    assert roots is not None
    assert roots.dirs == ((repo / ".agents/skills").resolve(),)
    assert roots.boundary == repo.resolve()
    assert [meta.name for meta in read_project_skills(roots)] == ["pdf-tools"]


def test_no_directory_found_is_none(tmp_path: Path) -> None:
    agent_home = tmp_path / "home" / "workspace"
    agent_home.mkdir(parents=True)

    assert project_skill_roots(tmp_path / "w", agent_home=agent_home, home=tmp_path / "home") is None


def test_a_symlinked_agents_dir_outside_the_boundary_is_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    _skill(elsewhere / ".agents/skills", "outside")
    _symlink_to(repo / ".agents", elsewhere / ".agents")

    roots = project_skill_roots(
        repo,
        agent_home=tmp_path / "home" / "workspace",
        home=tmp_path / "home",
    )

    assert roots is None


def test_nearer_skill_wins_a_name_collision(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _skill(repo / ".agents/skills", "fmt", body="outer")
    _skill(repo / "pkg/.agents/skills", "fmt", body="inner")

    roots = project_skill_roots(
        repo / "pkg",
        agent_home=tmp_path / "home" / "workspace",
        home=tmp_path / "home",
    )

    assert roots is not None
    skills = read_project_skills(roots)
    assert [s.content.strip() for s in skills if s.name == "fmt"] == ["inner"]


def test_the_id_follows_the_frontmatter_name(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _skill(repo / ".agents/skills", "display-name", dir_name="folder-name")

    roots = project_skill_roots(
        repo,
        agent_home=tmp_path / "home" / "workspace",
        home=tmp_path / "home",
    )

    assert roots is not None
    (meta,) = read_project_skills(roots)
    assert meta.name == "display-name"
    assert meta.id == "project/display-name"


def test_a_symlinked_skill_outside_the_boundary_is_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    skills_dir = repo / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    _skill(elsewhere, "ext")
    _symlink_to(skills_dir / "ext", elsewhere / "ext")
    _skill(repo / "tools", "own")
    _symlink_to(skills_dir / "own", repo / "tools" / "own")

    roots = project_skill_roots(
        repo,
        agent_home=tmp_path / "home" / "workspace",
        home=tmp_path / "home",
    )

    assert roots is not None
    assert {s.name for s in read_project_skills(roots)} == {"own"}


def test_a_skill_file_symlinked_outside_the_boundary_is_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    skills_dir = repo / ".agents" / "skills"
    bad = skills_dir / "bad"
    bad.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    secret = elsewhere / "secret.txt"
    secret.write_text("outside data", encoding="utf-8")
    _symlink_to(bad / "SKILL.md", secret)
    _skill(skills_dir, "good", body="inside")

    roots = project_skill_roots(
        repo,
        agent_home=tmp_path / "home" / "workspace",
        home=tmp_path / "home",
    )

    assert roots is not None
    assert {s.name for s in read_project_skills(roots)} == {"good"}


def test_a_skill_file_symlink_loop_is_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    skills_dir = repo / ".agents" / "skills"
    loop = skills_dir / "loop"
    loop.mkdir(parents=True)
    _symlink_to(loop / "SKILL.md", Path("SKILL.md"))
    _skill(skills_dir, "good")

    roots = project_skill_roots(
        repo,
        agent_home=tmp_path / "home" / "workspace",
        home=tmp_path / "home",
    )

    assert roots is not None
    assert {s.name for s in read_project_skills(roots)} == {"good"}


def test_always_and_inject_frontmatter_are_ignored(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    skill_dir = _skill(repo / ".agents/skills", "x", front="always: true\ninject: description")

    roots = project_skill_roots(
        repo,
        agent_home=tmp_path / "home" / "workspace",
        home=tmp_path / "home",
    )

    assert roots is not None
    control = build_skill_meta(skill_dir, "project")
    assert control is not None
    assert control.always is True
    assert control.inject == "description"
    (meta,) = read_project_skills(roots)
    assert meta.always is False
    assert meta.inject == "full"
    assert meta.source == "project"


def test_only_the_flat_layout_counts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    skills_dir = repo / ".agents" / "skills"
    deep = skills_dir / "group" / "deep"
    deep.mkdir(parents=True)
    (deep / "SKILL.md").write_text(
        "---\nname: deep\ndescription: deep skill\n---\n\nbody\n",
        encoding="utf-8",
    )
    _skill(skills_dir, "top")

    roots = project_skill_roots(
        repo,
        agent_home=tmp_path / "home" / "workspace",
        home=tmp_path / "home",
    )

    assert roots is not None
    assert {s.name for s in read_project_skills(roots)} == {"top"}


def test_an_unreadable_skill_file_is_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    skills_dir = repo / ".agents" / "skills"
    bad = skills_dir / "bad"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_bytes(b"\xff\xfe\x00bad")
    _skill(skills_dir, "good")

    roots = project_skill_roots(
        repo,
        agent_home=tmp_path / "home" / "workspace",
        home=tmp_path / "home",
    )

    assert roots is not None
    assert {s.name for s in read_project_skills(roots)} == {"good"}


def test_a_directory_removed_after_the_walk_is_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _skill(repo / ".agents/skills", "outer")
    _skill(repo / "pkg/.agents/skills", "inner")

    roots = project_skill_roots(
        repo / "pkg",
        agent_home=tmp_path / "home" / "workspace",
        home=tmp_path / "home",
    )

    assert roots is not None
    shutil.rmtree(roots.dirs[0])
    assert {s.name for s in read_project_skills(roots)} == {"outer"}


def _meta(name: str, description: str) -> SkillMeta:
    return SkillMeta(
        id=f"project/{name}",
        name=name,
        description=description,
        path=Path("/project") / name / "SKILL.md",
        content="",
        source="project",
    )


def _roots(repo: Path) -> ProjectRoots:
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    return ProjectRoots(((repo / ".agents" / "skills").resolve(),), repo.resolve())


def _counting_build(builds: list[int], *, started: threading.Event | None = None, gate: threading.Event | None = None):
    def build(roots: ProjectRoots) -> ProjectSkillIndex:
        builds.append(1)
        if started is not None:
            started.set()
        if gate is not None:
            assert gate.wait(5.0)
        return ProjectSkillIndex(read_project_skills(roots))

    return build


def test_index_search_ranks_by_bm25_and_get_by_name() -> None:
    idx = ProjectSkillIndex(
        [
            _meta("pdf-tools", "Extract text from PDF files"),
            _meta("pdf-merge", "Merge PDF documents"),
            _meta("weather", "Forecasts"),
        ]
    )

    hits = idx.search("pdf text", 5)

    assert [m.name for m, _ in hits] == ["pdf-tools", "pdf-merge"]
    assert all(score > 0.0 for _, score in hits)
    assert [m.name for m, _ in idx.search("pdf text", 1)] == ["pdf-tools"]
    assert idx.get("weather").name == "weather"
    assert idx.get("nope") is None


def test_the_fingerprint_tracks_skill_file_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    roots = _roots(repo)
    skill_dir = _skill(repo / ".agents" / "skills", "watched")

    before = project_fingerprint(roots)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: watched\ndescription: watched skill\n---\n\nmuch longer body\n",
        encoding="utf-8",
    )

    assert isinstance(before, tuple)
    assert project_fingerprint(roots) != before


def test_the_shared_cache_is_one_per_process() -> None:
    cache = shared_project_skill_cache()
    assert cache is shared_project_skill_cache()
    assert cache._max_entries == 8


async def test_a_cold_build_past_the_wait_returns_none_then_serves_the_next_call(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    roots = _roots(repo)
    _skill(repo / ".agents" / "skills", "cold")
    started = threading.Event()
    gate = threading.Event()
    builds: list[int] = []
    cache = ProjectSkillCache(build=_counting_build(builds, started=started, gate=gate))

    assert await cache.index_for(roots, wait_s=0.05) is None
    waiting = asyncio.create_task(cache.index_for(roots, wait_s=None))
    await asyncio.sleep(0)
    assert await asyncio.to_thread(started.wait, 5.0)
    gate.set()
    index = await waiting

    assert index is not None
    assert {m.name for m in index.metas} == {"cold"}
    assert len(builds) == 1


async def test_a_changed_project_is_rebuilt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    roots = _roots(repo)
    _skill(repo / ".agents" / "skills", "a")
    builds: list[int] = []
    cache = ProjectSkillCache(build=_counting_build(builds))

    first = await cache.index_for(roots, wait_s=None)
    assert first is not None
    assert {m.name for m in first.metas} == {"a"}
    again = await cache.index_for(roots, wait_s=None)
    assert again is first
    assert len(builds) == 1

    _skill(repo / ".agents" / "skills", "b")
    second = await cache.index_for(roots, wait_s=None)

    assert second is not None
    assert {m.name for m in second.metas} == {"a", "b"}
    assert len(builds) == 2


async def test_a_skill_file_appearing_in_an_existing_entry_dir_is_seen(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    roots = _roots(repo)
    _skill(repo / ".agents" / "skills", "early")
    late = repo / ".agents" / "skills" / "late"
    late.mkdir()
    builds: list[int] = []
    cache = ProjectSkillCache(build=_counting_build(builds))

    first = await cache.index_for(roots, wait_s=None)
    assert first is not None
    assert {m.name for m in first.metas} == {"early"}
    assert len(builds) == 1

    (late / "SKILL.md").write_text(
        "---\nname: late\ndescription: late skill\n---\n\nbody\n",
        encoding="utf-8",
    )
    second = await cache.index_for(roots, wait_s=None)

    assert second is not None
    assert {m.name for m in second.metas} == {"early", "late"}
    assert len(builds) == 2


async def test_a_slow_rebuild_serves_the_previous_index(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    roots = _roots(repo)
    _skill(repo / ".agents" / "skills", "a")
    started = threading.Event()
    gate = threading.Event()
    builds: list[int] = []

    def build(roots_in: ProjectRoots) -> ProjectSkillIndex:
        builds.append(1)
        if len(builds) > 1:
            started.set()
            assert gate.wait(5.0)
        return ProjectSkillIndex(read_project_skills(roots_in))

    cache = ProjectSkillCache(build=build)
    first = await cache.index_for(roots, wait_s=None)
    assert first is not None
    assert {m.name for m in first.metas} == {"a"}

    _skill(repo / ".agents" / "skills", "b")
    stale = await cache.index_for(roots, wait_s=0.05)

    assert stale is first
    assert {m.name for m in stale.metas} == {"a"}
    assert await asyncio.to_thread(started.wait, 5.0)
    gate.set()
    fresh = await cache.index_for(roots, wait_s=None)
    assert {m.name for m in fresh.metas} == {"a", "b"}
    assert len(builds) == 2


async def test_concurrent_calls_share_one_build(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    roots = _roots(repo)
    _skill(repo / ".agents" / "skills", "shared")
    started = threading.Event()
    gate = threading.Event()
    builds: list[int] = []
    cache = ProjectSkillCache(build=_counting_build(builds, started=started, gate=gate))

    first = asyncio.create_task(cache.index_for(roots, wait_s=None))
    second = asyncio.create_task(cache.index_for(roots, wait_s=None))
    assert await asyncio.to_thread(started.wait, 5.0)
    gate.set()
    one, two = await asyncio.gather(first, second)

    assert one is two
    assert one is not None
    assert {m.name for m in one.metas} == {"shared"}
    assert len(builds) == 1


async def test_two_boundaries_over_one_directory_tuple_do_not_share_an_index(tmp_path: Path) -> None:
    """Containment is decided against ``roots.boundary`` while the index is built,
    so the directory tuple alone cannot key it: an outer repo whose ``.agents``
    points into a nested repo collects the same resolved directory under a wider
    boundary, and whichever build landed first must not decide for the other."""
    outer = tmp_path / "outer"
    nested = outer / "nested"
    (outer / ".git").mkdir(parents=True)
    (nested / ".git").mkdir(parents=True)
    (outer / "work").mkdir()
    (nested / "work").mkdir()
    _skill(nested / ".agents" / "skills", "pdf-tools")
    outside = _skill(outer / "elsewhere", "evil")
    _symlink_to(nested / ".agents" / "skills" / "evil", outside)
    _symlink_to(outer / ".agents", nested / ".agents")
    agent_home = tmp_path / "agent-home"
    home = tmp_path / "home"

    outer_roots = project_skill_roots(outer / "work", agent_home=agent_home, home=home)
    nested_roots = project_skill_roots(nested / "work", agent_home=agent_home, home=home)
    assert outer_roots is not None and nested_roots is not None
    assert outer_roots.dirs == nested_roots.dirs
    assert outer_roots.boundary != nested_roots.boundary
    assert {s.name for s in read_project_skills(outer_roots)} == {"pdf-tools", "evil"}
    assert {s.name for s in read_project_skills(nested_roots)} == {"pdf-tools"}

    builds: list[int] = []
    cache = ProjectSkillCache(build=_counting_build(builds))
    assert await cache.index_for(outer_roots, wait_s=None) is not None
    second = await cache.index_for(nested_roots, wait_s=None)

    assert second is not None
    assert {m.name for m in second.metas} == {"pdf-tools"}


async def test_the_least_recently_used_project_is_evicted(tmp_path: Path) -> None:
    roots = []
    for name in ("p1", "p2", "p3"):
        repo = tmp_path / name
        roots.append(_roots(repo))
        _skill(repo / ".agents" / "skills", f"only-{name}")
    builds: list[int] = []
    cache = ProjectSkillCache(max_entries=2, build=_counting_build(builds))

    assert await cache.index_for(roots[0], wait_s=None) is not None
    assert await cache.index_for(roots[1], wait_s=None) is not None
    assert len(builds) == 2

    assert await cache.index_for(roots[0], wait_s=None) is not None
    assert len(builds) == 2

    assert await cache.index_for(roots[2], wait_s=None) is not None
    assert len(builds) == 3
    assert await cache.index_for(roots[1], wait_s=None) is not None
    assert len(builds) == 4


async def test_a_vanished_project_drops_its_entry(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    roots = _roots(repo)
    _skill(repo / ".agents" / "skills", "gone")
    cache = ProjectSkillCache()

    assert await cache.index_for(roots, wait_s=None) is not None

    shutil.rmtree(roots.dirs[0])
    assert await cache.index_for(roots, wait_s=None) is None


async def test_a_cache_with_no_room_serves_the_index_it_built(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    roots = _roots(repo)
    _skill(repo / ".agents" / "skills", "uncached")
    builds: list[int] = []
    cache = ProjectSkillCache(max_entries=0, build=_counting_build(builds))

    first = await cache.index_for(roots, wait_s=None)
    assert first is not None
    assert {m.name for m in first.metas} == {"uncached"}

    second = await cache.index_for(roots, wait_s=None)
    assert second is not None
    assert len(builds) == 2


async def test_a_failed_build_after_the_wait_is_retrieved(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    roots = _roots(repo)
    _skill(repo / ".agents" / "skills", "boom")
    started = threading.Event()
    gate = threading.Event()
    builds: list[int] = []

    def build(roots_in: ProjectRoots) -> ProjectSkillIndex:
        builds.append(1)
        if len(builds) == 1:
            started.set()
            assert gate.wait(5.0)
            raise RuntimeError("boom")
        return ProjectSkillIndex(read_project_skills(roots_in))

    cache = ProjectSkillCache(build=build)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unretrieved: list[str] = []
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(str(context.get("message", ""))))

    assert await cache.index_for(roots, wait_s=0.05) is None
    assert await asyncio.to_thread(started.wait, 5.0)
    gate.set()
    deadline = loop.time() + 5.0
    while cache._inflight and loop.time() < deadline:
        await asyncio.sleep(0.01)
    gc.collect()
    loop.set_exception_handler(previous_handler)

    assert not cache._inflight
    assert [message for message in unretrieved if "never retrieved" in message] == []
    index = await cache.index_for(roots, wait_s=None)
    assert index is not None
    assert {m.name for m in index.metas} == {"boom"}
    assert len(builds) == 2
