# 架构

所有运行时入口都通过同一个**装配根（Assembly Root）**组装 Raven：
`raven/core/runtime.py:build_runtime`。配置与插件贡献共同决定每个运行时世代（Generation）
包含的组件。Spine 为 Agent Loop 调度轮次，并投递执行过程中产生的事件。

## 入口、运行时、执行与状态 { #surface-runtime-execution-and-state }

下表说明各类请求如何进入运行时、在哪里执行，以及由谁保存状态。这是职责划分，
不是新增的代码分层。

| 入口 | 运行时接入 | 执行 | 状态所有者 |
| --- | --- | --- | --- |
| WebUI / TUI | 共享 RPC 与 Spine | 宿主 Agent Loop | 宿主会话和 Agent home |
| 消息平台 | Adapter、Intake、Gateway / Spine | 宿主 loop 与获准委派 | Channel/chat 对话、适配器账户状态 |
| 编辑器/本地 ACP 宿主 | ACP server 与 RPC 栈 | 作为 Agent 提供服务的 Raven | Server ACP 会话和配置 home |
| 入站 A2A peer | 已认证 HTTP、单次轮次适配 | 宿主 Agent Loop | 内存 A2A task store，加运行时 session 记录 |
| 宿主委派 | Roster 与子 Agent manager | Built-in、ACP、CLI 或 HTTP 后端 | 宿主调用历史，加后端会话状态 |
| 出站 A2A 调用 | Card discovery 与 A2A client | 独立运营的 peer | 远程 task 生命周期 |

记忆与 SkillForge 提供上下文，插件扩展运行时能力。
DAG 负责协调本地名册中的 Agent，不提供持久化的分布式任务队列。

配置使用见[使用 Raven](using-raven.md)、[Agent 集成](agent-integrations.md)和
[渠道与消息](channels.md)。开发者从[构建 Agent](building-agent.md)或
[协议与后端集成](protocol-backends.md)开始。

## 轮次的处理流程 { #how-a-turn-reaches-the-agent-loop }

请求从不同接口进入，由 Spine 统一协调轮次调度。以下组件分别负责执行、上下文管理和任务委派。

| 组件 | 输入 | 职责 |
| --- | --- | --- |
| **入口** | 来自 WebUI、TUI、外部 ACP 宿主、CLI、消息渠道和主动触发机制的请求 | 经由 RPC、ACP、网关，或直接向 Spine 提交任务 |
| **Spine** | 已提交的轮次 | 调度 Agent Loop 并投递事件 |
| **Agent Loop** | 已调度的轮次 | 协调 Harness 模块、工具和任务委派 |
| **Harness 模块** | Memory、Planning、Capability 和 Action 请求 | 提供上下文组装、规划、工具选择和模型响应策略 |
| **上下文引擎** | 本轮可用的上下文 | 借助记忆引擎与 SkillForge 组装上下文 |
| **记忆与技能** | 记忆和技能请求 | 从 EverOS 插件、本地技能库与 SkillHub 检索内容 |
| **委派** | 子 Agent 或 Playbook 调用 | 将任务分派给内置、ACP、CLI 或兼容 OpenAI 的后端 |

WebUI 与 React/Ink TUI 共用同一份契约 `rpc-schema/openrpc.json`。
ACP 服务端将外部宿主接入 Raven 的 RPC 栈，ACP 客户端则让 Raven 调用其他 Agent。
CLI 任务、消息渠道和主动触发机制也通过 Spine 提交任务。

## 协议入口与编排 { #protocol-faces-and-orchestration }

| 入口 | 边界 | 生命周期 | 主要用途 |
| --- | --- | --- | --- |
| ACP 服务端 | 本地进程和 stdio | 会话、轮次 | 编辑器或本地宿主运行 Raven |
| ACP 客户端 | 子 Agent 进程 | 会话、轮次 | Raven 委派给本地 Agent |
| A2A 服务端 | 使用 bearer 认证的 HTTP peer | 任务、事件流 | 远程 Agent 调用 Raven |
| A2A 客户端 | 远程 origin，可附配置凭据 | Card、任务、响应 | Raven 调用独立 peer |
| RPC | Raven UI 或控制客户端 | 连接、会话 | WebUI、TUI、控制面操作 |

这些接口最终调用 Agent Loop，但各有自己的认证方式和消息格式。入站 A2A 当前直接使用单轮
Agent Loop 路径，不经过交互式轮次使用的 Spine 调度和投递路径；其任务状态不是
持久化调度队列。

`run_subagent_dag` 定义任务依赖并协调本地执行，A2A 负责与独立运行的远程 Agent
通信。集成前请阅读[Agent 协议](agent-protocols.md)、[DAG 编排](orchestration.md)
及[权限与安全](permissions.md)。

## 组件边界与职责 { #what-the-boundaries-guarantee }

- **模块化执行。** Agent Loop 负责轮次状态、工具执行、持久化和事件顺序。四个 Harness
  模块提供可替换的记忆、规划、能力选择和模型响应策略；钩子与工具补充领域专用能力。
- **Agent 与插件组合。** `agents/` 下的定义将已安装的运行时与 Agent 专用配置、插件组合，
  通过 ACP 提供服务。`plugins-dist/` 以独立发行包的形式提供 EverOS 记忆、
  视觉设计与 PowerPoint 引擎。
- **内核边界。** `spine/`、`contracts/`、`tracing/` 与 `home.py` 构成独立的内核。内层运行时
  包不导入 CLI、RPC 或 ACP 接口模块，导入契约会检查这些边界。
- **Harness 自进化。** `evolver/` 用于诊断运行结果，并通过基准测试评估候选的 Harness
  改动。它作为独立工具调用 Raven 库；运行时不导入 Evolver 或仓库级的 Agent 定义。

各组件对应的目录和包，请参阅[仓库布局](repo-layout.md)。

## 核心系统 { #core-systems }

这些组件共同支撑 Raven 的核心能力：

| 系统 | 能力 |
| --- | --- |
| **Agent 编排** | 协调各个 Agent，管理任务依赖与并行执行，并将多步协作保存为可复用的工作流。 |
| **Evolver** | 诊断失败、评估候选 Harness 改动，分别记录候选晋升结果与统计证据。 |
| **EverOS 记忆** | 跨会话保留用户上下文、Agent 经验与世界知识，在后续任务中召回相关记忆和可复用技能。 |
| **SkillForge** | 从已配置的本地技能库、记忆后端和 SkillHub 检索相关技能；可用内容取决于这些来源。 |
| **主动性** | 通过 Sentinel 和定时触发机制，在配置与策略允许的范围内决定何时提醒或发起后续工作。 |

技能、记忆、MCP、插件、playbook 和专用 Agent 各有不同的扩展边界。请用
[技能与扩展](skills-and-extensions.md)选择方案，区分已有实现与需要额外服务的能力。

## 术语与边界定义 { #where-the-canonical-definitions-live }

仓库在实现代码之外维护以下权威定义：

- `CONTEXT-MAP.md` 指向每个子系统对应的上下文文件。
- `CONTEXT.md` 定义运行时术语，以及各包所属的架构层级（Layer Seats）。
- `pyproject.toml` 定义用于检查内核边界的导入契约。
