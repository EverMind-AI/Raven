# raven-code

A **coding agent**: it writes, runs and debugs code for one task per turn and
reports what it did. Pointed at a repository it edits that checkout directly;
otherwise it works in a private scratch directory.

This folder is both the agent and our caller-side record. Nothing about it lives
outside this directory.

## Where it comes from

| | |
|---|---|
| Remote source | `github.com/TongLi31/Raven`, branch `feat/acp_worktree_integration`, current remote tip `9bda1403` |
| Vendored source | Local continuation of that branch at `3edd204f`; the GitLab workflow does not publish GitHub branches |
| Local checkout | `./Raven-main` - the agent itself lives inside this folder |
| Package / version | `raven` 0.1.9 (integration line), vendored 2026-08-28 from local source commit `3edd204f` |
| Local patches | **none** - `local-patches.diff` records only that every former patch was upstreamed |
| **Read first** | **`Raven-main/README.SWARM.md`** - the branch's own contract, and the authority on everything below |

This is **Raven-X's swarm-integration branch**, not plain upstream Raven. It is
purpose-built for the shape we use it in: an external orchestrator hands work to
it as a worker. That branch, not us, now owns the coding identity prompt, the
completion gates, the tool surface, and the decision to keep its own state out of
the workspace.

It replaced an earlier drop (`Raven-main.zip`, plain upstream Raven 0.1.9) that
this folder had been adapted around with six local patches. **Every one of those
patches is obsolete** - the branch does all six jobs itself, and better:

| What we had patched | What the branch does instead |
|---|---|
| Narrowed the recursive-`rm` deny pattern, because `\brm\s+-[rf]{1,2}\b` blocked `rm -rf build` | Rewrote the deny-list around `_RM_RECURSIVE` + `_SYSTEM_DIRS`, blocking only `/`, `~`, `$HOME`, a whole first-level directory or a system tree. Its own comments cite the same class of false positive we hit, plus two we had not (`qemu -no-shutdown`, `which mkfs.ext4`) |
| Raised the 10k exec output cap and made it configurable | 30k cap **plus** spill-to-file: past the cap the full output is written out and the truncation notice names the path for `grep` / `read_file`. Strictly better than a bigger cap |
| Stripped `ask_user` from the system prompt, since the tool was disabled | Keeps `ask_user` registered and makes it answer instantly with `Error: ask_user not configured (no question broker)` when there is no channel. Verified here. So the tool stays enabled and the prompt stays honest |

Those six are gone for good. **Three more patches came back during the 2026-08
integration work - and have since been upstreamed into the fork branch, so zero
live patches remain.** The table below is kept as history (the WHY of each fix);
the fixes themselves now arrive with the vendored tree instead of being applied
on top of it:

| Patch | Why it is still here |
|---|---|
| `raven/plugin/memory/everos/backend.py` - coerce a message `timestamp` to the ms epoch | EverOS's `MessageItemDTO` declares `timestamp: int` while raven stamps its own records with an ISO-8601 string. A caller forwarding a persisted message fails the entire write with `422 INVALID_INPUT`, logged at warning level and surfaced nowhere else, so the store silently stops receiving turns. Upstream still carries the bare `m.get("timestamp") or now_ms` at `e16aec5f`. **Defense-in-depth, not an observed outage**: verified 2026-08-17 that no current caller sends an ISO string, so this is for the replay and direct-chat paths that do forward persisted messages |
| `raven/session/manager.py` + `tests/test_session_state_dir.py` - drop the in-workspace session migration | `SessionManager` read `<workspace>/sessions` as a legacy location, moved every file into the per-workspace state bucket and removed the source tree. Trunk now keeps a raven's live transcripts exactly there, and this fork's workspace defaults to `~/.raven/workspace` - the host agent's own home - so the migration ran against the host's live store rather than a legacy one, once per launch. **Observed 2026-08-27**, not defense-in-depth. The two upstream tests that asserted the migration are inverted in the same patch, so a replay restores the fix and its regression guard together |
| Agent Loop, coding tools, identity, bootstrap and checkpoints - consume the ACP session Working directory | ACP already validated and persisted `session/new.cwd`, but turns still used Raven-Code's Agent home for relative file and exec operations and described that path to the model. **Observed 2026-08-27** in a real Direct Chat: the ACP frame and session metadata named the host checkout while Raven-Code inspected `/root/.raven/workspace`. The patch binds the session directory for the whole turn, keeps Agent home separate in the prompt, reads repository rules from the bound checkout, and isolates shadow checkpoints per checkout so rendered credentials in Agent home are never snapshotted. |

`local-patches.diff` no longer holds appliable diffs: it records WHERE each of
the three went (fork commits on `feat/acp_worktree_integration`) so an update
can verify the fixes are still present upstream instead of re-applying them.
Everything that adapts this agent to our gateway lives *beside* the checkout,
never inside it - and with the patches upstreamed, that rule now holds without
exceptions.

**The 2026-08-20 update found this the hard way.** The tree that was here
differed from its own stated source in eight files. Seven were `ruff format`
churn with identical ASTs and were discarded; the eighth was this patch, which a
wholesale replacement would have dropped without a trace. Prove the split by AST
rather than by reading the diff -- reflowed strings and moved blank lines look
exactly like real edits, and `git diff --ignore-all-space` does not separate
them.

