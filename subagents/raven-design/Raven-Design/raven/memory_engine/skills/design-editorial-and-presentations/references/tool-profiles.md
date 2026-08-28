# 编辑与演示设计：工具能力档案

本文件是领域选择层，不是 Tool Registry，也不证明工具已经安装或用于某个产物。下列 ID 是待登记的稳定候选；实际版本、许可、平台、可用性和调用方式必须以共享 Registry 的已核验事实为准。

- `candidate_unavailable`：当前没有足够环境证据可自动调用；不等于工具没有该能力。
- `human_handoff`：当前应由有许可和合适平台的人在原生应用中完成；不得伪装成自动调用。
- 只有使用证据同时覆盖 dependency、version、license、invocation、editable master、rebuild/export、final consumer，项目记录才可写 `used`。

## 选择方法

先从媒介合同导出能力，再选择能让原生模型成为作品结构的 profile。不能只用它导出一张图或借一个控件，然后让另一套随意结构拥有同一 concern。若多个工具组成管线，必须明确每一步的输入、输出、authority 与失败返回。

## P01｜复杂分页出版：Adobe InDesign

- **任务子型**：书籍、杂志、目录、年报等需要长文流、母版、交叉引用、印前交接的分页出版物。
- **所需能力**：parent pages、paragraph/character/object styles、text threading、preflight、package、印刷 PDF 导出。
- **候选 / 状态**：`adobe-indesign-paged-publishing`（拟登记，默认 `human_handoff`；Registry 证明可调用前不得自动化）。
- **原生模型**：document/page/spread、parent page、frame、threaded story、style system、links、preflight profile。
- **权威母版**：可编辑 `.indd` 文档拥有分页布局与样式；外部文稿/数据分别拥有内容；PDF 仅为派生消费者。
- **为何选**：需要精细排版、长文流、链接资产管理与成熟印前交接时，其原生模型直接覆盖风险。
- **何时不选**：只需连续网页、协作式轻量文档，或交付方无法接收该商业格式与许可时。
- **替代**：`affinity-publisher-paged-publishing`、`scribus-paged-publishing`；数据驱动报告可转 `quarto-pandoc-report-pipeline`。
- **真实使用证据**：安装/许可与精确版本；原生应用打开记录；`.indd`、links/fonts 状态和 parent/style 清单；修改全局样式后的传播证明；package/preflight 日志；由该源导出的最终页像素或印刷 proof。

## P02｜独立团队分页出版：Affinity Publisher

- **任务子型**：品牌册、短书、作品集、活动手册等需要专业分页但不依赖 InDesign 生态的出版物。
- **所需能力**：master pages、text styles、linked frames、resource manager、preflight、PDF/X 输出。
- **候选 / 状态**：`affinity-publisher-paged-publishing`（拟登记，默认 `human_handoff`）。
- **原生模型**：publication/spread、master、frame、style、resource、preflight；不要把它降格为图片导出器。
- **权威母版**：可编辑 `.afpub` 拥有版式 concern；内容与资产仍由各自 ledger/source 拥有。
- **为何选**：需要桌面出版原语、可编辑交付与一次性商业许可路径，且接收方接受 Affinity 格式。
- **何时不选**：接收链强制 `.indd`、需要既有 InDesign 插件/脚本，或要求自动化服务器重建。
- **替代**：`adobe-indesign-paged-publishing`、`scribus-paged-publishing`。
- **真实使用证据**：安装、版本和许可；实际 `.afpub`；master/style/resource/preflight 证据；全局规则传播；可重复导出记录；最终 PDF/印刷消费者与源 hash 绑定。

## P03｜开放格式分页出版：Scribus

- **任务子型**：需要开放源文件、基础印前控制或可脚本化批处理的手册、刊物与宣传册。
- **所需能力**：page/master、text/image frames、styles、color management、preflight、PDF 导出与可审计脚本。
- **候选 / 状态**：`scribus-paged-publishing`（拟登记，当前 `candidate_unavailable`，须先做安装与能力 probe）。
- **原生模型**：`.sla` document、master page、frame、style、linked asset、preflight verifier。
- **权威母版**：`.sla` 与被声明的脚本拥有版式/自动化 concern；导出 PDF 不拥有源布局。
- **为何选**：开放格式和可复核构建比商业生态互操作更重要，并且所需排版能力经真实样张证明。
- **何时不选**：依赖复杂商业插件、接收方只接收 InDesign/Affinity，或 probe 显示关键语言/印前能力不足。
- **替代**：`adobe-indesign-paged-publishing`、`affinity-publisher-paged-publishing`、`pagedjs-browser-pagination`。
- **真实使用证据**：包与精确版本、许可、实际启动/脚本调用、`.sla` 母版、链接资产与字体状态、代表页重建、preflight/export 输出、最终页像素；probe 失败只能证明被测能力缺口。

