"""Project skills: read ``.agents/skills`` from the turn's working directory.

A project skill is a flat ``<name>/SKILL.md`` under a ``.agents/skills``
directory, the layout ``gh skill``, ``npx skills`` and Codex install into. The
walk starts at the working directory and climbs to the nearest ancestor holding
a ``.git`` entry (or stops at the working directory itself when there is none),
collecting each ``.agents/skills`` directory on the way, nearest first. The
user-level ``~/.agents`` tree and agent home are never read as project skills,
and a symlink counts only when its resolved target stays inside the project
boundary.

Those directories are then served from a process-wide cache of BM25 indexes
keyed by the directory tuple and its boundary, so every session in one checkout
shares one project index and two boundaries over the same directories do not.
Freshness is a fingerprint of the directories and their ``SKILL.md`` files,
re-read off the event loop, and a search waits for a build only as long as its
caller can afford.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from raven.memory_engine.skill_local.local_pool import _format_skill_text
from raven.memory_engine.skill_local.registry import build_skill_meta
from raven.memory_engine.skill_local.types import SkillMeta
from raven.utils.bm25 import BM25Okapi, tokenize

PROJECT_SKILLS_DIR: Path = Path(".agents") / "skills"


@dataclass(frozen=True)
class ProjectRoots:
    """The ``.agents/skills`` directories of one project, nearest first."""

    dirs: tuple[Path, ...]
    boundary: Path
    guarded: tuple[Path, ...] = ()


def _is_guarded(path: Path, guarded: tuple[Path, ...]) -> bool:
    return any(path.is_relative_to(root) for root in guarded)


def project_skill_roots(
    workdir: Path,
    *,
    agent_home: Path,
    home: Path | None = None,
) -> ProjectRoots | None:
    """Resolve the project's skill directories for one working directory.

    ``boundary`` is the git root, or the working directory when there is no
    git ancestor or the nearest one is home, an ancestor of home, agent home, or an
    ancestor of agent home. Paths resolving into the user-level ``.agents`` tree or agent
    home are never collected. Returns ``None`` when no directory qualifies.
    """
    home_r = (home or Path.home()).resolve()
    agent_home_r = agent_home.resolve()
    guarded = (home_r / ".agents", agent_home_r)
    excluded = {home_r, agent_home_r, *agent_home_r.parents}
    start = workdir.resolve()
    chain = [start, *start.parents]
    git_root = next((d for d in chain if (d / ".git").exists()), None)
    if git_root in {*excluded, *home_r.parents}:
        git_root = None
    walk = chain[: chain.index(git_root) + 1] if git_root is not None else [start]
    boundary = git_root or start
    dirs: list[Path] = []
    for d in walk:
        if d in excluded:
            continue
        cand = d / PROJECT_SKILLS_DIR
        if not cand.is_dir():
            continue
        resolved = cand.resolve()
        if not resolved.is_relative_to(boundary) or _is_guarded(resolved, guarded):
            continue
        dirs.append(resolved)
    return ProjectRoots(tuple(dirs), boundary, guarded) if dirs else None


def read_project_skills(roots: ProjectRoots) -> list[SkillMeta]:
    """Parse every flat project skill, nearest directory winning a name collision.

    A broken or unreadable ``SKILL.md`` is skipped without hiding its siblings;
    ``always`` and ``inject`` frontmatter are ignored, since project skills are
    never injected into the system prompt.
    """
    metas: list[SkillMeta] = []
    seen: set[str] = set()
    for directory in roots.dirs:
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
                resolved_entry = entry.resolve()
                if not resolved_entry.is_relative_to(roots.boundary):
                    continue
                if _is_guarded(resolved_entry, roots.guarded):
                    continue
                # SKILL.md itself can be a symlink out of the project.
                skill_file = (resolved_entry / "SKILL.md").resolve()
                if not skill_file.is_file() or not skill_file.is_relative_to(roots.boundary):
                    continue
                if _is_guarded(skill_file, roots.guarded):
                    continue
            except (OSError, RuntimeError):
                # Python 3.12 raises RuntimeError on a symlink loop; 3.13 yields the looped path instead.
                continue
            meta = build_skill_meta(resolved_entry, "project", always_enabled=False)
            if meta is None or meta.name in seen:
                continue
            seen.add(meta.name)
            metas.append(replace(meta, inject="full", id=f"project/{meta.name}"))
    return metas


class ProjectSkillIndex:
    """BM25 ranking over one project's skills, built like the local pool's."""

    def __init__(self, metas: list[SkillMeta]) -> None:
        self.metas: tuple[SkillMeta, ...] = tuple(metas)
        self._by_name: dict[str, SkillMeta] = {}
        for meta in self.metas:
            self._by_name.setdefault(meta.name, meta)
        self._bm25 = BM25Okapi([tokenize(_format_skill_text(meta)) for meta in self.metas])

    def get(self, name: str) -> SkillMeta | None:
        """Return the skill whose frontmatter name matches exactly."""
        return self._by_name.get(name)

    def search(self, query: str, top_k: int) -> list[tuple[SkillMeta, float]]:
        """Return the best BM25 matches with a positive score, best first."""
        if not self.metas:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(
            ((meta, score) for meta, score in zip(self.metas, scores) if score > 0.0),
            key=lambda hit: hit[1],
            reverse=True,
        )[:top_k]
        return [(meta, float(score)) for meta, score in ranked]


def project_fingerprint(roots: ProjectRoots) -> tuple:
    """Snapshot each directory, every skill entry's own mtime, and its ``SKILL.md``.

    The entry directory's own mtime is what moves when a ``SKILL.md`` appears
    inside it: the parent ``.agents/skills`` mtime does not.
    """
    fingerprint: list[tuple] = []
    for directory in roots.dirs:
        entries: list[tuple] = []
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            try:
                if not entry.is_dir():
                    continue
                entry_mtime_ns = entry.stat().st_mtime_ns
            except OSError:
                continue
            try:
                stat = (entry / "SKILL.md").stat()
            except OSError:
                entries.append((entry.name, entry_mtime_ns, None, None))
                continue
            entries.append((entry.name, entry_mtime_ns, stat.st_mtime_ns, stat.st_size))
        fingerprint.append((directory.stat().st_mtime_ns, tuple(entries)))
    return tuple(fingerprint)


def _build_index(roots: ProjectRoots) -> ProjectSkillIndex:
    return ProjectSkillIndex(read_project_skills(roots))


def _cache_key(roots: ProjectRoots) -> tuple[tuple[Path, ...], Path]:
    """The directories plus the boundary the index is built under.

    Containment inside a build reads ``roots.boundary``, so two working
    directories that collect the same resolved directories under different git
    roots see different skills; a key without the boundary would let whichever
    build landed first decide for both.
    """
    return (roots.dirs, roots.boundary)


class ProjectSkillCache:
    """Process-wide cache of project skill indexes, keyed by the directory tuple
    and its boundary.

    A build runs in a worker thread, one per key at a time; the caller waits for
    it at most ``wait_s`` seconds and otherwise gets the previous index, or
    ``None`` for a cold project, while the build finishes into the cache.
    """

    def __init__(
        self,
        *,
        max_entries: int = 8,
        build: Callable[[ProjectRoots], ProjectSkillIndex] | None = None,
    ) -> None:
        if max_entries < 0:
            raise ValueError("max_entries must be zero or greater")
        self._max_entries = max_entries
        self._build = build or _build_index
        self._entries: OrderedDict[tuple[tuple[Path, ...], Path], tuple[tuple, ProjectSkillIndex]] = OrderedDict()
        self._inflight: dict[tuple[tuple[Path, ...], Path], asyncio.Task[ProjectSkillIndex | None]] = {}
        self._lock = threading.Lock()

    async def index_for(self, roots: ProjectRoots, *, wait_s: float | None) -> ProjectSkillIndex | None:
        """Return the project's current index, waiting at most ``wait_s`` for a build."""
        key = _cache_key(roots)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(asyncio.to_thread(self._refresh, roots))
            self._inflight[key] = task

            def _discard(done: asyncio.Task[ProjectSkillIndex | None]) -> None:
                # Retrieve the outcome so a build whose caller timed out does not
                # log "Task exception was never retrieved"; a later awaiter still sees it.
                if not done.cancelled():
                    done.exception()
                self._inflight.pop(key, None)

            task.add_done_callback(_discard)
        if wait_s is None:
            return await asyncio.shield(task)
        try:
            return await asyncio.wait_for(asyncio.shield(task), wait_s)
        except TimeoutError:
            return self._cached_index(key)

    def _cached_index(self, key: tuple[tuple[Path, ...], Path]) -> ProjectSkillIndex | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry[1]

    def _refresh(self, roots: ProjectRoots) -> ProjectSkillIndex | None:
        key = _cache_key(roots)
        try:
            fingerprint = project_fingerprint(roots)
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry[0] == fingerprint:
                    self._entries.move_to_end(key)
                    return entry[1]
            index = self._build(roots)
        except OSError:
            with self._lock:
                self._entries.pop(key, None)
            return None
        with self._lock:
            self._entries[key] = (fingerprint, index)
            self._entries.move_to_end(key)
            while self._entries and len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return index


