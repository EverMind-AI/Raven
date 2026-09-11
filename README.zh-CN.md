<div align="center" id="readme-top">

![Raven banner](https://github.com/user-attachments/assets/6c6f585a-21b6-4e7b-9187-acffe59d0c10)

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

Raven 是构建在 [EverOS](https://github.com/EverMind-AI/EverOS) 之上的
**The Self-Improving Agent Harness**，并内置可选 Deep Research，用于多来源深度研究。

Raven 会持续迭代支撑 Agent 的 harness：tools、skills、memory、code execution
runtime、policies 和工作环境。EverOS 为这个 harness 提供跨会话持久存在的用户
记忆、Agent 记忆和世界知识，让每一次运行都能改进 Agent 的行动方式、知识状态，
并把可重复工作流沉淀成可复用 Agent Templates 和 digital workers。

**Update：** Raven 新增 Deep Research。运行 `raven deep-research enable` 后，
Agent 可以在需要深度调查的任务中使用 MiroThinker-backed、多来源 research tool。

> Raven 目前处于 pre-alpha 阶段，接口和配置可能快速变化。

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
docker compose up --build
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
make docker-build DOCKER_IMAGE=raven:dev
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

## 🤝 参与贡献

欢迎提交 issues 和 pull requests。请先阅读[开发工作流](docs/dev.md)，按照 [AGENTS.md](AGENTS.md) 中的仓库规则进行协作，并在 [GitHub Discussions](https://github.com/EverMind-AI/Raven/discussions) 讨论设计方案。

## ⚖️ 许可证

[Apache License 2.0](LICENSE)
