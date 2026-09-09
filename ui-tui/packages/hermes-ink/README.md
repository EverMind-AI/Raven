# @hermes/ink

A hard fork of the terminal rendering stack that `ui-tui` runs on. Two
upstreams are vendored here, both MIT (see `NOTICES.md` and `LICENSES/` at
the repo root):

- [ink](https://github.com/vadimdemedes/ink) — the React terminal renderer,
  under `src/ink/`, including a TypeScript port of yoga-layout under
  `src/native-ts/` so the package carries no native or WASM dependency;
- the TUI layer of
  [hermes-agent](https://github.com/NousResearch/hermes-agent), which did
  the original vendoring and whose modifications we inherited.

## Fork policy: hard fork, no upstream sync

This package does NOT track its upstreams. There is no sync script, no
planned rebase onto new ink releases, and upstream fixes arrive only if
someone ports them by hand on purpose. That is a deliberate trade, made for
these capabilities, which upstream ink does not carry and which touch its
internals too deeply to live as patches:

- text selection and clipboard integration across the rendered tree
  (`src/ink/selection.ts`);
- OSC terminal integrations (`src/ink/termio/osc.ts`);
- the dependency-free yoga-layout port (`src/native-ts/yoga-layout/`);
- screen/log-update behaviour tuned for a persistent chat transcript
  rather than ephemeral CLI output.

Practical consequences, so nobody relearns them the hard way:

- an ink bugfix or feature you read about upstream is NOT here unless this
  tree says so; check this source, not ink's docs, when behaviour differs;
- fixes belong here directly — do not try to "wait for upstream" or vendor
  a newer ink alongside; there is one rendering stack in the product and
  this is it. Upstream ink appears only as a devDependency of `ui-tui`,
  because `ink-testing-library` resolves it in tests; it is not imported by
  any source file and does not reach `dist/entry.js` (grep the bundle for
  `node_modules/ink/build` — zero hits);
- the fork is ~26k lines we own outright: treat it with the same test and
  review bar as first-party code, because that is what it is.
