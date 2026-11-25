"""ProjectSkillSource: the router lane over the turn's project skills.

The source answers from the working directory bound to the running turn, so
two sessions in one process see only their own project, a cold index costs the
router no more than its short wait, and a deleted working directory is a miss
rather than a failure.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
import time
from pathlib import Path

import pytest

from raven.agent import workdir
from raven.agent.context import ContextBuilder
from raven.config.raven import MemoryConfig, SkillForgeRouterConfig
from raven.context_engine.factory import _build_router
from raven.context_engine.scent import ScentMenu
from raven.memory_engine.skill_forge import (
    ForgeSkillSource,
    ProjectSkills,
    ProjectSkillSource,
    RouterHit,
    SkillForgeRouter,
)
from raven.memory_engine.skill_forge.project_skills import (
    ProjectSkillCache,
    ProjectSkillIndex,
    read_project_skills,
)


class _Bound:
    """The working directory and live switch a test hands the project."""

    def __init__(self, workdir_path: Path | None = None, *, enabled: bool = True) -> None:
        self.workdir = workdir_path
        self.enabled = enabled


def _project(
    tmp_path: Path,
    bound: _Bound | None = None,
    *,
    cache: ProjectSkillCache | None = None,
) -> ProjectSkills:
    bound = bound or _Bound()
    return ProjectSkills(
        agent_home=tmp_path / "agent-home",
        workdir_reader=lambda: bound.workdir,
        enabled_reader=lambda: bound.enabled,
        cache=cache if cache is not None else ProjectSkillCache(),
    )


def _repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def _skill(root: Path, name: str, body: str = "...") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


class _StaticSource:
    """A source that answers every search with the hits it was handed."""

    def __init__(self, name: str, weight: float, hits: list[RouterHit]) -> None:
        self.name = name
        self.weight = weight
        self._hits = hits

    async def search(self, query, history, k):
        return list(self._hits)


def test_conforms_to_the_source_protocol(tmp_path: Path) -> None:
    assert isinstance(ProjectSkillSource(_project(tmp_path)), ForgeSkillSource)


async def test_no_bound_workdir_returns_no_hits(tmp_path: Path) -> None:
    src = ProjectSkillSource(_project(tmp_path))
    assert await src.search("pdf", [], 5) == []


async def test_switch_off_returns_no_hits(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _skill(repo / ".agents" / "skills", "pdf-tools", body="Extract text from PDF files.")
    bound = _Bound(repo, enabled=False)
    src = ProjectSkillSource(_project(tmp_path, bound))
    assert await src.search("pdf", [], 5) == []


async def test_hits_are_project_qualified_and_carry_the_body(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    skill_dir = _skill(repo / ".agents" / "skills", "pdf-tools", body="Extract text from PDF files.")
    bound = _Bound(repo)
    src = ProjectSkillSource(_project(tmp_path, bound))

    (hit,) = await src.search("pdf text", [], 5)

    assert hit.qualified_id == "project/pdf-tools"
    assert hit.name == "pdf-tools"
    assert "Extract" in hit.content
    assert hit.score > 0.0
    assert hit.meta == {
        "source": "project",
        "description": "pdf-tools skill",
        "path": str((skill_dir / "SKILL.md").resolve()),
    }


async def test_two_workdirs_see_only_their_own_skills(tmp_path: Path) -> None:
    repo_a = _repo(tmp_path / "repo-a")
    repo_b = _repo(tmp_path / "repo-b")
    _skill(repo_a / ".agents" / "skills", "alpha", body="Alpha project helper.")
    _skill(repo_b / ".agents" / "skills", "beta", body="Beta project helper.")
    bound = _Bound(repo_a)
    src = ProjectSkillSource(_project(tmp_path, bound))

    names_a = {hit.name for hit in await src.search("alpha beta helper", [], 5)}
    bound.workdir = repo_b
    names_b = {hit.name for hit in await src.search("alpha beta helper", [], 5)}

    assert names_a == {"alpha"}
    assert names_b == {"beta"}


async def test_a_deleted_workdir_returns_no_hits_quietly(tmp_path: Path, caplog) -> None:
    repo = _repo(tmp_path / "gone")
    _skill(repo / ".agents" / "skills", "vanished")
    bound = _Bound(repo)
    src = ProjectSkillSource(_project(tmp_path, bound))
    shutil.rmtree(repo)

    assert await src.search("vanished", [], 5) == []
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


async def test_a_cold_build_does_not_hold_up_the_router(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _skill(repo / ".agents" / "skills", "cold", body="A cold project skill.")
    started = threading.Event()
    gate = threading.Event()

    def build(roots) -> ProjectSkillIndex:
        started.set()
        assert gate.wait(5.0)
        return ProjectSkillIndex(read_project_skills(roots))

    bound = _Bound(repo)
    project_source = ProjectSkillSource(_project(tmp_path, bound, cache=ProjectSkillCache(build=build)))
    router = SkillForgeRouter(
        sources=[
            _StaticSource("local", 1.0, [RouterHit(qualified_id="local/x", name="x", content="x", score=1.0)]),
            project_source,
        ]
    )

    hits = await asyncio.wait_for(router.select("x", [], k=3), 1.0)
    assert [hit.qualified_id for hit in hits] == ["local/x"]

    gate.set()
    assert await asyncio.to_thread(started.wait, 5.0)
    await asyncio.sleep(0.05)


async def test_a_slow_project_search_does_not_block_the_event_loop(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _skill(repo / ".agents" / "skills", "pdf-tools", body="Extract text from PDF files.")
    index = ProjectSkillIndex([])
    searched: list[str] = []

    def slow_search(query: str, k: int, *, should_stop=None) -> list[tuple]:
        searched.append(query)
        time.sleep(1.0)
        return []

    index.search = slow_search  # type: ignore[method-assign]
    src = ProjectSkillSource(_project(tmp_path, _Bound(repo), cache=ProjectSkillCache(build=lambda roots: index)))

    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = loop.time()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    started = time.monotonic()
    hits = await src.search("pdf text", [], 5)
    elapsed = time.monotonic() - started
    stop.set()
    await beat

    assert hits == []
    assert searched == ["pdf text"]
    assert max(gaps) < 0.2
    assert elapsed < 0.9


async def test_a_timed_out_search_stops_the_worker_it_left_behind(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _skill(repo / ".agents" / "skills", "pdf-tools", body="Extract text from PDF files.")
    index = ProjectSkillIndex([])
    stopped = threading.Event()

    def never_ending_search(query: str, k: int, *, should_stop=None) -> list[tuple]:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                stopped.set()
                return []
            time.sleep(0.005)
        return []

    index.search = never_ending_search  # type: ignore[method-assign]
    src = ProjectSkillSource(
        _project(tmp_path, _Bound(repo), cache=ProjectSkillCache(build=lambda roots: index)),
        wait_s=0.05,
    )

    started = time.monotonic()
    hits = await src.search("pdf text", [], 5)
    elapsed = time.monotonic() - started

    assert hits == []
    assert elapsed < 1.0
    assert await asyncio.to_thread(stopped.wait, 2.0)


async def test_a_cancelled_search_stops_the_worker_it_left_behind(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _skill(repo / ".agents" / "skills", "pdf-tools", body="Extract text from PDF files.")
    index = ProjectSkillIndex([])
    started = threading.Event()
    stopped = threading.Event()
    handed: list = []

    def never_ending_search(query: str, k: int, *, should_stop=None) -> list[tuple]:
        handed.append(should_stop)
        started.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                stopped.set()
                return []
            time.sleep(0.005)
        return []

    index.search = never_ending_search  # type: ignore[method-assign]
    src = ProjectSkillSource(
        _project(tmp_path, _Bound(repo), cache=ProjectSkillCache(build=lambda roots: index)),
        wait_s=10.0,
    )

    task = asyncio.create_task(src.search("pdf text", [], 5))
    assert await asyncio.to_thread(started.wait, 5.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert handed[0]() is True
    assert await asyncio.to_thread(stopped.wait, 2.0)


async def test_the_scent_menu_offers_the_bound_project_skill(tmp_path: Path) -> None:
    agent_home = tmp_path / "agent-home"
    repo = _repo(tmp_path / "repo")
    _skill(repo / ".agents" / "skills", "pdf-tools", body="Extract text from PDF files.")
    router = _build_router(
        builder=ContextBuilder(workspace=agent_home, start_watcher=False),
        backend=None,
        memory_config=MemoryConfig(),
        skill_forge_router_config=SkillForgeRouterConfig(),
        hub_enabled_reader=lambda: False,
    )

    with workdir.bind(repo):
        result = await ScentMenu(router).build("Extract text from every PDF in this repository", [])

    assert "project/pdf-tools" in result.text
    assert "project/pdf-tools" in result.skill_ids