## Updating to a newer source drop

The source is a git repository now, not a zip, which makes the update both
cheaper and checkable. Fetch it read-only, never as a push remote:

```bash
cd <repo root>
git fetch --no-tags https://github.com/TongLi31/Raven.git \
    'refs/heads/*:refs/remotes/tongli31/*'

# What you are about to take, as a real commit range. The base is the source
# commit named in the provenance table at the top of this file. (While the
# branch tip only exists locally - vendored from a not-yet-pushed commit -
# fetch has nothing newer; diff against the local fork checkout instead.)
git log --oneline <base>..tongli31/feat/acp_worktree_integration
```

Then replace the body from those objects. There is no patch step: live local
patches are zero, so the vendored tree must equal the source tree exactly.

```bash
cd subagents/raven-code
rm -rf Raven-main && mkdir Raven-main
git -C <repo root> archive <source-commit> | tar -x -C Raven-main
git add -A .
```

**The check that makes this safe**, run before committing: the staged tree must
differ from the source tree in NOTHING. Deletions of `.pdf` / `.png` / `.svg` /
`.html` are expected - the repo's `subagents/**` rules keep report assets out -
but any other line means either the extraction lost a file or something local
crept in unrecorded.

```bash
git diff --name-status "<source-commit>^{tree}" \
    $(git write-tree --prefix=subagents/raven-code/Raven-main)
```

Also refresh `subagents/TREE_HASHES` in the same commit
(`python3 scripts/check_vendored_subagents.py --update`) and update the source
commit in the provenance table at the top of this file.

A `.venv` inside the checkout is gitignored and so is absent from a fresh
worktree; in a clone that has one, move it aside first and `uv sync --reinstall`
afterwards (the install is editable, but `uv sync` bakes absolute-path shebangs
into `.venv/bin/`, so a venv that moved needs them rewritten). Re-sync whenever
`pyproject.toml` or `uv.lock` changes; never assume an older checkout's resolved
dependency set still matches the vendored source.

The zip-era procedure below still applies when a drop arrives as an archive
rather than a branch.

The work splits in two, and keeping the split is what makes an update cheap:

- **Adaptation** - `run.py`, `config.json`, `subagent.json`, this file. None of it
  lives inside `Raven-main/`, so a new drop cannot touch it.
- **Body** - `Raven-main/`, replaced wholesale.

```bash
cd subagents/raven-code

# 1. Keep the venv - a fresh `uv sync` is minutes, moving it back is seconds.
mv Raven-main/.venv /tmp/raven-code-venv

# 2. See what you are getting before you take it. A new drop is not
#    automatically newer, and README.SWARM.md is where the behaviour changes are
#    announced - read it before the diff.
unzip -q ~/<new>.zip -d /tmp/newdrop
cat /tmp/newdrop/*/README.SWARM.md
grep '^version' /tmp/newdrop/*/pyproject.toml
diff -rq Raven-main /tmp/newdrop/* | grep -vE '\.venv|__pycache__'

# 3. Replace the body, restore the venv.
rm -rf Raven-main && mv /tmp/newdrop/<dir> Raven-main
mv /tmp/raven-code-venv Raven-main/.venv

# 4. Only if pyproject.toml / uv.lock changed in the diff from step 2.
uv sync --reinstall
```

`--reinstall` rather than `uv sync`: the install is editable and the source is
live immediately, but `uv sync` bakes absolute-path shebangs into `.venv/bin/`,
so a venv that moved needs them rewritten.

If a future drop reintroduces something that has to be fixed, commit the fix to
the fork branch FIRST and re-vendor, so the tree-equality check keeps holding.
Only a change that cannot live upstream goes into `local-patches.diff` - and it
must then be re-added as an apply step here, the way the zip-era drops did it.

### History: what the 2026-08 swarm drop (`e16aec5f`) brought

125 files, +11053/-629 over 29 commits. Two items change this agent's risk
surface rather than its behaviour:

- **An in-process Python execution kernel** ships in the new `data-agent`
  plugin (`raven/plugin/data_agent/kernel_program.py` runs
  `exec(compile(...), self.ns)` on model-written code). It is **dormant here**:
  its manifest sets `enabled_by_default = false` and this folder's
  `config.json` has no `plugins.enabled`, so nothing loads it. Opting in would
  put model-generated Python in the agent's own process, where the `ExecTool`
  deny-list -- which only guards shell commands -- does not reach it.
- **`ripgrep-bin`** is a new runtime dependency that ships a prebuilt binary,
  rather than a pure-Python package.

The rest is upstream's own line: cron removed from the agent CLI, the
tree-sitter symbols tool dropped, the file-tool surface realigned, gate
defaults made explicit, and the everos memory backend reworked into an opt-in
with session-scoped recall.

### The five contract points to re-check

`run.py` depends on five behaviours of the checkout. None is a documented API, so
each is worth re-verifying rather than assumed. The first two silently produce
wrong results rather than errors.

