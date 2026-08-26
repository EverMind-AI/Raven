<div align="center" id="readme-top">

![Raven banner](https://github.com/user-attachments/assets/118d2bba-342f-4435-b446-2edafc33a38c)

<p align="center">
  <a href="https://x.com/evermind"><img src="https://img.shields.io/badge/EverMind-000000?labelColor=gray&style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://huggingface.co/EverMind-AI"><img src="https://img.shields.io/badge/HuggingFace-EverMind-F5C842?labelColor=gray&style=for-the-badge&logo=huggingface&logoColor=white" alt="Hugging Face"></a>
  <a href="https://discord.gg/gYep5nQRZJ"><img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdiscord.com%2Fapi%2Fv10%2Finvites%2FgYep5nQRZJ%3Fwith_counts%3Dtrue&query=%24.approximate_presence_count&suffix=%20online&label=Discord&color=404EED&labelColor=gray&style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/EverMind-AI/EverOS/discussions/67"><img src="https://img.shields.io/badge/WeCom-EverMind_Community-07C160?labelColor=gray&style=for-the-badge&logo=wechat&logoColor=white" alt="WeCom"></a>
</p>

[官网](https://raven.evermind.ai) · [English](README.md)

</div>

<br>

# Raven

Raven 是面向终端与长程任务的 **The Self-Improving Agent Harness**。它不再把模型与 harness 视为静态组合，而是将每次真实执行纳入“执行—追踪—记忆—评测—反馈”的持续闭环。

Raven 构建于 [EverOS](https://github.com/EverMind-AI/EverOS) 之上，让用户记忆、Agent 经验与世界知识跨会话延续。每次运行都可以持续改进模型周围的 tools、skills、context、policies 与 workflows，让经过验证的工作沉淀为可复用的 Agent Templates 和 digital workers。

> Raven 目前处于 pre-alpha 阶段，接口和配置可能快速变化。

## 基准测试

| 基准测试 | Raven 结果 | 对比 |
| --- | --- | --- |
| 效率 | 27B 下为 `56.7%`；397B 下为 `58.1%` | Hermes 为 `46.8%` / `47.9%`；27B 下领先 `+9.9pp` |
| 自我进化 | EvoAgentBench 排名 `#1` | 在四种方法中领先下一名 `+6.2pp` |
| 主动性 | ProAgentBench F1 为 `0.60` | 是 Hermes/OpenClaw `0.253` 的 `2.4x` |

以上结果对应已发布的测试配置；模型、任务集和评测协议都会影响最终结果。

https://github.com/user-attachments/assets/3c541dae-5852-447f-8ea6-c9877612ad57

## 快速开始

### 安装

Linux、macOS 或 WSL2：

```bash
curl -fsSL https://raven.evermind.ai/install.sh | bash
```

原生 Windows PowerShell：

```powershell
irm https://raven.evermind.ai/install.ps1 | iex
```

Windows PowerShell 5.1 可能拒绝重定向，请改用直连安装地址：

```powershell
irm https://raw.githubusercontent.com/EverMind-AI/Raven/refs/heads/main/install.ps1 | iex
```

### 完成引导并运行

```bash
raven
```

首次运行只需这一条：尚未配置时，`raven` 会先带你走完引导，然后在同一次会话里直接进入 TUI。之后想重新配置，再显式运行 `raven onboard`。

双语 onboarding 向导会配置六个方面，无需手动编辑 `~/.raven/config.json`：

1. LLM provider 和模型
2. Sandbox 或执行位置
3. 聊天渠道
4. EverOS 长期记忆
5. Deep Research
6. 从其他 AI 工具进行冷启动导入

Provider 配置包含向导内连通性检查。可选步骤可以跳过，之后再配置。如果设置尚未完成，请运行：

```bash
raven doctor
```

### 升级

```bash
raven upgrade --check
raven upgrade
```

升级会保留配置、sessions 和 memory。Raven 不会自动更新。

## Deep Research

Deep Research 为需要广泛网页搜索、来源阅读、分析和多来源交叉验证的开放式问题提供专用路径。它使用 [MiroThinker](https://miromind.ai/)，返回带有行内引用和参考来源的完整答案。

可以在 onboarding 时配置，也可以稍后启用：

```bash
raven deep-research enable
raven deep-research get
```

配置完成后，当任务需要的不只是快速查询时，Raven 可以调用 `deep_research`。在开始一次付费、分钟级的研究任务前，交互式界面会询问本次查询使用 Deep Research 还是常规搜索。

结果会根据 Raven 的运行位置选择不同交付方式：

- **CLI 和 TUI：** Raven 会在搜索、阅读页面和分析时持续显示进度；完成后的报告会直接展示，不再由主模型改写。
- **Gateway 渠道：** 任务在后台继续运行，完成后的报告会发送回原始会话。
- **本地归档：** 每次完成的结果都会保存在 `<workspace>/deep_research/`，便于之后使用。

查询单个事实或 URL 时使用常规搜索；做方案对比、行业综述、技术调研，以及需要核对多个来源一致性的问题时使用 Deep Research。

## Tracing

Tracing 让 Raven 的推理路径可以被检查，同时不会把 trace 数据发送到托管服务。运行以下命令打开本地 dashboard：

```bash
raven tracing
```

每个 `session.turn` 都会成为一棵 trace tree，展示该轮之下发生的工作：

- LLM 调用、模型、token 使用量、成本、延迟和错误
- Tool 输入和输出
- Subagent 运行及其父子关系
- Skill 读取和注入
- Memory recall、存储、提取和 consolidation
- 以独立 artifact 保存的大型 prompts 和结果

Tracing 默认启用，并且不会中断 Raven 的控制流。Spans 保存在本地 `~/.raven/traces/logs/audit-spans.log`；可以通过 `RAVEN_TRACING_DIR` 移动状态目录，或设置 `RAVEN_TRACING=0` 关闭记录。

Schema 遵循一个精简、带版本的语义契约。Span 名称、属性、artifact 行为和扩展规则请参阅 [Tracing Standard API](docs/TRACING_STANDARD_API.md)。

## 核心系统

| 系统 | 能力 |
| --- | --- |
| **EverOS 记忆** | 跨 sessions 持久保存用户记忆、Agent 记忆和世界知识 |
| **Context Engine** | 通过明确的 token 预算和统一组装流程保留最有价值的上下文 |
| **Proactivity** | Sentinel observations、计划任务、nudge policy 和延迟决策 |
| **SkillForge** | 内置、workspace、EverOS 和镜像 skills，支持检索、反馈和进化 |
| **Evolver** | 用于改进 Agent 和可复用流程的可复现评测循环 |
| **Agent Templates** | 基于同一套 harness 构建专用 digital workers 的可分享起点 |

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## Providers 和 Gateways

Raven 支持 API key、OAuth、本地和 OpenAI-compatible providers。Onboarding catalog 包括 OpenRouter、OpenAI、Anthropic、Gemini、MiniMax、DeepSeek、Z.ai、DashScope、Moonshot、VolcEngine、SiliconFlow、Groq、AiHubMix、Azure OpenAI、GitHub Copilot OAuth、OpenAI Codex OAuth、Ollama 和托管 vLLM。

十二个 gateway adapters 可以把 Raven 接入 Telegram、Slack、Discord、WhatsApp、Matrix、Feishu、WeCom、Mochat、QQ、DingTalk、Email 和 WeChat。

```bash
raven channels list
raven channels enable <adapter>
raven gateway
```

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `raven` 或 `raven tui` | 启动终端 UI |
| `raven agent -m "..."` | 运行一次性任务 |
| `raven onboard` | 配置 providers、sandboxing、channels、memory、research 和 import |
| `raven status` | 查看配置和运行时状态 |
| `raven doctor` | 诊断 provider 和环境问题 |
| `raven tracing` | 打开本地 trace dashboard |
| `raven sessions list` | 浏览、恢复、fork、导出或删除 sessions |
| `raven skill list` | 查看本地 SkillForge catalog |
| `raven sentinel status` | 查看主动记忆和计划 nudges |
| `raven cron list` | 查看计划任务 |
| `raven gateway` | 运行消息 gateways |
| `raven upgrade` | 升级受管理的安装 |

运行 `raven --help` 或 `raven <command> --help` 查看完整 CLI。

## 文档

- [文档索引](docs/README.md)
- [开发工作流](docs/dev.md)
- [Tracing Standard API](docs/TRACING_STANDARD_API.md)
- [Sandbox 使用说明](docs/sandbox/usage.md)
- [Memory plugin 架构](docs/memory-plugin-architecture.md)
- [Self-evolution loop mapping](docs/specs/self-evolution-loop-raven-mapping.md)
- [Proactivity 实现](docs/Proactivity-Implementation.md)

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## 架构

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

Python runtime 和 React/Ink TUI 只通过 typed TUI-RPC 通信。Spine 传递 runtime events，Agent Loop 负责协调 providers、tools、context、memory、skills、subagents 和主动任务。

关键目录：

```text
raven/agent/             agent loop、tools 和 subagents
raven/channels/          messaging adapters
raven/context_engine/    context assembly 和 token budgeting
raven/memory_engine/     EverOS integration 和 local skill memory
raven/proactive_engine/  sentinel、scheduling 和 nudges
raven/providers/         model providers 和 routing
raven/skill_hub/         external skill retrieval
raven/tracing/           instrumentation、storage 和 viewer
raven/tui_rpc/           typed runtime-to-TUI boundary
ui-tui/                  React/Ink terminal interface
```

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## EverMind 生态

Raven 是 [EverMind](https://evermind.ai/) 开源生态的一部分。你可以继续了解 [EverOS](https://github.com/EverMind-AI/EverOS)、[EverAlgo](https://github.com/EverMind-AI/EverAlgo)、[HyperMem](https://github.com/EverMind-AI/HyperMem)、[EvoAgentBench](https://github.com/EverMind-AI/EvoAgentBench)、[EverMemBench](https://github.com/EverMind-AI/EverMemBench) 和 [EverMe](https://github.com/EverMind-AI/EverMe)。

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## 参与贡献

欢迎提交 issues 和 pull requests。请先阅读[开发工作流](docs/dev.md)，按照 [AGENTS.md](AGENTS.md) 中的仓库规则进行协作，并在 [GitHub Discussions](https://github.com/EverMind-AI/Raven/discussions) 讨论设计方案。

## 许可证

[Apache License 2.0](LICENSE)
