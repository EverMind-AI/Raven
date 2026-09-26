# 四策略语义、信息通道与 Raven 接入

本篇提供 Raven 原生职责和执行位置的知识，供四策略语义协议做适配判断。Curator 公共策略由 [harness/strategies](../../harness/strategies/) 定义，概览见[设计文档](../../../docs/design.md#2-四个策略面)；下文的原生方法表不定义公共协议。适用范围见[阅读入口](index.md)，实际时序见[Loop 执行](loop-execution.md)。

## 1. 三种视角分别回答什么

| 视角 | 核心问题 | 应当产出的判断 |
|---|---|---|
| 策略职责与行为 | 需要改善什么，由哪些策略操作承接？ | 所需协作方法、状态和效果；不由宿主 Hook 名单倒推 |
| 信息与控制通道 | 信息由谁产生、传给谁，在哪里改变执行？ | 输入、消费位置、可见状态、时机和后续影响 |
| Raven 实现方式 | 当前宿主提供什么入口承载这项改变？ | 配置、Prompt、Skill、工具、Participant、Hook、ContextEngine 等具体选择 |

这三个视角不形成一一对应。一个 Skill 的步骤说明进入模型输入，影响规划与行动；一个工具同时拥有展示给模型的定义、执行实现、返回信息和潜在副作用。

Curator 的选面需要完成从“希望改变什么”到“在哪个调用位置以什么形式改变”的推导。只指定策略名称，没有说明作用路径和实际入口，仍不足以形成可执行方案。

## 2. Raven 原生四模块的运行职责

Raven 的原生四模块协议定义在 `raven.contracts.harness`，与 Curator 自有的公共策略协议分开。`HarnessModules` 把一代运行时采用的四个原生模块对象绑定在一起。Loop 的计数、阶段推进、工具调度、保存和事件责任仍属于 Loop。

| Raven 原生接口 | 职责 | 原生方法及其关系 | 当前默认行为 |
|---|---|---|---|
| `MemoryModule` | 决定模型窗口及相关记录 | 候选历史、预算、`assemble`、`shrink`、`after_turn`；组合 intake、system addendum、archive | 包装 ContextEngine，提供窗口策略并委托参与行为组合 |
| `PlanningModule` | 准备消息和逐步引导 | `prepare` 处理已装配的 turn 消息；`ask_advice` 组合参与者建议 | `prepare` 透传；建议由参与者提供 |
| `CapabilityModule` | 决定本次展示给模型的工具定义 | `select` 读取能力视图；`ask_select_tools` 组合参与者提出的视图变化 | 从 ToolRegistry 读取当前可展示定义 |
| `ActionModule` | 取得模型决策并提供审查判断 | `decide`；`ask_judge`、`ask_review`、`ask_salvage` | 调用原生 provider/流式路径，组合相应判断 |

`MemoryModule` 的窗口职责与长期记忆后端不同。`ActionModule` 的模型决策与整个 Loop 不同。`PlanningModule` 也不天然意味着额外运行一个 planner Agent。

公共策略方法可以通过工具、回调、上下文组件等入口组合接入，不预设替换整组原生模块。反过来，原生协议存在某方法也不等于已有相应装配入口。需要逐项核对公共语义的调用、状态和结果是否能被兑现，不能自行给 `loop.harness` 赋值绕过装配。

## 3. 四种信息与控制通道

本实验的通道名称是理解作用关系的视图。`harness/channels` 从同一 Declaration 筛选 Target，不创建新的执行引擎或权限表。

### 3.1 `model_input`：信息怎样进入模型

需要说明来源、放置位置、保留时间及处理顺序。例如：

- Bootstrap 文件在上下文装配时成为 system 内容。
- Skill 正文可以通过上下文注入或工具读取进入消息。
- intake 改写本次输入；system addendum 在迭代前补充 system；advise 经宿主放进后续模型可见内容。
- ContextEngine 选择历史，窗口策略在后续调用前还可能压缩内容。

判断改动是否生效，要检查实际模型请求，而不是仅检查文件写入或某个方法返回了文本。

### 3.2 `model_decision`：请求怎样成为模型输出

需要说明实际 provider、model、消息、工具和生成参数，以及得到的文本或工具调用提案。Raven 原生 ActionModule 描述模型决策位置；TokenWise 和原生模型绑定也可能影响最终请求。

这里的“决策”不保证结果正确或具有执行权限。模型输出仍要经过后续审查、解析与工具检查。改变一个配置值的效果，要确认对应实现读取了它，并检查最终请求和响应。

### 3.3 `tool_interaction`：能力怎样被发现、调用和返回

需要区分工具定义、注册实例、可用范围、参数检查、授权、实际执行和返回。工具结果可以再次成为模型输入，也可以被后续参与行为检查。

`select_tools` 改变展示给模型的定义；新增可执行能力需要真实实现及注册。缩小展示集合也不能代替执行约束。细节见[工具与扩展](tools-and-extensions.md)。

### 3.4 `execution_control`：执行怎样继续、改变或结束

需要明确控制决策由谁提出、谁消费、哪些条件下采纳。例如：

- review 提出 resample，Loop 检查预算后回退消息并再次决策。
- ToolGate 拒绝一个调用，并将拒绝作为该调用的结果交回。
- inbound 短路会结束本次普通任务路径。
- resident 服务启动和停止由宿主生命周期管理。

控制动作不必产生给模型阅读的文本。诊断 note、原生控制记录和实际注入模型的信息具有不同消费者。

## 4. 通道与两条出口是不同关系

上述通道描述 worker 的 Loop 内部交互。Raven 与 Harness Curator 之间的出口传递的是另一组信息：

- Raven → Curator：稳定机制知识、有效声明、当前装配和已有执行证据。
- Curator → Raven：方案、配置、代码与内容候选，由宿主检查并装配。

例如，system addendum 属于 worker 的模型输入通道；Curator 阅读“system addendum 在每轮前替换上一份附加内容”的说明，属于 Raven → Curator 的知识交付。两者都可以是文本，但角色、时机和用途不同。

共同声明连接的是两个出口中的接口与授权事实。它不能把方案预期或模型推测自动变成运行事实。

## 5. 从目标选择实现方式

下表列出原生入口的 Target 名称，用于定位已有机制。名称不是公共策略协议的方法清单，也不固定资源的策略归属；例如 planning.skills 是 Skill 资源入口的名称。方案先明确语义操作，再核对所需原生入口及其组合。

| 目标或问题 | 实现方式与阅读位置 | Target 名称示例 | 选择时必须确认 |
|---|---|---|---|
| 稳定的角色规则、输出要求 | [原生 Prompt 内容](context-and-resources.md#31-修改-bootstrap-内容) | `memory.prompt` | 实际上下文路径读取该资源，内容未被覆盖或省略 |
| 可复用的任务方法与步骤 | [Skill](context-and-resources.md#33-提供-skill) | `planning.skills` | 能被发现，且正文实际送达模型；引用工具与资源存在 |
| 每次决策前依状态调整提醒 | [advise、system addendum](participation-and-control.md#4-建议诊断和重采样注入) | `planning.advise`、`memory.system_addendum` | 所需状态在对应阶段可见，消息进入正确位置 |
| 缺少执行能力 | [工具工厂、配置、MCP](tools-and-extensions.md) | `capability.tools`、`capability.mcp` | 实现注册、连接成功、执行授权与结果表达 |
| 需要检查模型提案或已执行结果 | [review、原生 Hook](participation-and-control.md) | `action.review`、`action.hooks` | 调用阶段、结果是否已存在、合成顺序和拒绝后果 |
| 对每次工具派发施加限制 | [ToolGate](tools-and-extensions.md#4-reviewjudge-与-toolgate) | `action.tool_gates` | 原生逐调用检查路径可达，拒绝和错误的语义符合目标 |
| 历史、知识或记忆供给不合适 | [上下文与记忆](context-and-resources.md) | `memory.context_engine`、`memory.backends` | 读取路径、预算、身份及持久化生命周期 |
| 需要跨 turn 的后台工作或清理 | [服务、SessionObserver](tools-and-extensions.md#7-service-与-sessionobserver) | `action.services`、`memory.session_observers` | resident 条件、资源归属、停止和失败路径 |

这些例子不保证本轮授权开放，也不穷举所有组合。[select.materials](../../generation/stages/select.py) 从 Declaration 派生 target、binding、effect 等材料。四策略目标（如 planning.strategy）交付公共协议、绑定与阅读材料；原生资源条目与之并存可用。同一生效声明供选面、检查和装配使用。

## 6. 一个完整的推导例子

目标：回答“测试通过”之前应有实际测试结果。

1. 先读取任务要求和已有执行，确认问题是缺少测试能力、模型没有使用能力，还是忽略了失败结果。
2. 若缺少能力，需真实工具及注册；只有 Prompt 不能产生工具执行能力。
3. 若缺少行为引导，可用 Skill 或 advise 说明何时测试、如何解读结果。
4. 若需要交付前检查，应确认测试结果在 after_iteration 或明确的持久资源中可见。执行前审查无法读取尚未产生的结果。
5. 需要重采样时，用正确的模型可见注入携带修正信息，并设计预算耗尽后的行为。
6. 用执行记录分别证明工具运行、结果被读取、控制请求被采纳，最后再判断任务结果。

同一任务可能组合多种入口。组合的必要性来自不同责任与时机，而不是要求四策略各生成一个组件。

## 7. 代码依据

- [Raven 原生四模块协议](../../../../raven/contracts/harness.py)：宿主角色、请求与结果、`HarnessModules`。
- [默认策略](../../../../raven/agent/harness/__init__.py)及 [Memory](../../../../raven/agent/harness/memory.py)、[Planning](../../../../raven/agent/harness/planning.py)、[Capability](../../../../raven/agent/harness/capability.py)、[Action](../../../../raven/agent/harness/action.py)。
- [Target 与 Declaration](../../harness/declaration.py)：修改契约、schema 与授权收窄。
- [Raven 入口声明](../targets/__init__.py)与[通道视图](../../harness/channels/__init__.py)：宿主候选和派生视图的代码位置。
