---
name: build-ui-components-and-systems
description: "构建、扩展、审计或迁移可复用的 UI 组件与设计系统。当交付核心是跨页面或跨消费者复用的 token、组件 API、交互与无障碍合同、设计—代码映射、组件文档、分发包、版本治理或采用迁移时使用；若核心是完成一个产品工作流、阅读内容或单项视觉资产，则改用相应领域 Skill。"
---

# 构建 UI 组件与设计系统

## 领域结果

交付可被声明消费者真实导入、组合、验证和升级的界面合同，而不是组件展厅、业务页面换名或一组截图。系统的核心链是：`token → component API → behavior/a11y → distribution → consumer → governance`。

本 Skill 保持介质中立。先明确最终消费者、目标媒介与 `target renderer`，并记录组件、字体、图标和
数据的来源与许可；验收发生在真实消费接口、状态与最终像素，不默认落到浏览器展示页。

只有最终消费者或合同交付确为 Web 时，才调用 `$build-polished-visual-frontends`，由它负责 Web 技术栈、整体视觉语言和浏览器最终像素；非网页媒介不得为了预览方便加载该 Skill，也不能用浏览器证据冒充目标消费者证据。本 Skill 始终拥有组件系统合同与领域 gate；共享 `visual-artifact-design` 拥有工具证据、authority graph、渲染、review 和 scoped promotion，这里不另造生命周期或晋升状态。

按需读取：

- [patterns.md](references/patterns.md)：子型、consumer contract、API、token/state、a11y、分发与治理模式；
- [tool-profiles.md](references/tool-profiles.md)：领域工具能力、分发形态与选择失败条件。

## 1. 路由任务与声明上限

先记录 `mode / primary_domain / final_consumer / target_medium / target_renderer / target_profile / consumer_scope / target_claims / source_provenance / risk_triggers / explicit_non_goals`。模式为 `create / edit / diagnose / audit / migrate`；migrate 必须有真实旧实现或明确标注的 synthetic fixture。

- `greenfield` 才从消费者任务建立所需的最小 token、API、行为、分发和治理权威链，不预造企业级全家桶。
- 已有系统先锁定现有 owner、公共合同、消费者和兼容边界，再按变更影响图只检查和修改受影响类别；未受影响的 token、组件、状态、文档、分发或治理保持原样，不因局部 Edit 全量重做、换肤或迁移。
- Diagnose/Audit 一律零写入，只定位最早失效的 concern、受影响类别和返回 owner；无修复授权时不创建 proof、快照、缓存或“顺手修正”。

以下是目标档案与 claim ceiling，不是所有项目必须依次走完的线性流程：

| target_profile | 可声明的最高结果 | 必要边界 |
| --- | --- | --- |
| D10-M0 | 组件契约样张 | 可说明 token、API 与状态；不声称可导入或采用 |
| D10-M1 | 实现支撑的系统试验版 | 有真实源码、public entry、状态证据与文档；可仍限本仓库 |
| D10-M2 | 可采用 Preview | 有版本化可消费边界、显式 exports、独立消费者及行为/a11y 证据 |
| D10-M3 | Stable 系统 | 有生产采用或双消费者验证、升级/弃用、owner 与发布历史 |
| D10-M4 | 治理型多产品系统 | 有多平台或多品牌采用、兼容政策、同步与长期治理证据 |

直接选择与任务相称的档案；更高档案只增加被其 claims 触发的 gate。未满足证据时降低实际 claim ceiling，不用“完成度”掩盖缺口。

## 2. 建立 concern authority chain

先列消费者和 concern，再实现。一个 concern 只能有一个权威 owner；多个原生母版可以并存，但导出、截图、catalog 和 Gallery 都是派生物。

| concern | 常见权威 owner |
| --- | --- |
| reference / semantic tokens | 版本化 token 源；CSS、TS、平台变量为生成物 |
| component API | public types、exports 与组件源码 |
| behavior / accessibility | 组件实现与可重复交互测试 |
| design intent | 获批准的设计母版或版本化 rendered reference |
| documentation | 生成的 API/token 文档与人工叙事各自拥有明确部分 |
| distribution / release | package manifest、构建配置、tag、changelog 与发布记录 |

