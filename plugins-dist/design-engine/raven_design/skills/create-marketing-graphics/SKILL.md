---
name: create-marketing-graphics
description: 为明确受众、传播阶段与渠道任务创建、改版或诊断营销传播图形，包括 campaign 主视觉、社交与广告套系、发布与促销、海报户外、门店物料、动效及多尺寸生产；当工作需要统一承诺、证据、行动、资产谱系和跨媒介视觉角色，并以原生母版和真实渠道证据交付时使用。
---

# 创建营销传播图形

本 Skill 是 `visual-artifact-design` 的营销传播领域层，拥有 campaign 子型、传播真相、渠道适配、
专业工具路由和领域 gate。安全检查、authority graph、渲染复核、assurance 与交付语义沿用共享底座，
不另造“完成度”。详细模式见 [patterns.md](references/patterns.md)，工具能力见
[tool-profiles.md](references/tool-profiles.md)。

本领域始终以介质中立的方式定义最终消费者：记录实际承载传播的设备、平台、播放器、目标 renderer、印刷/制作链或现场环境，并以该消费者收到的最终像素、成品样张或最终验收结果为准。只有最终消费者或合同交付明确为 Web 时，才加载 `$build-polished-visual-frontends`，由其补充前端技术栈、响应式、DOM/CSS 和浏览器渲染批评；非网页媒介不能用浏览器 DOM、网页截图或 HTML 包装冒充目标消费者。

## 1. 路由与输出合同

- **Create**：从权威事实和渠道合同建立 campaign，再构建已要求的 rendition 或 channel output。
- **Edit**：保留当前母版与 build，先做变更影响图；所有受影响输出标为 `stale`，不得直接修导出物。
- **Diagnose**：只读复现现有 build 与渠道证据，定位最早失效 gate；没有修复授权时只报告事实、推断与待验证假设。
- **Audit**：只读核对 campaign record、母版、权利、消费者证据与 claims 的覆盖关系，不为补齐检查而生成新导出或改写状态。
- **写入边界**：Create/Edit 若必须证明专业工具能够重开、编辑或导出，只能在用户授权的 workspace 内从当前输入创建一次性验证副本；`Diagnose/Audit 一律零写入`，不得创建副本、导出物、缓存或状态记录。证据不足时返回 `unknown`、`blocked` 或请求转入获授权的 Create/Edit，而不是用写入补证。
- 主型可选 campaign system、社交/广告、零售/促销、活动/户外、产品发布、内容/议题传播或动效传播；混合任务仍指定一个主型。
- 若主要价值是长期身份、连续论述、独立插画、空间导航或产品操作，转交相邻领域；HTML 预览不把营销物自动变成网站。

输出范围只能来自合同：一个真实触点、有限套系或 campaign package。不要为展示“系统性”自动增加渠道、比例、动效、说明板或应用外壳。

## 2. 权威 campaign record

先建立一个机器可读或结构稳定的 campaign record，作为下列 concern 的唯一权威拥有者：

- 受众、阶段、阻力、单一承诺、证明及其来源；
- 行动、承接入口、时间窗口、价格、资格、法务和下架条件；
- 品牌、产品、人物、字体、图像和生成资产的稳定 asset id 引用及其传播角色；
- 视觉角色：主对象、信息层级、字体职责、颜色职责、证据位置和 CTA 关系；
- 每个渠道的任务、最终消费者、`must/adapt/omit`、原生规格、遮挡、channel output 与验收来源。

一个 concern 只能有一个 owner。Campaign record 拥有承诺、条件、行动及 asset id 引用；asset manifest 拥有这些 id 对应的文件版本、权利和批准状态；原生 layout master 拥有本媒介的构图与排版；export recipe 拥有从母版到交付格式的转换。模板族可以由一个明确版本化的 native bundle 作为 layout concern 的唯一 owner；媒介或比例出现独立构图真值时才拆分母版。PDF、PNG、视频等是 derived deliverable，拼图、QA board 和 Gallery 是 review index；二者都不得成为上游 authority。

## 3. 七个领域 gate

