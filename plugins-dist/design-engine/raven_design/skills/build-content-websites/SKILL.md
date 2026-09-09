---
name: build-content-websites
description: 构建以内容实体的发布、发现、阅读、引用、修订与归档为核心的网站；适用于单篇出版物、小型静态站、出版或机构站、文档与知识库、目录与档案、CMS 内容产品及内容迁移；不用于渠道图形、组件系统本体，或以反复操作业务对象为核心的产品界面。
---

# 构建内容型网站

## 领域边界

当主要对象是有身份和来源的内容实体，用户价值依赖其被找到、阅读、引用或持续维护时，使用本 Skill。若删除独立内容身份与阅读发现后价值仍不受损，应重新路由；营销图片属于传播图形，组件复用属于设计系统，审批、配置或保存业务对象属于产品界面。

服从 `$visual-artifact-design` 的共享治理。本 Skill 对内容 authority、发现和出版连续性保持介质中立：先确定最终消费者与目标介质，再选择呈现、构建和验收路径。只有最终消费者是 Web 或合同交付本身是 Web 时才调用 `$build-polished-visual-frontends`；非网页媒介不能用浏览器证据冒充目标消费者，浏览器预览最多是派生检查。

合同至少包含一个人类阅读或查看的最终消费者。网页、PDF、EPUB、邮件或应用内视图可各自呈现；
feed/API 只能附属于同一内容，不能替代人的最终消费。只有机器消费时退出本视觉 Skill。内容事实、
URL、媒体来源和发布历史不因介质改变。先读 [领域模式](references/patterns.md)；选工具前读
[工具能力档案](references/tool-profiles.md)。

## Web 视觉送达前置 gate

最终消费者包含 Web 时，在形成视觉方向或写第一行页面代码之前调用
`$build-polished-visual-frontends`，并完成以下记录；本域不能以内容 authority、框架选择或构建成功
替代它：

1. 锁定 `identity_route` 与 `layout_route` 并写入 `REFERENCE-CONTRACT`（定义见 `$build-polished-visual-frontends`）；
   两条路线各自锁定，一条不能代替另一条。保存实际来源画面和 3–6 条可观察关系，
   至少覆盖宏观结构／阅读顺序、层级／密度、主对象／图像角色；模板、标杆或品牌名称不算证据。
2. 建立 `VISUAL-THESIS` 与 `MASTER-VISUAL-CONTRACT`。在写页面 DOM、布局 CSS 或改造模板版式前，
   先调用 `image_generate` 产出并查看项目级主视觉母版；已有合法真实资产只有在完成同一语义工作时
   才可替代。每个主要页面族在布局前填写 `page_family / opening_job / visual_role / asset_lineage /
   reason / final_evidence`，`visual_role` 只用 `dominant_background / integrated_visual_field /
   content_first` 并默认第一种。如果最终像素表明大背景妨碍了阅读，调整文字大小或位置，或重新
   生成背景，而不是放弃大背景；实现便利、资产成本、没有真图、担心可读性或模型偏好不是选择
   `integrated_visual_field` 或 `content_first` 的理由；`visual_role` 声明须与渲染返回的 `opening_visual` 读数相符。大背景建立页面族开场，不要求每个
   后续区块铺图；后续用同源资产、排印、内容结构或交互延续身份，不能退回默认内容块。
3. 遇到对比、评测或选型任务，先判断读者意图属于
   `decision_support / neutral_comparison / benchmark_report / editorial_analysis`，关键词本身不决定
   页面形态。只有 `decision_support` 必须把场景或标准、选项、证据和边界组织成可扫描的决策结构；
   推荐必须有条件，不制造无依据的总体冠军。其他子型保留其自然文类，不强加推荐或决策矩阵。