Figma 不是默认真源。只有存在真实文件、许可/席位、调用或人工交接证据时，它才可拥有 `design intent` 或设计变量；它不能接管代码行为、包 exports 或发布事实。设计与代码不一致时，返回对应 concern owner，不在派生预览中补丁。

## 3. 选择并忠实采用成熟基础

先写：`任务子型 → 必需能力 → Registry 候选 → 选择理由 → 原生设计语言 → authority owner → 自定义边界 → 使用证据 → 失败返回`。Registry 只证明工具事实与当前可用性；项目为何选择它由本领域记录负责。

优先延续已有且被真实消费者采用的合格系统；否则选择一套主要成熟系统。选定 styled system 后，继承其 token、组件 anatomy、交互状态、排版、间距、密度、主题与反馈语言。只借一个控件，其余外壳按另一套字体、灰阶、圆角或状态语言手写，直接判定 foundation gate 失败。

Headless library 只拥有行为与语义，必须另有项目视觉 authority；source registry 或 source kit 把检入源码交给项目维护，不能冒充版本化组件包；workspace source 不能冒充可外部安装；Storybook 不能证明消费者采用。具体边界见 [tool-profiles.md](references/tool-profiles.md)。

工具只有在 dependency/version/license、真实 import/invocation、editable master、rebuild/export 与当前像素或消费者证据齐全时才记为 `used`。成熟能力缺失前先运行 capability probe；只有观察到 gap，才记录 `minimal_custom_boundary` 与 coherence evidence。不可用返回替代候选或 `human_handoff`，不把环境缺失写成工具能力缺口。

需要写入的专业工具 proof 仅允许 Create/Edit 在授权 workspace 建立一次性验证副本；该副本不能覆盖权威母版，也不能改变 canonical 或 promotion。proof 与最终交付分别记录来源、工具版本、输入/输出及 build hash，二者 hash 不同就不能继承结论；Diagnose/Audit 一律零写入。

## 4. 领域 gates

每个适用 gate 按共享格式记录 trigger、inputs、actions、evidence refs、claim ceiling 和 failure return。`not_applicable` 必须由 target claims 证明；未知、未实现或工具不可用不等于不适用。各轨道可并行，只按真实依赖排序。

- **D10-G0 路由与目标** — 输入 brief、现状、最终消费者、目标媒介/renderer 和来源/许可；确定 greenfield 或已有系统分支、子型、target profile、风险与非目标；证据是已接受领域合同；失败返回路由；最多声明“范围已界定”。
- **D10-G1 消费者与权威** — 输入真实 imports、token/设计/代码/发布源和变更影响图；建立 consumer contract 与逐 concern authority；已有系统只纳入受影响类别；证据是可追溯矩阵和无冲突 owner；失败返回合同；最多声明权威边界成立。
- **D10-G2 Foundation fidelity** — 输入 Registry 事实、候选样张和 capability probe；选择一个主系统并冻结原生语言、自定义边界和工具证据；失败返回候选选择；最多声明被证明的 foundation 已真实采用。
- **D10-G3 Contract slice** — 输入 token、public API 与关键组件族；构建最小真实切片，证明 anatomy、variant、state 与组合共享同一合同；失败返回 token/API owner；按实现证据最高到 M0 或 M1。
- **D10-G4 Behavior and access** — 由交互、复杂控件或支持矩阵触发；验证键盘、焦点、名称/关系、输入方式、本地化、恢复与适用状态；失败返回行为 owner；只覆盖已测试组件、状态与环境。
- **D10-G5 Distribution and consumer** — 由 M2+、可安装或跨边界复用声明触发；从权威源构建版本化产物，经 public exports 在独立消费者中导入、类型检查和生产构建；失败先回 package boundary，API 错误再回合同；最高到已证明 consumer scope。
- **D10-G6 Change governance** — 由 M3/M4、发布、升级、弃用或迁移声明触发；验证 owner、版本、changelog、兼容窗口、迁移/回退与采用证据；失败返回变更 owner；最高到实际治理覆盖范围。

### 并行证据轨道

