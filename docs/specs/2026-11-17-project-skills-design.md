# Project skills: reading `.agents/skills` from the turn's project

Date: 2026-11-17. Base: `origin/main` at `dc35a0507`.

## 1. Intent

Skill installers now describe an agent as two directories: where a skill goes for one
project, and where it goes for the user. GitHub CLI's `gh skill install --agent <id>`
keeps exactly that pair per agent (`internal/skills/registry/registry.go` in `cli/cli`:
`ProjectDir`, `UserDir`), and Vercel's `npx skills` does the same. Raven has the user
half (`<agent home>/skills`, by default `~/.raven/workspace/skills`) and no project half:
a skill installed into a repository is invisible to a Raven session working in that
repository.

The project directory most hosts already share is `.agents/skills`: in the `gh` registry
it is the project directory of GitHub Copilot, Cursor, Codex, Gemini CLI, OpenCode and
the generic "Universal" target. This change makes Raven read it, so that Raven can be
listed as a `gh skill` host (and in the other installers) with a project directory that
is true.

Success: after `gh skill install <repo> <skill> --dir <checkout>/.agents/skills` (and,
once Raven is listed, `--agent raven --scope project`), the next Raven turn whose working
directory is inside that checkout offers the skill in its scent menu, finds it with
`find_skill`, and reads it with `read_skill`, on every surface that runs turns.

## 2. Goals, non-goals, anti-goals

Goals:

- **G1.** The leader agent discovers project skills through the scent menu and
  `find_skill`, reads them with `read_skill`, and resolves their bundled files with
  `use_skill`.
- **G2.** It works on every surface that binds a working directory to a turn:
  `raven tui` / `raven agent` (the launch directory), `raven gateway` sessions (the
  channel directory, or a directory pinned from the web UI), and ACP sessions (the
  client's cwd, `acp/methods.py:391-397`).
- **G3.** Two sessions working in two projects at the same time never see each other's
  project skills.
- **G4.** On by default; one switch turns it off without a restart.
- **G5.** The documentation site states both of Raven's skill directories, so an
  installer maintainer has a page to point at.

Non-goals (this change):

- Sub-agents and DAG nodes. A built-in sub-agent's prompt lists every skill it can see,
  with no cap (`subagent/backends/raven_loop.py:191-201`); a project holding a full
  catalog install (thousands of skills) would flood it. They get their own design.
- The user-level `~/.agents/skills` (the Codex and "Universal" user directory). Reading
  it would pull in every skill installed for Codex.
- Other agents' project directories (`.claude/skills`, `.cursor/skills`, ...).
- The web UI's skill list, slash commands built from skills, and a settings-page toggle.
- The `gh` and `npx skills` registry submissions themselves; they follow a release.

Anti-goals (a failure even if everything else works):

- A project skill in `# Active Skills`, or any other part of the system prefix. Project
  files are repository content that Raven did not write.
- One session's project skills reaching another session's turn.
- Copying project skills into agent home. Nothing is installed or recorded in a ledger;
  the project's own files are the only copy.
- A first turn in a large project that waits on indexing beyond the scent menu's budget.
- Treating the user's home directory as a project, which would make `~/.agents/skills`
  a project directory whenever `~` is itself a git repository.

## 3. Constraints found in the code

- A turn's whole body runs inside `workdir.bind(turn_workdir)` (`agent/loop/turn_path.py`,
  around line 4662), so the scent menu and the skill tools can read the turn's working
  directory from `workdir.current()`.
- `SkillForgeRouter` fuses sources with weighted RRF (`memory_engine/skill_forge/router.py`,
  `fusion.py`). Sources today: `local` (0.96), the memory backend (0.9), `hub` (0.85)
  (`config/raven.py`, `SkillForgeRouterConfig.weights`). A hit's id is
  `<source name>/<native id>`.
- With the default `dedup_by: "name"`, a same-named skill from two sources collapses into
  one slot whose representative is the hit with the higher per-source score
  (`fusion.py:84-92`). Scores from two BM25 pools are not comparable, so today the winner
  of a cross-pool collision is arbitrary.
- `SkillRegistry` is one per process: agent home's `skills/`, then each
  `skillForge.localDirs` entry, then the packaged builtins. Its BM25 pool drops `always`
  skills from search because `# Active Skills` already carries them
  (`skill_local/local_pool.py:149`).
- `read_skill` sends every non-hub id to `lookup_on_disk(registry, source, native)`, and
  falls back to the router's held hits when that misses (`agent/tools/skill_hub.py:236-243`).
- The scent menu has a 2.0 s build budget (`context_engine/scent.py:48`) and wraps its
  lines as untrusted data.
