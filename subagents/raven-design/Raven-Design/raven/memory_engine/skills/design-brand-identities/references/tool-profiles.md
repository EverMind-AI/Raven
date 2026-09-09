# 品牌识别专业工具能力档案

选择工具的目的，是获得成熟的原生对象、工艺下限和可接管的母版，不是让交付清单显得专业。先沿用质量合格的批准源与团队工具；确需新选时，从当前身份 concern 声明能力，再核验安装、版本、许可、运行环境与下游消费者。同一 concern 只保留一个权威 owner。

每次选定工具都先写一份短“原生工艺合同”：它的原生对象是什么、保护哪些属性、允许怎样扩展、权威母版在哪里、如何重建/导出、谁会接管。只有真实调用过且留下可编辑母版、重开/导出和当前像素或消费证据时，才能写 `used`。商业 GUI/SaaS 没有项目账户、seat 或操作轨迹时只能写 `unavailable` 或 `human_handoff`；提到名字不算使用。

## 快速路由

| 当前 concern | 优先工具族 | 不要用它替代 |
| --- | --- | --- |
| 图形标、有限字标、静态锁版 | Illustrator / Affinity Designer / Inkscape | 完整字体 metrics、动态行为、DAM |
| lettering、多语言字形和 kerning | Glyphs / FontLab / RoboFont-UFO | 单纯排版、商标判断、文化验证 |
| 数字品牌组件、变量与团队消费 | Figma / Penpot / Sketch | 上游核心几何、印前和发布事实 |
| 正式分页指南 | InDesign / Affinity Publisher / Scribus | 无消费者的案例包装、品牌门户 |
| 动态或运行时身份 | After Effects / Rive | 静态核心、装饰性动效 |
| 字体资产与 shaping QA | HarfBuzz + FontTools | 字形设计、许可和母语判断 |
| PDF/X 与印前 | Acrobat Pro / callas pdfToolbox | 平面母版、供应商实物 proof |
| 材料和实体生产 | 供应商原生软件/设备 job | 上游身份母版和概念 mockup |

工具族只缩小候选，不能自动决定具体软件。用现有栈兼容、原生能力、许可、维护者、离线/自动化要求和供应商合同作选择；不能运行的强工具不如当前能被真实使用并接管的合适工具。

## 1. 矢量核心身份母版

- **任务子型**：图形标、有限字标轮廓、锁版、响应式静态版本和供应商通用矢量源。
- **需要能力**：精确 Bézier/复合路径、图层/画板、光学校正、颜色与可控 SVG/PDF 导出。
- **候选工具 / stable id**：Adobe Illustrator `adobe-illustrator-vector`；Affinity Designer `affinity-designer-vector`；Inkscape `inkscape-svg-vector`。
- **原生设计语言/数据模型**：路径、节点、复合形状、外观、图层、画板及格式化导出；选定工具的原生对象必须保留，不能只把它当截图器。
- **权威母版**：所选工具的可编辑原生项目；结构化 SVG 只在明确由 SVG-native 工具拥有并可无损编辑时拥有 geometry concern。
- **为何选**：核心 concern 是轮廓、负形、锁版与跨媒介矢量传播，且团队或供应商能接管该原生模型。
- **何时不选**：需要完整字形指标/kerning、复杂动态行为、DAM 治理或只有位图输出时；不要用它假装覆盖这些能力。
- **替代**：优先沿用现有批准矢量母版；三者按团队、许可、自动化和供应商要求择一，同一 geometry concern 不并设母版。
- **真实使用证据**：原生对象/图层、一次验证副本的节点或画板修改、重开、SVG/PDF 重导出、几何/像素回归、目标尺寸帧和许可记录。

## 2. 字标与 lettering 母版

- **任务子型**：定制字标、多语言锁版、有限字符系统、需精确 spacing/kerning 的响应版本。
- **需要能力**：glyph outlines、sidebearings、kerning、layers/masters、features、语言与导出管理。
- **候选工具 / stable id**：Glyphs `glyphs-lettering`；FontLab `fontlab-lettering`；RoboFont/UFO `robofont-ufo-lettering`。
- **原生设计语言/数据模型**：以 glyph、轮廓、指标、kerning 组、master/layer 和 feature 为基本对象，而非在画板上手移孤立文字。
- **权威母版**：可编辑字体/lettering 项目或 UFO/designspace bundle；批准 SVG/轮廓是 derived export。
- **为何选**：字形关系、语言覆盖、间距与版本需要可审计、可比较和可重建。
- **何时不选**：只是现成字体排版、简单图形标，或任务实际要求完整字体工程并应转交字体领域。
- **替代**：沿用团队已有字形源；无专业席位时 `human_handoff`，不能以转曲或 FontTools 抽轮廓冒充 lettering 母版。
- **真实使用证据**：原生 glyph/metrics/kerning 数据、correction ledger、真实操作轨迹、导出与 shaping 结果、目标语言/尺寸帧、字体许可。

