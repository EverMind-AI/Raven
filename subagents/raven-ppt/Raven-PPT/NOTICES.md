# Third-Party Notices

Raven is licensed under the Apache License 2.0. It incorporates code from some
of the projects below and data from others -- the preset shape geometry, the shape
intents and the icon outlines are data files, with no upstream code bundled -- and
the licences are not all the same. Each entry names its own: MIT for the agent
runtime, the TUI, the intents and the icons; Apache-2.0 for the geometry taken from
Apache POI; AGPL-3.0 for PyMuPDF, which is imported rather than redistributed.
Their copyright notices and license texts are retained in `LICENSES/`.

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

## Tabler Icons (editable outline icon data)
- Source: https://github.com/tabler/tabler-icons
- Copyright (c) 2020-2026 Paweł Kuna
- License: MIT — see `LICENSES/MIT-tabler-icons.txt`
- Scope: a curated 1304-icon subset of the Outline set, taken from `icons/outline/`
  at release `v3.46.0` (commit `8ac7d81b72ece11072ef25ea9fd92e80c6f3c9fc`, source
  archive
  `https://codeload.github.com/tabler/tabler-icons/tar.gz/8ac7d81b72ece11072ef25ea9fd92e80c6f3c9fc`,
  SHA-256 of that archive's bytes
  `6d7ecda12c53a543f305859c247b8beb4266205e40c58487b8469aeaeb7797b1`), with each
  icon's upstream `tags` and `category` header, extracted to the vendor's editable
  M/L/C/Z path grammar on a 24x24 grid and packaged as data in
  `raven/ppt/services/assets/data/tabler_outline.json`; no Tabler runtime is
  bundled. Regenerate with `scripts/build_tabler_icons.py --upstream <that archive>`,
  which hashes what it is handed and refuses to run if it is not what the pin names.

## Apache POI (DrawingML preset shape geometry)
- Source: https://github.com/apache/poi
- Copyright 2003-2025 The Apache Software Foundation
- License: **Apache-2.0** — see `LICENSES/APACHE-2.0-apache-poi.txt`
- Scope: `presetShapeDefinitions.xml` from release `REL_5_4_1` (commit
  `4554f204cbbf00ecbcaed134fe57e43a1779a612`, path
  `poi/src/main/resources/org/apache/poi/sl/draw/geom/presetShapeDefinitions.xml`,
  SHA-256 of the file's bytes as served
  `a7dad593d27bd70536b41da9b761fa16409536cc0c25ef2b6c7a61c5d9b3e738`, and of the
  same file with CRLF normalised to LF -- which is what a git checkout on Windows
  hands the converter --
  `4a762444d8d85876881c02a5b1dedf6f73006fcd8acb7b4e393435615b37c780`), converted
  from XML to `raven/ppt/services/assets/data/preset_shapes.json` with the guide,
  path, text-rectangle and connection-site expressions unchanged. The geometry
  itself is the ECMA-376 / ISO-IEC-29500 normative preset table; POI is the
  redistribution it was taken from. No POI code is bundled — the formula
  evaluator in `raven/ppt/services/assets/shapes.py` is an independent
  implementation of the same normative operator set.

## ppt-master (preset shape intents)
- Source: https://github.com/hugohe3/ppt-master
- Copyright (c) 2025-2026 Hugo He
- License: MIT — see `LICENSES/MIT-ppt-master.txt`
- Scope: the one-line `intent` per preset and the 43 group labels from
  `skills/ppt-master/scripts/pptx_shapes/data/presetShapeSemantics.json` (v5.0.0),
  reduced to `raven/ppt/services/assets/data/preset_shape_intents.json`. No
  ppt-master code is bundled.

## PyMuPDF (reading the source PDFs)
- Source: https://github.com/pymupdf/PyMuPDF
- Copyright (c) 2015-2026 Artifex Software, Inc.
- License: **AGPL-3.0** (a commercial licence is also available from Artifex)
- Scope: imported by `raven/ppt/services/ingest/` to read a source PDF's text
  with its layout, and to extract the figures and tables embedded in its pages;
  and by `raven/ppt/services/measure/type_size.py` to read the type sizes a
  render resolved. Not vendored, not modified, and no PyMuPDF source is
  redistributed here -- this project is Apache-2.0 and calls the library through
  its public API.
- Note for anyone building on this: AGPL obligations attach to *distributing*
  the library or offering it over a network, not to importing it. A local or
  internal deployment carries none; a public service built on this should read
  the AGPL or take Artifex's commercial licence. Rasterisation and word
  positions deliberately use pdfium (Apache-2.0) instead, so the AGPL surface is
  limited to reading source documents.

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
