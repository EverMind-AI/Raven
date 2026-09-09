# Adding a benchmark: the evolution plugin contract

The unified entry `python -m raven.evolver run --config <yaml>` runs the SOP
self-evolution loop on any registered benchmark (methodology:
[`self-evolution-loop-sop.md`](self-evolution-loop-sop.md); implementation
mapping: [`self-evolution-loop-raven-mapping.md`](self-evolution-loop-raven-mapping.md)).
This document answers: what do you build to make *your* benchmark evolvable?
Reference implementation: `benchmarks/appworld/evolve/entry.py`
(built-in scorer line). User-facing docs live in `raven/evolver/README.md`.

## What you bring (bench side)

| # | Deliverable | Shape | Hard requirement |
|---|---|---|---|
| 1 | **Scorer** | subprocess-invokable: given a task list, K attempts, and a checkout of the candidate code, write **one result file per trial** | Result files must distinguish "task failed" from "infrastructure failed" (an infra marker) — this feeds Gate-f and the fixed-denominator rule |
| 2 | **Result reader** | result files -> `TaskEval(task_id, passes, attempts, infra_attempts)` | Idempotent: re-reading the same directory yields the same evals |
| 3 | **Trajectories** | one renderable execution record per attempt, plus a "failing attempt -> diagnosis text" renderer | Diagnosis only ever reads train-side trajectories |
| 4 | **Task split** | train / test id lists (files or inline) | A test list enables the sealed flow; train∩test must be empty (checked at startup) |
| 5 | **Editable-path whitelist** | which path prefixes of the subject repo the designer may edit | Every prefix must match files at `base_sha` or the run refuses to start (prevents silently-dropped edits). **The scorer must not be inside it**: the grading *implementation* (oracle call, infra classification, result-write helper) must be covered by `path_guard.IMMUTABLE_PATTERNS` — a candidate that can edit its own judge is not being measured (AppWorld: `evolve/grade.py` + `batch.py` are immutable; `agent_cli.py`/`tool.py` are the editable agent surface). Note the limit: the editable agent process is still the *writer of record* — it runs candidate code in the same process as the immutable grader and owns the result file, so it can in principle discard the oracle verdict. Immutability stops accidental drift and casual gaming, not a determined adversary; see the README threat model (diff-audit promoted candidates) |

Optional: an environment precheck (pre-run health gate), a WHY taxonomy
(omitted -> induced automatically from vanilla failures), per-WHY focused
subsets (targeted probing).

**Two different models, do not conflate:** the *subject's* model (what the
benchmarked agent runs on — pinned inside your scorer config for the whole
run, same-regime rule) and the loop's driver/design/verdict models (the yaml
`models:` section; omitted -> Raven's own configured model).

## Implementation shape: one `build()` function

In your package (e.g. `benchmarks/<name>/evolve/entry.py`):

```python
from raven.evolver.launch.contract import BenchBundle, LaunchContext, validate_whitelist

def build(ctx: LaunchContext) -> BenchBundle:
    spec = ctx.spec            # bench/repo_root/base_sha/work_dir/funnel/bench_config
    bc = spec.bench_config     # your own yaml section; you own its schema
    validate_whitelist(spec.repo_root, spec.base_sha, whitelist)
    return BenchBundle(
        root_node_id="C0",
        root_node=...,          # HarnessNode anchored at base_sha
        journal_path=spec.work_dir / "journal" / "rounds.jsonl",
        cold_start_total=len(train_ids) * spec.funnel.k_confirm,
        cold_start_done=...,    # count existing vanilla trial result files
        run_cold_start=...,     # idempotent; invoked every run: fill missing
                                # trials + any infra-rerun ladder salvage
        build_orchestrator=..., # assemble EvolutionOrchestrator (EvalBackend + gate)
        unseal=...,             # only with a test set; journal records -> report dict
        precheck=...,           # optional Gate0 probe: raise RuntimeError (actionable
                                # message) on a dead endpoint / bound ports / missing
                                # install; `check` runs it before anything is spent
    )
```

Register it: `raven.evolver.launch.registry.BENCHES["<name>"] = "your.module:build"`.

Framework pieces you reuse instead of rewriting:

- `make_worktree_eval_fn` — candidate commit -> ephemeral worktree checkout;
  you only implement "score this directory on these tasks";
- `eval_with_infra_rerun` — the SOP §0 infra rerun ladder (<=2 reruns, KEPT rule);
- `compute_stability` / `simple_anchor` / `select_anchor` — cold-start
  bucketing and anchor selection;
- two gate policies: `PairedTwoSigmaGate` (generic anchor screen) and
  `FocusedFisherGate` (per-WHY targeted probe);
- `SealedTestRunner` + `unseal_retention` — the sealed test / retention suite.

## Idempotency requirements (what resume stands on)

The resume model is "artifacts are the state": `run_cold_start` and your eval
path must, on re-invocation, only fill missing trials (an existing parseable
result file == that trial is done; a half-written file must be re-run). Round
granularity is covered by the journal replay. If your scorer skips existing
result files, interruption/resume works with no further effort.

## Acceptance checklist

- [ ] `run --smoke` completes one round (pin 2-3 known-failing tasks in the
      yaml `smoke:` section)
- [ ] Ctrl-C mid-run, re-run the same command: completed trials are not re-run
- [ ] `status` at each of the three phases; no test numbers ever appear
- [ ] Put a wrong prefix in the whitelist: the run must refuse to start
