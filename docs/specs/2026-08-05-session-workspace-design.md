# Working directories and session storage — design

Date: 2026-08-05 (rewritten 2026-08-07 after the first design was withdrawn)
Status: implemented

## Goal

`workspace` meant two things at once. Split it, and give each half a layout
that matches how it is actually used:

- **Agent home** stays global (`~/.raven/workspace`): user memory, skills,
  session transcripts and their metadata, memory store, Skill Hub cache.
- **Working directory** is where a turn reads and writes files. The session's
  leader and every sub-agent it spawns share it.

Alongside that, two things that were previously scattered get a home:

- **Session storage** groups a conversation's transcript and its metadata
  directory under the project (terminal) or channel (gateway) it belongs to.
- **Sub-agent history** records what each delegation was asked and what it
  answered, for both `spawn` and `run_subagent_dag`.

## Context: what was broken

`workspace` was a single `Path` from `config.workspace_path`, handed to
`AgentLoop` by all three entrypoints. It carried two unrelated
responsibilities:

| Responsibility | Consumers |
|---|---|
| Agent home | `ContextBuilder`, `SessionManager`, Skill Hub cache, `MemoryConsolidator`, `MemoryStore` |
| Working directory | the filesystem tools and `ExecTool`, media tools, `DeliverFilesTool`, `SubagentManager`, `SubAgentDagTool`, `CheckpointService`, the sandbox executor |

Consequences on the running gateway:

1. Every chat shared one directory. All DAG runs landed flat in
   `~/.raven/workspace/.ravenx_dag/`, and every file artifact landed in
   `~/.raven/workspace/`, whichever chat produced it.
2. `raven tui` and `raven agent` ignored the directory they were launched from,
   the opposite of what a terminal tool is expected to do.
3. Terminal transcripts piled up under `sessions/cli/` and `sessions/tui/` with
   nothing tying a conversation to the project it was about.
4. A `spawn` left no record at all: its reply went into the conversation and
   nowhere else, so a sub-agent that failed left only a log line.

Sharing between a leader and its sub-agents already worked and is kept: the
leader's directory is passed to `backend.run(workspace=...)` and to
`build_executor`.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Only the working directory splits off; agent home stays global | Memory and skills are the agent's identity. Per-session memory would make every conversation a fresh amnesiac agent |
| D2 | `tui` / `agent` work in the launch directory; the gateway works per **channel** | A terminal tool works where you invoke it. A daemon has no launch context per conversation, and a channel is the unit a user configures — its chats are the same kind of work |
| D3 | A channel's directory is `channels.<name>.workspace` (`gateway.web.workspace` for web), defaulting to `<agent home>/../tmp/<channel>` | Configured next to that channel's credentials, where the user is already looking. Derived from agent home, not `$HOME`, so `--home` keeps an isolated instance isolated |
| D4 | Intermediate artifacts on `tui` / `agent` go under `<launch dir>/.raven/` | The agent works in a real checkout there; its bookkeeping must not litter the project root. The shadow-git repo already lived at `.raven/shadow.git` |
| D5 | `--workspace/-w` stops meaning agent home; agent home moves to `--home`. On `tui` / `agent` it is the working directory, on `gateway` the root the per-channel defaults hang off | Matches the user-facing meaning of "workspace". `--home` preserves the old capability |
| D6 | A per-session override persists in `Session.metadata["workdir"]`; no mid-turn switching | Same mechanism as the per-session model override. Switching mid-turn would strand a running sub-agent between two directories |
| D7 | The CLI `-w` flag is a single-run override and is **not** persisted | The CLI default is the launch directory; persisting `-w` would make a later launch elsewhere silently reuse a stale directory |
| D8 | Transcripts group by **project slug** on `tui` / `agent`, by **channel** on the gateway | A terminal conversation belongs to the checkout it started in; a gateway daemon serves every project at once, so the channel is the only grouping it can know |
| D9 | The slug reproduces Claude Code's `~/.claude/projects/` scheme exactly: each non-alphanumeric character becomes one `-` (per character, not per run), and past 200 characters the result is truncated and a base36 hash of the whole path appended | An existing convention beats inventing one, and matching it exactly means a path slugs the same in both tools. Transliterated from the shipped implementation (`2.1.224`); an earlier version inferred the rule from three directory names and got repeated separators and trailing slashes wrong |
| D9a | The slug is deliberately not made collision-free; the launch directory is recorded in `Session.metadata["project_dir"]` instead | `/srv/a_b` and `/srv/a/b` slug alike, in the reference too, and its 200-character hash only disambiguates long paths. The reference solves this by writing `cwd` on every transcript entry — the directory is a bucket, the record carries the identity. Adding a hash would have diverged from the very convention D9 exists to follow |
| D10 | Each session gets a metadata directory `sessions/<group>/<id>/`, beside its `<id>.jsonl` | A directory and a file of the same stem coexist, and the `*.jsonl` globs never see the directory. Keeps everything about one conversation in one place |
| D11 | Sub-agent history lives in that metadata directory, not in the working directory | It is an audit trail with the transcript's lifetime. A working directory is configured per channel and can be repointed; history recorded before that must not be orphaned |
| D12 | Both delegation paths record what `SubagentBackend.run()` returned | That is already what a DAG node's `.out.md` holds. Raw stdout would need a new contract across three backends for little gain — the returned value is what the agent actually saw |
| D13 | Failed, aborted and cancelled calls are recorded too, and the prompt is written before dispatch | The failure case is the one worth keeping; a call that never returns still shows what was asked |
| D14 | Session keys stay `<channel>:<chat_id>` | Grouping is a storage concern. Changing the key would ripple into the instance registry, both RPC surfaces and the UI for no user-visible gain |

