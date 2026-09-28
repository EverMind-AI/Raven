# Raven Harness 机制知识：阅读入口

本组知识材料帮助 Harness Curator 理解当前 Raven 的运行机制，并判断一项改动如何进入 AgentLoop、影响哪些信息与控制关系，以及需要什么证据确认效果。它也供开发人员核对生成方案。

## 1. 适用范围

本组说明覆盖原生 `AgentLoop`、`build_runtime` 及本适配层的接入机制。其他后端需要检查自己的调用链。

这些 Markdown 文件是 Raven 适配层提供给 Curator 的机制知识资源，随该适配层维护。它们解释宿主行为，不承担生成阶段的任务指令，也不是新的配置格式或授权来源。本页作为已登记的阅读入口，由材料组装器完整读入 orientation。四策略各自必需的公共协议、绑定契约、公共语义材料与 Raven 接入材料通过 Target.knowledge 直接进入所选目标的设计和实现请求；初选请求提供入口概览，仍可按缺口查询完整资料。本目录专题已按 reference.<文件名> 登记到 read_source，并核对内容摘要；补查结果继续进入后续阶段。其他专题不默认全量注入，文档链接本身仍不是登记来源名称。需要继续追踪依赖时，可使用本轮提供的原生工具探索源码副本；实际范围和路径见 exploration 材料。

机制专题使用中文，保留源码标识；四策略的公共与宿主材料使用英文直接供生成模型阅读。每项机制只维护一份说明，不建立语义可能漂移的平行译本。生成指令与参考知识各有用途，语言选择不改变协议和授权。

本组知识材料与以下文档各有用途：

- [设计](../../../docs/design.md)：Curator 的两层 Harness 结构、四个策略面、一次 curation 的流程及装配与观测。
- [迭代](../../../docs/rsi-iteration.md)：反馈怎样经 Analyst 与 Iteration 驱动多轮改进。
- 本组知识材料：Curator 作出改造判断所需的 Raven 机制知识。

## 2. 材料分工与阅读路径

| 材料 | 回答的问题 | 何时读取 |
|---|---|---|
| [Memory 接入](memory.md) / [公共语义](../../harness/reference/memory.md) | 检索、保留及上下文呈现怎样连接？ | 生成 memory.strategy |
| [Planning 接入](planning.md) / [公共语义](../../harness/reference/planning.md) | 规划操作怎样通过工具、上下文和观察生效？ | 生成 planning.strategy |
| [Capability 接入](capability.md) / [公共语义](../../harness/reference/capability.md) | 工具、Skill、能力选择与使用知识怎样交付？ | 生成 capability.strategy |
| [Action 接入](action.md) / [公共语义](../../harness/reference/action.md) | 语义决策怎样作用于判断和恢复？ | 生成 action.strategy |
| [Loop 执行](loop-execution.md) | 一次任务输入如何经过装配、模型调用、工具执行、恢复和保存？ | 建立整体时序；判断某方法是否会被调用 |
| [四策略与信息通道](strategies-and-channels.md) | 策略职责、信息关系和具体改造入口怎样对应？ | 定位改造对象、比较实现方式 |
| [参与行为与控制](participation-and-control.md) | Participant 与 Hook 在何时读取什么，返回值怎样合成并被应用？ | 生成 intake、advise、review、salvage 等行为或原生 Hook |
| [上下文与资源](context-and-resources.md) | Prompt、历史、Skill、记忆和上下文引擎怎样进入模型输入？ | 修改输入内容、知识供给、记忆或上下文管理 |
| [工具与扩展](tools-and-extensions.md) | 工具如何被展示、授权、执行，MCP 和插件如何参与？ | 增减能力、约束工具执行、贡献组件 |
| [装配与状态](assembly-and-state.md) | 配置、文件和工厂如何成为运行实例，状态怎样跨 turn 或换版延续？ | 生成产物、组合组件、规划生命周期 |
| [检查与证据](inspection-and-evidence.md) | 基础知识、当前事实、补查、验证和反馈怎样支持判断？ | 理解现有 Harness、核实效果、处理信息缺口 |

