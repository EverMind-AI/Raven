# 主动性参考

这是 Raven 主动性子系统的竣工参考：代码实际做了什么，并附上可直接打开的模块路径。
其配套文档[主动性设计](proactivity-design.md)描述的是设计意图。

Raven 的主动性 = 周期性节拍 + LLM 决策 + 对用户状态的感知——而不是一个由用户设定的定时器。
要点如下：

1. 两个来源：Sentinel（由 LLM 在每个节拍决定是否以及如何触达用户）与 Cron（由用户显式
   安排的提醒）。两者都以带来源标记的轮次经由 spine 抵达代理，并共用同一本 `NudgePolicy`
   账本，因此绝不会就同一话题重复提醒。
2. 一个决策是五种结构化动作之一：`skip`、`nudge`、`nudge_inject`、`nudge_defer`、
   `spawn_agent`。其中 `nudge_inject`（搭下一条回复的便车）与 `nudge_defer`（等当前话题
   告一段落再说）正是让代理感知用户此刻在做什么的关键。
3. 决策只读取一份打包好的上下文 `PlannerContext`。Planner 是纯函数，任何失败都降级为
   `skip`；它绝不抛出异常。
4. 一道共用的防打扰闸门 `NudgePolicy`，其松紧程度从用户反馈中学习。每个 nudge 执行器
   以及任务发现都要经过它。
5. 状态是持久化的，不是每个节拍重建：派生的决策信号落在 `user_memory/attention.md`，
   长期行为落在 `behaviors.md`，运行时状态落在一个带 `fcntl` 锁的 `state.json`。
   REPL 与网关共用这份状态。
6. 默认关闭（`sentinel.enabled=false`），需要显式启用。

**模块根目录**：`raven/proactive_engine/`，其下有 `sentinel/`（决策与执行子系统）、
`schedulers/cron/` 与 `schedulers/heartbeat/`（两个由定时器驱动的服务），以及 `wake.py`
（事件驱动的提前唤醒）。

**`sentinel/` 的子目录**：`predictor/`（ContextAssembler、RoutineLearner、RoutineStore、
TaskDiscoverer、DailyAnalysisService）、`executor/`（Runner、NudgeDispatcher、
NudgeInjector、DeferManager、ProactiveSpawn、PendingDecisionStore、DecisionRouter、
DecisionConsumer、ActionExecutor）、`feedback/`（NudgeFeedbackTracker、JsonStateStore）、
`trigger_policy/`（NudgePolicy、ProactivityPreferencesReader、提示词）、`tools/`
（nudge-feedback 工具）、`attention_producers/`（attention.md 的各个生产者及其基类），
以及顶层的 `attention_updater.py` / `discover_triggers.py`。

---

## 架构总览 { #architecture-overview }

主动性有两个来源：Sentinel（LLM 决策）与 Cron（用户安排），两者都以带来源标记的轮次
提交给 spine 的 `Scheduler` 后抵达代理。系统中没有消息总线。

```
SentinelRunner (tick loop)
  ContextAssembler.assemble()                    -> PlannerContext
    MemoryStore.read_long_term()                 -> memory_md
    history file tail                            -> history_md_recent
    RoutineLearner.learn(history)                -> routines
    SessionManager.sessions (active window)      -> active_sessions
    NudgePolicy.snapshot_state()                 -> nudge_policy_state
    attention.md selected sections               -> attention_md
    behaviors.md folded window                   -> behaviors_recent
    NudgePolicy ledger                           -> fire_history

  fast-path rules (skip-only)                    -> Decision | None
    quiet hours hard hit -> skip
    unchanged-context dedup -> skip

  scheduled-fire (cron-style plan execution)     -> Decision | None

  ProactivePlanner.decide(ctx)                   -> PlannerDecision
    tool call: planner_decision(...)
    5 actions: skip | nudge | nudge_inject | nudge_defer | spawn_agent

  _route(decision):
    skip         -> record tick, return
    nudge        -> NudgePolicy.check -> NudgeDispatcher.dispatch
    nudge_inject -> NudgePolicy.check -> NudgeInjector.queue
    nudge_defer  -> NudgePolicy.check -> DeferManager.register
    spawn_agent  -> ProactiveSpawn.dispatch (its own policy check inside)

  JsonStateStore (fcntl + atomic rename) <- NudgePolicy / NudgeInjector / DeferManager
  DeliveryHub.post(...)                  -> channel outlet
  NudgeFeedbackTracker                   <- engagement signals


CronService (timer loop, sleep capped so peer-process job edits are seen)
  _on_timer():
    fcntl lock on jobs.json.lock
    filter by allowed_channels + claim unclaimed jobs
    save claim, release lock
    execute job out of lock -> on_cron_job callback
      submit(TurnRequest(origin=CRON, ...)) -> run_turn -> hub delivery / broadcast
```

