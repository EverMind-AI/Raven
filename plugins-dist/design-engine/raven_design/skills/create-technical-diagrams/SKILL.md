---
name: create-technical-diagrams
description: 创建、编辑、诊断并验收说明对象组成、连接、流程、状态、时序、接口或因果机制的技术图解与工程文档。用于说明/培训图、系统与网络拓扑、流程与 SFC、电气/仪控图、机械剖面、科学机制图及其可编辑原生源；按用途与 assurance 区分说明、培训、维护和受控工程声明，不用于以数值规律为主的数据图、保留真实地理位置的地图、产品工作台或参数仿真。
---

# 创建技术图解与工程文档

## 领域职责与接口

本 Skill 拥有技术关系、任务子型、用途风险、领域 gate、claim ceiling、失败返回和专业签审门；工作
状态、authority/tool/review/promotion、通用渲染与证据治理沿用 `$visual-artifact-design`。

先按本文件路由。需要子型模型、专业语法和检查项时读取[领域模式与检查](references/patterns.md)；需要选择工具时只读取相关的[工具能力档案](references/tool-profiles.md)。工具名称、文件扩展名、成功导出和漂亮 PDF 都不能证明领域正确。

## 路由子型与用途等级

按读者要完成的判断选择一个主子型；其他视图必须回指同一对象或接口，不把异构语义硬叠在一页。

| 主子型 | 必须保真的关系 | 常见原生能力 |
| --- | --- | --- |
| 说明/培训图 | 对象、主关系、方向、边界、抽象声明 | 结构化 diagram、矢量说明、文档发布 |
| 系统/网络 | 组件、端口、接口、分区、依赖、冗余或信任边界 | graph/diagram 或 MBSE 模型 |
| 流程/SFC/时序 | 步骤或状态、事件、守卫、动作、分支、失败与恢复 | BPMN、SFC/PLC 或文本图模型 |
| 电气/仪控/工艺 | 设备、端子、导体/管线、信号、回路、代号与跨页引用 | ECAD/EDA、P&ID/仪控模型 |
| 机械/系统剖面 | 方位、剖切、装配、接口、尺寸/公差及来源 | 参数化 CAD 与关联工程图 |
| 科学机制 | 实体、过程、条件、因果方向、反馈、证据与不确定性 | 结构化模型加技术矢量图 |

用 claim scope 选择最高用途等级，不用画面外观推断：

| 等级 | 允许的最高领域声明 | 额外前提 |
| --- | --- | --- |
| L0 说明草案 | 内部概念、说明或培训候选；不可操作 | 事实/假设分开，限制可见 |
| L1 复核培训 | 指定受众的培训或技术说明 | 合格领域 reviewer 覆盖相应 claims |
| L2 维护辅助 | 指定配置的维护、调试或故障定位辅助 | as-configured/as-built 来源、原生 ID/端口、现场或维护复核 |
| L3 受控工程 | 工程、施工、安全或合规范围内的受控文件 | 设计依据、适用标准、计算/危害证据、授权校核与发布流程 |

`SELF_REVIEW_ONLY` 最高停在 L0；它可以支持当前 build 的限定技术/协议 internal-positive，但不能证明视觉独立通过、领域外部正确、现场有效或 L1–L3。`externally-validated` 只覆盖独立且角色合格的 reviewer 对同一 `build_hash` 明确通过的 claims。

## 领域契约

在进入 gate 前记录：

- `operation_mode: Create | Edit | Diagnose | Audit`、主子型、读者任务、最终消费者、目标媒介、目标用途等级和误用后果；
- `positive_for` 候选、明确非目标、`not_evidence_for` 与 `excluded_claims`；
- 必须准确、允许抽象、未知/假设、适用标准族及辖区/组织 profile；
- 需要共享的对象、端口、关系、状态、单位、方向、来源和稳定 ID；
- 授权 workspace 与写入边界，工具、符号库、字体、素材和第三方数据的来源、版本与许可；
- 需要的专业角色，以及哪些 claims 必须由谁独立复核。

缺失资料若会改变连接、顺序、尺寸、保护、因果或发布用途，保持未知并降级、等待或停止；不得用整洁布局补造事实。

## 操作、媒介与验证合同

本 Skill 始终介质中立：依据最终消费者选择原生母版、派生格式和验证环境，不把 HTML 或浏览器当默认外壳。只有最终消费者或合同交付明确为 Web 时，才加载 `$build-polished-visual-frontends`；它负责 Web 的 DOM/CSS、响应式、交互和浏览器像素，本 Skill 仍拥有技术语义、专业母版和领域 gate。所有媒介都必须在真实目标消费者中完成最终验收：视觉消费者还要验收与最终交付同一 `build_hash` 的最终像素；原生或非视觉消费者验收由领域契约定义的等价终态，例如可重开、可编辑以及字段、关系、状态与接口保持一致，不强迫生成像素证据。浏览器、HTML 查看器或 Gallery 只能作辅助预览，不能冒充非 Web 目标消费者证据。

