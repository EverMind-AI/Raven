# Third-Party License Notices

This directory keeps license texts for upstream projects and libraries whose
notices Raven preserves.

Raven itself is licensed under Apache-2.0. Some imported code, references, and
design influences originated from MIT-licensed projects; those notices remain
here so downstream users can audit attribution without searching through the
history.

## Files

- `MIT-hermes-agent.txt` - Hermes Agent license notice.
- `MIT-ink.txt` - Ink license notice.
- `MIT-cherry-studio.txt` - the provider and model brand marks under
  `ui-web/src/assets/providers/`, taken from `@cherrystudio/ui`
  (CherryHQ/cherry-studio, commit 6beb5e1b7e, 2026-09-07) at
  `packages/ui/icons/{providers,models}/{light,dark}`. That package declares
  MIT in its own `package.json` and README; the repository root is AGPL-3.0 and
  does not reach it. Files are copied verbatim, so each is byte-identical to
  the one upstream ships and the attribution is checkable with `cmp`. A
  `<name>-dark.svg` is upstream's `dark/` drawing of the same mark.
  The marks themselves are the vendors' trademarks, shown to identify the
  vendor whose row they sit on and not modified.
  One exception to verbatim: `dmxapi.svg` is the only mark upstream publishes
  as a raster in an SVG wrapper, at 342x342 for a slot drawn 20px wide, and
  143 KB of the bundle for one row. The wrapper is upstream's byte for byte and
  the artwork is unaltered in design or colour; only the embedded PNG is
  resampled to 96x96, which `cmp` will therefore not match for this file alone.
  Regenerate it with scripts/refresh_dmxapi_mark.py.
- `MIT-lobe-icons.txt` - twelve of the fourteen sub-agent brand marks under
  `ui-web/src/assets/agents/`, and `providers/vllm.svg`. Those twelve keep
  upstream's filenames, so each is byte-identical to the one
  `@lobehub/icons-static-svg@1.95.0` ships under that name and the attribution is checkable
  with `cmp` rather than by trust. `raven.svg` is this project's own mark, drawn here and
  not upstream's, so no `cmp` applies to it. The rest of `providers/` moved to the vendors'
  own colour marks; see the Cherry Studio entry above.
- `MIT-nanobot.txt` - Nanobot license notice.
