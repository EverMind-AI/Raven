---
name: create-illustrations-and-scenes
description: "规划、创作、编辑或审查以画面叙事、人物动作、空间、光色或视觉隐喻为主要价值的插画与场景；覆盖编辑/概念插画、叙事场景、角色与群像、环境世界观、物件主体、拼贴/摄影、3D 辅助、生成辅助和动画场景，并要求按风险证明构图、场景逻辑、专业工具、可编辑母版与限定声明。"
---

# 创作插画与视觉场景

本 Skill 是 `visual-artifact-design` 的插画领域层，拥有路由、专业 gate、能力需求、claim ceiling 和
失败返回；通用合同、工具事实、authority graph、渲染、review、promotion 与交付沿用共享底座。

需要选择构图、媒介模式或诊断画面时读取
[patterns.md](references/patterns.md)；需要比较专业工具能力时读取
[tool-profiles.md](references/tool-profiles.md)。候选名称不证明工具可用或已使用。

本 Skill 保持介质中立：先锁定最终消费者、自然媒介、真实观看/播放环境和最终验收，再决定
画布、母版、导出与证据。只有最终消费者是 Web 或合同明确要求用户侧网页交付时，才加载
`$build-polished-visual-frontends`；网页若只是查看器、内部预览或证据索引，不构成加载理由。
非网页媒介不能用浏览器截图、DOM 或网页渲染冒充目标消费者证据，必须在实际印刷、原生应用、
播放器、展陈、设备或约定 renderer 中检查最终像素、时间表现和使用条件。

## 1. 领域路由

当去掉标题、标签和控件后，画面中的主体、动作、空间、氛围或隐喻仍承担主要价值时，
以本领域为主。先选一个主子型：

- 编辑/概念插画：用具体画面回应议题或表达抽象关系；
- 叙事场景：让事件、因果、阻力、结果和环境反应可见；
- 角色、群像与生物：用身体、剪影、表演和关系建立身份；
- 环境与世界观：用地点、尺度、气候和生活痕迹建立场所；
- 物件与自然主体：以轮廓、连接、材质和观察角度说明对象；
- 动画场景：时间变化、表演或镜头运动本身承担叙事；
- 抽象/程序化：用可解释的形状关系、规则或参数表达非具象命题。

若长期识别、独立符号、传播转化、版面阅读、精确数值/结构/位置、可操作工作流、
交互解释或游戏规则才是主要价值，路由到相应领域；插画仅作为其资产时，本 Skill
只拥有该资产的画面 claims，不拥有外层系统。SVG、HTML、3D 或动画格式不决定领域。

## 2. 领域接口

### Scope 与 claims

在共享合同中增加：任务模式、视觉命题、主子型、最终消费者、自然媒介、观看距离、
版位/镜头、必须读出的对象与关系、允许省略项、事实/表现边界、真正需要的消费者变体，
以及每个消费者的最终验收方法。对象数量、草图数量、读者人数和比例数量不得作为通用
质量阈值；按 claim、风险与消费环境生成验证计划。

把 claims 分开记录：首读叙事、构图层级、动作/接触/受力、空间连续、光色一致、
媒介真实性、跨载体重构、母版可编辑性、来源/许可和动画连续性。未测试的 claim
必须进入 `not_evidence_for` 或 `excluded_claims`，不能由邻近 claim 外推。

先声明 `Create / Edit / Diagnose / Audit`。`Audit` 复用 `Diagnose` 的只读语义，只扩大核对范围，
不获得写入、制作或补证权限。Create/Edit 的正式制作只写用户授权范围；若为证明专业工具的
可编辑性、重开或导出能力而需要额外写入，只能在授权 workspace 从输入建立一个一次性验证
副本，操作前记录输入 hash，操作后记录副本与输出 hash，并保持原输入不变。
`Diagnose/Audit 一律零写入`：不创建验证副本，不保存、导出、重建、改元数据或更新缓存；
只核对既有母版、交付、记录和 hash，无法只读证明的能力如实标为未验证，不为补证计算或登记
新的 proof/build/output hash。proof 与最终交付的母版、派生输出 hash 分别记录；proof 不得
改变 canonical、release 或 promotion。

### Authority

为构图与场景几何、角色/物件设计、光色、外部资产、时间/镜头和消费者变体分别指定
唯一 authoritative owner。选定工具的原生项目或不可拆分的版本化 bundle 是对应 concern
的 master-of-record；扁平图、视频、网页查看器、截图和 Gallery 缓存都是 derived。
混合制作可以有多个母版，但同一 concern 不得双主；修改 derived 时返回其 owner。

### Tool selection

Create/Edit 先完成构图、动作、空间和叙事的低成本证明，再锁生产工具与母版；Diagnose/Audit
只核对已有证明，缺失时标为未验证，不制作证明。按
`任务子型 → 所需能力 → Registry 候选 → 选择理由 → 原生模型 → 权威母版 → 导出`
选择；工具一旦选定，其图层、节点、相机、时间线、资产链接或参数系统必须成为制作语言。

