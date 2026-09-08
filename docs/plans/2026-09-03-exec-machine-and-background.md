# Plan: one exec for every reach -- machine channel, background lane, alias addressing

Date: 2026-09-03
Branch: feat/exec_machine_and_background (off port/dispatch-fixes-v0_2_0)
Status: all four items implemented. ops_exec retired on the owner's ruling
(the same-name-shadow question closes in favour of the trunk parameter; see
docs/specs/2026-09-03-exec-machine-permission-face.md for the sign-off page).
One scope adjustment against the original item 2: the completion WAKE is not
built -- the main loop is turn-based, so a task finishing after the turn has
no live turn to notify; completion becomes visible by reading the managed log
(the start note says so), and work that must wake a watcher stays ops_submit's.
The custody half (tracked child, managed log, status/tail/reap, baseline env,
session-exit reaping of own tasks) is all there.

## Why now

Live evidence from tonight's field test (the first real dispatch of the rebuilt
oncall, TUI log 2026-09-03 15:50-16:15):

- Every remote command carried a raw address: `exec("ssh -p 58717 root@... nvidia-smi")`.
  The address came from the task statement, because without it no agent can
  reach the machine at all -- the registry's careful `_SHOWN` projection
  (host/port/key never handed to the model) is bypassed by necessity.
- The oncall had `ops_exec` (registry-addressed, 60s discipline) on its roster
  and never used it: the raw address in context is always the shorter path.
  A door nobody walks through while a hole sits beside it.
- A whole-tree `scp -r` (including `.venv`) was killed at its 180s timeout;
  the local ssh client died, and the agent self-healed by chunking. The cap-kill
  note pointed the transfer at `ops_submit` -- the right pointer for a training
  job, the wrong one for a file copy.
- Staging the working copy cost a whole oncall session (process launch, brief,
  rsync-then-scp trial and error, one timeout) for what is seven mechanical
  commands with zero domain judgement.

Prior incidents in the same shape: run8's orphan training process (local kill
never reached the remote, pid 554327 at 100% GPU), run9's takeover (main drove
35 jobs over raw `ssh nohup`, bypassing the occupancy gate, dedup, result
synthesis and GPU accounting -- the governance cannot read intent out of a
command string).

## The work taxonomy this plan enforces

| Work | Channel | Cap |
|---|---|---|
| Local look / build / install | `exec` | 600s (unchanged) |
| Long mechanical local work (downloads, rsync, packaging) | `exec(..., run_in_background=true)` | none; harness-tracked |
| Remote look (nvidia-smi, ls, md5sum) | `exec(..., machine=<id>)` | ~60s, killed on BOTH ends |
| Remote long work (training, sweeps) | `ops_submit` | budgeted, ledgered |

The 600s cap is a routing signal, not a resource limit: what changed in this
plan is never the number, only what happens at the boundary. (Prior ruling
stands: raising maxTimeout was rejected as a band-aid, 2026-09-02.)

## Item 1: `machine` parameter on the trunk exec tool

Port the fork's machine channel (vendored oncall `raven/agent/tools/shell.py`,
schema at line ~191, with its tests) onto the trunk exec tool.

- Semantics: the command runs ON the registered machine; address and
  credentials resolve from the trunk registry (`raven/ops/connections.py`,
  already lifted into the host by the v0.2.0 line) and never enter the prompt.
- Per-command cap ~60s, wrapped in the remote `timeout` binary where present,
  so a kill is transitive -- no more run8 orphans.
- The channel refuses detaching commands (`nohup`/`tmux`/`&`) and points at
  `ops_submit`, as the fork's channel did.

Prerequisites:

- **The same-name-shadow ruling.** The v0.2.0 design deliberately kept the
  machine face out of trunk exec and contributed it as the oncall plugin's
  `ops_exec` ("the same-name shadow is an open ruling", ops_exec.py docstring).
  Landing `machine=` on trunk exec answers that ruling and retires `ops_exec`
  (or demotes it to an alias). Needs the v0.2.0 lead's sign-off.
- **A permission-face page**, per the migration acceptance discipline (gate 1):
  main gains machine reach. The argument to record: main can already reach any
  machine by typing `ssh` into exec -- tonight's log is the proof -- so this
  channels an existing power through a governed door rather than widening the
  face. The boundary stays deployment-side (registry membership).

## Item 2: `run_in_background` parameter on the trunk exec tool

Claude Code's shape, verified daily at scale there: the harness keeps the
child, assigns the output file, returns a task id, and wakes the loop when the
command exits (exit code + output tail delivered; v0.2.0's wake machinery is
in place). Background children are enumerable and are reaped at session end.

- **Hard boundary: `machine=` and `run_in_background` are mutually exclusive.**
  Background work on a registered machine is the definition of an `ops_submit`
  job; allowing it here would re-legalise run9's accounting bypass.
- Why not just `nohup` in the command: no completion wake (the model polls or
  forgets), the log path is model-invented and gets lost (run7: a 257-byte
  crash log nobody was routed to), the child outlives the session untracked
  (run8's orphan, local edition), and the guards cannot distinguish
  `nohup rsync` from `nohup train.py` by text -- a declared parameter is what
  makes the boundary enforceable. The parameter is one boolean; the value is
  the harness custody behind it, which nohup cannot provide.

## Item 3: registry-maintained ssh aliases (cheapest; do first)

On connection add/update, write a `Host <alias>` entry into `~/.ssh/config`
(precedent: the `gpu-a800` alias, 2026-09-01, verified). Transfers are then
written as `rsync ... gpu-a800:...` -- the client runs locally (so `machine=`
does not apply), addressing resolves inside ssh's own config, and the context
carries only the registry name.

## Item 4: cap-kill note routes by shape (wording only)

The oncall-flow `ExecCapKillHook` note currently points every cap kill at
`ops_submit`. Split by shape: job-like command -> `ops_submit` (unchanged);
transfer-like command (scp/rsync/curl/wget) -> the background lane plus the
alias. First real firing tonight surfaced the mismatch.

## Companion hygiene (no code, but the plan discounts to zero without it)

- Task statements and machine briefs stop carrying raw addresses; they name
  `conn_gpu_a800` and nothing else. Measured lesson (d4be532c era, and again
  tonight): while a raw address sits in context, no governed channel gets used.
- The DAG skill teaches the taxonomy table above in one line each (the host no longer
  writes a per-node machine brief; where a job runs is the on-call agent's own business).

## End state

One exec for the whole system. The rebuilt oncall already runs the installed
raven, so main and oncall share the local exec today; item 1 unifies the
machine channel, and `ops_exec` retires with the shadow ruling. The vendored
fork (frozen A side) keeps its own copy untouched.

## Order and estimates

1. Item 3 -- half an hour.
2. Item 1 -- about a day, including the permission-face page and tests.
3. Item 2 -- about a day, including session-end reaping and tests.
4. Item 4 -- half an hour.

Each item lands as its own commit with tests, per AGENTS.md.