- A working directory can be neither agent home, nor inside it, nor contain it
  (`agent/workdir.py`, `validate_override`).
- Raven reads no project file into the model's context today; this is the first.

## 4. Approaches

- **A. A fourth router source, `project`** (chosen). A source that reads
  `workdir.current()` on every search, finds the project's `.agents/skills` directories,
  and searches a per-project index. Ids are `project/<name>`; `read_skill` and `use_skill`
  resolve them against the current turn's project. Covers every surface in G2, and G3
  falls out of keying the index by project. Most new code of the three.
- **B. Add the project directory to the shared registry each turn.** Reuses the existing
  scan and index, but the registry and its index are shared by every session of a
  gateway, so two sessions in two projects would race over one index. Rejected (G3).
- **C. Mount once at startup.** If the launch directory holds `.agents/skills`, add it as
  a `localDirs`-style layer. Least code, but only terminal sessions benefit, and the
  layer would inherit the local tier's trust, `always` injection included. Rejected
  (G2, anti-goals).

The owner chose A, on by default, on 2026-11-17, with the scope in section 2.

## 5. Design

### 5.1 Term

**Project skill**: a skill directory (`<name>/SKILL.md`) under a `.agents/skills`
directory found from the turn's working directory (5.2). Its id is `project/<name>`.
Added to `CONTEXT.md` in the same change, next to Skill Discovery. _Avoid_: "workspace
skill", which is agent home's `skills/` layer.

### 5.2 Which directories are read

From the turn's working directory, walk up to the git root, inclusive, and collect each
`<dir>/.agents/skills` that is a directory. The git root is the nearest ancestor holding
a `.git` entry, file or directory (a linked worktree has a `.git` file). With no git root
above the working directory, only `<workdir>/.agents/skills` is read.

- No working directory bound (`workdir.current()` is `None`): no project skills.
- The walk never reads `.agents/skills` in the user's home directory (`Path.home()`), nor
  in any directory that is agent home or one of its ancestors (`~/.raven`, `~`).
- Order is nearest first. When two collected directories hold the same skill name, the
  nearer one is the project skill of that name.
- Layout is flat: `<root>/<name>/SKILL.md` only, the layout `gh skill`, `npx skills` and
  Codex all write. Deeper SKILL.md files are not project skills.
- A `<name>` entry that is a symlink counts only when its resolved target lies inside the
  git root (inside the working directory when there is none); the same test applies to a
  `.agents` or `.agents/skills` that is itself a symlink. (My call: a repository
  should not be able to point Raven at arbitrary files elsewhere on the machine.)

### 5.3 The project index

A process-wide cache of project indexes, keyed by the ordered tuple of collected
directories (so every session in the same checkout shares one entry, and two checkouts
never do).

- An entry holds the parsed skills of those directories and a BM25 index over them, built
  with the same tokenizer as the local pool. `always` and `inject` frontmatter are
  ignored when parsing: every project skill is searchable and none is injected. (The local
  pool's `always` exclusion would otherwise hide an `always: true` project skill
  entirely.)
- Freshness: before a search the entry compares a fingerprint of its directories (each
  directory's mtime plus each `SKILL.md`'s mtime and size) and rebuilds on a change. No
  file watcher.
- The fingerprint and the build run off the event loop (a project may sit on a network
  filesystem), one build per key at a time: a second search for a key that is already
  building waits on that build rather than starting another.
- A search waits for a build only briefly (0.3 s, my call), then returns without it: no
  project hits for a cold project, the previous index for a changed one. The build keeps
  going and stores its result in the cache itself, so the next search has it. This bound
  is load-bearing, not a tuning knob: `SkillForgeRouter.select` gathers every source with
  no per-source timeout (`router.py:116`), and the scent menu cancels the whole selection
  at 2.0 s, so a project source that waited on a long build would cost the turn its local
  and Hub hits too.
- The cache is shared by concurrent turns and guarded accordingly. It holds at most a
  small number of entries (8, my call) and evicts the least recently used.

### 5.4 The router source

A `ProjectSkillSource` beside `LocalSkillSource` in `memory_engine/skill_forge/`, named
`project`, wired in `context_engine/factory.py` with the other sources.

- `search` reads `workdir.current()` and the live switch (5.6); with either missing it
  returns no hits. Otherwise it resolves the project's directories (5.2), takes the cached
  index (5.3), and returns `RouterHit`s whose content is the SKILL.md body.
- Weight: `skillForge.router.weights.project`, default 0.96, equal to `local`. (My call:
  a project's own skills are as specific to the task as the user's hand-written ones.)
  A config file that already spells out `weights` without a `project` key still gets
  0.96: the default applies per key, not only when the whole dict is absent.
