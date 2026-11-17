# Project Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raven's leader agent finds, reads and uses the skills in a project's `.agents/skills`, resolved per turn from the turn's working directory.

**Architecture:** A fourth `SkillForgeRouter` source, `project`, reads the turn's working directory through an injected reader, walks to the git root collecting `.agents/skills` directories, and searches a per-project BM25 index held in a process-wide LRU cache that rebuilds off the event loop. `read_skill` and `use_skill` resolve `project/<name>` against the same per-turn project, never from the router's held hits.

**Tech Stack:** Python 3.12, asyncio, pydantic config models, `raven.utils.bm25`, pytest.

**Spec:** `docs/specs/2026-11-17-project-skills-design.md`

## Global Constraints

- Source name `project`; skill id `project/<name>`, where `<name>` is `SkillMeta.name`.
- Config key `skillForge.projectSkills.enabled` (snake `skill_forge.project_skills.enabled`), boolean, default `true`, read live.
- Router weight key `skillForge.router.weights.project`, default `0.96`, applied per key (a `weights` dict without `project` still yields 0.96).
- A search waits on a project build at most `0.3` s; the cache holds at most `8` projects (LRU).
- Project skills are never `always`-injected and ignore `inject`; they are searchable regardless of either key.
- `raven.memory_engine` gains no import of `raven.agent`: the working directory reaches it as an injected `Callable[[], Path | None]`.
- Skill tool descriptions (`read_skill`, `use_skill`, `find_skill`) are not edited; the menu shows `project/<name>` verbatim.
- Every new file starts with an English module docstring (AGENTS.md 1.1); no other comments unless they say why.
- Tests run as `uv run --frozen --all-extras pytest <files> -q`, never with `-n 8` (CPU budget on the shared box).
- No commit, push or PR until the owner says so (AGENTS.md 3.4). Each task ends at a green checkpoint, not a commit.

## Review Focus

1. A `SKILL.md` that is not valid UTF-8, or unreadable, inside `.agents/skills`: that skill is skipped and its siblings are still indexed (Task 1).
2. A project skill deleted after it was indexed, then read: `read_skill` answers with a miss, not an exception (Task 5).
3. A session pinned to a working directory that has since been deleted: the project source returns no hits and logs nothing at warning level (Task 4).
4. The same name as a local skill and a project skill: the menu shows `local/<name>`, and `read_skill("project/<name>")` still returns the project body (Tasks 4 and 5).
5. The switch turned off mid-session: the next search offers no project skill, and `read_skill("project/<name>")` refuses with a message naming the key (Task 5).

---

### Task 1: Find a project's skill directories and read their skills

**Files:**
- Modify: `raven/memory_engine/skill_local/registry.py:447-490` (extract the body of `SkillRegistry._build_meta`)
- Create: `raven/memory_engine/skill_forge/project_skills.py`
- Test: `tests/test_skill_forge_project_skills.py` (create)

**Interfaces:**
- Produces, in `raven/memory_engine/skill_local/registry.py`:
  `build_skill_meta(skill_dir: Path, source: str, *, always_enabled: bool = True) -> SkillMeta | None` (module level; `SkillRegistry._build_meta` becomes a one-line delegate).
- Produces, in `raven/memory_engine/skill_forge/project_skills.py`:
  - `PROJECT_SKILLS_DIR: Path = Path(".agents") / "skills"`
  - `@dataclass(frozen=True) class ProjectRoots: dirs: tuple[Path, ...]; boundary: Path` (`dirs` resolved, nearest first; `boundary` is the git root, or the working directory when there is none)
  - `project_skill_roots(workdir: Path, *, agent_home: Path, home: Path | None = None) -> ProjectRoots | None` (`home` defaults to `Path.home()`; `None` when no directory qualifies)
  - `read_project_skills(roots: ProjectRoots) -> list[SkillMeta]` (source `"project"`, `always=False`, `inject="full"`)

- [ ] **Step 1: Write the failing tests**

