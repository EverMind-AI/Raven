# 主动性设计与实现

本文面向扩展或调试 Proactive Engine 的开发者，是站点中的实现参考，设计理由与其解释的契约
放在一起。配置、成本与操作步骤请阅读[主动性使用指南](proactivity.md)。

除非另有说明，以下路径均相对于 `raven/proactive_engine/`。配置类型定义在
`raven/config/raven.py`，运行时规范术语定义在 `CONTEXT.md`。

## 设计理由 { #design-rationale }

目标是在合适时提供有用的后续工作，而不是把每条观察都视为可以打扰用户或采取行动的授权。
以下三个选择决定了实现方式：

- **决策与执行分离。** Planner 读取组装后的上下文，返回结构化决策；执行器负责投递与
  任务派发。因此可以在不启动后台任务的情况下测试决策。
- **按场景选择打扰方式。** 保持安静、独立提醒、追加到回复、延迟提醒与后台任务，对活跃
  对话的影响各不相同。
- **复用运行时服务。** 策略、持久化反馈、Spine 与 SubagentManager 提供共享机制，
  Sentinel 不再实现第二套运行工具的 Agent 循环。

基于例行的辅助与上下文感知的前瞻行为是预期用途，不是保证兑现的能力。例如，记忆中的截止期
可用于判断是否提醒，近期部署记录可用于提出状态检查。是否实际发生取决于可用上下文、模型
输出、策略与工具。学习到一个模式，并不等于用户已经创建了 Cron 计划。

## 组件与组装 { #components-and-assembly }

| 职责 | 实现 |
| --- | --- |
| 栈构建与钩子 | `raven/core/proactive_stack.py` |
| 生命周期、节拍与路由 | `sentinel/executor/runner.py` |
| 上下文组装 | `sentinel/predictor/context_assembler.py`（`PlannerContextAssembler`） |
| 决策与校验 | `sentinel/planner.py`、`sentinel/types.py` |
| 工具 schema 与上下文渲染 | `sentinel/trigger_policy/prompts.py` |
| 限制与偏好 | `sentinel/trigger_policy/`（`policy.py`、`prefs.py`） |
| 提醒投递、回复追加与延迟 | `sentinel/executor/`（`dispatcher.py`、`injector.py`、`defer_manager.py`） |
| 主动任务派发 | `sentinel/executor/spawn.py` |
| 例行学习与任务发现 | `sentinel/predictor/` |
| 持久化与反馈 | `sentinel/feedback/`、`sentinel/state_files.py` |
| 派生注意力状态 | `sentinel/attention_updater.py`、`sentinel/attention_producers/` |

Sentinel 关闭时，`build_sentinel_stack()` 返回未启用结果；开启时则构建共享存储、策略、
Planner、执行器与 runner。网关在 DeliveryHub 创建后绑定 dispatcher 的 `post` 回调；
`attach_sentinel_spawn()` 与 `attach_sentinel_decision_consumer()` 接入依赖 Agent 的执行能力。

Planner 默认使用主 Agent 模型，可通过 `evaluator_model` 覆盖；`evaluator_base_url` 与
`evaluator_api_key_env` 可选择独立服务商。如果指定的 API key 环境变量为空，组装过程会告警
并回退到主服务商。系统提示词通过 `raven.i18n` 加载；工具 schema 与上下文渲染器仍位于
`trigger_policy/prompts.py`。

## 上下文与决策契约 { #context-and-decision-contracts }

`PlannerContext` 是 Planner 的输入。`PlannerContextAssembler` 收集以下内容：

| 上下文 | 来源 |
| --- | --- |
| `memory_md`、`history_md_recent` | 长期记忆与近期历史尾部 |
| `active_sessions` | 近期活跃会话及其最后一条用户和助手消息 |
| `routines` | 确定性的历史模式学习 |
| `calendar` | 调用方可选提供的日历函数；不代表内置了日历集成 |
| `nudge_policy_state`、`fire_history` | 策略计数、近期话题触发与拒绝记录 |
| `last_decision` | runner 记住的上一次决策 |
| `attention_md`、`behaviors_recent` | 选定的注意力章节与折叠后的行为窗口 |

来源缺失时通常返回空字段，而不是中止组装。记忆过滤、注意力章节选择与行为窗口均可配置；
Planner 不会自行搜索其他数据源。

`ProactivePlanner.decide()` 请求 `planner_decision` 工具调用，将非法动作归为 `skip`，
非法优先级归为 `low`，并把分数限制到 `[0, 1]`。提醒动作需要 `nudge_message`；
延迟提醒还需要 `defer_condition`；派发任务需要 `spawn_task`。缺少必要载荷时决策降级为
`skip`。`topic_tag` 用于按话题执行策略；模型未提供时，Planner 会派生一个兜底标签。

服务商返回错误、未返回工具调用或参数不是字典时，转为 `skip`。抛出的异常由 runner 处理，
而不是被 Planner 吞掉。结构化输出约束动作格式，并不能证明提议的动作正确或安全。

## 节拍生命周期 { #tick-lifecycle }

