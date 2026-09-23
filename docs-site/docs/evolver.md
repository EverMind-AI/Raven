# Evolver: usage and experiments

Evolver is a standalone, benchmark-driven tool for improving an agent harness:
diagnose failed tasks, design candidate edits, evaluate them, and retain useful
changes as Git commits. It is not a production Agent rewriting itself during
chat, memory recall, SkillForge retrieval, or a Playbook executor.

This is the onboarding and operations guide. [Design and implementation](self-evolution-map.md)
remains the detailed methodology-to-code reference.

## Status and intended audience

The current `evolver/README.md` marks this tree for planned retirement alongside
the vendored fork trees, pending sign-off. The implementation is still present;
this page documents that existing tool, not a long-term support commitment.
Confirm its lifecycle before building a new dependency on it.

Use it when you maintain a subject harness, have repeatable benchmark tasks
and scoring, and can budget repeated evaluations in an isolated environment.
For everyday task automation, use [Playbooks](playbooks.md); for a one-off
graph, use [DAG orchestration](orchestration.md).

The unified launcher currently registers **AppWorld only**. Other evaluation
modules in the tree do not automatically become registered launch targets.
The README records unit/fake-benchmark end-to-end coverage and a real AppWorld
smoke, but not a full-scale multi-round run or real-benchmark sealed-test
retention validation. Treat that as the documented evidence boundary, not a
claim of production readiness.

## How an experiment works

```text
Pinned root commit -> cold-start baseline -> training failure diagnosis
    -> candidate child commits -> cheap screen -> full-train confirmation
    -> gates -> select next parent -> repeat within limits
    -> terminate -> sealed-test report, if configured
```

The model handles diagnosis, patch design, and semantic interpretation. Code
handles scheduling, scores, gates, lineage, and checkpoints. A **WHY** is a
failure-cause category; **WHERE** describes the edited surface. **K** is the
number of attempts per task. Default screening uses `K=1` and confirmation
`K=3`; AppWorld uses a focused-subset Fisher screen with regression sentinels.

Screening cheaply removes clearly worse candidates; passing it is not
promotion. A candidate must pass confirmation gates and beat the incumbent's
training score to become the next parent. A useful candidate may remain in
the archive without replacing the parent.

The configured comparison baseline is distinct from the evolving parent.
AppWorld defaults to a frozen cold-start baseline. Setting
`bench_config.baseline_mode: same_session` measures the control in the same
window at roughly twice the evaluation cost, reducing endpoint-drift bias.

## Prerequisites and a run specification

Use a source checkout containing `evolver/` and the benchmark adapter, Git,
and the project environment managed by `uv`. The subject `repo_root` must be
a Git checkout with the benchmark integration available. Pin `base_sha` to a
real commit: uncommitted changes are never evaluated as the root.

For AppWorld, prepare:

1. A separate AppWorld installation and downloaded, non-empty `data/` directory.
   The adapter expects its binary at
   `<appworld_data_root>/appworld-venv/bin/appworld` unless `APPWORLD_BIN` is set.
   Keep this environment separate from Raven's environment.
2. A subject-agent runtime config JSON, starting from the repository's
   `docs/examples/subject_runtime.json`, with working model credentials.
3. Real training task ids, one per line, and optionally a disjoint test-id file.
   Placeholder ids are rejected. A sealed split is needed for retention claims.
4. Working model access for the loop's driver/design/verdict roles and enough
   free ports, disk space, and evaluation budget.

Create `my_run.yaml` outside your source tree, replacing every example path
and the root commit below:

```yaml
bench: appworld
repo_root: /path/to/Raven
base_sha: REPLACE_WITH_SUBJECT_COMMIT
work_dir: /path/to/evolution/work
funnel:
  k_screen: 1
  k_confirm: 3
  budget:
    max_why_per_round: 1
    candidates_per_why: 1
    recombinations_per_round: 0
  termination:
    patience: 2
    max_rounds: 3
bench_config:
  config_path: /path/to/subject_runtime.json
  appworld_data_root: /path/to/appworld
  train_task_file: /path/to/train.txt
  test_task_file: /path/to/test.txt
  n: 3
  conc: 1
  base_port: 8600
smoke:
  bench_config:
    n: 2
    conc: 1
    base_port: 8700
```

