# UI 组件系统工具能力档案

本文件帮助把任务能力映射到候选工具；它不替代 Shared Tool Registry，也不证明某项目已使用工具。每次选择都从 Registry 读取当前 `availability / version / license / environment_probe`。下文的 candidate id 只是稳定查询键，不携带静态登记或可用状态；Registry 无 exact profile 或严格同义 alias 时只能写 `considered` 或 `unresolved`，不得写 `available` 或 `used`。

项目决策记录使用：

```text
subtype → required capability → candidate ids → chosen foundation
native design language/model → concern authority → custom boundary
real usage evidence → rejected alternatives → failure return
```

一个项目只选一套主要视觉语言。行为基础、catalog、token builder 和 package tool 可以组成有限 pipeline，但每一步必须有独立能力和 authority；它们不能被合并成“万能设计系统”。

## 1. Appica：带视觉意见的发布包

- **Candidate id：** `appica-ui-react`。
- **形态：** styled React package，不是 headless、source registry 或 Figma kit。
- **原生模型：** 发布包拥有组件实现与基础行为；项目 theme/token 源拥有项目视觉映射。选择后应延续其 anatomy、状态、密度、排版、间距、形状、主题与反馈语言。
- **适用：** 当前 React/Tailwind 消费环境可解析该锁定包，目标需要现代中性成品基础且组件覆盖通过 capability probe。
- **不选：** 仅因工具链预装；已有另一套生产系统；目标平台或用户参考要求另一种语言；关键复杂控件缺失。
- **真实使用证据：** 锁定包/许可、真实 import 和样张、样式入口、theme master、rebuild，以及组件与自定义区域的像素一致性。

## 2. React Aria Components：headless 行为与国际化

- **Candidate id：** `react-aria-components`。
- **形态：** 可安装的 React 行为组件包；无成品视觉语言。官方定义为 unstyled，并提供 accessibility、internationalization、interaction 与组件/Hook API。
- **Authority：** 上游 package 拥有通用行为/语义；项目 wrapper 拥有收窄后的 public API；项目 token/CSS 拥有视觉。
- **适用：** 团队已有可信品牌/token 语言，需要完整视觉控制并愿意承担 styling、状态像素与回归成本。
- **不选：** 希望开箱成品审美，或没有 token、内容、主题和像素验收能力。
- **证据：** package import、wrapper 边界、支持组件的键盘/焦点/locale 状态，以及项目视觉 authority。官方入口：<https://react-spectrum.adobe.com/react-aria/getting-started.html>。

## 3. Radix：必须分开 Themes 与 Primitives

### Radix Themes

- **Candidate id：** `radix-themes`。
- **形态：** styled component system；Theme 配置、variants 与公开 token 形成成品视觉语言。
- **适用：** React 产品需要安静一致的系统基础，且其组件覆盖、密度与主题机制满足合同。
- **失败条件：** 把 Themes 当无样式 primitive 后逐组件覆盖 protected language，或与另一 styled system 混用。

### Radix Primitives

- **Candidate id：** `radix-ui-primitives`。
- **形态：** 可安装的 unstyled/low-level behavior packages；负责 WAI-ARIA 语义、focus、keyboard 与 composable parts，不负责项目视觉。
- **适用：** 已有项目设计语言，需要 Dialog、Menu、Popover 等可靠行为基础。
- **失败条件：** 只装 primitive 就声称已有设计系统；wrapper 复制其 DOM/state machine；没有视觉/token authority。官方入口：<https://www.radix-ui.com/primitives/docs/overview/introduction>。

## 4. Open-code 与 source kit：shadcn 和 Untitled UI

### shadcn/ui

- **Candidate id：** `shadcn-ui-local`。
- **形态：** open-code registry/CLI；将组件、hooks、config 等源码写入项目。它明确不是传统 npm component library。
- **Authority：** `components.json` 拥有 registry 配置；检入源码成为项目 component/API master；锁文件拥有行为依赖；项目 token/CSS 拥有视觉。
- **适用：** 团队愿意维护源码、上游更新 diff 和统一 API，并已建立 token 与视觉治理。
- **失败条件：** 把 copied source 写成“采用某版本 package”；随意修改单个控件导致语言碎裂；从多个 registry 拉入互不一致的系统。官方入口：<https://ui.shadcn.com/docs>、<https://ui.shadcn.com/docs/registry>。

### Untitled UI React

- **Candidate id：** `untitled-ui-react-source`。
- **形态：** React/Tailwind/React Aria 的 source kit，通过 CLI 或复制把源码放入项目；不是以组件 package 依赖为主的库。图标 package、免费源码与付费/PRO 内容是不同许可边界。
- **原生语言：** 有完整视觉意见和对应 Figma 体系；采用源码后项目负责保持 anatomy、token、state 与上游来源一致，而不是把它当散装页面模板。
- **适用：** 目标语言与其已查看的真实组件态匹配，React/Tailwind 栈成立，并可承担 copied source 的升级与许可追踪。
- **失败条件：** 未取得具体组件许可；把页面 block 当基础组件系统；把复制源码冒充可升级 package。官方入口：<https://www.untitledui.com/react/docs/introduction>、<https://www.untitledui.com/react/docs/installation>。

## 5. Material UI：Material 语言的 React 包