Fixtures build trees under `tmp_path` with a helper `_skill(root: Path, name: str, body: str = "...", front: str = "") -> Path` that writes `root/<name>/SKILL.md` with `name:` and `description:` frontmatter. Each test passes `home=tmp_path / "home"` and an `agent_home` under it so the real home is never read.

```python
def test_walk_collects_from_workdir_up_to_git_root_nearest_first(tmp_path): ...
    # repo/.git (dir), repo/.agents/skills, repo/pkg/.agents/skills; workdir = repo/pkg/sub
    assert roots.dirs == ((repo / "pkg/.agents/skills").resolve(), (repo / ".agents/skills").resolve())
    assert roots.boundary == repo.resolve()

def test_a_git_file_marks_a_linked_worktree_root(tmp_path): ...
    # repo/.git is a file "gitdir: ..."; parent of repo also has .agents/skills
    assert roots.dirs == ((repo / ".agents/skills").resolve(),)

def test_without_git_only_the_workdir_is_read(tmp_path): ...
    # parent/.agents/skills and parent/work/.agents/skills, no .git anywhere; workdir = parent/work
    assert roots.dirs == ((parent / "work/.agents/skills").resolve(),)
    assert roots.boundary == (parent / "work").resolve()

def test_home_and_agent_home_ancestors_are_never_project_dirs(tmp_path): ...
    # home/.git, home/.agents/skills, home/.raven/.agents/skills; agent_home = home/.raven/workspace
    # workdir = home/.raven/tmp/web (no .agents/skills there)
    assert project_skill_roots(workdir, agent_home=agent_home, home=home) is None

def test_no_directory_found_is_none(tmp_path): ...
    assert project_skill_roots(tmp_path / "w", agent_home=..., home=...) is None

def test_a_symlinked_agents_dir_outside_the_boundary_is_skipped(tmp_path): ...
    # repo/.agents -> elsewhere/.agents (outside repo)
    assert project_skill_roots(repo, ...) is None

def test_nearer_skill_wins_a_name_collision(tmp_path): ...
    # "fmt" in repo/.agents/skills (body "outer") and repo/pkg/.agents/skills (body "inner")
    skills = read_project_skills(roots)
    assert [s.content.strip() for s in skills if s.name == "fmt"] == ["inner"]

def test_a_symlinked_skill_outside_the_boundary_is_skipped(tmp_path): ...
    # repo/.agents/skills/ext -> elsewhere/ext ; repo/.agents/skills/own -> repo/tools/own
    assert {s.name for s in read_project_skills(roots)} == {"own"}

def test_always_and_inject_frontmatter_are_ignored(tmp_path): ...
    # front: "always: true\ninject: description"
    (meta,) = read_project_skills(roots)
    assert meta.always is False and meta.inject == "full" and meta.source == "project"

def test_only_the_flat_layout_counts(tmp_path): ...
    # repo/.agents/skills/group/deep/SKILL.md and repo/.agents/skills/top/SKILL.md
    assert {s.name for s in read_project_skills(roots)} == {"top"}

def test_an_unreadable_skill_file_is_skipped(tmp_path): ...  # Review Focus 1
    # bad/SKILL.md written as b"\xff\xfe\x00bad"; good/SKILL.md valid
    assert {s.name for s in read_project_skills(roots)} == {"good"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen --all-extras pytest tests/test_skill_forge_project_skills.py -q`
Expected: collection error, `ModuleNotFoundError: raven.memory_engine.skill_forge.project_skills`.

- [ ] **Step 3: Extract `build_skill_meta` and write the module**

Move `_build_meta`'s body to the module-level `build_skill_meta` unchanged; the method calls it.

`project_skill_roots` decides the walk exactly as follows:

```python
start = workdir.resolve()
chain = [start, *start.parents]
git_root = next((d for d in chain if (d / ".git").exists()), None)
walk = chain[: chain.index(git_root) + 1] if git_root is not None else [start]
boundary = git_root or start
home_r = (home or Path.home()).resolve()
excluded = {home_r, agent_home.resolve(), *agent_home.resolve().parents}
dirs = []
for d in walk:
    if d in excluded:
        continue
    cand = d / PROJECT_SKILLS_DIR
    if cand.is_dir() and cand.resolve().is_relative_to(boundary):
        dirs.append(cand.resolve())
return ProjectRoots(tuple(dirs), boundary) if dirs else None
```

