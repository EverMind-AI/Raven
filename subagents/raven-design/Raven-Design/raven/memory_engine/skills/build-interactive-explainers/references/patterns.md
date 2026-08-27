# 交互解释器领域模式与检查

只在建立交互解释器的模型、状态、表示或学习证据时读取。本参考扩展主 Skill 的领域 gate，不拥有通用渲染、review、promotion、release 或 Gallery 语义。

## 1. 路由卡与 claim 计划

先完成一张紧凑路由卡：

```text
primary_type:     explainer | calculator | simulation | game-route-out
audience:         使用者及其先备知识
question:         唯一主问题
intervention:     用户可改变的输入、步骤或事件
model_action:     模型实际执行的规则
observable:       可复查的过程或结果证据
learning_claim:   none 或要解释/预测/迁移的关系
real_world_claim: none 或明确、受限的现实用途
consumers:        最终使用环境
allowed_claims:   本轮可证明的措辞
forbidden_claims: 容易被误读而明确禁止的措辞
```

混合产物按主要价值分路，而不是按控件数量：

- 结果查算为主且过程可省略：calculator。
- 规律理解为主，干预后直接或分步解释：explainer。
- 状态随规则、时间、事件、随机或 solver 演化，路径影响结论：simulation。
- 分数、胜负、速度、解锁或技巧表现成为目标：game-route-out。

每条 claim 单独记录 `claim_id / claim_type / scope / risk / required_gate / evidence / external_role / ceiling`。`claim_type` 至少区分 model validity、computation、state protocol、representation、learning task 与 real-world use；一类证据不能替另一类。

## 2. Reference model contract

```text
model_id/version:       稳定身份与版本
question_supported:     能回答的问题
sources:                权威来源、数据或推导
equations_or_rules:     方程、状态转移、算法或约束
variables_and_units:    定义、类型与单位
initial_and_boundary:   初值、边界、拓扑或先验
assumptions:            简化、独立性、均匀性、忽略项
legal_domain:           数学/类型上可计算范围
trusted_domain:         来源与假设有依据的范围
teaching_domain:        为当前任务选取的可辨认子集
solver_or_transition:   解析、离散、采样、优化或求解配置
outputs:                直接量、派生量、事件与不确定性
limitations:            不能回答的问题与现实决策边界
risk:                   错误影响 × 复杂度 × 数值敏感性 × 外推风险
```

规则：

- 教学域可以比可信域窄，不能越过可信域制造戏剧性反馈。
- 默认值、预设和阈值必须来自合同或明确标为教学选择，不能凭画面需要设定。
- 单位、边界、分支、无解、非有限值和耦合约束必须先于 UI 提交验证。
- 模型改版会使旧 vectors、gate、review 与 promotion 对新 build 失效；不得静默继承。

## 3. 按 claim/risk 选择 oracle

| 风险 | 典型范围 | 最低充分的 computation evidence |
| --- | --- | --- |
| 低 | 可直接推导的闭式关系、有限透明规则 | 独立手算点或真值表、边界、反例、不变量 |
| 中 | 数值迭代、事件动力学、非平凡几何或一般算法 | 不同公式/离散路径的独立实现、reference vectors、误差与事件测试 |
| 高 | 随机、优化、敏感数值、复杂空间或高影响现实 claim | 成熟独立求解器/权威 benchmark，加收敛、残差、统计、不确定性及适当领域 coverage |

求解顺序：

1. 解析解、精确递推、有限枚举或守恒不变量可得时优先使用；不要为了“像仿真”引入积分误差。
2. 必须数值求解时，从模型刚性、事件、精度、规模和部署约束选择成熟 solver。
3. 将容差绑定到 claim、量纲、尺度、条件数、solver 和用户可见决策；禁止全领域固定常数。
4. 数值结果至少检查适用的步长/网格收敛、残差、守恒、事件时刻和长期行为。
5. 无精确真值时组合性质、区间、交叉 solver 与 benchmark；不得虚构 golden answer。

独立性检查：

