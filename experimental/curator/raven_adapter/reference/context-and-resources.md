# 上下文与资源：Prompt、Skill、历史和记忆如何生效

本篇说明模型输入的形成及可改造位置。适用范围与术语见[阅读入口](index.md)，调用顺序见[Loop 执行](loop-execution.md)。

## 1. 从持久资源到实际请求

Prompt 文件、Skill、会话记录和记忆后端都是输入来源。它们需要经过读取、筛选、装配及预算处理，才能进入一次模型请求。文件存在、资源被发现、正文被读取和模型实际收到内容，应当分别确认。

原生 Memory 策略先决定候选历史与预算，再委托 ContextEngine 组装消息。后续每轮仍有窗口维护、Participant/Hook 和 TokenWise 请求处理；装配结果并非未经后续处理的最终网络请求。

## 2. 默认 ContextEngine 的组成

当前原生工厂构造 `ContextAssembler`。它按 phase 组织 SegmentBuilder：

1. Phase A 并发构造不依赖已完成前缀的内容，随后按 builder.order 排序。
2. 构造本轮 user 消息，并把已完成的 system 前缀、user 消息和工具定义交给 Phase B。
3. Phase B 在同一批内并发处理需要读取前缀的 builder；默认包括上下文 Curator 的工作状态与历史选择。A/B 是上下文装配分组，不是 Participant 的 phase。
4. 合成 system、历史和当前 user 消息，返回附带 metadata 的 `AssembledContext`。

| 内容来源 | 在相应配置下的作用 | 需要观察的条件 |
|---|---|---|
| Identity | 身份、环境和能力相关说明 | 实际模型、workdir 和工具环境可能影响文本 |
| Bootstrap | agent home 下原生启动内容文件 | 原生路径、文件内容及对应 segment 是否启用 |
| Memory | 本地记忆内容与后端 recall 结果 | 后端实际绑定、身份、召回成功及返回内容 |
| Active skills | 可用的 always Skill | 依赖、所需工具、注入形式和数量上限 |
| Routed skills | 仅在 push 模式构造，提供选择与正文供给 | discovery 设置、路由、过滤和读取结果 |
| Context Curator | 工作状态与经过预算处理的历史 | 上下文配置、可用模型及本轮实际输出 |

当前 discovery 的原生默认值为 pull，此时工厂不构造 Routed skills 的 SkillsSegmentBuilder，而使用发现菜单和工具读取等路径；push 模式才增加对应的自动选择与正文供给。其他内容段仍受各自配置和可用性约束。

builder 失败可以导致对应 segment 被省略，并在装配 metadata 中记录降级；配置也可以移除 segment。默认工厂中，Context Curator 是提供历史的 builder。当前装配器从空 history 开始，只有成功返回的 Phase B segment 提供 history 才会替换它：移除 Curator 或该 builder 构建失败时，不会自动透传原始会话。`owns_compaction` 此时仍为 True，也不会自动改走其他历史路径。

自定义多个历史提供者时，当前装配器取遍历顺序中最后一个非 None history，不会自动合并。修改 segment 或替换引擎时，要显式核对最终消息中的历史，而不只检查 system 内容。

## 3. 四种常见输入改动怎样选择

### 3.1 修改 Bootstrap 内容

`memory.prompt` 对应原生 `BOOTSTRAP_FILES` 中的文件，装配时从 agent home 读取。允许的路径从原生常量派生，当前 schema 是具体文件集合的依据。

它适合相对稳定的规则和背景。必须保持原来仍然有效的内容，并说明修改哪个文件、为什么放在这里、怎样确认其进入实际请求。该入口不等于可以向任意工作目录写入任意文件。

### 3.2 修改当前输入或逐轮提示

intake 在上下文装配前改写当前正文；advise 产生迭代建议；system addendum 为当前调用补充 system 内容。三者时机与消息位置不同，详见[参与行为与控制](participation-and-control.md)。

需要根据实时执行状态变化的规则，应明确读取什么阶段数据。把动态判断写成一段静态 Prompt，不会自动产生宿主级控制；模型阅读规则后的行为仍需验证。

### 3.3 提供 Skill

Skill 适合承载任务知识、操作步骤及配套资源。当前实验把相关内容放到 agent home 的本地 Skill 根目录；一个包需要可发现的 `SKILL.md`。

