# 字体与字形专业工具能力档案

这些档案用于从任务能力选择候选，不声明当前环境已安装或项目已使用。稳定 candidate id 只有在共享 Tool Registry 登记、availability probe 与 artifact 使用证据齐全后，才能进入 `used`；未登记时保持 `considered / unknown`。

## 1. 商业家族与变量字体工作室

- **任务子型：** `font_family` 的多母版、静态家族、变量轴、复杂组件与 OpenType 生产。
- **所需能力：** 交互轮廓、spacing/kerning、masters/instances、兼容检查、features、命名与原生导出。
- **候选工具 / id：** Glyphs `glyphs-family-editor`；FontLab `fontlab-family-editor`，二选一并单独登记。
- **原生设计语言/模型：** Glyphs 的 glyph/layer/master/component/feature/instance 工程；FontLab 的 glyph/layer/master/axis/instance 与字体信息工程。
- **权威母版：** 选定工具的原生工程（如 `.glyphs/.glyphspackage` 或 FontLab 原生工程）或保留所需语义的 UFO/designspace 可直接为 master；OTF 默认只是输入基线或 derived output。只有显式 authority transfer/fork、同一 concern 的旧 owner 退役、接收方成为可编辑原生工程且重建证明通过后，该接收工程才可接管 concern。
- **为何选：** 任务需要设计师在同一原生模型内持续修字、调间距、管理家族与检查插值。
- **何时不选：** 环境无法运行且无人 handoff；团队要求开放、可 diff 的 UFO 工作流；任务只是固定文字或选型。
- **替代：** `robofont-ufo-editor`、`fontforge-open-font-editor`，或缩窄为人工 handoff。
- **真实使用证据：** Registry 版本/许可、打开与保存原生工程的调用、editable master、masters/features 截图或结构导出、干净导出日志、最终消费者 proof。
- **官方依据：** https://glyphsapp.com/learn/ 与 https://help.fontlab.com/fontlab/8/

## 2. UFO/designspace 可编程字体工作室

- **任务子型：** 开放源家族、跨工具协作、脚本化扩字/修复，以及需要逐 glyph 版本控制的字体。
- **所需能力：** UFO3 编辑、Space Center/spacing、kerning、components/anchors、designspace 与 Python 扩展。
- **候选工具 / id：** RoboFont `robofont-ufo-editor`；如另选浏览器式 UFO 工具必须以独立 id 登记。
- **原生设计语言/模型：** UFO glyph/layer/lib/groups/kerning/features，加 designspace 的 sources/axes/instances/rules。
- **权威母版：** 一组 UFO、designspace、feature 文件和明确的构建配置；编辑器缓存与导出二进制不是 authority。
- **为何选：** 需要开放格式、可逐文件审查、脚本扩展和多工具互操作，同时保留专业轮廓环境。
- **何时不选：** 团队没有 macOS/GUI handoff；现有工程以其他原生格式为唯一真源且转换会丢语义。
- **替代：** `glyphs-family-editor`、`fontlab-family-editor`、`fontforge-open-font-editor`。
- **真实使用证据：** Registry 事实、UFO/designspace 实际打开/修改记录、原生 spacing/outline 证据、source diff、构建命令和当前输出 proof。
- **官方依据：** https://www.robofont.com/documentation/topics/the-ufo-format/ 与 https://unifiedfontobject.org/

## 3. 开源一体式字体编辑器

- **任务子型：** `finite_charset_font`、小型静态字体、既有字体诊断/修复、格式检查与可脚本化导出。
- **所需能力：** 轮廓/metrics/kerning/lookup 编辑、SFD 项目、验证、Python/CLI 与多格式生成。
- **候选工具 / id：** FontForge `fontforge-open-font-editor`。
- **原生设计语言/模型：** SFD 中的 glyph、layers、splines、references、lookups、metrics 与 font metadata。
- **权威母版：** `.sfd` 及同库的 feature/build 配置；从别处导入时先决定是否正式迁移 authority。
- **为何选：** 需要开源、跨平台、GUI 与脚本同路的有限字体编辑/修复流程。
- **何时不选：** 复杂多轴家族所需语义或团队原生流程无法可靠表达；只有无界面环境且没有可复现脚本路径。
- **替代：** `robofont-ufo-editor`、商业家族编辑器、`fontparts-parametric-source-pipeline`。
- **真实使用证据：** 安装/version/license、SFD 的真实编辑或脚本调用、validation 结果、editable master、clean generate、二进制 hash 与消费者 proof。
- **官方依据：** https://fontforge.org/docs/ 与 https://fontforge.org/docs/scripting/python/fontforge.html

## 4. 参数化字形源与逐字 override 管线

