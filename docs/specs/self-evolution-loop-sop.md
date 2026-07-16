# Self-Evolution Main Loop — Standard Operating Procedure (SOP)

> **Provenance:** English translation of the upstream project's internal SOP
> document. Internal infrastructure details (box addresses, share paths,
> endpoint/tooling names) are redacted in this translation. The two
> evaluation lines it references are **TB2** (an internal terminal-agent
> benchmark) and **EvoAgentBench/LiveCode** (a coding benchmark); Raven's
> shipped example benchmark is AppWorld — see the mapping doc for how each
> clause lands in this repo. Raven's
> implementation mapping lives in
> [`self-evolution-loop-raven-mapping.md`](self-evolution-loop-raven-mapping.md).

**Date:** 2026-06-24 | **Purpose:** TB2 / EvoAgentBench scoring runs follow this to produce paper-composable, uniform results.
**Nature:** operating manual (humans / Claude follow it), not theory. Theory/design sources are listed under "Finalized sources" at the end.
**Companions:** upstream working documents (design-gap notes, three-tier sampling design, paper positioning), internal to the upstream project and not shipped here.
> ⚠ **Cross-repo:** this SOP stays in the upstream harness repo (operational runbook); the paper design docs above moved to the **paper repo `docs/specs/`** on 2026-06-26 (same for every paper doc cited in §6).

---

## 0. General rules (apply to every round)

