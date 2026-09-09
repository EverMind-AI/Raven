# 技术栈路由

当任务需要选择、增加或替换框架、组件系统、图形引擎或构建方式时读取本文件。目标是选择满足任务的最小成熟组合，而不是展示技术名词。下文库名都是能力候选；组件体系按[设计系统路由](design-system-routing.md)决策，实际版本和 API 必须从现有 lockfile、目标平台与当前官方资料发现，不能凭清单猜测。

## 0. 先过媒介门

先从主要领域 Skill 取得自然介质、专业工作流和权威源，再判断 Web 是否真是用户实际消费、操作或合同要求的用户侧交付表面。只有答案为“是”时，本参考才负责 Web 技术组合。

- 字体文件、图标资产、品牌源文件、印刷文档、演示稿、专业工程图或其他非 Web 文件是主交付时，保留其领域原生格式与专业工具为权威源；
- 浏览器样张、规范页、预览器或检查器若是用户直接使用的合同交付，可以选择支撑它的最小 Web 能力，但不以 Web 导出覆盖领域权威源；若只供内部验证，则停止本路由并使用领域工作流规定的渲染器；
- 用户不消费 Web、交付合同也不要求 Web 时，停止技术栈路由，按领域 Skill 完成源文件与对应验证。

文件扩展名、浏览器可预览或实现者熟悉 React，都不能单独证明 Web 是自然介质。

## 1. 先审计环境

编辑现有项目时先检查：

- `package.json`、锁文件、构建脚本、TypeScript 和 CSS 方案；
- 已有设计系统、组件、token、图标、图表和状态管理；
- 路由、服务端渲染、静态导出、部署和浏览器目标；
- 无障碍约束、内容安全策略、离线和单文件交付；
- 依赖许可、维护状态、包体积和当前项目版本兼容性。

若运行环境提供预装或离线前端工具链，先读取其 manifest、profile、design-system registry、lockfile 或 discovery 结果，不要凭印象判断“没有库”。预装只证明可用，不构成选择理由；具体版本、许可与能力以当次环境证据为准，不把主机路径或某次实验配置硬编码进产物。

优先沿用质量合格的现有栈。不要为了使用新工具重写稳定基础，也不要因为已有依赖就继续使用明显不适合任务的组件。需要替换时说明收益、迁移范围和回退路径。

新建项目时优先查阅官方文档，选择当前稳定版本和官方脚手架，提交锁文件。处于离线、冻结或预装工具环境时，以已锁定的包清单、许可、包元数据、类型声明、本地文档和可执行 smoke 为证据，不为联网而绕过环境约束，也不凭本参考猜版本或 API。

## 2. 技术组合的四层

每个项目最多为四层各选一套主要能力，简单项目可省略任意层：

1. **运行与内容层**：原生 HTML/CSS/JS、Vite、Astro、Next.js、Nuxt、SvelteKit 等；
2. **交互与组件层**：现有组件库、平台官方系统、成熟成品组件或无障碍 headless primitive；
3. **领域表达层**：图表、地图、节点图、排版、Canvas、3D 或游戏引擎；
4. **动效与输出层**：Motion、GSAP、Rive、Lottie、Slidev、Vivliostyle 等。

只有任务需要时才增加一层。不要同时引入多个同职能库，也不要混合两套相互竞争的主组件语言。

## 2.5 模板优先(有网环境)

环境可出网、任务是内容型或营销型站点时,先查 [模板池](template-pool/POOL.md),
**且模板池优先于预装骨架与本文件其余章节的自选**:真实设计师的成熟模板是比手写先验
更可靠的美学与结构基线。选型规则、获取命令与定制纪律以 POOL.md 为准;池内无合格形态
时才回落预装骨架或自选,并在 DESIGN-BRIEF 逐候选写明不适配理由。无网环境跳过本节。

## 3. 页面与应用框架

| 条件 | 优先方案 | 判断依据 |
| --- | --- | --- |
| 单页静态 SVG、海报、图解或小型演示 | 语义 HTML + CSS + 少量 JavaScript；需要构建时用 Vite | 依赖少、可离线、容易输出单文件 |
| 内容为主、页面多、静态发布 | Astro + 内容集合或 MDX | 默认静态、内容边界清楚、按需交互 |
| React 生态的产品工具或复杂多状态应用 | React + TypeScript + Vite；有真实服务端/路由需求时再评估 Next.js | 组件、状态、专业引擎和构建需求 |
| 已有 Vue 或 Svelte 工程 | Vue/Nuxt 或 Svelte/SvelteKit | 避免无收益迁移，使用生态内成熟能力 |
| 长文、数据故事、滚动叙事 | Astro/React/Svelte + 专用图形层 | 内容流与交互岛优先，不做全页工作台 |
| 单文件离线交付 | Vite/定制 bundler 后内联必要资源，或直接原生实现 | 先验证资源、worker、字体和动态 import 是否可内联 |

