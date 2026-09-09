# 图标与符号工具能力档案

这些是领域候选档案，不证明共享 Tool Registry 已登记，也不证明某次任务真实使用。先按任务能力查询相同 candidate id；若 Registry 缺项，记录待协调，不私自创造事实。某次任务的 `used / considered / rejected / unavailable / human_handoff` 必须写入 artifact tool-use evidence。

工具选择不统一表面风格。选定后，其原生数据模型、权威母版、组件/状态与导出关系必须进入 authority graph。若候选不可运行，换可用替代或人工交接；只有实际 capability probe 的 `fail` 才允许最小自定义。

## 1. UI 组件化矢量母版

- **任务子型：** 已有设计系统中的 UI 图标家族、同一图标的尺寸/样式/语义状态变体。
- **需要能力：** 协作矢量编辑、主组件、受控属性、共享库、批量导出与开发交接。
- **候选工具 / stable candidate id：** Figma Design / `figma-design`。
- **原生设计语言或数据模型：** vector nodes、main component、component set、variant properties、variables/modes 和 library instances；不同语义图标不塞进同一 variant set。
- **权威母版：** 项目 Figma library 中的主组件与几何节点；导出 SVG/PNG 和页面实例为 derived。
- **为何选择：** 宿主团队已用 Figma 管理组件、属性和交接，且需要多人维护同一图标系统。
- **何时不选：** 无真实席位/文件访问，或任务是注册安全原件、开放 SVG 唯一源、字体或物理生产专用母版。
- **替代：** `inkscape`、`adobe-illustrator`；组件治理缺失时另由宿主设计系统接管。
- **真实使用证据：** 文件/团队访问、版本与许可、真实 GUI/API 操作、组件 ID 与属性、可编辑节点、发布/导出轨迹、输出 hash、生产组件导入和当前像素。

## 2. 开放 SVG 矢量母版

- **任务子型：** 有限 UI、公共信息、设备或品牌微缩符号，需要离线开放格式和确定性导出。
- **需要能力：** SVG 节点/路径、布尔、复合路径、mask/clip、XML 检查、CLI 导出与跨工具重开。
- **候选工具 / stable candidate id：** Inkscape / `inkscape`。
- **原生设计语言或数据模型：** SVG/XML 元素、paths、strokes、clones、layers 与 Inkscape editor metadata。
- **权威母版：** 保留可编辑结构的 Inkscape SVG；批准的 plain SVG、sprite 和位图为 derived。
- **为何选择：** 合同要求开放、离线、浏览器兼容且可被脚本检查的矢量 authority。
- **何时不选：** 团队权威组件位于另一原生系统，或需要 Apple 多权重 symbol、复杂字体轴或商业供应链指定格式。
- **替代：** `adobe-illustrator`、`figma-design`，或已有项目 SVG authority。
- **真实使用证据：** 安装、解析版本、GPL 许可、真实打开/编辑/CLI 调用、可编辑 SVG、plain SVG 导出、重开、hash 与目标 renderer 像素。

## 3. 印刷与物理生产矢量母版

- **任务子型：** 公共/设备标牌、印刷、切割、雕刻、刺绣及供应商交接的复杂矢量。
- **需要能力：** 精细节点与曲线、复合形、Symbols/实例、色彩与印前、PDF/SVG 导出和供应商重开。
- **候选工具 / stable candidate id：** Adobe Illustrator / `adobe-illustrator`。
- **原生设计语言或数据模型：** `.ai` document、artboards、layers、paths、appearance、symbols 和 export presets。
- **权威母版：** 可编辑 `.ai` 文件或供应链约定的版本化 bundle；PDF/SVG/切割文件为 derived。
- **为何选择：** 真实供应商和生产流程以 Illustrator 原生对象、预设或 PDF 交付为权威接口。
- **何时不选：** 无商业席位/GUI 操作、合同要求开放 SVG authority，或只需平台现成图标。
- **替代：** `inkscape`；必须由供应商操作时记录 `human_handoff`。
- **真实使用证据：** 产品/席位与许可、版本、真实 GUI 操作、可编辑 `.ai`、Symbols/图层、导出预设、重开/供应商确认、文件 hash、实体或目标像素。

