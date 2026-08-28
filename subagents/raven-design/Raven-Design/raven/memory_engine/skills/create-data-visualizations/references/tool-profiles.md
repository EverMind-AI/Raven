# 数据可视化专业工具能力档案

本文件提供领域选择候选，不是环境事实或使用记录。以下稳定 candidate id 需要与共享 Tool Registry
对齐；Registry 没有 `available` 证据时，项目记录为 `candidate_unavailable`，需要账户、GUI、商业席位
或人工操作时记录 `human_handoff`。任何候选只有在具体 artifact 同时证明 dependency、resolved version、
license、真实 invocation、editable master、rebuild/export 与最终 pixels/consumer 后才能记为 `used`。

先定义任务所需的编码、数据状态、交互、导出、可访问性和 authority concern，再选择工具。选定后应
使用其原生模型和母版完成对应 concern；不能只借一个控件、默认主题或 renderer，其余继续任意手写。

## 组合规则：视觉系统与数据工具分开选择

- 解释报告和多页 data app 可让 Observable Framework 承担页面环境，但仍需明确图形引擎和主题；
- 持续操作型产品先选择一套成熟产品设计系统，再让 Plot、G2、ECharts 或 Vega-Lite 拥有图形；
- 精确表格、筛选和虚拟列表优先考察 TanStack Table/Virtual 或成熟 Data Grid，不从 div 表格重做；
- 设计系统负责控件、字体、表面和反馈，数据工具负责 mark、scale、legend、interaction 与 renderer；
- 两者通过共享 token 和 view state 衔接。默认主题只是起点，必须用代表视图证明最终融合；
- 工具比较、版本、schema、hash 和计算 proof 留在 evidence，不排成用户可见的状态条或卡片墙。

## 1. `observable-plot`

- **任务子型：** Web 解释图、探索性小倍图、常见统计关系与编辑式注释。
- **需要能力：** marks/transforms/scales、分面、直接标签、SVG、浏览器内组合和基础 ARIA。
- **候选工具 / 稳定 candidate id：** Observable Plot / `observable-plot`。
- **原生设计语言或数据模型：** JavaScript mark grammar；数据列经 transform 映射到位置、颜色、长度和符号。
- **权威母版：** 版本化 Plot JS/TS specification 拥有图形编码 concern；view data/manifest 分别拥有数据投影 concern。
- **为何选：** 普通统计图能以高层语法保留尺度、缺口、分面和 SVG 结构，减少手写轴与坐标错误面。
- **何时不选：** 需要复杂持久交互、专属几何、超出 mark grammar 的布局或产品级状态编排时。
- **替代：** `vega-lite`、`antv-g2`；只有能力探针失败后才考虑 `d3`。
- **真实使用证据：** 包与许可、真实 import/`Plot.plot` 调用、spec/master、离线 rebuild、SVG/HTML 输出 hash、
  ARIA/精确表检查和目标上下文最终像素。

## 2. `vega-lite`

- **任务子型：** 可移植声明式统计图、跨语言分析、组合视图和参数化交互。
- **需要能力：** JSON schema、transform、mark/encoding、layer/facet/concat、parameter/selection 与 SVG/Canvas 导出。
- **候选工具 / 稳定 candidate id：** Vega-Lite（必要时下沉 Vega）/ `vega-lite`。
- **原生设计语言或数据模型：** 版本化 JSON grammar；Vega-Lite spec 编译为 Vega dataflow 和 scenegraph。
- **权威母版：** 带 schema/version 的 `.vl.json` 与引用的数据/transform manifest；编译后 Vega 和渲染为派生物。
- **为何选：** spec 可校验、可重放、可跨 JavaScript/Python/R 生成，适合审计编码与当前参数状态。
- **何时不选：** 专属几何、复杂标签求解、细粒度直接操作或性能需求超出 grammar 时；不要为声明式而扭曲问题。
- **替代：** `observable-plot`；Python 可用 Altair 生成同一母版；复杂交互评估 `antv-g2` 或低层 Vega。
- **真实使用证据：** compiler/runtime 版本与许可、schema validation、真实 compile/render、spec 与 view state、
  数据导出、SVG/Canvas consumer 证据、干净环境重建和数值/像素对账。

## 3. `antv-g2`

