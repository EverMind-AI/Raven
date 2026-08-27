# UI 组件与设计系统模式

本文件是按需配方，不是组件清单。先选 `create / edit / diagnose / audit / migrate`，再选实际子型：token/主题、组件/API、复杂控件、文档/catalog、分发/package、采用/迁移。未被 target claims 触发的子型不扩产物。

## 1. 操作模式与子型

| 模式 | 核心动作 | 必须避免 |
| --- | --- | --- |
| create | 建立目标消费者所需的最小权威链与真实切片 | 自动膨胀成企业治理平台 |
| edit | 保留既有 public contract，给受影响 API/token/行为差异和回归 | 重写未受影响系统 |
| diagnose | 只读复现症状，定位最早失效的 concern、影响范围与剩余假设 | 为取得新证据而写源码、配置、快照或缓存 |
| audit | 只读核对来源、imports、exports、状态、消费者和发布事实 | 改锁文件、源码、快照或缓存 |
| migrate | 从真实旧调用建立映射、批次、回退与退出条件 | 虚构 legacy、采用量或生产 owner |

Diagnose/Audit 都映射到零写入路径，但输出不同：Diagnose 从可复现症状追到受支持的根因与 failure return；Audit 从既定合同检查覆盖、差异和证据缺口。二者都不能借“验证”创建 proof 副本，也不能隐式升级为 Edit。

子型可以组合，但“有 tokens”“有 Storybook”“有组件源码”“有 package”是不同 claims。先写本轮交付与明确非目标，避免把后续成熟度要求提前强加。

## 2. Consumer contract

每类消费者先记录介质中立核心合同：

```text
consumer_id / verified_or_target / final_consumer
target_medium / target_renderer / target_context
consumption boundary / version or revision / portability
public interface / required components, states, themes, locale and input modes
acceptance task / evidence / owner / rollback
source provenance / license / authoritative master / build hash
```

再按目标媒介追加字段；不适用项写 `N/A + 媒介理由`，不能把 Web 字段强加给其他消费者：

| 目标媒介 | 附加合同 | 消费证据 |
| --- | --- | --- |
| Web | runtime/framework/browser matrix；public imports、style/token entry、provider；SSR/CSP/offline | 浏览器 DOM/SVG、computed-style、交互事件与最终像素分别取证 |
| 原生平台 | OS/SDK/runtime/device matrix；module/package/resource entry；平台主题与输入模型 | 目标 renderer、原生 accessibility tree/action、设备或模拟器最终像素 |
| 设计工具 | 工具、文件/library identity、版本、席位/许可；组件属性、变量与发布接口 | 可编辑原生母版、重开、library 消费、映射与导出重建 |
| 设备或物理界面 | OS/firmware、输入/输出、生产规格、材料与观看条件 | 目标设备 renderer、真实控件状态、设备照片/测量或实体样张 |

DOM/ARIA 仅适用于 Web 消费者；其他媒介使用自身的语义树、可访问性 API、输入协议与目标 renderer。浏览器代理只能证明其派生预览，不能替代原生平台、设计工具或设备的最终消费者证据。

将证据强度绑定 target profile：

| 目标 | 消费证据 |
| --- | --- |
| D10-M0 | specimen 只证明合同可理解，不算 consumer |
| D10-M1 | 本仓库应用可从 public entry 运行；仍可声明 repo-only |
| D10-M2 | 独立构建边界从版本化产物导入并完成类型与生产构建 |
| D10-M3 | 真实生产消费者加独立验证消费者，或两个彼此独立的消费者；包含一次升级/弃用路径 |
| D10-M4 | 多平台或多品牌采用与兼容治理按声明范围取证 |

Story、文档站和维护该组件库的 demo 与源码共用构建图时，不算独立消费者。消费者数量只服务目标 claim，不外推为普遍采用量。

## 3. Authority matrix

先按 concern 建图，再决定工具：

