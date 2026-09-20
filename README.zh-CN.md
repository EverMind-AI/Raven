<div align="center" id="readme-top">

![Raven banner](https://github.com/user-attachments/assets/ff05474a-03f5-4ec2-b42b-55f1508ede06?raw=true)

<p align="center">
  <a href="https://x.com/evermind"><img src="https://img.shields.io/badge/EverMind-000000?labelColor=gray&style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://huggingface.co/EverMind-AI"><img src="https://img.shields.io/badge/HuggingFace-EverMind-F5C842?labelColor=gray&style=for-the-badge&logo=huggingface&logoColor=white" alt="Hugging Face"></a>
  <a href="https://discord.gg/gYep5nQRZJ"><img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdiscord.com%2Fapi%2Fv10%2Finvites%2FgYep5nQRZJ%3Fwith_counts%3Dtrue&query=%24.approximate_presence_count&suffix=%20online&label=Discord&color=404EED&labelColor=gray&style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/EverMind-AI/EverOS/discussions/67"><img src="https://img.shields.io/badge/WeCom-EverMind_Community-07C160?labelColor=gray&style=for-the-badge&logo=wechat&logoColor=white" alt="WeCom"></a>
</p>

[官网](https://raven.evermind.ai) · [文档](https://evermind-ai.github.io/Raven/zh/) · [English](README.md)

</div>

<br>

# Raven 是什么

<p align="center">
  <a href="https://github.com/user-attachments/assets/ef64cd3b-a48f-4516-9a30-47fe47a750be"><img src="https://github.com/user-attachments/assets/ef64cd3b-a48f-4516-9a30-47fe47a750be" alt="Raven unified surface and agent workflow" width="100%"></a>
</p>

<p align="center"><em>一个入口，全领域 Agent 协同：Raven 生成并编排任务 DAG，驱动多个专业 Agent 协作完成复杂任务。</em></p>

Raven 是 **The Harness of Harnesses**——一个持续自我演进的多 Agent 编排生态。作为 **Host Agent（宿主 Agent）**，它通过统一入口连接专业 Agent，负责任务委派、执行协调和结果整合。Raven 的长期目标是将这种编排能力扩展到不同设备、环境和领域。

基于 EverMind 的自进化 harness 引擎，并由 [EverOS](https://github.com/EverMind-AI/EverOS) 提供支持，Raven 跨会话保存上下文，持续改进 Agent harness 与协作工作流。

**内置 Agent：** **Raven-Research**、**Raven-Code**、**Raven-Design** 和 **Raven-Oncall** 支持研究、编程、视觉设计和无人值守流程自动化。

> Raven 目前处于 pre-alpha 阶段，接口和配置可能快速变化。

<p align="center">
  <a href="https://github.com/user-attachments/assets/e333694a-0f4c-4f27-8bfe-8120ff5339a0"><img src="https://github.com/user-attachments/assets/e333694a-0f4c-4f27-8bfe-8120ff5339a0" alt="Multi-Agent Orchestration Benchmark: Node F1, Edge F1, Partial Order Accuracy, and Exact Match Rate" width="100%"></a>
</p>

<p align="center"><em>Raven 在多 Agent 编排基准测试上的表现</em></p>

## ❯❯ 内置 Agent

Raven 的模块化架构面向 harness 自进化和子 Agent 创建。四个内置 Agent 在各自领域提供**先进水平（SOTA）的性能**，结合可复用的 harness 组件、领域专用工具、技能和 Agent 循环。Raven 可以将聚焦任务委派给单个 Agent，也可以在共享工作流中编排多个 Agent。它们共享的 harness 由 **Raven Evolver** 持续改进，这是一个独立工具，将 Raven 作为库调用，并基于基准测试评估候选的 harness 变更；它用于开发这些 Agent，而不是运行在它们内部。

> 四个 Agent 均已内置，开箱即可进行编排。

### ❯ Raven-Research

**Raven-Research** 为复杂问题、文献综述与技术分析提供**自主深度研究**能力。它交付清晰、结构化且来源可追溯的研究报告，帮助用户理解陌生领域、比较不同方案，并作出有依据的决策。

<p align="center">
  <a href="https://github.com/user-attachments/assets/d072a514-58fd-41c7-8fcd-40782ebb4c80"><img src="https://github.com/user-attachments/assets/d072a514-58fd-41c7-8fcd-40782ebb4c80" alt="DeepResearch Mixed: Accuracy, Input Tokens, Output Tokens, and Cost" width="100%"></a>
</p>

<p align="center"><em>Raven-Research 在 DeepResearch Mixed 基准测试中的表现</em></p>

### ❯ Raven-Code

**Raven-Code** 支持**智能体驱动的软件开发**，将需求转化为可运行、经过测试的代码。它支持功能实现、调试、重构、数据处理与数据分析，帮助用户在遵循项目规范的前提下构建新能力、解决问题并提升代码质量。

<p align="center">
  <a href="https://github.com/user-attachments/assets/a516ebc2-f0b4-47d4-b970-7dfd492adc0e"><img src="https://github.com/user-attachments/assets/a516ebc2-f0b4-47d4-b970-7dfd492adc0e" alt="Coding Benchmarks: SWE-bench Pro, SWE-bench Verified, WorkBuddy-Code Reward, and SWE-Refactor" width="95%"></a>
</p>

<p align="center"><em>Raven-Code 在编程基准测试中的表现</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/75f60aa2-3e2f-42fb-aa51-aa111bf078d0"><img src="https://github.com/user-attachments/assets/75f60aa2-3e2f-42fb-aa51-aa111bf078d0" alt="DataAgentBench (2026-08-24 Live): Raven-Code with Opus-5 achieves 0.8762 Pass@1" width="95%"></a>
</p>

<p align="center"><em>Raven-Code 在数据分析任务的 DataAgentBench 上排名第一（2026-08-24 Live）</em></p>

### ❯ Raven-Design

**Raven-Design** 提供**视觉设计**能力，将想法与内容转化为精美的视觉作品。它支持 PowerPoint 幻灯片、品牌素材、图表、示意图和网页界面创作，通过持续优化布局、字体与视觉一致性，帮助用户清晰表达信息，将创意变为作品。

<p align="center">
  <a href="https://github.com/user-attachments/assets/c42ff722-8aab-40ec-82e3-0655a859a9d9"><img src="https://github.com/user-attachments/assets/c42ff722-8aab-40ec-82e3-0655a859a9d9" alt="PresentBench: Raven-Design, Claude Code, and public leaderboard scores" width="95%"></a>
</p>

<p align="center"><em>Raven-Design 在 PresentBench 幻灯片生成测试中排名第一</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/061b5818-595b-4392-b9b6-53a1effb8347"><img src="https://github.com/user-attachments/assets/061b5818-595b-4392-b9b6-53a1effb8347" alt="Visual Design: Raven-Design, Claude Code, and Hermes on ArtifactsBench Dashboard, ArtifactsBench SVG, and GDPVal" width="95%"></a>
</p>

<p align="center"><em>Raven-Design 在视觉设计基准测试中的表现</em></p>

### ❯ Raven-Oncall

**Raven-Oncall** 为实验、优化与持续监控提供**无人值守的流程自动化**能力。它自主推进从启动到完成的整个工作流程，支持持续数小时乃至通宵运行并交付结果，仅在需要人工判断时请用户介入。

<p align="center">
  <a href="https://github.com/user-attachments/assets/bffd0fa2-e750-4f09-b74a-09452765a99d"><img src="https://github.com/user-attachments/assets/bffd0fa2-e750-4f09-b74a-09452765a99d" alt="AI4AI (Nanochat 50M Pretraining): Bits Per Byte (BPB), Runtime, Tokens, and Cost" width="95%"></a>
</p>

<p align="center"><em>在 AI4AI 任务上，Raven-Oncall 的性能与成本均显著优于 Claude Code</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/aafe30ca-6693-4d30-956e-73debf3ef2c9"><img src="https://github.com/user-attachments/assets/aafe30ca-6693-4d30-956e-73debf3ef2c9" alt="AI4S Internal Benchmark: Success Rate, Average Total Runtime, Average Total Tokens, and Average Cost" width="95%"></a>
</p>

<p align="center"><em>在 AI4S 任务上，Raven-Oncall 的成功率与成本均显著优于 Claude Code</em></p>

## ❯❯ 连接第三方 Agent

Raven 可以通过 ACP、CLI 或兼容 OpenAI 的 API 连接并编排 Agent，并提供 13 个**第三方 Agent**的预设，简化设置、任务委派和共享工作流中的协作。

<p align="center">
  <img src="https://github.com/user-attachments/assets/3370c883-00ac-4471-97ef-f4312df77202" width="80%" alt="Third-party agents: Claude Code, Codex, OpenCode, Hermes Agent, OpenClaw, MiroThinker, GitHub Copilot, Qwen Code, CodeBuddy, Qoder, Grok Build, Kimi Code, and Pi">
</p>

## ❯❯ 快速开始

### 📦 安装

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

也可以从源码检出安装，适合基于代码做开发，或运行尚未发布的版本：

```bash
git clone https://github.com/EverMind-AI/Raven.git
cd Raven
./install.sh
```

以文件方式运行时，`install.sh` 会以可编辑（editable）模式安装该检出目录：Raven 及其内置插件都链接回你的工作树，TUI 包和内置页面也从该目录构建。通过管道运行时，即使身处克隆仓库中也始终安装已发布的 wheel，这样一行命令的安装就不会使用工作树中的任意内容。如需在管道方式下强制使用可编辑安装，请设置 `RAVEN_LOCAL_SRC=<dir>`。

这些 Agent 随 Raven 一同发布：wheel 包包含 `agents/` 产品目录，首次使用时会将其复制到 Raven 主目录；源码安装则直接读取仓库中的目录。配置向导会逐一询问是否启用，并为所选 Agent 注册其适配的模型，或使用当前 Raven 的 LLM。详见 [`agents/README.md`](agents/README.md)。

首次运行之后的内容都在文档站：自托管、Docker 部署、WebUI、命令参考、运行时架构与仓库布局，中英文对照。

**[阅读文档](https://evermind-ai.github.io/Raven/zh/)**

## ❯❯ 核心系统

| 系统 | 能力 |
| --- | --- |
| **Agent 编排** | 协调 Agent，管理任务依赖和并行执行，将多步骤协作沉淀为可复用的工作流。 |
| **Evolver** | 通过失败诊断、候选改进测试和可复现评估，保留优于基线的变更，推动 harness 自进化。 |
| **EverOS 记忆** | 跨会话保留用户上下文、Agent 经验和世界知识，为后续任务召回相关记忆与可复用技能。 |
| **SkillForge** | 从本地技能库、EverOS 记忆以及 [SkillHub 的 **114,190 项技能**](https://github.com/EverMind-AI/SkillCorpus#public-artifacts)中检索相关技能，按需为 Agent 提供专业能力。 |
| **主动行为** | 结合事件监测与定时执行，预测用户需求，及时发出提醒并发起后续工作。 |

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>


## ❯❯ 启动 WebUI

Raven 的 WebUI 将对话、多 Agent 协作和工作区管理整合到浏览器中。你可以在同一界面与 Agent 对话、跟踪任务进度、查看文件和输出，以及浏览记忆与技能。

```bash
raven web
```

该命令会在浏览器中打开 WebUI，并让 Raven 在后台持续运行。使用 `raven web --stop` 停止后台服务。

> **截图占位 1：** 对话与工作区。

> **截图占位 2：** Agent 协作与任务图。

> **截图占位 3：** 记忆与技能管理。

## ❯❯ EverMind 生态

<p align="center">
  <a href="https://github.com/user-attachments/assets/6c4392ea-2f82-42c7-acde-8dfecb2e6c2c"><img src="https://github.com/user-attachments/assets/6c4392ea-2f82-42c7-acde-8dfecb2e6c2c" alt="The EverMind ecosystem: the EverMind mark and its slogan on an orbital field" width="100%"></a>
</p>

[EverMind](https://evermind.ai/) 将记忆研究、可用于生产环境的产品与实际集成连接为一个开源生态。

<table>
<tr>
<th colspan="2">产品</th>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverOS">EverOS</a></strong></td>
<td>优先本地运行、以 Markdown 为原生格式的长期记忆运行时，面向 Agent 与用户。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/Raven">Raven</a></strong></td>
<td>以记忆为核心、能够自我改进的 Agent harness，支持主动行为、上下文控制和技能演化。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverMe">EverMe (CLI)</a></strong></td>
<td>用于跨设备、跨 Agent 个人记忆的 CLI 与 Agent 插件套件。</td>
</tr>
<tr>
<th colspan="2">研究与评估</th>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/SkillCorpus">SkillCorpus</a></strong></td>
<td>经过整理、可用于检索的 Agent 技能语料库，配套检索与评估工具。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverAlgo">EverAlgo</a></strong></td>
<td>为 EverOS 提供无状态的提取、排序、解析和记忆算子。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/HyperMem">HyperMem</a></strong></td>
<td>基于超图的分层记忆，支持从粗到细检索长期对话内容。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/MSA">MSA</a></strong></td>
<td>Memory Sparse Attention，支持可扩展的潜在记忆与 100M Token 上下文。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverMemBench">EverMemBench</a></strong></td>
<td>评估记忆系统的事实召回、应用推理和个性化泛化能力。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EvoAgentBench">EvoAgentBench</a></strong></td>
<td>对 Agent 自进化、迁移效率、错误规避和技能使用进行纵向评估。</td>
</tr>
<tr>
<th colspan="2"><a href="https://github.com/EverMind-AI/plugins">集成</a></th>
</tr>
<tr>
<td><strong><a href="https://docs.openclaw.ai">OpenClaw</a></strong></td>
<td><a href="https://github.com/EverMind-AI/plugins/tree/main/openclaw">OpenClaw 插件</a>，用于自动召回、记忆采集和会话记忆生命周期管理。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/NousResearch/hermes-agent">Hermes Agent</a></strong></td>
<td><a href="https://github.com/EverMind-AI/plugins/tree/main/hermes">Hermes 插件</a>，为 Hermes 提供跨会话的持久化记忆。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/deepseek-ai/DeepSeek-Harness">DeepSeek Harness</a></strong></td>
<td><a href="https://github.com/EverMind-AI/plugins/tree/main/dsh">DSH 插件</a>，为 DeepSeek Harness Agent 提供记忆能力。</td>
</tr>
<tr>
<td><strong><a href="https://dify.ai">Dify</a></strong></td>
<td>提供<a href="https://github.com/EverMind-AI/plugins/tree/main/dify">自托管</a>与<a href="https://github.com/EverMind-AI/plugins/tree/main/dify_cloud">云端</a>工具，在工作流和 Agent 中显式搜索与存储记忆。</td>
</tr>
</table>

这些项目共同构成 EverMind 从研究到运行时的技术体系，将方法与基准转化为可复用的记忆基础设施、产品和 Agent 集成。

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## ❯❯ 参与贡献

欢迎提交 issue 和 pull request。请先阅读[开发工作流](docs/dev.md)，遵循 [AGENTS.md](AGENTS.md) 中的仓库规则，并在 [GitHub Discussions](https://github.com/EverMind-AI/Raven/discussions) 中讨论设计方案。

## ❯❯ 许可证

[Apache 许可证 2.0](LICENSE)