**★ Gate0 / environment & measurement validity (foremost; two checks, before and after scoring — violation voids that trial's data):**
- **Before scoring — environment health precheck (intra-env / sandbox check):** before running any task, verify the environment itself is sane — sandbox boots, docker networking works, dependencies installed, verifier can produce ctrf. Scores from a dirty environment (e.g. TB2's broken apt mirror in the sandbox image producing false-negative infra_err 38-45, or proxy x docker route hijacking producing connErr) are **all void**; fix the environment first (an infra overlay patch), then run.
- **After scoring — infra-failure handling ladder (detect -> rerun <=2 -> fix infra -> [still broken after 2] record as failure):**
  - **Detect:** ctrf missing / sandbox crashed / verifier timed out = infra failure (L1), **not "the agent could not solve it"**;
  - **★ Rerun (cap 2)** (user 2026-06-25): a trial that died on infra is **re-run once the environment is healthy**, topping K back up — measure the task's true ability;
  - **Fix infra:** for a **deterministic** infra failure (crashes every time, e.g. the broken-apt-mirror case above), rerunning is useless — fix the infrastructure first, then rerun (still counted within the 2);
  - **[Still broken after 2] record as unsuccessful** (user 2026-06-25): the rare task still broken after 2 reruns + an infra fix is **recorded as unsuccessful (numerator 0)** **but stays in the denominator** (no exclusion, no coverage bookkeeping). Every task ends up 0/1 -> clean denominator = total task count.
- **★ pass@1 denominator = total number of tasks (2026-06-25 user, hard rule):**
  - **Denominator = all tasks; never remove infra-failed tasks from the denominator.** Reruns (<=2) turn salvageable tasks into honest measurements; the unsalvageable score 0 and stay in the denominator.
  - **Why:** dropping infra-failed tasks = shrinking the denominator = **overestimating pass@1** (dropped tasks skew hard; cf. the v7 uv false-negative lesson: extrapolating a fair-subset to the full set overestimates).
  - **Three-way contrast (all with denom = total; they differ in how the numerator is filled):**
    - HarnessX A.3: an infra failure **counts as unsolved = 0 on sight** (no rerun) -> also crushes plenty of fixable/transient false negatives (contamination);
    - old "timeout-fair exclude-not-zero": infra failures **removed from the denominator too** -> shrunken denominator -> overestimate;
    - **ours:** **rerun <=2 first + fix infra** (salvaging TB2's 38-45 -> 7 fixable false negatives into real measurements), **only the truly persistent score 0, never excluded** -> denom = total, conservative and unbiased — the core of C1 measurement integrity (Gate-f); the differentiator is "two reruns before judging", not "zero on sight".


**Division of labor:**
- **Semantic operations = Claude (CC SDK):** diagnosis, WHY selection, candidate design, patch writing, verdict drafting.
- **Deterministic code/scripts:** scoring orchestration, gates, bookkeeping, coordinate binding, aggregation.

**Two verdicts (dual thresholds — do not mix):**
| Use | Criterion |
|---|---|
| Internal navigation (enter bank / choose parent) | **K=3 mean pass@1 > vanilla** (loose). ⚠ use the K=3 mean, never a single run |
| External claims (paper "credited") | **paired 2σ** (per-task pairing on the shared task set, σ = measured std of paired diffs, computed per-task transparently). **Paired 2σ < unpaired 2σ:** pairing removes between-task difficulty variance; the residual is dominated by borderline tasks -> TB2 bar ~3-4pp (child ~47-48%) vs unpaired ~8pp (~52%). The paper still says "2σ (z>=1.96)"; only σ is computed paired. Never argue "paired therefore small" verbally — attach the per-task computation. See the gap doc's significance section |

**Core discipline (settled after hard lessons; violations void the data):**
1. Diagnose only from train / failing trajectories; **held-out / test yields numbers only — never read its trajectories** (leak/overfit prevention).
2. Configuration identical throughout (timeout-fair same caps); failure fallbacks **never touch max_tokens/temp/thinking**; dirty data is filtered explicitly at the analysis layer.
3. Comparable only within same benchmark / same K / same verifier; single-run pass@1 swings ~5pp — **bank entry uses the K=3 mean**.

**★ train/test iron law + sealed test set (C3's lifeline; violation voids retention):**
- **train:** scoring + **trajectory reading for diagnosis** -> drives evolution (diagnosis / WHY choice / design / parent selection draw **exclusively** on train).
- **test = sealed test set:** scored for numbers only; never read its trajectories, never modify the harness based on it.
- **Test runs every round, but sealed:** test scoring is done by a **deterministic script and stored where Claude cannot read it**; **during evolution decisions Claude cannot see test numbers** (withheld from the evolution agent). The risk comes from "having seen test, using it" (early stopping / parent choice / subconscious direction leakage), not from the running itself. **Sealing by mechanism (invisible at decision time) is what counts as sealed; "resisting the urge to look" does not** — reviewers will challenge it.
- **Unseal after evolution ends:** pull each round's test numbers -> plot train/test curves (catch the overfitting knee) + pick the highest-test round as the deliverable + compute **retention rate = test lift / train lift** (the C3 metric).
- = sealed-but-logged (run and store each round, sealed from the decision maker): you get the curve at zero leakage; leak risk ≈ fully-sealed (score only at the very end). Mirrors the division of labor: test scoring = deterministic script (run + store, never shown) / evolution decisions = Claude (fed train only).
- **Paper wording:** "We evaluate on a **sealed test set**: scored each round by an automated harness but **withheld from the evolution agent** — no test signal informs diagnosis, candidate design, or parent selection; unsealed only after evolution completes." (Contrast HarnessX §6.1's own admission of same-set, no held-out.)

---

## 1. Cold-start SOP (once per (model x benchmark))

> ★ **Cold start also uses train only:** "full set / failing tasks / failure map" below all mean the **train set** (test is sealed and never enters diagnosis / sampling / the thick ledger). Vanilla's test score is blind-run by script and stored; it plays no part in cold start.

```
1. Run vanilla (root) on the train full set x K=3 -> thick ledger (for descendants to borrow + the locked anchor baseline)
2. Diagnosis scope (failing tasks):
   - train set <= 200 tasks -> read every failing task (borderline + always-fail); sample a few always-pass as controls/sentinels
   - train set > 200 tasks -> stratified sampling of failures by "failure signature" for coverage (this work: TB2 89 / LiveCode 97, both <= 200, read all)
3. Claude diagnoses failing trajectories one by one, using only "failure signatures (source A, native to raw trajectories)":
   finish_reason=length + empty (thinking-runaway / empty response) / repeated tool_call / iteration-cap hit / docker errors / code never pasted ...
   (no affinity — no mechanisms exist yet)
4. Emit a structured failure_map.json along the way (lightweight, CC SDK schema output, not the four-stage pipeline):
   each entry = { WHY (failure mode), which tasks (task_id + stability bucket/signature), WHERE-hint (soft lever suggestion, non-binding) }
5. Coverage check: number of WHY classes covered by the failure map (target >= ~7); enough -> cold start complete
```
Deliverables: `vanilla thick ledger (full set K=3)` + `failure_map.json`.

---

## 2. Per-round evolution SOP (the seven-step funnel)

> Tags: [now] do at current scale / [defer] only for large benchmarks / [human=Claude] semantic step.
> **Who does which step, calling which code -> see §8** (this section is flow only; §8 is the Claude Code <-> prepared-code division).

**One round = one "diagnose -> design n candidates -> each candidate takes its own anchor fork -> choose parent".**
"Return after the anchor" = that *candidate* was judged a clear loser at screen, culled on the spot, **never reaching the full set** — not every round walking the full set.

```
① Diagnose [human] — round 1 vs later rounds:
   - round 1: no "previous round's child" yet -> use the cold-start failure map directly (built from vanilla failures).
   - round 2+ (re-diagnosis): read the previous round's CHILD trajectories (not a rescan of all pass/fail), looking at:
     tasks where the mechanism fired (did it work / better or worse) + flipped tasks (rescued = confirmed credit /
     broken = regression) + still-failing tasks (next target) -> append-update the failure map.
   Note: the failure map is a CROSS-ROUND LIVING MAP (cold start builds v1; every round appends new failures +
   WHY-distribution drift), never frozen — that is exactly its value: auditable + round-comparable.

② Choose WHY + design candidates C1..Cn [human]: pick this round's target WHYs from the failure map; design candidates
   per WHY, across levers (prompt/knowledge/runtime/config). Patch env-gated, default off = byte-identical to vanilla,
   never touching the kernel.
   · Candidate count n (decided 2026-06-24, small-benchmark standard tier): 1-2 WHYs per round x 2-3 candidates per WHY
     = 3-4 candidates/round. n is driven by "how many WHYs this round's diagnosis deems worth attacking" + capped by
     budget, not fixed; the full 3x3=9 only on large budgets [defer].

③ Free pruning (zero GPU) [now]: beacon_guard (reject anything without activation_beacon) + preflight (trigger
   predicate with zero historical hits = inert, never fielded).

──── for each candidate Ci (candidate-level fork) ────────────────────────
④ Apply, create child node [deterministic]: git apply (path_guard shields the kernel) -> create node + bookkeeping (nodes/)

⑤a Prober screen: anchor subset (~15 tasks = affinity majority + always-fail icebreakers + 2-3 always-pass sentinels) x K=1
    · ★ The anchor draws from the TRAIN pool only, never test (test is sealed; the anchor is part of evolution
      decisions, touching test = leakage). Hence anchor ⊂ train. Screen/confirm both compare to vanilla on train.
    · Borrowing [defer]: tasks the mechanism does not fire on borrow the parent's thick ledger; the child skips them / K=1
    · Three-tier verdict (★ generous-pass; noise band σ_screen below):
        clearly above vanilla         -> ⑤b full set
        slightly below / inside band  -> ⑤b full set (C3: slightly-low is NOT culled! anchor mean != full-set mean)
        below vanilla by > ~1.2-1.5x σ_screen -> ✂ cull Ci, skip ⑤b/⑥, record status=pruned_at_screen
            (light bookkeeping: Ci / anchor performance / cull reason; those anchor results enter the thick ledger
            for borrowing; prevents next round redesigning the same Ci)
    · ★ σ_screen source (decided 2026-06-24): from the cold-start thick ledger (vanilla full train x K=3) take the
      anchor's ~15 per-task pass rates p_i and compute σ_screen = sqrt( (1/n²)·Σ_i p_i(1−p_i) )
      (= the sampling σ of a K=1 anchor mean). This is the BIG, LOOSE σ (K=1 + small n + deliberately high-variance
      borderline tasks) != Gate2's "full-set K=3 paired σ" (small, tight) — same ledger, two calibers.
      · Cull threshold = 1.5·σ_screen (never a hardcoded pp); "inside the band" = within ±σ_screen (indistinguishable
        from vanilla — still goes to the full set).
      · ⚠ The old "−6pp" is void: a too-tight placeholder (≈0.5 σ_screen; it would cull indistinguishable candidates,
        violating generous-pass). Rough estimate 9-13pp, far above the 5pp of a single full-set run.
      · ★ Who computes it: σ_screen + cull_threshold are emitted by select_anchor() (same ledger, same p_i(1−p_i),
        shared with borderline-task selection, see §8.2) — never Claude estimating on the spot.

⑤b Estimator / full-set confirm (screen survivors only): full set x K=3
    · TB2 89 / small benchmarks: estimator = the full set; large benchmarks that cannot afford it: representative
      subset (stratified by share + weighted) [defer]

⑥ Three shields [deterministic]:
    · Gate0/Gate-f (measurement validity, see general rules ★): environment health precheck + the infra-failure ladder
      (detect -> rerun -> fix infra -> report coverage on exclusion, never silent zeros). TB2's infra overlay fixed
      the apt-mirror false negatives (38-45 -> 7). Contrast HarnessX A.3 "infra counts as failure" = contamination.
    · Gate-b: attribution allowed only when the activation ledger recorded the mechanism firing
    · Gate2: paired lift; navigation pass@1 (K=3 mean) > vanilla -> enter bank, status=promoted; failed -> archived
      as pruned_at_confirm
──────────────────────────────────────────────────────

⑦ Choose parent [human/deterministic]: best in bank -> next round's parent; if every candidate was culled / none
   passed -> no new parent: keep the old parent + fresh diagnosis, proceed to the next round.
   -> check termination (below); if not triggered, back to ①
```

**Loop termination (decided 2026-06-24 by user; stop on either):**
```
① 10 consecutive rounds without a harness "better than vanilla on train"   <- primary signal (exploration exhausted)
② 20 rounds reached (hard cap, backstop)
```
- **★ ①'s baseline is vanilla, not the previous round's parent** (user emphasis): each round asks "did any candidate beat **vanilla** on train K=3 mean pass@1"; 10 consecutive rounds with **not one candidate above vanilla** -> stop. A fixed bar is more stable and reproducible than a rising one, and matches the promotion criterion.
- **Never consult test to decide stopping** (sealed iron law): ① uses train-vs-vanilla only.
- **10 / 20 are initial values, tuned on real runs** (a low-ceiling model may hit 10 promotion-free rounds quickly = ceiling signal).
- **Post-termination wrap-up:** (a) unseal test -> plot train/test curves + pick the highest-test round as deliverable + compute retention rate; (b) record the stop reason (10 promotion-free rounds / 20-round cap) — honest logging, never "ran enough, stopping".

**Node statuses (bookkeeping labels):** `pruned_at_screen` (died at anchor) / `pruned_at_confirm` (failed the full set) / `promoted` (entered bank as parent) / `pruned_inert` (died at ③ preflight). Every candidate leaves a record wherever it dies (auditable + ledger reuse + duplicate-design prevention).

**WHERE coordinate binding** (diagnosis only emits hints): once the patch is written, WHERE is bound mechanically from the artifact (target_file); modeling error concentrates on the WHY axis.

---

## 3. State persistence standard (uniform across windows/benchmarks — the SOP's core value)

**The loop is Claude-driven, not program-driven; state lives in three persistence layers (validated on LiveCode; standardized for both):**

| Layer | Content | Files |
|---|---|---|
| **findings work log** (primary state) | one section per round: what was tried, results, next target | `docs/specs/handoffs/<date>-<bench>-evolution-log.md` |
| **memory** (cross-session, read directly at cold start) | project state (updated per round) + discipline feedback | `memory/project_<bench>_*.md` + `MEMORY.md` index |
| **box durable artifacts** (diagnosis raw material + result evidence) | failure_map.json / per-round reports (pass@1 + failure distribution) / session.jsonl (trajectories = next round's read-only input) / node ledger | `nodes/` (per-model node ledger dir) / `reports/` / `jobs/*/session.jsonl` |

**Uniform requirements (both benchmarks, or the paper cannot compose them):**
- Every round's reports must contain: **per-task results (passes / K)** (for the paired σ) + failure-signature distribution + WHY distribution;
- **pass@1 denominator = total task count** (§0 hard rule): infra-failed tasks rerun (<=2) into valid measurements, never dropped; still broken after 2 -> score 0, stay in the denominator;
- mechanism patches: env-gated; record the flag name + target_file (for WHERE coordinate binding);
- scoring job names carry: benchmark / mechanism / K / split (train|test|full) / date.

### 3.1 Harness kept in git + node ledger (the physical form of the evolution tree)

**The evolution tree is not an abstraction; its physical form is exactly two things — git + the node ledger:**
- **Code lives in git:** each candidate harness = one **git branch + commit**; **C0/vanilla = the baseline branch**. Mechanism candidates = branches grown from the baseline/parent (e.g. `evolver/rN-*`). Mechanism patches are env-gated, default off = byte-identical to vanilla.
  ⚠ **Evolver infrastructure (select_anchor / gates / sampling) is written to the C0 baseline, never to a mechanism candidate branch** (the evolver operates on the harness; it is not itself a mechanism).
- **The tree structure lives in the node ledger** `nodes/*.json` (one node per file), anchored to git:

```json
{ "node_id": "R4F", "parent_id": "v7",          // tree lineage (parent_id) + git lineage, both recorded
  "git_branch": "evolver/r4f-std-v3", "git_commit_sha": "a996daa",
  "core_version": "v7", "created_at_iter": 4,
  "status": "promoted | pruned_at_screen | pruned_at_confirm | pruned_inert | archived-not-credited",
  "activation_spec": { "kind": "...", "threshold": N },   // mechanism trigger predicate (Gate-b / preflight)
  "patch": { "patch_where": "loop_override", "patch_why": "...",
             "components": [ { "target_file": "...", "diff": "..." } ] } }  // WHERE bound from target_file
```

- **How it is used:** `select_anchor` / sampling / diagnosis read the vanilla thick ledger -> Claude designs a patch -> new branch commit + one `nodes/<id>.json` anchored to that commit -> evaluate -> backfill `status`. The node ledger = the single source of truth for auditability + ledger borrowing (parent_id tree distance) + duplicate-design prevention; git is the code source of truth; the two align via `git_branch`/`git_commit_sha`.

---

## 4. Two-benchmark adaptation (difference table)

| Dimension | TB2 | EvoAgentBench (LiveCode) |
|---|---|---|
| Scoring | internal verifier (timeout-fair overlay) | official lcb_runner check_correctness (subprocess sandbox) |
| **split** | **train 53 / held-out 36 (cut, sealed)** `s_tb2_{train53,heldout36}.txt` | **train 97 / test 39 (official)** <- the C3 generalization main experiment lives here |
| Scoring flow | anchor screen (~15 from train53) -> confirm train53 -> held-out36 sealed blind run | currently: train+test both full-run K=3 (97 tasks are cheap enough) |
| Scale | 89 | train 97 / test 39 |
| Infra critical | sandbox infra overlay (docker0 bridge), fixed apt-mirror false negatives (38-45 -> 7) | NO_PROXY=* direct connection (avoids proxy x docker route hijacking) |
| K=3 orchestration | `--attempts 3` single job | conc2x2 interleaved (controls endpoint drift) |

**Uniform actions (paving C3 for the paper):**
- ~~TB2 should also cut a train/test split~~ **done (53/36, 2026-06-24)**; both benchmarks now have splits and can enter C3.
- Both benchmarks report held-out **retention rate = test lift / train lift** (the C3 metric), never just two absolute numbers.
- ⚠ The anchor currently draws from **train53** (~15/53 ≈ 28%, above §2 B's default 15-20% — normal for a small train set); σ_screen computed from the train53 thick ledger.

---

## 5. One-round checklist (tickable)

```
[ ] ① Read the previous round's child trajectories for diagnosis (failures only; never held-out) -> update failure_map
[ ] ② Choose WHY + design candidates (env-gated, across levers, never touching the kernel)
[ ] ③ preflight + beacon_guard prune inert candidates (zero GPU)
[ ] Before scoring: Gate0 environment health precheck (sandbox/docker/deps/verifier); fix a dirty environment first
[ ] Per candidate Ci: ④ apply + bookkeeping -> ⑤a anchor K=1 (three-tier generous-pass)
[ ]   ├ hopelessly bad -> cull Ci, record pruned_at_screen, skip the full set ("return after the anchor")
[ ]   └ clearly above / slightly low / inside the band -> ⑤b full set K=3 -> ⑥ Gate0/Gate-f (infra rerun -> exclude,
        never zeroed) / Gate-b / paired lift
[ ] ⑦ promotion (K=3 mean pass@1 > vanilla) -> best in bank becomes parent; all dead -> keep the old parent;
      three persistence layers + node status labels
[ ] test = sealed: script-run and stored where decisions cannot see it (withheld); decisions use train only
[ ] For "credited": paired 2σ (per-task computed σ)
[ ] Termination: 10 consecutive rounds with no candidate train>vanilla, or 20 rounds -> stop (never consult test)
[ ] After evolution, unseal: train/test curves + highest-test round + retention rate = test lift / train lift
```

---

## 6. Finalized sources (per-step basis)
> ⚠ **All paper design docs below live in the paper repo `docs/specs/`** (moved out of the harness repo 2026-06-26); this SOP stays in the harness repo.
- Cold start / failure signatures / 200 threshold / failure_map: upstream design-gap notes §3b, failure_map section (internal, not shipped here)
- Three tiers / anchor / borrowing / N* / paired 2σ: `2026-06-16-sampling-and-decision-design.md` + the gap doc's significance-caliber section
- Two ablations (anchor / borrowing): gap doc §3c
- Contributions C1/C2/C3 + wording red lines: `2026-06-15-paper-positioning.md` §4c
- Three shields / gate definitions: `2026-06-12-gsme-theorems.md` (Gate <-> (b,f)), `2026-06-15-evolver-code-architecture.md`

## 7. Current status (honest labeling, 2026-06-24)
- Actual runs = run_round MVP + scripts + Claude-in-loop, **not a fully deterministic funnel**; ② 3x3 breadth / ③ preflight / ⑤ borrowing are mostly missing or deferred.
- Top todo (outside this SOP; the main experiment): turn LiveCode into **GSME vs task-aware two arms + report retention** (the C3 headline result).
- TB2 numbers (van 44% / harness 45.3%) had infra issues at the time of writing and were being re-measured upstream; treat them as illustrative, not final.

## 8. Claude Code <-> prepared-code contract (who does which step / calls which code)

### §8.0 Guiding principle: semantics via Claude Code, deterministic arithmetic via prepared code
- **Semantic operations = Claude Code:** reading trajectories for diagnosis, choosing WHY, designing patches, writing code, judging "exploration incomplete vs ceiling".
- **Deterministic / arithmetic = prepared code:** task sampling, borrowing discounts, bookkeeping, σ computation, gate verdicts, aggregation.
- **Why anchor selection / borrowing must be code, never Claude improvising** (the heart of this contract):
  1. **Reproducible / auditable** (the paper's lifeline): which 15 tasks, what discount, what the paired 2σ works out to — must be computed by deterministic scripts and checkable. Claude "picking tasks by feel, eyeballing a discount" = irreproducible, unauditable = exactly the "ad-hoc diagnosis" pit we criticize HarnessX for.
  2. **LLMs cannot compute:** Bernoulli-variance ranking, ancestry-kernel discounts, exact paired σ = precise numerics; mental math will be wrong -> hand it to code.
- **Claude Code's role at these steps = invoke the code + read its output to make the next semantic decision**, never computing it itself.

### §8.1 Step-by-step division (maps §2's seven steps)
| §2 step | Claude Code does | Prepared code (deterministic) | Hand-off |
|---|---|---|---|
| ① Diagnose | **read trajectories, judge WHY/severity/WHERE-hint** (semantic) | failure-signature pre-extraction `analysis/proxy_features.py`; living map `analysis/failure_map_builder.py` | code emits signature + failure_map.json -> Claude reads, writes the diagnosis |
| ② Choose WHY + design + write code | **all Claude** (target choice, patch design, patch code) | — | Claude produces the patch artifact (WHERE mechanically bound from the artifact) |
| ③ preflight prune | reads the prune verdict, decides to drop or not | **`activation/preflight.py`** (reachability over historical trajectories, zero GPU) | code emits reachable? -> Claude decides |
| ④ apply, create node | triggers | `tree/*` (node creation + git + bookkeeping, deterministic) | code creates the node, returns node_id |
| **⑤a anchor selection** | triggers + reads the subset | **`analysis/stability_bucket.py` (bucketing) + `scheduler/bandit_tasks.py` (variance ranking) + `scheduler/affinity_picker.py` (mechanism-fire tasks)** | code emits the anchor task list (⊂ train) |
| **⑤ borrowing** | triggers | **`scheduler/tree_aware_bandit.py`** (ancestry-kernel discount + weighted posterior, pure math) | code emits "which tasks skip runs / at what discount" |
| ⑤b full-set confirm | triggers + reads results | run the full set + `scripts/aggregate_keq3.py` (K=3 aggregation) | code emits per-task + pass@1 |
| ⑥ Gate0/f/b/2 | reads gate verdicts | **Gate-f `scripts/gate0_ctrf_audit.py` (-> the external eval engine) / Gate-b `activation/gate_audit.py` / Gate2 paired σ `analysis/paired_significance.py` (generic, shared by 6 benchmarks)** | code emits pass/fail + σ -> Claude decides |
| ⑦ choose parent | **threshold verdict = code; "exploration incomplete vs ceiling" = Claude** | bank comparison (deterministic) | code emits the promotion verdict; Claude makes the semantic termination call |

### §8.2 Code inventory: parts present vs missing
- **✅ Present** (verified, in the repo): preflight, the anchor trio, borrowing, Gate-f/b, paired σ + retention (`analysis/paired_significance.py`, generic, shared by 6 benchmarks), K=3 aggregation, failure_map builder, failure signatures.
- **✅ `select_anchor()` built** (2026-06-24, on a feature branch off the C0 baseline, **unmerged** at the time of writing): `evolver/scheduler/anchor_selection.py` — returns `AnchorSelection(task_ids, σ_screen, cull_threshold=1.5·σ_screen, tasks, shortfalls)` in one call. Pure ledger read (depends only on `stability_bucket.compute_stability`): three tiers = sentinels (STABLE_PASS) + icebreakers (STABLE_FAIL, affinity-preferred optional) + borderline (BORDERLINE_*, ranked by `p(1−p)`); σ_screen dominated by borderline tasks; deterministic (ties by task_id). Unit tests 5/5 + 40 existing subsystem tests unbroken. **Todo:** ① run on the box against the train53 ledger for the real σ_screen ② commit/push/merge into C0.
- **⏸ sealed-test runner — not building yet (user decision 2026-06-24: mechanism design unsettled; interim relies on Claude discipline):**
  - **Interim discipline (the concrete content of "discipline", tickable):** during evolution, no decision of any round (diagnosis / WHY choice / design / parent selection / early stop) **ever reads test scores or trajectories**; test scoring runs and is stored only, **never opened in a decision context**; looked at only at the final unseal.
  - **Honest consequence (must be addressed at paper time):** this is **discipline-based sealing, not mechanism isolation**; §0 itself says "resisting the urge does not count — reviewers will challenge it". Before submission, one of: ① build the sealed-test runner (mechanism isolation); ② disclose honestly in limitations that test was discipline-sealed + provide auditable evidence (e.g. each round's test-score file timestamps predate that round's decision records). **Currently accepted as ① not built; on the books.**
- **✅ The two items formerly listed as missing are done in the upstream harness repo** (verified 2026-07-01, superseding the old round7_paired):
  1. **retention rate computation** — `evolver/analysis/paired_significance.py` (outputs retention).
  2. **generic paired σ** — same file: per-task paired z = Δ̄/(σ_paired/√n), credited iff |z|>=1.96, + p / 95% CI / bootstrap; shared by 6 benchmarks (moved from the paper repo's e0 into the harness repo).
  Note: **the gate ablation (paper §15, comparing Δ>0 / single-run / paired-2σ gates) is not a loop step**; it is a one-off methodological contrast, staying in the upstream analysis scripts, not entering the evolver.

### §8.3 Interface shape & integration timing (scale-dependent)
- **Current state = manual orchestration:** Claude (a human) runs the scripts above by hand, reads outputs, strings a round together. The parts exist; the "Claude Code auto-invokes code through a whole round" integration does not.
- **Small scale (TB2 89 / LiveCode):** few tasks, full-read/full-run affordable, anchor/borrowing rarely needed -> **manual orchestration + occasional scripts suffice**; no heavy integration.
- **Large scale (SWE-bench 500):** anchor/borrowing become genuinely necessary and frequent -> **then it pays to wrap these as one-click tools for Claude Code** (CLI subcommands / MCP tools) invoked directly at decision time.
- **In one sentence:** anchor/borrowing = deterministic arithmetic, computed by code and invoked by Claude Code (never improvised — unauditable and numerically wrong); packaging into one-click tools is a large-benchmark task; small scale runs fine on manual orchestration.

---

## 9. Pre-run dependencies (what this document does NOT cover — honest boundary)

**This SOP = a method spec (how to walk / what calibers / why test must not be peeked at / when to stop), not a turnkey runbook.** A fresh window can independently understand and execute the loop logic from it, but actually producing scores needs the external dependencies below. Both lines' benchmark/infra wiring has its own authoritative runbook:

### §9.1 Benchmark / infra wiring (authoritative runbooks; this SOP only points)
| | TB2 | LiveCode (EvoAgentBench) |
|---|---|---|
| **Runbook to follow** | `<internal runbook, redacted>` | `<internal runbook, redacted>` |
| box | `<internal box, redacted>` | same box |
| Deploy | `<internal paths, redacted>` | `<internal share, redacted>` |
| Model/endpoint | `<subject model, redacted>`, `<internal endpoints, redacted>` (rotating, must be in no_proxy) | same model, `NO_PROXY=*` direct |
| split | `s_tb2_train53.txt` / `s_tb2_heldout36.txt` (sealed) | `splits/livecode_task_split.json` train97/test39 |
| Invocation | `<internal scoring script, redacted>` `<ARM> <abs tasklist> <SUFFIX>` | `framework/.venv/bin/python src/run.py --config <cfg> --split <train\|test> --parallel N --job <name>` |
| vanilla anchor | vanilla baseline arm pass@1 0.440 | C0 train 64.6 / test 58.1 |
| Scoring | `result.json` reward==1.0, excluding exception_info (timeout-fair) | official `lcb_runner` check_correctness |

### §9.2 Code not yet built (= §8.2; affects "methodological cleanliness", not "can it run")
- ★ **Sharpest:** **the sealed-test runner is still unbuilt** — both lines' current sealing **relies on discipline** (memory feedback constrains to train-only reads), **not mechanism isolation**; §0 itself rules "resisting the urge does not count as sealed — reviewers will challenge it". True sealing requires "script runs test + stores where Claude cannot read".
- `select_anchor()` (with σ_screen) **built and merged** (`evolver/scheduler/anchor_selection.py`); retention computation + generic paired σ also merged into `evolver/analysis/paired_significance.py` (the gate ablation e6 is paper analysis, not loop, staying in the paper repo). §9.2's only genuine gap now is the sealed-test runner (mechanism isolation).

### §9.3 Must-do before running (once per model x benchmark)
- Cold start produces the `vanilla thick ledger (K=3 full train)` + `failure_map.json` (§1). **Current state (2026-06-24):**
  - **Thick ledger ✅ both lines** — TB2 vanilla baseline arm (full-89 K=3, train53 = the screening subset) / LiveCode C0 K=3 train97.
  - **Structured failure_map.json ❌ neither line** — currently ad-hoc / signature-classified diagnosis, not §1's `{WHY, tasks, WHERE-hint}` json. Cheap fix: **no re-scoring needed** — one CC SDK pass over the existing vanilla failure trajectories emits it (raw material on the box). The ledger itself only re-runs on a model/benchmark change.
- Environment health precheck (§0 Gate0): 1-task smoke to validate the endpoint (~10k tokens = healthy), sandbox/docker/verifier available.

### §9.4 Readiness verdict (honest)
- **Can score independently** ✅: both benchmarks wired, splits cut, vanilla anchors in place.
- **Can run a methodologically clean C3** ⚠️: sealed-test runner **deferred** (user decision 2026-06-24); the interim relies on Claude discipline; this is discipline-based sealing — before the paper, either build the mechanism or disclose honestly in limitations (on the books, see §8.2 ⏸).
- **Practice confirms** (LiveCode record §8): the loop = **agent-driven, no state-machine code, scripts only for batch scoring** — consistent with §8.3 "small benchmarks run fine on manual orchestration".
