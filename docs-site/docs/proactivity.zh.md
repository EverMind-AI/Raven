# 主动提醒与跟进

本文面向希望 Raven 在没有新请求时主动提供提醒和后续工作的用户与运维人员，说明如何启用、
配置、观察和停止 **Sentinel**。开发者请阅读
[主动性设计与实现](proactivity-design.md)，了解决策流程和执行契约。

## 选择合适的机制 { #choose-the-right-mechanism }

Proactive Engine 包含 Sentinel，以及按时间触发的 Cron 和 Heartbeat 服务。它们的用途
与生命周期不同：

| 机制 | 适用场景 | 工作如何开始 |
| --- | --- | --- |
| Sentinel | 让 Raven 判断某个贴合上下文的提醒或后续工作是否值得做 | 周期性评估记忆、近期对话与派生信号 |
| Cron | 明确安排一个定时任务或提醒 | 用户创建的计划 |
| Heartbeat | 在 `HEARTBEAT.md` 中维护周期性任务 | 独立计时器或提前唤醒请求 |

Sentinel 默认关闭（`sentinel.enabled=false`）。启用它不会自动开启任务发现；关闭它也不会
关闭 Cron 或 Heartbeat。各自的命令见[命令参考](commands.md)。

Sentinel 可以保持安静、发送独立提醒、把提醒追加到后续回复、等待会话空闲，或者派发后台任务。
这些都是模型决策，并非有保证的定时计划：需要明确时间时请用 Cron；不要把 Sentinel 作为
关键截止期的唯一告警方式。

## 启用前检查 { #before-enabling }

- 配置可用的模型服务商；Planner 通常继承 `agents.defaults.model`。见
  [快速开始](quick-start.md)。
- 配置用于接收提醒的消息渠道，并确认该渠道的普通消息收发正常。
- 确认允许所配置的模型服务商接收哪些 Agent 主目录记忆与会话数据；Sentinel 会将它们用于
  规划上下文。
- 检查工具权限、工作区限制和子 Agent 后端。网关启用 Sentinel 时也会接入后台任务派发，
  这个开关并非“仅提醒”模式。

以下步骤使用负责组装并运行 Sentinel 的 `raven gateway`。单独运行一条
`raven sentinel` 命令不会启动后台服务。

## 启用与配置 { #enable-and-configure }

在源码工作目录中查看已保存的配置：

```bash
uv run raven sentinel status
```

将以下片段合并到现有的 `config.json`（通常为 `~/.raven/config.json`），不要覆盖服务商或
渠道配置。使用 JSON，不是 YAML。将 `telegram:123456789` 替换为自己的已启用渠道与接收方：

```json
{
  "sentinel": {
    "enabled": true,
    "tick_interval_seconds": 1800,
    "task_discovery_targets": ["telegram:123456789"],
    "nudge_policy": {
      "max_nudges_per_hour": 1,
      "max_nudges_per_day": 4,
      "quiet_hours": [23, 7],
      "high_priority_bypasses_limits": false
    }
  }
}
```

本例采用比默认值更低的配额，并关闭高优先级豁免。随后启动网关；如果网关已经在运行，
则重启现有进程：

```bash
uv run raven gateway
```

如果部署已配置好接收目标与限制，也可以通过 CLI 开启：

```bash
uv run raven sentinel enable
```

启停开关与配额命令修改的是已保存配置，重启网关后才生效。配置命令须使用与网关相同的
`RAVEN_HOME`。`sentinel status` 显示的是已保存设置，不能证明运行中的进程已重新加载它们。

### 接收目标 { #delivery-targets }

虽然名为 `sentinel.task_discovery_targets`，该字段也为指向内部 `sentinel:direct` 目标的
普通提醒提供接收地址，例如日计划提醒。任务发现关闭时仍会使用它。

- `"channel:chat_id"`：指定接收方；一个渠道有多个会话时优先使用。
- `"channel"`：触发时解析该渠道最近的接收方。
- `"*"`：展开为网关中已启用的渠道，并解析各渠道最近的接收方。

列表为空时，每日任务发现批处理没有接收目标。普通提醒仍可指向具体会话。对于
`sentinel:direct` 等内部目标，如果配置的目标未解析出任何接收方（包括列表为空的情况），
runner 会回退到最近活跃的单个会话，而不是广播。如果仍无法解析出合适的会话，提醒便没有
投递目标。网关没有相应出口时，字面指定的 `tui` 目标不会被自动转发。建议先确认一个明确
接收方可用，再选择广播。

