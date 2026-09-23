# Building a Plugin

A Raven plugin contributes to the runtime through a declarative manifest and
Python factories. It is not a skill bundle, an MCP server, or a Codex plugin.
Plugin code executes inside Raven's process and is trusted code, not sandboxed
by the manifest.

## Choose a contribution

| Manifest group under `plugin.contributes` | Purpose |
| --- | --- |
| `tools` | Model-callable actions |
| `hooks` | Loop timing hooks or supported participant factories |
| `memory_backends` | Memory storage/recall implementation |
| `services` | Resident-host lifecycle work |
| `tool_gates` | Plugin-specific tool adjudication |
| `session_observers` | Session lifecycle observations |
| `onboard` | Setup steps for the plugin's memory backend |

Use the existing contract in `raven/contracts/` rather than importing CLI/RPC
internals. A Tool Gate is not the platform Permission Gate and does not replace
its authorization decisions.

## Start with a working template

The default agent scaffold includes a directory-shaped plugin:

```bash
raven agents new example-agent --dry-run
```

Create it as described in [Building an Agent](building-agent.md), then customize
the generated tool/hook. Its layout is:

```text
plugins/example-agent-flow/
  raven-plugin.toml
  example_agent_flow/
    __init__.py
    plugin.py
    tools/
      __init__.py
      hello.py
```

A manifest for that layout:

```toml
[plugin]
id = "example-agent-flow"
version = "0.1.0"
display_name = "Example Agent Flow"
enabled_by_default = true

[[plugin.contributes.tools]]
name = "example_agent_hello"
factory = "example_agent_flow.tools.hello:make_hello"

[[plugin.contributes.hooks]]
name = "example_agent_flow"
factory = "example_agent_flow.plugin:make_hook"
```

Directory discovery reads manifests without importing plugin code. Entry-point
discovery uses `importlib.resources.files()` to locate the packaged manifest,
which imports the package's `__init__.py`. Keep that module free of startup work.
Activation resolves the `module.path:callable` factories; a valid manifest alone
does not establish that a factory can load or its tool will register.

## Factories and configuration

A factory receives `PluginContext` from `raven.plugins.context`:

- `config` is only this plugin's admitted configuration slice.
- `services` exposes the declared host service surface.
- `logger` is bound to the plugin identity.

Tool factories return a `Tool` or `None` to decline registration. A tool
provides a stable name, description, JSON-schema parameters, and asynchronous
execution returning text or `ToolResult`. Reuse the generated greeting example;
do not invent a different signature for a factory.

The config slice belongs under `plugins.config["example-agent-flow"]`.
Declared config schema fields are checked at admission. An empty schema is
pass-through, not automatic validation of arbitrary values; validate your own
settings before using them. Unknown configuration keys may warn and pass through.

Prefix new tool names with the plugin/agent identity. A plugin tool using a
built-in name deliberately replaces that built-in; two plugins contributing
the same tool name conflict: the plugin activated first keeps the name and the
other is skipped. A plugin that fails to activate -- a factory that will not
import, or a name conflict -- is skipped alone: the host stays up, every other
plugin loads, the host prints a notice naming the plugin and its cause, and
`raven plugins` lists it as `failed`. So check the effective tool roster as well
as boot success. Adding its id to `plugins.disabled` stops the host loading it,
and the host's product sub-agents inherit that opt-out (a product never
inherits the opt-out of its own engine plugin).

## Hooks and per-turn participants

The `AgentHook` timing phases are `before_user_inbound`, `before_iteration`,
`before_execute_tools`, `after_iteration`, `terminal_answerless`, and `after_send`.
The generated example overrides only `after_send`; other phases pass through.
Tool withholding belongs to `before_iteration`, not an arbitrary phase.

For agent-specific judgements, the current `AgentParticipant` paper offers
read-only `StepView` inputs and optional verbs such as `intake`, `select_tools`,
`advise`, `system_addendum`, `review`, `salvage`, `outbound`, and `archive`.
The host owns timing and adapts supported participant factories through the
hook contribution path. A participant is constructed per turn, so turn-local
state does not leak into another concurrent conversation.

Read `raven/contracts/participant.py` and `raven/contracts/loop_hooks.py`
before choosing. Preserve the contract's version/tier boundaries rather than
writing to the loop's transcript or reaching into its scheduler.

## Lifecycle and packaging

Services implement the lifecycle in `raven/contracts/services.py`. Assembly
constructs contributions; the resident host starts and stops them. Do not start
background work as an import side effect or mutate the assembled runtime from
a service. Test shutdown and failure paths, not only startup.

Directory discovery scans `<root>/<plugin-id>/raven-plugin.toml`, including
roots supplied by `plugins.dirs`. Duplicate plugin ids resolve by the current
priority, from highest to lowest: bundled, user, project/extra directories,
then entry points.
Do not use a duplicate id as a version-selection mechanism without checking
which copy actually won.

For a separately installed engine, use the wheel-shaped scaffold. Its entry
point targets a package, and its manifest must be packaged inside that package:

```toml
[project.entry-points."raven.plugins"]
example-agent-engine = "example_agent_engine"
```

Installing an enabled-by-default engine can activate discovery in every Raven
process in that environment. Keep the engine template's explicit configuration
gate so tools only register for the intended agent. Use `uv` for dependencies,
and keep runtime state and secrets outside the distributable package.

## Verification checklist

1. Parse the manifest through `PluginManifest.from_toml_path`.
2. Import factories in the actual runtime environment and exercise enabled,
   disabled, and invalid-config cases.
3. Test tool arguments/results and any denial or rollback path without a live model.
4. Verify the effective roster and one harmless real task through the hosting
   surface that will use the plugin.
5. Test two concurrent conversations, service shutdown, and a missing dependency.

Maintainer regression checks:

```bash
uv run pytest tests/test_plugin_manifest.py tests/test_plugin_registry.py tests/test_plugin_tools.py tests/test_plugin_hooks.py -q
```

Schemas are exported under `schemas/`. Developer workflow and repository gates
are in [Development](development.md); permissions are documented separately in
[Permissions and Security](permissions.md).