`read_project_skills` iterates `roots.dirs` in order, each directory's entries in sorted name order; an entry counts when it is a directory holding `SKILL.md` and `entry.resolve().is_relative_to(roots.boundary)`. It calls `build_skill_meta(entry, "project", always_enabled=False)`, replaces `inject` with `"full"` (`dataclasses.replace`), and keeps the first meta per `name`. Any `OSError` on one entry skips that entry.

- [ ] **Step 4: Run the new tests and the registry's existing tests**

Run: `uv run --frozen --all-extras pytest tests/test_skill_forge_project_skills.py tests/test_skill_forge_local_pool.py tests/test_skill_router_local_source.py -q`
Expected: all pass.

- [ ] **Step 5: Checkpoint** (no commit; AGENTS.md 3.4)

---

### Task 2: Per-project index and the shared cache

**Files:**
- Modify: `raven/memory_engine/skill_forge/project_skills.py`
- Test: `tests/test_skill_forge_project_skills.py`

**Interfaces:**
- Consumes: `ProjectRoots`, `read_project_skills` (Task 1).
- Produces, in `project_skills.py`:
  - `class ProjectSkillIndex: def __init__(self, metas: list[SkillMeta]) -> None`; `metas: tuple[SkillMeta, ...]`; `def get(self, name: str) -> SkillMeta | None`; `def search(self, query: str, top_k: int) -> list[tuple[SkillMeta, float]]` (positive BM25 scores only, best first)
  - `def project_fingerprint(roots: ProjectRoots) -> tuple` (each dir's `st_mtime_ns`, and per skill entry its name plus `SKILL.md`'s `st_mtime_ns` and `st_size`)
  - `class ProjectSkillCache: def __init__(self, *, max_entries: int = 8, build: Callable[[ProjectRoots], ProjectSkillIndex] | None = None) -> None`; `async def index_for(self, roots: ProjectRoots, *, wait_s: float | None) -> ProjectSkillIndex | None`
  - `def shared_project_skill_cache() -> ProjectSkillCache` (one per process)

- [ ] **Step 1: Write the failing tests**

`build` is injectable so a test can hold a build open with a `threading.Event` and count builds.

```python
def test_index_search_ranks_by_bm25_and_get_by_name(): ...
    idx = ProjectSkillIndex([meta("pdf-tools", "Extract text from PDF files"), meta("weather", "Forecasts")])
    assert [m.name for m, _ in idx.search("pdf text", 5)] == ["pdf-tools"]
    assert idx.get("weather").name == "weather" and idx.get("nope") is None

async def test_a_cold_build_past_the_wait_returns_none_then_serves_the_next_call(tmp_path): ...
    # build blocks on gate; first call wait_s=0.05 -> None; gate.set(); second call wait_s=None -> index with the skill

async def test_a_changed_project_is_rebuilt(tmp_path): ...
    # index_for(wait_s=None) -> {"a"}; add skill "b"; index_for(wait_s=None) -> names {"a", "b"}

async def test_a_slow_rebuild_serves_the_previous_index(tmp_path): ...
    # first build fast; add a skill; second build blocks; index_for(wait_s=0.05) -> the old index (names {"a"})

async def test_concurrent_calls_share_one_build(tmp_path): ...
    # two index_for(wait_s=None) gathered while the build is blocked -> builds == 1

async def test_the_least_recently_used_project_is_evicted(tmp_path): ...
    # max_entries=2; projects p1, p2, p3 in turn; p1 again -> builds == 4
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen --all-extras pytest tests/test_skill_forge_project_skills.py -q`
Expected: the six new tests fail with `ImportError` on `ProjectSkillIndex` / `ProjectSkillCache`.

- [ ] **Step 3: Implement the index and the cache**