### 频率与免打扰时段 { #frequency-and-quiet-hours }

`sentinel` 下的常用配置：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `tick_interval_seconds` | `1800` | 评估间隔，单位秒；最小为 `60` |
| `evaluator_model` | `null` | 继承主模型，或指定 Planner 模型 |
| `nudge_policy.max_nudges_per_hour` | `3` | 基础小时配额；自适应与周末因子可改变实际限额 |
| `nudge_policy.max_nudges_per_day` | `10` | 策略的每日上限；高优先级也不能绕过 |
| `nudge_policy.quiet_hours` | `[23, 7]` | 按运行主机本地时间计算的免打扰窗口 |
| `nudge_policy.high_priority_bypasses_limits` | `true` | 允许高优先级绕过免打扰与小时配额，仍受策略检查约束 |
| `nudge_policy.min_interval_seconds` | `300` | 同一会话的最小提醒间隔 |
| `inject_enabled` / `defer_enabled` | `true` / `true` | 接入回复追加与延迟提醒；关闭其中一项后，对应决策不执行 |

不编辑 JSON 也可以修改两项配额：

```bash
uv run raven sentinel config set --max-nudges-per-hour 1 --max-nudges-per-day 4
```

修改后需重启。这些是 Sentinel 的策略限制，不是账户级费用上限。用户安排的 Cron 任务绕过
策略检查；接入共享账本时，它们的触发仍会消耗 Sentinel 看到的计数。

