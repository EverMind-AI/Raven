# Self-Evolution Orchestration Harness — design notes

> Solidify a self-evolution SOP that used to depend heavily on Claude into a
> deterministic harness, so that weaker models (qwen / kimi) can drive the
> whole loop too.

---

## 1. What this SOP does

**One sentence:** make an agent's harness (the outer structure around the
model — system prompts, tools, hooks, memory) improve itself iteratively and
grow stronger on a benchmark. The means is not swapping models or injecting
skills, but looping: **diagnose failures -> design patches -> verify
rigorously -> keep what works, cull what doesn't**.

### How it runs (cold start + the seven-step loop)

- **Cold start:** run the unmodified baseline (vanilla) K times over the
  train set to produce a **complete baseline record**. Then analyze the
  failing trajectories one by one, classify the failure causes (empty
  response never submitted / submitted without verifying / bad tool calls /
  time-arithmetic errors ...) into a **failure map**. The baseline record is
  also the **fixed comparison bar for the whole run**.

- **① Diagnose:** read the previous round's failing trajectories and classify
  them (round 1 uses the cold-start failure map directly). The failure map
  **accumulates across rounds**, making the failure distribution's drift
  auditable as evolution proceeds.

- **② Choose targets + design candidates:** pick 1-2 failure causes worth
  attacking this round; design 2-3 candidate patches each, across change
  levers (prompt / config / runtime logic). **Key constraint:** every patch
  is env-var gated and default-off — byte-identical to the baseline when
  off, so the baseline is never contaminated.

- **③ Free pruning:** at zero GPU cost, cull candidates that cannot possibly
  take effect (trigger condition never hit in historical trajectories =
  would never activate if fielded).

- **④ Apply and create a node:** apply the patch to the parent, derive a
  child version (git branch + commit), and register it in the node ledger.

- **⑤a Screen:** probe quickly on a small representative subset (the anchor,
  K=1). The verdict is **generous-pass**: only cull candidates clearly below
  the baseline; slightly-low or inside the noise band always advance — a
  single small-sample run has too much variance to conclude anything.

- **⑤b Full confirm:** screen survivors run the full train set K times,
  evaluated seriously.

- **⑥ Three gates:** confirm the lift is real, not an artifact —
  - *measurement validity:* infrastructure failures (crash/timeout) are not
    scored as 0; rerun or handle explicitly;
  - *attribution correctness:* credit only on tasks where the patch's
    mechanism actually fired;
  - *significance:* **paired comparison** against the baseline; the lift must
    clear 2σ to count.

- **⑦ Choose parent + termination check:** the round's best candidate enters
  the bank and becomes the next parent; if nobody beat the baseline, keep the
  old parent. Then check termination: **10 consecutive rounds without a
  candidate above the baseline** (exploration exhausted) or the **20-round
  cap**. **Iron law: never decide termination from the test set** — the test
  set stays sealed throughout; it is opened only after evolution ends, to
  compute the generalization retention rate.

### The core division of labor

Judgment work (diagnosis, design, decisions) goes to a model capable of
reasoning; arithmetic work (anchor selection, noise-band computation, gate
verdicts, aggregation) **must** be done by deterministic code — experiments
need to be reproducible and auditable.

### The problem

This SOP currently **depends on Claude**: Claude can read long specs, follow
instructions faithfully across a single very long session, and run dozens of
steps without drifting or stopping early. Swap in a weaker model and it
fails — weak instruction following, premature wrap-up, broken long chains.

---

## 2. Design idea: inversion of control

What used to live in the prompt ("how the loop advances, when to stop, do not
exit early") and rely on model discipline moves **into deterministic code (a
state machine)**; the model only makes one **scope-limited judgment with
schema validation + bounded retries** at each step. A weaker model only has
to answer one small question well at a time; "keep running without breaking"
is guaranteed by code.

**Two model roles:**

| Role | Who it is | Run by |
|---|---|---|
| **subject model** (being evolved) | the model that runs benchmark tasks | the external scorer (EvoAgentBench / AppWorld) |
| **driver model** (driving evolution) | the model doing diagnosis / design / decisions | this harness's semantic-node layer |

The point of solidifying is exactly that **the driver can be swapped for a
weaker model**.

---

## 3. SOP steps <-> code modules