每个 gate 记录 `input / action / promotion evidence / failure signal / failure return / claim ceiling`。Gate `status` 只使用 `pending / passed / failed / blocked / not_applicable`；未触发项写有 scope 理由的 `not_applicable`，未知、不可用和未测试不能写成不适用。`unknown` 与 `waived` 仅用于相应 evidence check；waiver 不等于 gate 通过。

### G1 · CAMPAIGN CONTRACT

- **Input**：用户范围、受众、阶段、传播阻力、事实源、成功任务与指定渠道。
- **Action**：冻结 campaign record；区分认知结果与行为 CTA，核实会改变承诺的事实和限制。
- **Promotion evidence**：每项主张可追溯到来源，每个要求有验收项，承接入口确实存在。
- **Failure / return**：承诺含混、事实冲突、假 CTA 或高风险未知时停止，返回范围与事实确认。
- **Claim ceiling**：只证明传播合同完整，不证明视觉质量、受众理解或渠道效果。

### G2 · TOOLCHAIN & MASTER

- **Input**：任务子型、所需能力、可用工具证据、现有资产与交接环境。
- **Action**：按 Tool Registry 选择工具，登记选择理由、原生设计语言、authority owner、母版、导出路径与使用证据。
- **Promotion evidence**：依赖/版本/许可、真实调用、可编辑母版、重建导出和当前最终消费者证据齐备。
- **Failure / return**：工具不可用则标 `candidate` 或 `human_handoff`；母版不可重开或只借单一功能时返回能力选择。
- **Claim ceiling**：只证明所选工具链和母版关系可执行，不证明 campaign 正确。

### G3 · CAMPAIGN GRAMMAR

- **Input**：campaign record、批准资产、锚点触点与最异质渠道输出的代表性 proof。
- **Action**：锁定承诺、证据、行动、资产谱系与视觉角色的一致关系，并明确哪些布局关系可随媒介变化。
  色条、边框、角标、图标容器或背景场只有在跨所需 rendition 持续承担 campaign 语法或品牌身份时
  才保留；一次性“强调条”不是 grammar。
- **Promotion evidence**：差异最大的 rendition 仍表达同一承诺和阶段，asset id 与角色一致，信息变化符合 `must/adapt/omit`。
- **Failure / return**：只剩颜色相似、主张/CTA 漂移、旧新资产混用或语气换代时，返回合同或代表性 proof。
- **Claim ceiling**：只证明 scoped campaign grammar，不证明所有渠道完成。

### G4 · CHANNEL ADAPTATION

- **Input**：已通过的 grammar、每个指定渠道合同、原生布局母版和真实内容极值。
- **Action**：按观看距离、停留、裁切、遮挡和媒介行为重新编排；按承诺、证据、行动与法务风险
  分配视觉权重，更新依赖后传播 `stale`。必要条件和限制邻近其约束的 claim 并保持可读，但不因
  “必须出现”自动成为最强区域。
- **Promotion evidence**：每个 channel output 由对应 layout concern 的原生母版或版本化 native bundle 生成，关键承诺与条件可读，stale 输出已重建复核。
- **Failure / return**：机械裁切、微字、关键对象断裂或 rendition 漂移时回本渠道母版；grammar 破坏则回 G3。
- **Claim ceiling**：只覆盖已列渠道和已测上下文，不外推到其他平台或尺寸。

### G5 · PROVENANCE & RIGHTS

- **Input**：全部可见资产、字体、品牌锁定、数据/引语、生成链和渠道使用范围。
- **Action**：按 asset id 记录来源、许可、同意、批准状态、编辑链、披露、地域/期限及替换责任。
- **Promotion evidence**：每个生产资产权利可追溯且覆盖目标用途；占位、概念和过期资产不会进入发布包。
- **Failure / return**：来源缺失、真实人物/产品被合成冒充、许可范围不足时回资产采购或停止发布。
- **Claim ceiling**：只证明记录范围内的使用权与真实性边界，不证明传播效果。

### G6 · CONSUMER & CHANNEL EVIDENCE