对于已入队消息，策略并不是严格的发送时刻限制。追加提醒在入队时检查并计入配额；延迟提醒
在登记时检查，但目前缺少发送时重新检查和记录配额的回调。若要让 Sentinel 提醒避开免打扰
窗口，还需关闭 inject、defer 与高优先级豁免，或保持 Sentinel 关闭。这不会让 Cron 或
已在运行的任务静默。详见[策略边界](proactivity-design.md#policy-boundaries)。

## 可选的任务发现 { #optional-task-discovery }

任务发现会提出带编号的候选任务菜单，需要单独启用。将以下字段合并到同一个 `sentinel`
对象中，并保留接收目标：

```json
{
  "task_discovery_enabled": true,
  "task_discovery_time": "08:00",
  "task_discovery_require_confirm": true
}
```

每日批处理在配置的本地时间之后的某个 Sentinel 节拍运行，不保证精确到分钟。目标列表默认
为空，因此只开启任务发现不会投递菜单。菜单默认最多四个选项，60 分钟后过期。

在同一会话回复 `/pick N`，即可通过确定性规则选择选项，无需调用分类器。纯数字或自然语言
选择需要为分类器配置模型服务商和模型，且分类结果需达到置信度要求。没有该分类器时，请用
`/pick N` 选择选项。如被要求确认，再回答确认问题。所选任务可以启动
Agent 工作、调用工具、派发子 Agent 或确认学习到的例行任务。确认设置只适用于菜单选择，
并不是每条 Planner `spawn_agent` 决策的审批步骤。

## 观察与排障 { #inspect-and-troubleshoot }

以下命令查看已保存设置或持久化状态：

```bash
uv run raven sentinel status
uv run raven sentinel nudges
uv run raven sentinel decisions
uv run raven sentinel routines
uv run raven sentinel attention
uv run raven sentinel behaviors
```

启用 Sentinel 后，可以查看一次规划决策：

```bash
uv run raven sentinel tick --dry-run
```

Dry-run 会关闭提醒执行与任务发现，但仍运行规划和状态维护，可能调用模型并刷新派生状态。
它不是只读检查，也不是免费的预览。CLI 的 `--live` 模式为 dispatcher 的输出使用无真实渠道
的接收端，包括普通提醒和任务发现菜单，因此不能用于验证实际渠道投递。

| 现象 | 检查项 |
| --- | --- |
| 已保存设置显示启用，但没有运行 | 重启网关并查看启动输出；确认使用同一个配置主目录 |
| 节拍返回 `skip` | 通常是正常结果：免打扰时段、上下文未变或没有值得做的动作；查看 `reason` 与 `route` |
| 有决策却没收到消息 | 检查接收方、渠道是否启用、策略拒绝、队列过期，以及 `no_delivery_target` / `degraded:...` 结果 |
| 没有任务菜单 | 检查独立的任务发现开关、目标是否非空、本地时间、配额与菜单过期时间 |
| 没有例行任务或行为记录 | 例行学习需要足够的可解析历史；行为提取默认单独关闭 |
| 提醒过于频繁 | 降低配额，检查高优先级豁免，并单独检查 Cron |

在同一会话收到近期提醒后回复 `/dismiss`，会记录拒绝并进入冷却期。普通回复记为中性，
不会自动算作接受。这两种回复都不是全局停止命令。

## 成本与安全限制 { #costs-and-safety-limits }

进入 Planner 的节拍会发起模型请求，服务商重试可能增加请求次数；快路径可以跳过该请求。
任务发现、可选的每日分析、例行验证、行为提取，以及派发的任务，都可能产生额外模型或工具
开销。30 分钟的间隔并非总成本上限。

Planner 可能接收记忆、近期对话片段、选定的 `attention.md` 章节与折叠后的行为事件。
运行时状态与反馈会跨重启保留；文件及职责见
[状态与反馈](proactivity-design.md#state-and-feedback)。

派发的任务使用所选子 Agent 后端。内置 `raven-loop` 后端有迭代上限，并且不提供消息发送
与递归派发工具。但 `tools.restrict_to_workspace` 默认为 `false`，ProactiveSpawn 不会
强制开启它，也不提供整体任务超时。外部 ACP 与 CLI Agent 作为宿主机进程运行。
隔离的覆盖范围与限制见[沙箱](sandbox.md)。

## 关闭 Sentinel { #disable-sentinel }

```bash
uv run raven sentinel disable
```

重启网关后，其 Sentinel 栈才会停止。单独执行该命令不会停止运行中的栈、取消已经派发的任务、
删除状态，或关闭 Cron 与 Heartbeat。需要立即停止时，请停止运行中的网关，并单独检查残留的
外部 Agent 进程。关闭开关不会清空持久化队列，再次启用前应检查它们。

## 实现参考 { #implementation-reference }

本页原有的实现章节已移至[主动性设计与实现](proactivity-design.md)。旧章节链接会定位到
下面的对应入口：

- <span id="architecture-overview"></span>
  [架构与组装](proactivity-design.md#components-and-assembly)
- <span id="1-data-types-sentineltypespy"></span>
  <span id="plannerdecision"></span>
  <span id="plannercontext"></span>
  <span id="3-context-assembly-contextassembler-sentinelpredictorcontext_assemblerpy"></span>
  <span id="4-decision-layer-proactiveplanner-sentinelplannerpy"></span>
  [数据类型、上下文组装与 Planner 决策](proactivity-design.md#context-and-decision-contracts)
- <span id="2-orchestration-sentinelrunner-sentinelexecutorrunnerpy"></span>
  <span id="one-tick"></span>
  <span id="fast-path-rules-skip-only"></span>
  <span id="scheduled-fire-fast-path"></span>
  <span id="drive-modes"></span>
  <span id="tickoutcome"></span>
  [Runner 生命周期、快路径与节拍结果](proactivity-design.md#tick-lifecycle)
- <span id="action"></span>
  <span id="degradation"></span>
  <span id="6-the-three-nudge-execution-paths"></span>
  <span id="nudgedispatcher-sentinelexecutordispatcherpy"></span>
  <span id="nudgeinjector-sentinelexecutorinjectorpy"></span>
  <span id="defermanager-sentinelexecutordefer_managerpy"></span>
  <span id="7-the-spawn_agent-path-proactivespawn-sentinelexecutorspawnpy"></span>
  [动作、提醒执行与主动派发](proactivity-design.md#action-routing)
- <span id="5-the-gate-nudgepolicy-sentineltrigger_policypolicypy"></span>
  <span id="layered-checks"></span>
  <span id="adaptive-multiplier"></span>
  <span id="readwrite-split"></span>
  <span id="personalization-and-persistence"></span>
  [策略检查、自适应与配额记录](proactivity-design.md#policy-boundaries)
- <span id="8-feedback-loop-nudgefeedbacktracker-the-nudge-feedback-tool"></span>
  <span id="9-state-files"></span>
  [状态文件与反馈](proactivity-design.md#state-and-feedback)
- <span id="12-task-discovery-anticipatory-menus"></span>
  [例行学习与任务发现](proactivity-design.md#routines-and-task-discovery)
- <span id="10-cron-schedulerscron"></span>
  <span id="11-heartbeat-and-event-driven-wake"></span>
  <span id="13-spine-integration-and-the-user-inbound-gates"></span>
  <span id="mid-turn-user-input-busypolicyinject"></span>
  <span id="ask_user-pausing-a-turn-to-ask-the-user"></span>
  [Cron、Heartbeat、Spine 集成与轮次控制](proactivity-design.md#cron-heartbeat-and-the-spine)
