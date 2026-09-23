# Documentation reference sources

The public guides under `docs-site/docs/` describe Raven's current implementation,
not feature parity with another product. The following documentation was consulted
on 2026-09-22 to improve task-oriented navigation, setup examples, security
explanations, and troubleshooting. These are design references, not dependencies.

| Reference | Approach adopted | Raven-specific boundary |
| --- | --- | --- |
| [OpenClaw CLI docs](https://docs.openclaw.ai/cli/docs) | Make documentation discovery explicit; distinguish installed CLI help from hosted documentation search | Raven has site search and CLI help, not an `openclaw docs`-equivalent hosted search command |
| [OpenCode V2 permissions](https://opencode.ai/v2/docs/permissions/) | Explain decision order, examples, approval scope, and the difference between permission and isolation | Raven uses a tool-tier mapping and strictest matching specific exec rule, with `*` as fallback; it does not use V2's ordered action/resource/effect array |
| [Hermes Agent documentation](https://hermes-agent.nousresearch.com/docs/) | Organize skills, memory, MCP, messaging, operations, and developer extensions by user task | Raven's retrieval, backend memory processing, and separate Evolver are distinct mechanisms; Hermes's self-improvement claims are not Raven guarantees |

## Implementation sources

- ACP: `raven/acp/capabilities.py`, `raven/acp/methods.py`,
  `raven/acp/replay.py`, and `raven/acp_client/permissions.py`.
- A2A: `raven/a2a/card.py`, `raven/a2a/routes_aiohttp.py`,
  `raven/a2a/runtime.py`, `raven/a2a/executor.py`, and `raven/a2a_client/client.py`.
- DAG execution: `raven/agent/subagent/dag_graph.py`,
  `raven/agent/subagent/dag_tool.py`, `raven/agent/subagent/dag_runner.py`, and
  `raven/agent/subagent/dag_resume.py`.
- Permission decisions: `raven/permissions/gate.py`, `raven/permissions/rules.py`,
  and `raven/permissions/session.py`.
- Capability vocabulary: `CONTEXT-MAP.md`, `CONTEXT.md`, and `agents/BUILDING.md`.
- Agent setup and presets: `raven/agent/subagent/presets.py`,
  `raven/agent/subagent/acp_registry_presets.py`, and `raven/cli/agents_commands.py`.
- Messaging: `raven/channels/registry.py`, the adapter `spec.py` declarations,
  `raven/cli/channel_commands.py`, `raven/channels/intake.py`,
  `raven/auth/allowlist.py`, and `raven/gateway/outlet.py`.
- Plugin development: `raven/plugins/manifest.py`,
  `raven/plugins/context.py`, `raven/contracts/participant.py`, and
  `raven/contracts/subagent_backend.py`.
- Playbooks: `raven/cli/playbook_commands.py`, `raven/playbook/types.py`,
  `store.py`, `validate.py`, `executor.py`, `params.py`, `credentials.py`,
  and `runtime.py` in `raven/playbook/`. Creation is immediately usable;
  disabling affects model discovery, not explicit CLI runs. Stored parameter
  type declarations do not enforce runtime value types.
- Evolver: `evolver/README.md` (including its planned-retirement notice),
  `evolver/cli.py`, `evolver/launch/`,
  `benchmarks/appworld/evolve/entry.py`, `evolver/orchestrator/gates/pipeline.py`,
  and `evolver/orchestrator/sealed/runner.py`. The launcher registers AppWorld;
  precheck can call the subject endpoint; navigation promotion is distinct
  from statistical credit, and finalization is distinct from pausing.

## Navigation choices

The collaboration expansion adds task-oriented guides for sub-agent instances,
Oncall campaigns, shared-browser work, knowledge retrieval, and trajectory
debugging. Evolver's two existing URLs are grouped under usage/experiments and
design/implementation. Proactivity retains separate Guide and Developers entries.

Additional implementation checks:

- Direct chat and steering: `raven/rpc/methods/instances.py`,
  `raven/agent/subagent/direct_chat.py`, `manager.py` in the same directory,
  and `ui-web/src/features/subagents/source.ts`. The current WebUI instance
  composer sends ordinary turns; server steering is a separate RPC contract.
- Dynamic workers: `raven/agent/loop/wiring.py`,
  `raven/agent/subagent/delegate.py`, `charter.py`, `charter_code.py`,
  and `raven/playbook/agent_generator.py`.
- Oncall: `agents/raven-oncall/subagent.json`, its `oncall-flow` tools,
  `wakes.py`, and `raven/cli/ops_connection_commands.py`.
- Browser: `docs/browser-and-desktop.md`, `raven/browser/driver.py`,
  `policy.py`, and `ui-web/src/features/browser/`. URL checks are not a
  complete network-isolation boundary.
- Knowledge: `raven/knowledge/_manager.py`, `_embedding.py`,
  `raven/rpc/methods/knowledge.py`, and `raven/rpc/models.py`. The default
  parsers are text/structured text; the current WebUI source has backend
  fixtures and styling but no complete mounted knowledge-management feature.
- Memory: `raven/context_engine/factory.py`, `segments/curator.py`,
  `raven/memory_engine/store_pipeline.py`, and
  `plugins-dist/everos-memory/raven_everos/backend.py`. The current host has
  no runtime consumer of the retained `skillForge.extraction` config despite
  older prose describing a local pipeline; the site no longer promises that
  enabling that field starts one.
- Trajectories: `raven/trajectory/replay.py`, `regression.py`,
  `raven/cli/trajectory_commands.py` and `trajectory_regression_commands.py`.
- Raven-Code: `agents/raven-code/plugins/code-flow/raven-plugin.toml`,
  `code_flow/flow.py`, `manifest.py`, `sessions.py`, and `tools/`.

The three audience groups remain Guide, Reference, and Developers. Daily-use,
channel, and agent-setup pages belong in Guide; wire/runtime contracts belong
in Reference; authoring agents, plugins, and adapters belongs in Developers.
Existing page filenames are retained when moving their navigation entries.

The Hermes-style separation of usage, messaging, integrations, and extension
development is an information-architecture reference, not a renaming of Raven's
runtime layers. The architecture page maps surfaces to runtime, execution, and
state ownership using Raven's own vocabulary.

Capability declarations and implementation tests take precedence over dated
plans and stale CLI prose when describing support. In particular, ACP session
modes and per-session stdio MCP are implemented despite older help text; DAG
confirmation without an ask channel currently proceeds unconfirmed; A2A peer
entries are credential mappings, not a network denylist.
