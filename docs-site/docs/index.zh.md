<p class="em-eyebrow">文档</p>

# Raven 文档

<p class="em-standfirst">安装并配置 Raven，选择部署方式，查阅常用命令。</p>

从安装开始，再选择适合自己的使用方式。**指南**介绍日常使用、消息渠道、Agent、
技能与部署；**参考**说明协议和运行时行为；**开发者**介绍如何构建
Agent、插件和适配器。

## 从这里开始 { #start-here }

<div class="grid cards em-step" markdown>

-   __安装 Raven__

    ---

    根据平台安装 Raven，配置模型服务商，然后开始使用 WebUI。

    [快速开始](quick-start.md)

-   __自托管部署__

    ---

    在自己的机器上部署 Raven，并完成服务配置。

    [自托管](self-hosting.md)

-   __打开 WebUI__

    ---

    打开浏览器界面，管理后台服务。

    [启动 WebUI](webui.md)

</div>

## 选择运行方式 { #choose-how-to-run-it }

<div class="grid cards em-run" markdown>

-   __Docker Compose__

    ---

    在同一个容器中构建并启动网关和 WebUI。

    [用 Compose 启动](self-hosting.md#compose)

-   __从源码运行__

    ---

    从本地克隆的仓库构建并运行 Raven，开发新功能或体验尚未发布的改动。

    [从源码启动](self-hosting.md#from-source)

</div>

两种部署方式、配置说明和 Docker 镜像构建步骤，请参阅[自托管](self-hosting.md)。

## 浏览文档 { #explore-the-documentation }

<div class="grid cards em-docs" markdown>

-   __自托管__

    ---

    通过 Docker Compose 或源码部署，并管理运行配置。

    [阅读](self-hosting.md)

-   __Docker 部署__

    ---

    了解容器服务、登录方式、模型服务商配置和数据存储。

    [阅读](docker.md)

-   __启动 WebUI__

    ---

    打开浏览器界面，启动或停止后台服务。

    [阅读](webui.md)

-   __架构__

    ---

    了解装配根、运行时分层和核心系统。

    [阅读](architecture.md)

-   __命令参考__

    ---

    查阅 Raven 命令及其用途。

    [阅读](commands.md)

-   __仓库布局__

    ---

    浏览顶层目录、运行时包及对应的提交范围（scope）。

    [阅读](repo-layout.md)

</div>

## 连接与扩展 Raven { #connect-and-extend-raven }

| 下一步任务 | 指南 |
| --- | --- |
| 选择日常交互入口 | [使用 Raven](using-raven.md) |
| 连接消息平台 | [渠道与消息](channels.md) |
| 配置 Raven 专用或外部 Agent | [Agent 集成](agent-integrations.md) |
| 连接编辑器或远程 Agent | [Agent 协议：ACP 与 A2A](agent-protocols.md) |
| 协调并行工作和依赖 | [DAG 编排](orchestration.md) |
| 理解审批和信任边界 | [权限与安全](permissions.md) |
| 添加技能、记忆、MCP、插件或 Agent | [技能与扩展](skills-and-extensions.md) |
| 构建专用 Agent | [构建 Agent](building-agent.md) |
| 增加运行时工具或 hook | [构建插件](building-plugin.md) |
| 实现协议或渠道适配器 | [协议与后端集成](protocol-backends.md) |

可通过站点搜索或[命令参考](commands.md)查找具体功能的配置步骤、示例和排障方法。

Raven 由 EverMind 开源维护。
