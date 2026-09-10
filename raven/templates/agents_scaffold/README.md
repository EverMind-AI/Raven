# My-Agent

A minimal Raven agent: a folder, not a fork. The host raven discovers
this folder, spawns `run.py` per connection, and `run.py` renders
`config.json` (secrets merged, LLM inherited from the host when this folder
holds no key) and execs the installed raven's own `raven acp`.

The full from-zero guide -- every file's job, custom tools, hooks, engine
wheels -- is `agents/BUILDING.md` in the raven repository. When the harness
outgrows the `plugins/` directory, `raven agents new <name> --engine-wheel`
scaffolds it as an installable wheel skeleton instead; the row then stays
disabled until `pip install -e <name>-engine` lands the wheel where raven
is installed.

## Ten minutes

### 1. Confirm it is discovered

Drop this folder under `<raven home>/agents/my-agent/` (the home tree is
discovery's first priority) and ask the roster:

```bash
uv run python -c "
from raven.agent.subagent.vendored_agents import discover_product_rows, product_state
print([ (r.name, r.enabled) for r in discover_product_rows() ])
print({ n: (s.ready, s.detail) for n, s in product_state().items() })"
```

Expect `('My-Agent', True)` and `ready=True`. A missing launcher or engine
wheel shows up here as a disabled row with the reason on it. Restart the
gateway to make a live raven re-read the tree.

### 2. First turn

Dispatch from the host (the roster row's description tells the model when to
pick this agent), or drive the server by hand:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}' \
  | python run.py --acp
```

A JSON reply with `"protocolVersion"` is the handshake; logs go to
`<state root>/logs/acp.log` (`RAVEN_ACP_LOG_LEVEL=DEBUG` shows the plugin
registration lines).

### 3. Add a second tool

1. Write a factory beside `hello.py` (four members: `name`, `description`,
   `parameters`, `async execute`); derive the tool's name from your agent
   the way `my_agent_hello` does -- tool names are one namespace across
   every activated plugin in the process.
2. Declare it in `raven-plugin.toml`:

   ```toml
   [[plugin.contributes.tools]]
   name = "my_second_tool"
   factory = "my_agent_flow.tools.second:make_second"
   ```

3. Restart. The name decides net-new versus replacement: a built-in's name
   (`web_search`, `ask_user`) replaces it, any other name adds a tool.

Config for your tools lives in `config.json` under
`plugins.config["my-agent-flow"]` -- your factory reads it as `ctx.config`.

### 4. The six hook phases

| Phase | Fires |
|---|---|
| `before_user_inbound` | fresh user message, before dispatch |
| `before_iteration` | before each LLM call |
| `before_execute_tools` | after the LLM proposes tool calls, before they run |
| `after_iteration` | after each iteration completes |
| `terminal_answerless` | the turn ended with no answer |
| `after_send` | after the outbound text is sent |

All default to pass-through; override only what you need and return a
`HookDecision` (short-circuit / rewrite / withhold tools / leave a note /
rollback).

### 5. Uninstall

Delete this folder. A discovered row disappears with it; if you ran
`install.py` (pinned row), also remove the pinned row -- with the interpreter
that serves raven:

```bash
python -c 'from raven.config.update_subagents import remove_third_party_subagent as rm; print(rm("My-Agent"))'
```

Restart the gateway.