- Reference model 是规则权威，production 是被测实现，oracle 是独立核对路径；三者不是同义词。
- Oracle 可以读取同一输入合同和模型版本，但不得导入 production 的核心 `compute/simulate`、读取其输出作为期望值或共享同一算法循环自证。
- 若符号工具生成 production 代码，它不能同时成为唯一 oracle；增加手算、不同实现或独立 solver。
- Reference vector 记录输入、模型/oracle 版本、期望或区间、误差、容差来源及适用的步长、网格、样本量或终止条件。

## 4. Authority concern 模式

按任务从以下 concern 中选择适用项，每项恰好一个 owner：

| Concern | 可接受的 authoritative owner | 常见 derived output |
| --- | --- | --- |
| `reference_model` | 版本化模型规范、原生方程/规则 bundle | 页面公式、参数说明 |
| `production_computation` | 生产引擎源码、原生模型或可执行 bundle | trajectory、result table |
| `oracle` | 独立脚本、solver model 或 benchmark bundle | vectors、误差报告 |
| `experiment_state` | 状态 schema、事件协议或场景文件 | UI 控件值、时间游标 |
| `representation_mapping` | 数据绑定/可视化 pipeline 配置 | 图形、公式代入、文本快照 |
| `learning_task` | 任务脚本、题目与判据 | 页面支架、提示 |
| `delivery` | 最终 consumer 的可编辑源 | 构建产物、截图、Viewer 缓存 |

若原生科学母版经导出进入 Web，科学母版继续拥有模型 concern，Web 源只拥有交付或表示 concern。不得编辑导出数据、截图或 Viewer 缓存后反称上游模型已修改。

## 5. 最小状态与重放

共同单向链为：

```text
draftInput → validateAndCommit → committedScenario → compute → authoritativeResult → viewModel
```

| 主型/子型 | 必需状态 | 通常不应出现 |
| --- | --- | --- |
| calculator | draft、committed input、result、error、view | trajectory、simulation time、playback、event log |
| 闭式 explainer | scenario、baseline/compare、result、selection、view | solver state、伪时间线 |
| 步骤/算法 explainer | input、rule version、step snapshots、step index | 连续墙钟；除非仅用于播放步骤 |
| 时间 simulation | model spec、scenario、ordered events、solver config、trajectory、simulation time、playback、view | 与模型无关的随机/优化状态 |
| 随机 simulation | model、seed、realization、batch、summary、selection | 将单次轨迹当总体规律 |
| 优化/约束 | problem、candidate、solver status、feasibility、result、residual/termination | 无意义 playback；除非解释迭代过程 |

状态规则：

- `viewState` 只能改变选择、缩放、相机或显示方式，不能改变模型结果。
- 墙钟、`requestAnimationFrame` 和 playback rate 只能移动结果游标，不能成为 solver 或改变轨迹。
- 时间事件使用稳定排序键，明确同时间戳顺序；恢复事件要追加语义，不删除历史后伪装未发生。
- 异步计算携带 revision/cancel；只有最新完整结果原子提交，旧、部分或失败结果保留在证据中但不能覆盖当前结果。
- 重置必须恢复参数、事件、种子、游标、错误和播放；同一序列化规格重放应得到同一结果或声明的统计等价。

最小 replay spec 按适用项保存：

```text
model id/version; committed parameters; initial/boundary state; ordered events;
solver and numerical configuration; sample grid/duration; seed; selected result/cursor;
production tool/version; build hash
```

## 6. 表示忠实矩阵

| 模型输出 | 允许的表示 | 禁止的外推 |
| --- | --- | --- |
| 聚合或 well-mixed 标量 | 存量/流量、边界交换、总量、表格、非空间曲线 | 局部箭头、梯度、热点、粒子密度、轨迹或局部因果 |
| 分区/网络 | 已定义节点、边、方向和分区差异 | 用连续渐变填补不存在的空间分辨率 |
| 坐标场/PDE/观测场 | 带坐标、网格/采样、方向、梯度、切片、等值面或流线 | 隐藏边界、分辨率、插值或不确定性后声称连续真值 |
| 随机结果 | 单次 realization 与分布/区间分开 | 用一条漂亮轨迹代表概率规律 |
| 优化/约束 | 候选、可行域、目标、约束余量、残差和终止 | 只显示“最佳”数字而隐藏可行性与条件 |
| 步骤/算法 | before、action、delta、why、分支与不变量 | 只移动高亮或播放预制帧 |