核心设计决策：

1. 纯函数决策层：Planner 是 `(ctx, provider, model) -> Decision`，没有副作用；任何失败
   都降级为 `skip`，且绝不抛出异常。
2. 结构化工具调用：Planner 通过 `planner_decision` 工具 schema 返回五种动作之一。
3. 五种动作、三个 nudge 执行器外加 spawn：比「跑还是不跑」的二元判断更细粒度，
   并且感知用户的当前状态。
4. 一道共用的 NudgePolicy 闸门：三个 nudge 执行器和任务发现都使用它。
5. spine 是唯一的传输通道：主动消息投递到 DeliveryHub；主动来源向 Scheduler 提交带来源
   标记的轮次。

---

## 1. 数据类型（`sentinel/types.py`） { #1-data-types-sentineltypespy }

### Action { #action }

```python
Action = Literal[
    "skip",            # nothing worth doing this tick
    "nudge",           # send a standalone message NOW
    "nudge_inject",    # append to the agent's next reply in target_session
    "nudge_defer",     # wait until target_session's current thread settles
    "spawn_agent",     # dispatch a micro-agent for a multi-step task
]
```

### PlannerDecision { #plannerdecision }

```python
@dataclass
class PlannerDecision:
    action: Action
    reason: str = ""
    priority: Priority = "low"           # low | medium | high
    proactivity_score: float = 0.0       # 0-1 confidence
    target_session: str | None = None    # "channel:chat_id"
    nudge_message: str | None = None     # required for the three nudge actions
    spawn_task: str | None = None        # required for spawn_agent
    defer_condition: str | None = None   # required for nudge_defer
    raw_llm_response: dict | None = None
```

### PlannerContext { #plannercontext }

Planner 唯一的输入。生产环境中由 ContextAssembler 构建。主要字段：

- `memory_md` / `history_md_recent`：工作区的 MEMORY.md，以及历史文件的尾部。
- `active_sessions`：近期窗口内活跃的渠道会话（含各自最后一条用户/助手消息）。
- `routines`：来自 RoutineLearner 的候选重复模式。
- `calendar`：日历条目。
- `nudge_policy_state`：剩余配额与免打扰时段状态。
- `last_decision`：上一节拍的决策（使 Planner 不重复自己）。
- `fire_history`：Planner 近期就哪些话题触发过，以及用户是否忽略了它们。直接由内存中的
  NudgePolicy 填充（不用 LLM，不读磁盘）。
- `attention_md`：从 `attention.md` 中选取的若干段落构成的 markdown 块（不是整个文件）。
  选哪些段落由配置决定。
- `behaviors_recent`：取自 `behaviors.md` 尾部、每个事件折叠为一行的块。

最后这三项负责把子系统派生出的决策状态显式喂给 Planner；每一项都由 ContextAssembler
各自的辅助函数组装。Planner 提示词的渲染位于 `trigger_policy/prompts.py`。

---

## 2. 编排：SentinelRunner（`sentinel/executor/runner.py`） { #2-orchestration-sentinelrunner-sentinelexecutorrunnerpy }

### 一个节拍 { #one-tick }

