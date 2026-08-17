# Changelog

All notable changes to Raven are documented here.

## Unreleased

### Added

- `spawn` now records every sub-agent call on disk, the way `run_subagent_dag`
  already recorded every node: one directory per call under
  `<agent home>/sessions/<group>/<chat_id>/subagents/spawn/`, holding
  the prompt, the result (or the error), and a small `meta.json`. Previously a
  `spawn` left no file at all -- its reply went straight into the conversation
  and nothing else -- so the two ways of delegating are now equally
  inspectable. Failed, aborted and cancelled calls are recorded too, and the
  prompt is written before dispatch, so a call that never returns still shows
  what was asked. The directory is append-only: there is no expiry or size cap,
  and reclaiming it means deleting the session, which removes its metadata
  directory too. DAG runs dominate the volume, because a node's rendered prompt
  inlines its dependencies' full output.
- A `run_subagent_dag` graph can read an earlier run's output from the same
  conversation. A completed node is named by its id alone (`{{ <id>.output }}` /
  `{{ <id>.output_path }}`, or an `inputs` entry of the form `{"node": "<id>"}`), needing no
  `depends_on` -- there is nothing left to order. `depends_on` may name it anyway, which
  records the dependency without ordering anything. Previously nothing carried across runs:
  the outputs sit under the session's metadata directory, which no working directory can
  be aimed at, so no relative path reached them. A node with no output to read -- failed,
  skipped, or in a run still in flight -- keeps its id but is refused, naming which case
  it is. Node ids are unique per conversation for this to work (see Breaking Changes).
- A DAG file reference (`{{ ref: }}` / `{{ ref_path: }}` / an `inputs` `{"file": ...}`)
  resolves inside two roots now, the session working directory and this conversation's
  sub-agent history (`<session_dir>/subagents/`, so the `spawn` records beside the DAG runs
  are reachable too), and `@runs/<run_id>/...` addresses the run history by path -- which is
  what reaches a file that is not a node output, or a run recorded before node ids were
  indexed. The second root stops at that directory rather than at agent home: agent home
  also holds user memory, installed skills, and every *other* conversation's transcript and
  sub-agent history, which `workdir.py` already keeps off the agent's file surface, and a
  DAG graph is LLM-authored and auto-run.
  Every `_path` form is now checked to exist before its sub-agent is dispatched.
- A DAG run stopped by `/stop` or a gateway shutdown now records its unfinished nodes as
  `skipped` in the session index on the way out. Those routes cancel the run task, which
  skipped the finalizer, so the ids the run had claimed stayed readable as "still being
  written" forever -- a later graph could then neither reuse them nor read them, and the two
  refusals it got contradicted each other. The id-reuse refusal also stops advising a
  reference to a node that has no output to reference.
- Sessions started by `raven tui` / `raven agent` record the directory they were
  launched in as `Session.metadata["project_dir"]`. The group directory name is
  a lossy slug, so this is what identifies the project.

### Breaking Changes

- A `run_subagent_dag` node id must now be unique across the whole conversation, not
  just within its own graph, and a graph that reuses one an earlier run took is refused
  before any node is dispatched. That is what makes an id an address: a later graph reads
  an earlier run's node with `{{ <id>.output }}` and no `depends_on`. Graphs that reused
  a generic id (`plan`, `step1`) across runs in one conversation used to run and now need
  a fresh id each time; the refusal says which run holds the id and offers referencing it
  instead.
- DAG run directories moved from `<workdir>/.ravenx_dag/<run_id>/` to
  `<agent home>/sessions/<group>/<chat_id>/subagents/mas_dag/<run_id>/`,
  and the per-session `index.json` moved with them. The history is keyed on the
  session now rather than on whichever directory the run happened to use, so
  repointing a session's working directory no longer orphans the runs already
  recorded. Existing `.ravenx_dag/` directories are neither migrated nor read;
  move them by hand to keep them visible in the UI.
- `raven agent -w <dir>` no longer starts an isolated agent instance rooted at
  `<dir>`. `-w`/`--workspace` now sets the working directory a turn reads and
  writes files in (defaulting to the process launch directory); use the new
  `--home <dir>` for the old "isolated instance" meaning (agent home: memory,
  skills, transcripts). A script that passed an absolute path to `-w` for
  isolation will keep running but silently share the default agent home
  instead. One that passed a relative path (`-w myproject`, resolved against
  the current directory) now fails outright: a working directory must be
  absolute, so the run exits with a parameter error. Both cases are fixed by
  switching the flag to `--home`.