复杂交互默认使用 TypeScript。一次性、范围很小且结构清晰的静态产物可以使用原生 JavaScript；不要让类型工程超过产物本身。

样式选择服从现有项目。CSS Modules、原生 CSS、Tailwind 或 CSS-in-JS 都可成立，但必须：

- 由统一 token 控制颜色、字体、间距、圆角和动效；
- 避免工具类、组件 demo 布局或样例皮肤未经判断成为最终审美；成熟基础组件的默认视觉可以保留；
- 保持覆盖关系可预测，不用层层补丁抵消样式；
- 让目标环境适配规则与组件或版面责任相邻。

## 4. UI 与产品能力

### 候选边界

1. **编辑项目先审计既有系统**：质量合格的现有设计系统，以及用户生态明确要求的官方系统，通常迁移成本最低；不合格时记录具体缺口，不因“已有”或“官方”自动采用；
2. **新建 React 产品 UI 先路由主系统**：按产品气质、密度、平台传统、品牌可塑性、组件覆盖和交付约束，从环境锁定的成熟系统中保留至多三个候选；
3. **用证据选一套而非混用**：从用户指定的官方系统、成熟成品组件、开放源码体系和无障碍 headless primitive 中选择一套主语言；预装 Appica、MUI 或其他包都不自动获得优先权；
4. **按专业任务建立能力候选**：TanStack Table/Query/Virtual、XState、Tiptap、Lexical、CodeMirror 等只在对应复杂度存在时引入；
5. **最后确定专属定制**：只自制项目专属组合与领域交互；现成能力不能满足的基础原语必须写明缺口与验证方案。

对进入决选的候选逐项记录 `满足 / 有缺口 / 阻塞` 及证据，不用模糊总分掩盖硬性失败：

| 决策面 | 必查证据 |
| --- | --- |
| 行为与状态 | 所需组件、键盘模型、焦点、错误/加载/禁用状态是否真实覆盖 |
| 现有栈兼容 | 框架、版本、SSR/hydration、样式方案、类型和既有 token 是否兼容 |
| 许可与维护 | 目标用途许可、发布版本、维护状态和可替换路径 |
| 交付约束 | 离线、CSP、包体、worker/wasm/字体和目标浏览器能否部署 |
| 视觉一致性 | 能否保留成熟基础视觉并用少量 token 对齐，是否会引入第二套组件语言 |

任何许可、关键行为或交付约束为“阻塞”的候选不得仅凭外观胜出。其余候选选择总代价最低且证据最完整者，而不是名字最流行者。

### 成品组件系统与原语的正确角色

成品组件系统提供经过打磨的行为和基础视觉下限。选中后应保留基础控件的 anatomy、内部尺寸、字体角色、状态、焦点与基础材质；项目负责宏观构图、内容层级、领域工作面和少量全局 token。可以保留成熟的中性默认组件，不复制示例页面、样例内容或演示皮肤，也不为“项目个性”逐组件重画。不要混用多套主组件语言；只有明确能力缺口且组合或全局 token 无法解决时才定制受保护属性，并验证行为、兼容、许可和离线交付没有回归。

### 常见专用能力

| 需求 | 优先考察 |
| --- | --- |
| 表格、筛选、虚拟长列表 | TanStack Table、TanStack Virtual；复杂企业需求可评估成熟 data grid |
| 异步数据、缓存、重试 | TanStack Query 或框架原生数据层 |
| 明确状态机、并行状态、恢复 | XState 或显式 reducer/状态图 |
| 富文本编辑 | Tiptap/ProseMirror、Lexical |
| 代码或结构化文本编辑 | CodeMirror、Monaco（仅在确有 IDE 级需求时） |
| 组件开发与状态样张 | Storybook |

不要手写对话框焦点圈、combobox 键盘模型、拖拽可访问性、虚拟滚动或复杂文本编辑器，再用少量测试假设其可靠。

## 5. 领域图形与媒介

### 数据可视化