`await tick_once()` 组装上下文后调用 `await tick_with_context(ctx)`，后者执行以下步骤：

1. 到期时裁剪反馈，并重新调整策略。
2. 刷新派生记忆状态；任务发现已开启且到期时，运行任务发现。
3. 检查每日触发计划中的周期性槽位；若满足条件，不调用 Planner，直接路由预先准备的消息。
4. 对免打扰时段，或上次为 skip 且上下文未变的情况应用仅跳过的快路径。到期的高优先级
   截止期会绕过这些捷径，以进入 Planner。
5. 请求 Planner 并路由决策。若抛出异常且兜底已启用，尝试带守卫的高优先级截止期兜底；
   否则返回错误 skip。
6. 记住决策供下一个节拍使用。

一次性的截止期槽位通常交给 Planner，以便从近期上下文判断用户是否已经完成工作。
故障兜底无法作出这一判断，因此仅限到期的高优先级截止期槽位，并仍走正常路由策略。
返回 `skip` 与抛出异常不同，前者不会触发该兜底。

`start()` / `stop()` 管理周期循环、延迟提醒循环，以及接入时的发现触发器消费循环。
触发器消费者按短周期轮询独立的文件存储，不等待下一个 Planner 节拍。runner 会记录意外的
后台异常并继续运行。`TickOutcome` 为诊断提供决策、执行结果、路由、可选的提醒标识与备注。

## 动作路由 { #action-routing }

| 动作 | 路径 | 何时视为已派发 |
| --- | --- | --- |
| `skip` | 不使用执行器 | 不派发 |
| `nudge` | 策略检查、目标解析、NudgeDispatcher → `DeliveryHub.post` | dispatcher 报告投递成功后 |
| `nudge_inject` | 策略检查、NudgeInjector 入队 | 入队时，早于用户收到回复 |
| `nudge_defer` | 策略检查、DeferManager 登记 | 登记只是待处理，不是投递 |
| `spawn_agent` | ProactiveSpawn 策略检查 → SubagentManager | 任务派发时，而非任务完成时 |

普通提醒与任务发现菜单直接投递给 DeliveryHub。它们是已生成的消息，不是应交给带工具的
Agent Loop 执行的提示词。这样既保留菜单格式，也避免 Agent 把提醒当作新的用户请求执行。

NudgeInjector 通过响应修饰钩子，将待发文字追加到符合条件的回复中，并设有过期时间与按会话
的 FIFO 上限。DeferManager 等待目标会话达到空闲阈值，超过最大等待时间则丢弃条目，并在
触发时解析接收目标。它不会让 LLM 评估 `defer_condition`，而是仅按空闲时间判断。

