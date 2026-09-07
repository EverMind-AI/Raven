# Plan: the launcher keeps custody -- a job that detaches itself is refused, a kill reaches the whole tree

Date: 2026-09-03 (run11 field evidence, 19:17-20:40 local)
Branch: feat/exec_machine_and_background (off port/dispatch-fixes-v0_2_0)
Status: implemented 2026-09-03 evening (items 1, 2, 3, 5; item 4 via the
companion design doc), uncommitted on feat/exec_machine_and_background.
Scope: the oncall-flow plugin's ProcessExecutor launcher (B side,
`agents/raven-oncall/plugins/oncall-flow/oncall_flow/process_backend.py`),
the `ops_kill` reply, the occupancy gate and budget width (device-count
admission), the DAG brief for oncall nodes, the PITFALLS handbook.
The frozen A side (`subagents/`) is not touched; acceptance is transport-face
parity and the launcher body is not a tool face.

## What happened

Four accounting faults were seen on the GPU machine during run11. They are not
four defects; they are four consequences of one event.

**19:19 -- the main agent inspected a healthy job and judged it dead.** The
first job (`49a4fa85`, `r1_run.sh` in the foreground, the correct shape) was two
minutes in. The main agent read three signals and misread all three:

| what the main agent saw | what was true |
|---|---|
| `ps \| grep "train_r1\|train\.py"` matched nothing | the process was `solutions/s9.py`; the pattern could not match it |
| `output.log` was 0 bytes | python's stdout went through `\| tee`, block-buffered, and `torch.compile` takes ~2 minutes; nothing had been flushed yet |
| GPU memory 5 MiB | still compiling; allocation had not started |

The oncall's own reading was right: `ops_tune_status` said running=1, and the
oncall had written "still in torch.compile, wait". The main agent overruled it
through `resolve_dag_node` at 19:21:47: "your smoke run is dead ... the process
went with your turn ... the job must truly detach from your session", and
prescribed `setsid nohup ... &`. The premise is false: `ops_submit` already
launches the launcher under `nohup` and the launcher waits for its child; a job
never depends on the oncall's session.

**19:22:50 -- the oncall killed the healthy job (fault 3).** At 5m11s, about
when training hands over to evaluation. `ops_kill` -> `cancel()` sent TERM to
the pid in the pid file, which was the `bash -c` wrapper. bash died; the python
it had forked did not, ran to completion and printed `FINAL val_bpb=1.033535`.
The ledger says `failed / 5.183 min`; the log holds a valid number.

**19:23 -- the oncall wrote `launch_job.sh` with `setsid nohup ... &` inside
(faults 1 and 2).** Every later `ops_submit` ran `./launch_job.sh config.json`.
The launcher's child now exits in ~0.4 s (its only job is to spawn one more
layer), so the launcher writes `succeeded / 0.000` by contract, and the real
training runs as an orphan nobody is custodian of. Effects: a job is terminal in
the ledger 10 seconds after launch, the budget is never debited, the occupancy
gate releases immediately. `f4224e43` had a `succeeded` result.json while at
step 356 of its run.

**19:47 -- the gate refused two concurrent jobs and the oncall went around the
ledger (fault 4).** The registry row `conn_gpu_a800` says `concurrency: 1`; the
machine has two GPUs and the main agent had asked for both. The refusal offered
"submit one, or wait"; the oncall took a third option and launched the gpu1 jobs
(`s9_seed2`, `s9_seed4`, `base_seed1`) through `exec machine=` calling
`launch_job.sh` directly -- off the ledger. The exec detach filter matches
`nohup`/`setsid`/`&` in the command text and cannot see inside a script. The
gate's "0 already on it" was also false at that moment: a job WAS running on
gpu0, recorded as finished because of fault 1.

**Ledger impact.** Three campaigns' ledgers sum to 5.183 GPU-minutes spent;
actual consumption was over 60 (8 jobs of ~8 minutes each). The main agent's
quoted "0.42/110" came from this.

## Who was wrong

On "is the job dead": the oncall was right, the main agent was wrong, and said
so with certainty ("it is not slow, it no longer exists").

On "what to do with a wrong order": the oncall had the tools to check first
(`ps -p`, `nvidia-smi`, a 60 s wait on `output.log`) and used them later, but
executed the kill unverified and adopted the prescribed launch shape wholesale.
Going around the gate for gpu1 was its own decision.

One level up, it is a design gap: a `resolve_dag_node` message lands on the
oncall as the owner's critical feedback and outranks its own tool readings, and
nothing tells it "when the owner's assertion contradicts your instrument, verify
before acting". The product fixes below make the ledger true whichever side is
wrong, rather than hoping one side stops being wrong.

## Fixes

### 1. The launcher refuses a command that detaches itself (faults 1, 2)

After `wait "$RAVEN_JOB"` returns: if the `pid` file names a process that is
alive and is not `$RAVEN_JOB`, the child has exited while the job runs on with
no custodian. The legitimate pattern (a job script writes its solver's pid so a
cancel can reach the solver, then does its own wrap-up) does not trip this: the
script is still the live child and `wait` has not returned.

