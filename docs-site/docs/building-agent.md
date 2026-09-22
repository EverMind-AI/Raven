# Building an Agent

Build a Raven agent when you need a separate identity, tool profile, and
execution policy on the shared runtime. Connecting an existing external product
instead is covered by [Agent Integrations](agent-integrations.md).

## Scaffold before customizing

Configure a host model provider first. Preview the files, then create an agent:

```bash
raven agents new example-agent --dry-run
raven agents new example-agent
```

The second command writes a new folder under `$RAVEN_HOME/agents/example-agent`.
It validates the manifests, compiles generated Python, scans discovery, and
by default launches an ACP initialize smoke check. The check can start the
runtime and write its state; it is not a full model task.

Names use lowercase kebab-case and cannot collide with shipped or discovered
agent identities. `--display` changes the display name. `--here` writes under
`./agents/`; `--no-smoke` skips launching; `--register` explicitly pins the row
in host config. The scaffold currently supports only `--kind acp` even though
the runtime can dispatch other backend kinds.

Do not re-run creation over an existing folder to upgrade it. Inspect the
generated README and edit that definition. A reported refusal for missing
provider credentials is different from a protocol or plugin-loading failure.

## Understand the generated files

| File | Responsibility |
| --- | --- |
| `subagent.json` | Dispatch identity, description, ownership, launcher command |
| `run.py` | Render config and launch the installed Raven ACP server |
| `config.json` | The agent's settings on top of the shared runtime |
| `.env.example` | Document secret slots and storage overrides |
| `install.py` | Optional pinning for a custom location or fixed roster row |
| `plugins/<id>/raven-plugin.toml` | Declare the agent's runtime contributions |
| Plugin Python package | Implement tool and hook factories |
| `README.md` | Instructions specific to the generated agent |

Discovery substitutes `{PYTHON}` and `{SUBAGENT_DIR}` in the manifest. Keep
those placeholders in the portable definition rather than hard-coding this
machine's checkout. Fill the generated description/ownership TODOs: the
dispatching model uses those fields to decide when to select this agent.

Do not change `name` or memory identity just to rename a UI label: existing
sessions and recorded calls refer to those identities.

## Configuration and state

The launcher renders a private config under the agent's state root. Keep the
publishable definition free of secrets; use the documented environment or
`.env` slots and keep private files out of git. Without a per-agent model key,
the launcher can inherit the host provider configuration. In own-key mode,
configure its provider/model endpoint too; a key alone is not a complete model
binding.

Keep three locations distinct:

- **Working directory:** task files supplied by the caller.
- **State root:** rendered configuration and product working state.
- **ACP home:** the child engine's identity, sessions, and skill pool.

Default storage is outside the definition folder. The child's ACP home must
not be swallowed by a working directory the host passes to it; use the
generated storage overrides only after checking that boundary.

Plugin-specific values belong under `plugins.config["<plugin-id>"]`. The
launcher injects the plugin roots; do not add a second discovery path by
guessing a relative directory. Optional ACP modes are declared profiles for
future turns, not a replacement for tool permission configuration.

## Case study: Raven-Code

Raven-Code specializes without a second core loop. Its launcher renders
config and discovers `agents/raven-code/plugins/code-flow/`. The plugin
contributes tools, a loop hook, and a session observer through the contracts
in [Building a Plugin](building-plugin.md).

| Responsibility | Seat inside the plugin's `code_flow/` package | Boundary |
| --- | --- | --- |
| Project instructions | `flow.py` iteration hook | Bound-workdir files and remaining prompt budget |
| Read-version tracking | `tools/read_state.py` and `tools/filesystem.py` | Session ledger, not a global read grant |
| Persistent checklist | `tools/todo.py` and flow/session lifecycle | Saved record is authoritative; bind the actual session |
| Concurrency notice | `sessions.py` and iteration hook | Shared-directory evidence, not exclusive ownership |
| Git report | `manifest.py` into `raven.harnessManifest` metadata | Unknown/shared facts cannot become “ready for integration” |
| Session cleanup | Manifest's session observer | Remove only the affected session's state |