## P04｜企业现场演示：Microsoft PowerPoint

- **任务子型**：会议陈述、决策汇报、培训或需要批注、讲者备注、企业模板互操作的现场演示。
- **所需能力**：slide master/layout、theme、notes、图表/表格、动画、放映、可编辑 `.pptx` 交接。
- **候选 / 状态**：`microsoft-powerpoint-native-slides`（拟登记，默认 `human_handoff`；只有已核验桌面或受支持自动化接口才可写 automated）。
- **原生模型**：presentation、slide master、layout、placeholder、theme、notes、timing/transition；幻灯片不是网页截图集合。
- **权威母版**：`.pptx` 的 master/layout/theme 拥有现场版式；讲稿事实由内容源拥有；导出 PDF/视频为派生消费者。
- **为何选**：接收者需要继续编辑、组织模板和会议室放映链以 PowerPoint 为共同语言。
- **何时不选**：核心消费者是连续网页、代码审阅或无法可靠保真打开 `.pptx` 的环境。
- **替代**：`apple-keynote-native-slides`、`slidev-browser-presentations`。
- **真实使用证据**：许可/版本/平台；原生应用或受支持 API 调用；可编辑 `.pptx`；master/layout/notes/timing 清单；实际放映、字体替换和重开测试；最终投影视距帧与导出消费者。

## P05｜讲者驱动演示：Apple Keynote

- **任务子型**：以现场讲述、节奏、过渡和高保真画面为主，且制作与放映链可使用 macOS 的演示。
- **所需能力**：master slides、object alignment、builds/transitions、presenter notes/display、可编辑交接与导出。
- **候选 / 状态**：`apple-keynote-native-slides`（拟登记，默认 `human_handoff`）。
- **原生模型**：document、master slide、placeholder、theme、build order、transition、presenter notes。
- **权威母版**：`.key` 拥有现场布局与动效；若交付 `.pptx`，必须把它当独立消费者验证，不能假定无损。
- **为何选**：讲者可控制原生 Keynote 环境，视觉节奏和现场演示体验优先于跨企业模板生态。
- **何时不选**：组织只接受 PowerPoint、需要服务器自动重建，或目标现场无法保证 Apple 放映链。
- **替代**：`microsoft-powerpoint-native-slides`、`slidev-browser-presentations`。
- **真实使用证据**：macOS/Keynote 版本与许可状态；实际 `.key`；master、notes、build order；本机完整放映与外接屏检查；必要导出格式逐页/逐动效对照；最终观看距离证据。

## P06｜浏览器现场演示：Slidev

- **任务子型**：技术分享、教学或可接受浏览器运行、需要代码 diff、版本控制和可重复构建的现场演示。
- **所需能力**：slide source、layout/theme、speaker notes、step reveals、syntax highlighting、静态/PDF 导出。
- **候选 / 状态**：`slidev-browser-presentations`（拟登记，当前 `candidate_unavailable`，需锁定运行时与包后再判可用）。
- **原生模型**：Markdown slide boundaries、frontmatter、Vue components、layouts、theme、click steps、presenter mode。
- **权威母版**：版本控制中的 Slidev source、theme 与 lockfile 拥有浏览器演示；截图和 PDF 为派生输出。
- **为何选**：内容由代码/公式驱动、团队以版本控制协作，并能接受浏览器作为放映运行时。
- **何时不选**：必须交付原生 `.pptx/.key`、非技术编辑者需频繁改版，或离线目标机无法锁定浏览器运行时。
- **替代**：`microsoft-powerpoint-native-slides`、`apple-keynote-native-slides`；连续长文改用 `astro-semantic-longform`。
- **真实使用证据**：package/lock/version/license；真实 build 与 present 调用；source/theme/layout/notes；无网构建与放映；键盘、presenter、reveal、导出检查；最终投影视距像素与控制台记录。

## P07｜连续网页长文：Astro 内容管线