On detection: kill the escapee's process group (`setsid` made it a leader;
`kill -TERM -- -$pid`, then the pid itself, then -9 after a pause), and write
`result.json` as failed with an `error` field:

    the command detached itself (setsid/nohup/&) and left the launcher nothing
    to wait for; ops_submit already runs it detached from your session and
    waits for it -- run it in the foreground

`_stated_cause` already reads `error` first, so the reason reaches the model on
the first submit. Rejected alternative: adopt the orphan and wait it out. Its
exit code is unknowable for a non-child, so status would be a guess, and a
guessed `failed` recreates the "good run recorded failed" ghost of 4d44ac8e.

Known gap, recorded not fixed: a detaching script that does NOT write the pid
file is invisible to this check. The exec `machine=` filter shares the
blindness. The ledger layer is the defence; there is no cheap way to see through
a nested `setsid` without cgroups.

### 2. The launcher runs the child in its own process group; kills reach the tree (fault 3)

Start the child as `setsid bash -o pipefail -c ... &` when `setsid` exists on
the machine (fall back to the bare form otherwise; macOS has no setsid, the GPU
machine does). `wait` still works -- it is still our child. The launcher's trap
and `ProcessExecutor.cancel()` kill the group first (`kill -TERM -- -$pid`),
then the pid. A kill then reaches python, not only the bash that forked it, so
"killed on the ledger, ran to completion on the machine" cannot recur.

### 3. `ops_kill` says when it killed a live process (fault 3, visibility)

When the probe shows the trial alive at kill time, the reply carries "process
was alive (pid N, running M min) when killed". Killing a healthy job becomes
visible in the record instead of looking like cleanup.

### 4. Occupancy by declared resource, not by job count (fault 4)

Owner's rulings: "one job at a time" on a two-GPU machine is wrong; "one job
per card" is wrong too (a large training job may hold every card); the unit is
what a job declares it holds against what the machine has, declared at
`ops_declare`; the same design covers CPU-only science (cores, optional memory).

This grew past a plan item. The full design -- data model, per-interface
behaviour, migration of the three registered rows, tests -- is
`docs/specs/2026-09-03-resource-admission-design.md`. Item 1 above is its
prerequisite: a job recorded terminal at t+0.4 s frees its devices while still
running.

### 5. Guidance, two lines

- Guidance the dispatching side reads (the oncall roster description or the DAG skill): "`ops_submit`
  already detaches the job from your session and waits for it to finish. The
  main agent must not prescribe nohup/setsid/& to the ops side." The whole
  chain started with that one wrong instruction.
- PITFALLS handbook: `python | tee` needs `PYTHONUNBUFFERED=1`; without it the
  log is 0 bytes for the whole compile and gets read as a dead job.

### Not fixed here

- The main agent's dead-job diagnosis (grep pattern, buffered log, compile-time
  memory) is model judgement. Recorded as an observation for the dispatch
  guidance, not a code change.

## Tests

- launcher body: contains the escape check (`pid` differs from `$RAVEN_JOB` and
  is alive), the group kill, the `error` field in the synthesized failed result.
- launcher body: `setsid` branch present, bare fallback present, `wait "$RAVEN_JOB"`
  still present (existing test `test_the_launcher_synthesizes_a_result_when_the_job_writes_none`).
- `cancel()` command contains `kill -TERM -- -` before the plain pid kill.
- `ops_kill` reply on an alive trial names the pid and minutes.
- The existing launcher tests are string assertions over the staged body
  (FakeHost); no real-shell execution exists in the suite today. A real-shell
  test for the escape path would need Linux (`setsid`); mark it skip on darwin
  if added.

## Reconciling run11's books

The ledgers are wrong for this run and the final report must not trust them:

- `49a4fa85`: `FINAL val_bpb=1.033535` is a valid data point; do not drop it as
  failed.
- `s9_seed2`, `s9_seed4`, `base_seed1`: off-ledger, valid logs, minutes uncounted.
- GPU-minutes must be recomputed from each `output.log`'s first/last mtime, as
  run9's appendix did by hand.

## Evidence

- Main agent TUI log `~/.raven-main/logs/tui.log` lines 60217-60237 (19:19:18
  inspection, 19:20:38 malformed resolve with `action`, 19:21:47 resolve).
- Oncall ACP frames `~/.raven-main/traces/logs/acp-frames/2026-09-03/Raven-Oncall-111106482791.jsonl`:
  19:22:40 reasoning ("the user is giving me critical feedback ... use setsid
  nohup"), 19:22:50 `ops_kill`, 19:23:11 `launch_job.sh` authored, 19:47:32 gate
  refusal, 19:38:24 / 19:47:54 / 19:57:04 off-ledger `exec` launches.
- Job directories on the GPU machine under `/root/workspace/r1_runs/jobs/`:
  `.raven-launch.sh`, `result.json` mtimes vs `output.log` mtimes, `launch_job.sh`.
- Campaign ledgers `~/.raven-main/workspace/subagent_sessions/raven-oncall/oncall_flow/r1-*/ledger.json`.