This is a small wiring-oriented example, not a statistically sufficient
experiment. Supply a small test file too: reducing `n` only caps training
tasks. Omit `test_task_file` only when you do not need a sealed-test report.
Smoke settings can also set `test_task_ids` or a separate `test_task_file`.

Paths resolve relative to the config file. Omitted `base_sha` resolves to the
subject's current HEAD on each load; if HEAD later moves, resume can fail its
config-drift check. Pinning the original SHA avoids that ambiguity.

Omitting `models` uses Raven's configured model for all loop roles. To
customize them, add `models.driver`, `models.design`, and `models.verdict`
provider specifications, as shown in `docs/examples/evolve_appworld.yaml`.
These are separate from the subject agent's model in `bench_config.config_path`.
Keep credentials out of shared YAML; supported provider specs can reference an
API-key environment variable.

## Check, smoke, then run

Run from the Raven checkout root:

```bash
uv run python -m evolver check --config /path/to/my_run.yaml
uv run python -m evolver check --config /path/to/my_run.yaml --smoke
uv run python -m evolver run --config /path/to/my_run.yaml --smoke
uv run python -m evolver status --config /path/to/my_run.yaml --smoke
```

`check` validates config, model setup, benchmark files, editable paths, and
environment readiness. **It is not an offline dry run:** AppWorld's default
precheck sends a small probe completion to the subject endpoint, which can
incur cost, although it does not run benchmark trials.

`--smoke` uses `<work_dir>_smoke`. Built-in defaults reduce the run to one WHY,
one candidate, one round, no recombinations, and `K=1` confirmation. Your
`smoke` overlay is applied afterward and can change those limits. It does
not automatically shrink every benchmark-owned task list. Even a smoke run
can call real models, execute candidate code, and create commits.

After inspecting the smoke artifacts and choosing appropriate task sets and
limits, start the full experiment:

```bash
uv run python -m evolver run --config /path/to/my_run.yaml
uv run python -m evolver status --config /path/to/my_run.yaml
```

Do not start simultaneous runners on the same `work_dir`. Status is an
inspection command; it does not expose sealed test scores.

## Read the gates correctly

| Check | Actual meaning | Important limit |
| --- | --- | --- |
| Gate-f | Reports infrastructure-contaminated measurements; upstream retries can salvage trials | Remaining infra failures count as non-passes, not removed tasks |
| Gate-b | Restricts attribution to tasks where candidate beacons fired, when instrumentation is supplied | No instrumentation means this check is a no-op; a beacon is not a sandbox |
| Gate2 | Computes paired candidate/control lift | Navigator improvement and `credited_2sigma` are separate results |

Do not shrink the reported denominator to clean or beacon-fired tasks.
Full-training score, attribution subset, and statistical credit answer
different questions. A promoted candidate can have `credited_2sigma: false`;
that is not proof of a statistically significant improvement.

Python candidate edits on the AppWorld line normally require an
`activation_beacon()`. Presence-level attribution proves that some instrumented
code executed, not that a particular mechanism caused the gain. Review the
diff and beacon placement before citing results.

## Sealed test and finalization

Training trajectories drive diagnosis, design, and selection. Test ids must
not overlap train/anchor tasks. Sealed-test scores are withheld from the
decision path; this is an experiment protocol, not an operating-system access
barrier against arbitrary code.

The AppWorld launcher wires sealed evaluation into finalization. The final
deliverable is selected by **training score**, never by searching for the
highest test result among candidates. `retention.json` reports the selected
deliverable, training/test curve, paired evidence, and retention: test lift
divided by training lift, undefined when training lift is not positive.

Natural termination finalizes the run. To stop early after completed rounds:

```bash
uv run python -m evolver finalize --config /path/to/my_run.yaml --yes
```