| # | What run.py assumes | Check |
|---|---|---|
| 1 | The transcript is at `<config dir>/sessions/<escaped workspace path>/cli/<id>.jsonl`, and **`set_config_path()`** is what moves the data dir off `~/.raven` | `run.py`'s `session_file()` asks the checkout itself rather than reproducing the escaping. Confirm the printed path lands under this folder, not `/root/.raven` - getting this wrong finds no answer and reports every run as answerless |
| 2 | Raven writes **nothing** into the workspace | After a scratch run, the workspace holds only what the agent made. `sync_workspace_templates` must stay unreachable from the `agent` path (it is `onboard`-only) |
| 3 | `--session cli:<id>` creates the session when absent and resumes it when present | `resolve_session_cross_channel` returns a key containing `:` unchanged, so a fresh id is created rather than rejected |
| 4 | Exit 1 means only a config or credential error | An answerless run still exits 0, which is why success is decided by finding a persisted answer |
| 5 | `ask_user` cannot block an unattended run | Call it directly: it must return an error string, not hang |

### What to re-verify, and why each one

| Check | Command | Catches |
|---|---|---|
| The CLI runs at all | `Raven-main/.venv/bin/raven --version` | A stale shebang after the venv move |
| Config still validates | `.venv/bin/python -c "from pathlib import Path; from raven.config.loader import load_config; c=load_config(Path('../config.json')); print(c.agents.defaults.model, c.tools.exec.model_dump())"` | `Config` forbids extra top-level keys, so a renamed section is a hard error. Nested models *ignore* extras, so a renamed leaf goes silently to its default - print the values, do not just check it loads |
| Suite | `HOME=$(mktemp -d) .venv/bin/python -m pytest -q -p no:randomly` | Everything else. Compare against the drop's own failures before blaming the adaptation - see below |
| One real run, scratch | `python3 run.py --session t1 --task "..."` | Credentials, proxy, and stdout discipline end to end |
| One real run, in place | `python3 run.py --session t2 --repo /throwaway/repo --task "..."` | That the edit lands in the real tree and the change summary is right |
| Multi-turn | the same `--session` twice, second turn forbidden from reading files | That history is really replayed, rather than the agent re-deriving the answer from disk |
| The installed entry | `POST /raven/subagents/test` | argv construction and prompt-file substitution through the real spawn backend |

Re-installing through the RPC is **only** needed if `subagent.json` changed. The
entry points at `run.py` by path, so a new body is picked up with nothing to
re-register.

### This drop's own test failures

30 failed / 4945 passed, with the checkout byte-identical to the zip - so these
are the drop's, not ours:

| File | Count | |
|---|---|---|
| `test_sandbox_debug_server.py` | 22 | Also failed on the previous drop |
| `test_channels_errors.py` | 3 | Optional channel SDKs are not installed here |
| `test_cli_theme.py` | 1 | Also failed on the previous drop |
| `test_provider_resolution_invariants.py` | 3 | **New.** Asserts a bare `claude-*` model resolves to the `openai/` prefix; it now resolves to `openrouter/` |
| `test_per_model_provider.py` | 1 | **New.** Same area |

The provider-resolution four do not reach us: `config.json` pins
`provider: "custom"` with an explicit `apiBase`, so nothing here resolves a
provider by model name, and live runs demonstrably reach OpenRouter. They are
still worth reporting upstream - `README.SWARM.md` asks for integration-side
findings as PRs to that branch, and names the provider layer as a shared
interface to coordinate on.

## Install

`requires-python >= 3.12`; the venv resolved to 3.13.

```bash
cd Raven-main
uv sync
.venv/bin/raven --version     # 0.1.9
```

## Calling it

```bash
python3 run.py --task "..."                          # scratch mode
python3 run.py --repo /path/to/repo --task "..."     # in-place mode
python3 run.py --session <id> --task "..."           # a turn of a conversation
python3 run.py --prompt-file f.txt                   # the spawned shape
```

`run.py` supplies the three things the branch's contract leaves to the caller:
a session identity per conversation, a *stable workspace* per conversation, and a
verdict on whether the run produced anything.

The last one is why the reply is not simply the child's stdout. This branch made
stdout clean (it is the model's reply, plus a `gate_triggers:` line if a gate
fires), so consuming it directly would work - but **exit 0 does not mean an
answer was produced**, and only the transcript distinguishes the two. The answer
is therefore read from `<config dir>/sessions/<escaped workspace>/cli/<id>.jsonl`,
taking the last assistant row with text and no tool calls.

**No wall-clock limit.** `--timeout` defaults to `0`, meaning the run is never
killed: a coding task has no predictable length, and killing one loses both the
answer *and* the memory, since the extraction drain happens at exit. Pass a
positive value to cap a run by hand.

This default was learned the hard way. `--timeout` was originally `2400`, and a
real spawn against `/root/.raven/tmp/web/Raven` died at exactly that:
`exit=None elapsed=2400s / timed out / answer_chars=0` - forty minutes of work
discarded with no answer. `exit=None` is reached only from `run.py`'s own
`TimeoutExpired` branch, so the launcher was the killer, not the gateway.

The full inventory of what can still end a run, since a "no timeout" claim is
only as good as the layers it has actually checked:

| Layer | Limit | Ends a long task? |
|---|---|---|
| `run.py --timeout` | 0 = none | No (this was the culprit) |
| Subagent entry `timeout` | `null` | No |
| `SubagentManager` / `CliAgentBackend` | none by design (`manager.py:491`: "with every automatic timeout also removed") | No |
| `agents.defaults.llmCallTimeout` | 1800s **per LLM call** | Only a stalled call, not a long task |
| `tools.exec.maxTimeout` | 1200s **per command** | Only one command |
| Gateway shutdown | `cancel_all()` | **Yes** - restarting the gateway cancels running spawns |

The child runs with `start_new_session=True` in its own process group, which is
why `cancel_all()` exists at all: a Ctrl-C to the gateway no longer reaches it, so
without that call a wedged child would outlive the gateway and keep working
against the workspace. The practical consequence is the last row - do not restart
the gateway while a long Raven-Code run is in flight.

### In-place editing

`repo: /absolute/path` points `--workspace` at that checkout, and the agent edits
it for real. There is no clone and no patch step.

This is the shape `README.SWARM.md` documents, and it only became possible with
this drop: the previous one seeded `AGENTS.md`, `SOUL.md`, `sessions/`, `memory/`
and more into its workspace root, so aiming it at a repository would have
overwritten the real `AGENTS.md`. The branch moved all of that under the config
directory, bucketed by the workspace's absolute path the way claude-code keeps
`~/.claude/projects/<escaped-path>/`. Verified here: after a scratch run the
workspace contained exactly one file, the one the agent wrote.

What the agent is told, in the task preamble: the edits are real, uncommitted
work already in the tree belongs to the caller, and it must not commit, stash,
reset or switch branches unless asked. Its reply ends with the working tree's
`git status --porcelain` plus a `git diff --stat` line - a summary rather than a
patch, because the diff is already in the caller's tree where `git diff` shows it
better than a copy pasted into a conversation.

Review and undo are the caller's `git`. Nothing here keeps a backup copy.

### Multi-turn

Registered **stateful**. The gateway mints a uuid on the first turn, substitutes
it into `command` as `{agent_id}`, binds it to the caller's `instance` handle, and
replays it through `resumeCommand` afterwards. `command` and `resumeCommand` are
identical here, because `--session cli:<id>` both creates and resumes: the
resolver returns a key containing `:` unchanged, so there is no create-versus-
resume decision to get wrong.

**Never `--continue`.** `README.SWARM.md` is explicit that it is racy in a swarm,
and the mechanism says why: it picks "the most recent cli session" *in the
workspace's bucket*, so two conversations against one repository would steal each
other's history.

The workspace is the subtle part. Sessions are bucketed by the workspace's
absolute path, so **the same id against a different workspace is a different
conversation with no history** - the one trap the branch's README calls out. A
On the ACP path, the host sends the current Working directory with both
`session/new` and `session/load`. The CLI compatibility path instead records the
first turn's choice in `<state root>/instance-<id>/workspace` and replays it. A
directive naming a *different* repository is refused with an explanation rather
than silently starting over.

Two consequences:

- A later turn sees an earlier turn's large tool output **truncated**: the
  transcript keeps the first 12k and last 4k characters of one. The model saw all
  of it live. This is the one place multi-turn is weaker than a single long turn.
- Answer extraction snapshots the transcript's line count before the run and
  reads only past it. Without that, a resumed turn that produced nothing would
  return the previous turn's reply and be scored a success.

### EverOS memory: on, and isolated from the host Raven's

```json
"memory": {"backend": "everos", "userId": "raven-code", "agentId": "raven-code", "memoryTopK": 5}
```

**Why the section must be present at all:** `memory.backend` defaults to
`everos` (`config/raven.py:1124`), and `userId` / `agentId` both default to
`default` - which is exactly what the host Raven at `~/.raven/config.json` uses.
A config file that simply omits the section, as this one originally did, silently
joins the host assistant's memory. That is not theoretical; it was measured. A
fresh conversation asked for a codeword given only in a *different* conversation
answered:

> NO PRIOR CONTEXT
> (For transparency: a recalled-memory snippet from a *different* session earlier
> today mentions a codeword "TOPAZ", but that is not from our conversation, so I'm
> not treating it as an answer.)

The model declined to use it, which is judgement, not isolation.

**What isolates it, and what does not.** The backend here resolves to
`_HttpEverosAdapter` against `http://localhost:18791` - a *remote service*, not
a directory. So isolation is entirely by the two ids the wire contract carries
(`user_id` XOR `agent_id` per search). Verified by recalling one query under each
id through the same code path:

| Track | hits |
|---|---|
| `user_id=default` (the host's) | 5 |
| `user_id=raven-code` | **0** |
| `agent_id=default` (the host's) | 1 |
| `agent_id=raven-code` | **0** |

EverOS is dual-track by design: `user_id` names the user's episodes/profile,
`agent_id` the agent's own cases/skills. Both are pointed at `raven-code` here.

**The user track isolates; the agent track does not.** Measured after a real
coding task: the extracted case came back under `agent_id=default` - the host's
namespace - with `metadata = {"type": "case", "owner_type": "agent", "id":
"default_ac_20260812_00000001"}`, while `agent_id=raven-code` and
`agent_id=agent:raven-code` both returned nothing. So raven-code's cases are
accruing in the host Raven's agent track despite `agent_id` being set in *both*
config surfaces, and `_convert_messages` stamping assistant/tool messages with
`sender_id = agent_id`. The owner EverOS assigns a case is evidently not that
`sender_id`. Nothing in `everos.toml` carries an app/project/agent identity to
correct it either - it holds only LLM, embedding and storage settings - so this is
not fixable from our config and belongs upstream with the branch owner
(`README.SWARM.md` asks for integration findings as PRs). Until then: user-track
memory is isolated, agent-track cases are shared.

**`EVEROS_ROOT` is a red herring in this mode - do not reach for it.** It looks
like the isolation lever (`configure_everos_env()` only `setdefault`s it, so an
override wins) and it was tried here: a separate root was seeded with copies of
`everos.toml` / `ome.toml` and passed to the child. Recall through both roots then
returned **byte-identical hits**, because the HTTP adapter never touches the
filesystem store. Two things were learned the hard way and are worth keeping:
`ensure_everos_home()` seeds the *hardcoded* `~/.everos/raven` and never
`EVEROS_ROOT`, so a bare override leaves an unconfigured root behind - and in
embedded mode a missing `ome.toml` makes the OME engine degrade to a **silent**
no-op. The whole experiment was reverted; the copied credentials are gone.

**Writes need two things, and both are easy to miss.**

*One:* `run.py` passes **`--wait-skill-extract`**. Without it the process exits as
soon as the answer is ready and interpreter shutdown cancels the in-flight
extraction, so nothing is ever persisted - two fresh conversations six minutes
apart could not recall a fact planted in a third. Deliberately *without*
`--flush-skill-buffer`: that forces a `session_end` drain every call, and a call
here is one turn of a conversation, not the end of one. Keeping the buffer between
calls is what lets boundary detection work across turns at all (a lone `-m` turn
never trips a boundary on its own, per the flag's own help).

*Two:* the recall ids and the **write-stamp** ids are separate config surfaces
and must agree. `memory.userId` / `memory.agentId` are what the host passes to
`backend.recall`; what the backend stamps on stored messages comes from
`plugins.config["everos-memory"]` (`raven.py:1120` says so outright, and the
manifest declares those keys snake_case, passed verbatim). Setting only the first
produced the worst of both: recall isolated to `raven-code` while writes still
went to the host's shared `default`. So `config.json` carries both:

```json
"plugins": {"config": {"everos-memory": {"user_id": "raven-code", "agent_id": "raven-code"}}}
```

Measured before and after that fix, same query: `user_id=raven-code` went 0 hits
-> 2 hits with timestamps matching the runs, while the `default` track's hits are
all from *before* isolation was switched on. A fresh conversation then confirmed
it from the inside: its recalled-memory block held only its own prior runs, and
explicitly no TOPAZ, no passphrase and nothing about the user.

Cost: a turn went from ~30s to 36-71s, the longer end being turns where a boundary
tripped and extraction actually ran.

**Two more things were needed before what got stored was worth storing.**

*The task must lead the message.* EverOS summarises each turn into an episode, and
with `run.py`'s environment preamble first it stored `"raven-code received a
runtime context in an empty scratch directory at 09:39"` - the boilerplate, not
the work. `build_task` now puts the task first and the environment notes after a
`---` rule. Same kind of task afterwards produced `"raven-code fixed a failing
test in the repository /tmp/..."`. The ordering is load-bearing, not cosmetic.

*`--flush-skill-buffer` must be passed too.* `--wait-skill-extract` alone blocks
on *in-flight* extraction, but a lone `-m` turn never trips a boundary, so on a
one-turn spawn - which most spawns are - there was nothing in flight and case
extraction never ran. The flush sends `session_end`, which is what drains buffered
turns through **case + skill** extraction. Cost: one extraction pass per turn
rather than per detected boundary. Raven's own multi-turn continuity is unaffected
- it lives in the transcript, not in EverOS's buffer.

With both, extraction demonstrably produces a case from real work:

```
type=case owner_type=agent
"Recall which file/line/operator was edited earlier, then add divide function with
 zero-division check, tests, and run suite. Read files before editing them; the
 tool enforces session context to prevent guessing..."
```

Latency after all of this: a real one-turn coding task runs 50-85s, most of the
growth being the extraction pass.

If **physical** isolation is ever wanted rather than namespace isolation, the
lever is a second EverOS service (its own `base_url`), not a filesystem root.

### The `repo:` directive

A spawned run only ever receives prose: the registered command is fixed at
`--prompt-file {prompt_file}`, so `--repo` is unreachable from the gateway unless
the caller can express it in the prompt. A first line of exactly

```
repo: /absolute/path
```

is therefore stripped off the task and used as `--repo`. Without this the whole
in-place mode would be dead code for every real spawn, which is how it was
caught. The subagent `description` is what tells the dispatching model to write
the line, and to write it on the first turn only.

## The LLM it runs on

`config.json` pins the model this agent is tuned for, and `subagent.json` records
the same choice as `recommendedLlm` so a reader does not have to infer it. Two
files naming one model can drift, so `install.sh` compares them and reports a
mismatch rather than preferring one.

Set this folder's api key and that pinned model is what runs. **Leave the key
blank and the agent inherits the host raven's LLM instead** - its `providers`
block, its `agents.defaults.provider` and `model`, and its `routing`. The rest of
`agents.defaults` stays this folder's: the token ceiling, the tool-iteration cap
and the timeouts are operating limits tuned for this agent's job, and they have
nothing to do with whose key is paying. The optional Serper and Jina keys fall
back the same way, each on its own.

The host's provider block is copied wholesale rather than matched by name. A
provider called `custom` here and one called `custom` there can be two different
endpoints, so picking by name would point this agent at a gateway its model is
not served on - which reads as a bad answer, not as an error.

Inheriting is a fallback, not an equivalence: a measurement taken on the pinned
model does not carry over to whatever the host happens to run. `launcher.log`
records which one was used on every turn, in the form
`llm: inherited from <path> (provider=... model=...); tuned for <recommended>`.

With no key here and no provider key in the host config either, the launcher
refuses rather than starting something that cannot answer.

## The boundary, and what is left of it

`tools.restrictToWorkspace: false`. There is no filesystem fence: the agent
reads and writes any path on this host that the process user can, and the shell
takes absolute paths and `../` like any other command. The workspace is now only
where the agent works by default - a scratch directory, or the checkout named by
a `repo:` line - not a limit on what it can reach. The launcher preamble says so;
the `description` does not, because the roster is kept to one or two lines per
agent.

This is deliberate and interim. The fence it replaced was textual, so it failed
in both directions and neither failure is fixed by tightening it:

- **It guarded command text, not what a program then did.** Measured on an
  earlier drop: the agent ran `pip install pytest -q`, which names no path, and
  pytest landed in the host's shared site-packages. A package manager, a
  `git push`, or anything else writing through a program rather than a path
  argument was never something the setting could stop.
- **It blocked legitimate commands**, since the check was textual: an absolute
  path in a heredoc, or a redirect to `/tmp`, read as an escape attempt.

A real boundary has to come from the executor, not from a regex over the command
string - `tools.sandbox.backend` is where that belongs. Until one is in place,
what remains is the deny-list in `ExecTool.DEFAULT_DENY_PATTERNS` (unchanged:
`rm -rf /`, recursive rm of a system tree, `mkfs`, raw writes to a block device,
`shutdown`, fork bombs) plus `tools.exec.extraDenyPatterns` for anything an
operator wants to add. Those bound destruction, not reach: assume a task handed
to this agent can touch anything on the host.

Separately, `DirectExecutor` passes commands a **minimal env allowlist**, not the
host environment: PATH / HOME / locale / TLS / proxy only, deliberately no API
keys, cloud credentials or SSH agent. A task needing one of those fails as
unauthenticated rather than as a permission error. `tools.exec.inheritEnv` turns
that off and is for disposable containers only.

`web_fetch` refuses URLs resolving to private or internal addresses, so the agent
cannot reach `127.0.0.1:8000` or anything on the LAN.

## Completion gates

The branch ships five completion gates, all **default off**, so that a
question-answering task is not blocked by gates written for code changes. They
are enabled per-run through the environment:
`RAVEN_REQUIRE_REAL_TEST_EVIDENCE`, `RAVEN_VERIFY_BEFORE_COMPLETE`,
`RAVEN_GATE_STALE`, `RAVEN_GATE_RED`, `RAVEN_GATE_EMPTY_DIFF` (the last two also
require the first). Semantics: `0/false/no/off/disabled` forces off, any other
value forces on.

**All five are left off here**, matching the branch default. `run.py` sets none of
them, and the subagent entry carries no `env`. To turn them on for coding work
only, the natural place is `run.py`'s repo-mode branch, which knows something the
CLI does not: whether the task named a repository.

## stdout discipline

Raven's CLI backend uses the child's **entire** output as the subagent's reply
(stdout plus stderr when stderr is non-empty), so anything a launcher prints
becomes conversation text attributed to the agent. `run.py` therefore prints only
the answer and the change summary, plus a one-line reason on failure so the
caller is never left guessing - including for caller mistakes (no task, a path
that is not a repository, a repository contradicting the conversation), which go
to stdout with exit 1 rather than to stderr.

This matters more than it looks: the CLI itself prints `Using config: <path>` to
**stderr** on every run. `run.py` captures the child's output rather than letting
it inherit, so that line never reaches the reply.

ACP diagnostics go to `<ACP home>/launcher.log` -- `CODE_ACP_HOME`, or
`<raven data directory>/subagent_sessions/raven-code/acp` by default, which is
not under the state root; CLI compatibility-mode
diagnostics go to `<state root>/instance-<id>/launcher.log`. `--verbose` mirrors
them to stderr and is for running by hand only, never for a spawn.

## Credentials

| Need | Status here |
|---|---|
| LLM provider key | `CODE_API_KEY` in `.env`, an OpenRouter key. Required - absent, the run exits 1 |
| Model | `anthropic/claude-opus-5` (1M context, $5/$25 per Mtok), `reasoningEffort: high`, `temperature: 1.0`. `providers.custom.models` lists it, and `apiBase` is set explicitly - `README.SWARM.md` warns that a missing `apiBase` silently falls back to `OPENAI_BASE_URL` or the official OpenAI endpoint. Changing model means changing **both** `agents.defaults.model` and the provider's `apiBase` |
| `CODE_SERPER_API_KEY` | Serper key for web search |
| `CODE_JINA_API_KEY` | **Deliberately empty.** `r.jina.ai` works unauthenticated at a lower rate limit, and an exhausted key is worse than none: the same URL answers `402` with the key that used to be here and `200` with none, which disables page reading outright |

### Where the secrets live, and where the transcripts do not

Every secret is in `.env` (mode 600, never published); `config.json` holds none
and ships as-is. `.env.example` is the template - copy it, fill it, `chmod 600`.

The indirection is not decoration. The config loader does **no**
environment-variable substitution and reads no key from the environment, so the
key has to be *in the config file* by the time the CLI loads it. `run.py`
therefore merges `.env` into a rendered copy at launch, hands the CLI that, and
deletes it in a `finally`. It is created with mode 600 via `os.open` **before** a
secret byte is written, rather than written and then `chmod`-ed.

**Where that rendered file goes is what decides where the transcripts go.** The
runtime derives its data directory from the config file's own parent and offers
no separate knob for the session directory, so `sessions/` follows the config -
and so does `session_file`, which resolves against the same path, which is why
the two cannot drift. Rendering it under

```
<raven data directory>/subagent_sessions/raven-code/acp/
```

keeps real task transcripts out of this folder.

Under the raven **data** directory (`$RAVEN_HOME`, `~/.raven`) rather than under
the host's Agent home, which is the other thing the host hands out: a session's
working directory. A raven engine refuses to work in a directory that contains
its own home, so homing this one under the host's Agent home made every dispatch
fail before it started. `CODE_ACP_HOME` moves it.

An operator who configures the host's Agent home to contain the raven data
directory puts the default back inside it. The engine is then homed beside the
Agent home, in a directory named for this `RAVEN_HOME` -- two instances on one
machine are told apart by that name, and their ACP sessions and allocation state
stay apart with it.

Beside the Agent home is not always somewhere this run may write -- an Agent home
of `~` puts the fallback in `/Users`. That is refused, naming `CODE_ACP_HOME`,
rather than left to fail on the first `mkdir` with a bare permission error.

`CODE_STATE_ROOT` moves the `raven-code/` root -- the repos and the CLI
compatibility path's `instance-<id>/` -- which is work, and stays with the work.

The rendered config also pins `agents.defaults.workspace` to that same state
partition. That is Raven-Code's Agent home; the ACP session `cwd` remains the
separate Working directory where repository files are read, edited and run.

Transcripts are bucketed by the **escaped absolute path of the workspace**, so
moving either root invalidates existing buckets: the recorded workspace pointer
and the bucket name have to be rewritten together, or a resumed conversation
silently starts from an empty history. The 2026-08-16 move did exactly that for
17 conversations.

The login shell exports `http_proxy` / `https_proxy` and the backend hands a
spawned subagent that environment, so the OpenRouter arm works with nothing extra
to configure.

## Tool surface

Enabled: `exec` plus its interactive-session and background-job companions
(`exec_write`, `exec_read`, `job_status`, `job_wait`, `job_cancel`),
`read_file`, `write_file`, `edit_file`, `find`, `grep`, `list_dir`, `todowrite`
(the served prompt tells the model to use it for multi-step work), `web_search`,
`web_fetch`, and `ask_user`.

`ask_user` is enabled deliberately, reversing what the previous drop needed: it
answers instantly with an error when there is no question channel, so it cannot
hang an unattended run, and the served prompt refers to it. The task preamble
tells the agent nobody is watching and to choose a defensible default instead.

Disabled in `tools.disabledTools`: `spawn` (no internal fission - the strongest
form of the clamp `README.SWARM.md` recommends), `message`, `read_skill`,
`use_skill`, `hub`, `tool_search`, `tool_call`, `deep_research`, and the three
media tools.

`tools.exec.timeout` is 600s with `maxTimeout` 1200s, so a long build or test run
has room without a single command being able to eat the launcher's own 2400s
budget. `tools.sandbox.backend` is `none`: commands run on this host, which is
what makes the workspace fence the only boundary.

## Installed as

A hand-written third-party subagent named `Raven-Code` (no `preset` field),
registered **stateful**. With `idSource: provisioned` the schema requires
`{agent_id}` in *both* `command` and `resumeCommand`, and rejects it in a
`command` whose `resumeCommand` is unset. The roster advertises it as
`Raven-Code [stateful, local-files]`, which is what lets the dispatching model
pass an `instance` handle - `spawn` exposes that parameter only when some agent
is stateful, and refuses a handle aimed at a stateless one.

A spawn with no `instance` still gets a fresh uuid, so one-shot use is unchanged
and two concurrent spawns cannot share a conversation. `hold_handle` serialises
turns that do share a handle, which the branch requires: multiple calls on one
session must not overlap.

Install through the RPC, not the file-level helper - the helper writes the file in
whatever process calls it and the live gateway keeps the roster it loaded at
startup, so the dispatching model never sees the change:

```bash
python3 install.py --dry-run   # print the resolved entry, change nothing
python3 install.py             # back up the current list, then register
```

The endpoint **replaces** the list, so `install.py` reads the live one first,
merges this entry into it by name, and writes a timestamped backup beside itself
before the PUT.

`subagent.json` ships with `{SUBAGENT_DIR}` and `{PYTHON}` unresolved so the
published file carries no path from the machine that built it. They are resolved
at install time and nowhere else: the gateway substitutes only `{agent_id}`,
`{prompt}` and `{prompt_file}`, and spawns with the *session workspace* as cwd,
so neither a relative command nor the entry's `cwd` field can stand in for the
real path. Moving this folder means re-running `install.py`.

An edit made in the web UI afterwards will not appear here, and re-installing
would overwrite it. Read the live entry first with
`curl --noproxy '*' -s http://127.0.0.1:8000/raven/subagents`.

The `description` is what the dispatching model reads when deciding whether to
hand work here, and it is deliberately **one or two lines**: what this agent
does, plus the `repo:` directive, which is a literal syntax nothing else can
convey. Everything else a caller needs - that a handle continues a session, that
it reads local files - is already rendered next to it as a capability tag or
stated in the `spawn` schema, and the roster is rendered into *two* tool
descriptions (`spawn` and `run_subagent_dag`), so every sentence here is paid for
twice on every request.

## Measured runtimes

| Run | Wall clock |
|---|---|
| Scratch: write FizzBuzz, run it, report output | 51s |
| In place: find and fix a one-line bug, run the tests | 59s |
| In place, turn 2 of the same conversation: recall, then add a function plus test | 1m07s |
| Registered-entry test RPC (`PONG`) | 33s |
| A conversational turn with memory off | 10-15s |

### Multi-turn, through the real spawn backend

Driven through `CliAgentBackend` with an `instance` handle - the path a real spawn
takes, not just `run.py`:

| Turn | Handle | uuid | Result |
|---|---|---|---|
| 1 | `conv-A` | minted `b316a975` | stored two facts |
| 2 | `conv-A` | **same** | recalled the codeword |
| 3 | `conv-A` | **same** | recalled the number *and* what was asked in turn 2 |
| 4 | `conv-B` | new `a6e213ba` | `NO PRIOR CONTEXT` |

The uuid staying put across turns 2 and 3 is the proof that the *resume* template
ran rather than a second create. Turn 3 mattering separately: recalling turn 2's
question shows more than one turn of history is replayed, not just the last one.
Every follow-up was forbidden from reading, listing or grepping files, so only
real conversation history could answer it.

Re-run after pinning memory off, on the shipped config: continuity held
(`MERIDIAN-77` recalled on the same handle) and a second handle answered
`NO PRIOR CONTEXT` **and** confirmed no recalled-memory snippet was present.

Verified on this drop: the workspace holds only what the agent wrote (no seeded
state); an in-place run edited `src/calc.py` in the real tree and left a planted
uncommitted `WIP.txt` alone; turn 2 without a `repo:` directive stayed bound to
the same checkout and recalled the file, line and operator from history while
forbidden from reading files; a directive naming a second repository was refused
on stdout with exit 1; and `ask_user` returned an error string instead of
blocking.

## Files here

| File | |
|---|---|
| `run.py` | Host-side launcher (conversation state, workspace binding, session-path resolution, answer-based verdict, change summary) |
| `install.py` | Resolves the `subagent.json` placeholders and registers the entry over the RPC |
| `config.json` | Run config. Holds **no** secrets |
| `.env` / `.env.example` | The real secrets (mode 600, never published) and their template |
| `subagent.json` | The third-party subagent entry, with install-time placeholders |
| `Raven-main/` | The agent itself. Ships as source; its `.venv` does not. **No live local patches** - `local-patches.diff` records only the history of their upstreaming |
| `sessions/` | Raven's own transcripts, bucketed by workspace path. Created by the build beside its config; not ours to curate |

## Publishing

`.gitignore` is the manifest: everything it lists stays out of a published tree.
Three entries are worth stating outright rather than leaving to a pattern match.

**The checkout ships, but not its `.venv`.** `Raven-main/` is published as
source, so a recipient gets a launcher and an agent together. `.venv/` is
excluded: it is ~700 MB of third-party wheels whose console scripts carry this
machine's absolute paths baked into their shebangs, so a copied one fails
outright where `uv sync` in the checkout rebuilds it correctly.

**`sessions/` and `runs/` are work data, not samples.** They are the transcripts
of real tasks against real checkouts - code, paths, and whatever the caller
pasted into a prompt. Nothing in them is safe to ship on the grounds of being
small.

**`.env` and the rendered config are the only files that ever hold a key.** If
either reaches a published tree, treat the keys as leaked: rotate first, then
work out how it got there. Deleting the commit does not undo it.
