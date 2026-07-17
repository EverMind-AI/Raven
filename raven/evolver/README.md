# raven.evolver — harness self-evolution

A budget-bounded loop that improves an agent harness against a benchmark:
diagnose failing trajectories, design candidate patches, screen them cheaply,
confirm survivors at K=3, and promote only what beats the baseline through
three verification gates — with a sealed test set for an honest
generalisation number. The methodology is specified in
`docs/specs/self-evolution-loop-sop.md` (the SOP) and mapped to this codebase
in `docs/specs/self-evolution-loop-raven-mapping.md`.

## Two ways to run self-evolution

The SOP is the methodology; this package is one executor of it. There are two:

1. **The evolver pipeline (this package)** — the automated loop below. The
   orchestrator executes the whole funnel unattended: diagnosis, candidate
   design, screening, K=3 confirmation, the three gates, promotion, and the
   sealed-test unseal, with resumability and the guardrails (path whitelist,
   fixed denominators, config fingerprint) enforced in code. Use this for
   long multi-round runs where the numbers have to be trustworthy.
2. **An agent driving the SOP directly** — hand
   `docs/specs/self-evolution-loop-sop.md` to a coding agent (e.g. Claude
   Code) inside the subject repo and have it walk the steps itself: read
   failing trajectories, pick a WHY, write a candidate as a git commit, run
   the benchmark scorer for the K evaluations, and apply the gate arithmetic
   by hand. Nothing enforces the protocol — sealed-test discipline, fixed
   denominators, and honest gate math are only as good as the agent's
   adherence — so treat results as exploratory. It is the fastest way to
   prototype on a benchmark that has no `BenchBundle` plugin yet, and what
   it learns (WHY taxonomy, whitelist, scorer wiring) feeds directly into
   writing one (`docs/specs/evolve-bench-contract.md`).

The rest of this README covers way 1.

## Glossary (terms used below)

- **WHY** — a failure-cause class (e.g. "stops before verifying"); each round
  targets one or two WHYs. **WHERE** — the code location a patch touches.
- **K** — attempts per task per evaluation; K=3 confirm is the SOP standard.
- **The funnel** — cheap screen (K=1 on a small anchor task set) before the
  expensive full-train K=3 confirmation; most candidates die cheap.
- **Three gates** — Gate-f: enough valid (non-infra) measurements; Gate-b:
  the candidate's code actually executed (beacon fired); Gate2: paired
  statistical significance vs the baseline on the same tasks.
- **Sealed test** — the test split is scored but stored where no decision
  step can read it; opened once, at the end (`retention.json`).
- **BenchBundle** — the plugin object a benchmark implements to become
  evolvable (see "Adding a benchmark").

## Quickstart

All commands run from the repo checkout root (they invoke the
`raven.evolver` module; from elsewhere, add `--project /path/to/checkout`
to `uv run`).

```bash
# 1. Write a run spec (copy docs/examples/evolve_appworld.yaml and edit paths)
# 2. Validate everything cheap — config, models, bench setup, plus one small
#    probe completion against the subject endpoint; no trials run:
uv run python -m raven.evolver check  --config my_run.yaml

# 3. Tiny wiring run (isolated <work_dir>_smoke; minutes, not hours):
uv run python -m raven.evolver run    --config my_run.yaml --smoke

# 4. The real run — one command does all three phases:
#    cold-start baseline (if missing) -> evolution rounds -> unseal + retention
uv run python -m raven.evolver run    --config my_run.yaml
```

A real run takes hours to days, and the cost is dominated by **subject-agent
evaluations**, not LLM calls: cold start = train x K trials (e.g. 90 x 3),
plus roughly a full train x K evaluation per candidate that survives
screening. The loop's own calls per round are modest — tens of driver calls
(diagnosis), one design call per candidate (2-6, on your `design` model,
typically the expensive one), a handful of verdict calls. Interrupt freely —
**re-running the same command resumes** from the last durable artifact:
completed trials are never re-run (trial-level idempotency), completed
rounds are replayed from the journal.

```bash
uv run python -m raven.evolver status   --config my_run.yaml   # progress; never
                                                               # reveals test numbers
uv run python -m raven.evolver finalize --config my_run.yaml --yes
                                                               # end now + unseal (one-way)
```

What you get at the end: the console summary plus, under `work_dir/`,
`retention.json` (the sealed-test verdict: best round, train/test curve,
retention rate, paired significance), `nodes/*.json` (every candidate's
ledger: git commit, final status, gate stats), `findings.md` (human-readable
per-round log), and the promoted harness as a **real git commit** in your
subject repo.

## Interruption semantics

| Interrupted during | What exists | `status` shows |
|---|---|---|
| phase 1 (cold start) | partial baseline trials (kept) | trial count, "no results yet" |
| phase 2 (rounds) | commit list: promoted nodes + train scores | rounds, candidates by status — test stays sealed |
| phase 3 (unseal) | everything | the final report |

While a run is resumable, test numbers are physically withheld (they sit in a
directory nothing in the decision path reads). `finalize` is the explicit
trade: end the run now, see the result, never resume — the one-way stamp in
`run_meta.json` enforces it. The same file records a config fingerprint: a
run refuses to resume under a changed configuration (candidate and control
arms must stay comparable).

## Config file