Stop the active runner before explicit finalization. This unseals results and
marks the run final. It is not a pause command. Without a configured test
split, finalization still marks the run final but produces no test-retention
report. If unseal scoring fails before the stamp, repair the environment and
retry; if the stamp exists but the report is missing, `finalize` can rebuild it.

`run --force` can override config-drift and unseal guards. It does not restore
an honest sealed experiment or automatically make changed measurements
comparable. Prefer a fresh work directory and an untouched holdout after
results have been seen.

## Budget, interruption, and outputs

Costs are dominated by subject-agent trials: a baseline costs roughly
`training tasks × K` attempts, and each surviving candidate adds a full-train
confirmation, plus screening, retries, driver calls, and optional test scoring.
Candidate/round caps are not a hard currency spending limit.

Defaults are two WHYs with three candidates each, up to one recombinant per
round, `patience: 10`, and `max_rounds: 20`. Patience counts rounds without a
full-train candidate beating the fixed vanilla baseline, not merely rounds
without a new champion. Consecutive errored rounds have a separate backstop.

Before finalization, interrupting and rerunning the same command resumes from
trial artifacts and completed-round checkpoints. Completed trials are reused;
the infrastructure-rerun ladder can still evaluate contaminated work. This is
not instruction-level resume of an interrupted model call.

| Artifact under `work_dir` | Use |
| --- | --- |
| `run_meta.json` | Effective configuration fingerprint and finalization stamp |
| `journal/rounds.jsonl` | Completed-round checkpoints and parent lineage |
| `nodes/*.json` | Candidate commit SHA, status, and gate statistics |
| `findings.md` | Human-readable round log |
| `failure_map.json` | Accumulated failure diagnosis and flip information |
| `runs/` | AppWorld trial results, including baseline/confirm evidence |
| `sealed/` and `retention.json` | Withheld measurements and final report, when configured |

Candidate versions are real Git child commits; the subject working tree is
not automatically replaced by the promoted candidate. Review recorded SHAs,
preserve desired commits with your normal Git workflow, test them, and obtain
deployment approval separately. Evolver promotion is not a merge or release.

## Security and troubleshooting

Run in a disposable container or VM with scoped credentials. Editable-path
guards and the immutable measurement kernel constrain candidate capture, but
the design step is not filesystem/network jailed. Candidate code runs with
scorer privileges; in-memory tampering and access to the benchmark oracle
remain threat-model limitations. Audit small candidate diffs before trusting
scores. Trajectories may contain private data and are sent to configured models.

| Symptom | Recommended action |
| --- | --- |
| Missing AppWorld data, binary, or runtime config | Correct paths and download/setup; rerun `check` |
| Placeholder/overlapping task ids | Use real disjoint split files |
| Dead whitelist prefix or empty candidate | Verify editable files exist at the pinned root and inspect captured edits |
| Endpoint probe or port check fails | Repair the endpoint or free this run's ports before paying for trials |
| Resume refuses config drift | Restore the original config/root SHA or start a separate experiment |
| Run is already unsealed | Read the final artifacts; do not bypass the guard to tune on test results |
| No retention report | Check whether a test split was configured and finalization succeeded |

## Extend a benchmark or inspect internals

A new launcher target implements `build(ctx) -> BenchBundle` and registers in
`evolver/launch/registry.py`. It needs a scorer, a `TaskEval` result reader
with infra markers, diagnosis trajectories, disjoint splits, and editable-path
rules. Cold-start closures must be trial-idempotent; expensive evaluation
belongs in closures, not bundle construction used by `status`.

Start with `docs/specs/evolve-bench-contract.md` and
`benchmarks/appworld/evolve/entry.py`. The SOP is
`docs/specs/self-evolution-loop-sop.md`; implementation design is in
`evolver/orchestrator/DESIGN.md`.

The [Self-Evolution Map](self-evolution-map.md) records deliberate gaps, including
unwired borrowing, missing affinity data, and opt-in zero-hit preflight. It is
the reference for module-level changes, not a claim that all SOP features are
active.