ProactiveSpawn 校验任务，以任务文字作为去重内容检查共享策略，然后调用 SubagentManager。
完成结果以 `SUBAGENT` 来源轮次返回发起会话，不经过 NudgeDispatcher。它不增加独立配额
或整体任务超时。后端执行与隔离限制见[使用指南](proactivity.md#costs-and-safety-limits)。

执行器未接入时，返回降级、未投递结果。尤其是关闭 inject 或 defer，不会把对应决策转换为
普通提醒。

## 策略边界 { #policy-boundaries }

`NudgePolicy.check()` 检查免打扰时段、学习得到及用户指定的免打扰窗口、每日/小时限制、
会话与拒绝冷却、话题反馈、内容去重及滚动话题配额。高优先级可以绕过部分软限制，但不能
绕过每日上限、冷却或话题限制。普通的用户指定免打扰窗口也受高优先级豁免设置影响，
并非无条件阻止发送。

自适应调节根据反馈调整小时乘数，周末因子可以进一步收紧。偏好覆盖只能收紧静态策略。
策略状态会持久化，因此重启不会重置配额。

检查与记录是分开的操作。不要假定所有路径在同一时刻消耗配额，也不要把持久化账本理解为
原子的“检查并预留额度”事务：

- 普通提醒在报告投递成功后记录；追加提醒在入队时记录；主动派发在派发后记录。
  入队或派发并不证明用户已看到结果。
- 延迟提醒在登记时检查策略，但当前 runner 没有接入记录最终触发的回调。DeferManager
  在空闲检查后直接投递，不重新检查策略，因此没有完整的发送时配额或免打扰保障。
- 任务发现菜单受策略约束。用户选择后的执行是独立路径，并非每种动作都再经 ProactiveSpawn。
- Cron 是用户显式安排的任务，绕过 `check()`。接入 Sentinel runner 时，成功触发会更新
  共享计数与话题账本。这有助于抑制重叠的 Sentinel 提醒，但不能保证所有消息语义上绝不重复。

## 状态与反馈 { #state-and-feedback }

默认运行时状态位于 `~/.raven/sentinel/`，随 `RAVEN_HOME` 迁移。文件名定义在
`sentinel/state_files.py`。

| 文件 | 职责 |
| --- | --- |
| `state.json` | 共享策略账本、待处理的追加与延迟提醒，以及互动状态 |
| `feedback.jsonl` | 用于自适应调节的派发与反馈事件 |
| `pending_decisions.json` | 任务发现菜单、过期与确认状态 |
| `routines.json` | 学到的例行任务及其持久化确认状态 |
| `discover_triggers.json` | 由运维发起、runner 消费的任务发现请求 |

`JsonStateStore` 对 JSON 的读改写使用 `fcntl` 锁与原子重命名。使用同一状态目录的进程
共享这些文件，这不代表每个聊天接收方都有独立配额。

在所配置的 Agent 主目录中，`user_memory/attention.md` 保存派生章节。AttentionUpdater
在文件锁外计算生产者输出，在锁内拼接章节，跳过未变化内容并隔离生产者故障。
可选的每日分析会让多个生产者复用一个 LLM 结果。`user_memory/behaviors.md` 保存提取出的
行为事件。每日分析与行为提取默认关闭。

当前反馈钩子将近期提醒后的 `/dismiss` 回复记为拒绝，并触发会话冷却。未分类的回复记为
中性，不会自动视为接受。任务发现的选择与确认分别记录反馈；长期未获回应的提醒可以形成
忽略信号。

## 例行学习与任务发现 { #routines-and-task-discovery }

RoutineLearner 按星期与时间段对带时间戳的历史分组，不调用 LLM，仅提取关键词。
无法解析的行会跳过，历史不足时不产生候选项；近期权重使当前模式优先。RoutineStore 刷新时
保留确认状态；确认会把候选项提升为 active，拒绝则将其置为 retired，经过冷却后才可再次
提议。仅仅没有回应，不会让例行任务变成已确认状态。

开启后，TaskDiscoverer 刷新候选项、按配置验证、合并整理后生成 `PendingDecision` 菜单。
PendingDecisionStore 管理过期、取代与确认状态。DecisionRouter 确定性匹配数字和
`/pick N`，并可使用模型分类器兜底。DecisionConsumer 在普通 Agent 轮次继续前处理匹配的回复。

完成所配置的确认步骤后，ActionExecutor 按类别派发：

- `reply`：将选中的提示词提交为带 `sentinel.action_origin` 的用户意图轮次，而不是使用
  NudgeInjector 追加。菜单选择钩子提交后不能等待同一 Lane 上的轮次，否则会自锁。
- `tool`：调用已注册工具。
- `spawn`：直接交给 SubagentManager。
- `routine_confirm`：提升例行任务状态；载荷有要求且接入 CronService 时，可创建 Cron 任务。

确定性选择避开的是普通对话的 LLM 路径；分类、确认或所选任务本身仍可能调用模型。

## Cron、Heartbeat 与 Spine { #cron-heartbeat-and-the-spine }

CronService（`schedulers/cron/service.py`）在文件锁下持久化任务、认领到期工作，然后在
锁外执行。归属遵循 **Fire-at-origin**：由任务创建时绑定的渠道/接收方所对应的 runner
执行与投递，不在触发时转发或广播。`raven/core/cron_stack.py` 将工作以 `CRON` 轮次
提交到 `cron:<job_id>`。

周期任务成功触发会增加 `silent_fire_count`，匹配的用户活动会将其重置。
达到 `silent_fire_limit`（默认 `12`）后任务被禁用。这里计数的是没有用户活动的触发，
不是失败次数。Cron 的反馈记录记为中性，不会拉低 Sentinel 学习到的接受率。
固定延迟的 `every` 计划从完成时刻计算下一次运行时间，而不是从上一次应触发时刻计算。

HeartbeatService（`schedulers/heartbeat/service.py`）通过结构化模型决策检查
`HEARTBEAT.md`，仅在结果为 `run` 时执行 Agent 工作。`wake.py` 合并提前唤醒请求、
限制频率，并在用户工作繁忙时延后。Wake 驱动 Heartbeat，不驱动 Sentinel 的节拍循环。

Spine 的 Scheduler 将轮次路由到按会话划分的 Lane 与按来源划分的并发池；
DeliveryHub 将输出投递到出口。用户入站与响应修饰钩子区分真实用户轮次和系统来源工作。
用户确认后的任务发现动作带独立标记，避免对一次选择重复计数。

通用轮次控制不是 Sentinel 专属功能：`BusyPolicy.INJECT` 处理轮次进行中的用户输入，
`ask_user` 与 QuestionBroker 处理结构化提问。见[架构](architecture.md)；
实现入口为 `raven/spine/scheduler.py`、`raven/agent/loop/main.py` 与
`raven/rpc/question_broker.py`。

## 验证入口 { #verification-entry-points }

决策行为见 `tests/test_sentinel_planner.py` 与 `tests/test_sentinel_fast_path.py`；
路由与策略见 `tests/test_sentinel_runner.py`、`tests/test_nudge_policy.py` 与
`tests/test_proactive_spawn.py`。组装测试在 `tests/test_core_sentinel_stack.py`，
Cron 账本集成测试在 `tests/test_core_cron_stack_ledger.py`，运维命令测试在
`tests/test_cli_sentinel_commands.py`。通过 `uv run pytest` 运行测试，使用注入的时钟
与模拟服务商验证契约，不要用真实通知验证。
