# agents/

Product definitions built on installed raven, serving over `raven acp`.
Five products today: `raven-code`, `raven-design`, `raven-oncall`,
`raven-ppt`, `raven-research`.

What one product directory carries:

- `run.py` -- a stdlib-only launcher: render the product config (secret
  slots, host LLM inheritance, mode catalogue), then exec
  `python -m raven acp --config <rendered>` on the installed raven.
- `config.json` -- the baseline profile.
- `modes/*.json` -- optional per-session overlays, surfaced as `acp.modes`
  for a client's mode picker. A product with no mode picker ships no
  `modes/` (every product but `research` today): its render carries no `acp`
  block, so `session/set_mode` stays method-not-found.
- `subagent.json` -- the roster row template `install.py` registers through
  `raven.config.update_subagents` (the same pinned surface the vendored
  installers use).
- `plugins/<id>/` -- the product's own harness as raven plugins: hooks on the
  loop's six phases, replacement tools under the built-in names, its own
  config slice under `plugins.config["<id>"]`. `run.py` names the directory
  through `plugins.dirs`; nothing in `raven/` knows the plugin exists.
  A product's harness may instead ship as a standalone distribution:
  `raven-ppt` and `raven-design` carry no `plugins/` directory -- their
  engines are the `plugins-dist/ppt-engine` and `plugins-dist/design-engine`
  wheels, found through the `raven.plugins` entry-point group.
  Prompt assets ride with the plugin whose conduct they teach, under
  `plugins/<id>/prompts/`: the workspace guide `run.py` seeds once
  (oncall's TOOLS.md section, code's whole-file TOOLS.md) lives there,
  byte-pinned to its fork's template by the launcher tests.
- `soul.md` + the contract the plugin renders into `agent.md` -- optional:
  the product's own identity, seeded into the workspace once, for a product
  that replaces the host identity (research does; its `context.dropSegments`
  keeps the host's own identity segment out of the prompt). A product whose
  vendored twin served the host-generic identity carries no `soul.md` and
  keeps the host identity segment -- oncall, code and design: each of those
  forks' SOUL.md is byte-identical to trunk's own template, its ACP path
  never seeded it into a workspace, so an added identity file would change
  the very prompt face the parity tests pin. `raven-ppt` also carries no
  `soul.md`, for a different reason: its fork's SOUL.md is its own, and it
  rides byte-for-byte at the engine wheel's prompts home
  (`raven_ppt/prompts/`), seeded into the pinned home by the engine plugin's
  hook at first turn.

Product vocabulary:

- **Rendered config** -- what `run.py` writes and execs against: the baseline
  `config.json` after secret slots, host LLM inheritance and the mode
  catalogue are applied. Rendered files live under the state root, never in
  the product directory.
- **State root** -- where the product keeps its work (repos, instance
  buckets, flow stores, rendered configs): `product_state_root()` in
  `raven/config/product_render.py`, default
  `<raven home>/workspace/subagent_sessions/<product>`, overridden by the
  product's `*_STATE_ROOT` variable.
- **ACP home** -- the engine's own Agent home, `product_acp_home()`: under
  the raven data directory, never inside the host's Agent home (the host
  hands its home out as a session working directory, and a raven engine
  refuses a working directory that contains its own home). The product's
  `*_ACP_HOME` variable overrides it outright.
- **Tool-face pin** -- each launcher test pins the exact tool face the
  rendered config exposes (`VENDORED_TOOL_FACE` in
  `tests/test_agents_<product>_launcher.py`) to its vendored twin's face.
- **Seed-once** -- workspace assets (guides, identity) are written only when
  absent (`seed_once()` in `raven/config/product_render.py`), so a user's
  later edits survive relaunches.

Ground rules:

- `subagents/` is frozen and is the A side of the comparison; nothing here
  may import from it or modify it.
- The runtime never imports this directory -- enforced by the import-linter
  contract "the runtime does not import the agents pilots".
- Outside the wheel: `packages = ["raven"]` already excludes it.
- Acceptance for a product is transport-face equivalence against its vendored
  twin: transcript shape, timeout semantics, everos records field by field.