- **任务子型：** Web 产品中的组合图、联动探索、中文生态分析界面和多 renderer 图形。
- **需要能力：** marks、encode、scale、transform、composition、interaction、theme、Canvas/SVG/WebGL renderer。
- **候选工具 / 稳定 candidate id：** AntV G2 / `antv-g2`。
- **原生设计语言或数据模型：** G2 chart/view/mark grammar 与 interaction state；图形从 options 或链式 API 生成。
- **权威母版：** 版本化 G2 options/TS 模块拥有图形与交互 concern；共享 viewModel 拥有显示数据和筛选状态。
- **为何选：** 需要多视图、产品状态和交互语法共同工作，且 renderer/组合能力能覆盖合同。
- **何时不选：** 单张静态普通图、高层声明式 spec 更重要，或 Canvas 语义替代和导出无法满足消费者时。
- **替代：** `observable-plot`、`vega-lite`；高密常规业务图比较 `apache-echarts`。
- **真实使用证据：** 锁定包/许可、真实 Chart invocation、options/native master、renderer 决策、build/export、
  当前 view state 数据对账、Canvas 的同上下文语义表面、键盘与最终像素。

## 4. `apache-echarts`

- **任务子型：** 高密业务图、常见监测图、较大数据量 Canvas 交互和成熟 dashboard 图型。
- **需要能力：** dataset/encode、axis/series、dataZoom、brush、tooltip、事件、Canvas/SVG renderer 和图片导出。
- **候选工具 / 稳定 candidate id：** Apache ECharts / `apache-echarts`。
- **原生设计语言或数据模型：** declarative option tree、dataset/series/component state 与事件模型。
- **权威母版：** 版本化 ECharts option/TS 模块与 viewModel；导出的 Canvas、SVG 或图片均为派生物。
- **为何选：** 合同需要成熟的缩放、密集序列、常见业务交互或大数据 renderer，而非专属视觉几何。
- **何时不选：** 简单解释图、可审计跨语言 spec 优先，或默认交互/Canvas 语义层不能满足任务时。
- **替代：** `antv-g2`、`vega-lite`、`observable-plot`；自定义 Canvas 不能因性能猜测直接获准。
- **真实使用证据：** 包/版本/Apache 许可、真实 init/setOption/event、option master、renderer 与性能探针、
  当前筛选导出、DOM/表格替代、重建 hash、压力数据和目标设备像素。

## 5. `ggplot2-quarto`

- **任务子型：** 统计分析、研究简报、批量图、可打印出版和 HTML/PDF 多格式报告。
- **需要能力：** grammar of graphics、统计变换、facet、主题、矢量/栅格导出、正文/表格/图交叉引用和可复现执行。
- **候选工具 / 稳定 candidate id：** ggplot2 + Quarto pipeline / `ggplot2-quarto`。
- **原生设计语言或数据模型：** R data/aesthetic/layer/stat/scale/facet 与 Quarto executable document。
- **权威母版：** `.R`/`.qmd`、数据 manifest、环境锁和出版配置组成版本化 pipeline bundle；PDF/SVG/HTML 为派生物。
- **为何选：** 统计变换、批量一致性、审阅文本与固定出版格式是核心，交互不是主价值。
- **何时不选：** 持续浏览器交互、低延迟筛选、运营权限或复杂直接操作是主要任务时。
- **替代：** `vega-lite`、`observable-framework`；既有 Python 分析环境可用 Altair/Matplotlib pipeline 候选。
- **真实使用证据：** R/Quarto/包版本与许可、环境锁、真实 render、母版 bundle、执行日志、SVG/PDF/HTML
  输出 hash、字体/单位/图表重开、独立复算和最终出版像素。

## 6. `observable-framework`

- **任务子型：** 可部署的数据探索器、交互报告、数据应用和以静态快照交付的 dashboard。
- **需要能力：** Markdown/JS 页面、响应式 state、build-time data loaders、静态部署、模块化图表和本地数据附件。
- **候选工具 / 稳定 candidate id：** Observable Framework / `observable-framework`。
- **原生设计语言或数据模型：** Framework project、reactive JavaScript、page modules 与 loader-generated snapshots。
- **权威母版：** 项目源码、loader、配置、锁文件和输入 manifest；构建目录、缓存和部署站点均为派生物。
- **为何选：** 探索/解释需要多页或交互状态，又希望数据在构建期固化并可静态部署。
- **何时不选：** 单张图、严格印刷母版、实时权限治理或持久处置工作流是主要合同。
- **替代：** `ggplot2-quarto`、`apache-superset`；单图使用 `observable-plot` 或 `vega-lite`。
- **真实使用证据：** CLI/package/version/license、真实 loader 与 build、项目母版、snapshot/hash、离线部署、
  交互/view state、错误/过期状态、导出重开和真实消费者像素。

## 7. `apache-superset`

