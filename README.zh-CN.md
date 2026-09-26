<div align="center" id="readme-top">

![Raven banner](https://github.com/user-attachments/assets/ae944083-9c61-4218-8372-2c3c0d483d4b?raw=true)

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

<p align="center"><em>一个入口，连接所有 Agent：Raven 为复杂任务生成 DAG，并编排多个专业 Agent。</em></p>

Raven 是 **The Harness of Harnesses**——一个自我演进的多 Agent 协作生态。作为 **Host Agent（宿主 Agent）**，它通过统一入口汇聚各类专业 Agent，负责委派任务、协调执行并整合结果。长远来看，Raven 希望把这种编排能力延伸到不同的设备、环境与领域。

Raven 构建于 EverMind 的自进化 harness 引擎之上，由 [EverOS](https://github.com/EverMind-AI/EverOS) 驱动，跨会话保留记忆，并持续改进 Agent harness 与协作工作流。

**内置 Agent：** **Raven-Research**、**Raven-Code**、**Raven-Design** 与 **Raven-Oncall**，分别覆盖研究、编程、视觉设计与无人值守的流程自动化。

> Raven 目前处于 pre-alpha 阶段，接口与配置可能会快速变化。

<p align="center">
  <a href="https://github.com/user-attachments/assets/1756ab94-706d-4c07-b8b3-3ef6dd8a147f"><img src="https://github.com/user-attachments/assets/1756ab94-706d-4c07-b8b3-3ef6dd8a147f" alt="Multi-Agent Orchestration Benchmark: Node F1, Edge F1, Partial Order Accuracy, and Exact Match Rate" width="100%"></a>
</p>

<p align="center"><em>Raven 在多 Agent 编排基准测试上的表现</em></p>

## ❯❯ 内置 Agent

Raven 采用模块化架构，专为 harness 自进化与子 Agent 创建而设计。四个内置 Agent 将可复用的 harness 组件与各领域专用的工具、技能和 Agent 循环相结合，在各自领域均达到**业界领先（SOTA）水平**。Raven 既可以把单一任务委派给某个 Agent，也可以在同一工作流中编排多个 Agent 协作。这些 Agent 共用的 harness 由 **Raven Evolver** 改进。Evolver 是一个独立工具，以库的形式调用 Raven，并在基准测试上评估候选的 harness 改动；它服务于 Agent 的研发过程，并不运行在 Agent 内部。

> 四个 Agent 均已内置，开箱即可参与编排。

### ❯ Raven-Research

**Raven-Research** 为复杂问题、文献综述与技术分析提供**自主深度研究**能力。它输出清晰、结构化且来源可追溯的研究报告，帮助用户了解陌生领域、比较不同方案，做出有据可依的决策。

<p align="center">
  <a href="https://github.com/user-attachments/assets/9dbc1aaa-3477-4871-b3fd-a42405c6ef02"><img src="https://github.com/user-attachments/assets/9dbc1aaa-3477-4871-b3fd-a42405c6ef02" alt="DeepResearch Mixed: Accuracy, Input Tokens, Output Tokens, and Cost" width="100%"></a>
</p>

<p align="center"><em>Raven-Research 在 DeepResearch Mixed 基准测试中的表现</em></p>

### ❯ Raven-Code

**Raven-Code** 提供 **Agent 驱动的软件开发**能力，把需求转化为可运行、经过测试的代码。它覆盖功能实现、调试、重构、数据处理与数据分析，在遵循项目既有规范的前提下，帮助用户开发新功能、修复问题并提升代码质量。

<p align="center">
  <a href="https://github.com/user-attachments/assets/a516ebc2-f0b4-47d4-b970-7dfd492adc0e"><img src="https://github.com/user-attachments/assets/a516ebc2-f0b4-47d4-b970-7dfd492adc0e" alt="Coding Benchmarks: SWE-bench Pro, SWE-bench Verified, WorkBuddy-Code Reward, and SWE-Refactor" width="95%"></a>
</p>

<p align="center"><em>Raven-Code 在编程基准测试中的表现</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/75f60aa2-3e2f-42fb-aa51-aa111bf078d0"><img src="https://github.com/user-attachments/assets/75f60aa2-3e2f-42fb-aa51-aa111bf078d0" alt="DataAgentBench (2026-08-24 Live): Raven-Code with Opus-5 achieves 0.8762 Pass@1" width="95%"></a>
</p>

<p align="center"><em>Raven-Code 在数据分析任务的 DataAgentBench 上排名第一（2026-08-24 Live）</em></p>

### ❯ Raven-Design

**Raven-Design** 提供**视觉设计**能力，把想法与内容转化为精美的视觉作品。它可以制作 PowerPoint 演示文稿、品牌素材、图表、示意图和网页界面，并优化布局、字体与视觉一致性，帮助用户清晰传达信息、让创意落地。

<p align="center">
  <a href="https://github.com/user-attachments/assets/c42ff722-8aab-40ec-82e3-0655a859a9d9"><img src="https://github.com/user-attachments/assets/c42ff722-8aab-40ec-82e3-0655a859a9d9" alt="PresentBench: Raven-Design, Claude Code, and public leaderboard scores" width="95%"></a>
</p>

<p align="center"><em>Raven-Design 在 PresentBench 幻灯片生成测试中排名第一</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/103c49f8-f31d-4735-8bfd-84cae9baee46"><img src="https://github.com/user-attachments/assets/103c49f8-f31d-4735-8bfd-84cae9baee46" alt="Visual Design: Raven-Design, Claude Code, and Hermes on ArtifactsBench Dashboard, ArtifactsBench SVG, and GDPVal" width="95%"></a>
</p>

<p align="center"><em>Raven-Design 在视觉设计基准测试中的表现</em></p>

### ❯ Raven-Oncall

**Raven-Oncall** 为实验、调优与持续监控提供**无人值守的流程自动化**能力。它自主推进工作流从启动到完成的全过程，可连续运行数小时乃至通宵并交付结果，只在需要人工判断时才请用户介入。

<p align="center">
  <a href="https://github.com/user-attachments/assets/bffd0fa2-e750-4f09-b74a-09452765a99d"><img src="https://github.com/user-attachments/assets/bffd0fa2-e750-4f09-b74a-09452765a99d" alt="AI4AI (Nanochat 50M Pretraining): Bits Per Byte (BPB), Runtime, Tokens, and Cost" width="95%"></a>
</p>

<p align="center"><em>在 AI4AI 任务上，Raven-Oncall 的质量与成本均显著优于 Claude Code</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/aafe30ca-6693-4d30-956e-73debf3ef2c9"><img src="https://github.com/user-attachments/assets/aafe30ca-6693-4d30-956e-73debf3ef2c9" alt="AI4S Internal Benchmark: Success Rate, Average Total Runtime, Average Total Tokens, and Average Cost" width="95%"></a>
</p>

<p align="center"><em>在 AI4S 任务上，Raven-Oncall 的成功率与成本均显著优于 Claude Code</em></p>

## ❯❯ 运行时自进化

**Raven 从架构设计之初就支持运行时自进化。** 它将 Agent 的运行循环拆分为四个相互解耦的策略模块——**Memory** 决定本轮能看到什么，**Planning** 决定如何着手，**Capability** 决定本轮开放哪些工具，**Action** 决定做什么并在执行前加以判断。**Curator** 正是在这四个位置上持续改写：可以是一项配置变更，也可以是为该 Agent 编写的一段判断逻辑。改动仅作用于对应的 Agent，装配完成后即按新的实现运行。

**Curator 改写的不止是提示词。** 它所使用的工具与外部服务、所遵循的技能与流程、在每个环节上的判断，都可以被替换。改写按轮次进行：你使用它完成工作，指出不足，它据此改写后再来一轮，直到你认可、它判断已无可改之处，或轮次预算用尽。质量由两点保障：装配前必须通过校验，未通过则返工；常规轮次的信号在交给它之前会剥掉参考答案，以减少答案的直接暴露。Curator 目前仍是实验性的：它随仓库提供，不包含在安装包中。

**数字人是它构建的第一个成果。** 只需描述你想要什么样的助手，Curator 就会为你构建：由一个主角色负责与你沟通，再从已有的 Agent 中选择合适的成员分工协作。你的需求会完整落实到助手的 **harness** 中，成为具体的职责分工、工具使用范围和行动前的检查规则。

> 描述你的需求，Raven 会为你创建助手，并在使用中持续改进。之后，只需一句话，就能让它再次为你工作。

## ❯❯ 案例展示

以下是三个由 Raven 驱动多智能体团队完成的完整项目案例。每个项目都从需求或目标出发，经由团队协作完成全过程，并交付下方展示的整套成果。

### ❯ THRESHOLD 阈限：一个完整的游戏开发项目

**需求文档由人编写，整个项目由 Raven 完成。** Raven 自主运行约 4 天，历经 42 轮规划、开发与验收，用 Godot 4 开发出一款以竞技场 Boss 战为核心玩法的第一人称射击游戏。项目最终交付可玩的游戏、游戏海报、演示文稿和官网，全部由 Raven 制作。

<table>
<tr>
<td colspan="2" valign="top"><p align="center"><b>实机视频</b></p></td>
<td width="22.8%" valign="top"><p align="center"><b><a href="https://livxue.github.io/threshold/">官网 ↗</a></b></p></td>
</tr>
<tr>
<td colspan="2" valign="top">

https://github.com/user-attachments/assets/44724e3e-6564-467a-b422-473aa8f474bd

</td>
<td rowspan="3" width="22.8%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/bf8f62c3-a78d-46ae-b964-94e6fea91435"><img src="https://github.com/user-attachments/assets/bf8f62c3-a78d-46ae-b964-94e6fea91435" alt="The Chinese THRESHOLD website as one long capture: hero, fight, kill cam, attack tells, evolution, making-of and footer" width="100%"></a></p></td>
</tr>
<tr>
<td width="45.8%" valign="top"><p align="center"><b>海报</b></p></td>
<td width="31.4%" valign="top"><p align="center"><b>演示文稿</b> · <b><a href="https://github.com/LivXue/Raven/releases/download/showcase-decks-2026-09-25/Raven-Game-0925.pptx">PPTX ↓</a></b></p></td>
</tr>
<tr>
<td width="45.8%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/edc9e0df-1a76-4302-8c6d-be73e088d65d"><img src="https://github.com/user-attachments/assets/edc9e0df-1a76-4302-8c6d-be73e088d65d" alt="THRESHOLD poster with its Chinese title: the Warden towers over the player on a molten arena floor" width="100%"></a></p></td>
<td width="31.4%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/3852c187-3412-4501-9b62-809a3dfd0b81"><img src="https://github.com/user-attachments/assets/3852c187-3412-4501-9b62-809a3dfd0b81" alt="Cover, slides and closing slide of the THRESHOLD deck" width="100%"></a></p></td>
</tr>
</table>

### ❯ Raven RSI：一个完整的递归自我改进项目

**让 AI 改进 AI，整个项目由 Raven 完成。** 给定任务目标与不可修改的评估标准，Raven RSI 会自主制定每轮计划、编写代码、运行实验并评估结果。在 nanochat 预训练实验中，它完成了 7 轮迭代、172 次训练，全程无崩溃，并在相同的单卡 20 分钟预算内，将 `val_bpb` 降低了 5.8%。同一流程还将溃坝仿真的越界量降低了三个数量级，并经过 8 轮二分迭代，完成了 FEA 极限载荷搜索。项目完整交付包括实验结果、可视化图表、海报、演示文稿和项目网站，全部由 Raven 制作。

<table>
<tr>
<td width="35.7%" valign="top"><p align="center"><b>CFD 溃坝仿真</b></p></td>
<td colspan="2" width="35.7%" valign="top"><p align="center"><b>FEA 极限载荷搜索</b></p></td>
<td width="28.6%" valign="top"><p align="center"><b><a href="https://livxue.github.io/raven-rsi/">网站 ↗</a></b></p></td>
</tr>
<tr>
<td width="35.7%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/44f81db4-4c17-47be-ad3b-047e26762a24"><img src="https://github.com/user-attachments/assets/44f81db4-4c17-47be-ad3b-047e26762a24" alt="Dam-break solve: a collapsing water column resolved to fine free-surface structure, with the out-of-bounds water fraction falling from 1e0 to 1.36e-10 over seven rounds" width="100%"></a></p></td>
<td colspan="2" width="35.7%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/89878a01-ab5c-4070-8359-5bc34c3d58b7"><img src="https://github.com/user-attachments/assets/89878a01-ab5c-4070-8359-5bc34c3d58b7" alt="Limit-load search: a cantilever beam under rising load colored by von Mises stress, with the bisection bracket narrowing from 1800-2000 kN down to 3.125 kN over eight rounds" width="100%"></a></p></td>
<td rowspan="3" width="28.6%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/5e001c7d-f861-470b-8f11-5b5e4cc6c020"><img src="https://github.com/user-attachments/assets/5e001c7d-f861-470b-8f11-5b5e4cc6c020" alt="The Chinese Raven RSI website as one long capture: research overview, nanochat, dam-break CFD, FEA solver convergence and model cost comparison" width="100%"></a></p></td>
</tr>
<tr>
<td colspan="2" width="41%" valign="top"><p align="center"><b>海报</b></p></td>
<td width="30.4%" valign="top"><p align="center"><b>演示文稿</b> · <b><a href="https://github.com/LivXue/Raven/releases/download/showcase-decks-2026-09-25/Raven-RSI-0925.pptx">PPTX ↓</a></b></p></td>
</tr>
<tr>
<td colspan="2" width="41%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/56ce36b7-05e9-4656-a332-7bbba91b5092"><img src="https://github.com/user-attachments/assets/56ce36b7-05e9-4656-a332-7bbba91b5092" alt="Raven RSI poster with its Chinese title: a spiral stone stair climbing into the light, with ravens circling it" width="100%"></a></p></td>
<td width="30.4%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/f2c368e3-789e-4a8d-890f-384240b36c25"><img src="https://github.com/user-attachments/assets/f2c368e3-789e-4a8d-890f-384240b36c25" alt="Cover, slides and closing slide of the Raven RSI deck" width="100%"></a></p></td>
</tr>
</table>

### ❯ Raven：一个完整的产品发布项目

**一只渡鸦，汇聚各路专家；整个项目由 Raven 完成。** Raven 的产品发布项目包括一款可在浏览器中直接游玩的物理小游戏、一份 16 页的产品介绍演示文稿、中英文海报，以及你正在阅读的这份 README，整套内容均由 Raven 制作。

<table>
<tr>
<td colspan="2" valign="top"><p align="center"><b>物理小游戏</b> · <b><a href="https://livxue.github.io/angry-raven/">在线试玩 ↗</a></b></p></td>
<td width="19.5%" valign="top"><p align="center"><b>README</b></p></td>
</tr>
<tr>
<td colspan="2" valign="top">

https://github.com/user-attachments/assets/dd186b6d-3752-4c59-89c7-eeea5c6fa962

</td>
<td rowspan="3" width="19.5%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/97476a17-f779-47ae-9006-2285ef2fe58a"><img src="https://github.com/user-attachments/assets/97476a17-f779-47ae-9006-2285ef2fe58a" alt="The top of the Chinese Raven README as one long capture: banner, introduction, the four built-in agents with their benchmarks, and runtime self-evolution" width="100%"></a></p></td>
</tr>
<tr>
<td width="46.2%" valign="top"><p align="center"><b>海报</b></p></td>
<td width="34.3%" valign="top"><p align="center"><b>演示文稿</b> · <b><a href="https://github.com/LivXue/Raven/releases/download/showcase-decks-2026-09-25/Raven-Overview-0925.pptx">PPTX ↓</a></b></p></td>
</tr>
<tr>
<td width="46.2%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/9152b06b-74a3-4059-adff-41267a69a207"><img src="https://github.com/user-attachments/assets/9152b06b-74a3-4059-adff-41267a69a207" alt="Raven poster with its Chinese title: a raven on a standing stone above sea cliffs at sunset" width="100%"></a></p></td>
<td width="34.3%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/2e19674e-83a8-4a77-a114-39f906c240d0"><img src="https://github.com/user-attachments/assets/2e19674e-83a8-4a77-a114-39f906c240d0" alt="Cover, slides and closing slide of the Raven overview deck" width="100%"></a></p></td>
</tr>
</table>

**[更多案例](docs/showcase.zh-CN.md)**

## ❯❯ 连接第三方 Agent

Raven 可以通过 ACP、CLI 或兼容 OpenAI 的 API 接入并编排 Agent，并内置 13 个**第三方 Agent** 预设，简化接入配置、任务委派以及在共享工作流中的协作。欢迎在 Raven 的统一界面中体验这些 Agent！

<p align="center">
  <img src="https://github.com/user-attachments/assets/3370c883-00ac-4471-97ef-f4312df77202" width="80%" alt="Third-party agents: Claude Code, Codex, OpenCode, Hermes Agent, OpenClaw, MiroThinker, GitHub Copilot, Qwen Code, CodeBuddy, Qoder, Grok Build, Kimi Code, and Pi">
</p>

## ❯❯ 快速开始

### 🤖 让 Agent 帮你安装

你可以让自己的 Agent 代为安装 Raven。把下面这段提示词发给任意一个能读取网页并执行 shell 命令的 Agent（例如 Claude Code 或 Codex）：

```text
阅读 https://evermind-ai.github.io/Raven/zh/quick-start/ 并按照其中的步骤安装 Raven；如果已经安装过，就将其更新。
```

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

如果要基于源码开发，或想使用尚未发布的版本，也可以从源码安装：

```bash
git clone https://github.com/EverMind-AI/Raven.git
cd Raven
./install.sh
```

直接运行本地的 `install.sh` 文件时，脚本会以可编辑（editable）模式安装当前检出目录：Raven 及其内置插件都链接回你的工作树，TUI 包和 Web 页面也从该目录构建。而通过管道运行时，即使当前位于克隆仓库内，也始终安装已发布的 wheel，确保一行命令安装不会误用工作树中的内容。如需在管道方式下强制可编辑安装，请设置 `RAVEN_LOCAL_SRC=<dir>`。

如果不想在宿主机上装任何东西，也可以用容器运行，只需 Git 和 Docker：

```bash
git clone https://github.com/EverMind-AI/Raven.git
cd Raven
docker compose -f docker/docker-compose.yml up
```

然后在浏览器中打开 `http://localhost:18793`。容器里跑了哪些进程、页面如何自动登录，以及端口对外暴露时该怎么配置，详见 [`docker/README.md`](docker/README.md)。前置依赖：[Git](https://git-scm.com/)、[Docker](https://www.docker.com/) 与 [Docker Compose](https://docs.docker.com/compose/)。

内置 Agent 随 Raven 一同分发：wheel 包自带 `agents/` 目录，首次使用时会复制到 Raven 主目录；源码安装则直接读取仓库中的该目录。配置向导会逐个询问：该 Agent 是使用其调优所用的模型（需要单独的 key），还是直接使用当前 Raven 的 LLM。这一步不做注册：只要目录存在，Agent 就会出现在名册中。详见 [`agents/README.md`](agents/README.md)。

在文档站了解更多关于 Raven 的内容。

**[阅读文档](https://evermind-ai.github.io/Raven/zh/)**

## ❯❯ 核心系统

| 系统 | 能力 |
| --- | --- |
| **Agent 编排** | 协调多个 Agent，管理任务依赖与并行执行，把多步骤协作沉淀为可复用的工作流。 |
| **Evolver** | 诊断失败原因、测试候选改进，并保留在可复现评估中优于基线的改动，驱动 harness 自进化。 |
| **EverOS 记忆** | 跨会话保留用户上下文、Agent 经验与世界知识，在后续任务中召回相关记忆和可复用技能。 |
| **SkillForge** | 从本地技能库、EverOS 记忆以及 [SkillHub 的 **114,190 项技能**](https://github.com/EverMind-AI/SkillCorpus#public-artifacts)中检索相关技能，按需为 Agent 补充专业能力。 |
| **主动行为** | 结合事件监测与定时执行，预判用户需求，及时提醒并发起后续工作。 |

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>


## ❯❯ 启动 WebUI

Raven 的 WebUI 把对话、多 Agent 协作与工作区管理集中到浏览器中。你可以在同一个界面里与 Agent 对话、跟踪任务进度、查看文件与产出，并浏览记忆和技能。

```bash
raven web
```

该命令会在浏览器中打开 WebUI，并让 Raven 在后台持续运行；执行 `raven web --stop` 即可停止后台服务。

<p align="center">
  <a href="https://github.com/user-attachments/assets/79de2a65-076d-4b57-930d-2f8123e3d580"><img src="https://github.com/user-attachments/assets/79de2a65-076d-4b57-930d-2f8123e3d580" alt="Raven WebUI new task page" width="90%"></a>
</p>

<p align="center"><em>新建任务：一个输入框，技能、Playbook、知识库与记忆一键可达。</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/cc16c25a-8931-4e6b-a8df-9fae57997dde"><img src="https://github.com/user-attachments/assets/cc16c25a-8931-4e6b-a8df-9fae57997dde" alt="Raven WebUI subagents page" width="90%"></a>
</p>

<p align="center"><em>子 Agent：所有已接入的 Agent，无论内置还是第三方，都汇集在同一份名册中。</em></p>

## ❯❯ EverMind 生态

<p align="center">
  <a href="https://github.com/user-attachments/assets/65a5e3f1-dccb-496f-94f5-9782070ec8b4"><img src="https://github.com/user-attachments/assets/65a5e3f1-dccb-496f-94f5-9782070ec8b4" alt="The EverMind ecosystem: the EverMind mark and its slogan on an orbital field" width="100%"></a>
</p>

[EverMind](https://evermind.ai/) 将记忆研究、生产级产品与落地集成连接成一个开源生态。

<table>
<tr>
<th colspan="2">产品</th>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverOS">EverOS</a></strong></td>
<td>面向 Agent 与用户的长期记忆运行时，本地优先、以 Markdown 为原生格式。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/Raven">Raven</a></strong></td>
<td>以记忆为核心、可自我改进的 Agent harness，支持主动行为、上下文控制与技能演化。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverMe">EverMe (CLI)</a></strong></td>
<td>面向跨设备、跨 Agent 个人记忆的 CLI 与 Agent 插件套件。</td>
</tr>
<tr>
<th colspan="2">研究与评估</th>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/SkillCorpus">SkillCorpus</a></strong></td>
<td>经过整理、可直接检索的 Agent 技能语料库，附带检索与评估工具。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverAlgo">EverAlgo</a></strong></td>
<td>为 EverOS 提供无状态的提取、排序、解析与记忆算子。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/HyperMem">HyperMem</a></strong></td>
<td>基于超图的分层记忆，支持对长期对话进行由粗到细的检索。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/MSA">MSA</a></strong></td>
<td>Memory Sparse Attention（记忆稀疏注意力），支持可扩展的潜在记忆与 100M Token 上下文。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverMemBench">EverMemBench</a></strong></td>
<td>评估记忆系统的事实召回、应用推理和个性化泛化能力。</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EvoAgentBench">EvoAgentBench</a></strong></td>
<td>从纵向维度评估 Agent 的自进化、迁移效率、错误规避与技能使用。</td>
</tr>
<tr>
<th colspan="2"><a href="https://github.com/EverMind-AI/plugins">集成</a></th>
</tr>
<tr>
<td><strong><a href="https://docs.openclaw.ai">OpenClaw</a></strong></td>
<td><a href="https://github.com/EverMind-AI/plugins/tree/main/openclaw">OpenClaw 插件</a>，支持自动召回、记忆采集与会话记忆的生命周期管理。</td>
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

这些项目共同构成了 EverMind 从研究到运行时的技术栈：让方法与基准沉淀为可复用的记忆基础设施、产品与 Agent 集成。

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## ❯❯ 参与贡献

欢迎提交 issue 和 pull request。开始前请先阅读[开发工作流](docs/dev.md)并遵循 [AGENTS.md](AGENTS.md) 中的仓库规则；设计方案欢迎到 [GitHub Discussions](https://github.com/EverMind-AI/Raven/discussions) 讨论。

## ❯❯ 许可证

[Apache License 2.0](LICENSE)