```python
async def tick_once(self) -> TickOutcome:
    ctx = self.assembler.assemble()
    return await self.tick_with_context(ctx)

async def tick_with_context(self, ctx):
    self._maybe_cleanup_feedback()        # daily trim of the feedback JSONL
    self._maybe_retune_policy()           # adaptive NudgePolicy multiplier
    await self._refresh_memory_state()    # refresh attention.md + behaviors.md
    await self._maybe_run_task_discovery()# daily task-discovery batch
    scheduled = self._fast_path_scheduled_fire(now)   # cron-style plan execution
    if scheduled is not None:
        self.assembler.remember_last_decision(scheduled)
        return await self._route(scheduled)
    fast = self._fast_path_rules(ctx)     # skip-only rule short-circuit
    if fast is not None:
        self.assembler.remember_last_decision(fast)
        return TickOutcome(decision=fast, result=None, route="fast_path_skip")
    try:
        decision = await self.planner.decide(ctx)
    except Exception:
        if (fb := self._fallback_deadline_fire(now)) is not None:
            return await self._route(fb)  # high-priority deadline outage fallback
        decision = PlannerDecision(action="skip", reason="planner_error:...")
    self._warn_unfired_due_deadline(decision, now)
    outcome = await self._route(decision)
    if decision.action == "skip":
        setattr(decision, "_ctx_signature", self._context_signature(ctx))
    self.assembler.remember_last_decision(decision)
    return outcome
```

### 快路径规则（只产生 skip） { #fast-path-rules-skip-only }

有两条规则会短路掉 Planner 的 LLM 调用。两者都属于「拒绝是安全的」——它们只会产生
`skip`，绝不会误发提醒：

| 规则 | 条件 | 收益 |
|---|---|---|
| 免打扰时段 | `ctx.nudge_policy_state.in_quiet_hours` | 在静音窗口里跑 Planner 是浪费（NudgePolicy 反正会拒绝） |
| 上下文未变去重 | `last_decision.action == skip` 且上下文签名一致 | 两个节拍之间记忆、历史、会话都没变化，省掉第二次 LLM 调用 |

该签名是对 `memory_md`、`history_md_recent` 和活跃会话键求出的短字节哈希，作为动态属性
挂在决策对象上。skip 缓存有一个有界的 TTL，因此一个安静的人格不会把 `skip` 永久锁死、
把反馈闭环饿死。

### 计划触发快路径 { #scheduled-fire-fast-path }

一个日计划生产者会排出当天打算触发的主动消息。计划触发路径就是它的 cron 式执行器：
当某个节拍落在某个槽位的时间窗口内、且该话题当天尚未触发时，就直接发送预先规划好的消息，
不调用 LLM。周期性槽位直接触发；一次性的截止期槽位则交给 Planner 处理，因为只有 Planner
能读取近期历史，分辨「还没做」与「用户已经做完了」——这是语言层面的判断。若 Planner
不可用，一条带守卫的兜底路径可以盲发，但仅限高优先级的截止期槽位，并且仍要走正常的策略
闸门，以免在故障期间静默错过硬性截止期。

### 驱动方式 { #drive-modes }

- `start()` / `stop()`：runner 拥有自己的节拍循环，按固定间隔运行（默认 1800 秒 / 30 分钟），
  并并发运行 DeferManager 循环，在网关中还运行一个触发器消费循环。runner 不依赖唤醒
  调度器——事件驱动的唤醒是心跳的职责（见第 11 节）。
- `tick_once()` / `tick_with_context()`：单次同步节拍，供测试与基准适配器使用。

### 降级 { #degradation }

每一层都有包裹：ContextAssembler 失败变成 `skip`；Planner 失败先尝试截止期兜底、再转
`skip`；执行器失败返回一个未投递的结果，节拍继续。runner 绝不抛出异常——单次失败的节拍
绝不会破坏整个生命周期。

### TickOutcome { #tickoutcome }

```python
@dataclass
class TickOutcome:
    decision: PlannerDecision
    result: ExecutionResult | None   # None when skip or no executor fired
    nudge_id: str | None = None      # correlates the NudgeFeedbackTracker
    route: str = ""                  # which executor path was taken
    notes: list[str] = field(default_factory=list)
```

---

## 3. 上下文组装：ContextAssembler（`sentinel/predictor/context_assembler.py`） { #3-context-assembly-contextassembler-sentinelpredictorcontext_assemblerpy }

把每个信号源汇聚进 PlannerContext，并按字段做优雅降级：