| 需求 | 工具倾向 |
| --- | --- |
| 快速、清晰的统计图与编辑注释 | Observable Plot |
| 组合视图、复杂交互语法、中文生态 | AntV G2 |
| 大量常见图型、业务图表与 Canvas 性能 | ECharts |
| 声明式规范、可移植图表 | Vega-Lite |
| 多页数据报告、构建期快照和静态部署 | Observable Framework，再明确选择图形引擎与主题 |
| 精确表格、筛选和虚拟长列表 | TanStack Table / Virtual 或成熟 Data Grid |
| 高度定制的编码、布局或几何 | D3；只用需要的模块，不重造通用图型 |

组件库只负责控件、外壳和产品视觉语言；图形工具负责比例尺、编码、注释和交互。Observable
Framework 适合报告和 data app，不自动适合持续操作型产品；后者通常由成熟设计系统承担外壳，再
组合 Plot、G2、ECharts、Vega-Lite 和表格能力。所有图形必须映射主系统的字体、token、焦点与状态。

### 图解与节点系统

- React Flow：可选择、连接、编辑或交互的节点工作区；
- ELK、Dagre：自动拓扑布局；
- Mermaid：内容稳定、语法可表达且定制要求较低的技术文档；
- SVG + D3：需要专用几何、标注和合同内目标环境适配的静态或轻交互图解。

不要用 Mermaid 代替需要精确视觉层级的成品，也不要逐点硬编码本可由布局引擎处理的复杂拓扑。

### 地图与空间

- MapLibre GL：矢量瓦片、缩放、交互图层和地图产品；
- 经许可的 MapTiler/CARTO/组织 Style：成熟制图视觉起点；离线时必须本地固定全部资源；
- deck.gl：大量点、弧线、热区和 GPU 空间叠加，仅在数据量与图层需求成立时采用；
- PMTiles：单文件或对象存储友好的离线/静态瓦片分发；
- OpenLayers：多源 GIS、投影和传统地理能力；
- D3 Geo：专题地图、投影与高度定制的静态/叙事地理图形；
- 纯 SVG：范围有限、几何固定、无需连续缩放的楼层图、路线图和场地示意。

真实位置是核心语义时必须说明数据源、坐标、投影、比例尺和简化方式。地图引擎不自动提供审美，
成熟 Style 也不替代任务特异的层级和标签判断。不要用生成图、无地理依据的形状或手写 SVG 折线
冒充地图、路网或服务区；固定印刷地图优先交给领域 Skill 选择 QGIS/ArcGIS 等专业制图母版。

### 编辑、演示与印刷

- Slidev：开发者演示、代码、主题和可导出演讲；
- Reveal.js：轻量网页演示和自定义舞台；
- Vivliostyle：HTML/CSS 同源的分页出版和 web-to-print；Paged.js 作为能力匹配时的替代；
- Typst：纯 PDF、技术文档和可复现结构化排版；
- Astro/MDX：滚动专题、长文和章节内容；
- Observable Plot/G2：嵌入真实数据证据。

不要把长报告机械改成幻灯片，也不要把一种媒介的版式等比缩放成另一种媒介。

### 2D、3D、动效与游戏

- SVG：可访问、可缩放、结构化的图标、图解和中等复杂图形；
- Canvas/PixiJS：大量对象、高频绘制、粒子或 2D 场景；
- Konva：可选择、拖拽、变换的 2D 编辑画布；
- Three.js：定制 WebGL 场景；React 项目可用 React Three Fiber 与 Drei；
- Phaser：规则、场景、输入、物理和资源循环完整的 2D 游戏；
- Motion：React 组件状态和布局过渡；
- GSAP：复杂时间线、滚动叙事和精确编排；
- Rive/Lottie：已有设计资产的状态动画或矢量动效交付。

普通 UI 反馈不要引入游戏或 3D 引擎。使用 Canvas/WebGL 时提供尺寸适配、性能预算、reduced-motion 和必要的静态/无 WebGL 回退。

### 图标与字体

- 优先沿用现有图标家族；新项目可从 Phosphor、Lucide、Iconify 或领域专用图标集中选择一套；
- 缺失的领域语义图标可以定制，但必须纳入同一网格、笔画和尺寸系统；
- 不混用 emoji、多个图标家族和随意手绘路径；
- 字体必须确认语言覆盖和许可，离线项目自托管或使用系统字体回退。

## 6. 十四领域快速路由

下表只回答“已经确认需要用户侧 Web 交付后，可以考察什么”。自然介质、权威源和主交付仍由领域 Skill 决定；用户会直接使用的派生预览采用能忠实读取权威源的最小组合，纯内部预览不进入本路由。

