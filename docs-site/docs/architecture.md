# Architecture

All runtime entry points assemble Raven through the same **Assembly Root**:
`raven/core/runtime.py:build_runtime`. Configuration and plugin contributions
determine the components in each runtime Generation. The Spine schedules turns
for the Agent Loop and delivers the resulting events.

## Surface, runtime, execution, and state

The table below follows requests from each interface to execution and storage.
These are responsibilities, not additional package layers.

| Surface | Runtime entry | Execution | State owner |
| --- | --- | --- | --- |
| WebUI / TUI | Shared RPC and Spine | Host Agent Loop | Host sessions and Agent home |
| Messaging platform | Adapter, Intake, Gateway / Spine | Host loop and its permitted delegations | Channel/chat conversation; adapter account state |
| Editor / local ACP host | ACP server and RPC stack | Raven served as an agent | Server's ACP sessions and configured home |
| Inbound A2A peer | Authenticated HTTP, one-shot turn adapter | Host Agent Loop | In-memory A2A task store plus runtime session records |
| Host delegation | Roster and sub-agent manager | Built-in, ACP, CLI, or HTTP backend | Host call history plus backend-owned session state |
| Outbound A2A call | Card discovery and A2A client | Independently operated peer | Remote task lifecycle |

Memory and SkillForge provide context; plugins add runtime capabilities.
DAGs coordinate agents in the local roster. They do not provide a durable,
distributed task queue.

For setup use [Using Raven](using-raven.md),
[Agent Integrations](agent-integrations.md), and
[Channels and Messaging](channels.md). Developers should begin with
[Building an Agent](building-agent.md) or
[Protocol and Backend Integration](protocol-backends.md).

## How a turn reaches the Agent Loop

Requests arrive through several interfaces. The Spine coordinates turn
scheduling across them, while the following components handle execution,
context, and delegation.

| Component | Input | Role |
| --- | --- | --- |
| **Entrances** | Requests from the WebUI, TUI, external ACP hosts, CLI, messaging channels, and proactive triggers | Route work through RPC, ACP, the Gateway, or directly to the Spine |
| **Spine** | Submitted turns | Schedule the Agent Loop and deliver events |
| **Agent Loop** | A scheduled turn | Coordinate Harness Modules, tools, and delegation |
| **Harness Modules** | Memory, Planning, Capability, and Action requests | Provide strategies for context assembly, planning, tool selection, and model responses |
| **Context Engine** | Context available to a turn | Assemble context using the Memory Engine and SkillForge |
| **Memory and skills** | Memory and skill requests | Retrieve from the EverOS plugin, local skill libraries, and SkillHub |
| **Delegation** | A subagent or playbook call | Dispatch work to built-in, ACP, CLI, or OpenAI-compatible backends |

The WebUI and the React/Ink TUI share one contract, `rpc-schema/openrpc.json`.
The ACP server connects external hosts to Raven's RPC stack; the ACP client
lets Raven invoke other agents. CLI tasks, messaging channels, and proactive
triggers also submit work through the Spine.

## Protocol faces and orchestration

| Face | Boundary | Lifecycle | Main use |
| --- | --- | --- | --- |
| ACP server | Local process and stdio | Session and turn | An editor or local host runs Raven |
| ACP client | Child agent process | Session and turn | Raven delegates to a local agent |
| A2A server | HTTP peer with bearer authentication | Task and event stream | A remote agent calls Raven |
| A2A client | Remote origin with optional configured credentials | Card, task, response | Raven calls an independent peer |
| RPC | Raven UI or control client | Connection and session | WebUI, TUI, control-plane operations |

These interfaces reach the Agent Loop but do not share authentication or wire
semantics. Inbound A2A currently uses a one-shot Agent Loop path directly,
rather than the Spine scheduler and delivery path used by interactive turns.
Its task state is not a durable scheduling queue.

`run_subagent_dag` defines task dependencies and coordinates local execution;
A2A handles communication with independently operated peers.
Read [Agent protocols](agent-protocols.md), [DAG orchestration](orchestration.md),
and [Permissions and security](permissions.md) before integrating these surfaces.

## What the boundaries guarantee

- **Modular execution.** The Agent Loop owns turn state, tool execution,
  persistence, and event ordering. Its four Harness Modules provide replaceable
  memory, planning, capability selection, and model-response behaviour; hooks and
  tools add domain-specific capabilities.
- **Agent and plugin composition.** Definitions in `agents/` combine the
  installed runtime with agent-specific configuration and plugins to expose
  agents over ACP. `plugins-dist/` contains the EverOS memory, visual design, and
  PowerPoint engines as separate distributions.
- **Kernel boundaries.** `spine/`, `contracts/`, `tracing/` and `home.py` form
  the standalone Kernel. Inner runtime packages do not import the CLI, RPC or ACP
  interfaces. Import contracts enforce these boundaries.
- **Harness self-evolution.** `evolver/` diagnoses runs and evaluates candidate
  harness changes against benchmarks. This separate tool uses Raven as a
  library. The runtime does not import Evolver or repository-level agent
  definitions.

See [Repo Layout](repo-layout.md) for the directories and packages associated
with each component.

## Core systems

These components support Raven's core capabilities:

| System | Capability |
| --- | --- |
| **Agent Orchestration** | Coordinates agents, manages task dependencies and parallel execution, and turns multi-step collaboration into reusable workflows. |
| **Evolver** | Diagnoses failures, evaluates candidate harness changes, and records promotion decisions and statistical evidence separately. |
| **EverOS Memory** | Preserves user context, agent experience, and world knowledge across sessions, recalling relevant memories and reusable skills for future tasks. |
| **SkillForge** | Retrieves relevant skills from configured local libraries, the memory backend, and SkillHub. Available content depends on those sources. |
| **Proactivity** | Uses Sentinel and scheduled triggers to decide when to send reminders or start follow-up work, subject to configuration and policy. |

Skills, memory, MCP, plugins, playbooks, and specialized agents have different
extension boundaries. Use [Skills and extensions](skills-and-extensions.md) to
choose one and understand what is implemented versus what requires another service.

## Where the canonical definitions live

The repository maintains canonical definitions alongside the implementation:

- `CONTEXT-MAP.md` identifies the context file for each subsystem.
- `CONTEXT.md` defines runtime terms and assigns packages to layers (Layer Seats).
- `pyproject.toml` defines the import contracts that enforce Kernel
  boundaries.