4. 先按整页定义图像角色，再按主要区块建立资产路由表：
   `truth_asset | standard_symbol | generated_visual | exact_graphic`。内容页无合适真图时，hero、
   场景、插画、纹理、表现型 icon 与 feature pictogram 使用 `image_generate`；系列
   变体默认以主视觉或家族母图作为 image edit 输入。每项资产填写 `semantic_job` 和它与背景、
   排印、内容结构或交互中至少两项的关系。标准功能 icon 来自一个既有系统或成熟家族；数据、文字、坐标和拓扑由
   专业库或 renderer 负责。所有生成图在 provenance 记录真实性边界；纯氛围背景只记录 provenance，
   不在成品展示制作过程。只有省略说明会造成事实误认时才用内容语境内的短说明消除误认，说明后
   仍会误认则不得使用。不存在手绘 SVG、CSS 装饰或最终占位图降级路线。
   页头、favicon 与 og 图的标识槽位按 `$visual-artifact-design` 的既有身份盘点：主体已有标识必须用它，没有
   必须设计出来；简报里"无 logo / 自制标记"之类结论不是证据，自己去出处查看像素。
5. 代表画面直接使用已经取得的真实／生成资产，并逐条对照参考合同。`generated_visual` 只有在
   成功调用、查看原图、记录 lineage、进入最终文件且被产品代码引用后才算使用；生成失败是能力
   缺口，不授权用 SVG、Canvas 或 CSS 模拟。
6. 建立 `DETAIL-CONTRACT`：`SURFACE-MANIFEST` 覆盖页面族、顶层区块、独特状态和视觉例外；
   `COMPONENT-BEHAVIOR-MAP` 按独特行为类记录示能、动作、反馈、恢复与自动遍历入口。同组件、同状态
   合同和同数据规则的重复实例可共享视觉证据，但所有实际目标仍须由浏览器自动遍历。最后一次可见
   修改后逐行结账，再执行替图测试和模板距离测试；整页缩略图、构建成功或少数控件通过不能代替。
   来源、权利、方法和真实性边界邻近其约束的内容，不自动做成巨幅或重复横幅。色条、标题旁竖线、
   局部边框和图标外框只有在稳定编码导航、状态、分组或身份语法时保留，不能用来制造“权威感”。

什么算好，九条判据（CHECK 按代号逐条自评）：

- **J1 身份稳定可引**：每个内容实体有稳定 URL 身份，深链直达、可引用、可分享；
- **J2 阅读优先**：正文的行长、字号与层级优先于外壳；打开即读，不被巨幅壳面挡住内容；
- **J3 发现真实**：浏览、搜索与分面路径真实工作并覆盖 corpus；导航不是装饰；
- **J4 来源与时间可见**：来源、发布/更新日期与更正历史对读者可见、可信；
- **J5 单源派生**：页面、摘要、feed 与结构化数据全部由权威内容源派生，无第二事实源；
- **J6 长尾成立**：最长内容、缺值内容与极端标题在版式中不破版、不失义。
- **J7 世界先于图槽**：主视觉在布局前完成，每个主要页面族都有经任务证明的视觉角色和同源资产
  关系，页面身份同时进入排印、结构和交互；
- **J8 逐区闭环**：首页以下的每个区块和每个页面族都以真实内容达到成品状态，不把正确内容直接
  倾倒进默认模板，也不以精修首屏代表整站完成；
- **J9 示能兑现**：所有视觉上承诺交互的元素都有真实动作、状态和反馈；静态内容不伪装成控件。

## 1. 冻结领域合同

先记录 `work_mode: Create | Edit | Migrate | Diagnose | Audit`；Diagnose/Audit 对原对象和外部系统零写入。再明确：受众任务、内容子型、内容与媒体来源、目标部署、更新责任、风险、验收要求、目标 claims 和非目标。

冻结 `final_consumers`：至少一个主要人类消费者，以及适用的附属机器消费者；逐一记录目标介质、实际 renderer/client/device 及版本、入口与任务、视觉尺寸或非视觉输入条件、成功/失败状态和最终验收。内容 authority 与呈现必须分离；同一内容可以有多个消费者，但每个消费者分别声明呈现 owner、构建 hash 和证据，不能用某一媒介通过外推另一媒介。

选择一个主子型：单篇出版物、小型静态站、出版/机构站、文档/知识库、目录/档案或 CMS 内容产品。混合站按页面族标出从属领域，不能用“混合”跳过主对象与权威来源判断。

### 先声明 claim families

