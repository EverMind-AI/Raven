# EverOS memory — extraction & retrieval E2E test plan

End-to-end test plan for the `raven_everos` memory backend over a real
`everos` runtime. Validates dual-track recall (user-side memory +
agent-side skills) and that memory extraction produces skills matching
expectations.

## Goals / acceptance criteria

1. **User-side recall** — storing a conversation with user facts makes
   `recall(owner_id="user:…")` return episodes/profiles whose content
   matches the facts.
2. **Agent-side skill recall** — repeated task demonstrations are
   extracted and `recall(owner_id="agent:…")` returns `agent_skill`(s).
3. **Skill matches expectation** — an extracted skill's
   `name`/`description`/`content` semantically match the demonstrated
   procedure, with `confidence`/`maturity_score ∈ [0,1]` and non-empty
   `source_case_ids`.
4. **Dual-track isolation** — a user query never surfaces agent skills;
   an unprefixed `owner_id` returns `[]`.
5. **Degradation / contract** — everos absent ⇒ `recall → []`, `store`
   does not raise (covered by unit layer; kept as regression).

## Hard constraints (from everos 1.0.0)

| Constraint | Fact | Test impact |
|---|---|---|
| Accumulate-then-extract | `memorize` returns `status ∈ {accumulated, extracted, skipped}`; extraction fires on a boundary (`hard_token_limit` / `hard_msg_limit`) or an `is_final=True` flush | A turn or two won't extract; tests must cross a boundary or flush |
| Skills cluster from cases | agent pipeline runs `trigger_skill_clustering` + `extract_agent_skill`; skills carry `maturity_score`, `source_case_ids` | The corpus must repeat the same procedure across sessions |
| Backend has no flush | `_RealEverosAdapter.memorize` sends `{session_id, messages}` only, no `is_final` | L2 calls `everos.service.memorize(..., is_final=True)` directly; L3 relies on boundary-by-volume |
| LLM non-determinism | every `extract_*` calls a real LLM + embedding | Assertions are structural + semantic-keyword, never exact-string |
| Runtime deps | `EVEROS_LLM__*`, `EVEROS_EMBEDDING__*`, sqlite/lancedb under `EVEROS_MEMORY__ROOT` | Real LLM key required; store isolated to a temp dir |

### Search item fields (assertion targets — `everos.memory.search.dto`)

- user/episode: `summary, subject, episode, score, atomic_facts`
- user/profile: `profile_data (dict), score`
- agent/skill: `name, description, content, confidence, maturity_score, source_case_ids, score`
- agent/case: `task_intent, approach, quality_score, key_insight, score`

## Layers

| Layer | What | Where | LLM? |
|---|---|---|---|
| L1 | Backend translation (owner routing, message conversion, result flatten, degradation) | `tests/test_em2_backend.py`, `tests/test_em3_http.py` | no (fakes) — default CI |
| L2 | everos extraction quality — direct service + `is_final` flush | `tests/integration/test_everos_extraction_real_llm.py` | yes (`real_llm`) |
| L3 | backend ↔ everos e2e — embedded mode `store`/`recall` | `tests/integration/test_everos_backend_e2e.py` | yes (`real_llm`) |

### Skill-validation strategy (three tiers, increasing strictness)

1. **Structural** (always): fields present + typed; `confidence/maturity ∈ [0,1]`; `score > 0`; `source_case_ids` non-empty.
2. **Semantic keyword** (always): `name/description/content` hit the seeded keyword set (`expect_keywords` in the corpus).
3. **LLM-judge** (opt-in, `@pytest.mark.llm_judge`): an LLM grades whether the skill faithfully summarizes the demonstrated procedure. Closest to "matches expectation" but costly/noisy — kept out of the default `real_llm` set.

## Files

```
tests/integration/
  conftest.py                         # markers, everos_env (gating), ids, corpus, payload helper
  data/everos_skill_corpus.json       # user facts + repeated skill demonstrations + expect_keywords
  test_everos_extraction_real_llm.py  # L2
  test_everos_backend_e2e.py          # L3
docs/everos-memory-e2e-test-plan.md   # this file
```

## Fixtures / environment / gating

- `everos_env` (session): sets `EVEROS_MEMORY__ROOT` → temp dir,
  `EVEROS_MEMORIZE__MODE=agent`, a tight `EVEROS_BOUNDARY_DETECTION__HARD_MSG_LIMIT`,
  clears `load_settings` cache, and **skips** when everos / LLM key /
  embedding model are absent.
- `ids`: unique `user:` / `agent:` / session ids per test for isolation
  (everos service singletons are process-global; we partition by owner).
- `corpus`: loads the seed JSON. `as_everos_payload(...)` mirrors the
  backend's message conversion so L2 and L3 feed everos the same shape.

## How to run

```bash
# unit only (default; no LLM)
uv run pytest tests -m "not real_llm"

# real everos extraction + e2e (requires a configured runtime)
EVEROS_LLM__API_KEY=...      EVEROS_LLM__MODEL=...      \
EVEROS_EMBEDDING__API_KEY=...  EVEROS_EMBEDDING__MODEL=... \
uv run pytest tests/integration -m real_llm
```

## Risks / known gaps

1. **Non-determinism** — handled by the tiered assertions; the must-pass
   set uses only structural + keyword checks.
2. **Backend flush gap** — L3 triggers extraction by volume, which can be
   slow to cluster; the skill-present check `xfail`s rather than flakes.
   Suggested follow-up: add an optional `final` arg to
   `EverosBackend.store` that forwards `is_final` for deterministic
   session close.
3. **Cost / latency** — `real_llm` tests are slow and billable; isolate to
   a dedicated CI job, never the default suite.
4. **Store pollution** — the fixture forces `EVEROS_MEMORY__ROOT` to a
   temp dir and clears the settings cache so `~/.everos` is never touched.

## Naming compliance note (AGENTS.md §5.2)

The production-path / demo smokes were renamed to drop their
ticket/version scope: `tests/integration/test_tui_rpc_production_smoke.py`
and `tests/integration/test_tui_rpc_demo_smoke.py`.