_SHARED_CACHE: ProjectSkillCache | None = None
_SHARED_CACHE_LOCK = threading.Lock()


def shared_project_skill_cache() -> ProjectSkillCache:
    """Return the process-wide project skill cache."""
    global _SHARED_CACHE
    with _SHARED_CACHE_LOCK:
        if _SHARED_CACHE is None:
            _SHARED_CACHE = ProjectSkillCache()
        return _SHARED_CACHE


class ProjectSkills:
    """The project skills of the working directory the running turn is bound to.

    The working directory and the live switch arrive as readers rather than
    values: one router serves every session of a gateway, so a turn in another
    project, or a switch thrown on the settings page, must reach the next
    search. The default cache is the process-wide one, so every session in one
    checkout shares a single index.
    """

    def __init__(
        self,
        *,
        agent_home: Path,
        workdir_reader: Callable[[], Path | None],
        enabled_reader: Callable[[], bool] | None = None,
        cache: ProjectSkillCache | None = None,
    ) -> None:
        self._agent_home = agent_home
        self._workdir_reader = workdir_reader
        self._enabled_reader = enabled_reader
        self._cache = cache if cache is not None else shared_project_skill_cache()

    def enabled(self) -> bool:
        """Whether the live switch allows project skills right now."""
        return self._enabled_reader is None or self._enabled_reader()

    def roots(self) -> ProjectRoots | None:
        """The project's skill directories, or ``None`` when off, unbound or empty."""
        if not self.enabled():
            return None
        workdir = self._workdir_reader()
        if workdir is None:
            return None
        try:
            return project_skill_roots(workdir, agent_home=self._agent_home)
        except (OSError, RuntimeError):
            # Path.resolve() raises RuntimeError on a symlink cycle under 3.12.
            return None

    async def index(self, *, wait_s: float | None) -> ProjectSkillIndex | None:
        """The project's index, waiting at most ``wait_s`` for a cold build."""
        roots = self.roots()
        if roots is None:
            return None
        return await self._cache.index_for(roots, wait_s=wait_s)

    async def lookup(self, name: str) -> tuple[ProjectRoots | None, SkillMeta | None]:
        """Resolve one project skill by name, waiting for a cold build to finish."""
        roots = self.roots()
        if roots is None:
            return None, None
        index = await self._cache.index_for(roots, wait_s=None)
        if index is None:
            return roots, None
        return roots, index.get(name)


__all__ = [
    "PROJECT_SKILLS_DIR",
    "ProjectRoots",
    "ProjectSkillCache",
    "ProjectSkillIndex",
    "ProjectSkills",
    "project_fingerprint",
    "project_skill_roots",
    "read_project_skills",
    "shared_project_skill_cache",
]