- Name collisions with another source (`dedup_by: "name"`): the `local` hit is the
  representative, not the higher raw score. The user's own skill is never displaced by a
  repository's skill of the same name; the project skill stays readable by its explicit
  `project/<name>` id. (My call; this changes the representative rule in `fusion.py` for
  this one pairing only.)

### 5.5 The skill tools

- `read_skill("project/<name>")` resolves against the current turn's project index and
  renders the body the way a local skill is rendered (`render_skill_body`: the directory
  named, bundled paths absolute). Above the body it adds the same note the Hub read adds
  when `lint_external_paths` flags another product's home directory; a flagged path is a
  note, not a refusal. A miss names the directories that were searched, and does **not**
  fall back to the router's held hits the way other non-local sources do: a held hit may
  come from another session's project (the router is shared), so serving it would break G3.
- `use_skill("project/<name>")` returns the skill's `scripts/` path inside the project.
  It downloads nothing and copies nothing.
- `find_skill` needs no change; it searches the same router.
- The operator blocklist (`skillForge.blocklist`) applies to project skills at every step,
  as it does to every other source. An agent's skill allow-list applies by name, as today.
- A project skill's scripts run only when the model runs them, through `exec`, under the
  ordinary fence and permission rules.

### 5.6 Configuration

`skillForge.projectSkills.enabled`, a boolean, default `true`. It is read live, like the
Hub switch (`config/live.py`, `skill_hub_enabled`): setting it to `false` stops the next
search, with no restart. A block rather than a bare boolean leaves room for later knobs.

`skillForge.router.enabled` is the router's own master switch, and it is read at boot:
where the factory builds the router, and again where the tool gate asks. `false` leaves
the engine with no router at all: no source is mounted, so the scent menu has nothing to
advertise and carries no menu either, and `find_skill` stays registered for the
explanation, but its schema now says skill retrieval is disabled in this deployment and a
call answers that it is not available. The tools take the same gate -- a host that throws
the switch mounts no `ProjectSkills`, so `read_skill("project/<name>")` and
`use_skill("project/<name>")` answer that project skills are not available to this agent,
the sentence a sub-agent's already gets. The memory-backend rebind reads the router per
switch and skips a missing one, so turning memory on live does not grow a router behind
the switch's back. What the switch does not reach is the on-disk registry: an `always` /
`inject` skill still rides the system prompt and `read_skill("local/<name>")` still
renders it, and the Hub lane still answers to its own switch (`skill_hub_enabled`).
Because it is read at build time, a deployment that wants it off sets it in the file and
restarts; that is the promise its docstring has made since 2026-06-28, and it is now what
holds.

### 5.7 Documentation

- `docs-site/docs/skills-and-extensions.md` and its `.zh.md` twin gain a short section
  stating both directories: agent home's `skills/` (default `~/.raven/workspace/skills`,
  moved by `RAVEN_HOME` and `--home`) and `.agents/skills` in the project, read from the
  working directory up to the git root, with the switch and the limits of 5.2 and 5.5.
  This is the page an installer's maintainer will be pointed at.
- `CONTEXT.md`: the new term (5.1); SkillForge's "three sources" becomes four; Skill
  Discovery gains the `project/` route.
- `SkillForgeConfig`'s and `SkillForgeRouterConfig`'s docstrings name the new source.

## 6. Left to the builder

- The fingerprint's exact form, and where the cache lives.
- Whether the project parser reuses `SkillRegistry` with a flag or is a small reader of
  its own, provided 5.2 and 5.3 hold.
- Test files and their names, under AGENTS.md section 5. The cases that must be covered:
  the walk (git root, a `.git` file, no git root, home and agent-home exclusion,
  nearest-wins, a symlink pointing outside); the index (rebuild on change, eviction,
  `always` ignored); the source (no bound directory, switch off, two working directories
  in one process seeing only their own skills); the representative rule on a name
  collision; `read_skill` and `use_skill` on `project/` ids, the lint note and the
  blocklist included.

## 7. Follow-ups

- After a release carries this: a feature request on `cli/cli` asking for
  `{ID: "raven", ProjectDir: ".agents/skills", UserDir: ".raven/workspace/skills"}`,
  honouring `RAVEN_HOME` the way `CLAUDE_CONFIG_DIR` and `PI_CODING_AGENT_DIR` are honoured
  there; and the matching entry in `vercel-labs/skills`.
- Sub-agent and DAG-node access to project skills, searchable or capped rather than
  listed in full.