| 领域 | 常见前端组合；仅在需要时采用 |
| --- | --- |
| 品牌识别 | SVG、CSS token、Astro/React 规范页；主交付仍是身份资产 |
| 图标与符号 | SVG sprite、SVGO、图标样张；不必建应用 |
| 字体与字形 | 字体文件 + HTML/CSS specimen；可用 variable font axes |
| 插画与场景 | SVG、PixiJS、Rive、Three.js；按发布尺寸选择 |
| 营销图形 | Astro/React、Motion/GSAP；渠道比例决定输出 |
| 编辑与演示 | Astro/MDX、Slidev/Reveal、Vivliostyle、数据图形库 |
| 数据可视化 | Plot/G2/ECharts/Vega-Lite/D3 + 最少控制层 |
| 技术示意 | SVG、React Flow、ELK、D3；交互由关系复杂度决定 |
| 地图与空间 | MapLibre/PMTiles/OpenLayers/D3 Geo/SVG |
| UI 组件系统 | 现有系统或经能力审核的成熟组件/无障碍原语 + 项目框架 + 必要 catalog |
| 内容网站 | Astro/Next/Nuxt/SvelteKit + 内容层/MDX |
| 产品与工具 | React/TypeScript/Vite + 按产品气质和能力证据选中的一套成熟系统，再按任务增加 TanStack/XState/领域引擎；既有 Vue/Svelte 项目沿用其成熟系统 |
| 解释器与仿真 | 内容框架 + SVG/Canvas/Three + Plot + Motion/GSAP |
| 游戏与趣味 | Phaser/Pixi/Three + 资源、输入和音频管理 |

## 7. 依赖与离线门槛

引入依赖前确认：

- 官方包名、文档和当前维护状态；
- 与项目运行时和 TypeScript 版本兼容；
- 许可允许目标交付；
- 只导入实际使用的模块和组件；
- 字体、图片、worker、wasm、瓦片、模型和音频能按合同部署；
- 离线任务没有 CDN、远程字体、远程图片、在线 API 或隐式遥测；选中的锁定依赖应编入本地生产包，不因离线退回手写控件；共享 runtime 中其他已安装系统不得进入项目依赖或 bundle；
- SSR、静态导出和 hydration 与库的浏览器假设兼容；
- 构建后检查 bundle、控制台、网络请求和关键帧像素。

不要复制网页示例代码便假设包已安装。先检查项目依赖与环境 profile；已提供锁定的本地包时复用它，否则再通过项目包管理器安装并锁定版本。

## 8. 技术决策记录

编码前写：

```text
现有环境：框架、构建、组件、约束
运行层：选择与理由
组件层：候选与选择证据；主系统、锁定版本、复用组件/原语及被阻塞候选
领域层：图表、地图、画布、编辑器或游戏能力
动效/输出层：是否需要，为什么
专属定制：只有本项目需要的部分
明确不用：避免的重复或过重能力
交付策略：在线/离线、静态/服务端、单文件/多资源
风险：兼容、许可、性能、可访问性和回退
```

若“明确不用”为空，重新检查是否堆叠了工具。

## 官方入口

- Appica UI：https://appica.dev/ui?twclid=29gtatkrmtuyybk8krvxieoehx
- shadcn/ui：https://ui.shadcn.com/docs
- Radix Themes：https://www.radix-ui.com/themes/docs/overview/getting-started
- Mantine：https://mantine.dev/
- Chakra UI：https://chakra-ui.com/docs/get-started/installation
- Material UI：https://mui.com/material-ui/getting-started/
- Ant Design：https://ant.design/docs/react/introduce/
- Fluent 2：https://fluent2.microsoft.design/components/web/react/
- Primer：https://primer.style/product/getting-started/react/
- React Spectrum 2：https://react-spectrum.adobe.com/getting-started
- React Aria：https://react-spectrum.adobe.com/react-aria/
- Base UI：https://base-ui.com/react/overview/quick-start
- TanStack：https://tanstack.com/
- Astro：https://docs.astro.build/
- Next.js：https://nextjs.org/docs
- Observable Plot：https://observablehq.com/plot/
- AntV G2：https://g2.antv.antgroup.com/
- ECharts：https://echarts.apache.org/
- MapLibre：https://maplibre.org/
- React Flow：https://reactflow.dev/
- PixiJS：https://pixijs.com/
- Three.js：https://threejs.org/
- Phaser：https://phaser.io/
- Motion：https://motion.dev/
- GSAP：https://gsap.com/docs/v3/
- Slidev：https://sli.dev/
- Vivliostyle：https://vivliostyle.org/

实际使用前打开对应官方文档；若合同要求离线，则读取与锁定版本一起交付的本地文档、类型声明和包元数据，并以可执行 smoke 验证。任何情况下都不从本清单猜 API。