| 字段 | 来源 | 来源缺失时 |
|---|---|---|
| `memory_md` | `MemoryStore.read_long_term()` | `""` |
| `history_md_recent` | 历史文件的尾部 | `""` |
| `routines` | `RoutineLearner.learn(history_md)` | `[]` |
| `active_sessions` | `SessionManager.sessions` 按活跃窗口过滤 | `[]` |
| `nudge_policy_state` | `NudgePolicy.snapshot_state()` | 默认对象 |
| `calendar` | 调用方注入的日历函数 | `[]` |
| `last_decision` | runner 的 `remember_last_decision()` | `None` |
| `attention_md` | `attention.md` 中选定的 H2 段落 | `""` |
| `behaviors_recent` | `behaviors.md` 折叠后的尾部 | `""` |
| `fire_history` | 内存中的 NudgePolicy 账本（不用 LLM，不读磁盘） | `{}` |

与 SessionManager 的耦合刻意保持松散（属性访问，不做类型假设）。

---

## 4. 决策层：ProactivePlanner（`sentinel/planner.py`） { #4-decision-layer-proactiveplanner-sentinelplannerpy }

生成参数是钉死的（较小的 max-tokens、较低的 temperature、不启用推理强度），因此全局的
服务商配置无法渗入并污染决策。

`decide()` 的流程：

1. 构造消息：系统提示词加上渲染后的上下文提示词。
2. 带上 `planner_decision` 工具调用服务商。
3. 每一条失败路径都降级为 `skip`（服务商报错、没有工具调用、工具参数不是字典）。
4. 字段校验与截断：非法的 `action` 变为 `skip`，非法的 `priority` 变为 `low`，
   超出范围的 `proactivity_score` 被截断到 `[0, 1]`。
5. 动作与字段的一致性守卫：缺少 `nudge_message` 的 nudge 动作、缺少 `defer_condition`
   的 `nudge_defer`、缺少 `spawn_task` 的 `spawn_agent`，一律降级为 `skip`。

第 5 步是关键防线：工具 schema 只强制要求 action / reason / score，因此那些按条件必填的
字段在这里统一把关，下游执行器就无需再做防御性检查。

提示词位于 `sentinel/trigger_policy/prompts.py`：系统提示词（Planner 的身份、五种动作的
语义，以及「除非明显值得，否则默认 skip」）、结构化工具 schema，以及把 PlannerContext
渲染为 markdown 的上下文提示词构造器。

---

## 5. 闸门：NudgePolicy（`sentinel/trigger_policy/policy.py`） { #5-the-gate-nudgepolicy-sentineltrigger_policypolicypy }

三个 nudge 执行器（dispatcher / injector / defer）与任务发现器在投递前都必须通过
`check()`。

### 分层检查 { #layered-checks }

`check()` 会依次运行一系列闸门，大致顺序如下：

| 层 | 规则 | 高优先级可否绕过？ |
|---|---|---|
| 1 | `action == skip` → 拒绝 | 不适用 |
| 2 | 免打扰时段（默认 23:00–07:00） | 可以（但当高优先级消息的接受率偏低时，这条豁免本身会被收回） |
| 2b | 从反馈中学习得到的按小时动态免打扰 | 可以 |
| 3 | 按人格划分的免打扰窗口 | 可以 |
| 4 | 按天配额 | 不可以（硬上限） |
| 5 | 按小时配额（受自适应乘数与周末乘数缩放） | 可以 |
| 6 | 按会话冷却期 | 不可以 |
| 7 | 忽略后的冷却期 | 不可以 |
| 8 | 按话题的加权硬拒绝冷却期 | 不可以 |
| 9 | 按话题的接受率闸门 | 不可以 |
| 10 | 窗口内的内容去重 | 不可以 |
| 11 | 按话题的滚动配额栈（小时 / 天 / 周） | 不可以 |

### 自适应乘数 { #adaptive-multiplier }

`apply_adaptive_tuning()` 由 runner 每个节拍调用，让小时配额的乘数随用户近期接受率对称
移动：高度参与的用户可以放宽到基线之上，低参与的用户则收紧到基线之下；其中有冷启动下限、
非对称的样本量闸门（放宽所需样本多于收紧），以及一条防抖滞回带。周末收紧器会在此之上
叠加一个独立因子。该乘数还会作为软信号出现在 Planner 提示词中，使 Planner 能提高自己的
价值阈值，避免发起注定会被拒绝的 LLM 调用。

### 读写分离 { #readwrite-split }

