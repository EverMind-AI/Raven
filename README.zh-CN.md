<div align="center" id="readme-top">

![Raven banner](https://github.com/user-attachments/assets/d56804e5-5d4b-4493-bc70-71bd38833806)

<p align="center"><strong>下一版本方向：</strong>The Harness of Harnesses 是 Raven 下一版本的发展方向，并非当前公开版本已经支持的能力。</p>

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

当前公开版本的 Raven 是一套已经可以运行的开源 **自我进化 Agent Harness**。它将终端执行、本地 Tracing、长期记忆、Skills、评测与可复用工作流整合进同一套系统，面向长程 AI 任务持续学习与改进。

## 下一版本方向：The Harness of Harnesses

随着 AI Agent 从单一任务走向长程、多领域协作，依赖人工设计一个不断膨胀的 harness 已经难以持续扩展；而与特定模型和领域深度绑定的单一 harness，也无法覆盖通用智能所需的全部能力。

Raven 的下一版本将走向 **The Harness of Harnesses**：一个持续进化、自主协作、开放共建的 Multi-Agent 生态。它将面向特定模型与领域构建和提升 Agent Harness，并把不同 Harness 的异构执行能力汇聚为统一的 **全领域协作网络（All-Domain Collaboration Network）**。

| **可信** | **可延续** | **可进化** |
| --- | --- | --- |
| Harness 能力将基于真实、可验证的表现进行评分，而非由自我声明决定。 | 这一网络将让经过验证的结果、任务状态与长期记忆跨执行者延续。 | 每次经过验证的执行都将把经验反馈到能力档案、Skills、调度与整个协作网络。 |

下一版本的架构将不再把模型与 harness 视为静态组合，而是通过持续的 **评测 → 执行 → 验证 → 记忆 → 反馈** 闭环，发现、编排并优化每项任务所需的能力。经过验证的工作将沉淀为可复用经验，使 Agent 个体与更广泛的能力网络共同进化。

这一方向背后的内部研究原型已在 **22 个 Agent 基准任务**上完成评测，覆盖任务性能、成本与关键机制收益。报告结果显示，其在性能与效率上相较现有 Agent 系统实现了全面提升，并进一步推进了 **质量—成本帕累托前沿**。

> 当前公开版本的 Raven 尚未实现 The Harness of Harnesses。今天的 Raven 是本仓库中可运行的自我进化 Agent Harness；上述内容描述的是我们正在构建的下一版本方向。

> Raven 目前处于 pre-alpha 阶段，接口和配置可能快速变化。

## 下一版本研究评测

| 基准测试 | 研究原型结果 | 对比 |
| --- | --- | --- |
| 效率 | 27B 下为 `56.7%`；397B 下为 `58.1%` | Hermes 为 `46.8%` / `47.9%`；27B 下领先 `+9.9pp` |
| 自我进化 | EvoAgentBench 排名 `#1` | 在四种方法中领先下一名 `+6.2pp` |
| 主动性 | ProAgentBench F1 为 `0.60` | 是 Hermes/OpenClaw `0.253` 的 `2.4x` |

以上结果来自内部研究原型，并不代表当前公开版本已经支持 The Harness of Harnesses 协作网络。模型、任务集和评测协议都会影响最终结果。

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

Raven 支持 API key、OAuth、本地和 OpenAI-compatible providers。Onboarding catalog 包括 OpenRouter、OrcaRouter、OpenAI、Anthropic、Gemini、MiniMax、DeepSeek、Z.ai、DashScope、Moonshot、VolcEngine、SiliconFlow、Groq、AiHubMix、Azure OpenAI、GitHub Copilot OAuth、OpenAI Codex OAuth、Ollama 和托管 vLLM。

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

EverMind 将记忆研究、可直接使用的产品与实际集成连接为一个开源生态。

<table>
<tr>
<th colspan="2">产品</th>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverOS">EverOS</a></strong></td>
<td>本地优先、Markdown 原生的 Agent 与用户长期记忆运行时。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/Raven">Raven</a></strong></td>
<td>以记忆为核心的自进化 Agent Harness，具备主动性、上下文控制与 Skill 进化能力。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverMe">EverMe（CLI）</a></strong></td>
<td>面向跨设备、跨 Agent 个人记忆的 CLI 与 Agent 插件套件。</td>
</tr>
<tr>
<th colspan="2">研究与评测</th>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/SkillCorpus">SkillCorpus</a></strong></td>
<td>将分散的 Agent Skill 整理为可检索语料库，并提供检索与评测工具。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverAlgo">EverAlgo</a></strong></td>
<td>为 EverOS 提供无状态的提取、排序、解析与记忆算法。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/HyperMem">HyperMem</a></strong></td>
<td>基于超图的分层记忆架构，用于由粗到细的长期对话检索。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/MSA">MSA</a></strong></td>
<td>面向可扩展潜在记忆与一亿 Token 上下文的 Memory Sparse Attention。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverMemBench">EverMemBench</a></strong></td>
<td>从事实召回、应用推理和个性化泛化三个层面评测记忆系统。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EvoAgentBench">EvoAgentBench</a></strong></td>
<td>纵向评测 Agent 自进化、迁移效率、错误规避和 Skill 使用能力。</td>
</tr>
<tr>
<th colspan="2"><a href="https://github.com/EverMind-AI/plugins">插件与集成</a></th>
</tr>
<tr>
<td><strong><a href="https://docs.openclaw.ai">OpenClaw</a></strong></td>
<td><a href="https://github.com/EverMind-AI/plugins/tree/main/openclaw">OpenClaw 插件</a>，自动管理召回、写入与会话记忆生命周期。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/NousResearch/hermes-agent">Hermes Agent</a></strong></td>
<td><a href="https://github.com/EverMind-AI/plugins/tree/main/hermes">Hermes 插件</a>，为 Hermes 会话提供持久记忆。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/deepseek-ai/DeepSeek-Harness">DeepSeek Harness</a></strong></td>
<td><a href="https://github.com/EverMind-AI/plugins/tree/main/dsh">DSH 插件</a>，让 DeepSeek Harness Agent 使用长期记忆。</td>
</tr>
<tr>
<td><strong><a href="https://dify.ai">Dify</a></strong></td>
<td><a href="https://github.com/EverMind-AI/plugins/tree/main/dify">本地版</a>与<a href="https://github.com/EverMind-AI/plugins/tree/main/dify_cloud">云端版</a>工具，在工作流和 Agent 中显式搜索与写入记忆。</td>
</tr>
</table>

这些项目共同构成 EverMind 从研究到运行时的完整链路：将方法与评测转化为
可复用的记忆基础设施、产品和 Agent 集成。

## 参与贡献

欢迎提交 issues 和 pull requests。请先阅读[开发工作流](docs/dev.md)，按照 [AGENTS.md](AGENTS.md) 中的仓库规则进行协作，并在 [GitHub Discussions](https://github.com/EverMind-AI/Raven/discussions) 讨论设计方案。

## 许可证

[Apache License 2.0](LICENSE)
