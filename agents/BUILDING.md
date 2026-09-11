# Building an agent

The from-zero guide to a new Raven agent: one command scaffolds a folder, the
folder is discovered, and its server answers an ACP handshake. **The bar this
guide holds itself to: a developer who has never read the source reaches a
green handshake in ten minutes.** If any step below forces you into the
source, that is a defect in this guide or in the scaffold -- report it as one.

## 1. The mental model

Raven is a harness of harnesses, two layers deep:

- The **host raven** is the first harness: it owns the turn loop, the
  permission gate, the built-in tools, memory, and the roster of agents it
  can dispatch to.
- **Your agent** is a second harness riding that loop: a folder with a
  launcher that renders a config and execs the installed raven's own
  `raven acp`. Your identity, your tools, and your hooks enter through the
  plugin seam; the core never imports your code and never learns your name.

Three words carry precise, different meanings here:

| Word | Means | Where it lives |
|---|---|---|
| **agent** | the thing itself: a folder the roster discovers, spawns, and dispatches to | `agents/<name>/` or `$RAVEN_HOME/agents/<name>/` |
| **harness** | the mechanism an agent steers the loop with: plugins contributing tools and hooks | `plugins/<id>/` inside the agent, or an engine wheel |
| **subagent** | the protocol seat: the roster row the dispatching model picks, spoken over ACP | `subagent.json`, validated by `raven/config/schema.py` |

## 2. Ten minutes to a green handshake

```bash
raven agents new my-agent
```

That is the whole quick start. Five things happen, and the command prints a
four-segment card as they do:

1. **`1/4 wrote`** -- eleven files land under `$RAVEN_HOME/agents/my-agent/`
   (assembled in a staging directory and renamed as a whole, so a failure
   never leaves a half-written agent). `--here` lands in `./agents/<name>`
   instead; `--dry-run` prints the tree and writes nothing.
2. **`2/4 doctor`** -- the folder examines itself: `subagent.json` through
   the roster schema, `raven-plugin.toml` through the manifest parser, every
   `.py` compiled, and a real discovery scan reporting the row and its
   readiness.
3. **`3/4 smoke`** -- the discovered command is spawned and sent one ACP
   `initialize` frame. Three readable outcomes:
   - **GREEN**: a legal reply came back; the chain works end to end.
   - **refused**: no LLM key anywhere, so the launcher exited loudly before
     serving. This is the fail-closed design working, not a broken chain --
     provide a key (see `.env.example` in the folder) or configure a provider
     in the host raven, and the same command goes green.
   - **FAILED**: anything else, with the stderr tail; the folder stays on
     disk for inspection. `--no-smoke` skips this segment.
4. **`4/4 next`** -- restart the gateway so a live raven re-reads the tree;
   fill in the two TODO prompts in `subagent.json`; optionally pin the row.

There is no registration step: a folder under `$RAVEN_HOME/agents/` is
discovered on every scan (the home tree is discovery's first priority, and it
survives upgrades). Dispatch happens when the host's model reads your row's
`description` and picks it -- which is why those TODOs matter.

A note on names: one kebab name derives every identity. `my-agent` is the
folder and machine id (never renamed), `My-Agent` the display name
(`--display` overrides), `my_agent` the python package, and `MY_AGENT` the
env-var prefix (the `raven-` prefix drops for shipped agents: `raven-code`
answers to `CODE_API_KEY`).

## 3. The eleven files

Each file, its one job, and the mistake most often made in it:

| File | Job | Common mistake |
|---|---|---|
| `subagent.json` | The roster row: the only file written for someone else (the dispatching model). `name` is the dispatch identity; `description` and `owns` are prompts -- the model decides from them alone when to pick you; `everos` ids are the machine identity (never change them after first use); `command` keeps `{PYTHON}` / `{SUBAGENT_DIR}` verbatim -- discovery resolves them in memory | Leaving the TODO prompts in place: the agent then never gets picked, and nothing errors |
| `run.py` | The launcher: renders the config (secret slots, host LLM inheritance, state root, plugin roots) and execs `python -m raven acp` -- it is not in the room during the session | Writing state into the agent folder; everything the agent persists belongs under the state root |
| `config.json` | The birth certificate: the diff between your agent and stock raven, in the host's own config schema. Ships as the minimal plugin slice | Adding `plugins.dirs` here -- the launcher injects it absolute at render time, and overwrites whatever this file says |
| `install.py` | The optional pinning ceremony: registers the row through `raven.config.update_subagents`. Needed only for a folder outside the discovery tree, an edited row, or a deliberately pinned one | Running it and expecting a live raven to notice -- the roster is read at startup; restart the gateway |
| `.env.example` | The secret slots, documented: the API key (own-key mode), the state root override, the ACP home override. Copy to `.env`, which stays in the folder and out of every wheel | Setting the key but no `providers` block in `config.json`: own-key mode renders a key with no provider/model to route on -- handshake green, first real turn dead |
| `README.md` | The folder's own ten-minute card for whoever finds it later | -- |
| `plugins/<id>/raven-plugin.toml` | The treaty: declarative manifest of what your plugin contributes (tools, hooks, and four more kinds). The host validates it without importing you. Empty `config_schema` means your slice passes through verbatim | A factory path that does not resolve -- activation logs a warning and skips the plugin; boot survives, your tool is silently absent |
| `.../plugin.py` | The hook factory: an `AgentHook` with all six phases inherited as pass-through, one overridden as the worked example | Returning a decision that does nothing because the wrong phase was overridden -- check the table in section 5 |
| `.../tools/hello.py` | The tool factory: the minimal `Tool`, four members (`my_agent_hello` -- the name derives from the agent). `description` is a prompt too | A bare name (`hello`): tool names are one namespace across every activated plugin in the process, and a collision takes the whole registry down |
| `.../my_agent_flow/__init__.py` | Package marker | Deleting it: the factory path stops resolving and the plugin silently skips |
| `.../tools/__init__.py` | Package marker for the tools subpackage | Same |

