# agents/

Agent definitions built on installed raven, serving over `raven acp`.
Five shipped agents today: `raven-code`, `raven-design`, `raven-oncall`,
`raven-ppt`, `raven-research`. A new agent starts as `raven agents new <name>`
-- the scaffold command that instantiates this whole shape into a fresh
folder; `BUILDING.md` in this directory is the from-zero guide.

What one agent directory carries:

- `run.py` -- a stdlib-only launcher: render the agent's config (secret
  slots, host LLM inheritance, mode catalogue), then exec
  `python -m raven acp --config <rendered>` on the installed raven.
- `config.json` -- the baseline profile.
- `modes/*.json` -- optional per-session overlays, surfaced as `acp.modes`
  for a client's mode picker (`research`, `raven-oncall`, and `code` ship one today).
  An agent that ships no `modes/` renders no `acp` block, and the raven it
  execs then falls back to its own three built-in tiers -- so
  `session/set_mode` answers with those rather than method-not-found. An
  overlay's `agents.defaults.reasoningEffort` is lifted onto the mode entry as
  the trunk's `reasoningEffort` knob; the rest of an overlay is the agent's
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
  Two manifest fields shape how the row is reached rather than what it runs:
  `"hidden": true` keeps the row off the roster the dispatching model reads
  (and off the WebUI's sub-agent page) while a task routed to it still runs
  there; `"routes": [{"to": "<name>"}, ...]` makes this
  row's backend a routing entry (`agent/subagent/backends/routing.py`): every
  caller -- `spawn`, a DAG node, a direct chat -- runs the row, and the entry
  picks the implementation on `run`, reading the task as the model wrote it
  (`authored_task`), never the rendering with file contents inlined. A reused
  instance handle stays where its transport bound it; otherwise the host's
  own model picks between the targets' roster
  lines and this row. A fronting row is only as ready as its targets: a route
  to a folder that is missing, unready or switched off disables the row with
  the reason on it, so a half-installed agent does not run the missing
  half's work on the wrong implementation. A route entry may also carry
  `"owes"` and `"note"`: what the work still owes when the entry keeps it on
  this row instead of sending it, and what to tell this row's own
  implementation when that happens, appended to the task under a `[host]`
  marker. Both are the row's words, because the gate that closes a route
  serves every row that declares one; a route that declares neither hands the
  task over exactly as it arrived. Prose past a sentence goes in
  `"noteFile": "<name>.md"` beside the manifest instead -- discovery reads that
  file into `note`, so a requirement long enough to be worth writing stays
  reviewable as a diff; declaring both is refused. What puts a route under that
  gate at all is its own declaration, and the two halves are independent:
  `"needs": ["image_generation", "image_search"]` names what the target's own
  pipeline cannot work without, and `"minTier": "max"` the lowest tier the
  route may open at. A route naming neither is dispatched exactly as routes
  were before the gate existed -- never probed, never tiered -- because
  `routes` is a general facility and a row routing for reasons of its own must
  not inherit conditions it never asked for. The names in `needs` come from a
  closed vocabulary (`ROUTE_REQUIREMENTS`), since the host is what answers
  them; one outside it is warned about and treated as met, so a manifest
  written for a later raven keeps its route on an older one. And the probe
  answers for the *target's* lane, reading the product folder's own `.env`
  first (`PPT_SERPER_API_KEY`, `PPT_IMAGE_API_KEY`) and falling back to the
  host sections the launcher would inherit -- the host's own credentials
  answer for the host loop, and a lane is free to be equipped differently.
  It follows the launcher one branch further than the explicit keys: with no
  image key anywhere and an OpenRouter endpoint, the key paying for the lane's
  words pays for its pictures, so a folder holding only `PPT_API_KEY` still
  counts as able to draw -- and towards any other gateway it does not.
  All four fields are manifest facts: a
  stored row takes them from the folder on every merge. `raven-ppt` ships hidden behind
  `raven-design`'s routes, so the model sees one design agent and decks still
  build on the deck engine, its own model and key; the two halves share one
  everos identity (`raven-design`), so what the user says over a deck is
  remembered for the next design turn. Its `PPT_API_KEY` is
  configured through `raven subagents setup` or the folder's `.env`, since the
  page no longer lists it.
- `plugins/<id>/` -- the agent's own harness as raven plugins: hooks on the
  loop's six phases, replacement tools under the built-in names, its own
  config slice under `plugins.config["<id>"]`. `run.py` names the directory
  through `plugins.dirs`; nothing in `raven/` knows the plugin exists.
  An agent's harness may instead ship as a standalone distribution:
  `raven-ppt` and `raven-design` carry no `plugins/` directory -- their
  engines are the `plugins-dist/ppt-engine` and `plugins-dist/design-engine`
  wheels, found through the `raven.plugins` entry-point group. Each engine
  under `plugins-dist/` belongs to its agent -- but an entry-point plugin
  with `enabled_by_default = true` ACTIVATES in every raven process in the
  environment; staying out of the host's and the sibling agents' turns is
  the engine's own duty, done by factories that decline when their config
  slice is absent (what the scaffold's engine templates do), or by the
  operator's `plugins.disabled` list.
  Prompt assets ride with the plugin whose conduct they teach, under
  `plugins/<id>/prompts/`: the workspace guide `run.py` seeds once
  (oncall's TOOLS.md section, code's whole-file TOOLS.md) lives there.
  `raven-code` also seeds its identity as `soul.md` (beside `run.py`, the
  raven-research shape) and a coding conduct as `agent.md` (one variant per
  model family, the fork's split; a partition still carrying the other
  variant or older managed seed is refreshed, an operator's edit never is),
  and its code-flow hook
  contributes the working directory's own instruction files (`AGENTS.md` /
  `CLAUDE.md` / `CONTEXT.md`, the slice's `projectFiles`) to each turn's
  system message through the context assembler, without rewriting the query.
  The same plugin carries one more surface:
  - `code-flow/code_flow/tools/` -- the product's own tool face, contributed
    by the same plugin: the fork's spelling (`file_path` / `old_string`, with
    the host's names accepted as aliases), `glob`, `todo`. Four of them
    ride the built-in names, which is how a product serves its own file tools
    without touching `raven/agent/tools/` or the four other products that
    share it: plugin tools register last and a same-name registration
    replaces the built-in instance, so the model is only ever shown this one.
    Disabling the built-in name instead is not the same thing -- withholding
    is by name and would take the replacement with it. The face is a section
    of the flow slice (`tools`), with its own switch and a
    `restrictToWorkspace` the launcher renders from the product's `tools`
    block: a plugin factory cannot read that field, and a replacement built
    without it is fenceless whatever the product asked for.
  The ask tier is decided per hosting, not product-wide: the ACP hosting
  keeps trunk's `ask` (raven dispatching a sub-agent answers those prompts
  itself, and a person in an editor should still be asked), and the one-turn
  CLI hosting renders `permissions.mode: full` because that hosting has no
  channel to ask on -- with the default it refused every write and the model
  reported the task incomplete.
- `soul.md` + the contract the plugin renders into `agent.md` -- optional:
  the agent's own identity, seeded into the workspace once, for an agent
  that replaces the host identity (research does; its `context.dropSegments`
  keeps the host's own identity segment out of the prompt). An agent whose
  vendored twin served the host-generic identity carries no `soul.md` and
  keeps the host identity segment -- oncall and design: each of those forks'
  SOUL.md is byte-identical to trunk's own template, its ACP path never
  seeded it into a workspace, so an added identity file would change the
  very prompt face the parity tests pin. `raven-code` is the exception with
  a reason: its one-turn CLI hosting runs `raven agent`, whose workspace
  sync writes trunk's template `soul.md` -- a personal assistant with a
  personality -- in front of the coding conduct, while its ACP hosting (no
  sync) read none; seeding its own `soul.md` first gives both hostings one
  identity and keeps the host identity segment. `raven-ppt` also carries no
  `soul.md`, for a different reason: its fork's SOUL.md is its own, and it
  rides byte-for-byte at the engine wheel's prompts home
  (`raven_ppt/prompts/`), seeded into the pinned home by the engine plugin's
  hook at first turn.

Agent vocabulary:

- **Rendered config** -- what `run.py` writes and execs against: the baseline
  `config.json` after secret slots, host LLM inheritance and the mode
  catalogue are applied. Rendered files live under the state root, never in
  the agent directory.
- **State root** -- where the agent keeps its work (repos, instance
  buckets, flow stores, rendered configs): `product_state_root()` in
  `raven/config/product_render.py`, default
  `<raven home>/workspace/subagent_sessions/<agent>`, overridden by the
  agent's `*_STATE_ROOT` variable.
- **ACP home** -- the engine's own Agent home, `product_acp_home()`: under
  the raven data directory, never inside the host's Agent home (the host
  hands its home out as a session working directory, and a raven engine
  refuses a working directory that contains its own home). The agent's
  `*_ACP_HOME` variable overrides it outright.
- **Tool-face pin** -- each launcher test pins the exact tool face the
  rendered config exposes (`VENDORED_TOOL_FACE` in
  `tests/test_agents_<agent>_launcher.py`) to its vendored twin's face.
- **Seed-once** -- workspace assets (guides, identity) are written only when
  absent (`seed_once()` in `raven/config/product_render.py`), so a user's
  later edits survive relaunches.

Agent notes:

- `raven-ppt` (the deck agent) needs the ten bundled templates the
  `ppt-engine` plugin offers, and gets them from the clone: they are tracked
  under `plugins-dist/ppt-engine/raven_ppt/assets/templates/`, which the
  editable install serves and the ppt-engine wheel bundles. Nothing to fetch
  and no token to hold -- the registry they used to be fetched from is
  private, and requiring it left an outside install with an empty catalogue
  and a wheel carrying zero templates. `plugins-dist/ppt-engine/templates.manifest.json`
  still pins every file by sha256; `make verify-templates` checks the tracked
  copies against those pins, `make fetch-templates` lands a fresh cut over
  them (maintainers, `GITLAB_TOKEN`), and release.yml refuses a wheel that
  does not carry all ten.

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
- Acceptance for a shipped agent is transport-face equivalence against its vendored
  twin: transcript shape, timeout semantics, everos records field by field.
