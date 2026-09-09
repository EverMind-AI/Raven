---
name: build-interactive-explainers
description: "构建、改造或诊断由可执行模型驱动的交互解释器、计算器与仿真。适用于用户通过参数、状态、步骤或事件理解规律的任务；负责主型分路、reference model、独立 oracle、状态复现、表示忠实、学习任务、专业工具选择及限定 claim，不用于以外部数据分析、持续生产工作流或计分挑战为主的产物。"
---

# 构建交互解释器与仿真

## 领域权责

本 Skill 拥有领域路由、模型与学习 claim、八个领域 gate、专业能力需求、claim ceiling 和失败返回；
任务契约、工具事实、authority/promotion、渲染、review 与交付语义沿用共享视觉底座。

本 Skill 保持介质中立。开工前锁定最终消费者、目标媒介或 renderer、输入设备、尺寸/观看条件、必须可达的状态和最终验收；介质由任务和真实消费环境决定，不因实现方便默认 HTML、浏览器外壳或固定画幅。最终像素、声音、触觉、打印、原生场景或可编辑文件只检查当前媒介实际承诺的通道。

只有最终消费者或合同交付明确为 Web 时，才加载 `$build-polished-visual-frontends`，由它补充 Web 技术栈、组件系统、响应式、DOM/CSS、浏览器交互与像素批评。非网页媒介不能用浏览器截图、Gallery iframe、HTML 查看器或 DOM 测试冒充目标消费者证据；浏览器只作为查看器时，必须回到目标 renderer、实体样张、原生应用、投影/打印环境或声明设备完成最终验收。

模型契约、生产计算、独立 oracle、实验状态、表示映射、学习任务和交付源是不同 concern。Reference model 与 production computation 拥有规则和结果 truth；表现层只消费带版本的权威 result/snapshot，拥有尺度、编码、布局、相机和交互映射，不能反向改写模型或以动画补出不存在的因果。每个 concern 只能有一个 authoritative owner，但不同 concern 可以由不同原生母版拥有；模型或结果 hash 改变时，相关表示与最终消费者证据全部 stale。截图、Viewer、导出和缓存只能是 derived output。

## 1. 先分路，再允许实现

先写一句：`用户改变什么 → 模型执行什么 → 观察什么证据 → 最多能解释什么`。

| 主型 | 成立条件 | 默认 claim ceiling |
| --- | --- | --- |
| **explainer** | 操纵输入或步骤是为了理解、解释或迁移一条规律 | 当前模型范围内的关系被可操作地揭示；不声称学习效果 |
| **calculator** | 主要价值是从已验证输入得到结果，理解过程并非必要 | 按声明模型计算结果；不声称时间过程、因果学习或现实预测 |
| **simulation** | 状态依规则、时间、事件、随机或求解器演化，过程本身影响解释 | 复现声明模型的演化；不等同真实系统或决策工具 |
| **game** | 主要循环是挑战、计分、胜负、解锁或技巧表现 | 路由到游戏领域；模型说明只能作为次要合同 |

外部观测数据的比较与发现路由到数据可视化；持续保存、协作和运营处置路由到产品工具；无需干预即可理解的固定关系路由到技术图解。文件是 HTML、SVG、Canvas 或 WebGL 不改变主领域。

## 2. 专业工具与 authority 接口

按 `任务子型 → 所需能力 → Registry candidate → 选择理由 → 原生模型/语言 → authority concern → 使用证据 → 失败返回` 选择工具。具体路由读取[专业工具能力档案](references/tool-profiles.md)，领域模式读取[模式与检查](references/patterns.md)。