Registry 的 `available / unavailable / human_handoff` 是环境事实；任务中的
`used / considered / rejected` 由真实 evidence 决定。未安装、商业或 GUI 工具不得
写成已使用；环境不可用也不等于工具缺能力。只有可运行 capability probe 对合同所需能力
返回 `fail`，才可自绘/自研缺失部分，并记录 gap、最小自定义边界与一致性证据。

Create/Edit 中，画面质量依赖构图、画风、材质、光影、有机形体或系列一致性时，锁定方向后默认
使用 `image_generate` 创建生产画面，并用 image edit 完成局部修正、构图变体与系列延展。成功调用、
原图查看、编辑 lineage、最终消费者引用和像素检查共同构成使用证据；生成文件未进入交付不算使用。
工具不可用或调用失败返回能力缺口，不授权改用手写 SVG、CSS 或 Canvas 模拟。若消费者合同要求
正式可编辑矢量母版，生成图只作批准的视觉母图，后续必须交给专业矢量工具并保留 lineage；模型
手工描 path 不算可编辑专业母版。

复杂多人、人体接触、有机表情或高风险透视不得手写 SVG/Canvas/CSS。只有程序化几何、精确数据、
文字或拓扑本身就是视觉命题时，代码才可拥有该最小 concern；图像生成失败不能把审美绘制改写为
程序化几何任务。

## 3. 领域 gates

每个适用 gate 使用共享记录字段：`trigger / inputs / actions / evidence_refs /
claim_ceiling / failure_return`。不可用、未实现或未测试不能写成 `not_applicable`。

### Gate 1 — `illustration.contract-lock`

- **触发：** 创建、实质编辑，或 Diagnose/Audit 需要判断画面是否满足用途。
- **输入：** 共享合同、任务模式、最终消费者、内容/事实来源和现有画面。
- **动作：** Create/Edit 锁定主子型、视觉命题、自然媒介、观看/播放条件、必须关系、允许省略、
  风险 claims 与最终验收路径；Diagnose/Audit 只从既有请求和证据复原同一合同并标出未知项。
- **晋升证据：** Create/Edit 为每项显式要求定义可观察检查；Diagnose/Audit 只引用既有合同、
  消费者证据和缺口清单，不创建新的产物证据。
- **Claim ceiling：** 只允许“范围与验证假设已定义”。
- **失败返回：** 共享 routing/scope；保留已核实素材和观察。

### Gate 2 — `illustration.direction-lock`

- **触发：** Create/Edit 需要新视觉方向、风格迁移或现有方向缺少依据；Diagnose/Audit 仅在请求
  核对既有方向依据时适用。
- **输入：** 合同、经核验参考和主题中的对象/动作/环境/材料。
- **动作：** Create/Edit 提取“学机制/勿抄表面”，形成少量可证伪方向与复杂度预算；
  Diagnose/Audit 只核对既有参考、方向记录和消费者画面，不画方向稿或重建参考局部。
- **晋升证据：** Create/Edit 用方向草图说明焦点、空间、值组、媒介痕迹和主题特异性；
  Diagnose/Audit 只列既有依据与缺口，缺失项标为未验证。
- **Claim ceiling：** Create/Edit 只允许“方向假设成立”；Diagnose/Audit 只支持既有依据的观察，
  均不允许称成品或原创性已验证。
- **失败返回：** Gate 1；参考与合同矛盾时重写命题。

### Gate 3 — `illustration.composition-proof`

- **触发：** Create/Edit 出现新构图、比例改变或焦点/首读风险；Diagnose/Audit 仅在请求核对
  既有构图证据时适用。
- **输入：** 方向、目标画幅、叠字/裁切约束和必要关系。
- **动作：** Create/Edit 用缩略大形证明焦点、负空间、视线、值组和关系位置；Diagnose/Audit
  只核对既有目标消费者画面、缩略/灰度记录，不制作缩略、灰度导出或新 hash。色条、边框、角标、
  图标框和其他辅助标记必须参与构图、动作、空间或视觉命题；只为“显得完整”就删除。
- **晋升证据：** Create/Edit 用无标题首读、缩略/灰度和目标尺寸检查支持限定 claims；
  Diagnose/Audit 只登记既有 coverage 与未验证项。
- **Claim ceiling：** Create/Edit 最多为 `composition candidate`；Diagnose/Audit 只支持观察结论，
  均不覆盖人体、光色或最终质量。
- **失败返回：** Gate 2；若合同强迫不可读密度则回 Gate 1。

插画作为页面背景时负责建立开场或作品世界；后续资产优先由同一母图 image edit 派生，不要求每个
区块重复铺图。外层排版和 CSS 可以裁切、遮罩并提供可读性 scrim，但不能用渐变、边框和伪元素另造
插画细节，也不能把成图降格为“背景图 + 半透明文字板”。事实边界或必要说明邻近可能误认的对象，
其视觉权重与风险相称；风险不是主要内容时，不让说明反客为主。