## 4. Apple 平台符号系统

- **任务子型：** Apple 平台 UI 中的现成或 custom symbol、权重/尺度/渲染层和平台动画。
- **需要能力：** 平台符号语义、weight/scale、rendering modes、layer annotations、Xcode 资产消费与辅助功能。
- **候选工具 / stable candidate id：** SF Symbols / `apple-sf-symbols`。
- **原生设计语言或数据模型：** Apple symbol template、分层路径、weights/scales、rendering annotations、variable/animation metadata。
- **权威母版：** 对现成符号为固定版本的平台依赖；对 custom symbol 为可编辑源、SF Symbols 模板/项目与 Xcode asset 的版本化关系。
- **为何选择：** 真实消费者是 Apple 平台，已有系统符号可表达语义，或 custom symbol 必须遵守平台模板。
- **何时不选：** Web/Android/Windows、公共或安全标志、身份标志，或当前环境没有 macOS/GUI。
- **替代：** `google-material-symbols`、`microsoft-fluent-system-icons`、`figma-design`、`inkscape`。
- **真实使用证据：** macOS、SF Symbols/Xcode 版本与许可、真实导入/校验、模板和权重/尺度、asset catalog、编译运行、VoiceOver/方向与实机像素。

## 5. Material 平台符号系统

- **任务子型：** 采用 Material 语言的 Android/Web UI，优先复用现成语义和光学轴。
- **需要能力：** 官方符号检索、SVG/字体消费、`FILL / wght / GRAD / opsz` 轴、Android/Web 集成。
- **候选工具 / stable candidate id：** Material Symbols / `google-material-symbols`。
- **原生设计语言或数据模型：** 官方 glyph set、variable-font axes、命名语义和平台包；`opsz` 是系统提供的光学维度。
- **权威母版：** 现成符号由固定版本官方依赖拥有；项目配置只拥有选取、轴值和消费映射。
- **为何选择：** 宿主明确采用 Material，现成 glyph 与 referent 匹配并能减少不必要自绘。
- **何时不选：** 新颖运营/安全含义、品牌身份、非 Material 宿主，或为了风格统一强改官方语义。
- **替代：** `apple-sf-symbols`、`microsoft-fluent-system-icons`；缺失语义才进入自定义矢量候选比较。
- **真实使用证据：** 固定包/仓库/字体版本与 Apache-2.0、真实依赖和 import、glyph/axis 配置、自托管或原生调用、宿主可访问名称与各目标 `opsz` 像素。

## 6. Fluent 平台符号系统

- **任务子型：** Fluent 宿主中的 Web、Windows、iOS、Android 或 Flutter UI 图标复用。
- **需要能力：** 官方 regular/filled 资产、离散光学尺寸、方向 metadata、多平台包与 SVG 消费。
- **候选工具 / stable candidate id：** Fluent UI System Icons / `microsoft-fluent-system-icons`。
- **原生设计语言或数据模型：** 命名资产、size/style variants、direction metadata、SVG 与平台 packages。
- **权威母版：** 现成符号由固定版本官方包拥有；项目只拥有选择、包装、别名与宿主映射。
- **为何选择：** 真实产品采用 Fluent，且官方资产覆盖所需语义、尺寸、样式和方向。
- **何时不选：** 非 Fluent 产品、公共/安全/品牌符号，或新语义没有官方 referent。
- **替代：** `google-material-symbols`、`apple-sf-symbols`、`figma-design`、`inkscape`。
- **真实使用证据：** 固定版本/MIT、真实 package 安装或 SVG import、所选 size/style、RTL metadata、生产组件、可访问性与目标平台像素。

## 7. 图标字体生产流水线