- **任务子型**：专题长文、叙事报道、指南或文档型作品，需要语义结构、稳定 URL、连续阅读和响应式重排。
- **所需能力**：内容集合、Markdown/MDX、模板/layout、语义 HTML、图片管线、静态构建与渐进增强。
- **候选 / 状态**：`astro-semantic-longform`（Registry 已有 Web 运行时并不自动证明该候选可用；未登记前为 `candidate_unavailable`）。
- **原生模型**：content entry/schema、page layout、component island、route、asset pipeline；阅读正文应由语义文档而非幻灯片页模拟。
- **权威母版**：内容文件/schema 拥有文稿；Astro layout/components 拥有 Web 布局；构建目录和截图均为派生。
- **为何选**：主消费者按滚动、链接、查找和重排阅读，且需要可维护的静态内容系统。
- **何时不选**：需要编辑后台/CMS 工作流、复杂持续运营站点，或主合同是印刷分页/现场放映。
- **替代**：现有内容平台；轻量静态生成器；自动化报告用 `quarto-pandoc-report-pipeline`。
- **真实使用证据**：包与 lockfile、版本/许可、真实 build；content schema、layout 与组件源；增量内容修改传播；响应式/键盘/打印检查；最终 URL 消费者和像素证据。

## P08｜可重建研究与周期报告：Quarto＋Pandoc

- **任务子型**：研究报告、政策简报、周期数据报告和需要引用、参数、代码结果与多格式发布的文档。
- **所需能力**：结构化 source、citations、cross-reference、参数化执行、模板、HTML/PDF/DOCX 输出和环境锁定。
- **候选 / 状态**：`quarto-pandoc-report-pipeline`（拟登记为 pipeline，当前 `candidate_unavailable`；Quarto 与 Pandoc 版本须分别核验）。
- **原生模型**：project config、Markdown/Quarto document、YAML metadata、filters、bibliography、code cells、templates、render target。
- **权威母版**：source、数据、引用库、模板、filter 与锁定环境分别拥有其 concern；任何导出都不是唯一 master。
- **为何选**：报告必须从可追踪数据与引用反复重建，并且多输出共享内容但允许各自自然模板。
- **何时不选**：版面需要大量逐页手工艺术指导、现场动画是核心，或执行代码带来不可接受的供应链风险。
- **替代**：`pagedjs-browser-pagination`、原生桌面出版；简单网页用 `astro-semantic-longform`。
- **真实使用证据**：两工具依赖、精确版本、许可、filters/extensions；真实 render 命令；source/config/template/lockfile；干净环境重建；引用/交叉引用和失败日志；每个声明输出的最终消费者证据。

## P09｜Web 源分页：Paged.js

- **任务子型**：以 HTML/CSS 为权威布局源、需要浏览器排版后生成分页 PDF 的报告、手册或实验性出版物。
- **所需能力**：paged-media CSS、running elements、page counters、named pages、break control、可脚本化浏览器输出。
- **候选 / 状态**：`pagedjs-browser-pagination`（拟登记，当前 `candidate_unavailable`；必须先验证依赖、浏览器与字体）。
- **原生模型**：semantic HTML、CSS paged media、Paged.js handlers/polyfill、browser print pipeline；不应套用桌面出版的 frame/master 假象。
- **权威母版**：HTML/content source、print CSS 与锁文件拥有分页 Web concern；生成 PDF 为派生消费者。
- **为何选**：内容天然来自 Web/自动化管线，需要代码审阅与可重复分页，并且代表性复杂页经 probe 通过。
- **何时不选**：印前供应链要求桌面出版源、复杂排版能力 probe 失败，或仅为连续网页阅读。
- **替代**：`quarto-pandoc-report-pipeline`、`scribus-paged-publishing`、商业桌面出版 handoff。
- **真实使用证据**：包/浏览器/font 锁定、版本许可、真实 CLI/browser invocation；HTML/CSS/handler 母版；代表页 capability probe；干净重建、页数和溢出检查；最终 PDF 页像素与元数据。

## P09A｜HTML/CSS 同源出版：Vivliostyle

