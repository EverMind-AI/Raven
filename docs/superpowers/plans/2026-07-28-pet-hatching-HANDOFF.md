# Pet Hatching Phase 0 — handoff / resume state

Machine switch on 2026-07-28. Everything below is pushed to
`feat/memory_driven_pet_hatching` (base: `origin/main`).

## How to resume

1. `git fetch origin && git checkout feat/memory_driven_pet_hatching`
2. Read the plan: `docs/superpowers/plans/2026-07-28-memory-driven-pet-hatching-phase-0.md`
   (11 tasks, 78 steps, complete code in every step).
3. Resume at **Task 2 round-2 fixes** (below), then continue with Task 3.
4. Execution method: `superpowers:subagent-driven-development` — one implementer
   subagent per task, then a reviewer subagent, then fix subagents for
   Critical/Important findings.

## Progress

| Task | State |
|---|---|
| 1. Profile models + `pet` extra | Complete, reviewed clean |
| 2. Redaction / instruction stripping | Code written, 2 review rounds done, **round-2 fixes outstanding** |
| 3. Memory evidence collector | Not started |
| 4. Profile builder | Not started |
| 5. Brief compiler | Not started |
| 6. `ImageGenerationPort` + tool refactor | Not started |
| 7. Hatch run store | Not started |
| 8. Base-preview check | Not started |
| 9. Hatch service | Not started |
| 10. CLI | Not started |
| 11. Domain terms + docs | Not started |

Current test state: `68 passed` (19 models + 49 redaction), ruff clean.

## Outstanding Task 2 fixes (do these first)

Two findings from the second review round, both verified by executing the code.
A fix dispatch for them was drafted but not run.

### A. Critical — quadratic ReDoS in the contacts email pattern

`raven/pet/redaction.py`, the pattern
`r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"` has no literal anchor
before its leading `+`, so on text without an `@` the engine retries at every
offset. Measured: **10.9s** on 80,000 chars of `"x"`; **6.3s** through
`sanitize_memory_text` on a 60 KB input. This runs on untrusted remembered
text, so it is a denial of service on the collector.

Fix with possessive quantifiers (`++`) or an atomic group — both supported on
Python 3.12 — plus realistic length bounds, and consider a cheap `"@" in text`
precheck. Then audit **every** other pattern in the module the same way: time
each on >= 200,000 chars of adversarial input designed to make that specific
pattern fail late, and fix anything over 1 second.

Add a timing regression test that fails if sanitizing a 100,000-character
input containing no `@` takes more than a second.

### B. Important — the widened alternations destroy ordinary vocabulary

Round-1 widening matched these terms as bare words regardless of context. All
six currently drop and must survive:

- `This library aids development significantly.` (aids)
- `Her zodiac sign is Cancer.` (cancer)
- `Uses a hindu-arabic numeral system in the renderer.` (hindu)
- `The project lead is named Christian.` (christian)
- `Prefers a queer-coded aesthetic for the UI, according to the design brief.` (queer)
- `The scheduler handles a data race safely with locks.` (race)

Rework the demographics and sensitive-personal-data patterns so a term only
triggers a drop when it reads as a personal fact — preceded by a personal
subject or attribution cue (`is`, `was`, `has`, `identifies as`,
`diagnosed with`, `suffers from`, `their`, `his`, `her`, `user's`, `they are`).
Prefer a small helper composing "personal cue + term alternation" over one
unreadable megaregex.

These must still drop after the change:

- `identifies as gay and prefers minimalist decor`
- `is HIV positive and prefers quiet evenings`
- `went through a divorce last year`
- `discussed their salary band last review`
- `takes a prescription every morning`
- `is 34 years old`

Add tests for all twelve sentences. Do not weaken, rename, or delete any of the
existing 49 tests.

### C. Minor — logged, fix opportunistically

`tests/test_pet_redaction.py` has a navigation-strip test that duplicates a
case already covered by the `test_instruction_shapes_are_all_stripped`
parametrization.

## Environment note

`uv run` and `uv sync` fail **repo-wide** on an Intel Mac
(`macosx_15_0_x86_64`): `lancedb==0.33.0` publishes no wheel or sdist for that
platform. This is pre-existing and unrelated to this branch — a clean
`uv sync --extra dev` fails identically on `main`.

Workaround used on that machine: the repo's existing `.venv`, driven directly.

```bash
.venv/bin/pytest tests/test_pet_*.py -q
.venv/bin/ruff format raven/pet tests/test_pet_*.py
.venv/bin/ruff check raven/pet tests/test_pet_*.py
```

Dependency declaration still went through `uv add --optional pet --no-sync` and
`uv add --dev --no-sync`, so `pyproject.toml` and `uv.lock` were never
hand-edited.

**On an Apple Silicon or Linux machine, use the plan's commands as written**
(`uv run --extra dev pytest ...`) and ignore this section.

## Conventions in force

- Nothing has been committed by a subagent. Commits happen only on explicit
  request (AGENTS.md 3.4). The commits on this branch were made by the user's
  request at the machine switch.
- Implementer subagents are told: do not commit, do not touch `pyproject.toml`
  or `uv.lock`, use `.venv/bin/*` if `uv run` is broken.
- No bare `assert` in `raven/` product code — ruff selects S101.
- Per-task review diffs are scoped by file path, since there are no per-task
  commits to diff against.
