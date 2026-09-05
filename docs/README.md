# Raven Documentation

This directory holds design notes, dated records, and developer references
that are too detailed for the main README.

For first-time users, start at the root `README.md`. Domain terms live in
`CONTEXT.md`, routed from `CONTEXT-MAP.md`. Several files below are dated
records kept for their history rather than descriptions of the current tree;
each such file carries a banner saying so.

## Index

- `dev.md` - local development notes.
- `TRACING_STANDARD_API.md` - the tracing span contract between raven and
  raven-tracing.
- `sandbox/` - BoxLite sandbox usage and debugging notes.
- `specs/` - dated design records (`YYYY-MM-DD-*.md`) plus the self-evolution
  SOP and playbook specs; each describes the tree as of its date.
- `plans/` - historical implementation plans; an archive, deliberately not
  held to today's layout.
- `examples/` - benchmark and runtime configuration examples.
- `Proactivity-Plan.md` / `Proactivity-Implementation.md` - proactive-behavior
  design intent and as-built notes.
- `Proactivity-Cost-Analysis.md` - dated cost snapshot (2026-04).
- `memory-plugin-architecture.md` - design record for the memory-plugin
  architecture; the bundled-in-tree layout it proposes was superseded by
  `plugins-dist/`.
- `everos-memory-e2e-test-plan.md` - dated end-to-end memory validation plan.
- `skill-hub-integration-design.md` - design record; shipped as
  `raven/skill_hub/`.