- `raven tui` and `raven agent` now default their working directory to the
  process launch directory instead of agent home.
- `raven gateway` now gives each *channel* its own working directory instead of
  running every conversation in the agent-home root. Set it per channel with
  `channels.<name>.workspace` (`gateway.web.workspace` for the web channel),
  configured alongside that channel's credentials; unset means
  `~/.raven/tmp/<channel>`. Existing files already at the agent-home root are
  left in place. A single session can still be pinned elsewhere from the web
  UI, taking effect on the next turn.
- Session transcripts on `raven tui` and `raven agent` moved from
  `sessions/<channel>/<id>.jsonl` to `sessions/<project slug>/<id>.jsonl`,
  where the slug is the launch directory flattened the way Claude Code names
  `~/.claude/projects/` entries -- so a project's conversations stay together
  instead of piling up under `cli/` and `tui/`. Gateway transcripts keep the
  channel as their directory. Sessions written before this keep their existing
  file and still resume by id; nothing is moved.
- Each session now also has a metadata directory beside its transcript,
  `sessions/<group>/<id>/`, holding the sub-agent call history (`subagents/`).
- Shadow-git checkpoints now follow the working directory instead of always
  snapshotting agent home, so launching the agent inside a real project
  directory checkpoints that project. On `raven gateway` the same rule gives
  every channel its own shadow repository inside its own working directory,
  replacing the single one that used to cover all of agent home (session
  transcripts and user memory included). Because the working directory is now
  wherever you launched from, checkpointing refuses a root that is your home
  directory or an ancestor of it -- snapshotting a whole home every turn copies
  whatever you keep there into a repository that retains history, and an
  interrupted turn edits a project rather than a home. Run from a project
  directory, or set `runtime.checkpoint.policy` to `never`. Private keys are
  excluded everywhere else: `*.key` and `*.pem` never matched `id_ed25519`.
- `raven agent --continue` and the TUI's resume-most-recent now look only at the
  project you launched in, instead of the newest session on that channel
  anywhere. Resuming across projects also corrupted what it resumed, by
  appending this project's turns to a transcript filed under the other one.
  Sessions written before project grouping carry no project attribution and
  stay reachable from any directory. Cron and sentinel delivery are unchanged:
  they still forward to whichever session is live, wherever it started.
- A working directory may no longer be agent home itself, nor any directory
  containing it, on the CLI flag or the per-session override. Working at agent
  home puts `user_memory/` and `skills/` one relative path away from every
  write the agent makes; working above it (`~/.raven`, `~`) additionally puts
  `config.json` and `oauth/` inside the per-turn shadow-git snapshot. Use a
  subdirectory.

## v0.1.0 - Public Preview - 2026-06-30

Raven is now available as an Apache-2.0 open-source project.

This is the first public preview release: stable enough for developers to try,
inspect, and build around, while the CLI surface, plugin contracts, and runtime
internals may still evolve before v1.0.

### Highlights

- AI-native command line agent built for terminal-first workflows.
- Memory-first runtime with context assembly, routing, and session management.
- Proactive execution path for scheduled, event-driven, and background work.
- Native TUI and bridge packages aligned under the v0.1.0 release line.
- Skill and plugin foundations for extensible agent behavior.
- Provider routing and fallback paths for multiple model backends.
- Public repository hygiene: CI, commit linting, pre-commit checks, issue
  templates, security policy, contribution guide, and Apache-2.0 licensing.

### Installation

```bash
curl -fsSL https://raven.evermind.ai/install.sh | bash
```

### Release Status

- Version: `0.1.0`
- Tag: `v0.1.0`
- Stability: public preview
- License: Apache-2.0

### Notes

- Raven is not yet API stable.
- The public install endpoint and package distribution flow should be verified
  before publishing the GitHub Release.
- Future releases should use semantic versioning: patch releases for fixes,
  minor releases for new capabilities, and v1.0 once the public CLI and plugin
  contracts are stable.
