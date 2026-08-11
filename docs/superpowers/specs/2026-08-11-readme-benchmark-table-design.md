# README Benchmark Table Design

## Goal

Replace the benchmark image with native Markdown so the evidence loads immediately, remains searchable and accessible, and does not depend on a second GitHub User Content image.

## Scope

- Keep the Raven hero banner unchanged.
- Remove the benchmark image from `README.md`.
- Replace the benchmark image and the three repetitive summary bullets with one Markdown table.
- Keep the evaluation caveat below the table.
- Keep official source links attached to the benchmark names.

## Table

Use three columns so the table remains readable on narrow GitHub layouts:

| Benchmark | Raven Result | Comparison |
| --- | --- | --- |
| Efficiency | `56.7%` at 27B; `58.1%` at 397B | Hermes `46.8%` / `47.9%`; `+9.9pp` at 27B |
| Self-evolution | Ranked `#1` on EvoAgentBench | `+6.2pp` over the next result across four methods |
| Proactivity | `0.60` F1 on ProAgentBench | `2.4x` Hermes/OpenClaw at `0.253` |

The final README links `Efficiency` to the Raven benchmark overview, `Self-evolution` to the EvoAgentBench methodology, and `Proactivity` to EverMind benchmark updates.

## Verification

- Confirm the README contains only the hero banner image.
- Confirm each benchmark source link is present in the table.
- Run `git diff --check` and `make check-large-files`.
- Preview the rendered table on the pull request branch.

## Rollback

Revert the table commit to restore the benchmark image reference.