| concern | 可接受 owner | 派生输出 | 漂移检查 |
| --- | --- | --- | --- |
| reference / semantic token | 版本化 token 数据或平台原生变量母版 | CSS、TS、Sass、原生资源、设计变量 | 重建 diff、未映射值审计 |
| component API | public types、exports、组件源码 | API 表、示例签名 | API snapshot、consumer typecheck |
| behavior / a11y | 组件实现与交互测试 | story、trace、报告 | 键盘/焦点/目标媒介语义 API/恢复测试；Web 时含 ARIA |
| design intent | 获批准的设计母版或版本化 rendered reference | 标注、预览、设计映射 | component/variant 映射与视觉 diff |
| narrative docs | 人工维护的使用与内容规则 | 文档页面 | owner review、断链检查 |
| distribution / release | package manifest、构建配置、tag 与发布记录 | tarball、registry 页面、changelog 页面 | pack 清单、hash、clean install |

同一 token 不能同时由 Figma variables 和代码 JSON 权威拥有；可指定一个 owner，另一端通过有证据的 transform 派生。设计工具可以拥有设计意图，代码拥有行为；“双向同步”若没有冲突规则和可复现轨迹，应改称人工映射。

## 4. Token 与主题合同

```text
reference: color.*, type.*, space.*, size.*, radius.*, shadow.*, motion.*
semantic:  surface.*, content.*, border.*, action.*, focus.*, feedback.*
component: button.*, field.*, dialog.*, table.* ...
```

- reference 表示可用尺度，semantic 表示职责，component 表示 slot/state 映射；
- 只有跨组件复用、主题化或治理需要的决定才成为公共 token；
- 组件不得绕过 semantic 层散落 raw values；主系统已有 token 时优先原生 token 或一一映射；
- 明暗、高对比、品牌或密度 mode 替换角色值，不改变 API、焦点顺序或状态名称；
- token transform 配置是 authority edge，生成的 CSS/TS/平台文件不是上游真源；
- 记录弃用别名、破坏性重命名、设计映射和消费者升级路径。

主题支持矩阵按任务列出系统偏好、持久化、初值、嵌套和目标平台的高对比/减弱动效/打印等能力；Web 才追加 SSR、forced-colors 与浏览器打印合同。未测试写 `unsupported` 或 `unverified`，不写成支持。

## 5. 组件族与 public API

组件族可包括操作、表单、导航、弹层、反馈、数据展示和布局工具；只实现 consumer contract 需要的族。每个 public 组件至少记录：

```text
responsibility / foundation / maturity
anatomy and slots / composition rules
variant axes / runtime states / illegal combinations
controlled and uncontrolled model / defaults
events / ref and focus contract / form relationship
content, locale, overflow and responsive boundaries
token mapping / accessibility contract
public export / version / owner / deprecation
```

优先 composition、slot 或有限枚举；不要堆叠会冲突的布尔 props。Wrapper 只收窄 public API、统一默认文案/事件/表单关系或映射 token，不复制上游渲染树、focus 和状态机；Web 的渲染树具体表现为 DOM。业务权限、分析、持久化和领域结果属于产品适配层。

复杂控件先写数据规模、异步、选择模型、键盘模型、焦点、locale、移动替代和性能边界，再做能力探针。Combobox、date/range、grid、tree、dialog/menu、upload、editor、drag-and-drop 不得用静态外观冒充成熟行为。

## 6. Variant、state 与 accessibility

| 轴 | 示例 | owner |
| --- | --- | --- |
| variant | size、density、emphasis、tone、orientation | public API 与 token mapping |
| interaction | hover、pressed、focus-visible、disabled、read-only | 行为实现 |
| selection/disclosure | selected、checked、expanded | 单一运行时 state owner |
| data | empty、loading、partial、error、success、stale | 组件或组合合同 |
| business outcome | submitted、approved、failed job | 产品/领域层，不进入基础原语 |

从 API 合同生成适用矩阵，不做全排列。验证真实事件而非强加 CSS class：

