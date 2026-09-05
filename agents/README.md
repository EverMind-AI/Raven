# agents/

Product definitions built on installed raven, serving over `raven acp`.

What one product directory carries:

- `run.py` -- a stdlib-only launcher: render the product config (secret
  slots, host LLM inheritance, mode catalogue), then exec
  `python -m raven acp --config <rendered>` on the installed raven.
- `config.json` -- the baseline profile.
- `modes/*.json` -- optional per-session overlays, surfaced as `acp.modes`
  for a client's mode picker. A product with no mode picker ships no
  `modes/` (oncall and code today): its render carries no `acp` block,
  so `session/set_mode` stays method-not-found.
- `subagent.json` -- the roster row template `install.py` registers through
  `raven.config.update_subagents` (the same pinned surface the vendored
  installers use).
- `plugins/<id>/` -- the product's own harness as raven plugins: hooks on the
  loop's six phases, replacement tools under the built-in names, its own
  config slice under `plugins.config["<id>"]`. `run.py` names the directory
  through `plugins.dirs`; nothing in `raven/` knows the plugin exists.
  Prompt assets ride with the plugin whose conduct they teach, under
  `plugins/<id>/prompts/`: the workspace guide `run.py` seeds once
  (oncall's TOOLS.md section, code's whole-file TOOLS.md) lives there,
  byte-pinned to its fork's template by the launcher tests.
- `soul.md` + the contract the plugin renders into `agent.md` -- optional:
  the product's own identity, seeded into the workspace once, for a product
  that replaces the host identity (research does; its `context.dropSegments`
  keeps the host's own identity segment out of the prompt). A product whose
  vendored twin served the host-generic identity carries no `soul.md` and
  keeps the host identity segment -- oncall and code: the fork's SOUL.md is
  byte-identical to trunk's own template, its ACP path never seeded it into
  a workspace, so an added identity file would change the very prompt face
  the parity tests pin.

Ground rules:

- `subagents/` is frozen and is the A side of the comparison; nothing here
  may import from it or modify it.
- The runtime never imports this directory -- enforced by the import-linter
  contract "the runtime does not import the agents pilots".
- In the wheel as data, never as code: `hatch_build.py` maps the tracked
  files to `raven/agents` for the roster's file-level discovery, and the
  import-linter contract still keeps the runtime from importing any of it.
- Acceptance for a product is transport-face equivalence against its vendored
  twin: transcript shape, timeout semantics, everos records field by field.
