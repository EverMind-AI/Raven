# Raven operational scripts

Standalone CLIs and admin utilities. Not part of the Raven wheel —
these scripts are invoked directly from a checkout and don't ship to
end users.

## Layout

```
scripts/
├── README.md                     This file.
└── check_mirasim_frames.py       Check a Mirasim host bundle for the frames the Mirasim adapter uses.
```

## When to use

| Script | Purpose | See |
|---|---|---|
| `check_mirasim_frames.py` | After Mirasim updates, check that its host still names every frame and field `raven/acp/mirasim/` speaks. Run it `--against` the bundle that last worked: a name renamed in one of the places that say it then shows as occurring fewer times, which presence alone cannot see. Reads the bundles as text and never runs them; exits 1 naming what is missing or fewer, 2 when a bundle cannot be read. | the module docstring |

## Where fixture generators live

Test / benchmark data generators are NOT in `scripts/` — they live
alongside the benchmark or test that consumes them. Examples:

- `benchmarks/skill_evals/fixtures/build_mock_library.py` — synthetic
  SQLite mass-library DB for `test_skill_forge_e2e.py` and
  `skill_evals/run_eval.py`.