## 3. 协作品牌系统与数字资产库

- **任务子型**：数字 lockup、颜色/字体角色、模板变量、团队复用、设计到实现的品牌资产分发。
- **需要能力**：components、variants、variables/modes、libraries、权限、发布与下游更新。
- **候选工具 / stable id**：Figma `figma-design`；Penpot `penpot-design`；Sketch `sketch-design`。
- **原生设计语言/数据模型**：节点、组件/实例、变量、样式、library publication 与 update graph；这是一种协作模型，不是默认表面风格。
- **权威母版**：经项目声明的设计文件/库拥有 digital-system concern；核心矢量、代码行为和发布状态仍由各自 owner 拥有。
- **为何选**：真实设计团队需要复用、更新和审核数字品牌资产，并已有合法账户与目标文件。
- **何时不选**：仅交单个标志、没有设计文件消费者、要求离线自动重建，或试图把它当万能 Logo/印前/运行时工具。
- **替代**：现有团队库、版本化 token/asset bundle；替代物必须满足真实消费者，不为“看起来专业”新建库。
- **真实使用证据**：账户/seat/许可、精确文件版本、组件/变量 ID、真实编辑与发布、实例消费/更新、授权导出和最终宿主证据。

## 4. 品牌指南出版与交接文档

- **任务子型**：正式品牌指南、分页规范、可印刷或可长期维护的交接文档。
- **需要能力**：页面/母版页、段落与对象样式、链接资产、长文档、package、PDF/X 与 preflight。
- **候选工具 / stable id**：Adobe InDesign `adobe-indesign-guidelines`；Affinity Publisher `affinity-publisher-guidelines`；Scribus `scribus-guidelines`。
- **原生设计语言/数据模型**：分页、master pages、styles、linked assets、preflight 与 package；不是网页卡片或手工绝对定位页面。
- **权威母版**：可编辑出版项目拥有 guideline-publication concern；其中嵌入/链接的标志与 token 仍来自上游 owners。
- **为何选**：消费者需要严肃分页、版本化、打印或供应商交接，而非只在网页浏览。
- **何时不选**：交付只是少量资产与紧凑规则、Web 门户才是真实消费者，或该文档会成为无维护者的案例包装。
- **替代**：团队现有文档系统或真实品牌门户；替代前先确认分页、更新和消费要求。
- **真实使用证据**：原生源、样式/链接结构、真实编辑轨迹、package、字体/图片许可、PDF 导出、preflight 与打印/阅读证据。

## 5. 品牌资产治理与 DAM

- **任务子型**：多团队、多市场、大量 approved exports 的搜索、权限、版本、批准、生命周期和弃用。
- **需要能力**：metadata、collections、versions、approvals、roles、download renditions、audit trail、expiry/deprecation。
- **候选工具 / stable id**：Frontify `frontify-brand-dam`；Bynder `bynder-brand-dam`；Brandfolder `brandfolder-dam`。
- **原生设计语言/数据模型**：资产记录、元数据、版本、批准状态、权限、下载变体和审计事件。
- **权威母版**：DAM 可拥有 release catalog、approval/lifecycle concern；不得拥有上游 geometry、lettering 或 motion source concern。
- **为何选**：真实组织存在分发、权限、失效资产和审计成本，且已有租户、owner 与治理流程。
- **何时不选**：小型一次性交付、无资产管理员、无真实租户或只想用门户外观证明品牌系统成熟。
- **替代**：现有组织 DAM；小范围可用版本化 manifest + 受控存储，但不得宣称等同企业 DAM 能力。
- **真实使用证据**：租户/seat/许可、资产 ID、上传/版本/批准操作、权限测试、下载变体、弃用/回滚和 audit export。

## 6. 动态与响应式身份母版

- **任务子型**：时间线标志、广播/视频身份、交互式或数据驱动身份、受控响应行为。
- **需要能力**：composition/timeline/keyframes 或 artboard/state machine/data binding，以及静态和 reduced-motion 回退。
- **候选工具 / stable id**：After Effects `after-effects-motion-identity`；Rive `rive-motion-identity`。
- **原生设计语言/数据模型**：AE 使用 composition、layers、timeline、properties；Rive 使用 artboard、animation、state machine 与 data model。按消费者选择，不能混称同一能力。
- **权威母版**：`.aep` 类工程或 `.riv` 类运行时工程拥有 motion-behavior concern；静态核心几何仍引用上游身份 owner。
- **为何选**：时间、状态或运行时参数本身承担身份，且存在视频编辑或运行时消费者。
- **何时不选**：静态身份足够、动效只是装饰、目标平台无对应 runtime，或无法提供静态/低动态回退。
- **替代**：团队既有动效/运行时工具；简单确定性过渡可由目标平台原生能力拥有，但必须记录新的 behavior owner。
- **真实使用证据**：工程版本/许可、真实编辑、composition 或 state graph、受控参数、重建/运行时导出、首中末帧、静态/reduced-motion 和目标宿主证据。