- 键盘序列、可见焦点、关闭后的合理焦点返回；
- 可读名称、描述、错误关联、live feedback 与非颜色冗余；
- pointer、touch、IME、RTL、本地化、长内容、缩放与 forced-colors；
- loading 的重复提交防护，异步竞态/取消、空结果、失败重试和 stale result；
- controlled/uncontrolled 一致性，目标媒介语义树、视觉与事件同源；仅 Web 检查 DOM/ARIA。

自动扫描只能发现部分问题；claim 必须说明测试环境、组件、状态与辅助技术范围。

## 7. 分发形态不可混称

| 形态 | 权威母版与消费 | 可以声明 | 不能声明 |
| --- | --- | --- | --- |
| source-only / copied source | 项目检入源码并自行构建 | 源码已纳入项目维护 | 采用了可升级组件 package |
| source registry | registry item/CLI 把源码和配置写入项目 | 通过该 registry 导入了特定源码 | 上游自动拥有本地修改或运行版本 |
| workspace package | monorepo package + workspace resolution | 在该 workspace 的 package boundary 消费 | 可被外部 clean consumer 安装 |
| tarball/private package | 版本化 artifact + 显式 exports | 在声明环境可安装与消费 | 已公开发布或广泛采用 |
| public package | registry artifact、版本与公开入口 | 对应版本已发布 | 任意消费者兼容或迁移已完成 |
| catalog | stories 与静态站 | 组件可发现并在隔离状态运行 | package、独立采用或发布稳定 |

可安装 claim 要保存实际 package artifact 与文件清单；clean consumer 不得访问 `src/`、内部 alias 或复制 CSS，必须导入 public JS/types/styles/tokens/provider，并执行目标 runtime 的类型检查和生产构建。复杂组件、失败状态和主题只在 target claims 要求时加入，而非凑清单。

## 8. 文档与 catalog

文档子型按职责组织，而不是截图画廊。组件页覆盖适用内容：用途/禁用场景、anatomy、composition、API、状态/variant、内容与 locale、键盘/a11y、token/theme、import、版本、owner、限制和迁移。

Story 从 public entry 加载真实组件；fixture 只提供确定输入，不复制实现或维护第二套状态。交互 story、a11y 结果和浏览器帧绑定同一 build。Catalog 证明隔离开发与检索，不拥有 API、behavior、token 或 release concern。

## 9. 发布、弃用与迁移

按公共影响区分 token、public API、渲染/语义结构、事件、样式和视觉变更；Web 的结构变更再细分 DOM。需要发布/治理时记录：

- version、artifact hash、exports、peer dependencies、style/provider 入口与支持矩阵；
- changelog、owner、维护窗口、deprecation warning 与移除版本；
- consumer upgrade diff、codemod/迁移说明、兼容层与 rollback；
- 真实调用点的 before/after、剩余调用和退出条件；
- CI 中与 claims 相称的 API、type、interaction/a11y、visual、theme、locale 和 pack/consumer proof。

没有真实旧实现时只写 adoption guide；用户要求迁移演示时明确标注 synthetic fixture。M3 的双消费者证据用于检验 public contract 不依赖单一宿主，不能由两张相似 specimen 代替。

## 10. 领域验收

- [ ] 目标档案、消费者和分发形态与实际证据一致；
- [ ] 每个 concern 只有一个 authority owner，所有派生物可追溯；
- [ ] 一套主要设计语言贯穿基础组件、外壳和自定义区域；
- [ ] source registry、package、workspace、catalog 与 Figma 没有互相冒充；
- [ ] public API、token、行为、a11y 与文档引用同一实现；
- [ ] 自定义部分有 capability probe、gap、最小边界与 coherence evidence；
- [ ] consumer、发布、稳定、迁移与治理 claims 未超过 target profile；
- [ ] 共享 review、promotion、release 和 canonical 没有被领域 gate 代替。
