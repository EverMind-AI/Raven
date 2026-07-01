<div align="center" id="readme-top">

<img src="https://github.com/user-attachments/assets/224c1623-2705-4a48-8a60-fd5681ca0cb2" alt="Raven banner" width="100%">

<p align="center">
  <a href="https://x.com/evermind"><img src="https://img.shields.io/badge/EverMind-000000?labelColor=gray&style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://huggingface.co/EverMind-AI"><img src="https://img.shields.io/badge/🤗_HuggingFace-EverMind-F5C842?labelColor=gray&style=for-the-badge" alt="HuggingFace"></a>
  <a href="https://discord.gg/gYep5nQRZJ"><img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdiscord.com%2Fapi%2Fv10%2Finvites%2FgYep5nQRZJ%3Fwith_counts%3Dtrue&query=%24.approximate_presence_count&suffix=%20online&label=Discord&color=404EED&labelColor=gray&style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/EverMind-AI/EverOS/discussions/67"><img src="https://img.shields.io/badge/WeCom-EverMind_社区-07C160?labelColor=gray&style=for-the-badge&logo=wechat&logoColor=white" alt="WeChat"></a>
</p>

<p align="center">
  <a href="https://github.com/EverMind-AI/raven/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/EverMind-AI/raven/ci.yml?branch=main&label=CI" alt="CI"></a>
  <a href="https://github.com/EverMind-AI/raven/releases"><img src="https://img.shields.io/github/v/release/EverMind-AI/raven?label=Release" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/EverMind-AI/raven" alt="License"></a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python 3.12+">
</p>