- 先读取共享 Tool Registry；档案中的 candidate id 不是 availability 或 usage 证明。
- 选定工具后，让其原生方程、构造图、状态模型、数据集、组件、管线或导出成为对应 concern 的作品语言，不只借一个控件后手写其余能力。
- `used` 必须有依赖、解析版本、许可、真实调用、可编辑母版、重建/导出及当前 consumer/pixel 证据。
- GUI、商业或当前不可执行的候选只记录 `unavailable` 或 `human_handoff`；不得模拟调用。
- 自研前必须实际运行 capability probe；只有 `fail` 能证明能力缺口。`unavailable` 返回其他候选或 handoff。
- 自定义只拥有已证明缺失的最小边界，并记录数据、交互、表示和导出的一致性证据。
- 专业工具若必须写入才能形成 capability proof，`Create/Edit` 只可在用户授权 workspace 内操作隔离的“一次性验证副本”：记录输入 hash、最小真实操作、重开/重算、导出 hash 和目标消费者结果；不得写入 canonical master、替换当前交付或改变 canonical/promotion。`Diagnose/Audit 一律零写入`，不得为取得 proof 新建验证副本、改配置、重算缓存或制造新帧。Proof 与最终交付分别记录自身 hash；前者只回答被探测能力，不能自动晋升后者。

## 3. 八个领域 gate

每门按共享 gate schema 记录 `trigger / inputs / actions / evidence_refs / claim_ceiling / failure_return`。未知、未测和工具不可用不能写成 `not_applicable`。

### G1 `interactive.claim-route-locked`

- **输入与动作：**锁定受众、主型、一个主问题、干预、可观察证据、现实使用边界及禁止措辞。
- **晋升证据：**claim card、主型判定和可完成任务；混合任务说明各 concern 的 owner。
- **失败返回：**回到需求与主领域路由；不以增加控件掩盖类别冲突。
- **Ceiling：**只能声称问题与类别已界定，不能声称模型有效。

### G2 `interactive.reference-model-passed`

- **输入与动作：**建立 reference model：来源/版本、方程或规则、变量与单位、初值/边界、假设、合法域、可信域、教学域和限制。
- **晋升证据：**可追踪 model contract、量纲/规则核对、参数来源和风险分级；自行简化必须显式。
- **失败返回：**回 G1 缩小 claim，或更换 reference model。
- **Ceiling：**只能声称模型被明确且有依据地描述，不能声称生产实现正确或能预测现实。

### G3 `interactive.toolchain-authority-passed`

- **输入与动作：**为建模、生产计算、oracle、表示和交付选择最小工具链；为每个 concern 指定唯一 authority owner 和 derived edge。
- **晋升证据：**Registry refs、选择记录、原生母版、tool-use 或 honest non-use、重建路径；适用时附 capability probe 与最小自定义边界。
- **失败返回：**能力不合适回候选比较；环境不可用回替代或 `human_handoff`；owner 冲突回 authority graph。
- **Ceiling：**只能按完整 tool-use evidence 声称所选工具实际拥有并执行对应 concern；不能据此声称模型或结果正确。

### G4 `interactive.computation-oracle-passed`

- **输入与动作：**先判断解析解、有限真值表或不变量是否足够；解析可得时优先解析，否则使用成熟求解器并增加收敛、残差、独立实现或权威 benchmark。
- **晋升证据：**与生产核心职责独立的 oracle、reference vectors、边界/事件/误差测试和由 claim/risk 推导的容差。
- **失败返回：**方程错回 G2；工具或离散路径错回 G3；生产实现错留本门修复。
- **Ceiling：**只能声称当前实现于已测域和误差内忠实执行 reference model。

### G5 `interactive.state-replay-passed`

- **输入与动作：**按主型分离 draft、committed scenario、model events、result、playback 和 view state；只保留适用状态，并定义提交、事件排序、重置、序列化与重放。
- **晋升证据：**state schema、transition/event table、同规格重放 trace、边界与错误恢复；墙钟只能推进结果游标。
- **失败返回：**结果不同回 G4；状态所有权或工具集成错误回 G3；事件语义不成立回 G2。
- **Ceiling：**只能声称声明场景在测试环境可确定复现，不能声称表示或学习任务成立。

### G6 `interactive.representation-fidelity-passed`