The scaffold's originals live under `raven/templates/agents_scaffold/` -- real
linted code, not string templates -- and every generated `.py` is what the
doctor just compiled.

## 4. Your own tools, in four steps

1. **Write a factory** beside `tools/hello.py`. A tool is four members --
   `name`, `description`, `parameters` (a JSON-schema dict), and
   `async execute(**kwargs) -> str | ToolResult` (`raven/contracts/tool.py`;
   optional knobs: `timeout_seconds`, `blocking_interaction`, `channels`).
   The factory signature is `Callable[[PluginContext], Tool | None]` --
   return `None` to decline registration for this run.
2. **Declare it** in `raven-plugin.toml`:

   ```toml
   [[plugin.contributes.tools]]
   name = "my_second_tool"
   factory = "my_agent_flow.tools.second:make_second"
   ```

3. **Restart.** The boot log (`RAVEN_ACP_LOG_LEVEL=DEBUG`, under
   `<state root>/logs/`) shows `registered tool <name> from <plugin id>`.
4. **Configure it.** Your factory reads `ctx.config` -- the
   `plugins.config["<id>"]` slice from `config.json`, passed verbatim.

**The name decides net-new versus replacement.** A name no built-in uses adds
a tool to the menu; a built-in's name (`web_search`, `ask_user`, ...) replaces
that built-in for your agent's turns. Both are legitimate; only one of them
is what you meant. Derive new names from your agent (`my_agent_hello`, not
`hello`): tool names are one namespace across every activated plugin in a
process, and two plugins contributing one name is a conflict that takes the
whole plugin registry down -- every plugin, not just the colliding pair.

## 5. The six hook phases

Your `plugin.py` factory returns an `AgentHook`
(`raven/contracts/loop_hooks.py`). All six phases default to pass-through;
override only what you need.

| Phase | Fires | Typical use |
|---|---|---|
| `before_user_inbound` | fresh user message, before dispatch | rewrite or veto input |
| `before_iteration` | before each LLM call | steer the next call; the one phase that can withhold tools |
| `before_execute_tools` | after the model proposes tool calls, before they run | audit or veto the batch (short-circuit, rollback, note) |
| `after_iteration` | after each iteration completes | observe, budget, note |
| `terminal_answerless` | the turn ended with no answer | salvage or report |
| `after_send` | after the outbound text is sent | file a report, update state |

Every phase returns a `HookDecision`. Its vocabulary, by field:

- **pass through** (`pass_through=True`, the default) -- decide nothing;
- **short-circuit** (`short_circuit_result`) -- end the phase with this
  result instead of continuing;
- **rewrite** (`modified_content`) -- replace the inbound text
  (`before_user_inbound`, chained hook to hook);
- **withhold tools** (`modified_tools`) -- hand the iteration a reduced (or
  reordered) tool list; honored in `before_iteration` only -- returned from
  any other phase it is silently ignored;
- **leave a note** (`append_note` / `notes`) -- attach a harness note the
  model sees on the last message;
- **roll back** (`rollback`, with `rollback_overrides` /
  `rollback_inject`) -- reject the iteration and rerun it amended.

## 6. Configuration: two layers

- **The agent's own config** is `config.json` -- the same schema the host
  reads (`raven/config/schema.py`), holding only your diff from stock raven.
  Any block works here: providers, tools, memory, context. The launcher
  renders it (secrets merged, LLM inherited when you hold no key, state root
  and plugin roots pinned) and the rendered copy lives under the state root,
  never in the folder.
- **Your plugin's slice** is `plugins.config["<id>"]` inside that file --
  yours alone, delivered verbatim as `ctx.config` to every factory. This is
  where your tools' and hooks' own knobs live; the host never interprets it.

Secrets never sit in `config.json` (it is publishable): they ride `.env` or
the environment, merged in at render time. With no key of your own, the
launcher inherits the host's whole provider block; the environment riders
`RAVEN_PARENT_MODEL` / `RAVEN_PARENT_REASONING_EFFORT` act only on that
inherit branch. No key anywhere is a loud pre-exec refusal, not a dead
session.