[官网](https://raven.evermind.ai) · [English](README.md)

</div>

<br>

# Raven

Raven 是一个面向个人 Agent 和可复用 Agent Templates 的 **memory-first**
Agent OS。

Raven 由 EverMind 构建，底层基于
[EverOS](https://github.com/EverMind-AI/EverOS)。EverOS 为 Raven 提供跨会话
持久存在的用户记忆、Agent 记忆和世界知识。今天你可以把 Raven 作为原生终端
TUI 和 CLI 使用；如果你在构建自己的 Agent，也可以基于 Raven 把可重复工作流
封装成可分享的 digital workers。

<details>
  <summary><kbd>目录</kbd></summary>

<br>

- [快速开始](#快速开始)
- [2 分钟能做什么](#2-分钟能做什么)
- [消息网关](#消息网关)
- [为什么是 Raven](#为什么是-raven)
- [Raven 适合什么](#raven-适合什么)
- [Agent Templates](#agent-templates)
- [常用命令](#常用命令)
- [按目标阅读文档](#按目标阅读文档)
- [架构](#架构)
- [开发工作流](#开发工作流)
- [当前状态](#当前状态)
- [Star 支持](#star-支持)
- [EverMind 生态](#evermind-生态)
- [参与贡献](#参与贡献)

<br>

</details>

## 快速开始

安装 Raven，运行配置向导，然后启动原生终端 UI：

```bash
curl -fsSL https://raven.evermind.ai/install.sh | bash
source ~/.bashrc    # 或：source ~/.zshrc
raven onboard
raven
```

Raven 支持 OpenRouter、OpenAI、Anthropic、Gemini、DeepSeek、GitHub Copilot、
OpenAI Codex OAuth，以及自定义 OpenAI-compatible endpoints。

如果配置失败，或者 provider 还没有准备好，运行：

```bash
raven doctor
```

## 2 分钟能做什么

- 用 `raven` 或 `raven tui` 启动一个终端原生 Agent。
- 用 `raven agent -m "..."` 从 shell 里执行一次性任务。
- 用 `raven onboard` 配置 providers、sandbox、channels 和 memory。
- 用 `raven skill list` 浏览内置和本地 SkillForge skills。
- 用 `raven sessions list` 恢复、fork、导出或删除之前的工作。
- 用 `raven sentinel status` 查看主动记忆和 scheduled nudges 状态。

## 消息网关

Raven 目前内置 12 个 gateway adapters。用 `raven channels list` 查看本地安装中
可用的 adapters，用 `raven gateway` 启动 gateway daemon。

| Gateway | Package id | 说明 |
| --- | --- | --- |
| Telegram | `telegram` | Bot-based messaging |
| Slack | `slack` | Workspace messaging |
| Discord | `discord` | Server 和 bot messaging |
| WhatsApp | `whatsapp` | 使用内置 TypeScript bridge |
| Matrix | `matrix` | Matrix rooms 和 direct messages |
| Feishu | `feishu` | Lark/Feishu app integration |
| WeCom | `wecom` | 企业微信群和 app messaging |
| Mochat | `mochat` | API/socket-based messaging |
| QQ | `qq` | QQ bot integration |
| DingTalk | `dingtalk` | DingTalk stream integration |
| Email | `email` | IMAP/SMTP mailbox integration |
| WeChat | `weixin` | 个人微信 integration |

## 为什么是 Raven

大多数 Agent CLI 只做到 "LLM + tools + loop"。Demo 阶段够用，但一旦进入
真实日常工作就会遇到这些问题：

- 长会话撑爆上下文，重要信息开始丢失。
- 每轮都重复发送 system prompt、skills 和工具定义，Token 成本失控。
- Agent 永远被动等待输入，即使它已经看到有事需要处理。
- 有用的工作流留在聊天记录里，没有变成可复用技能。

Raven 把这些问题当成产品本身，而不是边缘 case。

Raven 围绕三个产品判断构建：

- **Memory-first：** 用户记忆、Agent 记忆和世界知识彼此独立、持久存在，并且
  可以跨会话复用。
- **Self-improving skills：** 重复工作流可以沉淀成 skills，记录反馈，并在失效时
  继续进化，而不是埋在聊天记录里。
- **Agent Templates：** 构建者可以从 Raven 出发，为具体场景定义一个 Agent，并在
  不重做底层 operating layer 的情况下分享出去。

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

## Agent Templates

Raven 是 EverMind 构建的 Apache-2.0 licensed、memory-first agent library。
它提供 runtime、memory layer、tools 和 Agent Templates，用来构建定制 Agent
和 digital workers。

当你想复用 Raven 的 operating layer，但又需要自己的场景、人格、workflow
policy、skills、integrations 或分发方式时，就可以从 Agent Template 开始。
一个 template 可以先是某个人的个人 Agent，之后再变成团队或社区可复用的
digital worker。

用 Raven 创建的 agents、templates、skills、workflows 和 modules 属于它们的
创建者。构建者可以在 Apache-2.0 license 下使用、修改、商业化和分享基于 Raven
或 Raven Agent Templates 创建的 Agent。

我们鼓励构建者标注 "Built with Raven" 并链接回这个仓库。未经 EverMind 明确
授权，不得使用 Raven 或 EverMind 的名称和 logo 暗示官方背书。

## 常用命令

| 目标 | 命令 |
| --- | --- |
| 启动原生 TUI | `raven` 或 `raven tui` |
| 检查 TUI runtime | `raven tui --check` |
| 配置 Raven | `raven onboard` |
| 执行一次 shell 任务 | `raven agent -m "..."` |
| 查看 providers | `raven provider list` |
| 列出消息渠道 | `raven channels list` |
| 启动 messaging gateway | `raven gateway` |
| 管理 sessions | `raven sessions list` |
| 查看 scheduled jobs | `raven cron list` |
| 浏览 skills | `raven skill list` |
| 查看 proactive state | `raven sentinel status` |
| 查看 plugins 和 memory backend | `raven plugins` |
| 调试 sandbox VMs | `raven sandbox list` |
| 查看本地状态 | `raven status` |
| 诊断配置 | `raven doctor` |

## 按目标阅读文档

| 目标 | 从这里开始 |
| --- | --- |
| 第一次安装和配置 | [快速开始](#快速开始) |
| 源码开发 | [开发工作流](#开发工作流) 和 [docs/dev.md](docs/dev.md) |
| Memory 和 plugin 架构 | [docs/memory-plugin-architecture.md](docs/memory-plugin-architecture.md) |
| Sandbox 使用和调试 | [docs/sandbox/usage.md](docs/sandbox/usage.md) |
| Proactivity 设计 | [docs/Proactivity-Plan.md](docs/Proactivity-Plan.md) |
| 详细设计文档 | [docs/README.md](docs/README.md) |

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

Raven 使用 Apache License 2.0。部分 runtime 和 TUI layer 来自 MIT 协议的
上游项目；相关 copyright notices 与 license texts 保留在
[NOTICES.md](NOTICES.md) 和 [LICENSES/](LICENSES/) 中。
