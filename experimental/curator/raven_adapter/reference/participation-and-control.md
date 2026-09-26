# 参与行为与控制：时机、可见状态和结果合成

本篇回答 Participant 或 Hook 的一次返回如何影响原生 Loop。适用范围见[阅读入口](index.md)，总体时序见[Loop 执行](loop-execution.md)。精确输入输出 schema 以本轮 Declaration 及原生协议为准。

## 1. Participant、Hook 与四策略的连接

`AgentParticipant` 提供按语义命名的参与方法。`ParticipantHook` 把这些方法连接到原生 Hook 阶段，并将参与者集合交给当前原生四模块中的对应方法合成。组合结果转换为 `HookDecision` 后，Loop 在具体调用点应用它。

这条链上的责任是：参与者提出意见；策略负责对应意见的合成；Hook 负责阶段和原生控制形状；Loop 决定实际执行后果。下文的合成规则对应当前默认原生模块及 ParticipantHook；若宿主采用其他策略实现，需要核对其实际合成逻辑。

原生 `AgentHook` 是更直接的生命周期入口，接收可变的 `AgentHookContext`。它与受声明包装的 Participant 不具有完全相同的状态访问和结果检查方式。选择哪种入口应由所需行为与实际授权决定。

## 2. 方法与阶段的对应

下表描述正常可达路径；来源条件、提前短路、错误及预算停止可能使后续阶段不发生。

| Participant 方法 | 原生 Hook 阶段 / StepView.phase | 关键输入 | 返回被怎样使用 |
|---|---|---|---|
| `intake` | `before_user_inbound` / `user_inbound` | 本次正文 `text`、此前会话历史；此时尚未装配本轮模型消息 | 串行改写输入，或提前回答 |
| `select_tools` | `before_iteration` / `iteration` | 当前 offered 定义、迭代消息 | 改变本轮模型可见的工具定义 |
| `advise` | `before_iteration` / `iteration` | 调用前状态，`response` 为 None | 形成接下来模型可见的建议 |
| `system_addendum` | `before_iteration` / `iteration` | 当前消息、预算等；同一参与集合之前的附加内容已移除 | 为当前调用补充 system 内容，或提前回答 |
| `review` | `before_execute_tools` / `execute_tools` | 模型提出的工具调用；本批结果尚未存在 | 接受、要求重采样，或结束 |
| `advise`、`review` | `after_iteration` / `after_iteration` | 工具分支已有结果；文本分支的草稿在 `response` 中 | 建议仅在接受时落入消息；审查可回退或结束 |
| `salvage` | `terminal_answerless` / `answerless` | 结束时消息，`response` 被清空 | 提供缺失的最终回答 |
| `archive` | `after_send` / `sent` | `reply` 参数、可用的此前历史、参与者自己保留的状态 | 合并本轮观察记录，参与者也可写入明确的持久资源 |

当前实验只包装 Plan 选择且 Declaration 允许的方法和 phase；类中额外实现的方法不会因此自动开放。表中的 Participant 返回由有效声明校验，原生 AgentHook 则直接返回 HookDecision，二者的格式与检查位置不同。

### 2.1 可见状态的边界

`StepView` 是阶段观察，不是对整个运行时对象的任意访问入口。以下说明常用字段的消费方式，完整字段与类型仍以原生协议和本轮交付为准。