`ProjectSkillIndex` builds its corpus with `raven.memory_engine.skill_local.local_pool._format_skill_text` and `raven.utils.bm25.BM25Okapi` / `tokenize`, so both pools rank alike.

`index_for` decides the concurrency exactly as follows (entries are `(fingerprint, index)` keyed by `roots.dirs`, in an `OrderedDict` guarded by a `threading.Lock`; in-flight work is a `dict[key, asyncio.Task]`):

```python
key = roots.dirs
task = self._inflight.get(key)
if task is None:
    task = asyncio.ensure_future(asyncio.to_thread(self._refresh, roots))
    self._inflight[key] = task
    task.add_done_callback(lambda _t: self._inflight.pop(key, None))
if wait_s is None:
    return await asyncio.shield(task)
try:
    return await asyncio.wait_for(asyncio.shield(task), wait_s)
except TimeoutError:
    return self._cached_index(key)  # previous index, or None for a cold project
```

`_refresh` (worker thread) computes `project_fingerprint(roots)`; if it equals the cached entry's, it returns that index; otherwise it runs `build(roots)` (default: `ProjectSkillIndex(read_project_skills(roots))`), stores the entry, marks it most recent, and evicts the oldest past `max_entries`. An `OSError` from either step (a directory removed between the walk and the stat) drops the key's entry and returns `None`, so a vanished project never surfaces as a router warning.

- [ ] **Step 4: Run the tests**

Run: `uv run --frozen --all-extras pytest tests/test_skill_forge_project_skills.py -q`
Expected: all pass.

- [ ] **Step 5: Checkpoint** (no commit)

---

### Task 3: The switch and the weight

**Files:**
- Modify: `raven/config/raven.py` (new `ProjectSkillsConfig` beside `LocalDirConfig`; field on `SkillForgeConfig`; `SkillForgeRouterConfig.weights` default and docstrings)
- Modify: `raven/config/live.py` (after `skill_hub_enabled`)
- Test: `tests/test_config_raven_sections.py`, `tests/test_config_live.py`

**Interfaces:**
- Produces: `class ProjectSkillsConfig(_Base): enabled: bool = True`; `SkillForgeConfig.project_skills: ProjectSkillsConfig` (alias `projectSkills`); `SkillForgeRouterConfig().weights == {"local": 0.96, "everos": 0.9, "hub": 0.85, "project": 0.96}`.
- Produces, in `raven/config/live.py`: `def project_skills_on(raw: Any) -> bool`; `def project_skills_enabled(live: LiveConfig) -> bool` (cached per parsed file, the way `skill_hub_enabled` caches).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config_raven_sections.py
def test_project_skills_default_on():
    assert SkillForgeConfig().project_skills.enabled is True

def test_project_skills_reads_the_camel_key():
    assert SkillForgeConfig.model_validate({"projectSkills": {"enabled": False}}).project_skills.enabled is False

def test_router_weights_carry_the_project_source():
    assert SkillForgeRouterConfig().weights["project"] == 0.96

# tests/test_config_live.py, class TestProjectSkillsEnabled (same _live helper as the Hub class)
def test_no_block_is_on(self, tmp_path): assert project_skills_enabled(self._live(tmp_path, {})) is True
def test_an_explicit_off_is_off(self, tmp_path):
    assert project_skills_enabled(self._live(tmp_path, {"skillForge": {"projectSkills": {"enabled": False}}})) is False
def test_the_snake_spelling_is_read(self, tmp_path):
    assert project_skills_enabled(self._live(tmp_path, {"skill_forge": {"project_skills": {"enabled": False}}})) is False