```python
verdict = policy.check(action, session_key, content, priority)
if verdict.verdict == "allow":
    await dispatch(...)
    policy.record_fired(action, session_key, content)
```

状态只在投递成功之后才写入，因此「拒绝 → 投递 → 报错 → 不扣配额」这种情形能被正确处理。

### 个性化与持久化 { #personalization-and-persistence }

该策略接受一个可选的覆盖函数。ContextAssembler 绑定了一个 `ProactivityPreferencesReader`，
它在每个节拍重新读取用户偏好，因此从「学习到的偏好」到「生效的闸门」这条链路始终是最新的。
覆盖只能朝收紧方向生效（用户偏好可以拉长免打扰窗口，绝不能缩短）。

当以 `JsonStateStore` 构造时，策略会在每次检查前从磁盘加载状态，并在每次触发后原子写回。
NudgePolicy、NudgeInjector 与 DeferManager 共用同一个存储实例，因此所有修改都经过同一把
`fcntl` 锁，REPL 与网关不会互相撕裂对方的状态。

一个 `now_fn` 注入点让测试可以在冻结时钟下驱动配额窗口、去重 TTL 与冷却期。

---

## 6. 三条 nudge 执行路径 { #6-the-three-nudge-execution-paths }

### NudgeDispatcher（`sentinel/executor/dispatcher.py`） { #nudgedispatcher-sentinelexecutordispatcherpy }

dispatcher 是无状态的（限流与目标解析由调用方负责），它向 spine 的 DeliveryHub 投递。
hub 的 `post` 可调用对象通过 `set_post` 后期绑定，因为 hub 是在运行中的循环里、于
dispatcher 之后构建的。

- `dispatch(decision, targets)` 处理 `action == nudge`。它向每个解析出的
  `(channel, chat_id)` 投递一个 `Text`，并带上 `source.extras._sentinel_origin=True`。
  它走的是 hub 的非轮次 `post`，因此用户收到的是一条独立的主动消息——它绝不会重新进入
  带工具的代理循环，代理因此也无法「就提醒采取行动」（伪造一份交付物或把截止期标记为
  已完成）。真实渠道出口原样投递内容；交互式 CLI 出口会读取 `_sentinel_origin`，
  以加上一个主动消息的前缀标记。
- `dispatch_options(decision)` 处理任务发现菜单。它把 PendingDecision 渲染成 markdown
  菜单并以同样方式投递。该菜单本身就是面向用户的成品（带编号的选项、一行说明、
  「回复编号即可」），因此原样投递是有意为之——让它过一遍代理只会被改写并破坏格式。
  用户的选择会在抵达 LLM 之前，由下游的 DecisionConsumer 钩子截获。

### NudgeInjector（`sentinel/executor/injector.py`） { #nudgeinjector-sentinelexecutorinjectorpy }

`action == nudge_inject` 会把消息入队，并作为代理的 `response_modifier` 被消费：
`NudgeInjector.__call__(session_key, content) -> content` 取出该会话的待发消息，
并追加到代理即将发出的回复上。代理循环在其 `after_send` 链中应用这一步，并对系统来源的
轮次跳过，以免一条主动回复被再叠加一层 nudge。

约束：TTL 会丢弃过期条目，按会话的上限会按 FIFO 淘汰最旧的条目，待发队列通过共用的
`JsonStateStore` 持久化，因此 REPL 与网关不会重复消费或丢失一次注入。

两者之间的接缝只是一个普通的 `Callable[[str, str], str]`：代理循环不导入 Sentinel，
Sentinel 也不导入代理循环。

### DeferManager（`sentinel/executor/defer_manager.py`） { #defermanager-sentinelexecutordefer_managerpy }

`action == nudge_defer` 把决策登记到一个优先级堆上，待目标会话安定下来后再（沿同一条
nudge 路由）投递。「安定」基于时间判定：该会话空闲达到阈值。一个最大等待时长限定了
一个被推迟的决策在过期前最多能滞留多久。该堆通过共用的 `JsonStateStore` 持久化，
因此待发的推迟项可以跨重启存活（唯一未被序列化的是可选的投递回调，而它只是埋点）。

---

