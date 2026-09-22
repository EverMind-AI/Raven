# DAG 编排

任务包含并行工作和明确依赖时，可以使用有向无环图（DAG）。Raven 校验图、把节点派给
可用 Agent，并记录结果。这是 Raven 宿主内的编排，不是持久化的分布式工作流服务。

## 选择委派形式 { #choose-the-delegation-shape }

| 形式 | 适用场景 | 共享内容 |
| --- | --- | --- |
| 直接执行轮次 | 无需委派的任务 | 当前对话 |
| `spawn` | 单个聚焦的子 Agent 任务 | 明确任务与可选 instance |
| `run_subagent_dag` | 多个有依赖的任务 | 节点输出、引用、运行状态 |
| [Playbook](playbooks.md) | 反复使用的已审阅工作流 | 已保存的过程、参数及随附配置 |

本地 roster 可包含 ACP、CLI、内置和兼容 OpenAI 的后端。A2A peer 独立于它：
`a2a_send` 调用远程宿主，不是名为 `a2a` 的 DAG 节点后端。详见[Agent 协议](agent-protocols.md)。

## 动态 Worker 与任务 Charter { #dynamic-workers-and-task-charters }

普通 roster 列出已注册 Agent。显式启用生成模式后，可以为当前轮次准备
**Worker Table**：每个 worker 有 label、底层 Agent 和本任务的职责 brief。
两个 worker 可以使用同一 Agent 承担不同职责，不等于安装了两个新 Agent。

将此片段合并到宿主配置，并 reload/restart 宿主，让后续轮次使用它：

```json
{
  "playbooks": {
    "agentHarness": "generate"
  }
}
```

默认是 `"default"`，不生成表。生成会在委派前增加模型工作；失败时记录警告，
按未生成 Worker Table 的配置继续本轮。它不会自行决定任务图，也不移除主 Agent 工具；
模型仍自行决定委派什么。

| 层次 | 生命周期 | 含义 |
| --- | --- | --- |
| Roster Agent | 注册配置 | 哪个后端能执行工作 |
| Worker Table 行 | 一个宿主轮次 | 本任务的 label 和职责 |
| Charter | 一个被委派 worker 轮次 | 收窄的指令、工具、检查与可选期限 |
| 保存的 Playbook | 可复用文件 | 已编写的图/指导与参数 |

生成模式中的 `spawn`、DAG 和 replan 参数应使用当前工具的 worker label。
保存的 Playbook 私有 DAG 则使用底层 registry 名称，避免同名临时 label 改写已保存流程。

**Charter** 通过 `_meta["raven.playbook"]` 发送给支持它的 Raven ACP worker，
内置 worker 则在本地绑定。它与 worker 已有权限取交集：可收窄工具集或期限，
不能恢复已禁用工具或延长配置的 deadline。任意外部 ACP Agent 不一定理解该元数据。

声明式检查与可选生成 judge 代码是不同机制。代码通过 AST allowlist 准入；
不合规代码会被丢弃并记录警告，评估器异常也不会自动变成拒绝。
Charter 不是操作系统沙箱，也不是 fail-closed 安全边界；应独立配置
[权限](permissions.md)和后端隔离。

实例追问和 steer 的操作见[与子 Agent 协作](agent-collaboration.md)。
实现位于 `raven/agent/subagent/delegate.py`、`charter.py`、`charter_code.py`
以及 `raven/playbook/agent_generator.py`。

## 第一张图 { #a-first-graph }

先配置所需 Agent 并选择对话工作目录。让 Raven 分别检查项目的两个独立方面，再综合
发现，不修改文件。模型可以提交以下**工具参数**；它不是 Shell 命令或独立配置文件：

