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

## 📊 下一版本研究评测

| 基准测试 | 研究原型结果 | 对比 |
| --- | --- | --- |
| 效率 | 27B 下为 `56.7%`；397B 下为 `58.1%` | Hermes 为 `46.8%` / `47.9%`；27B 下领先 `+9.9pp` |
| 自我进化 | EvoAgentBench 排名 `#1` | 在四种方法中领先下一名 `+6.2pp` |
| 主动性 | ProAgentBench F1 为 `0.60` | 是 Hermes/OpenClaw `0.253` 的 `2.4x` |

以上结果来自内部研究原型，并不代表当前公开版本已经支持 The Harness of Harnesses 协作网络。模型、任务集和评测协议都会影响最终结果。

https://github.com/user-attachments/assets/3c541dae-5852-447f-8ea6-c9877612ad57

## 🚀 快速开始

### 🧰 前置依赖

安装脚本会自带 Python 工具链和 Node 运行时，只有一个程序需要你自己先装好：

| 程序 | 用途 | 安装 |
| --- | --- | --- |
| **LibreOffice** | 把 deck 转成 PDF：deck agent 的渲染、测量、预览都走这一步，读取 Office 源文档也靠它。可选项——没有它 deck 仍然能生成并交付，但所有基于渲染页面的检查都不会运行。 | `apt install libreoffice` / `brew install --cask libreoffice` / `winget install TheDocumentFoundation.LibreOffice` |

`raven doctor` 会报告它是否被找到。

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

### 🧭 完成引导并运行

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

### ⬆️ 升级

```bash
raven upgrade --check
raven upgrade
```

升级会保留配置、sessions 和 memory。Raven 不会自动更新。

## 🏠 自托管

Raven 可以直接从源码仓库运行，也可以作为单个 Docker Compose 服务运行。
Compose 部署通过 nginx 提供页面，在同一个容器中运行 Raven 引擎及其子服务，
并将持久化数据保存到命名卷中。

### 📝 前置条件

使用 Docker 部署时，请安装 Docker Engine 和 Docker Compose v2。使用源码部署时，
请安装 Python 3.12、`uv`、Node.js 和 npm，并在启动引擎前安装仓库依赖。

### 🚀 从源码启动服务

在仓库根目录运行：

```bash
make install-deps
make build-ui
uv run raven web
```

`raven web` 会打开本地页面，并在终端退出后保持引擎运行。默认地址是
`http://127.0.0.1:18792`。调试时可以使用 `uv run raven web --foreground`，
使用 `uv run raven web --stop` 停止常驻引擎。首次启动时可以暂时不配置模型，
在 **Settings > Models** 中添加，或运行 `uv run raven onboard`。

如果只需要启动引擎而不打开浏览器页面，请使用 `uv run raven gateway`。

### 🐳 使用 Docker Compose 启动

仓库中的 Compose 配置会在构建镜像时完成页面和 Python 环境的构建，因此不需要
在宿主机上单独构建：

```bash
cd docker
docker compose up
```

然后打开 <http://127.0.0.1:18793>。Compose 容器始终运行完整的 `gateway`
引擎，因此在 **Settings > Models** 中添加 Provider 后，无需重启即可在下一轮
对话中使用。

容器布局、登录流程、Provider 配置和运维说明请参阅
[`docker/README.md`](docker/README.md)。

### ⚙️ 配置

Docker 使用 [`docker/.env`](docker/.env) 中的已提交默认值，然后加载可选的、
被 git 忽略的 `docker/.env.local` 覆盖这些默认值。请将凭据和部署相关的覆盖项
写入 `.env.local`，不要写入已提交的文件。常用配置包括：

| 变量 | 用途 |
| --- | --- |
| `RAVEN_WEB_PORT` | Compose 对外发布的页面端口，默认为 `18793` |
| `RAVEN_AUTO_LOGIN` | 是否自动为本地浏览器登录；远程暴露时设为 `0` |
| `RAVEN_EXTRAS` | 可选镜像扩展，例如 `channels`、`tools`、`sandbox`、`browser` 或 `eval` |
| `RAVEN_PLUGINS` | 要安装到镜像中的内置插件，例如 `everos-memory` |
| `RAVEN_PROVIDER` | 可选 Provider，在容器启动时写入 `config.json` |
| `RAVEN_API_KEY` | 可选 Provider 密钥；本地 Provider 可以留空 |
| `RAVEN_API_BASE` | 可选自定义端点；无密钥的本地 Provider 只需设置此项 |

Raven 会将配置、sessions、workspace、日志和 memory 保存在 `RAVEN_HOME` 下。
Compose 镜像将其映射到 `raven-data` 卷中的 `/data`。升级或重启时请保留该卷；
`docker compose down -v` 会删除卷及其中的数据。

### 🛠️ 构建 Docker 镜像

使用 Makefile 目标构建镜像：

```bash
make docker-build
```

默认标签是 `raven:local`。如需指定其他标签或可选依赖，可以运行：

```bash
make docker-build DOCKER_IMAGE=raven:local
docker build -t raven:local --build-arg RAVEN_EXTRAS="channels,tools,sandbox" .
```

要通过 Compose 运行本地构建的镜像，请设置 `RAVEN_IMAGE=raven:local`（也可以在
命令前直接设置该变量），然后在 `docker/` 目录运行 `docker compose up`。对应的
Makefile 快捷方式是 `RAVEN_IMAGE=raven:local make docker-up`。使用
`make docker-down` 停止服务。

## 📚 文档

本页保留简介、快速开始和自托管入口。规范性内容——仓库布局、命令参考、贡献规则——以英文
[README.md](README.md) 与 [AGENTS.md](AGENTS.md) 为准，不再在此维护中文副本，
以免两份文档漂移。完整文档见 `docs/` 目录，领域术语从 `CONTEXT-MAP.md` 入口查阅。

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## 🌐 EverMind 生态

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

## 🤝 参与贡献

欢迎提交 issues 和 pull requests。请先阅读[开发工作流](docs/dev.md)，按照 [AGENTS.md](AGENTS.md) 中的仓库规则进行协作，并在 [GitHub Discussions](https://github.com/EverMind-AI/Raven/discussions) 讨论设计方案。

## ⚖️ 许可证

[Apache License 2.0](LICENSE)