- **任务子型：** 几何/模块化命题明确的家族或闭集字体、批量扩展、可复现修复；不能作为“少做逐字设计”的借口。
- **所需能力：** glyph recipe、共享参数、逐字/逐 master override、可审阅轮廓快照、确定性构建与差异检测。
- **候选工具 / id：** FontParts/defcon/ufoLib2/fontTools 组合 `fontparts-parametric-source-pipeline`。
- **原生设计语言/模型：** 版本化代码、数据化 recipes/overrides、测试夹具，生成 UFO 或等价轮廓模型供专业检查。
- **权威母版：** 代码 + recipes + overrides + 锁定环境；生成 UFO 若允许手改，必须正式切换为新的唯一 authority，禁止双真源。
- **为何选：** 参数关系本身是设计命题，且自动化能保持大范围一致而不阻碍逐字修正。
- **何时不选：** 只是用少数几何原语拼满字符；无法表达光学校正；人工修改不能回写；成熟编辑器已满足需求。
- **替代：** `robofont-ufo-editor`、`glyphs-family-editor`、`fontforge-open-font-editor`。
- **真实使用证据：** 依赖/许可、命令、确定性 hash、生成中间源、逐字 override 实例、人工检查记录、无下游暗改证明和最终消费者 proof。
- **官方依据：** https://fontparts.readthedocs.io/ 与 https://github.com/fonttools/fonttools

## 5. 固定字标与 lettering 矢量工作室

- **任务子型：** `wordmark_lettering`、包装固定短语、一次性标题字、切割/刺绣/压印用完整词形。
- **所需能力：** Bézier/节点、布尔、对齐与 guides、完整词形编辑、颜色/反白版本和生产导出。
- **候选工具 / id：** Inkscape `inkscape-vector-lettering`；Illustrator/Affinity 必须分别登记为商业 GUI candidate。
- **原生设计语言/模型：** 原生路径、groups/layers、artboards/pages、guides、fills/strokes 与固定 composition。
- **权威母版：** 选定工具的原生可编辑文档；纯导出 SVG/PDF/EPS 默认是 derived output。只有显式 authority transfer/fork、同一 concern 的旧 owner 退役、接收方成为可编辑原生工程且重建证明通过后，该接收工程或有界 fork 才可接管 concern；导出文件可打开或合同点名本身都不足以升格。
- **为何选：** 交付对象是不可拆散的固定词形和生产轮廓，而非键盘输入系统。
- **何时不选：** 需要 cmap、OpenType、额外字符、上下文输入或家族关系；这时改走字体路由。
- **替代：** 合适的原生矢量 GUI human handoff；若需字体则选专业字体编辑器。
- **真实使用证据：** 工具版本/许可、原生文档、实际编辑调用、完整词形节点/负形证据、从 master 重导、生产软件导入与目标尺寸 proof。
- **官方依据：** https://inkscape.org/learn/

## 6. 可重复字体构建与表处理管线

- **任务子型：** `font_family` 与 `finite_charset_font` 的 UFO/designspace/features 到 OTF/TTF/variable/WOFF2，或受控表修复。
- **所需能力：** 可锁版本 CLI、编译 features、实例化/变量构建、subset、表解析/修改、可重复输出。
- **候选工具 / id：** fontmake + fontTools，必要时 AFDKO，组合 id `fontmake-fonttools-build-pipeline`。
- **原生设计语言/模型：** 源字体 + designspace/feature/config + 明确命令构成有向构建图；TTX 只承接声明的表级 concern。
- **权威母版：** glyph geometry 仍属于上游字体源；构建配置只拥有 `build_config`，不能反向成为视觉母版。
- **为何选：** 需要从同一源重建多输出、检查表和保留可审计命令。
- **何时不选：** 用来替代轮廓、spacing 或语言判断；输入源无 authority；任务只是固定 vector lettering。
- **替代：** 编辑器原生导出（仍须记录调用）或项目已有且 Registry 已验证的构建管线。
- **真实使用证据：** package lock、版本/许可、完整命令与日志、干净环境 rebuild、表/实例结果、输出 hash、安装/加载 proof。
- **官方依据：** https://github.com/googlefonts/fontmake 与 https://fonttools.readthedocs.io/

## 7. Shaping 与可视 proof 管线

- **任务子型：** 字体创建/修复、复杂文字、多语种、feature、变量轴和选型的输入行为验证。
- **所需能力：** 指定 text/direction/script/language/features/variations，输出 glyph 序列、clusters、positions 与可视渲染。
- **候选工具 / id：** HarfBuzz CLI + 可选 FontGoggles，组合 id `harfbuzz-shaping-proof-pipeline`。
- **原生设计语言/模型：** Unicode buffer 属性 + font + feature/axis 参数 → positioned glyph stream；proof corpus 与期望结果是测试模型。
- **权威母版：** 测试语料/参数/期望值拥有 shaping-proof concern；字体轮廓仍由上游 master 拥有。
- **为何选：** 可把“表存在”变成可复验的实际替换和定位结果，并生成统一 proof。
- **何时不选：** 单独证明目标专有应用完全一致、阅读质量或现场可读性；仍需真实消费者 gate。
- **替代：** 目标应用的调试/渲染接口，或经 Registry 验证的同类 shaper。
- **真实使用证据：** 安装/version/license、完整 hb-shape/hb-view 调用、输入与 JSON/图像输出、期望 diff、目标消费者对照和当前 build hash。
- **官方依据：** https://harfbuzz.github.io/utilities.html 与 https://github.com/justvanrossum/fontgoggles