- **任务子型：** 消费者明确要求 icon font、变量字体、固件字库或既有字体接口兼容。
- **需要能力：** glyph master、UFO/DesignSpace、OpenType 构建/子集、glyph map、基线、hinting 与验证。
- **候选工具 / stable candidate id：** UFO/DesignSpace + fontTools pipeline / `icon-font-ufo-fonttools-pipeline`。
- **原生设计语言或数据模型：** UFO glyph sources、DesignSpace axes/rules、feature data 与 OpenType build graph。
- **权威母版：** UFO/DesignSpace/feature 源及构建配置； TTF/OTF/WOFF 为 derived。
- **为何选择：** 真实宿主只能或明确需要字体接口、插值轴、子集与稳定 glyph mapping。
- **何时不选：** 普通 UI 可直接使用 SVG/原生资产，或字体会损害语义、颜色、可访问性与维护性。
- **替代：** `inkscape`/平台包；GUI 字形编辑可 `human_handoff` 给 Glyphs、FontLab 或 FontForge 流程。
- **真实使用证据：** 依赖、fontTools 与源格式版本/许可、真实构建命令、UFO/DesignSpace、glyph map/PUA 策略、确定性输出 hash、字体检查、宿主基线与 fallback 像素。

## 8. 状态与动效符号

- **任务子型：** 动画本身传达状态、进度或可交互反馈，并需跨运行时数据绑定。
- **需要能力：** artboard、矢量形状、动画、state machine、data binding、runtime 导出和静态降级。
- **候选工具 / stable candidate id：** Rive / `rive`。
- **原生设计语言或数据模型：** artboards、shapes、animations、state machines、transitions 和 view-model/data-binding properties。
- **权威母版：** 可编辑 Rive 项目与状态模型；版本匹配的 `.riv` 是 runtime derived output。
- **为何选择：** 状态图和运行时输入是交付核心，宿主确实使用受支持 Rive runtime。
- **何时不选：** 静态图标、简单一次性 CSS/平台反馈、受监管标志，或没有真实 runtime 集成。
- **替代：** 宿主平台原生动画；线性时间线需要时人工交接到既有动效流水线。
- **真实使用证据：** 编辑器/runtime 版本与许可、真实项目操作、可编辑 artboard/state graph、export、应用 import、输入/转移轨迹、首中末帧、取消/终态和 reduced-motion 证据。

## 9. 符号理解实验

- **任务子型：** 无标签自由命名、知觉质量、指代关联、最近邻混淆或短时识别实验。
- **需要能力：** 刺激随机化、trial/timeline、响应与时序记录、原始数据、协议复现和分析导出。
- **候选工具 / stable candidate id：** jsPsych / `jspsych`。
- **原生设计语言或数据模型：** browser experiment timeline、trial plugins、stimulus parameters、response events 和 per-trial data。
- **权威母版：** 协议源码、刺激物 hash、随机化/编码规则与分析脚本；实验页面和结果表为 derived。
- **为何选择：** 浏览器控制实验能覆盖所需方法，且目标人群、设备和环境限制已纳入合同。
- **何时不选：** 需要实体标牌、严格实验室设备、现场任务、参与者招募或统计/伦理审批本身。
- **替代：** 主持式目标用户研究、实验室工具或现场人因测试的 `human_handoff`。
- **真实使用证据：** package/version/license、真实运行、协议与刺激 hash、随机化/时序、参与者标准、去标识原始数据、编码与分析、环境限制和 role-matched review。

## 10. Web 宿主组件消费验证

- **任务子型：** SVG、sprite、字体或图标组件在 Web 设计系统中的真实状态、主题、RTL 与交互消费。
- **需要能力：** 隔离 stories、args/state、生产包 import、interaction/a11y checks 和视觉回归。
- **候选工具 / stable candidate id：** Storybook / `storybook`。
- **原生设计语言或数据模型：** component stories、args、play/interaction functions、addons、test runner 和 snapshots。
- **权威母版：** Storybook 不拥有图标几何；stories 可拥有消费者测试场景，生产组件包拥有宿主接口。
- **为何选择：** 真实 Web 项目已经使用 Storybook，且需证明生产组件中的尺寸、主题、RTL、状态与可访问关系。
- **何时不选：** 原生平台、物理标牌、用户理解或现场有效 claim；它也不能替代矢量母版。
- **替代：** 目标平台 sample app、Xcode/Compose/Flutter 测试宿主或物理样片。
- **真实使用证据：** package/version/license、真实配置与启动、stories 导入生产资产、args/interaction/a11y 结果、视觉快照、资产 build hash 和消费者限制。
