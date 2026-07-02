<div align="center" id="readme-top">

![Raven banner](https://github.com/user-attachments/assets/5a99d736-49ee-49c9-8b51-890f14078e78)

<p align="center">
  <a href="https://x.com/evermind"><img src="https://img.shields.io/badge/EverMind-000000?labelColor=gray&style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://huggingface.co/EverMind-AI"><img src="https://img.shields.io/badge/🤗_HuggingFace-EverMind-F5C842?labelColor=gray&style=for-the-badge" alt="HuggingFace"></a>
  <a href="https://discord.gg/gYep5nQRZJ"><img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdiscord.com%2Fapi%2Fv10%2Finvites%2FgYep5nQRZJ%3Fwith_counts%3Dtrue&query=%24.approximate_presence_count&suffix=%20online&label=Discord&color=404EED&labelColor=gray&style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/EverMind-AI/EverOS/discussions/67"><img src="https://img.shields.io/badge/WeCom-EverMind_社区-07C160?labelColor=gray&style=for-the-badge&logo=wechat&logoColor=white" alt="WeChat"></a>
</p>

[Website](https://raven.evermind.ai) · [中文](README.zh-CN.md)

</div>

<br>

# Raven

Raven is **The Self-Improving Agent Harness**, built on [EverOS](https://github.com/EverMind-AI/EverOS).

Raven helps agents improve across runs by continuously refining the systems around them: tools, skills, memory, code execution, policies, and working environment. EverOS provides durable user memory, agent memory, and world knowledge across sessions, so successful workflows can evolve into reusable Agent Templates and digital workers.

<p align="center">
  <img src="https://github.com/user-attachments/assets/a4dc5b21-c8e7-4397-95e1-50afeeb826e4" alt="Starting Raven from the command line" width="100%">
</p>

<details>
  <summary><kbd>Table of Contents</kbd></summary>

<br>

- [Quick Install](#quick-install)
- [What You Can Do in 2 Minutes](#what-you-can-do-in-2-minutes)
- [Messaging Gateways](#messaging-gateways)
- [Why Raven](#why-raven)
- [What Raven Is Built For](#what-raven-is-built-for)
- [Agent Templates](#agent-templates)
- [Useful Commands](#useful-commands)
- [Docs by Goal](#docs-by-goal)
- [Architecture](#architecture)
- [Developer Workflow](#developer-workflow)
- [Status](#status)
- [Star Us](#star-us)
- [EverMind Ecosystem](#evermind-ecosystem)
- [Contributing](#contributing)

<br>

</details>

## Quick Install

### Linux, macOS, WSL2

```bash
curl -fsSL https://raven.evermind.ai/install.sh | bash
```

### Windows (native, PowerShell)

> **Heads up:** Native Windows runs Raven without WSL. CLI, TUI, gateway, and
> tools install natively. If you would rather use WSL2, the Linux/macOS
> one-liner above works there too.

Run this in PowerShell:

```powershell
irm https://raven.evermind.ai/install.ps1 | iex
```

### After installation

The installer handles everything: uv, Python 3.12, Node.js 22, and Raven.

Open a new terminal. On Linux, macOS, or WSL2, you can also reload your current
shell:

```bash
source ~/.bashrc    # or: source ~/.zshrc
```

Then run:

```bash
raven onboard
raven
```

Raven supports OpenRouter, OpenAI, Anthropic, Gemini, DeepSeek, GitHub Copilot,
OpenAI Codex OAuth, and custom OpenAI-compatible endpoints.

If setup fails or a provider is not ready, run:

```bash
raven doctor
```

## What You Can Do in 2 Minutes

- Start the Raven harness in a terminal-native TUI with `raven` or `raven tui`.
- Run a one-shot shell task with `raven agent -m "..."`.
- Configure providers, sandboxing, channels, and memory with `raven onboard`.
- Browse built-in and local SkillForge skills with `raven skill list`.
- Resume, fork, export, or delete previous work with `raven sessions list`.
- Check proactive memory and scheduled nudges with `raven sentinel status`.

## Messaging Gateways

Raven currently ships 12 gateway adapters. Use `raven channels list` to see the
adapters available in your local install and `raven gateway` to run the gateway
daemon.

| Gateway | Adapter id | Notes |
| --- | --- | --- |
| Telegram | `telegram` | Bot-based messaging |
| Slack | `slack` | Workspace messaging |
| Discord | `discord` | Server and bot messaging |
| WhatsApp | `whatsapp` | Uses the bundled TypeScript bridge |
| Matrix | `matrix` | Matrix rooms and direct messages |
| Feishu | `feishu` | Lark/Feishu app integration |
| WeCom | `wecom` | WeCom group and app messaging |
| Mochat | `mochat` | API/socket-based messaging |
| QQ | `qq` | QQ bot integration |
| DingTalk | `dingtalk` | DingTalk stream integration |
| Email | `email` | IMAP/SMTP mailbox integration |
| WeChat | `weixin` | Personal WeChat adapter; `weixin` is the current CLI id |

## Why Raven

Most agent tools stop at "LLM + tools + loop." That works for demos, but it
breaks down when the agent becomes part of your daily environment:

- Long sessions overflow context and lose important details.
- Every turn re-sends the same system prompt, skills, and tool definitions.
- The agent waits passively even when it can see something that needs action.
- Useful workflows stay trapped in chat history instead of becoming reusable
  skills.

Raven treats the harness around the agent as the product, not a thin wrapper or
an edge case.

Raven's self-improving harness is built around three product bets:

- **Memory-first harness:** user memory, agent memory, and world knowledge stay
  separate, durable, and reusable across sessions.
- **Self-improving skills:** repeated workflows can become skills, collect
  feedback, and evolve instead of staying buried in chat history.
- **Agent Templates:** builders can start from Raven, define an agent for a
  scenario, and share it without rebuilding the harness layer.

<table>
<tr>
<th width="28%">Capability</th>
<th width="36%">Raven</th>
<th width="36%">Typical tool-based agent</th>
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

## What Raven Is Built For

Raven is designed for the workflows where ordinary chat agents and static tool
loops feel too small.

### 1. Terminal-Native Daily Work

Raven can run the harness as a native TUI, a direct CLI entry point, or a
gateway-backed runtime. The TUI is not a web shell: it is a React/Ink
application talking to Raven's Python runtime through a typed RPC protocol.

### 2. Memory That Becomes Useful

Raven connects the harness to EverOS for long-term user and agent memory.
Sessions, procedures, and reusable patterns can be turned into local skill
material instead of disappearing into old transcripts.

### 3. Context That Does Not Collapse Under Pressure

The context stack has a legacy path and a Curator path. Under pressure, the
harness can archive, retrieve, and assemble context with explicit budgets
instead of blindly clipping the oldest messages.

### 4. Agents That Can Reach Out First

Sentinel watches events, schedules checks, evaluates whether a nudge is useful,
and routes proactive actions through guardrails. The point is not noisy
notifications; the point is an agent harness that can notice.

### 5. Skills That Improve

SkillForge treats skills as procedural memory. It can detect reusable workflows,
write skill files, track execution feedback, and evolve instructions when they
stop working.

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Agent Templates

Raven is an Apache-2.0 licensed, self-improving agent harness built by EverMind.
It provides the runtime, memory layer, tools, and Agent Templates for building
custom agents and digital workers.

Use an Agent Template when you want Raven's harness layer but your own
scenario, personality, workflow policy, skills, integrations, or distribution
model. A template can start as one person's agent and later become a repeatable
digital worker for a team or community.

Agents, templates, skills, workflows, and modules created with Raven belong to
their creators. Builders may use, modify, commercialize, and share agents built
with Raven or based on Raven Agent Templates under the Apache-2.0 license.

We encourage builders to say "Built with Raven" and link back to this
repository. The Raven and EverMind names and logos may not be used to imply
official endorsement unless explicitly approved by EverMind.

## Useful Commands

| Goal | Command |
| --- | --- |
| Start the native TUI | `raven` or `raven tui` |
| Check the TUI runtime | `raven tui --check` |
| Configure Raven | `raven onboard` |
| Run a one-shot shell task | `raven agent -m "..."` |
| Review providers | `raven provider list` |
| List messaging channels | `raven channels list` |
| Start the messaging gateway | `raven gateway` |
| Manage sessions | `raven sessions list` |
| Inspect scheduled jobs | `raven cron list` |
| Browse skills | `raven skill list` |
| Inspect proactive state | `raven sentinel status` |
| Show plugins and memory backend | `raven plugins` |
| Debug sandbox VMs | `raven sandbox list` |
| Show local status | `raven status` |
| Diagnose setup | `raven doctor` |

## Docs by Goal

| Goal | Start here |
| --- | --- |
| First-time install and setup | [Quick Install](#quick-install) |
| Source-based development | [Developer Workflow](#developer-workflow) and [docs/dev.md](docs/dev.md) |
| Memory and plugin architecture | [docs/memory-plugin-architecture.md](docs/memory-plugin-architecture.md) |
| Sandbox usage and debugging | [docs/sandbox/usage.md](docs/sandbox/usage.md) |
| Proactivity design | [docs/Proactivity-Plan.md](docs/Proactivity-Plan.md) |
| Detailed design notes | [docs/README.md](docs/README.md) |

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

Source setup, focused checks, and PR rules live in
[CONTRIBUTING.md](CONTRIBUTING.md) and [docs/dev.md](docs/dev.md).
AI-collaboration rules live in [AGENTS.md](AGENTS.md); `CLAUDE.md` is kept as a
compatibility entry point.

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

If Raven is the kind of agent harness you want to exist, star the repo. It
helps more builders of self-improving agents discover the project and gives the
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
<td><strong>Self-Improving Agent Harness</strong></td>
<td><a href="https://github.com/EverMind-AI/raven">Raven</a> - the terminal-native agent harness for tools, skills, memory, proactivity, context control, and reusable Agent Templates.</td>
</tr>
<tr>
<td><strong>Memory Runtime</strong></td>
<td><a href="https://github.com/EverMind-AI/EverOS">EverOS</a> - the memory substrate Raven uses for durable user memory, agent memory, case extraction, skill extraction, and multimodal parsing.</td>
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

1. Read [AGENTS.md](AGENTS.md).
2. Keep the change scoped.
3. Add or update tests for behavior changes.
4. Run the relevant `make` targets.
5. Use a Conventional Commit title.

### License

Raven is licensed under the Apache License 2.0. Portions of the runtime and
TUI layer originated from MIT-licensed upstream projects; their copyright
notices and license texts are retained in [NOTICES.md](NOTICES.md) and
[LICENSES](LICENSES/).