- `session_key` 标识本次会话，可用于关联明确的会话资源，不是 Session 对象。
- `phase` 是当前观察时机；同一个方法在不同 phase 的输入与后果可能不同。
- `iteration` 是当前 attempt 的迭代编号；inbound、sent 等尚未提供迭代信息的 context 可呈现 0，不能据此推断已经执行的模型调用总数。
- `question` 来自 Loop 进入该 attempt 时提取的本轮问题，不会随每个中途输入重新计算；intake 应读取显式的 text 参数。
- `max_iterations` 是该阶段交付的迭代上限，`window` 是交付的窗口容量信息；缺失时可为 None，它们都不是精确剩余预算。
- `mode` 与 `mode_overlay` 是宿主交付的模式信息，不能当作修改宿主配置的入口。
- `response` 表示当前模型响应；调用前和 answerless 阶段没有当前响应。
- `transcript` 是该阶段交付的消息。文本候选在 after_iteration 时尚未保存为助手消息，要从 `response` 读取草稿。
- `history` 是宿主交付的会话历史视图，不能与包含当前执行结果的 transcript 混用。
- `turn_base` 用于区分窗口中的当前 turn 部分；必须结合实际提供的 transcript 使用。
- `tools` 描述阶段交付的工具视图，不证明每个定义都可执行。
- `rollbacks` 读取原生元数据中的已采纳回退计数，不是参与者自己发出请求的次数；有 rerun 时的作用域见 [Loop 的计数说明](loop-execution.md#31-计数的作用域)。
- `tools_ran` 由 `phase == "after_iteration"` 派生。它帮助区分工具响应的执行前后；纯文本响应也经过这个 phase，因此仍需检查 `response.tool_calls` 和实际结果。

`sent` 阶段的原生 context 没有重新附上本轮完整消息和响应。archive 不应假设拿到完整执行轨迹；如果需要本轮检查状态，可由同一 turn 内的参与者实例保存，再结合 `reply` 归档。

同一次 `before_iteration` 中，适配器先构造 StepView，再依次调用 select_tools、advise、system_addendum。select_tools 的 offered 参数会在参与者之间传递改动后的数组，但这些方法使用的 StepView 不会因此重新构造：后续 advise 的 `step.tools` 仍可能是选工具前的视图。同一工厂下的方法如需配合，可显式保存本 turn 的选择结果；不能假设所有返回都已反映到当前 step。

原生 StepView 的容器只读性与嵌套对象的行为应分别考虑。当前实验包装进一步复制交付给生成方法的观察数据；对这份数据的修改不能作为改变真实 Loop 的手段。改变执行要通过声明允许的返回路径。

## 3. 多个参与者怎样合成

`CompositeHook` 按注册顺序运行 Hook。针对 ParticipantHook，它先准备该阶段的参与者集合，由最后一个成功取得席位的适配器一次性发起集合调用，其余适配器在本阶段不重复调用。

这意味着混合原生 Hook 与 ParticipantHook 时，参与者合成发生的位置需要看真实组合结构，不能简单理解成每个 ParticipantHook 到达时各调用一次方法。前面的原生 Hook 提前短路，也可能使参与者合成尚未发生。

当前 `build_runtime` 先接入启用的 Eval Hook，再接入插件 Hook，然后是原生 Charter ParticipantHook 和宿主传入的 Hook。实验观察器与生成 ParticipantHook 位于宿主传入部分。这个顺序决定谁先看到并可能阻断某阶段；Participant roster 的集中调用仍遵循上面的规则。

| 方法 | 参与者之间的合成语义 |
|---|---|
| intake | 后一个读取前一个留下的正文；首个非 None reply 结束合成 |
| select_tools | 后一个读取前一个留下的定义数组；None 表示不改变 |
| advise | 非空文本按参与者顺序连接 |
| system_addendum | 文本按顺序连接；首个 reply 终止该合成并要求提前回答 |
| review | 首个 resample 或 end 决定结果；全部接受时继续 |
| salvage | 首个非空字符串提供最终回答 |
| archive | 按观察者名称合并；同名映射浅合并，后值覆盖冲突键 |

原生 Hook 链遇到 `short_circuit_result` 或 `rollback` 会停止继续调用该阶段后面的 Hook。内容修改、工具视图修改与模型建议只有在相应阶段才有消费者；不能把所有 HookDecision 字段当成任意阶段通用的控制语言。

## 4. 建议、诊断和重采样注入

这三类文本去向不同：

| 表达 | 消费者与效果 |
|---|---|
| `advise` 的返回 | 变成 `append_note`，宿主尝试附加在当前最后一条消息的内容上 |
| verdict 中的 `reason` / `note`、Participant 的诊断 trail | 诊断记录；不会自动成为下一次模型调用的纠正提示 |
| resample 的 `inject` | 获准回退后追加到消息，供下一次调用读取，并按原生保存规则处理 |

因此，review 若要求模型换一种做法，应通过正确的注入表达纠正信息，而不能只把原因写在诊断字段里。`advise` 在文本结束分支产生的内容，也不保证还有下一次模型调用读取它。

system addendum 的宿主操作是移除上一次附加内容，再插入本次内容，避免每轮累加相同说明。它需要已有 system 消息作为落点；自定义上下文引擎也必须考虑这种消费条件。

## 5. resample 的实际控制语义

1. review 返回 resample，ParticipantHook 转为 rollback 决策。
2. Loop 在支持回退的调用位置检查当前 attempt 的回退上限。
3. 若允许，移除本次迭代在消息边界之后追加的内容，追加 inject，过滤可用的生成参数覆盖，并重新进入迭代。
4. 已采纳的回退不消耗普通迭代名额，但消耗独立回退预算；覆盖仅用于下一次决策。
5. 达到上限后，Loop 继续当前路径并记录回退拒绝；本次 rollback_inject 和 rollback_overrides 也不会应用。纠正内容没有因返回 resample 而自动送达模型。

执行前回退会放弃尚未运行的工具提案；执行后回退移除当前迭代在记录边界后追加的消息。回退不会撤销边界之前消息的原地修改，也不会恢复参与者属性，更不会撤销已经产生的文件、网络或其他副作用。

回退请求先使 CompositeHook 停止调用本阶段后面的 Hook，再由 Loop 检查上限。即使 Loop 因上限而拒绝回退，也不会补跑已经被跳过的后续 Hook；不能把它理解成所有审查均已通过。

需要限制某个具体工具调用时，应比较 review 与原生 ToolGate 的语义；后者在每个调用派发前产生拒绝结果，见[工具与扩展](tools-and-extensions.md)。

## 6. end、salvage 与 archive

`end` 被转为 `short_circuit_result=reply`，宿主仅在该值不是 None 时短路。仅返回 end 标签而令 reply 为 None，不会产生预期的结束效果；方案还应保证提供的内容能够解释当前任务结果。

salvage 只在 Loop 判定错误或无可见最终回答，且没有已安排的 rerun 时调用。预算耗尽可能已经通过原生总结产生回答，所以不能把 salvage 当作所有停止路径上的回调。

在同一次 Participant 集合调用中，after_send 先合成 outbound，但 archive 收到的 reply 参数仍是进入该集合时的文本，不是合成后的 sending。生成 Participant 当前没有 outbound 开放面，但同一集合中已有原生参与者可能使用它。

archive 的记录合并到本轮观察元数据，原生保存逻辑将它附到本轮最后一条有内容或工具调用的助手消息。若没有可附着的消息，就不能假定记录已落入会话。archive 的调用也受 after_send 可达性约束。

## 7. 实例生命周期与失败语义

ParticipantHook 在首次需要该 turn 席位时调用工厂，原生装配 ParticipantHook 时不会提前实例化参与者；同一 turn 的多个 phase、重采样及正常 rerun 可访问同一实例。当前适配层把相同工厂引用下选中的方法组合到同一实例，支持方法之间共享 turn 内状态。不同引用和不同 turn 不承诺共享实例。

原生参与者调用或 Hook 抛出异常，通常被记录并按无意见继续；这与 ToolGate 的“异常即拒绝当前调用”不同。当前实验包装在原生容错前记录生成参与者异常，并检查返回格式。验证应检查这些错误记录，不能只检查 runner 是否最终返回。

普通 Participant 的 `judge` 当前没有通过 Hook 席位接到 ToolRegistry 的逐调用审查链。需要逐调用约束时使用已接通的入口；不能因为协议有 `judge` 就认为覆盖该方法会生效。`outbound` 也不是当前生成 Participant 的开放目标。

## 8. 具体实例需要核实

- 方法与 phase 是否在本轮 Declaration 内，origin 是否允许到达它。
- 参与者集合及原生 Hook 的组合位置，前面的短路是否会阻断调用。
- 所需结果位于 response、transcript、实例属性还是持久资源。
- 重采样请求是否被采纳，纠正信息是否真的出现在后续模型输入。
- 异常、缺失输入、回退预算用尽和没有后续模型调用时的行为。

## 9. 代码依据

- [Participant 协议](../../../../raven/contracts/participant.py)：`AgentParticipant`、`StepView` 与返回构造函数。
- [参与者合成](../../../../raven/agent/harness/participants.py)：`compose_*`、`read_intake`、`read_verdict`。
- [ParticipantHook](../../../../raven/agent/hook/participant.py)：phase 映射、roster、system addendum、`_decide`。
- [CompositeHook](../../../../raven/agent/hook/composite.py)：`_run_phase`。
- [原生 Hook 装配](../../../../raven/core/hooks_stack.py)与 [build_runtime](../../../../raven/core/runtime.py)：注册顺序。
- [Hook 协议](../../../../raven/contracts/loop_hooks.py)：context 与 decision。
- [Loop 消费位置](../../../../raven/agent/loop/turn_path.py)：`_hook_rollback`、文本草稿、answerless、after_send 和 `_stamp_turn_observers`。
- [建议落点](../../../../raven/agent/loop/_shared.py)：`append_hook_note`。
- [实验包装](../observe.py)：`participant_factory`、`_participant_method`。