| Claim family | 只回答什么 |
| --- | --- |
| `content_identity` | 哪些实体与字段可被当前权威源支持 |
| `url_lifecycle` | 哪些身份、路径、迁移和终止行为已验证 |
| `discovery_behavior` | 所选发现轴上的浏览、查询、分面或关系是否成立 |
| `publication_continuity` | frozen、maintained 或 governed 中哪些动作真实发生 |
| `media_provenance` | 哪些原件、权利和派生变换可追溯 |
| `delivery_integrity` | 当前构建在哪些最终消费者、介质、页面族或等价内容单元及目标环境成立 |

不要把其中一族的通过外推到另一族；例如搜索命中不能证明内容事实，发布成功不能证明 URL 长期稳定。

### 两个独立能力轴

| 轴 | 值 | 合同含义 |
| --- | --- | --- |
| `publication_continuity` | `frozen` | 固定版本或一次性交付；保留身份、来源和构建，不声称持续维护 |
|  | `maintained` | 有更新责任、修订/更正、归档和 URL 变更策略 |
|  | `governed` | 有真实角色、审批、权限、回滚、审计、备份与发布监控 |
| `discovery_complexity` | `single` | 一个稳定入口和内容身份；不伪造集合或搜索 |
|  | `collection` | 多个独立实体、集合入口、浏览规则和可达路由 |
|  | `search` | 真实索引、查询合同、结果语境、零结果和新鲜度 |
|  | `faceted_relational` | 搜索之外还有受控分面、计数、分页、关系语义和状态恢复 |