- **输入与动作：**把图形、公式、读数、表格、文字和替代表示逐项映射到同一权威 result/snapshot，并声明尺度、采样、插值、精度和不确定性；按已锁定媒介确定目标 renderer 与表现层 authority。
- **晋升证据：**representation map、同快照抽查，以及在真正最终消费者中取得的关键状态证据和最终像素/最终输出。聚合或 well-mixed 标量不得画成局部方向、梯度、热点、轨迹或流场；这些编码必须由真实坐标场及分辨率支撑。
- **失败返回：**数据错回 G4，时间/选择错回 G5，只有显示变换错时留本门。
- **Ceiling：**只能声称已检查的表示忠实投影模型结果，不能提升模型或学习 claim。

表示层中的色条、边框、轨道、图标框、动画强调和背景场必须映射模型结果、view state、选择、尺度、
不确定性或学习关系；删除后理解路径与状态判断不变，就删除。视觉面积和运动强度按主要问题、干预和
结果风险分配，限制邻近受影响输出或控件，不复制成巨幅告示。CSS 只承担布局、排印、必要分隔、
真实反馈及成图裁切，不用渐变或伪元素制造与模型无关的“科学感”资产。

Web 开场若用生成背景建立解释世界，只在承担该职责的页面族使用；进入操作与证据区后，以同源资产、
编码、排印和交互延续，不重复铺图或套“背景图 + 半透明面板”。

### G7 `interactive.learning-task-passed`

- **输入与动作：**若存在学习 claim，按子型建立最小闭环：预测/候选 → 真实动作 → 过程与结果证据 → 解释 → 新条件复核；calculator 无学习 claim 时可有证据地 `not_applicable`。
- **晋升证据：**完整任务 trace、同源解释、错误/边界/反例和迁移检查；自由探索或控件可点击不算闭环。
- **失败返回：**目标不清回 G1，模型不足回 G2，证据关系错误回 G6。
- **Ceiling：**作者自测只证明任务可执行；没有目标学习者证据不得声称学习发生或可迁移。

### G8 `interactive.domain-coverage-passed`

- **输入与动作：**从 claim/risk 生成状态空间，覆盖适用的基线、干预、边界、stress、非法、事件、重置/重放、异步、随机、降级与运动模式；不按固定样本数凑证据。
- **晋升证据：**绑定当前 model/build/delivery hash 的 gate 汇总、目标媒介中的最新最终消费者证据、最终验收、已关闭 blocker 和 known limitations；Web 查看器证据不能替代非 Web 交付证据。
- **失败返回：**直接回到首个失效的 G1–G7；保留不受影响的 authority 与已通过证据。
- **Ceiling：**`SELF_REVIEW_ONLY` 最多支持其已覆盖的技术、计算、状态或表示同源协议 claim，不能冒充独立视觉质量、领域有效性或学习者验证。

## 4. Scoped claim 与 positive pool

- `internal-positive` 与 `externally-validated` 分开；二者都只覆盖 promotion record 的 `scope / positive_for / claim_ceiling`。
- `externally-validated` 要求每条 promoted positive claim 都有角色匹配、非作者、同 build hash 的独立 coverage；部分覆盖不能外推。
- `diagnostic` 与 `negative` 通常使用 `pool: none`；局部 component/protocol 通过不能把 whole 晋升。
- release、canonical、artifact lifecycle、work status、assurance 和 pool 保持正交；Gallery 选择不创造任何事实。
- 更宽 scope、更高风险或更强现实措辞必须新建证据与 promotion record，不编辑旧记录扩张历史。

## 停止与交接

当适用的八门通过、每项 authority 与工具陈述可追踪、模型与表现层无反向依赖、最终消费者中的最终验收绑定当前 hash、claim ceiling 和已知限制明确时停止。缺少外部证据时降低 pool 或 claim，而不是虚构 pass；工具/授权不可达时交付明确 handoff，不以手写替代伪装完成。
