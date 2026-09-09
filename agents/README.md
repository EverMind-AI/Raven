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
  for a client's mode picker (`research`, `raven-oncall`, and `code` ship one today).
  A product that ships no `modes/` renders no `acp` block, and the raven it
  execs then falls back to its own three built-in tiers -- so
  `session/set_mode` answers with those rather than method-not-found. An
  overlay's `agents.defaults.reasoningEffort` is lifted onto the mode entry as
  the trunk's `reasoningEffort` knob; the rest of an overlay is the product's
  own hooks' to read.
- `subagent.json` -- the roster row template `install.py` registers through
  `raven.config.update_subagents` (the same pinned surface the retired
  vendored installers used). The inverse is the uninstall story -- delete the
  roster row with the interpreter that serves raven:

  ```bash
  # Unquoted on purpose: the shebang may be an `env` line, which is two words.
  $(sed -n '1s/^#!//p' "$(command -v raven)") -c \
    'from raven.config.update_subagents import remove_third_party_subagent as rm; print(rm("raven-code"))'
  ```

  The folder itself can stay -- an unregistered folder is inert. A live raven
  holds the roster it read at startup; restart it afterwards.
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

Product notes:

- `raven-ppt` (the deck product) needs the eight bundled templates the
  `ppt-engine` plugin offers. They are not in git: they are a generic package
  in this project's GitLab package registry, pinned by sha256 in
  `plugins-dist/ppt-engine/templates.manifest.json`. After cloning, run
  `make fetch-templates` with `GITLAB_TOKEN` set to a token that can read the
  repository (CI uses `CI_JOB_TOKEN`); it fills the gitignored
  `plugins-dist/ppt-engine/raven_ppt/assets/templates/`, which the editable
  install serves and the ppt-engine wheel bundles. Without it the engine's
  template catalogue is empty and a deck task has to bring its own template.

Ground rules:

- The A side of the comparison is the retired vendored tree: byte snapshots
  of its record live under `tests/fixtures/vendored_fork/`, and the full trees
  remain in git history (the commit that removed `subagents/` is the anchor).
  Nothing here may re-grow a dependency on that tree.
- The runtime never imports this directory -- enforced by the import-linter
  contract "the runtime does not import the agents pilots".
- In the wheel as data, never as code: `hatch_build.py` maps the tracked
  files to `raven/agents` for the roster's file-level discovery, and the
  import-linter contract still keeps the runtime from importing any of it.
- Acceptance for a product is transport-face equivalence against its vendored
  twin: transcript shape, timeout semantics, everos records field by field.