| 轨道 | 拥有的证明 |
| --- | --- |
| DESIGN / TOKEN | 设计意图、token 角色、主题与生成映射 |
| CODE / BEHAVIOR | public API、运行状态、交互与实现 |
| DOCS / ACCESS | 使用合同、内容规则、键盘与辅助技术范围 |
| ADOPTION / RELEASE | 分发物、消费者、版本、迁移与维护责任 |

失败只返回首个被证据否定的 owner 或 gate；保留其他轨道仍有效且绑定当前 build 的证据。target profile 只是这些轨道的声明组合，不是第五条状态轴。

## 5. 构建领域合同

Token 采用 reference → semantic → component 三层；主题替换角色映射，不逐组件打补丁。公共组件记录 responsibility、anatomy/slots、variant、运行状态、受控/非受控模型、事件、ref、内容边界与版本。复杂控件优先由成熟行为基础拥有状态机，项目 wrapper 只收窄 API、映射 token 和统一内容策略。

强调色、色条、边线、图标容器或背景层只有在 semantic token 稳定映射组件职责、交互状态、真实分组
或已批准身份语法时，才能成为系统能力；不能为一次页面装饰新增 token。视觉面积和对比按组件职责、
状态风险与使用频率分配，说明邻近受影响状态。CSS 可实现布局、排印、必要边界和状态反馈，但不能用
渐变、伪元素或边框制造脱离组件合同的装饰资产；展示页背景也不能替组件本身证明系统质量。

状态分为 variant、interaction、selection/disclosure、data 与 business outcome；只测试合同允许的组合，不生成笛卡尔积。可见文本、ARIA、样式与事件共享状态 owner；错误、异步竞态、取消、重试和焦点返回都有明确恢复路径。

Catalog 从 public entry 载入真实组件和状态。文档可证明可发现性与局部行为，却不能证明 package 可安装、独立采用或稳定发布。分发形态必须明确为 source-only、workspace、tarball/private package 或 public package，并按其真实边界命名。

## 6. 证据与交付

证据量服从 target claims，不以固定组件数、story 数或截图数代替覆盖。M2 需要独立 consumer；M3 的跨消费者稳定性需要生产采用加独立验证消费者，或彼此独立的消费者，并覆盖一次真实变更路径。详见 [patterns.md](references/patterns.md)。

四类证据必须分别命名，不能互相替代：

| 证据类型 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| 浏览器 DOM/SVG | Web 宿主中的结构、语义、边界框和可枚举状态 | Canvas/WebGL/raster 内部事实，或原生、设计工具、印刷与设备消费者 |
| computed-style | 指定浏览器 build 的级联结果、实际字体/颜色/间距与可见状态 | 另一渲染器的排版、色彩管理、事件模型或最终像素 |
| 目标 renderer | 声明平台、宿主或生产导出链对当前 build 的真实渲染与交互 | 未测试平台、未覆盖状态或物理生产结果 |
| 实体样张 | 指定材料、设备、观看距离和生产条件下的物理结果 | 组件 API、包可消费性或其他材料/批次 |

Web 是最终消费者时，浏览器可以同时是目标 renderer，但 DOM/SVG、computed-style 与最终像素仍是不同层次。最终消费者不是 Web 时，浏览器目录、Storybook 或 HTML specimen 只可作为派生索引；必须以目标 renderer 取证，物理交付还要实体样张或明确的人工交接。所有证据绑定同一来源、权威母版、build hash、消费者和验收条件。

最终报告 target profile、实际 claim ceiling、通过/失败/不适用的 D10 gates、authority owners、来源与许可、工具使用状态、分发形态、最终消费者、目标 renderer、版本、证据 hash 与限制。渲染/review assurance、promotion、release 和 canonical 继续由共享治理记录表达；任何一项都不能从 D10 gate 自动推导。

## 失败信号

- 只有组件展示页、静态矩阵或设计稿，没有可消费 API；
- 同时混用多套有视觉意见的系统，或组件与自绘区域像不同产品；
- copied source、workspace、Storybook 或 Figma 被写成可安装 package 证据；
- token、API、行为、文档或发布存在双重 authority；
- 手写复杂状态机却没有 capability probe、最小边界与一致性证据；
- 为单个页面新增无语义 accent token、色条或包裹框，删除后组件职责与状态完全不变；
- 支持、采用、稳定、迁移或 owner 数字来自文案而非当前 build 证据。