- **任务子型**：希望以语义 HTML/Markdown 和 CSS 同时维护连续 Web 阅读与分页 PDF 的手册、杂志、报告和短书。
- **所需能力**：CSS Paged Media、named page、running element、目录/封面、主题、浏览器预览、CLI 构建与出版级 PDF。
- **候选 / 状态**：`vivliostyle-cli-publishing`；实际 availability、版本、浏览器和字体必须由当前环境验证。
- **原生模型**：内容源、Vivliostyle config、theme/CSS、template、Viewer 与 CLI build；连续阅读和分页布局可以共享内容，不强迫共享同一 CSS 规则。
- **权威母版**：内容源拥有文稿，Vivliostyle config/theme 拥有分页 concern；PDF 和 Viewer 页面均为派生物。
- **为何选**：Web 是真实消费者，同时需要可重复的分页出版，而不是先做网页截图再拼 PDF。
- **何时不选**：接收方强制桌面出版源、复杂页型 probe 失败、只需要纯 PDF，或连续 Web 根本不是合同。
- **真实使用证据**：锁定 package/CLI/browser/font、真实 create/build/viewer 调用、代表跨页和最密页、连续 Web 与 PDF 分别重开、页数/溢出/链接/字体及最终像素。

## P09B｜可复现纯 PDF 排版：Typst

- **任务子型**：技术文档、研究材料、结构化手册和需要快速稳定编译、版本控制与高质量 PDF 的出版物。
- **所需能力**：结构化 markup、样式函数、页面/网格、交叉引用、表格/图、字体与 PDF 标准导出。
- **候选 / 状态**：`typst-pdf-publishing`；HTML 能力只有在当前版本和目标合同的真实 probe 通过后才可承担 Web。
- **原生模型**：Typst source、package/template、style function、assets 与 compiler；不模拟 InDesign frame，也不把编译后 PDF 当源。
- **权威母版**：`.typ`、本地资产、字体和锁定构建配置拥有文档/版式 concern；PDF、PNG、SVG 或 HTML 为派生输出。
- **为何选**：纯文档/PDF 的可复现性、排版速度和代码审阅比桌面出版交接或复杂 Web 适配更重要。
- **何时不选**：需要精细桌面出版 handoff、成熟 CMS/连续 Web 体验，或目标语言/页型能力未经验证。
- **真实使用证据**：compiler/package/version/license、真实 compile、可编辑 source、代表页、字体与标准设置、干净重建、PDF 结构/页面和最终像素。

## P10｜可访问 PDF 发布管线：Acrobat＋独立验证器

- **任务子型**：在分页、演示或报告主合同上叠加 PDF/UA 或组织无障碍交付要求。
- **所需能力**：上游语义导出、tag tree/reading order、artifact/alt text、表格与表单语义、标准验证、辅助技术抽查。
- **候选 / 状态**：pipeline id `accessible-pdf-release-pipeline`（拟登记）；`adobe-acrobat-pro-accessibility` 默认 `human_handoff`，`pac-pdf-accessibility-checker` 与 `verapdf-validator` 在 Registry 核验前分别为 `candidate_unavailable`。这些步骤不是彼此替代。
- **原生模型**：上游作者工具拥有语义母版；Acrobat 处理 PDF tag/reading-order 修复；PAC/veraPDF 产生规则检查报告；人工语义判断与辅助技术任务拥有体验 coverage。
- **权威母版**：优先修上游语义源；若 PDF 后期修复不可避免，修复版 PDF 只拥有该 release 的 PDF 结构 concern，并须记录回流/漂移风险。
- **为何选**：自动规则、PDF 内部结构与真实辅助技术体验是不同能力，必须组成有边界的发布管线。
- **何时不选**：交付未要求 PDF，或把单一“检查通过”误作完整可访问性证明时；不要用后期修复掩盖不可维护的上游源。
- **替代**：能原生生成所需语义的上游工具＋其他经 Registry 核验的标准验证器；不可自动完成时明确 human handoff。
- **真实使用证据**：每一步安装/版本/许可/调用；上游可编辑语义母版；修复操作和 PDF hash；机器报告；人工 tag/reading-order/alt/table 检查；适用辅助技术与任务记录；最终 release hash 与外部 reviewer coverage。

## 工具组合与失败返回

- 一个项目可以有不同消费者母版，但每个 concern 只允许一个 authoritative owner；跨工具同步必须声明派生方向。
- 工具格式互转是独立消费者，不继承原格式的版式、动效、语义或可访问性 claim。
- 候选不可用时，返回替代候选或 `human_handoff`；不要以“环境没装”为理由进入自研。
- capability probe 必须使用该任务最高风险的代表内容。只有能力本身失败，才记录 gap 并允许最小自定义边界。
- 所有候选 ID 在写入共享 Registry 前均为待协调名称；Registry 登记后以其事实、版本和 availability 覆盖本文件中的规划状态。