## 7. spawn_agent 路径：ProactiveSpawn（`sentinel/executor/spawn.py`） { #7-the-spawn_agent-path-proactivespawn-sentinelexecutorspawnpy }

`action == spawn_agent` 包装 `SubagentManager.spawn(...)`，运行一个独立的微代理来完成
多步任务（一份摘要、一次状态检查）。`dispatch()` 的步骤：

1. 校验 `action == spawn_agent` 且 `spawn_task` 非空；
2. 通过自己的 NudgePolicy 检查，复用共享的配额与去重（以 spawn 任务作为内容哈希的键）；
3. 把目标会话拆分为 channel 与 chat_id；
4. 派生微代理；结果经由 NudgeDispatcher 投递回发起的渠道。

spawn 使用自己的配额线（因此不会抢占响应式 nudge 的配额），但共用同一套去重，
因此同一个 spawn 任务不会在短时间窗口内重复。

---

## 8. 反馈闭环：NudgeFeedbackTracker 与 nudge-feedback 工具 { #8-feedback-loop-nudgefeedbacktracker-the-nudge-feedback-tool }

NudgeFeedbackTracker（`sentinel/feedback/tracker.py`）把每条 nudge 的生命周期
（dispatched / accepted / dismissed / neutral）记入 JSONL 日志，runner 每个节拍读取其
近期接受率，用以重新调校 NudgePolicy 的乘数。

判定来自一个工具（`sentinel/tools/`）：当用户回复时，主 LLM 可以调用
`nudge_feedback(verdict, nudge_id, reason)`，把一条已投递的 nudge 标记为接受、忽略或中性。
这不产生额外的 LLM 调用（主轮次本来就要跑），也避免了「除非用户说停，否则默认算接受」
这种脆弱的启发式——后者会把一句明确的「别再提醒我」算成接受。

用户入站钩子通过上下文变量发布一个按轮次的会话键，使该工具无需改动代理的 tool-execute
签名就能找到当前会话。

---

## 9. 状态文件 { #9-state-files }

该子系统把派生状态持久化，而不是每个节拍重建。

- `attention.md`（位于 `user_memory/` 下）：一组生产者（`sentinel/attention_producers/`）
  各自拥有一个 H2 段落，由 `AttentionUpdater`（`sentinel/attention_updater.py`）在每个
  节拍分两阶段维护（锁外计算、锁内拼接），并带有「无变化则跳过」的比较，以及按生产者的
  失败隔离。一个每日分析服务（`sentinel/predictor/daily_analysis.py`）每天做一次 LLM
  调用，其结果由多个生产者共享。多数生产者是纯算法；依赖 LLM 的那些以及每日分析默认关闭。
- `behaviors.md`（位于 `user_memory/` 下）：一个由空闲触发的 LLM 提取器把会话消息转成
  结构化的行为事件；Planner 读取其尾部折叠后的窗口。该提取器默认关闭。
- `state.json`（位于 sentinel 数据目录下）：由 NudgePolicy、NudgeInjector 与 DeferManager
  共用的运行时账本，在同一把 `fcntl` 锁下以原子重命名写入。

---

## 10. Cron（`schedulers/cron/`） { #10-cron-schedulerscron }

CronService（`schedulers/cron/service.py`）运行自己的定时器循环，并把任务持久化到带
`fcntl` 锁的 `jobs.json`。它「睡到下次唤醒」的时长设有上限，以便及时感知对等进程写入的
任务（该存储在 mtime 变化时重新加载）。每次触发都在锁内以 pid 加时间戳认领（并带过期
认领的 TTL），使两个进程不会重复触发同一个任务；此外还有渠道过滤，让 REPL 不会抢走
本该发往真实渠道的任务。

任务触发时，`on_cron_job` 回调（`raven/core/cron_stack.py`）向 spine 提交一个 `CRON`
来源的 `TurnRequest`，绑定到 `cron:<job_id>` 这个会话。投递按分支显式处理：单目标的
投递型任务搭 hub 送往其唯一出口；广播型或静默型任务则以任务自身的（临时）渠道作为来源
提交，使 hub 丢弃该回复，随后广播再把回复显式投递给每个解析出的目标。投递目标在触发时
解析（因此 `cron config set` 会在下一次触发时生效）：真实渠道直接通过，临时渠道
（cli / tui）展开为配置的转发渠道，chat_id 则从每个渠道最近的会话中查得。