## 7. 多语言 shaping 与字体资产 QA

- **任务子型**：品牌系统使用 live type、复杂文字、可变字体、Web 字体或多语言 token。
- **需要能力**：OpenType 表检查、字符覆盖、script/language/direction shaping、subset、版本与回归。
- **候选工具 / stable id**：HarfBuzz + FontTools 管线 `harfbuzz-fonttools-brand-qa`。
- **原生设计语言/数据模型**：Unicode buffer、script/language/direction、positioned glyphs、OpenType tables、glyph sets 和确定性命令。
- **权威母版**：字体二进制/源由字体 owner 拥有；本管线只拥有 QA corpus、配置和 transform concern。
- **为何选**：需要可复验地发现缺字、错误 shaping、特性丢失或子集回归。
- **何时不选**：纯轮廓字标没有 live-type claim，或试图用脚本结果代替字形设计、母语判断和字体许可。
- **替代**：目标平台原生 shaping 引擎与经过固定输入的字体 QA；必须保留脚本、语言和版本。
- **真实使用证据**：锁定依赖/版本/许可、命令与 corpus、输入字体 hash、表/覆盖/shaping 输出、subset 重建、目标引擎像素和差异记录。

## 8. 印前、颜色与 PDF/X 预检

- **任务子型**：印刷指南、包装/标牌输出、专色、ICC/output intent、出血和商业印刷 handoff。
- **需要能力**：PDF/X、对象/字体/图像检查、颜色空间、专色、output intent、preflight profile/report。
- **候选工具 / stable id**：Acrobat Pro `acrobat-pdfx-preflight`；callas pdfToolbox `callas-pdftoolbox-preflight`。
- **原生设计语言/数据模型**：PDF objects、boxes、fonts、inks、ICC/output intent、standards conformance 与 preflight profiles。
- **权威母版**：印刷应用源拥有版式；锁定的 job PDF + profile/report 可拥有特定 production-export concern，不拥有品牌核心。
- **为何选**：合同明确要求可交给印厂的标准化输出和可审计预检。
- **何时不选**：屏幕-only 资产、没有输出条件或供应商要求，或只有普通打印预览却想声称印前通过。
- **替代**：供应商的正式 preflight/RIP handoff；开放工具只有在能力 probe 覆盖同一合同后才能替代。
- **真实使用证据**：产品/席位/许可、真实 profile 调用、PDF hash、字体/专色/出血/output intent 检查、report、供应商 proof 与最终样张。

## 9. 供应商原生生产交接

- **任务子型**：刺绣、丝印、织物、陶瓷、切割、雕刻、标牌、RIP 或其他材料生产。
- **需要能力**：把批准身份转换为工艺原生对象、材料/颜色/尺寸公差、proof、返工与验收。
- **候选工具 / stable id**：供应商与工艺确定后建立/选择 `supplier-native-brand-production`，再在 Registry 中绑定实际软件、设备与 handoff。
- **原生设计语言/数据模型**：工艺决定的针迹、切割路径、分色/油墨、刀路、网点、材料层或设备 job；不能用平面 mockup 代替。
- **权威母版**：上游品牌母版仍拥有 identity concern；供应商原生 job 可拥有限定工艺的 production-adaptation concern。
- **为何选**：claim 涉及真实可生产性、颜色/材料表现或供应商接管。
- **何时不选**：没有已确定工艺/供应商、只需概念示意，或要求本 Skill代替工程、法规和施工责任。
- **替代**：另一家能返回等价原生 job 与 proof 的合格供应商；无供应商时保持 `human_handoff`。
- **真实使用证据**：供应商/设备/软件版本与责任、真实导入和修改、原生 job、工单、材料/色样、尺寸与公差、实物 proof、批准/返工记录。

## 10. 选择记录与自定义边界

每次选择写成：

```text
身份 concern → required capability → 当前可用候选 → 选择理由 →
native model → 受保护属性 → authoritative master → export/consumer → evidence
```

同一 concern 只能选一个 authoritative owner。先对实际安装版本做最小 capability probe：创建或导入一个代表性对象、修改受保护属性、重开并重新导出。`unavailable` 只允许改选工具或 `human_handoff`；只有 probe 在可运行条件下真实失败，才记录 gap 和最小 custom boundary。

自定义边界只实现缺失能力，不重写已经通过 probe 的原生功能。若自定义实现开始拥有大量路径、字形、页面、格式或交互状态，重新检查是不是选错了工具；不得用巨型脚本生成大量资产来回避逐个视觉决定。所有自定义部分仍须提供可编辑源、确定性重建、最终像素和下游接管证据。
