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
  <a href="https://github.com/user-attachments/assets/ed33ed0f-1f28-45cf-bbf5-7de49785def8"><img src="https://github.com/user-attachments/assets/ed33ed0f-1f28-45cf-bbf5-7de49785def8" alt="Raven unified surface and agent workflow" width="100%"></a>
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
  <a href="https://github.com/user-attachments/assets/103c49f8-f31d-4735-8bfd-84cae9baee46"><img src="https://github.com/user-attachments/assets/103c49f8-f31d-4735-8bfd-84cae9baee46" alt="Visual Design: Raven-Design, Claude Code, and Hermes on ArtifactsBench Dashboard, ArtifactsBench SVG, and GDPVal" width="95%"></a>
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

## ❯❯ 案例展示

真实运行记录，每条都取自 Raven 的任务图。上方展示了 Raven 为该任务生成的多智能体编排图，
下方是这次运行的产出。

<table>
<tr>
<td valign="top"><p align="center"><b>用 Godot 4 做的 FPS Boss 竞技场游戏</b></p></td>
</tr>
<tr>
<td valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/951e35bd-ff9c-4456-b539-32dd192827e1"><img src="https://github.com/user-attachments/assets/951e35bd-ff9c-4456-b539-32dd192827e1" alt="Task graph: three Raven-Code nodes running in sequence across 42 game rounds" width="100%"></a></p></td>
</tr>
<tr>
<td valign="top">

https://github.com/user-attachments/assets/44724e3e-6564-467a-b422-473aa8f474bd

</td>
</tr>
</table>

<table>
<tr>
<td width="50%" valign="top"><p align="center"><b>用二分法测出一根梁的极限载荷</b></p></td>
<td width="50%" valign="top"><p align="center"><b>一次溃坝模拟，调到水相不再越界</b></p></td>
</tr>
<tr>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/05fd70ce-85ca-4f8a-9891-d9085d3f4783"><img src="https://github.com/user-attachments/assets/05fd70ce-85ca-4f8a-9891-d9085d3f4783" alt="Task graph: Raven-Research into Raven-Code into Raven-Oncall, run in sequence for a CalculiX cantilever plastic limit chain, 3 of 3 done in 5m21s, 1m28s and 4m11s" width="100%"></a></p></td>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/050707b5-2af9-4f29-9d65-1564e96fef9d"><img src="https://github.com/user-attachments/assets/050707b5-2af9-4f29-9d65-1564e96fef9d" alt="Task graph: Raven-Research into Raven-Code into Raven-Oncall, run in sequence for a dam-break chain, 3 of 3 done in 11m48s, 1m16s and 3m58s" width="100%"></a></p></td>
</tr>
<tr>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/7d195366-8764-4788-84a6-22bf60cbb058"><img src="https://github.com/user-attachments/assets/7d195366-8764-4788-84a6-22bf60cbb058" alt="Limit-load search: a cantilever beam under rising load colored by von Mises stress, with the bisection bracket narrowing from 1800-2000 kN down to 3.125 kN over eight rounds" width="100%"></a></p></td>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/e814c6b9-f41f-4070-94cb-d1bc8ca9c51e"><img src="https://github.com/user-attachments/assets/e814c6b9-f41f-4070-94cb-d1bc8ca9c51e" alt="Dam-break solve: a collapsing water column resolved to fine free-surface structure, with the out-of-bounds water fraction falling from 1e0 to 1.36e-10 over seven rounds" width="100%"></a></p></td>
</tr>
</table>

<table>
<tr>
<td width="50%" valign="top"><p align="center"><b>宋代居家美学</b></p></td>
<td width="50%" valign="top"><p align="center"><b>古希腊如何被漂白</b></p></td>
</tr>
<tr>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/8fa50c96-3d2d-4107-8a8c-7d041d1ac8b4"><img src="https://github.com/user-attachments/assets/8fa50c96-3d2d-4107-8a8c-7d041d1ac8b4" alt="Task graph: three Raven-Research nodes running in parallel into a synthesis node, then one Raven-Design node" width="100%"></a></p></td>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/c8e9ac77-0377-487e-9d07-fb4fe51a0257"><img src="https://github.com/user-attachments/assets/c8e9ac77-0377-487e-9d07-fb4fe51a0257" alt="Task graph: three Raven-Research nodes running in parallel into a synthesis node, then one Raven-Design node" width="100%"></a></p></td>
</tr>
<tr>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/379328ea-e3ee-430f-865d-68ed90dae69f"><img src="https://github.com/user-attachments/assets/379328ea-e3ee-430f-865d-68ed90dae69f" alt="Cover, slides and closing slide of the Song-dynasty aesthetics deck" width="100%"></a></p></td>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/3c5a2a9a-d6d7-40ad-b3dc-70e9f5c095df"><img src="https://github.com/user-attachments/assets/3c5a2a9a-d6d7-40ad-b3dc-70e9f5c095df" alt="Cover, slides and closing slide of the Greek polychromy deck" width="100%"></a></p></td>
</tr>
</table>

