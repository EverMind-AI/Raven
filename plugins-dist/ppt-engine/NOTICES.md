# Third-Party Notices

The ppt-engine distribution is licensed under the Apache License 2.0, the
licence the vendored Raven-PPT engine it was ported from carries. It bundles
data reduced from the projects below and imports one AGPL library; their
copyright notices and licence texts are retained in `LICENSES/`. These
sections are the fork's own notices (Raven-PPT `NOTICES.md`), carried with the
files whose provenance they record; only the in-package paths are respelled.

## Tabler Icons (editable outline icon data)
- Source: https://github.com/tabler/tabler-icons
- Copyright (c) 2020-2026 Paweł Kuna
- License: MIT -- see `LICENSES/MIT-tabler-icons.txt`
- Scope: a curated 1304-icon subset of the Outline set, taken from `icons/outline/`
  at release `v3.46.0` (commit `8ac7d81b72ece11072ef25ea9fd92e80c6f3c9fc`, source
  archive
  `https://codeload.github.com/tabler/tabler-icons/tar.gz/8ac7d81b72ece11072ef25ea9fd92e80c6f3c9fc`,
  SHA-256 of that archive's bytes
  `6d7ecda12c53a543f305859c247b8beb4266205e40c58487b8469aeaeb7797b1`), with each
  icon's upstream `tags` and `category` header, extracted to the vendor's editable
  M/L/C/Z path grammar on a 24x24 grid and packaged as data in
  `raven_ppt/services/assets/data/tabler_outline.json`; no Tabler runtime is
  bundled. The regeneration pin lives in the data file's own provenance record
  (`raven_ppt/services/assets/icons.py`), which hashes what it is handed and
  refuses an archive the pin does not name.

## Apache POI (DrawingML preset shape geometry)
- Source: https://github.com/apache/poi
- Copyright 2003-2025 The Apache Software Foundation
- License: **Apache-2.0** -- see `LICENSES/APACHE-2.0-apache-poi.txt`
- Scope: `presetShapeDefinitions.xml` from release `REL_5_4_1` (commit
  `4554f204cbbf00ecbcaed134fe57e43a1779a612`, path
  `poi/src/main/resources/org/apache/poi/sl/draw/geom/presetShapeDefinitions.xml`,
  SHA-256 of the file's bytes as served
  `a7dad593d27bd70536b41da9b761fa16409536cc0c25ef2b6c7a61c5d9b3e738`, and of the
  same file with CRLF normalised to LF -- which is what a git checkout on Windows
  hands the converter --
  `4a762444d8d85876881c02a5b1dedf6f73006fcd8acb7b4e393435615b37c780`), converted
  from XML to `raven_ppt/services/assets/data/preset_shapes.json` with the guide,
  path, text-rectangle and connection-site expressions unchanged. The geometry
  itself is the ECMA-376 / ISO-IEC-29500 normative preset table; POI is the
  redistribution it was taken from. No POI code is bundled -- the formula
  evaluator in `raven_ppt/services/assets/shapes.py` is an independent
  implementation of the same normative operator set.

## ppt-master (preset shape intents)
- Source: https://github.com/hugohe3/ppt-master
- Copyright (c) 2025-2026 Hugo He
- License: MIT -- see `LICENSES/MIT-ppt-master.txt`
- Scope: the one-line `intent` per preset and the 43 group labels from
  `skills/ppt-master/scripts/pptx_shapes/data/presetShapeSemantics.json` (v5.0.0),
  reduced to `raven_ppt/services/assets/data/preset_shape_intents.json`. No
  ppt-master code is bundled.

## DejaVu fonts (text measurement)
- Source: https://dejavu-fonts.github.io/
- License: the DejaVu fonts licence (Bitstream Vera derivative) -- carried
  beside the faces as `raven_ppt/services/assets/fonts/LICENSE-DejaVu.txt`
- Scope: `DejaVuSans.ttf` and `DejaVuSans-Bold.ttf`, bundled as measuring
  faces for text-fit checks; no font is embedded into produced decks.

## PyMuPDF (reading the source PDFs)
- Source: https://github.com/pymupdf/PyMuPDF
- Copyright (c) 2015-2026 Artifex Software, Inc.
- License: **AGPL-3.0** (a commercial licence is also available from Artifex)
- Scope: imported by `raven_ppt/services/ingest/` to read a source PDF's text
  with its layout, and to extract the figures and tables embedded in its pages;
  and by `raven_ppt/services/measure/type_size.py` to read the type sizes a
  render resolved. Not vendored, not modified, and no PyMuPDF source is
  redistributed here -- this distribution is Apache-2.0 and calls the library
  through its public API.
- Note for anyone building on this: AGPL obligations attach to *distributing*
  the library or offering it over a network, not to importing it. A local or
  internal deployment carries none; a public service built on this should read
  the AGPL or take Artifex's commercial licence. Rasterisation and word
  positions deliberately use pdfium (Apache-2.0) instead, so the AGPL surface is
  limited to reading source documents.

# External Runtime Tools (not vendored)

LibreOffice (`soffice`) is invoked via `subprocess` to convert built decks to
PDF for rendering and review; `poppler-utils` is an optional fallback
rasteriser where pypdfium2 is absent. Neither is bundled, linked, imported or
redistributed; each is found on `PATH` at runtime and carries its own licence.