def test_an_invalid_block_reads_as_the_default(self, tmp_path):
    assert project_skills_enabled(self._live(tmp_path, {"skillForge": {"projectSkills": {"enabled": "maybe"}}})) is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen --all-extras pytest tests/test_config_raven_sections.py tests/test_config_live.py -q`
Expected: the seven new tests fail (`AttributeError` / `ImportError`).

- [ ] **Step 3: Implement**

`project_skills_on` picks the `skillForge` / `skill_forge` block the way `skill_hub_on` does (after `_migrate_config`), then `ProjectSkillsConfig.model_validate(block.get("projectSkills", block.get("project_skills")) or {})`; a validation error or a non-dict block reads as `True`. Docstrings: `ProjectSkillsConfig.enabled` states what it turns on and that it is read live; `SkillForgeConfig` and `SkillForgeRouterConfig` say four sources and name Project.

- [ ] **Step 4: Run the tests**

Run: `uv run --frozen --all-extras pytest tests/test_config_raven_sections.py tests/test_config_live.py tests/test_config_raven_loader.py -q`
Expected: all pass.

- [ ] **Step 5: Checkpoint** (no commit)

---

### Task 4: The `project` router source, its name-collision rule, and its wiring

**Files:**
- Modify: `raven/memory_engine/skill_forge/project_skills.py` (add `ProjectSkills`)
- Create: `raven/memory_engine/skill_forge/project_source.py`
- Modify: `raven/memory_engine/skill_forge/__init__.py` (export `ProjectSkillSource`, `ProjectSkills`)
- Modify: `raven/memory_engine/skill_forge/fusion.py:82-92` (representative rule)
- Modify: `raven/context_engine/factory.py:122-129, 214-286` (`_build_router` gains the source)
- Test: `tests/test_skill_router_project_source.py` (create), `tests/test_skill_router_fusion.py`, `tests/test_context_engine_factory.py`

**Interfaces:**
- Consumes: `ProjectRoots`, `project_skill_roots`, `ProjectSkillCache`, `shared_project_skill_cache` (Tasks 1-2); `project_skills_enabled` (Task 3).
- Produces, in `project_skills.py`:
  `class ProjectSkills: def __init__(self, *, agent_home: Path, workdir_reader: Callable[[], Path | None], enabled_reader: Callable[[], bool] | None = None, cache: ProjectSkillCache | None = None) -> None`; `def enabled(self) -> bool`; `def roots(self) -> ProjectRoots | None` (`None` when off, unbound, or nothing found; an `OSError` while walking also yields `None`); `async def index(self, *, wait_s: float | None) -> ProjectSkillIndex | None`; `async def lookup(self, name: str) -> tuple[ProjectRoots | None, SkillMeta | None]` (waits for the build; `(None, None)` without roots; `(roots, None)` on a miss).
- Produces, in `project_source.py`: `class ProjectSkillSource: name = "project"; weight: float = 0.96; def __init__(self, project: ProjectSkills, *, wait_s: float = 0.3) -> None; async def search(self, query: str, history: list[dict[str, Any]], k: int) -> list[RouterHit]`. Each hit: `qualified_id=f"project/{meta.name}"`, `name=meta.name`, `content=meta.content`, `score=<bm25>`, `meta={"source": "project", "description": meta.description, "path": str(meta.path)}`.
- Produces: `_build_router(..., project_enabled_reader: Callable[[], bool] | None = None)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_skill_router_project_source.py
def test_conforms_to_the_source_protocol(tmp_path):
    assert isinstance(ProjectSkillSource(_project(tmp_path, workdir=None)), ForgeSkillSource)

async def test_no_bound_workdir_returns_no_hits(tmp_path): assert await src.search("pdf", [], 5) == []
async def test_switch_off_returns_no_hits(tmp_path): ...  # enabled_reader=lambda: False
async def test_hits_are_project_qualified_and_carry_the_body(tmp_path): ...
    (hit,) = await src.search("pdf text", [], 5)
    assert hit.qualified_id == "project/pdf-tools" and "Extract" in hit.content and hit.meta["source"] == "project"
async def test_two_workdirs_see_only_their_own_skills(tmp_path): ...  # G3
    # one ProjectSkills over a shared cache; the reader returns repo_a then repo_b
    assert names_a == {"alpha"} and names_b == {"beta"}
async def test_a_deleted_workdir_returns_no_hits_quietly(tmp_path, caplog): ...  # Review Focus 3
    assert await src.search("x", [], 5) == [] and not [r for r in caplog.records if r.levelno >= logging.WARNING]