## Layout

```
~/.raven/workspace/                       agent home
├── user_memory/  skills/  ...
└── sessions/
    └── <group>/                          project slug (tui/agent) | channel (gateway)
        ├── <session_id>.jsonl            transcript
        └── <session_id>/                 session metadata
            └── subagents/
                ├── spawn/<call_id>/      prompt.md, out.md | error.md, meta.json
                └── mas_dag/              index.json
                    └── <run_id>/         graph.json, manifest.json,
                                          <node>.prompt.md, <node>.out.md

~/.raven/tmp/<channel>/                   gateway working directory (default)
<launch dir>/                             tui/agent working directory
└── .raven/shadow.git                     its checkpoints
```

`<call_id>` and `<run_id>` are both `<UTC timestamp>-<suffix>`, so the two
history trees sort and read alike. No filename is ever derived from a
sub-agent's output — only from ids raven mints — so nothing here is
attacker-controlled.

## Per-entrypoint behaviour

| Entrypoint | Working directory | Session group | Workdir policy |
|---|---|---|---|
| `raven tui`, `raven agent` | the launch directory | `project_slug(cwd)` | `LAUNCH_DIR` |
| `raven gateway` (web, IM, cron, sentinel) | `channels.<name>.workspace`, else `~/.raven/tmp/<channel>` | the channel name | `PER_CHANNEL` |

The gateway's `-w` sets the root the per-channel defaults hang off; it does not
name a single directory, because a daemon has no single working directory.

## Mechanism

A per-turn `ContextVar` (`raven/agent/workdir.py`) is bound by
`AgentLoop.run_turn` for the whole turn body. Components running synchronously
inside the turn read it. Anything that outlives a turn — a sub-agent, a DAG run
— captures the directory **as data** at spawn time and is marked with an
explicit `follow_binding=False` on its tools.

That distinction is load-bearing: an `asyncio.Task` inherits the binding at
creation, so a re-read appears to work and then silently follows the wrong
directory once the two diverge.

A DAG run touches two directories that must stay separate, so `run_dag` takes
both:

- `workdir` — each node's cwd, and what `{{ ref:<path> }}` resolves against.
  It has to be where the user's files are.
- `run_root` — this session's `subagents/mas_dag`, where the records go.

Collapsing them would either write the audit trail into the user's project or
resolve the user's file references against the history directory. The read side
derives `run_root` from agent home and the session key alone, never from the
turn binding: reads arrive *between* turns (a reloaded tab, a gateway restarted
mid-run) when nothing is bound.

## Guards

- An override must be absolute, and may be neither agent home, nor one of its
  `user_memory` / `skills` / `sessions` subtrees, nor any directory containing
  agent home. The last matters most: `~/.raven` is the instance data directory
  (`config.json`, `oauth/`, `cron/`, `logs/`), and the per-turn checkpoint runs
  `add -A` over the working directory — the `.raven/` default exclude cannot
  help when `.raven` *is* the work-tree root.
- Directory segments derived from a session key go through
  `safe_path_segment`, which additionally folds dot-only names. `safe_filename`
  leaves `.` and `..` intact; harmless for `<chat_id>.jsonl`, where the suffix
  makes an ordinary name, but a bare directory segment named `..` resolves back
  out of the root it was meant to stay in.
- Under a sandbox, a working directory outside the mount root is refused before
  the directory is created, rather than silently escaping.

## Compatibility

- Nothing on disk is migrated by the code. Files already at the agent-home root
  stay there.
- A session whose transcript predates project grouping keeps its existing file:
  `_get_session_path` falls back to any existing `sessions/*/<id>.jsonl`.
  Without that, resuming by id would open an empty session beside the real
  transcript and look like the history had vanished.
- `list_sessions(channel=...)` and `find_most_recent_chat_id` filter on the
  session key, not the parent directory name — under project grouping the
  directory no longer names the channel.
- Existing `.ravenx_dag/` directories are neither read nor migrated.

## Risks

- Each distinct working directory grows its own shadow-git repository once a
  checkpoint runs there. The cache is never evicted, so a long-lived gateway
  holds one per channel it has served; they are reclaimed only by deleting
  those directories. Bounded by the channel count, not the chat count.
- The slug is not reversible and not collision-free: `/srv/a_b` and `/srv/a/b`
  produce one segment, so two projects can share a group directory. Session ids
  are unique so nothing overwrites, and `Session.metadata["project_dir"]`
  carries the real launch directory for anything that needs to tell them apart.
  `--continue` is the one lookup scoped by group, and it filters on
  `project_dir` rather than on the group alone, so a collision does not hand one
  project the other's session. Sessions written before that field existed carry
  no attribution to contradict and stay reachable from either directory — the
  same rule the transcript fallback already applies.
- Sub-agent history under `sessions/<group>/<id>/subagents/` is append-only: no
  TTL, no count cap, and no reclaim path other than deleting the session, which
  takes its metadata directory with it. DAG prompts dominate the volume —
  `{{ <dep>.output }}` inlines an upstream node's full output into the
  downstream node's `prompt.md`, so a chain stores the same text once per hop
  on top of the one `.out.md`. Bounding it (a per-session cap, or a prune
  command) is deliberately a separate change: silently dropping audit records
  needs its own design.
- `workspace` is inherited from `ChannelBase`, so it sorts ahead of each
  channel's own credentials in the reflected field list and appears above them
  in the web UI's channel form. The CLI onboarding wizard excludes it
  explicitly, since it is not a credential and has a working default.

## Out of scope

- The AgentScope-side `PER_SESSION` workspace under
  `ui-webui/service/workspaces/`. Those directories are created and left empty
  because the chat turn runs on `RavenGatewayAgent`, which forwards to the
  gateway over WebSocket and never passes a workdir. This design routes around
  it entirely. Removing that dead path is a separate change.
- Mid-turn switching of the working directory (D6).
- Persisting full tool results in the session metadata directory
  (`tool-results/`); only `subagents/` is written today.