## 8. 字体结构 QA 与版本回归管线

- **任务子型：** 发布前结构检查、既有字体修复、家族一致性、前后版本差异和回归定位。
- **所需能力：** OpenType sanitize、适用 profile 检查、表/轮廓/度量/渲染差异、可解释豁免。
- **候选工具 / id：** Fontspector、OpenType Sanitizer、Diffenator 等拆分登记，组合 id `font-qa-regression-pipeline`。
- **原生设计语言/模型：** 版本化 QA profile、baseline font set、machine-readable findings、exceptions 与 diff report。
- **权威母版：** QA 配置和已审签豁免拥有检查 concern；被测字体与其设计 authority 不转移给报告。
- **为何选：** 自动发现结构、兼容和回归风险，并把变更范围交给人工复核。
- **何时不选：** 把全绿报告当审美、语言、可读性或发布充分条件；profile 与目标发行生态不匹配。
- **替代：** 项目/发行方官方 QA、编辑器检查与针对性 fontTools 脚本，但同样需 Registry 和使用证据。
- **真实使用证据：** 版本/许可、实际命令、所用 profile、原始 findings、失败修复后 rerun、豁免 authority、baseline/current hash 与人工差异复核。
- **官方依据：** https://github.com/fonttools/fontspector 与 https://github.com/googlefonts/fontdiffenator

## 9. 授权字体选型与部署管线

- **任务子型：** `font_selection` 的品牌、编辑、UI、数据、多语言替换、采购和迁移。
- **所需能力：** 合法候选获取、版本/hash、许可权限、覆盖/shaping、受控 specimen、目标应用与 fallback 部署。
- **候选工具 / id：** 不绑定字体商店品牌；把“本地许可资产 + 受控比较器 + 目标消费者”登记为 `licensed-font-selection-pipeline`。
- **原生设计语言/模型：** 不可变候选包、license manifest、需求/风险矩阵、同条件 proof、deployment mapping 与决策记录。
- **权威母版：** 比较矩阵拥有 `selection_decision`；字体文件及轮廓仍属于其合法上游，部署配置拥有消费映射。
- **为何选：** 用户需要从成熟字体中做可复核采用决定，而不是制造新轮廓。
- **何时不选：** 需求本质是定制字形、已有候选均不满足、修改权不清或需要创造新的书写系统。
- **替代：** 缩窄需求后重选；转 `font_family` / `finite_charset_font`；商业采购由 human handoff 完成。
- **真实使用证据：** 候选来源/hash/version、许可原文或凭据、相同内容/参数 comparison、覆盖/shaping 结果、目标应用 captures、fallback 与最终部署记录。

## 10. 真实消费者与实体现场验证管线

- **任务子型：** 任何涉及实际 rasterizer、打印/加工、距离/光照/材料、长期阅读、观察者辨识或安全后果的 claim。
- **所需能力：** 目标应用/设备、真实介质与测量、未缩放采样、受控任务、原始观察、构建与条件追溯。
- **候选工具 / id：** 任务专属 consumer + hardware + protocol pipeline，稳定 id `target-consumer-field-proof-pipeline`；常需 `human_handoff`。
- **原生设计语言/模型：** 风险驱动测试矩阵、设备/材料/环境参数、consumer capture、实物样本、原始观察与分析记录。
- **权威母版：** test plan 与 raw evidence 只拥有 field-proof concern；字形、构建和部署仍由各自上游 authority 拥有。
- **为何选：** 模拟和浏览器截图无法支持真实实体、观察者或现场 claim。
- **何时不选：** 合同只要求概念探索或屏幕 preflight；此时明确降级 claim，而不是伪造现场测试。
- **替代：** `harfbuzz-shaping-proof-pipeline` 与像素/工艺模拟只能作 preflight；缺设备时交由实验室、工艺方或用户研究团队。
- **真实使用证据：** 设备/软件版本、材料批次、尺寸/距离/光照等条件、当前 build 的未缩放输出、匿名原始响应/测量、审阅者身份、失败返回与限制。

## Registry 协调清单

上述 candidate id 当前只是领域稳定键。共享 Registry 应按 atomic tool 分别登记环境事实，并把 4、6、7、8、9、10 登记为引用这些 atomic entries 的 pipeline；在登记完成前，本 Skill 只能选择与计划，不能产生 `used` 事实。
