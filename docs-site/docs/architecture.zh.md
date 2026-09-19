# 架构

所有运行时入口都通过同一个**装配根（Assembly Root）**组装 Raven：
`raven/core/runtime.py:build_runtime`。配置与插件贡献共同决定每个运行时世代（Generation）
包含的组件。Spine 为 Agent Loop 调度轮次，并投递执行过程中产生的事件。

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
| **Agent 编排** | 协调各个 Agent，管理任务依赖与并行执行，并把多步协作沉淀为可复用的工作流。 |
| **Evolver** | 诊断失败、测试候选改进，并在可复现的评估中保留优于基线的改动，推动 Harness 自进化。 |
| **EverOS 记忆** | 跨会话保留用户上下文、Agent 经验与世界知识，在后续任务中召回相关记忆和可复用技能。 |
| **SkillForge** | 从本地技能库、EverOS 记忆以及包含 **114,190 项技能**的 SkillHub 目录中检索相关技能，按需为 Agent 提供专业能力。 |
| **主动行为** | 结合事件监测与定时执行，预测用户需求、及时提醒，并主动发起后续工作。 |

## 术语与边界定义 { #where-the-canonical-definitions-live }

仓库在实现代码之外维护以下权威定义：

- `CONTEXT-MAP.md` 指向每个子系统对应的上下文文件。
- `CONTEXT.md` 定义运行时术语，以及各包所属的架构层级（Layer Seats）。
- `pyproject.toml` 定义用于检查内核边界的导入契约。