- `Create/Edit`：若专业工具 proof 必须写入，每项 proof 只允许在授权 workspace 中从已记录 `input_hash` 的输入创建一个一次性验证副本；仅在副本上执行重开、往返编辑、重建或导出，并记录工具版本、真实调用与 `output_hash`。副本不是 authoritative master 或最终交付；其 hash 可以作为 scoped gate evidence，但 proof 不得改变 canonical、promotion、release 或 claim ceiling，也不能单独支持 promotion。
- Diagnose/Audit 一律零写入。不得保存、迁移、转换、导出、创建验证副本或产生会改变项目、配置、缓存的操作。只读取既有文件、日志、hash 与证据；工具无法只读验证时，明确阻断并请求切换到获授权的 `Create/Edit`，不得自行提权。
- 最终交付与专业 proof 分开记账：最终交付记录 authoritative master 的输入 hash、每个消费者输出的 output hash、许可和派生关系；proof 与最终交付都保留输入/输出 hash。若存在 proof，它只作为 scoped evidence；任何 promotion 都必须另行绑定最终交付和目标消费者的最终验收证据。

## Authority 与工具选择接口

每个 concern 只能有一个 authoritative owner。多视图、跨工具、状态/模式、安全相关或 L2–L3 任务必须建立 canonical semantic model；低风险单视图只有在原生文件能完整保存所需对象与关系时，才可由该原生模型同时拥有事实。

- canonical model 拥有适用的对象、端口、连接、状态、条件、来源与跨视图 ID；
- native view master 拥有选定专业工具中的符号、几何、连接、约束、引用和版面；
- 文档发布 master 只拥有分页、标题栏、目录、引用与发布呈现，不反向拥有图内专业事实；
- PDF、PNG、SVG、HTML、Viewer、截图和搜索索引均为 derived；不得直接修它们后声称上游已更新。

先列能力，再用 Registry 的稳定 candidate id 比较相关 profile。可用性必须来自 Registry/environment probe；某工具未安装是 availability gap，不是 capability gap。商业或 GUI 工具不可自动运行时记录 `unavailable` 或 `human_handoff`，不得声称 `used`。

一旦选择工具，就用其原生对象、字段、连接、状态、库、母版和导出方式完成相应 concern；不能只借一个控件，其余用自由文字或手绘近似。自绘/自研仅在可运行 capability probe 对所需能力返回 `fail` 后允许，并记录 `gap / minimal_custom_boundary / coherence_evidence`；`unavailable` 不授权自研。

## 跨视图与签审分门

每个视图登记 `view_id / purpose / included_ids / abstraction / source_revision / claim_scope`。同一对象进入多视图时沿用稳定 ID；名称、端点、方向、状态、单位或 revision 发生冲突，先修 authority owner，再重建视图，不在导出物局部圆谎。

- canonical → native → publish → consumer 的每条派生边记录输入版本、转换方式和输出 hash；反向人工修改必须被禁止或显式回写。
- 不同专业 master 可以并存，但每个 concern 只有一个 owner；交接文件必须说明工具、格式、版本、库和重建入口。
- 说明/培训审查关注抽象是否诚实、术语与步骤是否会误导；不得顺带批准维护或操作用途。
- 维护/调试审查关注指定配置、接口 ID、状态、反馈、故障路径及现场可用性；过期配置立即降级。
- 工程/安全审查按组织与辖区拆分设计、校核、危害/计算、批准和文控角色；缺一项只限制相应 claim，不伪造签审。
- reviewer 必须独立于被审 claim 的作者角色，并明确 `reviewed_claims / excluded_claims / build_hash / verdict`；泛化的“看过”不晋升。

每条线、色条、边框、框体、箭头和颜色必须编码对象、连接、边界、状态、方向、不确定性或适用标准；
删除后技术模型和读者追踪路径不变，就是装饰。限制与未知邻近受影响关系并保持可读，除非风险本身是
主要任务，不做成压过技术对象的巨幅或重复告示。发布外壳可用 CSS 排印、布局、必要分隔与状态反馈，
但不能用渐变、伪元素或“工程感”色条伪造技术资产。

若说明页需要图像背景，它只建立开场语境并与技术视图分层；后续以同一符号、排印和结构延续，
不在图解背后重复铺图，也不把技术内容放进半透明文字板。

## 领域 gate

每个 gate 按共享模板记录 trigger、inputs、actions、evidence_refs、claim_ceiling 和 failure_return；`not_applicable` 必须有已接受的 scope 理由。