```json
{
  "task_summary": "Review project readiness",
  "background": false,
  "nodes": [
    {
      "id": "api_audit",
      "subagent": "Raven-Code",
      "node_summary": "Inspect API contracts",
      "prompt_template": "Read the public API and report compatibility risks with file references. Do not edit files."
    },
    {
      "id": "test_audit",
      "subagent": "Raven-Code",
      "node_summary": "Inspect test coverage",
      "prompt_template": "Read the tests and report missing coverage with file references. Do not edit files."
    },
    {
      "id": "readiness_summary",
      "subagent": "Raven",
      "node_summary": "Synthesize readiness findings",
      "depends_on": ["api_audit", "test_audit"],
      "prompt_template": "Combine these findings into a prioritized review. Do not edit files. API: {{ api_audit.output }} Tests: {{ test_audit.output }}"
    }
  ]
}
```

前两个节点可以并发，最后一个等待两者完成。名称应替换为当前工具公布的 roster 名称；
宿主生成 worker table 时，使用 worker label，而不是底层 Agent 名。再次向同一对话
提交图时，要选择新的节点 id。

写入任务应划分文件所有权或使用不同工作副本；并行节点不代表自动拥有独立 worktree
或文件排他锁。

## 节点契约与数据流 { #node-contract-and-data-flow }

| 字段 | 含义 |
| --- | --- |
| `id` | 整个对话内唯一的任务地址，包括早先 DAG 和 `spawn` 调用 |
| `subagent` | 当前工具声明的 Agent 名或 worker label |
| `node_summary` | 展示给用户的简短步骤标题 |
| `prompt_template` | 任务指令和输入占位符 |
| `depends_on` | 前置节点 id |
| `inputs` | 字符串字面值、`{"file": "path"}` 或 `{"node": "id"}` |
| `skills` | 省略：Agent 自身菜单；`[]`：无技能；列表：在支持时收窄菜单 |
| `mcps` | 省略：配置行默认值；`[]`：不附加 MCP；列表：替换选择 |
| `instance` | 复用子 Agent 会话，不是节点输出地址 |

图要求 `task_summary` 和 `nodes`。`background` 是工具调用参数；`confirm` 是整图
确认开关，默认 false。面向模型的 schema 使用 snake_case，保存的 playbook 也接受
camelCase 节点字段。未知节点字段会被拒绝，不会静默成为指令。

常用内容形式为 `{{ inputs.brief }}`、`{{ api_audit.output }}` 和 `{{ ref:brief.md }}`；
`{{ inputs.brief.path }}`、`{{ api_audit.output_path }}`、`{{ ref_path:brief.md }}`
则把路径交给能读本地文件的后端。宿主路径不等于远程文件传输；后端没有本地文件访问
能力时，优先使用内容形式。

引用同一张图中的节点时必须声明依赖。可以直接引用同一对话中早先完成的节点，无需
重跑，但其保存输出必须存在。失败、跳过、取消、中断或仍在运行的节点不是有效输入。
Id 的唯一性比较忽略大小写，失败后也不会释放 id。

文件引用只允许落在工作目录和当前对话的子 Agent 历史内，不暴露整个 Agent home、
记忆、技能或其他对话。`@nodes/` 表示当前对话的扁平节点产物目录。

## 调度与共享限制 { #scheduling-and-shared-limits }

调度前 Raven 检查必填字段、id、依赖、循环、输入契约、路径边界和后端能力。已就绪的
独立节点可并发，并与 `spawn` 和其他 DAG 共享宿主信号量。
`agents.defaults.maxConcurrentSubagents` 默认 8；
`agents.defaults.maxSubagentSpawnsPerHour` 默认每会话 30，也限制重复提交 DAG。

共享 `instance` 的节点串行执行。顺序重要时仍要声明依赖；共享 instance 本身不保证
确定的顺序。不支持会话续接的后端不会因为传入 instance 就获得状态。可选的技能/MCP
收窄不受支持时会报告降级，应先检查通知，再判断限制是否生效。

## 前台、后台与审批 { #foreground-background-and-approval }