- **Candidate id：** `mui-material`。
- **形态：** `@mui/material` styled React package，带 Material 设计语言与 ThemeProvider/theme API；不是 headless。实际支持的 Material 规范代际必须按当前官方版本核验，不能凭品牌名假设。
- **Authority：** 发布包拥有组件实现/行为；项目 theme 源拥有被允许的品牌映射；public wrapper 可隔离供应商 API。
- **适用：** Material 生态或其交互、几何与广覆盖组件确实匹配任务。
- **不选：** 用户/平台明确排斥 Material 语言，或需要低视觉意见的行为基础。
- **证据：** `@mui/material` 与 styling peer 版本/许可、真实 imports、ThemeProvider/theme master、复杂组件与 SSR/consumer proof。官方入口：<https://mui.com/material-ui/>、<https://mui.com/material-ui/customization/theming/>。

## 6. 平台/企业 styled systems：Fluent、Carbon、Ant

这些系统都是有视觉和交互意见的完整体系，不是可互换的“后台组件包”。选中其中之一后继承其原生语言，不能再叠加另一套 styled system。

| Candidate id | 事实形态与适用信号 | 不选或失败信号 | 官方来源 |
| --- | --- | --- | --- |
| `fluent-ui-react-v9` | `@fluentui/react-components` React package；Fluent 2 的生产力、协作与 Microsoft 平台语言 | 无平台关联却借 Microsoft 外观；混用旧/新 Fluent API 未设迁移边界 | <https://fluent2.microsoft.design/>、<https://react.fluentui.dev/> |
| `carbon-react` | `@carbon/react` package、styles/icons 与 Carbon token/主题语言；适合受规范约束的数据和企业界面 | 轻量消费界面；只借灰色/高密表面；绕过 Carbon role tokens | <https://carbondesignsystem.com/developing/frameworks/react/>、<https://carbondesignsystem.com/elements/themes/overview/> |
| `ant-design-react` | `antd` package；面向丰富企业交互，具国际化、ConfigProvider 与 global/component token 主题 | 低密消费或编辑体验；仅为表格引入后让其余界面使用第二套语言 | <https://ant.design/docs/react/introduce/>、<https://ant.design/docs/react/customize-theme/> |

选择这些 candidate 前必须从 Registry 核对当前 package version、license、环境 probe、权威 master、import/export/rebuild 与 evidence contract；事实不全时保持 `considered` 或 `unresolved`，本表不构成 availability。

## 7. Storybook：catalog 与 component-test 环境

- **Candidate ids：** `storybook-react-vite`；需要浏览器行为取证时另查询 `storybook-playwright-component-proof`，不得从名称推定 pipeline 可用。
- **形态：** CSF stories + runtime/static catalog + interaction/a11y tooling；没有产品视觉语言。
- **Authority：** story 只拥有 example contract 与 fixture；Storybook config 拥有 catalog build。组件 API、behavior、token 和 release 仍回各自 owner。
- **适用：** 隔离浏览 public states、文档和组件级交互；story 必须从 public entry import。
- **不能证明：** package 可安装、独立消费者采用、生产兼容、设计质量或 release stability。官方入口：<https://storybook.js.org/docs/get-started/browse-stories>、<https://storybook.js.org/docs/writing-tests>。

## 8. Token interchange 与 build

- **Candidate id：** `style-dictionary`。
- **形态：** token build system；读取 token source，通过 platform-specific transforms/formats 生成 CSS、JS、iOS、Android 等输出。它不提供组件或视觉语言。
- **Authority：** token 数据是 master；Style Dictionary config 拥有 transform；生成文件是 derived consumer artifacts。
- **DTCG 边界：** DTCG format 是交换格式规范，不是执行工具、同步服务或治理成熟度。不要把“符合 DTCG”写成设计—代码自动一致。
- **适用：** 一个权威 token 源需要确定性生成多个消费格式。
- **失败条件：** 编辑生成物；不同平台各自改 token；未固定 transform/version；没有 drift/rebuild 证据。官方入口：<https://styledictionary.com/getting-started/installation/>、<https://styledictionary.com/info/tokens/>。

## 9. Package build 与分发证明

- **Candidate id：** `npm-cli-pack`。
- **形态：** package manifest/exports 与 `npm pack` 形成版本化 tarball 的工具能力；不提供组件、视觉语言、版本策略或消费者采用。
- **Authority：** `package.json`/build config 拥有 public entry 与文件边界；组件/token 源仍是上游；tarball 是 derived artifact。
- **适用：** Node package 需要核对实际发布文件和 clean install 输入。
- **不能证明：** tarball 能被目标消费者正确使用、已发布到 registry、semver 正确或迁移完成。必须另有独立 consumer 的 install/import/type/build evidence。官方入口：<https://docs.npmjs.com/cli/pack/>、<https://docs.npmjs.com/cli/configuring-npm/package-json/#exports>。

Workspace resolver、bundler、changeset/release automation 应按项目栈分别登记；不要把它们揉成一个跨所有语言的 distribution profile。

## 10. Figma：可选设计母版与人工交接

- **Candidate id：** `figma-design`。
- **形态：** GUI/SaaS 设计创作与 library 分发；平台本身不提供应被所有项目采用的视觉语言。
- **可拥有：** 经明确分工的 design intent、design variables 与设计—代码 mapping。
- **不可拥有：** 运行时 behavior、package exports、consumer build 或 release truth；截图和导出也不是设计文件的替代 master。
- **使用条件：** 有真实席位/许可、文件 identity、操作或交接记录、editable nodes/variables、export/rebuild 与代码映射证据。否则保持 `human_handoff` 或 `unavailable`，不得模拟 `used`。

## 运行时解析

项目只提交本轮能力所需的 candidate ids，并保存 Registry 的 exact/alias/unresolved 解析结果。严格 alias 必须是同一工具、同一分发形态的同义标识；相同用途的不同工具不能互作 alias。Registry 状态变化只改变项目选择与可声明证据，不回写成本文件中的静态事实。