- **Input**：当前 build、最终消费者定义、最终 derived deliverable、目标 renderer/设备/平台/样张、承接端和专业检查合同。
- **Action**：在消费者真实接收的缩放、裁切、遮挡、转码、播放、远距、印前或制作条件下检查内容、最终像素或最终验收、文件和行动路径。
- **Promotion evidence**：证据绑定消费者、介质、环境、任务、输入/输出 hash、build hash 与结果；最后修改后从权威母版重新导出并复验。
- **Failure / return**：只看源码/总览、用浏览器证据替代非 Web 消费者、失效链接、转码破坏或印前失败时，回对应母版或导出链。
- **Claim ceiling**：自检最多支持技术与视觉观察；没有目标受众证据不能声称理解或有效。

### G7 · CLAIM-BOUND RELEASE

- **Input**：通过的适用 gates、authority/tool/rights/consumer 证据、已知限制与当前 build hash。
- **Action**：分别记录 work status、lifecycle、release/canonical、assurance 和 scoped promotion；清除或披露所有 stale。
- **Promotion evidence**：每条 claim 只引用适格 reviewer 和对应证据；发布包、原生母版和派生索引角色清楚。
- **Failure / return**：缺 gate、旧 build review、未清 stale 或 status inflation 时返回最早失效 gate，不得带病晋升。
- **Claim ceiling**：release 只证明交付；positive pool 只证明其 claims；两者互不推出。

## 4. 工具与自定义边界

按“子型 → 能力 → candidate id → 选择理由 → 原生语言 → master → custom boundary → 使用证据 → failure return”选择工具。选定后必须继承其原生组件/样式、图层与资产模型、母版和导出方式；只借一个功能而主体随意手写，G2 判失败。

没有真实安装、权限、许可和调用证据时，不得把 Figma、Adobe、Canva、DAM 或其他 GUI/商业工具写成 `used`。程序化生成只有在渠道规模、确定性或数据驱动确有需要时采用，并保留权威内容输入、模板源码、可重建输出和像素证据。

自绘或自研前必须先运行能力 probe；只有可运行条件下观察到的 gap 才允许定义 `minimal custom boundary`，随后证明它与所选原生系统在内容、资产、视觉和导出上连贯。偏好手写不是 gap。

Campaign 主视觉可作为所需页面族或渠道族的开场母版，并用 image edit 派生构图；不要求每一触点
重复铺大背景。后续通过同源资产、字体、构图关系与运动延续。Web/CSS 只承担布局、裁切、遮罩、
必要分隔和真实反馈，不用渐变、伪元素或边框伪造精致资产，也不退化成“背景图 + 半透明文字板”。

专业工具 proof 与正式生产严格分离。Create/Edit 的一次性验证副本只能验证声明中的最小能力，操作前记录输入 hash，操作后记录输出 hash、调用轨迹和结果，并在证据中指向而不替换最终交付；最终交付本身另记母版与 derived deliverable hash。验证副本及其结果不得改变 authority owner、canonical、release 或 promotion，也不得被包装成新产物。Diagnose/Audit 只能读取已有 proof 与现有 hash；缺失专业 proof 时如实降低 claim ceiling。

## 5. Promotion 与 assurance 边界

- `internal-positive` 只能覆盖当前 build 中被内部证据支持的具体 grammar、母版、渠道或生产 claim。
- `externally-validated` 需要独立于作者、角色适格且绑定同一 build hash 的 reviewer 对具体 claim 明确通过；合格内部独立 reviewer 可以提供该 coverage。
- `SELF_REVIEW_ONLY` 只说明作者自检，不能证明目标受众理解、渠道有效、广告效果、权利签认或生产发布。
- 受众理解需要受众任务证据；渠道有效需要真实渠道或适格渠道 reviewer；广告效果需要投放数据与明确归因；生产发布需要 release record。

## 6. 交付与边界

交付 campaign record、各 concern 的原生母版、依赖与 stale 状态、derived deliverable、最终消费者/渠道证据、权利记录和已知限制。不要把 Gallery、截图、拼图或总览 SVG 称为 campaign master。

若相邻领域拥有独立真值或交付，调用其 Skill；否则只继承批准资产，不扩写长期品牌系统、完整网站、产品界面、独立插画或编辑出版物。