从机制词汇定位 Plan 中的目标，可使用[机制选择与目标示例](strategies-and-channels.md#5-从目标选择实现方式)。本轮完整目标名、binding 和约束由 Declaration 派生并交给生成阶段；参考材料提供阅读导航，不另存完整绑定表。

首次理解当前执行路径时，先读本页、Loop 执行和四策略与信息通道，再按任务读取专题。一个方案涉及多个机制时，要读取它们的组合边界。例如“根据工具结果审查回答”同时涉及工具与扩展、参与行为与控制、检查与证据。

按需读取意味着减少无关主题，不意味着删掉决定机制正确性的前提、分支、失败处理或状态条件。模型已有足够材料时可直接推进；缺失具体依据时再补查。

## 3. 先辨认信息的性质

| 信息 | 来源 | 能说明什么 |
|---|---|---|
| 稳定机制知识 | 本组说明及所引用的原生实现 | 给定入口在什么条件下如何执行 |
| 共同修改契约 | 当前有效的 `Declaration` | 本轮允许选择和提交什么；解析、校验和装配的依据 |
| 基线设计意图 | 宿主提供的基线说明、已有方案 | 为什么采用某种组成；实际效果仍需证据 |
| 当前装配事实 | `Inspection.facts`、源码版本索引 | 此实例实际使用哪些组件、配置、工具与资源 |
| 执行证据 | 具体执行的调用、结果、控制记录 | 哪条路径发生了什么 |
| 改造预期 | Curator 的 Plan | 改动希望产生什么效果，如何核验 |
| 反馈 | 原始人类反馈或其他监督来源 | 对具体执行、产物或任务结果的评价 |

基础知识和接口描述不能代替当前实例事实；装配事实不能代替行为证据；一次检查通过不能代替任务改善的证据。

若说明与当前源码、有效声明或实际执行不一致，应定位差异并修正相应说明。不能用文档中的概括扩大授权，也不能凭模型推测补出缺失接口。带来源的假设可以进入方案，不能当成已观察到的事实。

## 4. 核心术语

| 术语 | 在本组知识材料中的含义 |
|---|---|
| Task | 用户希望完成的当前任务，可跨多次交互、turn 和 Harness 修订 |
| Turn | 一次原生 `TurnRequest` 的处理过程，通常包含多次模型与工具交互 |
| Iteration | 原生 Loop 中一次模型决策及其后续处理；重采样和恢复会影响计数 |
| Attempt | 一次 `_run_agent_loop` 调用；宿主安排 rerun 时，一个 turn 可以包含多个 attempt |
| Harness 修订 | 任务中采用的一组代码、配置与内容改动；不是每次进程启动都产生新修订 |
| Runtime generation | 一次原生装配得到的运行对象集合，有自己的资源生命周期 |
| Curator 四策略 | 承接生成实现的 Memory、Planning、Capability、Action 语义职责；公共协议由 [harness/strategies](../../harness/strategies/) 定义，概览见[设计](../../../docs/design.md#2-四个策略面) |
| Raven 原生四模块 | `MemoryModule`、`PlanningModule`、`CapabilityModule`、`ActionModule`，是本组说明原生执行位置时引用的宿主接口 |
| 信息与控制通道 | 分析 Loop 内信息进入、模型决策、工具交互和执行控制关系的视角 |
| 两条出口 | Raven 向 Curator 提供知识与证据；Curator 向宿主交付候选产物 |
| manual | 宿主对既有声明的收窄规则；当前由目标、字段和 phase 限制参数表达 |
| Agent home | 配置中的 workspace：长期记忆、Skill、会话等资源的根目录 |
| Workdir | 当前任务实际操作文件的工作目录，与 agent home 可以不同 |
| Harness Curator | 本实验中理解并改造 worker Harness 的生成流程 |
| Context Engine 的 Curator | Raven 内部管理上下文工作状态与历史的组件，与 Harness Curator 职责不同 |

名称和更广范围的定义以仓库 [CONTEXT.md](../../../../CONTEXT.md) 为入口。

## 5. 材料组织与更新原则

每篇先说明问题和核心机制，再展开顺序、条件、组合及生命周期，最后列出具体实例仍需核实的信息和源码入口。例子说明机制关系，不增加新的生成权限。

同一机制的详细解释只在对应专题维护；其他篇引用它。接口签名、字段枚举、配置默认值和权限清单继续由源码与共同声明提供，文档不手工复制完整 schema。这里的完备性以支撑正确选面、实现和验证为界：每种入口应能找到职责、时机、可见输入、结果消费者、组合规则、状态寿命、失败后果及实例核验依据；具体字段和实例值在相应接口处展开。

机制描述需要区分三个层次：原生协议约定什么、当前原生调用者实际怎样消费、本实验适配层额外检查或限制什么。阅读一个机制时先定位这三个层次，不能将其中一层的保证直接推到另一层。

原生机制变化时，应沿相关篇末的源码入口核对说明。只有会改变结论的来源变化才要求修订相应内容；当前工具列表、配置值等实例信息由运行时取得，不写成通用事实。已登记材料由文件摘要检查变化；内容改变后需要重新取得当前检查与声明，不自动改写运行机制说明。


## 当前有效实例

朴素 Raven、定制实例和改造后的实例统一使用[当前 Harness 阅读说明](effective-harness.md)。先区分原生模块、公共策略与实际职责覆盖，再沿机制中的组件和来源引用补查。当前 source snapshot 同时覆盖登记的实现包和宿主明确提供的材料根；名称列表不代替实际调用证据。

## 两层组合的当前机制

根和受管理子 Harness 的共用协议、节点要求、材料交接、部署保护与来源记录见[两层组合](composition.md)，登记来源为 `reference.composition`。两层都生成完整四策略实现；Charter 不是子层生成限制。当前实例及权限仍以本轮 Inspection 为准。

## Shared observation contracts

[Runtime observations and consumers](observations.md) explains the representations used by all four strategy bindings. Selected contracts automatically include the applicable native response types and serialization sources; these basic interface facts do not depend on the model discovering an undocumented dependency.
