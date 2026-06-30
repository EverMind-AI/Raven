<div align="center" id="readme-top">

![Raven banner](https://github.com/user-attachments/assets/224c1623-2705-4a48-8a60-fd5681ca0cb2)

<p align="center">
  <a href="https://x.com/evermind"><img src="https://img.shields.io/badge/EverMind-000000?labelColor=gray&style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://huggingface.co/EverMind-AI"><img src="https://img.shields.io/badge/🤗_HuggingFace-EverMind-F5C842?labelColor=gray&style=for-the-badge" alt="HuggingFace"></a>
  <a href="https://discord.gg/gYep5nQRZJ"><img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdiscord.com%2Fapi%2Fv10%2Finvites%2FgYep5nQRZJ%3Fwith_counts%3Dtrue&query=%24.approximate_presence_count&suffix=%20online&label=Discord&color=404EED&labelColor=gray&style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/EverMind-AI/EverOS/discussions/67"><img src="https://img.shields.io/badge/WeCom-EverMind_社区-07C160?labelColor=gray&style=for-the-badge&logo=wechat&logoColor=white" alt="WeChat"></a>
</p>

[Website](https://raven.evermind.ai) · [中文](README.zh-CN.md)

</div>

<br>

<details>
  <summary><kbd>Table of Contents</kbd></summary>

<br>

- [Why Raven](#why-raven)
- [Quick Install](#quick-install)
- [Getting Started](#getting-started)
- [What Raven Is Built For](#what-raven-is-built-for)
- [Architecture](#architecture)
- [Developer Workflow](#developer-workflow)
- [Status](#status)
- [Star Us](#star-us)
- [EverMind Ecosystem](#evermind-ecosystem)
- [Contributing](#contributing)

<br>

</details>

## Why Raven

Raven is a native command line agent, not a chat box wrapped around a shell.
Built by EverMind, Raven is the first memory-first, self-improving agent OS:
its model, code, modules, and policy are decoupled so it can improve across the
whole stack, not just the skills layer. Layered memory and 100,000 built-in
skills let Raven serve personal agents, cloud fleets, and reusable digital
workers that get better with every session. It is built for users who already
think in terminals, repos, logs, scripts, sessions, and long-running workflows.
The goal is simple: give your terminal an agent that can remember, act, use
tools, manage context, and improve its own procedural skills over time.

Most agent CLIs stop at "LLM + tools + loop." That works for demos, but it
breaks down when the agent becomes part of your daily environment:

- Long sessions overflow context and lose important details.
- Every turn re-sends the same system prompt, skills, and tool definitions.
- The agent waits passively even when it can see something that needs action.
- Useful workflows stay trapped in chat history instead of becoming reusable
  skills.

Raven treats those problems as the product, not edge cases.

<table>
<tr>
<th width="28%">Capability</th>
<th width="36%">Raven</th>
<th width="36%">Typical agent CLI</th>
</tr>
<tr>
<td><strong>Native terminal product</strong></td>
<td>Interactive TUI, CLI, gateway mode, and typed RPC between Python and React/Ink</td>
<td>Usually a thin command wrapper around a chat loop</td>
</tr>
<tr>
<td><strong>Long memory</strong></td>
<td>EverOS-backed memory, local skills, session history, and workspace templates</td>
<td>Usually transient context or provider-side chat history</td>
</tr>
<tr>
<td><strong>Context control</strong></td>
<td>Curator and legacy context engines with explicit token budgets and fail-safes</td>
<td>Usually truncation, summarization, or hidden prompt heuristics</td>
</tr>
<tr>
<td><strong>Proactivity</strong></td>
<td>Sentinel, scheduler, nudge policy, and deferred decision flow</td>
<td>Usually waits until the user types again</td>
</tr>
<tr>
<td><strong>Skill evolution</strong></td>
<td>Detects reusable procedures, materializes skills, tracks feedback, and evolves them</td>
<td>Usually static markdown prompts or manually installed plugins</td>
</tr>
</table>

<br>

## Quick Install

```bash
curl -fsSL http://raven.evermind.ai/install.sh | bash
```

After installation, reload your shell and run the setup wizard:

```bash
source ~/.bashrc    # or: source ~/.zshrc
raven onboard
```

Raven supports OpenRouter, OpenAI, Anthropic, Gemini, DeepSeek, GitHub Copilot,
OpenAI Codex OAuth, and custom OpenAI-compatible endpoints.

## Getting Started

```bash
raven                  # Native TUI - start a conversation
raven tui              # Explicitly launch the native TUI
raven tui --check      # Verify the TUI runtime before launching
raven onboard          # Configure provider, sandbox, channels, and memory
raven agent -m "..."   # Run a one-shot task from the shell
raven provider list    # Review LLM providers and model configuration
raven channels list    # List available messaging channels
raven gateway          # Start the messaging gateway
raven sessions list    # List, resume, fork, export, or delete sessions
raven cron list        # Inspect scheduled jobs and automations
raven skill list       # Browse SkillForge skills
raven sentinel status  # Inspect proactive memory and nudge state
raven plugins          # List installed plugins and the active memory backend
raven sandbox list     # Inspect sandbox VMs when sandbox debugging is enabled
raven status           # Show local config and runtime status
raven doctor           # Diagnose config, routing, and LLM readiness
```

For source-based development, use the [Developer Workflow](#developer-workflow).

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## What Raven Is Built For

Raven is designed for the workflows where ordinary chat agents feel too small.

### 1. Terminal-Native Daily Work

Raven can run as a native TUI, a direct CLI agent, or a gateway-backed agent.
The TUI is not a web shell: it is a React/Ink application talking to Raven's
Python runtime through a typed RPC protocol.

### 2. Memory That Becomes Useful

Raven connects to EverOS for long-term user and agent memory. Sessions,
procedures, and reusable patterns can be turned into local skill material
instead of disappearing into old transcripts.

### 3. Context That Does Not Collapse Under Pressure

The context stack has a legacy path and a Curator path. Under pressure, Raven
can archive, retrieve, and assemble context with explicit budgets instead of
blindly clipping the oldest messages.

### 4. Agents That Can Reach Out First

Sentinel watches events, schedules checks, evaluates whether a nudge is useful,
and routes proactive actions through guardrails. The point is not noisy
notifications; the point is an agent that can notice.

### 5. Skills That Improve

SkillForge treats skills as procedural memory. It can detect reusable workflows,
write skill files, track execution feedback, and evolve instructions when they
stop working.

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Architecture

Every turn flows through the Spine: one entry (`submit`), one exit (`emit`),
and per-conversation lanes for ordering and cancellation. Feature engines plug
into the agent loop through explicit handoffs instead of importing each other.

```text
Channels / TUI / Gateway
        |
        v
   Raven Spine
 submit -> lanes -> emit
        |
        v
   Agent Loop
 tools · skills · providers
        |
        +--> Context Engine   legacy / curator
        +--> Memory Engine    EverOS / local skills / SkillForge
        +--> Proactive Engine Sentinel / scheduler / nudge policy
        +--> TokenWise        usage tracking / cache placement / routing
        +--> Eval Engine      task judgement and coordination
```

### Repo Layout

```text
raven/
├── spine/              # Per-turn backbone: submit -> lanes -> emit
├── agent/              # Agent loop, tools, hooks, subagents, context builder
├── channels/           # Telegram, Discord, Slack, Matrix, WhatsApp, WeCom, ...
├── tui_rpc/            # Python side of the native TUI protocol
├── providers/          # LLM provider adapters
├── context_engine/     # Context assembly and Curator path
├── proactive_engine/   # Sentinel, scheduler, nudges, feedback
├── memory_engine/      # EverOS memory, local skills, SkillForge
├── token_wise/         # Usage tracking, cache placement, routing
├── sandbox/            # Isolated command execution
├── security/           # Trust boundaries and network checks
├── cli/                # `raven` command line entry point
└── config/             # Config schema and update helpers

ui-tui/                 # React/Ink native terminal UI
bridge/                 # WhatsApp TypeScript bridge
```

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Developer Workflow

Install everything and set up hooks:

```bash
make install
```

Run the local CI gate:

```bash
make ci
```

Focused commands:

```bash
make lint-python
make lint-tui
make lint-bridge
make test-python
make test-tui
```

The repository uses:

- `uv` for Python dependency management;
- `ruff` and `pre-commit` for Python and repository hygiene;
- `commitlint` plus a Python checker for Conventional Commit subjects and
  ASCII-only public history;
- `eslint`, `tsc`, `vitest`, and RPC drift checks for the TUI;
- `npm ci`, `tsc`, and `npm audit --audit-level=critical` for the bridge.

`CLAUDE.md` contains the full collaboration rules for branch naming, commit
format, dependency updates, testing, and PR hygiene.

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Status

Raven is pre-alpha and moving quickly. APIs can change without notice, but the
core product surfaces are already in the repository.

| Layer | Status |
| --- | --- |
| Native TUI + CLI | Functional |
| Spine runtime | Functional |
| Base agent loop, tools, providers | Functional |
| Context engine | Implemented, still evolving |
| Sentinel proactivity | Implemented, still evolving |
| TokenWise strategies | Implemented |
| SkillForge | Implemented |
| Eval engine | Partial |

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Star Us

If Raven is the kind of command line agent you want to exist, star the repo.
It helps more terminal-native builders discover the project and gives the
EverMind ecosystem a stronger signal to keep investing in open agents.

### Star History

[![Star History Chart](https://api.star-history.com/svg?repos=EverMind-AI/raven&type=Date)](https://www.star-history.com/#EverMind-AI/raven&Date)

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## EverMind Ecosystem

EverMind is an open-source ecosystem for long-term memory, self-evolving
agents, AI-native interfaces, and memory evaluation.

<table>
<tr>
<th colspan="2">EverMind Open-Source Ecosystem</th>
</tr>
<tr>
<td><strong>Memory Runtime</strong></td>
<td><a href="https://github.com/EverMind-AI/EverOS">EverOS</a> - the local memory operating system and research-backed runtime for agent and user memory.</td>
</tr>
<tr>
<td><strong>AI-Native CLI Agent</strong></td>
<td><a href="https://github.com/EverMind-AI/raven">Raven</a> - the native command line agent that brings memory, proactivity, context control, and skill evolution into the terminal.</td>
</tr>
<tr>
<td><strong>Algorithm Engine</strong></td>
<td><a href="https://github.com/EverMind-AI/EverAlgo">EverAlgo</a> - stateless extraction, ranking, parsing, and memory operators that power EverOS.</td>
</tr>
<tr>
<td><strong>Hypergraph Memory</strong></td>
<td><a href="https://github.com/EverMind-AI/HyperMem">HyperMem</a> - hypergraph memory for long-term conversations, with benchmark-backed topic -> episode -> fact retrieval.</td>
</tr>
<tr>
<td><strong>Benchmarks</strong></td>
<td><a href="https://github.com/EverMind-AI/EverMemBench">EverMemBench</a> · <a href="https://github.com/EverMind-AI/EvoAgentBench">EvoAgentBench</a> - evaluation suites for conversational memory and agent self-evolution.</td>
</tr>
<tr>
<td><strong>Long-Context Research</strong></td>
<td><a href="https://github.com/EverMind-AI/MSA">MSA</a> - Memory Sparse Attention for scalable latent memory and 100M-token contexts.</td>
</tr>
<tr>
<td><strong>Personal Memory Layer</strong></td>
<td><a href="https://github.com/EverMind-AI/EverMe">EverMe</a> - CLI and agent plugin suite for cross-device, cross-agent personal memory.</td>
</tr>
<tr>
<td><strong>Developer Integrations</strong></td>
<td><a href="https://github.com/EverMind-AI/evermem-claude-code">evermem-claude-code</a> · <a href="https://github.com/EverMind-AI/everos-plugins">everos-plugins</a> - plugins, skills, and migration tooling for AI coding agents.</td>
</tr>
</table>

Together, these repositories form EverMind's research-to-runtime stack: memory
methods, reusable algorithms, benchmark evidence, native agent products, and
practical developer integrations.

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Contributing

Raven is early, and useful contributions are welcome across runtime
architecture, TUI polish, provider support, memory workflows, proactivity,
benchmarks, documentation, and issue reports.

Before opening a PR:

1. Read `CLAUDE.md`.
2. Keep the change scoped.
3. Add or update tests for behavior changes.
4. Run the relevant `make` targets.
5. Use a Conventional Commit title.

### License

Raven is licensed under the Apache License 2.0. Portions of the runtime and
TUI layer originated from MIT-licensed upstream projects; their copyright
notices and license texts are retained in [NOTICES.md](NOTICES.md) and
[LICENSES/](LICENSES/).
