<div align="center" id="readme-top">

# Raven

### 为终端重度用户打造的 AI-native Command Line Agent。

<p align="center">
  <a href="https://github.com/EverMind-AI/raven/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/EverMind-AI/raven/ci.yml?branch=main&label=CI&style=for-the-badge" alt="CI"></a>
  <a href="https://github.com/EverMind-AI/raven/blob/main/LICENSE"><img src="https://img.shields.io/github/license/EverMind-AI/raven?style=for-the-badge" alt="License"></a>
  <a href="https://x.com/evermind"><img src="https://img.shields.io/badge/EverMind-000000?labelColor=gray&style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://huggingface.co/EverMind-AI"><img src="https://img.shields.io/badge/HuggingFace-EverMind-F5C842?labelColor=gray&style=for-the-badge" alt="HuggingFace"></a>
</p>

[Website](https://evermind.ai) · [Documentation](https://docs.evermind.ai) · [EverOS](https://github.com/EverMind-AI/EverOS) · [English](README.md)

</div>

<br>

<details>
  <summary><kbd>目录</kbd></summary>

<br>

- [为什么是 Raven](#为什么是-raven)
- [快速开始](#快速开始)
- [Raven 适合什么](#raven-适合什么)
- [架构](#架构)
- [开发工作流](#开发工作流)
- [当前状态](#当前状态)
- [Star 支持](#star-支持)
- [EverMind 生态](#evermind-生态)
- [参与贡献](#参与贡献)

<br>

</details>

## 为什么是 Raven

Raven 是一个原生 Command Line Agent，不是给 shell 套了一层聊天框。
它面向已经生活在终端、仓库、日志、脚本、会话和长流程里的用户。
目标很直接：让你的终端拥有一个会记忆、会行动、会使用工具、会管理
上下文，并且能持续沉淀自身技能的 Agent。

大多数 Agent CLI 只做到 "LLM + tools + loop"。Demo 阶段够用，但一旦进入
真实日常工作就会遇到这些问题：

- 长会话撑爆上下文，重要信息开始丢失。
- 每轮都重复发送 system prompt、skills 和工具定义，Token 成本失控。
- Agent 永远被动等待输入，即使它已经看到有事需要处理。
- 有用的工作流留在聊天记录里，没有变成可复用技能。

Raven 把这些问题当成产品本身，而不是边缘 case。

<table>
<tr>
<th width="28%">能力</th>
<th width="36%">Raven</th>
<th width="36%">常见 Agent CLI</th>
</tr>
<tr>
<td><strong>原生终端产品</strong></td>
<td>交互式 TUI、CLI、Gateway 模式，以及 Python 与 React/Ink 之间的 typed RPC</td>
<td>通常只是聊天循环外面的一层命令包装</td>
</tr>
<tr>
<td><strong>长期记忆</strong></td>
<td>EverOS-backed memory、本地 skills、session history 和 workspace templates</td>
<td>通常是临时上下文或 provider 侧聊天历史</td>
</tr>
<tr>
<td><strong>上下文控制</strong></td>
<td>Curator 与 legacy context engines，显式 token budgets 和 fail-safes</td>
<td>通常是截断、摘要或隐藏 prompt heuristic</td>
</tr>
<tr>
<td><strong>主动性</strong></td>
<td>Sentinel、scheduler、nudge policy 和 deferred decision flow</td>
<td>通常等用户再次输入</td>
</tr>
<tr>
<td><strong>Skill 进化</strong></td>
<td>识别可复用流程，生成 skill，追踪反馈，并在失效时进化</td>
<td>通常是静态 markdown prompt 或手动安装插件</td>
</tr>
</table>

<br>

## 快速开始

> 目标：从源码启动 Raven，完成 onboarding，然后进入原生 TUI。

### 0. 前置条件

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Node.js 22+，用于原生 TUI
- 一个模型 provider key，或通过 onboarding 配置 OAuth provider

Raven 支持 OpenRouter、OpenAI、Anthropic、Gemini、DeepSeek、GitHub Copilot、
OpenAI Codex OAuth，以及自定义 OpenAI-compatible endpoint。

### 1. 从源码安装

```bash
git clone https://github.com/EverMind-AI/raven.git
cd raven
uv sync --extra dev --dev
```

安装 TUI 依赖：

```bash
npm ci --prefix ui-tui
```

### 2. 运行 Onboarding

```bash
uv run raven onboard
```

Wizard 会配置：

- LLM provider 和默认模型；
- 可选 sandbox 执行环境；
- 可选聊天 channel；
- 可选 EverOS 长期记忆。

### 3. 启动 Raven

```bash
uv run raven
```

裸 `raven` 会打开原生 TUI。也可以直接执行一次任务：

```bash
uv run raven agent -m "Inspect this repo and tell me what to improve first."
```

对接 Telegram、Slack、Discord、Matrix、WhatsApp、WeCom 等平台时：

```bash
uv run raven gateway
```

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Raven 适合什么

Raven 面向那些普通聊天 Agent 显得太轻、太浅、太短的工作流。

### 1. 终端原生日常工作

Raven 可以作为 native TUI、直接 CLI agent 或 gateway-backed agent 运行。
TUI 不是网页 shell，而是一个 React/Ink 应用，通过 typed RPC 与 Python
runtime 通信。

### 2. 会变得有用的记忆

Raven 连接 EverOS 作为长期用户记忆与 Agent 记忆层。Sessions、procedures
和可复用模式可以转成本地 skill 材料，而不是消失在旧 transcript 里。

### 3. 不会在压力下崩掉的上下文

Context stack 有 legacy path 和 Curator path。在 token 压力下，Raven 可以
归档、检索并组装上下文，而不是盲目裁掉最旧消息。

### 4. 会主动开口的 Agent

Sentinel 监听事件、调度检查、判断 nudge 是否有用，并通过 guardrails 路由
主动动作。目标不是制造通知噪音，而是让 Agent 真的能 notice。

### 5. 会进化的 Skills

SkillForge 把 skills 当成 procedural memory。它可以识别可复用工作流、写入
skill 文件、追踪执行反馈，并在 instruction 失效时进化它。

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## 架构

每个 turn 都流经 Spine：一个入口 `submit`，一个出口 `emit`，并用
per-conversation lanes 处理顺序与取消。各个 feature engine 通过显式 handoff
接入 Agent loop，而不是互相 import。

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

### 仓库结构

```text
raven/
├── spine/              # Per-turn backbone: submit -> lanes -> emit
├── agent/              # Agent loop, tools, hooks, subagents, context builder
├── channels/           # Telegram, Discord, Slack, Matrix, WhatsApp, WeCom, ...
├── tui_rpc/            # Native TUI protocol 的 Python 侧
├── providers/          # LLM provider adapters
├── context_engine/     # Context assembly 与 Curator path
├── proactive_engine/   # Sentinel, scheduler, nudges, feedback
├── memory_engine/      # EverOS memory, local skills, SkillForge
├── token_wise/         # Usage tracking, cache placement, routing
├── sandbox/            # Isolated command execution
├── security/           # Trust boundaries and network checks
├── cli/                # `raven` command line entry point
└── config/             # Config schema and update helpers

ui-tui/                 # React/Ink 原生终端 UI
bridge/                 # WhatsApp TypeScript bridge
```

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## 开发工作流

安装依赖并设置 hooks：

```bash
make install
```

运行本地 CI gate：

```bash
make ci
```

常用命令：

```bash
make lint-python
make lint-tui
make lint-bridge
make test-python
make test-tui
```

仓库使用：

- `uv` 管理 Python 依赖；
- `ruff` 和 `pre-commit` 做 Python 与 repo hygiene；
- `commitlint` 加 Python checker 校验 Conventional Commit subjects 和
  ASCII-only public history；
- `eslint`、`tsc`、`vitest` 和 RPC drift check 校验 TUI；
- `npm ci`、`tsc` 和 `npm audit --audit-level=critical` 校验 bridge。

`CLAUDE.md` 包含 branch naming、commit format、dependency update、testing 和
PR hygiene 的完整协作规则。

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## 当前状态

Raven 仍处于 pre-alpha，变化会很快。API 可能调整，但核心产品面已经在仓库里。

| 层级 | 状态 |
| --- | --- |
| Native TUI + CLI | 可用 |
| Spine runtime | 可用 |
| Base agent loop, tools, providers | 可用 |
| Context engine | 已实现，持续演进 |
| Sentinel proactivity | 已实现，持续演进 |
| TokenWise strategies | 已实现 |
| SkillForge | 已实现 |
| Eval engine | 部分完成 |

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Star 支持

如果 Raven 是你希望存在的 Command Line Agent，请 Star 这个仓库。它会帮助更多
terminal-native builders 发现项目，也会给 EverMind 生态一个更强的信号，继续
投入开源 Agent。

### Star 趋势

[![Star History Chart](https://api.star-history.com/svg?repos=EverMind-AI/raven&type=Date)](https://www.star-history.com/#EverMind-AI/raven&Date)

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## EverMind 生态

EverMind 是一个面向长期记忆、自进化 Agent、AI-native interfaces 和记忆评测的开源生态。

<table>
<tr>
<th colspan="2">EverMind Open-Source Ecosystem</th>
</tr>
<tr>
<td><strong>Memory Runtime</strong></td>
<td><a href="https://github.com/EverMind-AI/EverOS">EverOS</a> - 本地记忆操作系统，以及有研究支撑的 Agent 和用户记忆 runtime。</td>
</tr>
<tr>
<td><strong>AI-Native CLI Agent</strong></td>
<td><a href="https://github.com/EverMind-AI/raven">Raven</a> - 把记忆、主动性、上下文控制和 skill evolution 带进终端的 native command line agent。</td>
</tr>
<tr>
<td><strong>Algorithm Engine</strong></td>
<td><a href="https://github.com/EverMind-AI/EverAlgo">EverAlgo</a> - stateless extraction、ranking、parsing 和 memory operators，为 EverOS 提供算法能力。</td>
</tr>
<tr>
<td><strong>Hypergraph Memory</strong></td>
<td><a href="https://github.com/EverMind-AI/HyperMem">HyperMem</a> - 面向长期对话的 hypergraph memory，拥有 benchmark-backed topic -> episode -> fact retrieval。</td>
</tr>
<tr>
<td><strong>Benchmarks</strong></td>
<td><a href="https://github.com/EverMind-AI/EverMemBench">EverMemBench</a> · <a href="https://github.com/EverMind-AI/EvoAgentBench">EvoAgentBench</a> - conversational memory 和 Agent self-evolution 的评测套件。</td>
</tr>
<tr>
<td><strong>Long-Context Research</strong></td>
<td><a href="https://github.com/EverMind-AI/MSA">MSA</a> - Memory Sparse Attention，用于可扩展 latent memory 和 100M-token contexts。</td>
</tr>
<tr>
<td><strong>Personal Memory Layer</strong></td>
<td><a href="https://github.com/EverMind-AI/EverMe">EverMe</a> - CLI 和 Agent plugin suite，用于跨设备、跨 Agent 的个人记忆。</td>
</tr>
<tr>
<td><strong>Developer Integrations</strong></td>
<td><a href="https://github.com/EverMind-AI/evermem-claude-code">evermem-claude-code</a> · <a href="https://github.com/EverMind-AI/everos-plugins">everos-plugins</a> - AI coding agents 的 plugins、skills 和 migration tooling。</td>
</tr>
</table>

这些仓库共同构成 EverMind 的 research-to-runtime stack：记忆方法、可复用算法、
benchmark evidence、native agent products 和开发者集成。

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## 参与贡献

Raven 还很早。欢迎在 runtime architecture、TUI polish、provider support、
memory workflows、proactivity、benchmarks、documentation 和 issue reports 上贡献。

提交 PR 前：

1. 阅读 `CLAUDE.md`。
2. 保持改动范围清晰。
3. 行为变化需要添加或更新测试。
4. 运行相关 `make` targets。
5. 使用 Conventional Commit 标题。

### 许可证

Raven 使用 MIT License。基础 agent runtime 来自 HKUDS 的 MIT 协议项目
[nanobot](https://github.com/HKUDS/nanobot)。第三方归属说明见
[LICENSE](LICENSE)、[NOTICES.md](NOTICES.md) 和 [LICENSES/](LICENSES/)。
