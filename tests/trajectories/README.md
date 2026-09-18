# Trajectory regression cases

One case per directory, three parts (the code is the authority —
`raven/trajectory/regression.py` documents both file schemas in its module
docstring):

- `expect.yaml` — the assertion DSL: where the replay's first divergence must
  land and what the live side must do there.
- `case.yaml` — the human contract: `issue`, `owner`, `why`, `re_record`
  (required, non-blank), optional `risk`/`created_from`, and
  `reviewed_residuals` (per-token human sign-offs on residual-scan findings:
  full-token sha256 plus a reason).
- `cassette/` — a minimized, redacted bundle produced by
  `raven trajectory minimize`. Never hand-copy a raw bundle here; the
  validate gate rejects a cassette without a `minimized` manifest block.

## Workflow

Scaffold a case from a bundle directory, attempt id, trajectory report
tarball, or bug report package:

    raven trajectory regression init <source> --name <case_name>

The scaffold is a draft: fill every TODO in `case.yaml` and shape
`expect.yaml`, then make the gate pass before committing:

    raven trajectory regression validate tests/trajectories/<case_name>

CI runs the replays plus `validate --all` in the `trajectory` job.

## Rules

- Case names are lower snake_case with no version or ticket segments
  (`whatsapp_lid_mapping`, not `fix_v2` or `eve151`).
- `MIN_COMMITTED_CASES` in `tests/test_trajectory_regressions.py` is the
  hand-maintained floor on the committed case count; raise it when a real
  case lands, so deleting a case is a deliberate, reviewable act.