从发现到使用需要经过不同步骤：

| 步骤 | 成立的事实 | 尚不能推断的事实 |
|---|---|---|
| 文件物化 | 包和资源写入指定位置 | 注册表一定发现它 |
| 注册表发现 | 元数据可被列出，来源和路径已知 | 依赖满足、正文已加载 |
| 可用性检查 | 对应依赖检查通过 | 当前回合一定选择它 |
| 内容供给 | 正文或摘要进入上下文或工具结果 | 模型遵循了步骤、脚本实际执行 |
| 实际使用 | 出现相应模型行为、工具调用和结果 | 任务已被正确完成 |

always Skill 仍区分完整正文与 description 摘要；摘要模式需要进一步读取正文。可用性和 required-tools 筛选作用于相应 always 集合；完整正文还受数量上限限制，description 摘要不占用这个完整正文名额。

`read_skill` 用于读取正文；`use_skill` 对本地资源给出内容与脚本位置，对远端资源可能进行原生下载、安装。返回脚本路径并不执行脚本。Skill 中的步骤和工具名也不会为模型增加原本没有的权限或实现。

### 3.4 改造 ContextEngine

`memory.context_engine` 通过工厂返回一个实现原生 ContextEngine 交互的对象。该入口负责组装消息和 after_turn 等生命周期，不是单纯替换一段 Prompt。

| 层次 | 要求 |
|---|---|
| 继承原生 ContextEngine ABC | 抽象成员为 name、owns_compaction、assemble；after_turn、set_provider、set_context_window 有默认无操作实现 |
| 当前实验适配层构造检查 | name、owns_compaction 存在，assemble、after_turn、set_provider 可调用；不要求继承 ABC |
| 实际调用 | assemble、after_turn 遵循原生异步调用约定；提供属性或可调用对象还不能证明参数、返回和状态行为正确 |

可以继承 ABC 复用其默认方法，也可以提供符合要求的结构实现。当前适配层不把 set_context_window 作为构造必需项。

实现需要明确：候选历史如何处理、预算如何使用、当前 user 消息和 system 内容如何组织、metadata 如何返回、provider 或窗口变化如何响应，以及 after_turn 如何更新自身状态。默认实现中的 Bootstrap、Skill、记忆等行为不会自动被一个全新引擎继承。

如果引擎缓存了 provider 或窗口容量，就需要明确怎样更新这些状态。当前默认模型绑定更新会调用 ContextEngine.set_provider；这与每个 session 在 turn 中使用自己的绑定不同。set_context_window 是原生提供的更新方法，但当前适配路径没有显式调用它，不能仅凭实现该方法就认定窗口变化已接通。可以结合本轮 assemble 的 budget 和实际绑定取得所需信息。对于不保存相应状态的实现，无操作方法可以是合理选择。

返回对象是否满足构造检查，只证明基础接口存在；实际消息顺序、缺少正文、状态丢失和预算行为需要对应执行验证。

## 4. 历史、长期记忆与上下文工作状态

| 对象 | 责任 | 生命周期与关联 |
|---|---|---|
| 原生会话记录 | 保存交流和执行消息 | 按 session 身份持久化，是历史来源 |
| 当前模型窗口 | 本次调用实际看到的消息 | 可筛选、压缩或重建，不等于完整会话 |
| Context Curator 工作状态 | 在预算内维护可用状态与历史 | 由原生上下文机制管理 |
| 长期记忆后端 | recall、store、feedback，以及 start/stop 等生命周期 | 通过原生配置、身份与生命周期接入 |
| Harness Curator 的方案与反馈 | 改造当前 worker 的依据 | 位于本实验的生成和任务迭代层 |

当 ContextEngine 声明自己负责 compaction 时，默认 Memory 提供完整候选会话，并由该引擎决定取舍；否则采用原生会话的 post-consolidation 历史路径。不能从窗口里没有一条消息推断持久会话里也没有。

当前适配层构造生成的记忆后端时，要求 recall、store、feedback、start、stop 五个方法可调用；实现还需符合各方法的原生异步参数与返回约定。MemoryBackend 协议另有 recall_session、delete、health；它们服务于相应读取、管理或诊断路径，当前适配层不把这三项作为构造必需项。是否需要实现，应依据所接入的宿主调用路径判断，不能把本实验的检查子集当作所有 Raven 入口的完整协议。