为每个可见通道维护 representation map：`element → source field/snapshot → transform → units/scale → precision/interpolation → uncertainty → fallback`。

检查：

- 图、公式、读数、表格、状态标签和解释句必须从同一 snapshot 读取；差异只能来自声明的显示精度。
- 曲线必须绑定真实有序样本，显示定义域、单位、采样与插值；平滑不得制造新极值、相位、事件或精度。
- 动画插值只改变呈现；事件、阈值、读数和解释从权威结果取得。
- 装饰运动若可能被理解为数据必须删除；确需非数据插图时明确标识且不承担证据。
- reduced-motion、静态、文本或表格 fallback 必须保留同一因果关系和任务结果，不得只放占位首帧。

## 7. 学习任务模式

学习 gate 只在存在学习 claim 时适用；calculator 可在路由卡证明无学习 claim 后标记 `not_applicable`。

| 子型 | 最小任务闭环 | 复核 |
| --- | --- | --- |
| 因果/参数 | 预测方向或结果 → 冻结必要条件 → 干预 → 比较过程与结果 → 解释 | 新条件、边界或反例 |
| 步骤/算法 | 预判下一步 → 应用真实规则 → 查看 before/delta/why | 换输入重放并可回退 |
| 随机 | 区分单次与分布假设 → 固定种子运行 → 批次汇总 | 同种子重放与新种子重采样 |
| 优化/约束 | 提出候选 → 求解 → 查看可行性、目标与残差 | 扰动约束或与基准解比较 |
| 空间 | 设置边界/源项 → 观察真实场或网络 → 查守恒、尺度与分辨率 | 改边界、网格或采样复核 |

任务 trace 至少能回答：学习者做了什么；模型执行了什么；哪项证据变化；为什么能支持该解释；换条件后是否仍成立。自由探索、动画播放、按钮可点或作者能解释，都不能单独证明闭环。

证据 ceiling：

- 自动化和作者自测：任务路径可执行、状态/结果一致。
- 独立领域 reviewer：仅覆盖其审查的模型或表示 claim。
- 目标学习者形成性观察：可覆盖可发现性、误解和任务完成，不等同学习效果。
- 对齐目标的研究设计：才可能支持学习或迁移效果；样本和阈值由研究问题决定，不写成领域常数。

## 8. Claim/risk 状态空间

从 claim 反推覆盖，不按固定截图数：

```text
reference/baseline; each declared intervention; trusted-domain boundaries;
stress outside teaching domain; invalid/coupled input; branch/no-solution;
event start/middle/end and same-time ordering when applicable;
reset and serialized replay; asynchronous stale/fail when applicable;
seed replay and distribution checks when applicable;
primary, reduced-motion and causal-equivalent fallback representations
```

每个状态记录触发、期望、实际结果、gate、consumer evidence 与 build hash。覆盖缺失就是 `unknown`，不能写 `not_applicable`；只有路由卡和模型合同证明该维度不存在时才可 `not_applicable`。

## 9. 失效定位与返回

| 失效 | 首个返回位置 |
| --- | --- |
| explainer/calculator/simulation/game 混淆或现实措辞越权 | G1 claim/route |
| 来源、单位、假设、域或规则不能支撑问题 | G2 reference model |
| 工具只被提及、母版缺失、owner 冲突或 availability 不明 | G3 toolchain/authority |
| production 与 oracle 不一致，或仅“趋势看起来对” | G4 computation/oracle |
| 参数提交、事件排序、重置、seed、revision 或 replay 不确定 | G5 state/replay |
| 多重表示不同源，聚合模型伪装空间场，fallback 改变结论 | G6 representation |
| 只有控件/自由探索，没有解释和新条件复核 | G7 learning task |
| 当前 build 的边界、错误或关键状态缺证据 | G8 domain coverage |
| 外部 coverage 缺失或 reviewer role 不匹配 | 降低 pool/claim，不修改已通过 artifact gate |

只修复首个失效假设，保留不受影响的原生母版和证据。若失败来自治理标签、canonical 或 Viewer，返回其 owner，不能通过改产物来“修好”标签。
