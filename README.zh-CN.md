<div align="center" id="readme-top">

<img src="https://github.com/user-attachments/assets/5a99d736-49ee-49c9-8b51-890f14078e78" alt="Raven banner" width="100%">

<p align="center">
  <a href="https://x.com/evermind"><img src="https://img.shields.io/badge/EverMind-000000?labelColor=gray&style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://huggingface.co/EverMind-AI"><img src="https://img.shields.io/badge/🤗_HuggingFace-EverMind-F5C842?labelColor=gray&style=for-the-badge" alt="HuggingFace"></a>
  <a href="https://discord.gg/gYep5nQRZJ"><img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdiscord.com%2Fapi%2Fv10%2Finvites%2FgYep5nQRZJ%3Fwith_counts%3Dtrue&query=%24.approximate_presence_count&suffix=%20online&label=Discord&color=404EED&labelColor=gray&style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/EverMind-AI/EverOS/discussions/67"><img src="https://img.shields.io/badge/WeCom-EverMind_社区-07C160?labelColor=gray&style=for-the-badge&logo=wechat&logoColor=white" alt="WeChat"></a>
</p>

[官网](https://raven.evermind.ai) · [English](README.md)

</div>

<br>

# Raven

Raven 是构建在 [EverOS](https://github.com/EverMind-AI/EverOS) 之上的
**The Self-Improving Agent Harness**，并内置可选 Deep Research，用于多来源深度研究。

Raven 会持续迭代支撑 Agent 的 harness：tools、skills、memory、code execution
runtime、policies 和工作环境。EverOS 为这个 harness 提供跨会话持久存在的用户
记忆、Agent 记忆和世界知识，让每一次运行都能改进 Agent 的行动方式、知识状态，
并把可重复工作流沉淀成可复用 Agent Templates 和 digital workers。

**Update：** Raven 新增 Deep Research。运行 `raven deep-research enable` 后，
Agent 可以在需要深度调查的任务中使用 MiroThinker-backed、多来源 research tool。

<p align="center">
  <img src="https://github.com/user-attachments/assets/a4dc5b21-c8e7-4397-95e1-50afeeb826e4" alt="从命令行启动 Raven" width="100%">
</p>

<details>
  <summary><kbd>目录</kbd></summary>

<br>

- [快速安装](#快速安装)
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

## 快速安装

### Linux、macOS、WSL2

```bash
curl -fsSL https://raven.evermind.ai/install.sh | bash
```

### Windows（原生 PowerShell）

> **提示：** 原生 Windows 可以不经过 WSL 运行 Raven。CLI、TUI、gateway 和
> tools 都会在 Windows 下原生安装。如果你更想用 WSL2，也可以直接使用上面的
> Linux/macOS 一键安装命令。

在 PowerShell 里运行：

```powershell
irm https://raven.evermind.ai/install.ps1 | iex
```

在 **Windows PowerShell 5.1**（Windows 自带的默认版本）下，上面的命令会报
`Permanent Redirect`，请改用直连地址：

```powershell
irm https://raw.githubusercontent.com/EverMind-AI/Raven/refs/heads/main/install.ps1 | iex
```

### 安装完成后

安装器会处理全部依赖：uv、Python 3.12、Node.js 22 和 Raven。

打开一个新终端。Linux、macOS 或 WSL2 也可以刷新当前 shell：

```bash
source ~/.bashrc    # 或：source ~/.zshrc
```

然后运行：

```bash
raven onboard
raven
```

Raven 支持 OpenRouter、OpenAI、Anthropic、Gemini、DeepSeek、GitHub Copilot、
OpenAI Codex OAuth，以及自定义 OpenAI-compatible endpoints。

如果配置失败，或者 provider 还没有准备好，运行：

```bash
raven doctor
```

### 升级现有安装

检查最新发布的稳定版本：

    raven upgrade --check

升级 Raven，同时保留现有配置、sessions 和 memory：

    raven upgrade

Raven 升级需要由用户主动触发，不会自动进行。Raven 会拒绝覆盖 editable source
installs；请更新源码 checkout，并重新运行对应的开发环境配置。
在 POSIX 系统上，命令会同步等待 helper 输出最终结果。原生 Windows 会先调度外部
helper，让当前运行的可执行文件退出；请等 helper 输出完成消息后再运行 Raven。

## 2 分钟能做什么

- 用 `raven` 或 `raven tui` 启动 Raven 的终端原生 harness。
- 用 `raven agent -m "..."` 执行一次性 shell 任务。
- 用 `raven onboard` 配置 providers、sandbox、channels 和 memory。
- 用 `raven deep-research enable` 启用 MiroThinker-backed deep_research tool。
- 用 `raven tracing` 打开 LLM/tool/memory spans 的本地 tracing dashboard。
- 用 `raven skill list` 浏览内置和本地 SkillForge skills。
- 用 `raven sessions list` 恢复、fork、导出或删除之前的工作。
- 用 `raven sentinel status` 查看主动记忆和 scheduled nudges 状态。

## 消息网关

Raven 目前内置 12 个 gateway adapters。用 `raven channels list` 查看本地安装中
可用的 adapters，用 `raven gateway` 启动 gateway daemon。

| Gateway | Adapter id | 说明 |
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
| WeChat | `weixin` | 个人微信 adapter；`weixin` 是当前 CLI id |

## 为什么是 Raven

大多数 Agent 工具只做到 "LLM + tools + loop"。Demo 阶段够用，但一旦进入
真实日常工作就会遇到这些问题：

- 长会话撑爆上下文，重要信息开始丢失。
- 每轮都重复发送 system prompt、skills 和工具定义，Token 成本失控。
- Agent 永远被动等待输入，即使它已经看到有事需要处理。
- 有用的工作流留在聊天记录里，没有变成可复用技能。

Raven 把 Agent 周围的 harness 当成产品本身，而不是一层薄包装或边缘 case。

Raven 的 self-improving harness 围绕四个产品判断构建：

- **Memory-first harness：** 用户记忆、Agent 记忆和世界知识彼此独立、持久存在，并且
  可以跨会话复用。
- **Deep Research as a tool：** 长篇、多来源研究可以通过 `raven deep-research enable`
  启用，并在任务需要更深调查时交给 Agent 使用。
- **Self-improving skills：** 重复工作流可以沉淀成 skills，记录反馈，并在失效时
  继续进化，而不是埋在聊天记录里。
- **Agent Templates：** 构建者可以从 Raven 出发，为具体场景定义一个 Agent，并在
  不重做底层 harness layer 的情况下分享出去。

<table>
<tr>
<th width="28%">能力</th>
<th width="36%">Raven</th>
<th width="36%">常见工具型 Agent</th>
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
<td><strong>Deep Research</strong></td>
<td>可选的 MiroThinker-backed deep_research tool，通过 <code>raven deep-research enable</code> 启用</td>
<td>通常依赖外部搜索标签页、临时 browser prompt 或一次性 research scripts</td>
</tr>
<tr>
<td><strong>Skill 进化</strong></td>
<td>识别可复用流程，生成 skill，追踪反馈，并在失效时进化</td>
<td>通常是静态 markdown prompt 或手动安装插件</td>
</tr>
</table>

<br>

## Raven 适合什么

Raven 面向那些普通聊天 Agent 和静态工具循环显得太轻、太浅、太短的工作流。

### 1. 终端原生日常工作

Raven 可以把 harness 作为 native TUI、直接 CLI 入口或 gateway-backed runtime
运行。TUI 不是网页 shell，而是一个 React/Ink 应用，通过 typed RPC 与 Python
runtime 通信。

### 2. 会变得有用的记忆

Raven 将 harness 连接到 EverOS，作为长期用户记忆与 Agent 记忆层。Sessions、
procedures 和可复用模式可以转成本地 skill 材料，而不是消失在旧 transcript 里。

### 3. 不会在压力下崩掉的上下文

Context stack 有 legacy path 和 Curator path。在 token 压力下，这个 harness
可以归档、检索并组装上下文，而不是盲目裁掉最旧消息。

### 4. 会主动开口的 Agent

Sentinel 监听事件、调度检查、判断 nudge 是否有用，并通过 guardrails 路由
主动动作。目标不是制造通知噪音，而是让这个 Agent Harness 真的能主动发现需要处理的事。

### 5. 会进化的 Skills

SkillForge 把 skills 当成 procedural memory。它可以识别可复用工作流、写入
skill 文件、追踪执行反馈，并在 instruction 失效时进化它。

### 6. 会自我进化的 Harness

`raven.evolver` 可以基于 benchmark 做可度量的 harness self-evolution：诊断失败
trajectories，把候选 harness patch 设计成真实 git commits，并且只提升通过统计
gate 的改动。它带有 sealed test set，用来保留诚实的泛化结果。一个命令
（`python -m raven.evolver run --config <yaml>`）即可运行，并支持完整 resume。
从 [raven/evolver/README.md](raven/evolver/README.md) 开始。

### 7. Harness 内的研究与可观测性

Raven 现在提供两个可选入口来支持更深的工作：`raven deep-research` 用于配置
MiroThinker-backed `deep_research` tool，让 Agent 在任务需要时执行多来源 research；
`raven tracing` 会打开本地 dashboard，查看已捕获的 LLM、tool 和 memory spans，
方便在不改变 agent workflow 的情况下复盘一次运行内部发生了什么。

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Agent Templates

Raven 是 EverMind 构建的 Apache-2.0 licensed、self-improving agent harness。
它提供 runtime、memory layer、tools 和 Agent Templates，用来构建定制 Agent
和 digital workers。

当你想复用 Raven 的 harness layer，但又需要自己的场景、人格、workflow
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
| 执行一次性 shell 任务 | `raven agent -m "..."` |
| 查看 providers | `raven provider list` |
| 配置 Deep Research | `raven deep-research enable` |
| 查看 Deep Research 配置 | `raven deep-research get` |
| 打开 tracing dashboard | `raven tracing` |
| 列出消息渠道 | `raven channels list` |
| 启动 messaging gateway | `raven gateway` |
| 管理 sessions | `raven sessions list` |
| 查看 scheduled jobs | `raven cron list` |
| 浏览 skills | `raven skill list` |
| 查看 proactive state | `raven sentinel status` |
| 查看 plugins 和 memory backend | `raven plugins` |
| 调试 sandbox VMs | `raven sandbox list` |
| 查看本地状态 | `raven status` |
| 检查 Raven 更新 | `raven upgrade --check` |
| 升级 Raven | `raven upgrade` |
| 诊断配置 | `raven doctor` |

## 按目标阅读文档

| 目标 | 从这里开始 |
| --- | --- |
| 第一次安装和配置 | [快速安装](#快速安装) |
| 源码开发 | [开发工作流](#开发工作流) 和 [docs/dev.md](docs/dev.md) |
| Memory 和 plugin 架构 | [docs/memory-plugin-architecture.md](docs/memory-plugin-architecture.md) |
| 配置 Deep Research | `raven deep-research --help` |
| 查看 tracing 和 observability | `raven tracing` 和 [docs/TRACING_STANDARD_API.md](docs/TRACING_STANDARD_API.md) |
| Sandbox 使用和调试 | [docs/sandbox/usage.md](docs/sandbox/usage.md) |
| Proactivity 设计 | [docs/Proactivity-Plan.md](docs/Proactivity-Plan.md) |
| Benchmark self-evolution | [raven/evolver/README.md](raven/evolver/README.md) |
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
        +--> Tracing          captured LLM / tool / memory spans
        +--> Eval Engine      task judgement and coordination
        +--> Evolver          benchmark-driven harness self-evolution
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
├── tracing/            # Span capture 和本地 tracing dashboard
├── evolver/            # Benchmark-driven harness self-evolution
├── sandbox/            # Isolated command execution
├── security/           # Trust boundaries and network checks
├── cli/                # `raven` command line entry point
└── config/             # Config schema and update helpers

ui-tui/                 # React/Ink 原生终端 UI
bridge/                 # WhatsApp TypeScript bridge
benchmarks/             # Benchmark adapters，包括 AppWorld evolver wiring
```

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## 开发工作流

源码开发、focused checks 和 PR 规则放在
[CONTRIBUTING.md](CONTRIBUTING.md) 与 [docs/dev.md](docs/dev.md) 里维护。
AI 协作规则放在 [AGENTS.md](AGENTS.md)；`CLAUDE.md` 作为兼容入口保留。

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
| Deep Research tool | 已实现，可选配置 |
| Tracing dashboard | 已实现 |
| Evolver pipeline | 已实现，benchmark adapters 持续演进 |
| Eval engine | 部分完成 |

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Star 支持

如果 Raven 是你希望存在的 Agent Harness，请 Star 这个仓库。它会帮助更多
self-improving agent builders 发现项目，也会给 EverMind 生态一个更强的信号，
继续投入开源 Agent。

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
<td><strong>Self-Improving Agent Harness</strong></td>
<td><a href="https://github.com/EverMind-AI/raven">Raven</a> - 终端原生 Agent Harness，负责 tools、skills、memory、proactivity、context control 和可复用 Agent Templates。</td>
</tr>
<tr>
<td><strong>Memory Runtime</strong></td>
<td><a href="https://github.com/EverMind-AI/EverOS">EverOS</a> - Raven 默认接入的 memory substrate，提供长期用户记忆、Agent 记忆、case/skill 提取和 multimodal parsing。</td>
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

1. 阅读 [AGENTS.md](AGENTS.md)。
2. 保持改动范围清晰。
3. 行为变化需要添加或更新测试。
4. 运行相关 `make` targets。
5. 使用 Conventional Commit 标题。

### 许可证

Raven 使用 Apache License 2.0。部分 runtime 和 TUI layer 来自 MIT 协议的
上游项目；相关 copyright notices 与 license texts 保留在
[NOTICES.md](NOTICES.md) 和 [LICENSES](LICENSES/) 中。