See `docs/examples/evolve_appworld.yaml` for the annotated schema. The
sections: `bench` (registered benchmark name), `repo_root`/`base_sha` (the
subject repo being evolved and its root commit; omit `base_sha` to use the
repo's current HEAD, resolved once at launch and pinned for the whole run —
uncommitted changes are never part of the root), `models` (the loop's
driver/design/verdict brains — omit entirely to use Raven's own configured
model; note this is distinct from the *subject agent's* model, which is
pinned inside the bench config for the whole run), `funnel` (K values,
per-round budget, termination), `bench_config` (bench-owned), and `smoke`
(overlay applied by `--smoke`).

## Bootstrap: AppWorld

The shipped example bench. One-time setup:

1. Install [AppWorld](https://github.com/StonyBrookNLP/appworld) into a
   directory of its own — it is the *subject environment*, kept out of this
   repo's uv env. In that directory: create a venv at `appworld-venv/`,
   `pip install appworld && appworld install`, then `appworld download data`
   — it must end up with a `data/` folder. Point
   `bench_config.appworld_data_root` at the directory.
2. Write the subject runtime config JSON (the *benchmarked agent's* model
   endpoint — not the loop's models). Copy
   `docs/examples/subject_runtime.json` and fill in your endpoint; `check`
   validates it against the schema. Point `bench_config.config_path` at it.
3. Put train (and optionally test) task ids in text files, one id per line.
   Valid ids come from AppWorld's official splits:

   ```bash
   APPWORLD_ROOT=<install> <install>/appworld-venv/bin/python -c \
     "from appworld import load_task_ids; print('\n'.join(load_task_ids('train')))" > train.txt
   ```

   (`APPWORLD_ROOT` matters: appworld resolves `data/` relative to the
   current directory otherwise. The bench exports it automatically at run
   time, but this manual step happens before any bench code runs.)

   (splits: `train`, `dev`, `test_normal`, `test_challenge`; using `dev` as
   the sealed test file is the usual setup). Placeholder ids like
   `<failing-task-1>` are refused at `check` time.
4. `check`, then `run --smoke`, then `run`. For the `smoke:` section's
   pinned tasks, any 2-3 train ids work for a wiring test; ids your agent
   *fails* make the smoke round exercise the full design path (you learn
   which ones from the cold start of a real run, or a quick manual batch).

## Adding a benchmark

Implement one `build(ctx) -> BenchBundle` function and register it — the
contract (scorer subprocess, result reader with an infra marker, trajectory
renderer, splits, editable-path whitelist) is documented in
`docs/specs/evolve-bench-contract.md`. `benchmarks/appworld/evolve/entry.py`
is the reference implementation.

## Documentation map

| Document | What it is |
|---|---|
| this README | user-facing: quickstart, config, bootstrap, security |
| `docs/specs/self-evolution-loop-sop.md` | the methodology spec (SOP; English translation of the upstream document) |
| `docs/specs/self-evolution-loop-raven-mapping.md` | SOP clause -> Raven code, deviations, unwired parts |
| `docs/specs/evolve-bench-contract.md` | what a new benchmark implements |
| `orchestrator/DESIGN.md` | design notes: inversion of control, SOP<->module table, cross-module conventions |

## Status and known limitations (2026-07)

- **Verified:** the unit/e2e suite (126 tests: gate math, the infra-rerun
  ladder, git commit/worktree primitives, sandbox whitelist capture, driver
  transports, config, guards, idempotency, interrupt -> resume -> finalize
  with a fake bench) plus a real AppWorld smoke through the unified entry: cold start -> resume -> one full round
  (real diagnose/design, a real candidate commit, focused probe + confirm,
  promoted with an honest `credited=False` Gate-b verdict) -> termination.
- **Not yet exercised at scale:** a full-size run (90 tasks x K=3,
  multi-round, hours) and the sealed-test unseal/retention path on a real
  benchmark have only been exercised via tests, not a production run.
- One bench example ships today (AppWorld, built-in scorer line); a
  framework-line example (external scorer) is planned.

## Security notes

This system has an LLM **edit and execute code**. Understand the boundaries
before running it anywhere sensitive:

- Candidate edits are constrained to a per-bench **path whitelist**
  (`path_guard` / sandbox capture); everything else the designer touches is
  reverted, and each whitelist prefix is validated at startup. On top of the
  whitelist, an **immutable kernel** (`applier/path_guard.py`) carves out
  the measurement surface — the evolver itself, and for AppWorld the scorer
  (`evolve/` including `grade.py`, and `batch.py`) — so a candidate can
  rewrite the agent (prompt, loop wiring, tools) but never the code that
  grades it or records infra failures.
- **Threat model:** these guards stop an LLM designer from taking shortcuts,
  not a determined adversary. Candidate code runs in the same process as
  the (immutable-on-disk) grader and the benchmark oracle is reachable over
  HTTP, so in-memory tampering or oracle probing is possible in principle.
  Promoted candidates are a handful of small commits — **diff-audit them**
  before citing numbers (`nodes/*.json` records every commit).
- Candidate code **runs during evaluation** with the same privileges as your
  benchmark scorer, and the design-step sandbox is not filesystem/network
  jailed. Run evolutions in an isolated environment (container/VM, scoped
  credentials), not on a workstation with live secrets.
- Python candidates are required to carry an `activation_beacon()` call, and
  credit is only assigned when the beacon actually fired (Gate-b) — this is
  an attribution mechanism (presence-level, see the mapping doc), not a
  sandbox.
- The default per-round baseline is frozen at cold start (cost-bound); if
  your subject endpoint's throughput drifts across a long run, set
  `bench_config.baseline_mode: same_session` (~2x eval cost) so control and
  candidate arms are always measured in the same window.
- The design step sends your failing trajectories to whatever model you
  configure; treat trajectory content accordingly.