- **任务子型：** 受治理的数据集、指标、查询、权限、过滤和持续 BI dashboard。
- **需要能力：** database/dataset/metric、query context、chart/dashboard、native filter、role/access、刷新和数据导出。
- **候选工具 / 稳定 candidate id：** Apache Superset / `apache-superset`。
- **原生设计语言或数据模型：** Superset metadata、dataset/metric definitions、chart config、dashboard/filter state 与权限模型。
- **权威母版：** 可导入的 YAML/ZIP bundle、数据库/指标定义和版本化部署配置；截图与导出的工作簿不是上游母版。
- **为何选：** 任务是多人反复查询受治理指标，过滤、权限、新鲜度和部署运维属于真实合同。
- **何时不选：** 一次性解释、新闻叙事、离线单文件、专属视觉或业务处置命令是主要任务时。
- **替代：** `observable-framework`；运维时序可登记 Grafana 候选；轻量 BI 可由 Registry 提供其他档案。
- **真实使用证据：** 真实实例/version/license、数据库与 dataset 配置、metric/query、dashboard bundle 导出再导入、
  权限/过滤/过期/失败轨迹、当前状态数据导出和目标消费者证据。无实例时是 `candidate_unavailable`。

## 8. `datawrapper`

- **任务子型：** 新闻编辑部和传播团队的标准图、表、简单地图与多渠道出版。
- **需要能力：** 数据导入、成熟 chart type、注释/来源/alt text、组织主题、嵌入与 PNG/PDF 等出版导出。
- **候选工具 / 稳定 candidate id：** Datawrapper / `datawrapper`。
- **原生设计语言或数据模型：** 托管 chart project 的数据、visualization type、annotate/layout/theme 与 publish state。
- **权威母版：** 能被版本化或重建的项目配置/导出、输入数据和组织主题；若配置不可取得，不能担任权威母版。
- **为何选：** 组织已有编辑生产流程，需要一致的标准图型、来源/替代文本和渠道化导出。
- **何时不选：** 机密离线数据、复杂变换、专属交互、高定制几何或代码级可复现是硬要求时。
- **替代：** `observable-plot`、`vega-lite`、`ggplot2-quarto`；动画模板可由 Registry 另登记候选。
- **真实使用证据：** 账户/席位与许可条款、真实 GUI/API 操作、可编辑项目、输入与设置版本、publish/export、
  从记录重建、alt/data-download 检查和最终渠道像素。无可自动化账户时必须 `human_handoff`。

## 9. `d3`

- **任务子型：** 高层工具无法表达的专属几何、布局、直接操作、标签求解或动态性能边界。
- **需要能力：** scale/axis/shape/layout/data join、SVG/Canvas/WebGL 组合、zoom/brush/drag 和精确生命周期控制。
- **候选工具 / 稳定 candidate id：** D3 modules / `d3`。
- **原生设计语言或数据模型：** 低层 JavaScript modules 与 data join；没有替作者决定 chart/scale/annotation 的高层 grammar。
- **权威母版：** 最小 D3 TS/JS 模块、viewModel、几何参数和坐标/交互测试；自定义部分只拥有获准 concern。
- **为何选：** 可运行的高层候选探针明确 `fail`，且失败能力直接阻断合同。
- **何时不选：** 普通条/线/点/分面、一次性分析、只因“更自由”或当前环境没装高层工具时。
- **替代：** `observable-plot`、`vega-lite`、`antv-g2`、`apache-echarts`；availability gap 返回替代或 handoff。
- **真实使用证据：** 高层候选 capability probe、observed gap、minimal custom boundary、D3 dependency/invocation、
  editable master、坐标/状态/性能等价测试、语义替代、重建和 coherence pixels。

## 10. `playwright-chromium`

- **任务子型：** 浏览器数据产物的行为、状态、投影一致性、导出和消费者证据验证；它不是作图工具。
- **需要能力：** 可见入口操作、viewport/state 覆盖、DOM/SVG/Canvas 观察、下载、网络/console/page error 和截图。
- **候选工具 / 稳定 candidate id：** Playwright with pinned Chromium / `playwright-chromium`。
- **原生设计语言或数据模型：** browser automation script、locator/action/assertion、trace、download 与 screenshot artifacts。
- **权威母版：** 测试脚本和 claim-derived fixture 拥有验证协议 concern；trace、截图和报告是派生证据。
- **为何选：** Web 消费合同需要可重复地证明状态、键盘、筛选、当前下载和最终渲染，而非作者口头检查。
- **何时不选：** 非浏览器原生媒介的核心验证；也不能用像素/DOM 断言替代统计、语义或领域专家判断。
- **替代：** 目标工具的原生测试/导出检查、PDF/图像解析器、人工辅助技术任务；必要时组成验证 pipeline。
- **真实使用证据：** package/browser/version/license、真实 test invocation、测试母版、trace/report、下载重开、
  当前 build hash 绑定、目标 origin/container、最终截图和零绕过可见入口的任务轨迹。

## Registry 协调清单

上述 candidate id 只有在共享 Registry 存在事实档案后才能解析 availability、version、license、probe 和
handoff。领域 Skill 仍拥有项目选择；Registry 条目不能证明某个 artifact 已使用工具。若稳定 id 发生冲突，
优先迁移本文件引用并保留旧 id 的治理映射，不在具体 artifact 中私造同义 id。