**Editor validation** (one step earlier than any doctor): the repo exports
JSON Schemas for both manifests, generated from the same pydantic models the
loader uses (`scripts/export_agent_schemas.py`, files under `schemas/`).
VS Code, `settings.json`:

```jsonc
"json.schemas": [
  { "fileMatch": ["**/agents/*/subagent.json", "**/subagent.json"], "url": "./schemas/subagent.schema.json" }
]
```

and for `raven-plugin.toml` (Even Better TOML):

```jsonc
"evenBetterToml.schema.associations": {
  "\\.*raven-plugin\\.toml$": "./schemas/raven-plugin.schema.json"
}
```

Outside a checkout, point `url` at the raw repository URL of the same files.

## 7. When to grow an engine wheel

An agent whose harness outgrows a plugin folder can ship it as a standalone
distribution instead -- its own installable wheel on the `raven.plugins`
entry-point group, the shape `plugins-dist/ppt-engine` and
`plugins-dist/design-engine` already have. Four questions decide; promote on
any yes:

1. Heavy dependencies the host should not carry?
2. Large assets (anything near the repo's 1 MiB file limit)?
3. A release cadence of its own?
4. A need for its own signing or exemption area?

All four no: stay in `plugins/` -- the default, private and zero-ceremony.

The promoted shape: a package with
`[project.entry-points."raven.plugins"] <id> = "<package>"` in its
pyproject, the `raven-plugin.toml` living **inside the package** (it is read
via `importlib.resources`), and the agent's `subagent.json` declaring
`"engine": {"package": "<import name>", "wheel": "<distribution name>"}`.
Discovery then lists the agent but disables it until the wheel is importable
where raven runs, naming the missing wheel on the row. Know what installing
one means: an entry-point plugin with `enabled_by_default = true` activates
in EVERY raven process in the environment -- the host's and every sibling
agent's turns included. Staying scoped to your own agent is the engine's own
duty: the scaffolded factories decline (`return None`) unless their config
slice says `enabled: true`, and only your agent's rendered config carries
that slice. Keep that gate when you replace the examples, or your tools ride
everyone's turns; the operator's other lever is `plugins.disabled`.

`raven agents new <name> --engine-wheel` scaffolds exactly this shape: the
agent folder with no `plugins/` directory, plus a `<name>-engine/` project
(in your working directory; under `plugins-dist/` with `--here`) whose
manifest already lives inside the package. The doctor then reports the row
disabled-as-designed until `pip install -e <name>-engine` lands the wheel
where raven runs.

## 8. Register, uninstall, move, troubleshoot

**Register** -- usually never: the home tree is discovered as-is. Pin a row
(`--register` at scaffold time, or `python install.py` later) only for a
folder outside the discovery tree, a row you edited, or a row you want fixed
against manifest changes.

**Uninstall** -- delete the folder; a discovered row disappears with it. If
you pinned a row, also remove it (the charter `agents/README.md` carries the
one-liner) and restart the gateway.

**Move** -- move the folder; `{PYTHON}` and `{SUBAGENT_DIR}` resolve against
wherever it sits now. If the row was pinned, re-run `install.py` from the new
location: that re-resolution is the whole migration story.

**Troubleshoot**:

| Symptom | Meaning | Fix |
|---|---|---|
| row absent from the roster | manifest unreadable, or the folder is outside every discovery root | `raven agents new --dry-run` shows the expected tree; check `subagent.json` parses; remember the home tree shadows the checkout tree |
| row listed but disabled, reason `launcher` | a file the command names is not on disk | restore `run.py`, or fix the command in `subagent.json` |
| row listed but disabled, reason `engine` | the declared engine wheel is not importable where raven runs | install the wheel into raven's environment, restart |
| launcher exits 1 before serving, one `error:` line | no LLM key anywhere (fail-closed) | put the key in `.env`, export it, or configure a host provider |
| handshake green, first turn dies | own-key mode with no `providers` / `agents.defaults` block | add the provider block to `config.json`, or drop the key and inherit |
| boot warning `memory.backend='everos' requested but no plugin contributes it` | the everos distribution is not installed | expected quiet degrade; install `plugins-dist/everos-memory` to turn memory on |
| your tool never appears | factory path typo (activation skipped it), or a relative `plugins.dirs` resolved against the wrong cwd | check the boot log for the skip warning; the launcher always writes absolute plugin roots |
| pydantic `extra_forbidden` on boot | a misspelled key under `plugins` in `config.json` | the error names the key; fix the spelling |
| edits to a pinned row have no effect | a live raven holds the roster it read at startup | restart the gateway |
| every dispatch dies with a working-directory-contains-home refusal | an explicit `agents.defaults.workspace` in the agent's `config.json` points inside the host Agent home -- explicit values are honored verbatim; the placement guard runs only for the default | remove the explicit workspace (the default lands outside), or point it somewhere outside the host Agent home |
| a raw ACP client's turn stalls right after a tool call | the agent's permission gate is waiting for `session/request_permission` to be answered (the host UI answers it for you) | reply with `{"outcome": {"outcome": "selected", "optionId": <an allow option>}}` -- measured on a live scaffolded turn |
