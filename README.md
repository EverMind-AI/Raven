<div align="center" id="readme-top">

![Raven banner](https://github.com/user-attachments/assets/6c6f585a-21b6-4e7b-9187-acffe59d0c10)

<p align="center">
  <a href="https://x.com/evermind"><img src="https://img.shields.io/badge/EverMind-000000?labelColor=gray&style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://huggingface.co/EverMind-AI"><img src="https://img.shields.io/badge/HuggingFace-EverMind-F5C842?labelColor=gray&style=for-the-badge&logo=huggingface&logoColor=white" alt="Hugging Face"></a>
  <a href="https://discord.gg/gYep5nQRZJ"><img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdiscord.com%2Fapi%2Fv10%2Finvites%2FgYep5nQRZJ%3Fwith_counts%3Dtrue&query=%24.approximate_presence_count&suffix=%20online&label=Discord&color=404EED&labelColor=gray&style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/EverMind-AI/EverOS/discussions/67"><img src="https://img.shields.io/badge/WeCom-EverMind_Community-07C160?labelColor=gray&style=for-the-badge&logo=wechat&logoColor=white" alt="WeCom"></a>
</p>

[Website](https://raven.evermind.ai) · [中文](README.zh-CN.md)

</div>

<br>

# Raven

Raven is **The Self-Improving Agent Harness**, built on [EverOS](https://github.com/EverMind-AI/EverOS), with opt-in Deep Research for multi-source investigation.

Raven helps agents improve across runs by continuously refining the systems around them: tools, skills, memory, code execution, policies, and working environment. EverOS provides durable user memory, agent memory, and world knowledge across sessions, so successful workflows can evolve into reusable Agent Templates and digital workers.

**Update:** Raven added Deep Research. Enable it with `raven deep-research enable`
to give the agent access to MiroThinker-backed, multi-source research when a
task needs deeper investigation.

> Raven is pre-alpha. Interfaces and configuration may change quickly.

## Benchmarks

| Benchmark | Raven Result | Comparison |
| --- | --- | --- |
| Efficiency | `56.7%` at 27B; `58.1%` at 397B | Hermes `46.8%` / `47.9%`; `+9.9pp` at 27B |
| Self-evolution | Ranked `#1` on EvoAgentBench | `+6.2pp` over the next result across four methods |
| Proactivity | `0.60` F1 on ProAgentBench | `2.4x` Hermes/OpenClaw at `0.253` |

Results describe the published test configurations; model, task set, and evaluation protocol all affect outcomes.

https://github.com/user-attachments/assets/3c541dae-5852-447f-8ea6-c9877612ad57

Install Raven, run `raven` in your terminal, and this is the welcome screen you will see:

| Light theme | Dark theme |
| --- | --- |
| ![Raven terminal welcome screen in light theme](https://github.com/user-attachments/assets/d415573d-98ab-4265-872b-67c33b42dcee) | ![Raven terminal welcome screen in dark theme](https://github.com/user-attachments/assets/0ffa1ba4-c03f-4d3f-bfff-d9eda87122dd) |

## Quick Start

### Install

Linux, macOS, or WSL2:

```bash
curl -fsSL https://raven.evermind.ai/install.sh | bash
```

Native Windows PowerShell:

```powershell
irm https://raven.evermind.ai/install.ps1 | iex
```

Windows PowerShell 5.1 may reject the redirect. Use the direct installer URL instead:

```powershell
irm https://raw.githubusercontent.com/EverMind-AI/Raven/refs/heads/main/install.ps1 | iex
```

### Onboard and run

```bash
raven onboard
raven
```

The bilingual onboarding wizard configures six areas without requiring manual edits to `~/.raven/config.json`:

1. LLM provider and model
2. Sandbox or execution location
3. Chat channels
4. EverOS long-term memory
5. Deep Research
6. Cold-start import from other AI tools

Provider setup includes an in-step connectivity check. Optional steps can be skipped and configured later. If setup is incomplete, run:

```bash
raven doctor
```

### Upgrade

```bash
raven upgrade --check
raven upgrade
```

Upgrades preserve configuration, sessions, and memory. Raven does not update automatically.

## Deep Research

Deep Research gives Raven a dedicated path for open-ended questions that require broad web search, source reading, analysis, and multi-source cross-checking. It uses [MiroThinker](https://miromind.ai/) and returns a self-contained answer with inline citations and references.

Configure it during onboarding or later:

```bash
raven deep-research enable
raven deep-research get
```

Once configured, Raven can invoke `deep_research` when a task needs more than a quick lookup. Before a paid, minute-scale run, interactive surfaces ask whether to use Deep Research or regular search for that query.

Delivery adapts to where Raven is running:

- **CLI and TUI:** progress streams inline while Raven searches, reads pages, and runs analysis. The completed report is shown directly without being rewritten by the main model.
- **Gateway channels:** the run continues in the background and the completed report is delivered back to the originating conversation.
- **Local archive:** every completed result is saved under `<workspace>/deep_research/` for later use.

Use regular search for a single fact or URL. Use Deep Research for comparisons, landscape reviews, technical investigations, and questions where source agreement matters.

## Tracing

Tracing makes Raven's reasoning path inspectable without sending trace data to a hosted service. Open the local dashboard with:

```bash
raven tracing
```

Each `session.turn` becomes a trace tree containing the work that happened beneath it:

- LLM calls, models, token usage, cost, latency, and errors
- Tool inputs and outputs
- Subagent runs and parent-child relationships
- Skill reads and injections
- Memory recall, storage, extraction, and consolidation
- Large prompts and results stored as out-of-line artifacts

Tracing is enabled by default and is designed to never interrupt Raven's control flow. Spans are stored locally at `~/.raven/traces/logs/audit-spans.log`; set `RAVEN_TRACING_DIR` to move the state directory or `RAVEN_TRACING=0` to disable recording.

The schema follows a small, versioned semantic contract. See the [Tracing Standard API](docs/TRACING_STANDARD_API.md) for span names, attributes, artifact behavior, and extension rules.

## Core Systems

| System | What it adds |
| --- | --- |
| **EverOS memory** | Durable user memory, agent memory, and world knowledge across sessions |
| **Context Engine** | Explicit token budgets and a unified assembly pipeline that preserves the most useful context |
| **Proactivity** | Sentinel observations, scheduled work, nudge policy, and deferred decisions |
| **SkillForge** | Built-in, workspace, EverOS, and mirrored skills with retrieval, feedback, and evolution |
| **Evolver** | Reproducible evaluation loops for improving agents and reusable procedures |
| **Agent Templates** | Shareable starting points for specialized digital workers built on the same harness |

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Providers and Gateways

Raven supports API-key, OAuth, local, and OpenAI-compatible providers. The onboarding catalog includes OpenRouter, OpenAI, Anthropic, Gemini, MiniMax, DeepSeek, Z.ai, DashScope, Moonshot, VolcEngine, SiliconFlow, Groq, AiHubMix, Azure OpenAI, GitHub Copilot OAuth, OpenAI Codex OAuth, Ollama, and hosted vLLM.

Twelve gateway adapters connect Raven to Telegram, Slack, Discord, WhatsApp, Matrix, Feishu, WeCom, Mochat, QQ, DingTalk, Email, and WeChat.

```bash
raven channels list
raven channels enable <adapter>
raven gateway
```

## Command Reference

| Command | Purpose |
| --- | --- |
| `raven` or `raven tui` | Launch the terminal UI |
| `raven agent -m "..."` | Run a one-shot task |
| `raven onboard` | Configure providers, sandboxing, channels, memory, research, and import |
| `raven status` | Show configuration and runtime status |
| `raven doctor` | Diagnose provider and environment problems |
| `raven tracing` | Open the local trace dashboard |
| `raven sessions list` | Browse, resume, fork, export, or delete sessions |
| `raven skill list` | Inspect the local SkillForge catalog |
| `raven sentinel status` | Inspect proactive memory and scheduled nudges |
| `raven cron list` | Inspect scheduled jobs |
| `raven gateway` | Run messaging gateways |
| `raven upgrade` | Upgrade a managed installation |

Run `raven --help` or `raven <command> --help` for the complete CLI surface.

## Documentation

- [Documentation index](docs/README.md)
- [Developer workflow](docs/dev.md)
- [Tracing Standard API](docs/TRACING_STANDARD_API.md)
- [Sandbox usage](docs/sandbox/usage.md)
- [Memory plugin architecture](docs/memory-plugin-architecture.md)
- [Self-evolution loop mapping](docs/specs/self-evolution-loop-raven-mapping.md)
- [Proactivity implementation](docs/Proactivity-Implementation.md)

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Architecture

```text
CLI / TUI / Messaging Gateways
              |
              v
          TUI-RPC / Spine
              |
              v
           Agent Loop
      +-------+-------+
      |       |       |
  Providers  Tools  Subagents
      |       |       |
      +--- Context Engine ---+
              |
      +-------+--------+
      |                |
 EverOS Memory     SkillForge
      |                |
      +--- Proactivity + Evolver
```

The Python runtime and React/Ink TUI communicate only through typed TUI-RPC. The Spine carries runtime events, while the Agent Loop coordinates providers, tools, context, memory, skills, subagents, and proactive work.

Key directories:

```text
raven/agent/             agent loop, tools, and subagents
raven/channels/          messaging adapters
raven/context_engine/    context assembly and token budgeting
raven/memory_engine/     EverOS integration and local skill memory
raven/proactive_engine/  sentinel, scheduling, and nudges
raven/providers/         model providers and routing
raven/skill_hub/         external skill retrieval
raven/tracing/           instrumentation, storage, and viewer
raven/tui_rpc/           typed runtime-to-TUI boundary
ui-tui/                  React/Ink terminal interface
```

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## EverMind Ecosystem

Raven is part of the [EverMind](https://evermind.ai/) open-source ecosystem. Explore [EverOS](https://github.com/EverMind-AI/EverOS), [EverAlgo](https://github.com/EverMind-AI/EverAlgo), [HyperMem](https://github.com/EverMind-AI/HyperMem), [EvoAgentBench](https://github.com/EverMind-AI/EvoAgentBench), [EverMemBench](https://github.com/EverMind-AI/EverMemBench), and [EverMe](https://github.com/EverMind-AI/EverMe).

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Contributing

Issues and pull requests are welcome. Start with the [developer workflow](docs/dev.md), follow [AGENTS.md](AGENTS.md) for repository rules, and use [GitHub Discussions](https://github.com/EverMind-AI/Raven/discussions) for design conversations.

## License

[Apache License 2.0](LICENSE)