async def test_a_cold_build_does_not_hold_up_the_router(tmp_path): ...
    # SkillForgeRouter(sources=[<fake local source returning local/x>, ProjectSkillSource(blocked build)])
    hits = await asyncio.wait_for(router.select("x", [], k=3), 1.0)
    assert [h.qualified_id for h in hits] == ["local/x"]
async def test_the_scent_menu_offers_the_bound_project_skill(tmp_path): ...
    # router from _build_router(builder=ContextBuilder(workspace=agent_home), ...); a fat message (as in test_context_scent.py)
    with workdir.bind(repo):
        result = await ScentMenu(router).build(message, [])
    assert "project/pdf-tools" in result.text

# tests/test_skill_router_fusion.py
def test_a_local_hit_represents_a_name_shared_with_a_project_hit():  # Review Focus 4
    # project hit "x" score 9.0, local hit "x" score 0.1; run with both source orders
    assert out[0].qualified_id == "local/x"
    assert set(out[0].meta["contributing_sources"]) == {"local", "project"}
def test_other_collisions_still_keep_the_higher_score():
    # local "x" 0.1 vs hub "x" 9.0 -> representative is hub/x, as today

# tests/test_context_engine_factory.py
def test_router_mounts_the_project_source_at_its_default_weight(tmp_path):
    # weights={"local": 1.0} (no project key)
    assert {s.name: s.weight for s in router._sources}["project"] == 0.96
def test_router_project_weight_follows_config(tmp_path):
    # weights={"project": 0.5} -> 0.5
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen --all-extras pytest tests/test_skill_router_project_source.py tests/test_skill_router_fusion.py tests/test_context_engine_factory.py -q`
Expected: the new tests fail (`ImportError` for the source; the fusion test picks `project/x`; no `project` source in the factory).

- [ ] **Step 3: Implement**

- `ProjectSkills.roots()` returns `None` when `enabled()` is false or `workdir_reader()` is `None`; otherwise `project_skill_roots(workdir, agent_home=self._agent_home)` inside `try/except OSError: return None`. `index()` returns `None` when `roots()` is `None`, else `await self._cache.index_for(roots, wait_s=wait_s)`. `cache` defaults to `shared_project_skill_cache()`.
- `ProjectSkillSource.search` ignores `history`, calls `await self._project.index(wait_s=self._wait_s)`, and maps `index.search(query, k)` to hits.
- `fusion.py`: when a key already has a representative, a `local` hit replaces a `project` representative, a `project` hit never replaces a `local` one, and every other pair keeps the existing higher-`score` rule. Track each representative's source name beside `best_hit`.
- `factory.py`: import `workdir` from `raven.agent` and `project_skills_enabled` from `raven.config.live`; in `_build_router`, after the Hub block, build `project_source = ProjectSkillSource(ProjectSkills(agent_home=builder.workspace, workdir_reader=workdir.current, enabled_reader=project_enabled_reader))`, set `project_source.weight = float(weights.get("project", 0.96))` (the way `local_source.weight` is set), and append it. The call site passes `project_enabled_reader=lambda: project_skills_enabled(live)`. Update the `_build_router` docstring (one to four sources).

- [ ] **Step 4: Run the tests and the import contracts**

Run: `uv run --frozen --all-extras pytest tests/test_skill_router_project_source.py tests/test_skill_router_fusion.py tests/test_context_engine_factory.py tests/test_context_scent.py -q`
Expected: all pass.
Run: `make lint-imports`
Expected: every contract kept (no new `raven.memory_engine -> raven.agent` edge).

- [ ] **Step 5: Checkpoint** (no commit)

---

### Task 5: `read_skill` and `use_skill` resolve `project/` ids

**Files:**
- Modify: `raven/agent/tools/skill_hub.py` (`ReadSkillTool.__init__` / `execute`, `UseSkillTool.__init__` / `execute`)
- Modify: `raven/agent/loop/wiring.py:1512-1550` (pass `project=` to both tools)
- Test: `tests/test_skill_hub_tools.py`

**Interfaces:**
- Consumes: `ProjectSkills` (Task 4); `project_skills_enabled` (Task 3); `render_skill_body`; `lint_external_paths`.
- Produces: `ReadSkillTool(..., project: ProjectSkills | None = None)` and `UseSkillTool(..., project: ProjectSkills | None = None)`.

- [ ] **Step 1: Write the failing tests**

A helper builds `ProjectSkills(agent_home=..., workdir_reader=lambda: repo, enabled_reader=lambda: flag)` over a fresh `ProjectSkillCache()`.

```python
async def test_read_skill_reads_a_project_skill_from_the_bound_project(tmp_path):
    # body links [guide](references/guide.md) and that file exists: render_skill_body
    # names the directory only when a bundled reference resolves (catalog.py:85-93)
    out = await ReadSkillTool(project=proj).execute(skill_id="project/pdf-tools")
    assert "Extract" in out and str(repo / ".agents/skills/pdf-tools/references/guide.md") in out