| SOP step | Code | Role |
|---|---|---|
| Seven-step loop + rounds + "no early exit" | `loop.py` (EvolutionOrchestrator) | deterministic state machine owning all control flow |
| Termination (10 / 20 rounds, vs **vanilla** only) | `termination.py` + `loop.py`'s `beat_vanilla` | patience signal = "some candidate's full-train confirm beat vanilla's fixed mean", independent of which baseline gates use (incl. the per-parent ratchet); errored rounds don't burn patience — separate counter (`errors_exhausted`) |
| ① Diagnose | `nodes/diagnose.py` | reuses the existing judge's prompts and parser + schema validation / repair retries; emits the failure map |
| Cross-round failure-map accumulation | `nodes/diagnose.py` `merge_failure_maps` | merged into the living map and persisted each round |
| ② Choose targets + design | `nodes/design.py` | deterministic WHY selection + driver-designed candidate patches, schema-validated |
| ③ Free pruning | `loop.py`'s `preflight(cand, parent)` hook + `production.make_zero_hit_preflight`; the design step's sandbox also runs an AST compile check + import smoke | catches both kinds of dead candidate: **crashers** (bad imports / module-level NameError — import smoke) and **inert ones** (driver-declared `TRIGGER_REGEX` with zero hits across the parent's failing trajectories = would never fire; culled at zero inference cost and recorded as a preflight pseudo-outcome, never silently dropped). No predicate / bad regex / no corpus all pass (fail-open: prune only on positive evidence of inertness) |
| ④ Apply + create node | reuses `tree/store.py` `create_child_node` | git apply + kernel protection + ledger registration |
| ⑤a Screen + generous-pass | `nodes/screen.py` (SWE anchor line) / `gates/strategies.py` FocusedFisher stage-1 (AppWorld line) | both lines keep generous-pass: the SWE line culls only below baseline by > `1.5×σ`; the AppWorld line culls only when the focused probe is **significantly worse** (reverse Fisher p<α) or sentinel regression exceeds one flaky trial (>1.5/(n·K)); slightly-low / indistinguishable always advances to the full set |
| ⑤b Full confirm | `benchmarks/evoagentbench/evolve/adapter.py` / `benchmarks/appworld/evolve/adapter.py` `run_eval` | the piece that was genuinely missing — submit the candidate to the scorer, read per-task pass counts back |
| ⑥ Three gates | `gates/pipeline.py` (Gate-f -> Gate-b -> `gates/paired.py`), shared by both lines | navigator promotion (mean beats control, the SOP's loose caliber; FocusedFisher can add `min_confirm_lift`) + an independent credited-2σ label. **Gate-b (active on the AppWorld line):** python patches must carry `activation_beacon()` (missing = rejected, checked post-edit); `batch.py` injects a private beacon dir per task-attempt subprocess; `fired_source` reads the per-task firing table back from the confirm out-dir + infra-ladder union; attribution only on fired tasks. Three-state semantics: no instrumentation data (no `.enabled` marker / beacon-less candidate) = None = fail-open skip; marker present with zero firings = an honest zero = no attribution; prompt/config patches are beacon-exempt, attribution covered by the focused probe + sentinels |
| ⑦ Choose parent + verdict | `loop.py` + `nodes/verdict.py` | **argmax parent selection (paper Alg.1 L135):** the round's best gated candidate takes over only if its train score strictly beats the incumbent parent; ties/lower enter the bank without taking over (under the frozen-vanilla baseline a lower scorer could once displace a higher parent — fixed); the driver drafts each round's verdict |
| GSME bank + cross-cell recombination (paper §3.3) | `archive.py` + `production.make_git_recombine_fn` | each (WHERE x WHY) cell keeps one elite that "beat vanilla on full confirm" (**bank bar = the vanilla navigation caliber**, deliberately not the ratchet promotion bar — an independently-effective mechanism that loses to the current champion still banks); each round appends up to `budget.recombinations_per_round` recombinant candidates beyond the designed ones: read the elite commit's changed file bytes, stack onto the current parent, walk the **same** apply->gate pipeline. Same-cell / same-file-conflict / already-tried pairings auto-excluded; pairing outcomes recorded to avoid re-proposing during patience |
| WHERE mechanical binding (paper App. A) | `archive.bind_where` | the cell coordinate's WHERE lever derives from the files the patch **actually touched** (path/suffix -> prompt/knowledge/runtime/config; cross-lever = mixed; no files = edit); the driver's self-declared `patch_where` stays in the ledger for audit and never decides the coordinate — zero modeling noise on the WHERE axis, noise concentrated on WHY |
| ①'s flip analysis (rescued/regressed) | `scoring.flip_summary` + per-candidate ledger writes in the loop | every full-confirm candidate is compared per-task against the round's control: rescued / regressed / still-failing, written into outcome.stats, the node ledger, `failure_map.json`'s `_flips`, and via outcome_hook into design history — next round's designer sees causal feedback (who got cured / who got broken), not just a static failure set |
| Division: judgment=model, arithmetic=code | `nodes/semantic.py` | every semantic call goes through schema enforcement + bounded repair retries (the core backstop for weak drivers) |
| Broad driver support: qwen/kimi/claude | `providers/openai_compat.py` + reuse of `judge/llm_client` | OpenAI-compatible endpoints, reasoning models handled |
| Sealed-test iron law | `sealed/runner.py` | test blind-scored into a driver-invisible directory, returning None; leak guard + retention. **Unseal picks the deliverable by train argmax only (paper Alg.1 L140)**, never max-over-test (max over many measurements = selection effect, systematically inflating Δ/retention); the per-round curve is display-only; the report attaches the sealed paired z + credited-2σ label |
| State persistence / resumability | `state/journal.py` + failure-map persistence + `loop.py` writing node ledgers/findings | per-round checkpoints; a killed process resumes; on resume the parent node is rebuilt from the journal's recorded commit SHA, and the AppWorld baseline falls back to rebuilding from the confirm out-dir |
| Scorer format adaptation | `benchmarks/evoagentbench/evolve/adapter.py` + `benchmarks/appworld/evolve/adapter.py` | both benchmarks unified onto the `TaskEval` contract; the upper layers are benchmark-agnostic |

---

## 3.5 Two cross-module conventions (required reading for wiring a new benchmark)

**Two candidate-activation paradigms — pick one, never mix:**

| Paradigm | How a candidate takes effect | Gate-b / preflight | Where used |
|---|---|---|---|
| **env-gated** (SOP §2 ② original) | the patch is committed but env-var gated, default off = byte-identical to vanilla; eval passes `activation_env` | has an activation ledger -> Gate-b attribution and zero-hit preflight available (wire `fired_source`) | SWE / framework line |
| **commit-checkout** (AppWorld line) | the candidate = a real child commit; eval checks the commit out into a worktree and runs there (`cwd=worktree`); the code *is* the candidate, ungated | **has** an activation ledger: python patches must embed `activation_beacon()`, batch injects a beacon dir per attempt, `read_fired_tasks` reads back -> per-task Gate-b attribution; prompt/config patches exempt (focused probe + sentinels cover). The EvoAgentBench line (one subprocess runs the whole job; per-trial injection impossible) is not wired yet — fail-open | AppWorld, and the default for new benches |

New benches default to commit-checkout (cleaner: git lineage guarantees zero
vanilla contamination, and weak models edit files far more reliably than they
emit valid unified diffs); the `activation_env` path in
`make_appworld_backend` is an SWE leftover — don't use both on a new line.

**Confirm-artifact naming is a cross-module contract:** the gate's full
confirm job name = `gates/strategies.confirm_job_name(node_id)`
(=`<node_id>_confirm`); it is simultaneously the dir-type scorer's out-dir
name, and next round's diagnosis finds the parent's failing trajectories by
it. Both reader and writer must use this function — never a hand-written
f-string.

**work_dir on-disk layout** (maintained by the loop; all best-effort, all
resumable):

```
work_dir/
  journal/*.jsonl      # per-round checkpoints (resume control state + the sealed unseal's curve material)
  nodes/<node_id>.json # node ledger: identity + git anchor + final status + gate stats + candidate metadata
                       #   (WHY/WHERE/files/beacon/predicate/recombination source — fills the audit fields
                       #    when a bench candidate is not an AppliedPatch and patch is null)
  findings.md          # human-readable per-round record (factual summary + driver verdict)
  failure_map.json     # cross-round accumulated living map (incl. _diagnosed_parents: no duplicate diagnosis)
  archive.json         # GSME bank: per-cell elites + promotion lineage (cells/files) + tried recombination pairs; auto-reloaded on resume
  history.json         # cross-round per-WHY attempt history (incl. flip counts); auto-reloaded on resume — the design step never forgets
  taxonomy.json        # WHY/WHERE taxonomy discovered in induce mode (induced once, reused)
  taxonomy_seed.json   # seed failure map emitted during induction; fed to the loop so round 1 skips re-judging the same trajectories
```

---

## 4. Validated

- **Offline alignment:** recomputed on real SWE round-3 data — σ_screen
  7.3pp, cull line 26.3%, confirm 77.2% match exactly; this also surfaced and
  fixed a navigator-vs-2σ semantic confusion.
- **One real AppWorld round:** real qwen3.6-27B diagnosed failures ->
  autonomously picked VERIFY_FINALIZE (matching the empty-response problem)
  -> ran the candidate -> gates ruled it did not beat the baseline ->
  correctly not promoted, clean termination. The full closed loop ran on real
  components.
- Unit tests: everything orchestrator-related passes (full evolver suite
  545 passed / 16 skipped).