| Gate | 输入与动作 | 晋升证据 | Claim ceiling | 失败返回 |
| --- | --- | --- | --- | --- |
| `td0-use-risk-bound` | 解析操作模式、读者任务、主子型、最终消费者/媒介、L0–L3、误用后果、授权写入范围和非目标 | 领域契约无歧义，消费者、许可、权限与 claim scope 明确 | 仅任务计划 | 回路由/用户约束；必要时降级或等待授权 |
| `td1-facts-standards-locked` | 锁定事实、接口、假设、未知、标准/代号/符号 profile | 每个关键事实可追溯或显式未知；偏差已登记 | 有来源的概念模型 | 回 `td0` 缩小用途；关键来源缺失则等待/阻断 |
| `td2-tool-authority-contracted` | 把所需能力映射到 Registry candidate，探针可用性，分配 concern owner 和自定义边界 | 工具选择理由、availability、authority graph、替代/交接和 probe evidence 齐全 | 已规划的原生实现候选 | 换候选、拆视图、`human_handoff` 或回 `td0` 降级 |
| `td3-semantic-model-validated` | 建立适用的对象—端口—关系—状态—证据模型并运行子型断言 | ID/端点/方向/边界、适用可达性与守卫、尺寸/因果来源通过 | 语义模型草案 | 事实错回 `td1`；模型/工具不匹配回 `td2` |
| `td4-native-master-proven` | 在选定工具原生语言中构建/导入；`Create/Edit` 的写入 proof 仅操作授权 workspace 内的一次性验证副本，`Diagnose/Audit` 保持零写入 | 真实 dependency/version/license/invocation、editable master、重开/rebuild/export，以及 proof 的 input/output hash；proof 仅作 scoped evidence，未改变 canonical/promotion | 可编辑原生候选 | 无只读能力或写入未授权则等待授权/回 `td0`；原生能力不足回 `td2`；投影错误回 `td3` |
| `td5-semantics-roundtrip-verified` | 对账 canonical 与 native；检查跨视图、标准偏差、人工编辑边界和往返 | 阻断差异为零；引用可解析；允许修改可传播且不会静默覆盖 | 内部技术候选 | 语义错回 `td3`；原生/同步错回 `td4` |
| `td6-consumer-task-verified` | 在真实最终消费者中执行最终验收并让目标读者追对象、关系、顺序或接口；视觉消费者检查同一 `build_hash` 的最终像素，原生/非视觉消费者检查领域定义的等价终态；仅 Web 交付组合 `$build-polished-visual-frontends` | 目标消费者、媒介和终态一致，必要状态和读者任务无阻断误读；最终输入/输出 hash 与许可齐全，证据晚于 master | L0 `SELF_REVIEW_ONLY` 候选 | 媒介/证据错回 `td0`；图型/工具错回 `td2`；误读回 `td3`；导出错回 `td4` |
| `td7-review-claim-bound` | 按目标等级取得角色合格、独立、claim-bound、hash-bound 的复核，并绑定 scoped promotion/release 记录 | reviewer role、method、verdict、evidence 与允许 claims 一致；promotion 同时绑定最终交付、目标消费者证据和适用 gate，proof 不能单独晋升 | 仅达 reviewer 明确批准的 L1/L2/L3 | 回最早失效 gate；未覆盖 claims 留在 L0，不整体抬升 |

## 失败与变更返回

- 工具未安装或无席位：保留能力需求与探针记录，选替代或 `human_handoff`；不要转手写。
- 专业字段只能作为自由文字：返回 `td2` 换工具或降低图型/用途声明。
- 多视图同一对象、状态或接口不一致：返回 `td3` 修 canonical，不逐页补丁。
- 原生修改被生成器覆盖或不能回写：返回 `td4`，选定单向生成或受控双向同步。
- `Diagnose/Audit` 所需步骤会写入，或 `Create/Edit` 的 proof 超出授权 workspace/直接改 authoritative master：停止并返回 `td0` 取得明确授权；不得以验证为由继续。
- 非 Web 交付只有浏览器、HTML 查看器或 Gallery 帧：`td6` 失败，回真实目标消费者补证；不得提升 claim ceiling。
- PDF 看起来正确但 native/语义不成立：返回 owning gate；视觉通过不抵消领域失败。
- 色条、外框或背景被当作技术语义，但 canonical model 与符号合同中没有对应职责：返回 `td3/td4`。
- 新 build、范围扩大或 claim 加强：重跑受影响 gate 并新建 promotion/review 记录，不继承旧 hash。

## 领域完成条件

只有所选用途等级适用的 gate 已通过，authority concern 无冲突，真实工具使用证据完整，目标消费者已完成最终验收，视觉消费者的最终像素证据绑定交付 `build_hash`，原生/非视觉消费者的等价终态与原生 master 同版，专业 proof 与最终交付的输入/输出 hash 和许可齐全，且 claim ceiling/限制在每个可独立流通的消费者中可见，才可交付对应声明。

`internal-positive` 与 `externally-validated` 分开记录；component/protocol 的局部通过不得外推 whole。发布、canonical、lifecycle、work status、promotion 与 assurance 保持正交。无法取得领域签审时可以交付诚实的 L0 候选，但不得使用“正式、工程就绪、可施工、可操作、标准合规”等更强措辞。