两轴不是成熟度阶梯。选择满足任务的最小完整组合；升级任一轴都必须由内容规模、用户任务、编辑责任或风险触发。canonical、迁移、CMS、结构化数据、媒体权利和公开 SEO 也按 [触发条件](references/patterns.md#能力触发) 加载，不能把未知或未实现写成不适用。

## 2. 建立领域 authority graph

每个 concern 只能有一个权威 owner；一个版本化 bundle 可以作为不可拆分 owner。至少分开记录：

| concern | authoritative owner | 永远只是 derived 的对象 |
| --- | --- | --- |
| 内容事实 | 内容仓库、CMS 数据集或已接受的结构化源 | 页面、摘要、feed、结构化数据 |
| URL 身份 | 稳定 ID、路由、alias/redirect/canonical 的版本化映射 | 链接、sitemap、导航缓存 |
| 媒体 provenance | 原件与来源、权利、变换规则的资产清单或 DAM | 裁切、缩略图、转码和页面嵌入 |
| 搜索索引 | 版本化检索合同：字段映射、分析器、权重、过滤与同步规则 | 由内容和检索合同生成的实际 index、结果缓存 |
| 发布历史 | CMS revision log、VCS/release ledger 或批准的变更账本 | 页面更新时间标签、Gallery 和 Viewer 展示 |
| 视觉语言 | 已接受的品牌规范、设计系统或项目 token | 介质 token 映射、computed style、截图和最终像素 |
| 呈现映射 | 每种目标介质的模板、内容到介质的字段/结构映射与转换配置 | DOM、分页文件、阅读器视图及其他消费输出 |

内容源拥有事实，视觉语言 owner 决定可复用的视觉语法；呈现 owner 只消费二者，将内容映射进特定介质，不能拥有或复制另一套标题、日期、作者、关系、更正事实或视觉 token。没有视觉表面的附属消费者可为 `visual_language` 写带范围理由的 `not_applicable`；视觉消费者没有已接受的品牌或设计系统时，则以已证明的最小自定义边界建立独立、版本化的项目 token owner，不能在模板里形成隐式第二皮肤。Astro、Next、Nuxt、Eleventy 等框架只可拥有其内容模型、路由和构建 concern；它们没有可被虚构为共同品牌皮肤的原生视觉语言。索引、截图、预览、导出和 Viewer 都不能反客为主。

## 3. 选择并证明工具

先列能力，再比较共享 Tool Registry 与 [领域档案](references/tool-profiles.md)：内容建模/路由、编辑治理、文档、检索、媒体处理、目标构建和发布验证可以由不同工具承担，但每项 concern 仍只有一个 owner。edit/migrate 优先保留经探针证明可用的既有栈。

工具只有在依赖、解析版本、许可、真实 import/invocation、editable master、rebuild/export 及当前最终像素或消费者证据齐全时才记 `used`。GUI、商业或 SaaS 能力不可用时记 `unavailable` 或 `human_handoff`。自研检索、编辑器、媒体处理或复杂控件前，必须先执行 capability probe，并记录 `gap / minimal_custom_boundary / coherence_evidence`。

若专业工具的 proof 必须写入，只有 Create/Edit 可在用户授权的 workspace 创建隔离的“一次性验证副本”；记录输入、proof 输出和最终交付各自的 hash，验证副本不得替换权威母版，也不得改变 canonical、release 或 promotion。Migrate 的目标写入必须由迁移合同另行授权，不能借 proof 扩权；Diagnose/Audit 一律零写入，不创建验证副本、不保存回源或改变外部状态。

### 内容机制标杆

需要补足出版、发现或来源呈现机制时，读取本域 `references/benchmarks/` 的实际图版与 README。
它只能补充主要视觉参考尚未覆盖的内容机制，不得成为第二套视觉语法；把观察写入同一份
`REFERENCE-CONTRACT`，说明它补哪项风险、迁移到哪个实现对象，并在代表帧和终态帧结账。

## 4. 通过领域 gates

按依赖顺序建立 gate record；每条都绑定当前 `build_hash`。`not_applicable` 只用于触发谓词确实不成立。

| Gate | 输入与动作 | 晋升证据与 claim ceiling | 失败返回 |
| --- | --- | --- | --- |
| `D11_ROUTED` | brief、最终消费者、目标介质、主对象 → 选择子型、两轴、claims 与触发项 | 路由与消费合同；最多证明所选领域、介质和合同范围 | 领域路由/合同 |
| `D11_CONTENT_AUTHORITY_PROVEN` | 内容盘点、schema、owner → 校验身份、字段、关系、不确定值和来源 | 权威覆盖与边界内容；最多证明当前源中已核验事实 | 内容合同或 owner |
| `D11_TOOLCHAIN_BOUND` | 能力矩阵、Registry、probe → 选择原生模型、母版和变换 | 完整 tool-use/probe 记录；最多证明有证据的工具能力 | 工具选择或 handoff |
| `D11_WEB_VISUAL_CONTRACT_LOCKED` | Web 消费者、实际参考帧、页面形态与资产槽位 → 锁定参考合同和资产路线，用真实内容与实际资产完成代表画面 | 参考关系逐条映射、生成 lineage、当前代表帧像素；最多证明该画面方向与实际上页资产 | 参考路由、资产取得或代表画面 |
| `D11_SLICE_RUNNING` | 权威输入、呈现 owner 与目标消费者 → 打通代表实体到身份入口、呈现和来源/行动 | 可重复切片及目标消费者输出；视觉介质含当前像素，非视觉介质含等价消费结果；最多证明该切片 | schema、身份映射或 transform |
| `D11_IDENTITY_URL_PROVEN` | ID、路由与迁移触发 → 验证深链、404 及适用的 alias/redirect/canonical | 路由清单、链接与目标 base 证据；最多证明测试 URL 集 | URL owner |
| `D11_DISCOVERY_PROVEN` | discovery 轴与查询合同 → 验证入口、浏览、索引、搜索/分面和返回语境 | 查询、计数、新鲜度与状态证据；最多证明所选轴及当前 corpus | 发现合同或索引变换 |
| `D11_PUBLICATION_TRUST_PROVEN` | continuity 轴、内容/媒体 provenance → 验证责任、日期、更正、归档、权利及适用工作流 | ledger、manifest 与真实角色轨迹；最多证明已测角色、资产和连续性 | 内容/媒体 owner 或发布流程 |
| `D11_SURFACES_CLOSED` | 完整页面族、`SURFACE-MANIFEST` 与 `COMPONENT-BEHAVIOR-MAP` → 逐区检查真实内容、排印、图文关系、状态、合同内目标环境和所有视觉示能，并自动遍历全部实际目标 | 最后一次修改后的逐区可读像素、整页节奏证据、独特行为状态和全量目标结果；最多证明当前 build、页面族、视口与已遍历行为 | 未完成区块、页面族、视觉示能或实现 |
| `D11_DELIVERY_INTEGRITY_PROVEN` | 目标构建、内容单元族、呈现 owner、最终消费者与发布合同 → 在目标 renderer/client/device 验证阅读或解析、语义、导航/引用、回退和适用质量项 | 所有消费者均有当前 `build_hash` 的最终验收；视觉消费者另有同一 hash 的最终像素和真实消费轨迹；浏览器证据只证明 Web 消费，不证明 release、canonical 或 positive | 构建、呈现、部署或消费合同 |

失败时返回首个被否定的假设，保留不相关的权威母版与 passed gates。触发范围改变、权威输入改变或 hash 改变后，只重跑受影响的 gate；旧证据不自动继承。

## 5. 构建与检查

1. 从权威内容、身份/URL 和媒体清单生成最薄的真实内容单元切片，并由独立呈现 owner 映射到目标介质。
2. 在切片上证明身份入口、来源/行动和最长或最缺字段的内容；Web 再验证目标 base、直接深链与 404，其他介质验证其真实导航、引用、分页或解析接口，然后才扩展 corpus。
3. 按 discovery 轴增加集合、真实 index、搜索或分面；实际 index 始终由权威内容与检索合同重建。
4. 按 continuity 轴接入真实更新、更正、归档或治理流程；静态页面不得冒充 CMS。
5. 构建目标产物，检查 schema、失效关系、身份映射、索引新鲜度、媒体权利和该介质适用的链接、路由、分页或元数据。
6. 扩展页面族时逐行关闭 `SURFACE-MANIFEST`。每个区块都用真实内容检查职责、层级、密度、
   排印、图文关系、边界、状态和合同内目标环境；重复组件可以复用系统，但不能把下方区块或次级页面当作
   无需设计的填充区。整页截图只检查节奏，另以可读尺度检查每个区块。
7. 在真实最终消费者中遍历 `COMPONENT-BEHAVIOR-MAP`。按钮轮廓、链接样式、chevron、hover/focus、
   selected/current、可点击卡片和可展开面板都是行为承诺；每种独特行为验证动作、可见反馈和恢复／
   下一步，静态项则移除交互暗示。重复组件可以共用视觉状态证据，但每个实际目标和结果仍由浏览器
   自动遍历。
8. 最后一次变更后重新构建，在真实最终消费者中检查阅读或解析、导航、查询、长内容、缺值、错误
   和回退；在同一 hash 上为视觉媒介取得逐区像素、整页像素和交互轨迹，并为所有媒介取得最终验收。

验收证据服从最终消费者：Web 使用合同浏览器、设备/视口、DOM/可访问树和交互轨迹，并按需调用 `$build-polished-visual-frontends`；PDF/印刷、EPUB/阅读器、邮件客户端或应用内视图使用各自目标 renderer、client 或设备。附属 feed/API 使用其解析器、schema、任务结果和失败行为完成验收，但不能据此替代人类消费者的阅读或查看证据。视觉消费者检查实际目标尺寸与关键状态的最终像素；非 Web 输出即使能在浏览器打开，浏览器截图也只能作为辅助预览。

测试覆盖由真实页面族、状态空间和风险决定，不使用固定样本量。页面漂亮、构建成功或 Gallery 可打开，都不能替代内容事实、URL、发现、出版和部署证据。

## 6. 限定 claims 与晋升

领域 gate 通过后，才由共享底座为当前 hash 写 scoped promotion record。`positive_for` 应精确到内容身份、URL 行为、发现机制、出版流程或媒体 provenance；把未测试轴、未连接 CMS、未覆盖 corpus、生产流量、安全、法规与长期运维写入 `not_evidence_for / excluded_claims / claim_ceiling`。

`internal-positive` 只依赖项目接受的当前 claim 证据；`externally-validated` 要求每条正向 claim 都有与角色匹配、非作者、当前 hash 的独立 coverage。`SELF_REVIEW_ONLY` 只是 assurance，不能产生 external coverage。release、canonical、positive pool 与 Viewer 展示继续彼此独立。

## 域内险境（见到即停下返回对应 gate）

- 外壳先于内容——主视觉应与主对象／阅读起点同屏成立；若巨幅 hero、欢迎语或装饰卡把核心内容推到多屏之后，返回 J2；
- 对比任务没有先判断读者意图，或实际为 `decision_support` 却让场景、选项、证据与边界退居装饰之后
  （返回 Web 视觉合同）；
- 装饰导航与假分面：入口存在但无真实路径或计数（J3，返回 discovery）；
- 静态页面冒充 CMS 与持续维护（返回 continuity 轴）；
- 无日期、无来源、无更正历史的“权威内容”（J4）；
- 呈现层复制或改写内容事实形成第二源（J5，返回 authority）；
- 只用理想长度的示例内容验版式（J6，返回切片）；
- 生成图只写入 brief、停在 generated 目录或没有进入产品代码（返回 Web 视觉合同）。
- 生成图只是可随意换图的模板槽位；页面族没有预先声明视觉角色，`dominant_background` 退化成普通
  图卡，或其他角色被用作退回默认模板的借口（返回主视觉前置 gate）。
- 首屏完成而后续区块或次级页面仍是默认排版、模板余料、内容倾倒或未经可读尺度检查（返回
  `D11_SURFACES_CLOSED`）。
- 行、卡片、标签、选中态、chevron 或 hover 看起来可操作却没有行为，或动作发生后没有可见反馈
  （返回 `D11_SURFACES_CLOSED`）。
- 来源、权利或真实性说明压过了它所约束的内容，或同一说明被做成多个强横幅（返回 J2/J4）。
- 色条、局部边框、标题旁竖线或图标外框删除后不影响导航、状态、分组和身份，却仍以“强调内容”
  为由保留（返回 Web 视觉合同）。

## 最终检查

- [ ] 主子型、两轴、触发能力、claims 和非目标明确。
- [ ] 至少一个人类最终消费者及其目标介质、renderer/client、验收任务与证据条件明确；附属机器消费者没有反客为主。
- [ ] 内容事实、URL、媒体、搜索、发布历史、视觉语言与呈现映射各有唯一 owner，所有 index/Viewer/导出可回溯。
- [ ] 所有 `used` 工具和自定义边界有完整证据。
- [ ] Web 的唯一参考路线、可观察关系、资产路由、代表帧与终态像素已结账；需要生图时有真实
      API、provenance、最终文件与产品引用证据，主视觉在布局前完成，每个主要页面族都有经任务
      证明的视觉角色与同源资产关系（与 `opening_visual` 读数一致，`uniform` 已在 `reason` 说明），表现型精致视觉没有用手绘 SVG/CSS/Canvas 兜底，纯氛围图只
      记录 provenance。
- [ ] `VISUAL-THESIS`、`MASTER-VISUAL-CONTRACT`、`SURFACE-MANIFEST`、
      `COMPONENT-BEHAVIOR-MAP`、替图测试与模板距离测试均已按最终像素和真实操作回填，不存在
      “待回填／待检查／计划规避”。
- [ ] 每个顶层区块和页面族都有最后一次可见修改后的可读尺度证据；整页缩略图没有被用来替代细节
      检查。所有看起来可操作的实例均已兑现并遍历，或已改成明确的静态表达。
- [ ] 视觉面积与读者任务相称；来源和限制贴近受影响内容但没有反客为主，强色条、边框与图标外框
      均有可复述的稳定职责。
- [ ] 适用 D11 gates 对当前 hash 通过，失败返回位置可执行。
- [ ] 最终内容单元、发现路径、出版状态和目标部署由真实消费者验证；视觉输出有当前最终像素，所有输出有最终验收。
- [ ] 只有 Web 消费者调用前端 companion；非 Web 证据来自目标 renderer/client，专业工具 proof 未扩张写入权限或晋升状态。
- [ ] 正例池、external coverage、release 和 canonical 没有互相冒充。
