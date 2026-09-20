# Architecture

All runtime entry points assemble Raven through the same **Assembly Root**:
`raven/core/runtime.py:build_runtime`. Configuration and plugin contributions
determine the components in each runtime Generation. The Spine schedules turns
for the Agent Loop and delivers the resulting events.

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
| **Evolver** | Drives harness self-evolution by diagnosing failures, testing candidate improvements, and retaining changes that outperform the baseline in reproducible evaluations. |
| **EverOS Memory** | Preserves user context, agent experience, and world knowledge across sessions, recalling relevant memories and reusable skills for future tasks. |
| **SkillForge** | Retrieves relevant skills from local libraries, EverOS memory, and SkillHub's catalog of **114,190 skills**, giving agents specialized expertise on demand. |
| **Proactivity** | Combines event monitoring and scheduled execution to anticipate user needs, deliver timely reminders, and initiate follow-up work. |

## Where the canonical definitions live

The repository maintains canonical definitions alongside the implementation:

- `CONTEXT-MAP.md` identifies the context file for each subsystem.
- `CONTEXT.md` defines runtime terms and assigns packages to layers (Layer Seats).
- `pyproject.toml` defines the import contracts that enforce Kernel
  boundaries.
