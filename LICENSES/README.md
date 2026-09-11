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
- `MIT-lobe-icons.txt` - Lobe Icons 1.95.0 provider SVGs under `ui-web/src/assets/providers/`,
  and twelve of the fourteen sub-agent brand marks under `ui-web/src/assets/agents/`. Those
  twelve keep upstream's filenames, so each is byte-identical to the one
  `@lobehub/icons-static-svg@1.95.0` ships under that name and the attribution is checkable
  with `cmp` rather than by trust. `raven.svg` is this project's own mark, drawn here and
  not upstream's, so no `cmp` applies to it.
- `MIT-nanobot.txt` - Nanobot license notice.