async def test_read_skill_project_miss_does_not_fall_back_to_held_hits(tmp_path):
    # held_hit returns RouterHit(qualified_id="project/ghost", content="HELD BODY", ...)
    out = await ReadSkillTool(project=proj, held_hit=lambda q: held).execute(skill_id="project/ghost")
    assert out.startswith("Error: no project skill 'ghost'") and "HELD BODY" not in out

async def test_read_skill_project_flags_foreign_home_paths_as_a_note(tmp_path):
    # body mentions ~/.config/opencode
    assert "note:" in out and "~/.config/opencode" in out and "Error" not in out

async def test_read_skill_project_respects_the_blocklist(tmp_path):
    out = await ReadSkillTool(project=proj, blocklist=["pdf-tools"]).execute(skill_id="project/pdf-tools")
    assert "blocklist" in out

async def test_read_skill_project_when_switched_off(tmp_path):  # Review Focus 5
    assert "skillForge.projectSkills.enabled" in out and out.startswith("Error")

async def test_read_skill_project_skill_deleted_after_indexing(tmp_path):  # Review Focus 2
    # read once (indexed), shutil.rmtree the skill dir, read again
    assert second.startswith("Error: no project skill 'pdf-tools'")

async def test_read_skill_reads_both_ids_of_a_shared_name(tmp_path):  # Review Focus 4
    # local registry and project both hold "fmt" with different bodies
    assert "LOCAL BODY" in local_out and "PROJECT BODY" in project_out

async def test_a_tool_without_project_access_refuses_project_ids(tmp_path):
    assert (await ReadSkillTool().execute(skill_id="project/x")).startswith("Error: project skills are not available")

async def test_use_skill_project_returns_the_scripts_dir(tmp_path):
    # pdf-tools/scripts/ exists
    assert f"scripts_dir: {repo / '.agents/skills/pdf-tools/scripts'}" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen --all-extras pytest tests/test_skill_hub_tools.py -q`
Expected: the nine new tests fail (`TypeError: unexpected keyword argument 'project'`).

- [ ] **Step 3: Implement**

In both `execute` methods, after the allow-list and bare-name blocklist checks and before the `source != "hub"` branch, `if source == "project": return await self._read_project(native)` (and `_use_project` in `UseSkillTool`). Exact replies:

| condition | reply |
|---|---|
| tool built without `project` | `Error: project skills are not available to this agent.` |
| switch off | `Error: project skills are turned off (skillForge.projectSkills.enabled is false).` |
| no roots | `Error: no project skill {native!r}: this turn's working directory has no .agents/skills directory up to its git root.` |
| index miss | `Error: no project skill {native!r} found. Project ids cover only the skills under {", ".join(str(d) for d in roots.dirs)}.` |

Resolution checks `project.enabled()` for the switch-off reply, then awaits `project.lookup(native)`. A hit passes the blocklist check on `meta.name`; `read_skill` returns `render_skill_body(meta)`, prefixed by the Hub read's note line when `lint_external_paths(meta.content)` flags anything; `use_skill` returns the same three shapes `_use_on_disk` returns for a local meta. Neither path consults `held_hit`.

