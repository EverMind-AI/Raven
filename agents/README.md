# agents/

Product definitions built on installed raven, serving over `raven acp`.

What one product directory carries:

- `run.py` -- a stdlib-only launcher: render the product config (secret
  slots, host LLM inheritance, mode catalogue), then exec
  `python -m raven acp --config <rendered>` on the installed raven.
- `config.json` -- the baseline profile; `modes/*.json` -- overlays.
- `subagent.json` -- the roster row template `install.py` registers through
  `raven.config.update_subagents` (the same pinned surface the vendored
  installers use).
- `plugins/<id>/` -- the product's own harness as raven plugins: hooks on the
  loop's six phases, replacement tools under the built-in names, its own
  config slice under `plugins.config["<id>"]`. `run.py` names the directory
  through `plugins.dirs`; nothing in `raven/` knows the plugin exists.
- `soul.md` + the contract the plugin renders into `agent.md` -- the product's
  identity, seeded into the workspace once; `context.dropSegments` keeps the
  host's own identity segment out of the prompt.

Ground rules:

- `subagents/` is frozen and is the A side of the comparison; nothing here
  may import from it or modify it.
- The runtime never imports this directory -- enforced by the import-linter
  contract "the runtime does not import the agents pilots".
- Outside the wheel: `packages = ["raven"]` already excludes it.
- Acceptance for a product is transport-face equivalence against its vendored
  twin: transcript shape, timeout semantics, everos records field by field.