cron 的一次触发还会写入共用的 NudgePolicy 账本（一次话题标记触发，外加一条标为中性的
已投递记录），使 Sentinel 在去重窗口内抑制自己就同一话题发出的主动提醒，同时又不拉低
Sentinel 用来学习的接受率（cron 是用户发起的，不是 Sentinel 自己的提议）。一个周期性
任务若反复触发而始终无人回应，在达到连续失败次数上限后会自动衰减，以遏制那种
「每几分钟一次、永远不停」的失控任务。

---

## 11. 心跳与事件驱动唤醒 { #11-heartbeat-and-event-driven-wake }

HeartbeatService（`schedulers/heartbeat/service.py`）是一个独立的、由定时器驱动的服务。
第一阶段读取 `HEARTBEAT.md`，并通过一次虚拟工具调用询问 LLM 是否存在活跃任务
（从而避免解析自由文本）；第二阶段仅在决策为 `run` 时执行，它走完整的代理循环并投递结果。

事件驱动唤醒（`raven/proactive_engine/wake.py`）把唤醒请求合并为提前的心跳节拍。
生产者（一次 cron 完成、一次子代理完成、一次手动触发）调用 `request_wake_now`，
心跳循环则等待调度器的唤醒事件而非裸 sleep，因此一次唤醒只是让当前的睡眠提前结束。
一个频率守卫会拉开连续触发的间隔；当代理正忙于处理用户消息时，唤醒会被暂存，
并由代理循环的「轮次完成」回调重新触发。唤醒只驱动 HeartbeatService——SentinelRunner
保持自己独立的节拍循环，不引用唤醒调度器。触发器存储（`sentinel/discover_triggers.py`）
是另一套基于文件的进程间通信（仿照 cron 的任务文件），runner 在自己的短周期循环中消费它，
用于由运维人员发起的发现运行。

---

## 12. 任务发现：前瞻性菜单 { #12-task-discovery-anticipatory-menus }

在响应式 nudge 路径之外，每日一次的任务发现批处理会提出一份候选任务菜单供用户挑选。
流水线如下：

- TaskDiscoverer（`sentinel/predictor/task_discoverer.py`）：由 runner 的节拍每天触发
  一次（带时间守卫，与 `sentinel.enabled` 共享生命周期），它读取近期记忆与历史，产出一个
  含若干选项的 `PendingDecision`，经 NudgePolicy 把关后，通过
  `NudgeDispatcher.dispatch_options` 投递格式化好的菜单。
- PendingDecisionStore（`sentinel/executor/pending_decision.py`）：一个带 `fcntl` 锁的
  JSON 存储，具备 TTL、待确认状态与取代语义。
- DecisionRouter（`sentinel/executor/decision_router.py`）：监视用户回复，确定性地匹配
  数字或 `/pick N`，并以 LLM 分类器作为兜底（需超过置信度阈值）。一旦匹配，该回复即被
  消费，不再抵达代理循环。
- DecisionConsumer（`sentinel/executor/decision_consumer.py`）：把匹配到的选择转换为一次
  ActionExecutor 调用，必要时先经过一道确认步骤。
- ActionExecutor（`sentinel/executor/action_executor.py`）：执行 `reply`（经由 injector）、
  `tool`（经由代理的工具）或 `spawn`（经由 ProactiveSpawn）。每条路径都要通过 NudgePolicy
  并记录触发。

用户的选择在代理循环的钩子链中被短路（由 DecisionConsumer 适配器完成），LLM 不会被调用。

---

## 13. spine 集成与用户入站闸门 { #13-spine-integration-and-the-user-inbound-gates }

主动轮次抵达代理的方式与用户消息完全相同：作为带 `Origin`（`USER`、`SENTINEL`、`CRON`、
`HEARTBEAT`、`SUBAGENT`）的 `TurnRequest` 提交给进程级的 `Scheduler`
（`raven/spine/scheduler.py`），由它路由到按会话串行的 `Lane`。回复与主动消息都经由
`DeliveryHub`（`raven/spine/delivery.py`）投递。

代理循环（`raven/agent/loop/main.py` 的 `run_turn`）读取 `req.origin` 来把守两件事：