<table>
<tr>
<td width="50%" valign="top"><p align="center"><b>流行音乐如何被制造出来</b></p></td>
<td width="50%" valign="top"><p align="center"><b>抽象艺术的一百年</b></p></td>
</tr>
<tr>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/032fdf6e-8b65-49a0-92ba-2ea3066a7451"><img src="https://github.com/user-attachments/assets/032fdf6e-8b65-49a0-92ba-2ea3066a7451" alt="Task graph: three Raven-Research nodes running in parallel into a synthesis node, then one Raven-Design node" width="100%"></a></p></td>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/4b606bae-667e-4f2f-ae7d-2880b47ddbf6"><img src="https://github.com/user-attachments/assets/4b606bae-667e-4f2f-ae7d-2880b47ddbf6" alt="Task graph: three Raven-Research nodes running in parallel into a synthesis node, then one Raven-Design node" width="98%"></a></p></td>
</tr>
<tr>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/9ab75945-81ba-4437-aec0-9ef6a633ed55"><img src="https://github.com/user-attachments/assets/9ab75945-81ba-4437-aec0-9ef6a633ed55" alt="Cover, slides and closing slide of the pop music deck" width="100%"></a></p></td>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/b3dfce6c-366d-4e31-9a57-7c98df054618"><img src="https://github.com/user-attachments/assets/b3dfce6c-366d-4e31-9a57-7c98df054618" alt="Cover, slides and closing slide of the abstract art deck" width="100%"></a></p></td>
</tr>
</table>

<table>
<tr>
<td width="50%" valign="top"><p align="center"><b>六个智能体编排框架横向对比</b></p></td>
<td width="50%" valign="top"><p align="center"><b>参数扫描：编写、运行、绘图</b></p></td>
</tr>
<tr>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/92ff2c83-3b0f-49eb-9995-c1ad4c298452"><img src="https://github.com/user-attachments/assets/92ff2c83-3b0f-49eb-9995-c1ad4c298452" alt="Task graph: two Raven-Research nodes running in parallel into one Raven-Design node" width="100%"></a></p></td>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/e842677a-8b71-4f1b-ae02-0162a440a1e8"><img src="https://github.com/user-attachments/assets/e842677a-8b71-4f1b-ae02-0162a440a1e8" alt="Task graph: Raven-Code into Raven-Oncall into Raven-Design, run in sequence" width="100%"></a></p></td>
</tr>
<tr>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/5120c2c0-9aeb-4224-ac85-df9dc188b5cc"><img src="https://github.com/user-attachments/assets/5120c2c0-9aeb-4224-ac85-df9dc188b5cc" alt="Comparison board: six orchestration frameworks against four dimensions, each tagged explicit graph or canvas, declared task flow, or dynamic at runtime" width="100%"></a></p></td>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/10679855-e2c5-4422-81c3-17cd784123ce"><img src="https://github.com/user-attachments/assets/10679855-e2c5-4422-81c3-17cd784123ce" alt="Retrieval sweep dashboard: recall@k is set by top_k alone and latency stays broadly flat, with a sixteen-cell grid of measured recall and latency and the best cell ringed at top_k 10, chunk_size 1024" width="99%"></a></p></td>
</tr>
</table>

<table>
<tr>
<td width="50%" valign="top"><p align="center"><b>光污染如何偷走野生动物的睡眠</b></p></td>
<td width="50%" valign="top"><p align="center"><b>GPS 为什么需要四颗卫星</b></p></td>
</tr>
<tr>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/d3e4c873-cf95-4011-8471-0fda72778cfd"><img src="https://github.com/user-attachments/assets/d3e4c873-cf95-4011-8471-0fda72778cfd" alt="Task graph: three Raven-Research nodes in parallel into a Raven-Code node; that node and a Raven-Design plate node running alongside them both feed the final Raven-Design node" width="83%"></a></p></td>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/9754976b-11ff-4738-b53d-0621ec6ae7da"><img src="https://github.com/user-attachments/assets/9754976b-11ff-4738-b53d-0621ec6ae7da" alt="Task graph: two Raven-Code nodes in parallel into a Raven-Oncall cross-check; that node and a Raven-Design plate node running alongside them both feed the final Raven-Design node" width="100%"></a></p></td>
</tr>
<tr>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/7767ac05-6785-4e6a-9e27-c1b123753766"><img src="https://github.com/user-attachments/assets/7767ac05-6785-4e6a-9e27-c1b123753766" alt="Key visual plus the derived series: street poster, data panel, social square and wide banner" width="100%"></a></p></td>
<td width="50%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/25c57220-c5fc-4f72-94fb-ea4b7e83f429"><img src="https://github.com/user-attachments/assets/25c57220-c5fc-4f72-94fb-ea4b7e83f429" alt="GPS trilateration explainer: the live page, and the fix at two, three and four satellites" width="97%"></a></p></td>
</tr>
</table>

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

<p align="center">
  <a href="https://github.com/user-attachments/assets/28248163-3607-408f-86cf-a1acb621767b"><img src="https://github.com/user-attachments/assets/28248163-3607-408f-86cf-a1acb621767b" alt="Raven WebUI new task page" width="90%"></a>
</p>

<p align="center"><em>新建任务：一个输入框，技能、剧本、知识库与记忆都在一侧。</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/76bf13ac-763b-4120-b323-0f694c237365"><img src="https://github.com/user-attachments/assets/76bf13ac-763b-4120-b323-0f694c237365" alt="Raven WebUI subagents page" width="90%"></a>
</p>

<p align="center"><em>子智能体：内置与第三方 Agent 汇总在同一张花名册里。</em></p>

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