更换或新增记忆后端要同时考虑注册与选择：贡献工厂使后端可被选择，配置决定实际选用哪个后端。`user_id`、`agent_id` 来自宿主统一的记忆身份配置，不应在插件内部再维护一份相同身份。

recall 的文本成为模型材料，metadata 不会自动全部渲染到 Prompt。召回失败、超时或没有命中时，默认实现可继续执行并缺少这部分记忆；正常回答本身不能证明记忆路径正常。后端的 feedback 接口也可以没有实际处理逻辑，不能把调用该接口等同于 RSI 已生效。

## 5. 窗口预算与中途压缩

初始预算需要考虑模型上下文容量、输出预留、工具定义和 system 内容；可用历史空间是它们共同作用的结果。

Loop 在迭代前调用 Memory 的主动缩减与图片窗口处理；遇到特定 provider 拒绝后，又可能请求响应式缩减。Memory 决定取舍，Loop 决定是否重试和怎样消耗预算。`changed=False` 意味着该次缩减没有提供新的恢复动作。

`memory.token_config` 对应 TokenWise 请求处理栈，它与 Raven 原生四模块不是同一组对象。原生 `install_from_config` 根据配置安装已实现的请求处理器；字段存在并不意味着安装器使用它。请求处理先于 Action.decide，可调整消息、工具或模型，响应处理用于观察用量等信息。缓存标记是否适用还取决于当前模型绑定的 provider 能力，应在实际请求处核验。

ContextEngine 的 `owns_compaction` 控制候选历史和 turn 收尾的合并责任；它不取消 Loop 每轮向 Memory 发起的窗口维护。两条路径需要分别考虑。

## 6. 一个组合例子

如果用户要求“每次给出结论前展示证据来源”，可以先提供 Skill 说明取证步骤，再由可用工具取得证据；如果需要宿主执行层面的交付检查，再接入合适阶段的 review。

验证应分别观察 Skill 正文送达、工具执行、结果进入可见状态，以及检查的控制结果。只看到 Prompt 中写了“必须引用证据”不足以证明组合机制已经工作。

## 7. 具体实例需要核实

- 当前 ContextEngine、启用的 segment、降级记录和实际 provider 输入。
- Bootstrap 的真实内容，当前 Skill 来源、可用性、注入模式与正文。
- 当前记忆后端、身份、召回与存储的真实结果。
- 模型绑定、窗口容量、预算与发生过的压缩或恢复。
- 新的上下文实现是否保留任务所需的历史、当前输入和资源行为。

## 8. 代码依据

- [Memory 策略](../../../../raven/agent/harness/memory.py)：候选历史、预算、装配和 shrink。
- [ContextEngine 协议](../../../../raven/contracts/context.py)与[工厂](../../../../raven/context_engine/factory.py)。
- [ContextAssembler](../../../../raven/context_engine/assembler.py)：两阶段装配、降级、消息与 metadata。
- [Bootstrap 读取](../../../../raven/context_engine/segments/render.py)：`BOOTSTRAP_FILES`、`load_bootstrap_files`。
- [Active skills](../../../../raven/context_engine/segments/active_skills.py)、[路由 Skill](../../../../raven/context_engine/segments/skills.py)、[Skill 注入形式](../../../../raven/memory_engine/skill_forge/catalog.py)。
- [Skill 注册表](../../../../raven/memory_engine/skill_local/registry.py)与[Skill 工具](../../../../raven/agent/tools/skill_hub.py)。
- [记忆段](../../../../raven/context_engine/segments/memory.py)、[MemoryBackend 协议](../../../../raven/contracts/memory.py)。
- [上下文 Curator 段](../../../../raven/context_engine/segments/curator.py)。
- [TokenWise 装配](../../../../raven/core/token_wise_stack.py)。
- [实验 Memory 原生入口声明](../targets/memory.py)与[Planning 原生入口声明](../targets/planning.py)。
- [实验构造检查](../bind.py)：`construct_component` 与 ContextEngine instance socket 的检查。
- [模型默认绑定更新](../../../../raven/agent/loop/wiring.py)：`set_default_binding`。