The manifest deliberately replaces names such as `read_file` and `edit_file`.
Expose the actual replacement parameter schemas, not documentation for the
old tool face.

Two config gates are independent: `plugins.config["code-flow"].enabled` for
notices/reports, and `tools.enabled` inside that same slice for replacement
tools. Both default false in the plugin model; the Raven-Code launcher enables
its own render. Checklist/read-ledger binding remains necessary when notices
are disabled but tools remain enabled.

The flow appends repository instructions without replacing host identity or
rewriting the user's inbound text. The launcher's `CODE_PROJECT_FILES` selects
project files; `off` selects none. Trusted instruction loading must not become
arbitrary access outside the bound project.

For a new coding agent, begin with the scaffold and a small reviewed tool set.
Reuse a contract rather than copying the product. Test:

- edits before a read and after another writer changes the file;
- two sessions sharing a directory;
- checklist restoration after a new turn or context trimming;
- session deletion and runtime replacement;
- unavailable Git facts and non-Git directories;
- sandboxed execution, where the host-exec replacement must decline.

Relevant tests include `tests/test_agents_code_flow_read_state.py`,
`test_agents_code_flow_todo.py`, `test_agents_code_flow_project_files.py`,
`test_agents_code_flow_manifest.py`, and `test_agents_code_tools_plugin.py`.
The [usage guide](agent-integrations.md#raven-code-workspace-and-verification)
explains what operators can infer from these mechanisms.

## Add tools and behaviour

Start from the generated greeting tool and hook factory. A tool implements
`name`, `description`, `parameters`, and `async execute`. Declare it in the
plugin manifest and test it in isolation before exposing it to a model.

The generated hook demonstrates the loop timing contract. Current shipped
agents also use the narrower `AgentParticipant` contract for per-turn
judgements; choose the seam that fits the behaviour rather than copying a
whole agent loop. See [Building a Plugin](building-plugin.md).

Keep contributions scoped to this agent's config. Importing a plugin in the
host must not accidentally enable the agent's tools in unrelated sessions.
Do not put product-specific imports into the shared runtime to make discovery
work; Raven treats the definition directory as data and loads contributions
through the plugin seam.

## Package an engine when needed

For a separately released harness, preview the wheel-shaped scaffold:

```bash
raven agents new example-engine-agent --engine-wheel --dry-run
```

Without `--dry-run` it creates an agent definition and an engine project.
The engine manifest lives inside its Python package, with an entry in the
`raven.plugins` entry-point group. The agent declares the engine package and
distribution; readiness stays false until that package is importable by the
child interpreter.

Manage dependencies with `uv` in the intended project/environment. Resolve
Raven from your verified checkout or distribution; the unqualified `raven`
name on a public package index can refer to a different project. Do not copy a
bare package-install command from an older design note. See
[Development](development.md) for repository environment setup.

## Verify, discover, and operate

1. Validate the generated manifests and import every factory with the same
   interpreter that will launch the agent.
2. Check ACP initialize and the expected tool roster, not just process exit.
3. Run a bounded read-only task, then test the specific mutation, output, or
   session continuation the agent needs.
4. Test refusal paths: missing credentials/engine, permission denial,
   cancellation, and plugin activation failure.
5. Restart the resident host after changing a definition, then verify that it
   can select and dispatch the agent.

A home-tree definition is discovered without pinning. For a definition outside
that tree, use the scaffold's registration option or its generated installer
deliberately. A pinned row may continue pointing at an old directory after a
move; update the pin rather than assuming rediscovery fixes it.

Repository evidence lives in `agents/BUILDING.md`,
`raven/cli/agents_commands.py`, and the schema exports under `schemas/`.
Useful maintainer checks:

```bash
uv run pytest tests/test_cli_agents_commands.py tests/test_subagent_registry.py -q
```

These tests exercise scaffolding and registry behaviour; they do not replace
a real acceptance task for your new agent.