In `wiring.py`, build one `ProjectSkills(agent_home=self.workspace, workdir_reader=workdir.current, enabled_reader=lambda: project_skills_enabled(self._live_config))` (the shared cache is the default) and pass it as `project=` to `ReadSkillTool` and `UseSkillTool`. Tool descriptions stay as they are.

- [ ] **Step 4: Run the tests**

Run: `uv run --frozen --all-extras pytest tests/test_skill_hub_tools.py tests/test_skill_hub_policy.py tests/test_skill_forge_blocklist_live.py -q`
Expected: all pass.

- [ ] **Step 5: Checkpoint** (no commit)

---

### Task 6: Terms and documentation

**Files:**
- Modify: `CONTEXT.md` (SkillForge entry near line 1915; Skill Discovery near line 1929; new **Project skill** entry after Skill Discovery)
- Modify: `docs-site/docs/skills-and-extensions.md`, `docs-site/docs/skills-and-extensions.zh.md` (new subsection under "Discover and manage skills")

**Interfaces:**
- Consumes: the behavior of Tasks 1-5; the definitions must match the code as merged.

- [ ] **Step 1: Write the entries**

- `CONTEXT.md` **Project skill**: a skill directory (`<name>/SKILL.md`) under a `.agents/skills` directory found from the turn's working directory up to its git root (`memory_engine/skill_forge/project_skills.py`), offered as `project/<name>` by the `project` router source; never `always`-injected; read fresh per turn, never from held hits; skipped for the user's home and agent home's ancestors; leader agent only; switch `skillForge.projectSkills.enabled`. _Avoid_: "workspace skill" (agent home's `skills/` layer).
- SkillForge: "three sources" becomes four, naming project skills.
- Skill Discovery: one sentence on `project/` ids and their per-turn resolution.
- Docs page, both languages: "Skill directories" subsection stating agent home's `skills/` (default `~/.raven/workspace/skills`, moved by `RAVEN_HOME` and `--home`) and the project's `.agents/skills` (the walk, nearest wins, the switch, not injected, leader agent only, the symlink rule), with one install example: `gh skill install <owner>/<repo> skills/<name>/SKILL.md --dir .agents/skills`.

- [ ] **Step 2: Verify**

Run: `make docs-build`
Expected: the strict build succeeds. (If the main environment cannot build the site, use the docs-site run recipe and record which one was used.)
Run: `make check-source-language check-large-files`
Expected: both exit 0.

- [ ] **Step 3: Checkpoint** (no commit)

---

### Task 7: Whole-branch verification and the approval gate

**Files:** none new.

- [ ] **Step 1: Run every touched suite together**

Run: `uv run --frozen --all-extras pytest tests/test_skill_forge_project_skills.py tests/test_skill_router_project_source.py tests/test_skill_router_fusion.py tests/test_skill_router_local_source.py tests/test_skill_forge_local_pool.py tests/test_context_engine_factory.py tests/test_context_scent.py tests/test_skill_hub_tools.py tests/test_config_live.py tests/test_config_raven_sections.py tests/test_config_raven_loader.py -q`
Expected: all pass.

- [ ] **Step 2: Lint**

Run: `make lint-python lint-imports lint-types`
Expected: exit 0.

- [ ] **Step 3: A live check in a real checkout**

In a scratch git repo holding `.agents/skills/<name>/SKILL.md`, run one `raven agent` turn from that directory, on the configured test model (deepseek-flash, never a premium model), with a message that names the skill's subject. Expected: the turn's trace shows `project/<name>` in the scent menu, and a `read_skill("project/<name>")` call returns its body. Record the command and the trace lines.

- [ ] **Step 4: Pre-submit sweep**

Run the `pr-review-patterns` sweep over `git diff origin/main...HEAD` and fix what it finds.

- [ ] **Step 5: Approval gate**

Show the owner the diff summary, the exact commit message, and the PR description (ASCII-checked with `uv run python -m scripts.check_pr_body <file>`). Commit, push and open the PR only on the owner's word.