`background` 默认 true：工具先返回 run id，稍后将结果送回原对话。主 Agent 必须
等待输出时设为 false。前台调用返回最终结果或节点异常报告；通过 `resolve_dag_node`
回答后继续等待。

原轮次仍活动时，前台运行处于 **bound（绑定）** 状态。轮次结束后运行被
**released（释放）**，后续报告走后台路径。绑定时的异常等待没有裁决期限；释放后或
后台等待有期限。“后台”不意味着进程终止后任务仍会继续。

希望用户在任何节点运行前批准整图时，设置 `confirm: true`，尤其是发送、发布或付费
任务。**未接入 ask channel 时，当前实现记录提示并在未经确认的情况下执行。**已接入时，
拒绝或投递错误会阻止调度。因此该开关不是无人值守安全门。整图批准不会绕过单工具策略；但 ACP 委派可能自动
批准子 Agent 的协议请求。执行有外部影响的图前，请阅读[权限与安全](permissions.md)。

## 失败处理与重新规划 { #failure-handling-and-replanning }

| 状态 | 含义 |
| --- | --- |
| `pending` | 尚未调度，可能等待依赖或执行额度 |
| `running` | 节点活动中 |
| `completed` | 已由配置的完成判断路径接受 |
| `exception` | 工作未成功，等待继续、放弃或重新规划 |
| `failed` | 该节点最终失败 |
| `skipped` | 前置失败，或调度前执行已停止 |
| `cancelled` | 活动节点被取消 |
| `interrupted` | 历史读取发现未完成记录，但已无活动运行 |

默认情况下，模型 **verdict（裁决）** 根据输出、transcript 尾部及可用的传输 stop
reason 判断节点是否真正完成 prompt 的任务；进程正常返回本身不足以证明成功。但
verdict 是质量检查，不是安全门：关闭 judge，或 judge 失败/超时时，会退回接受正常
后端返回的行为。

进入 exception 后，`resolve_dag_node` 可以提供修正指令继续、放弃节点，或重新规划。
重新规划会结束旧运行并启动新运行；按引用复用已完成输出，为替代节点分配新 id。
前置节点最终失败会使依赖节点跳过，不必丢弃独立分支。

`subagentDag` 默认启用 verdict，judge 超时为 180 秒，后台裁决窗口 600 秒，每节点
最多两次继续。这些设置限制额外模型调用和等待，不证明外部动作可安全重试。继续有
副作用的节点前，应检查是否已创建文件、发送消息或启动远程任务。

## 检查与恢复 { #inspect-and-recover }

可让 Raven 用 `dag_status` 查看运行，用 `cancel_dag` 停止运行。这些是模型工具，
不是 `raven dag` Shell 命令。TUI 与 WebUI 接收实时进度并可读取已保存的图；RPC 提供
`dag.get` 和 `dag.node`。

历史归会话管理器所有，与用户交付文件分开：

```text
<session directory>/subagents/
  mas_dag/<run_id>/graph.json
  mas_dag/<run_id>/manifest.json
  nodes/<node_id>.prompt.md
  nodes/<node_id>.out.md
  nodes/<node_id>.error.md
  nodes/<node_id>.meta.json
  nodes/<node_id>.memory.json
```

并非所有结果都会产生每种文件；未结束运行可能没有最终 manifest。重载 UI 会重建
记录状态，而不是重放全部实时事件。宿主重启后，未完成节点可能显示 interrupted：
恢复历史**不等于自动恢复执行**。检查已完成输出和副作用后，为剩余工作提交新图。

使用 `raven tracing` 查看运行时 span，使用 `raven trajectory --help` 查保存的
轨迹工作流。故障报告不要公开原始 prompt、凭据或私有文件。核心实现位于
`raven/agent/subagent/dag_graph.py` 及同目录下的 `dag_runner.py`、`dag_tool.py`、
`dag_control_tools.py` 和 `dag_resume.py`。