- 用户入站钩子（参与度检测、发现菜单的消费者）只对真正的用户输入运行。`SENTINEL` 与
  `SUBAGENT` 轮次不是真实的用户输入，因此被挡在外面。
- 对于输出本身由系统生成的来源，`after_send` 链（NudgeInjector / 响应修饰器）会被跳过，
  以免一条主动回复被再叠加一层 nudge。这与用户入站那道闸门是彼此独立的——尽管今天两者的
  成员恰好重合，但它们表达的是不同的含义。

cron 轮次还会额外把守 cron 工具（通过在 lane 任务中设置的上下文变量），使代理无法在运行
途中安排新的 cron 任务。

### 轮次进行中的用户输入（BusyPolicy.INJECT） { #mid-turn-user-input-busypolicyinject }

当某个会话已有轮次在运行时又来了一条用户消息，网关入站分发
（`raven/cli/gateway_commands.py`）会检测到进行中的轮次（`Scheduler.has_inflight`），
并以 `BusyPolicy.INJECT` 提交该消息，而不是排队一个新轮次。lane 把这次注入放进一个邮箱；
正在运行的那一轮会在每次工具循环迭代开始时调用 `drain()`，并在下一次 LLM 调用之前把待处理
的注入合并为用户轮次（`raven/spine/scheduler.py`、`raven/agent/loop/main.py`）。
若某次注入始终没被该轮消费掉，它会回退为一个新追加的轮次，因此不会丢失。`INJECT` 与
`INTERRUPT` 仅限用户使用；主动来源若请求其中之一，会被降级为 `APPEND`。

### ask_user——暂停一轮以向用户提问 { #ask_user-pausing-a-turn-to-ask-the-user }

`ask_user` 工具（`raven/agent/tools/ask_user.py`）暂停一轮，向用户提出一个结构化问题并
等待回答。它把该轮的 conversation_id 与提示交给 `QuestionBroker`
（`raven/rpc/question_broker.py`），后者发出一条 `clarify.request` 通知并阻塞
（阻塞在一个以 conversation_id 为键的 future 上）直到答案到达，并带有一个兜底默认值，
确保循环总能拿回一个字符串。`clarify.request` / `clarify.respond` 正是 ui-tui 前端既有的
多选提示契约（ClarifyPrompt），broker 直接复用了它。

答案通过两条路径抵达 broker：

- TUI：前端渲染 ClarifyPrompt，并以一次 `clarify.respond` RPC 作答，由
  `raven/rpc/methods/question.py` 处理，它调用 `broker.reply(...)`。
- 渠道：broker 把问题渲染为发往该会话所属渠道的出站 Text；网关入站分发在下一条属于
  「有待回答问题的会话」（`broker.pending_req(cid)` 已设置）的消息到来时，会把该消息路由到
  `broker.reply(cid, text)`，而不是启动或注入一个轮次（`raven/cli/gateway_commands.py`）。

由于一轮是串行的，每个会话至多有一个待回答的问题；重叠的提问会让过期的那个走兜底。

阻塞在 `ask_user` 上的轮次会占住它的 lane 和一个用户并发槽位（`OriginPools(user=...)`），
直到答案到达或 broker 的超时兜底触发。在网关默认 4 个槽位的情况下，三个待回答的问题仍留有
一个空闲槽位；只有四个会话同时阻塞才会耗尽整个池。默认超时限定了最坏情况，因此一个被遗忘的
问题不会无限期地卡住网关。

`clarify.request` 的线上负载携带 `{question, choices, header, recommended, timeout_s,
index, total, batch}`。`header` 是一个简短的标签片；`recommended` 指出代理会选的那个选项；
`index` / `total` / `batch` 把该问题定位在它所属的这次调用中，使界面既能展示整组问题及其
进度，又仍然一次只收集一个答案。ClarifyPrompt 渲染的是单选提示，标出推荐选项，倒计时显示
剩余预算，并接受自由文本作答或在选择之外附加一条备注；不提供多选。

一次 `ask_user` 调用中的所有问题共享同一份预算（`tools.ask_user.timeout`，默认 600 秒），
因此上面那段结论对成组提问同样成立——一次三个问题的调用不会把一条 lane 占住三个超时那么久。