### Gate 4 — `illustration.scene-logic-lock`

- **触发：** 画面包含动作、人体/生物、承重、遮挡、复杂空间、连续道具或动机光。
- **输入：** 选定构图与最高风险的代表性局部。
- **动作：** Create/Edit 证明剪影、归属、关节、接触、受力、透视、空间层级、光源和事件因果；
  Diagnose/Audit 只核对既有风险局部、整图与母版结构，不生成局部或修正稿。
- **晋升证据：** Create/Edit 用真实目标尺寸的风险局部与整图闭合；Diagnose/Audit 只登记既有
  coverage 与未验证项；不适用项均须有子型理由。
- **Claim ceiling：** 只覆盖已证实的 scene-logic 组件；只读观察不得外推，均不证明整件完成。
- **失败返回：** 关系错误回 Gate 3；媒介无法表达所需结构则进入工具候选重选。

### Gate 5 — `illustration.tool-master-lock`

- **触发：** Create/Edit 在场景逻辑可行、准备扩展整件或承诺专业交接时适用；Diagnose/Audit
  仅在请求核对既有工具与母版证据时适用。
- **输入：** 所需能力、Registry 档案、探针、现有原生母版和许可条件。
- **动作：** 选择真实可用工具并建立 authority graph。Create/Edit 如需写入式专业 proof，
  只操作授权 workspace 内的一次性验证副本；Diagnose/Audit 一律零写入，只对既有文件做
  只读结构检查，不运行会创建输出、缓存或副作用的重建。
- **晋升证据：** Create/Edit 记录版本/许可、真实调用、输入 hash、必要验证副本与输出 hash、
  母版、重开、重建/导出和当前消费者像素；Diagnose/Audit 只核对既有版本、调用记录、hash、
  母版结构和历史消费者证据，不补做验证副本、重开、重建、导出或新 hash。最终交付另记 hash。
- **Claim ceiling：** 写入证明只支持该 concern 的工具路径与可编辑性；只读诊断只支持结构观察，均不证明审美质量。
- **失败返回：** 返回候选选择或 `human_handoff`；探针 `unavailable` 不授权自研。

### Gate 6 — `illustration.artwork-variant-build`

- **触发：** Create/Edit 在前述证明通过且合同要求完成整件、系列或消费者变体时适用；
  Diagnose/Audit 仅在请求核对既有变体时适用。
- **输入：** 已锁构图、scene logic、母版和明确的变体合同。
- **动作：** 先路由安全裁切、局部重排、重新构图或独立变体。Create/Edit 从权威母版执行所选
  分支，只有重排/重构分支才改变焦点或动作端点；Diagnose/Audit 只比较既有变体，不派生、
  导出、保存或生成新 hash。安全裁切不得冒充重构。
- **晋升证据：** Create/Edit 记录当前整图、各最终消费者表现、必要局部、变体差异、lineage 与
  交付 hash；Diagnose/Audit 只引用既有输出、lineage 与 coverage，缺失项标为未验证。
- **Claim ceiling：** Create/Edit 形成 candidate；Diagnose/Audit 只支持既有变体的观察结论；
  只有作者检查时，视觉/领域 claims 仍不得进入正例池。
- **失败返回：** 返回首个失效的 Gate 3、4 或 5，保留无关已通过证据。

### Gate 7 — `illustration.domain-review-promotion`

- **触发：** 请求领域质量结论、few-shot 晋升或更强 claims。
- **输入：** 既有 `build_hash`、领域 gate、真实消费者像素、tool/authority 和 review evidence；
  Diagnose/Audit 若缺少既有 hash 或消费者证据，直接标为未验证。
- **动作：** Create/Edit 按 claim/risk 在真实最终消费者中执行最终验收；Diagnose/Audit 只核对
  既有像素、播放与记录，不新渲染、导出、计算 hash 或写 promotion；两者都限定 scope。
- **晋升证据：** Create/Edit 为每条正向 claim 提供角色相称、hash 绑定的 coverage；
  Diagnose/Audit 只报告既有 coverage、限制与排除项，不补造证据。
- **Claim ceiling：** `SELF_REVIEW_ONLY` 仅支持技术/协议 claim；视觉正例最多为
  `internal-positive` 且需项目接受的非自证据；全量独立 coverage 才可
  `externally-validated`。
- **失败返回：** 回到拥有失败 claim 的 gate；不得用改 Gallery 标签修复作品或证据。

## 4. 停止与边界

达到合同范围、适用 gate 和声明所需 evidence 后停止。继续增加角色、细节、纹理、
镜头、媒介或变体必须由新需求触发。Gate 通过不自动改变 lifecycle、canonical、
release、few-shot identity、positive pool 或 assurance；promotion 始终是 hash 绑定、
scope 限定的独立记录。
写入式专业 proof 始终是可丢弃、hash 绑定的验证活动，不改变 canonical 或 promotion；
最终交付必须用自身的母版与消费者输出 hash 取得验收，不能继承 proof 的通过状态。
