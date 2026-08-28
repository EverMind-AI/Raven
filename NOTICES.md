# Third-Party Notices

Raven is licensed under the Apache License 2.0. It incorporates code from the
following MIT-licensed projects. Their copyright notices and license texts are
retained in `LICENSES/`.

## nanobot (base agent runtime)
- Source: https://github.com/HKUDS/nanobot
- Copyright (c) 2025 nanobot contributors
- License: MIT — see `LICENSES/MIT-nanobot.txt`
- Scope: forked at v0.1.5.post3; modified throughout `raven/`
  (agent/, bus/, channels/, cli/, config/, cron/, providers/, session/,
   skills/, templates/, utils/).

## hermes-agent (TUI layer)
- Source: https://github.com/NousResearch/hermes-agent
- Copyright (c) 2025 Nous Research
- License: MIT — see `LICENSES/MIT-hermes-agent.txt`
- Vendored at commit: `dd0923bb89ed2dd56f82cb63656a1323f6f42e6f` (2026-05-12)
- Scope: full `ui-tui/` import (169 files / ~31.6k LOC TypeScript src
  + ~26.3k LOC vendored `@hermes/ink`). Modifications: branding replaced
  with minimal Raven component, gateway stubbed (no real IPC; deferred
  to `tui-ipc-bridge` L2), 36+ env vars renamed `HERMES_*` → `RAVEN_*`,
  literal Hermes/Nous brand strings replaced throughout `ui-tui/src/`,
  SPDX/Copyright headers added to every ≥ 50 LOC source file.

## ink (vendored via `@hermes/ink`)
- Upstream source: https://github.com/vadimdemedes/ink
- Copyright (c) Vadym Demedes, Sindre Sorhus, and ink contributors
- License: MIT — see `LICENSES/MIT-ink.txt`
- Scope: hermes-agent ships its own fork of community ink at
  `ui-tui/packages/hermes-ink/` (~26.3k LOC). Raven inherits this
  vendor verbatim (package name `@hermes/ink` preserved for attribution).
  Triple attribution chain (ink contributors → Nous Research hermes-ink
  → EverMind modifications) is encoded in the 5-line SPDX header of
  every substantial file under `ui-tui/packages/hermes-ink/src/`.
  Re-evaluation triggers (Nous halts maintenance / severe CVE / community
  ink converges / patch debt > 500 LOC) are recorded in
  `docs/RepoMem/temp/tui-fork-hermes-import/02-hermes-ink-vendor-vs-community.md`
  (to be promoted to `docs/RepoMem/persist/architecture/hermes-fork-strategy.md`
  at L2 archive time).

# External Runtime Tools (not vendored)

The following tools are invoked by Raven via `subprocess` calls but are
**not bundled or redistributed** as part of any Raven release artifact.
Their attribution here is supply-chain hygiene, not a license requirement.
Users install them separately through their respective package managers.

## tui-use (TUI autotest harness Tier 1 backend)
- Source: https://github.com/onesuper/tui-use
- Copyright (c) 2026 Wei Hong (onesuper)
- License: MIT
- Install: `npm install -g tui-use` (npm package `tui-use`)
- Scope: invoked by `tests/tui/autotest/runner.py::Harness` for PTY-driven
  TUI subprocess control. Selected as Tier 1 backend per Day 0 spike
  (2026-05-20) — all 5 acceptance gates S1-S5 passed. Raven does NOT
  vendor, redistribute, or modify `tui-use` source.
- Fallback contingency: if upstream maintenance halts (>90 days no push) or
  a severe incompatibility surfaces, the L0-L3 ladder in
  `docs/RepoMem/temp/tui-auto-test/tier1-backend-comparison.md` defines the
  vendor-or-pivot strategy. Vendoring (path L1) would require moving
  `tui-use` to `vendor/tui-use/` and adding its LICENSE to `LICENSES/` plus
  updating this section's "Scope" line to "vendored" — same pattern as
  `hermes-agent` above.
