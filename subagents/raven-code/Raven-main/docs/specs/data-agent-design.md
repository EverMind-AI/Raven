# Raven → 强力通用 Data Agent：设计与实现方案（v1）

> 状态：讨论稿。目标：以 Raven 为 L0 执行底座，构建通用 data agent；以 DataAgentBench（DAB）为主牵引评测，**不做任何形式的 benchmark hack**。
> 依据：`/Users/admin/data-agent-research/` 全部调研（4 benchmark × 50+ 方案），Raven 架构实地梳理（2026-08-12，`feat/dba` 分支）。
> 引用格式：研究结论标 `[来源]`；Raven 代码标 `path:line`。

---

## 0. 一页摘要

1. **不自研任何主循环。** 通用 coding agent 裸跑 DAB ≈ 0.55–0.61（Pi 0.6103 / Claude Code Spider2-Lite 61.2 / smolagents KramaBench 55.83，三个独立数据点），Raven 的 AgentLoop 就是我们的 L0。全部预算投在四层数据增量上。
2. **四层增量 = L1 证据层（40% 预算）+ L2 方法论（25%）+ L3 一致性（25%）+ L4 答案契约（10%）**，对应从 0.61 补到 0.80+ 的实测路径（Pi 分桶：≈12 分 flaky 是 L3 缺失，≈11 分全灭是 L2 缺失）。
3. **交付形态：一个 bundled plugin（`raven/plugin/data_agent/`）+ 一组 skills + 若干 AgentHook + `benchmarks/dataagentbench/` 评测装置。** 核心 `raven/agent/` 不改或极少改——全部走 Raven 已有的四个扩展缝（plugin tools / skills / hooks / MCP）。
4. **红线：只做 general-purpose 能力，不做 per-dataset 规则。** 不读 validator、不碰 GT、不写数据集专属口径进 prompt、不针对 validator 窗口做排版硬编码。DAB 的 rubric 明确允许：profiling、self-critique、世界知识（`SUBMISSION_RUBRIC.md` §1）。
5. **评测纪律：先测一致性，再测正确率。** 每题 5 trial 分三桶（稳定/flaky/全灭），flaky 占比决定投 L3 还是 L1/L2；单次运行比 RUNS=3 系统性乐观 +0.08，一切消融按预注册 + ≥3 runs 执行。

预期路径（参考锚点，非承诺）：Raven 裸跑 ≈ 0.55–0.65 → Stage 1–2 后 ≈ 0.70–0.75 → Stage 3 后冲 0.78–0.82（general-purpose 组当前最高 0.7433）。

---

## 1. 目标与约束

### 1.1 目标

做**通用**的 data agent：面对任意异构数据环境（多库多方言、脏 join key、自由文本里的结构化值、领域口径），能稳定地给出正确、可审计的答案。DAB 是主牵引信号，不是目标本身。

### 1.2 为什么 DAB 是对的牵引

- 单体能力覆盖最广（26 项原子能力覆盖 46/54，四榜第一），无明显短板；
- 唯一用 5-trial Stratified Pass@1 惩罚不稳定性的榜——flaky 分桶诊断法开箱即用；
- `validate.py` 本地可自评，不用等榜单；
- 治理最严：强制交 270 条完整 trace + 维护者逐条审计 + `benchmark-informed` / `general-purpose` 分组公示。

### 1.3 不 hack 的操作性定义（红线清单）

| # | 禁止 | 依据 |
|---|---|---|
| 1 | 读取 `ground_truth.csv` / `validate.py` / 其他提交产物 / `docs/data/queries.json` | rubric §2.1；LabRat PR #54 事故（子代理 `cat validate.py`） |
| 2 | 任何外部数据获取（含本地 HF 缓存、文件系统上翻找原始数据集） | rubric §2.1；fabric-rlm PR #76 三层逃逸实录 |
| 3 | prompt 中出现 gold 值、per-dataset 规则、validator 行为描述 | rubric §2.2；SCRIBE PR #57 事故 |
| 4 | 针对 validator 实现细节做适配（如硬编码 10 字符邻近窗口、`[:200]`） | LabRat 自律边界：「rubric-legal 但 scorer-fitting」也不做 |
| 5 | 「数字霰弹枪」式答案（罗列大量候选值碰 recall-over-precision） | 维护者靠 trace 审计执法：「无推导直出 gold」记 0 |
| 6 | 把 hints 之外的题目口径解读写死进 prompt | SCRIBE「fewest-excludes-zero」被判泄漏 |

允许且应当用足的：hints（sanctioned input，rubric 明确允许复述）、profiling、self-critique 喂自己的查询结果、世界知识、通用数据工程方法论 addendum（Alkera 先例，0.8328）。

**自查机制**：提交前跑「泄漏自检」脚本——对全部 GT 值（数字+字符串）在我们所有 prompt/skill/代码里做全文搜索；逐 query diff opening prompt 与官方三件（question + description + hints）；扫 trace 里的网络/越界文件访问。这正是维护者的审计手法，先于他们做一遍。

---

## 2. 现状：Raven 有什么、缺什么

### 2.1 可直接复用的（L0 已就位）

| 能力 | 位置 | 备注 |
|---|---|---|
| Agent 主循环（40 轮迭代、流式、空响应恢复、工具失败 loop-break nudge、耗尽后 Synthesis 收尾） | `raven/agent/loop/main.py:1868` | max_iterations 可注入，数据任务需调高 |
| 工具注册/执行（超时、参数校验、`ToolResult{model_text, blocks}`） | `raven/agent/tools/registry.py`、`base.py:78` | 大结果「摘要 + 落盘路径」契约现成 |
| Plugin 扩展缝（manifest + factory 贡献 Tool） | `raven/plugin/`，样例 `raven/plugin/memory/everos/` | **数据工具的落点** |
| Skills（SKILL.md + scripts/，SkillForge RRF 检索注入） | `raven/memory_engine/skill_local/`、`tools/skill_hub.py` | **L2 方法论的落点** |
| AgentHook 五相中间件（before/after_iteration、on_tool_call…） | `raven/agent/hook/base.py` | **L3/L4 门禁的落点** |
| 沙箱（boxlite microVM，`allowNet` 域名白名单，extraVolumes 只读挂载） | `raven/sandbox/` | **默认 `none`，数据场景必须开 boxlite** |
| 子代理（spawn，独立 VM、7 工具、15 轮） | `raven/agent/subagent/manager.py` | 盲第二求解器的载体 |
| 不信任内容围栏（`wrap_untrusted` nonce 围栏） | `raven/security/trust.py` | 数据即注入面，天然契合 |
| Tracing（`audit.span.v1`、tool.call 全记录） | `raven/tracing/` | DAB 要求全量 trace，改造成导出器即可 |
| 评测装置模式（单任务 agent_cli + batch N×K 断点续跑 + evolver BenchBundle） | `benchmarks/appworld/` | **DAB harness 照此复制** |
| Evolver（诊断→设计→筛→confirm→sealed test 的 harness 自进化） | `raven/evolver/` | 我们独有的加速器，见 §9 |
| 有状态 Python REPL 工具的参考实现 | `benchmarks/appworld/tool.py` | 改造为通用 kernel 工具 |

### 2.2 缺口（就是本方案要造的）

- **没有任何数据能力**：无 SQL 工具、无有状态 Python kernel（`exec` 单发无状态）、无 profiling、无 DuckDB/pandas 依赖。
- 工具结果截断链（`_TOOL_RESULT_MAX_CHARS=16_000`、`ExecResult` 10k）对宽表输出不友好——必须用「引用传递」而非截断。
- 无验证层、无答案契约层。
- 子代理固定 7 工具、15 轮，做盲第二求解器需要允许注入自定义工具集（小改 `subagent/manager.py`，或绕过 spawn 由 harness 直接起第二个 AgentLoop）。

---

## 3. 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│ Raven L0（不改）                                                  │
│ AgentLoop · ToolRegistry · ContextEngine · Sandbox · Tracing     │
│ Spine · Session · SubagentManager                                │
└─────────────────────────────────────────────────────────────────┘
          ▲ plugin tools        ▲ skills          ▲ hooks
┌───────────────────┐ ┌──────────────────┐ ┌──────────────────────┐
│ L1 证据层           │ │ L2 知识层         │ │ L3/L4 门禁            │
│ raven/plugin/       │ │ skills/data/*    │ │ DataAgentHook        │
│ data_agent/         │ │ (SKILL.md +      │ │ · pre-emit 完备性检查  │
│ · DuckDB 统一查询平面│ │  scripts/)       │ │ · loop-breaker 加强   │
│ · 有状态 py kernel   │ │ + system         │ │ · sql 只读 AST gate   │
│   (Tier A/B 内置)    │ │   addendum       │ │ (提交门在 submit 工具) │
│ · profile/distinct/  │ │   (~10KB 通用     │ └──────────────────────┘
│   verify_join/       │ │    数据工程纪律)   │
│   search_values      │ └──────────────────┘
│ · semantic_map 批量   │
│   LLM 抽取算子        │
│ · artifact 引用传递    │
└───────────────────┘
          ▼
┌─────────────────────────────────────────────────────────────────┐
│ benchmarks/dataagentbench/                                       │
│ agent_cli.py（窄工具面组装）· batch.py（54×5 断点续跑）             │
│ 本地 validate · 三桶报告 · 泄漏自检 · trace 导出                   │
│ evolve/entry.py（BenchBundle，接 raven.evolver）                  │
└─────────────────────────────────────────────────────────────────┘
```

**关键设计决策**

| 决策 | 选择 | 理由 |
|---|---|---|
| 数据能力放哪 | bundled plugin `raven/plugin/data_agent/`（`raven-plugin.toml` 贡献 tools） | 走 sanctioned 扩展缝，核心零侵入；产品与评测同一套工具 |
| 查询平面 | **单一 DuckDB 会话**：`ATTACH` sqlite/postgres scanner，Mongo 会话初物化 TEMP 表 | 定律 2：多引擎是纯复杂度税。fabric-rlm 单工具 $0.06/trial 拿 0.6623；Permute 纯 SQL rank 2 |
| Python 执行 | 常驻 kernel 子进程（沙箱内），与 DuckDB 同进程共享连接 | 复刻 Jupyter 语义；Tier A/B 仪表化在这里注入；崩溃后显式告知「变量全没了」 |
| 工具面宽度 | 评测模式 **8 个工具**（见 §4.2），产品模式与通用工具共存 | Sentinel：工具面 5→2，验证使用率 ×250；每加一个工具都在抢注意力预算 |
| 验证 | **自动注入**为主（Tier A/B 搭车返回），主动断言为辅 | Sentinel 实测：自动注入 4036 次 vs 主动调用 1 次 |
| 多候选/盲双解 | 默认关，按风险信号开启 | ~4× 成本；fabric-rlm 盲双解 +0.076 但 3.29× token |
| LLM 自我批判 | **不做** | LabRat −0.2pp、fabric-rlm CoVe 净负、DS-GURU 迭代 5→20 掉分 |

---

## 4. L1 证据层（40% 预算，预期 +0.06~0.10）

### 4.1 统一查询平面

会话初始化（`data_agent` plugin 的 session bootstrap，或 kernel 启动脚本）：

```python
import duckdb
con = duckdb.connect()                          # 内存 hub
con.execute("INSTALL sqlite_scanner; LOAD sqlite_scanner;")
con.execute("INSTALL postgres_scanner; LOAD postgres_scanner;")
con.execute("ATTACH 'x.db' AS metadata (TYPE sqlite, READ_ONLY)")
con.execute("ATTACH 'dbname=... host=...' AS crm_support (TYPE postgres, READ_ONLY)")
# MongoDB：pymongo → Arrow → CREATE TEMP TABLE（会话开始一次性物化）
```

- 跨库 join 变成普通 SQL；模型只需一种方言、一套命名。
- 数据源配置：`plugins.config["data-agent"].sources[]`（逻辑名 → 连接参数），评测 harness 从 DAB 的 `db_config.yaml` 生成。
- 连接一律 READ_ONLY；DuckDB 的文件读取函数（`read_csv` 等指向白名单外路径）在评测模式由 SQL gate 拦截（§6.3）。

### 4.2 工具面（评测模式 8 个）

设计原则：窄面、输出带下一步动作、校验搭车返回、返回上限明确、大结果引用传递。

| 工具 | 签名（要点） | 返回 |
|---|---|---|
| `sql` | `(query)` | `{preview ≤50行, handle, stats{rows,cols,dtypes}, notes[]}`。AST 只读 gate；CTE-aware 行封顶；独立 deadline；**0 行 / 触顶 / 截断告警必须冒泡进 notes 并给建议动作** |
| `python` | `(code)` | 常驻 kernel；stdout+最后表达式；**自动追加 Tier A/B 检查**（§6.1）；`con` 变量可直接用 DuckDB；崩溃重启后显式提示变量丢失 |
| `profile` | `(table \| table.column)` | row_count / dtype / null 率 / n_distinct / 低基数 top-k 值+频次 / 数值 min-max-分位数 / **疑似哨兵值**（M、NA、-999、9.99E32、空串、`[Not Available]` 类方括号占位）/ **同列类型混杂检测** |
| `distinct_values` | `(table, col, limit=100)` | 取值分布。**优先于 sample rows**（ReFoRCE：样本行有偏；KramaBench：10→150 行分数不动、token 6.5×） |
| `verify_join` | `(left, right, on, transform?)` | 5 个 COUNT 探针 → match_rate + fan-out + 自然语言 verdict（LabRat 工具层净贡献 +5.5pp 的主力，跨库难题 +30pp） |
| `semantic_map` | `(handle, column, task, labels?/schema, model="small")` | **批量 LLM 抽取/分类算子**：对一列文本做结构化抽取或分类，批处理小模型执行，结果物化为 TEMP 表回到 SQL 平面。这是 DAB 论文点名的全场最大方法论盲区（「所有 agent 都只会用正则」→ patents 0%、agnews 全灭） |
| `get_artifact` | `(ref, offset?)` | 按需取回落盘大结果（ContextLedger 模式） |
| `submit_answer` | `(structured_result)` | 结构化提交门：raw values + provenance refs → 确定性 formatter → 触发 pre-emit 完备性检查（§7） |

产品模式下这些工具与 Raven 通用工具（文件、web、spawn 等）共存；评测模式由 `agent_cli.py` 用 `disabled_tools` 收窄（appworld 同款做法，`benchmarks/appworld/agent_cli.py:60`）。

实现要点（都来自实测教训）：

- **结果按引用传递**：每个查询结果落 `<workspace>/derived/<handle>.parquet` + `.profile.json`，context 只进 preview+stats+handle；handle 可以在后续 SQL 里当表引用（物化为 view）。这一举解决上下文遗忘、结果不可复用、无法被后续步骤 join 三个问题 [AgenticData]。
- **降载阶梯**：预览截断先无损（列裁剪、模式分组）后有损（表级/列级筛），每级带触发阈值与损失说明 [ReFoRCE L0–L5]。
- 工具输出的报错三件事：拦了什么、为什么、该改用什么（点名具体工具）[6 个方案独立发现]。

### 4.3 离线画像资产（Dataset Card）

每接入一个数据源，跑一次离线 profiling（可缓存、可增量）：

```
① 结构事实   表/列/类型/行数
② 分布事实   null 率、n_distinct、top-k 值+频次、数值分位数
③ 脏数据事实 哨兵值、类型混杂、编码不一致（★ 差异化所在）
④ 关系事实   候选外键 = 名字相似度 × 值域重叠（权重 0.35/0.65，值重叠必须更高）
             + transform 枚举（前缀剥离/大小写/分隔符）；双阈值 accept/review/reject
⑤ 结构折叠   同构分区表折叠（stockmarket 2754 张同构表 → 1 个族条目）
```

三条硬约束 [ktx / MinusX / DataBridge 血泪教训]：
1. **只写机器能证明的事实**——每条关系断言写入前真跑一次探针；
2. **LLM 说的结构性事实必须代码复验**（phantom join 比不注入更糟）；
3. **按 query 过滤注入，不全量灌**。

补充一个 DS-STAR analyzer 原语（消融 −18.26 的最大单点）：对无 schema 的对象（如 PANCANCER 的 100+ 列 clinical_info），让模型写「描述脚本」执行，stdout 即名片——描述天然覆盖取值分布，而取值恰恰是 agent 最常猜错的。

注意 LabRat 反例：机械事实地图是 provider-aware 的（Sonnet +8pp / GPT-5.6 −8.7pp）。**离线画像做成可开关组件，上线前逐 backbone 消融。**

### 4.4 上下文策略

- **递增式，不是削减式**：起点是完整但极粗的地图（源/表清单，DAB 规模 <100 项几乎不花 token），细节按需 zoom in [AutoLink：21.2K token SRR 91.2% vs 全量注入 171.9K token SRR 64%]。
- DAB 表规模小，暂不需要 HyDE 检索三件套；stockmarket（2754 表）靠结构折叠解决。schema 检索层作为后置项留给真实大仓场景（Spider 2.0 压测时再上）。
- 探索预算 ~20% tool-call 是健康区间 [DAB 论文：两个最强 baseline 都在 20%]，写进 L2 skill 而非硬编码。

---

## 5. L2 知识层（25% 预算，预期 +0.08~0.15）

最便宜的高杠杆：Alkera 用一份 9.9KB benchmark-agnostic addendum 拿 0.8328（rank 3）。

### 5.1 System addendum（~8–12KB，评测/产品共用）

按 Alkera 的八段结构写我们自己的版本（全部通用规则，逐字节稳定，进版本管理）：

```
1. 读题纪律（限定词逐个映射到数据操作；限定词不咬人启发式）
2. SHAPE & GRAIN 前置（一个值还是每组一行；哪列、什么粒度；算完用 distinct 组数对行数）
3. 列选择（code-vs-name 规则：要 code 就找 name 旁边的专用 code 列，按 name 分组会静默合并）
4. 分组/排序/并列（tie-break 必须显式；for each/per/every ⇒ 全枚举不塌缩）
5. 通用分析约定（时间序列聚合先 reindex 补零、跨币种先归一、比例题先钉分母——注意：写「怎么看」不写「看到什么」）
6. 提交前验证（cohort 门：每个显著过滤后 count+样本；换实现复算而非重跑同一条）
7. 只用提供的数据（完整性纪律）
8. 交付纪律（结构化提交，格式约束不混进推理——由 submit_answer 承接）
```

写作纪律 [ktx / DataBridge / SCRIBE]：每条规则附一句「为什么在任何数据库上都成立」；不写 ALWAYS/NEVER 大写命令式；成本焦虑不进 prompt；规则教方法不教结论（DataBridge 反例：特化最深的四个数据集通过率最低）。

### 5.2 Skills 清单（`raven/memory_engine/skills/data/*` 或 workspace skills）

| Skill | 触发 | 要点 |
|---|---|---|
| `data-investigation` | 默认 | investigation-not-pipeline 心智；先读全部文档再动手；DISTINCT 优先于 sample rows；探索预算 ~20% |
| `ambiguity-resolution` | 歧义信号 | 强制枚举 2–4 个候选读法 + confidence: committed\|split；每个歧义维度发一个只读探针取证；evidence-before-edit 四句门禁 [SOMA-SQL] |
| `dirty-data-triage` | nan/0/空结果 | 「是 coding 问题、逻辑设计问题、还是真实数据问题？」三分诊断；空结果 ≠ 错误，触发诊断查询而非直接改 |
| `text-extraction` | 自由文本抽取 | 超越正则的阶梯：`pd.to_datetime`/dateutil fuzzy → 结构化解析 → `semantic_map` 批量 LLM 抽取；子串陷阱（MALE 命中 FEMALE）；先采样格式全集再定解析策略 |
| `entity-resolution` | 疑似重复实体 | 阻断（blocking）→ 属性相似度 → 聚簇 → 聚合前先验证簇数量级；join key 规整的 transform 枚举 |
| `verification` | 提交前 | 证伪而非证实（"Probe to contradict, not to confirm"）；只修 FAIL、PASS 不翻案、全 PASS 即停 |
| `format-registry` | 非常规格式 | xlsx 多 sheet/header 探测、嵌套 JSON-string 字段、字符串化 dict、en-dash 等编码陷阱 |

挂载纪律 [fabric-rlm：预加载正文 +124% 成本零收益]：SkillForge 已有的「索引常驻 + 命中才读正文」正好匹配；评测模式为保证 270 trial 逐字节可复现，改为**确定性关键词路由**（不走 LLM gate），并在 harness 里固化 skill 版本。

### 5.3 领域知识的沉淀通道（产品向，learn→distill→infer）

NVIDIA KGMON 范式（DABstep hard 89.95 用 Haiku 级小模型）：每接入一个新数据域，用强模型跑一次探索，把发现的规则蒸馏成**带单测的 helper 函数库**（`sys.path` import，只有签名占 context）+ 解法册。这就是 data agent 的一次性「上手成本」。落点：skill 的 `scripts/` 目录（`use_skill` 已支持物化 scripts 给 `exec`，`tools/skill_hub.py:176`）。**注意：在 DAB 上不做这件事**——对 12 个已知数据集做 distill 就是 per-dataset 规则，越线。这是产品/真实数仓专用通道。

---

## 6. L3 一致性层（25% 预算，预期 +0.08~0.12）

治 flaky。Pi 的教训：与榜首能力上限只差 11pp，实际分差 23.5pp——一半差距是「做得不稳」。

### 6.1 Tier A/B：kernel 内自动注入（不给关闭权限）

在 `python` 工具的 kernel 里做代码仪表化（Sentinel 骨架，修掉它的重复求值缺陷）：

- **Tier A `[AUTO-INSPECT]`**：cell 最后表达式若为 DataFrame/Series 且用户没自己打印，自动追加 shape/dtypes/head(3)。**缓存 exec 阶段的值，不重新 eval**（Sentinel 的已知副作用：有副作用的表达式被执行两遍）。
- **Tier B `[VERIFY: PASS|WARN|FAIL]`**：正则识别 groupby/merge/boolean-mask，注入后置断言——groupby 不增行、join 膨胀 ≤2× 最大输入、filter 后非全空。全部 try/except，绝不打断用户代码。**PASS 也带观测值**。
- **SQL 侧等价物**：`sql` 工具对 0 行、行封顶命中、疑似截断、`-- Omit` 偷懒注释做规则检查，结果进 notes。
- **kernel 硬纪律**：`stop_reason=="length"` 时整批拒绝执行 tool call（截断的 SQL 往往仍是合法 SQL，会静默出错）；禁止生成代码里 `try/except` 吞错（fail-fast，否则 debug 循环失效）；debug 反馈里附 schema 而不只 traceback。

### 6.2 工具级熔断与 loop-breaker

- Raven 已有 `_loop_break_nudge`（`main.py:243`）。加强判据：代码哈希 + 异常类型 + 异常消息**三者连续 N 轮全同**才触发（消息不同 = 在推进）[APEX/LabRat/fabric-rlm 三家独立实现]。
- 工具级熔断：连续 N 次环境级失败就摘掉该工具并告知模型 [Permute 的 python 工具 93 次调用 100% 失败无人察觉]。落点：`DataAgentHook.on_tool_call` 统计 + 注入。

### 6.3 SQL 只读 AST gate

sqlglot 解析 → 节点类型白名单（fail-closed：解析失败即拒）；词法快 guard 先行（宁可误拒）；语句堆叠检测先剥注释和字符串；行封顶 CTE-aware；独立执行 deadline 且 kernel 可 SIGKILL [DataBridge/ktx/SignalPilot 一致：**Safety is deterministic, not instructional**]。评测模式额外拦 DuckDB 的文件系统读函数指向白名单外路径。

### 6.4 盲第二求解器（按风险开启）

- 触发信号：结果为空/涉及去重与时间窗/多解读 split 未收敛/用户要求高置信度。
- 实现：起一个全新上下文的求解（不继承任何中间状态），只给原题 + description + hints。**判等用代码不用 LLM**（数值容差、集合比对）；不一致才进 reconcile，reconcile 是「定位分歧点后重推」不是投票取平均 [fabric-rlm +0.076；Sarvam 0.8208 的核心机制]。
- 载体：现有 `spawn` 子代理工具面（7 工具）不含数据工具，需二选一：(a) `SubagentManager` 支持注入自定义工具集（小核心改动，一次性）；(b) 评测 harness 直接起第二个 AgentLoop。**v1 选 (b)**，零核心改动；产品化时再做 (a)。
- 关键：「协议不强制 = 协议不存在」[Sarvam：同一份 SKILL.md，harness 强制后触发率 12.6%→99.3%，+3.97pp]。盲双解由 harness/hook 强制触发，不靠模型自觉。

### 6.5 明确不做

LLM-as-judge 自评、K-of-N consensus、CoVe、convention pinning、把迭代次数加大当修复。全部有多方案负收益实测（§3 决策表）。唯一允许的 LLM 判断是**可判定问题**：「信息够不够」（DS-STAR 充分性 verifier，8B 级即可）——留作 v2 可选组件。

---

## 7. L4 答案层（10% 预算，预期 +0.01~0.03，不做白丢分）

格式约束混进推理 prompt 会降低推理能力 [DABstep 论文引 Tam et al.]。所以：

1. **推理只产出结构化 raw result**：`submit_answer(values=[{name, value, unit?, precision?}], grain, provenance=[handle/sql refs], assumptions=[])`。
2. **确定性 formatter** 负责渲染：key-value 紧邻、集合全枚举不截断、原生精度不重舍入、实体名逐字符复制。这是通用好实践（任何下游消费者都受益），**但不硬编码任何 validator 窗口参数**。
3. **pre-emit 完备性检查**（hook 在 `submit_answer` 上强制）：
   - 计划阶段钉死的输出列清单逐项 ✓；
   - 每个请求的指标/实体标识符/派生值输入 ✓；
   - 行数 vs distinct 组数对账；
   - 集合题：宣称的 count 与枚举条数一致；
   - 缺项 → 打回一轮 finalization，不静默提交。
4. **拒绝没有 provenance ref 的数字** [Permute EQ：消灭「报告数字与查询结果不一致」这类最难发现的错误]。
5. 空答案兜底：turn 耗尽时 Raven 已有 Synthesis 收尾；harness 层再加 marker 校验 + 一轮补交（Gemini-2.5-Flash 0.1041 的真相是 59.9% 的 run 没交出答案——协议错误不是能力错误）。

---

## 8. 评测装置：AgentEval 主跑道 + 仓内轻量调试器

**决策（2026-08-12）：不自建 batch/scoring/沙箱基础设施，用团队的 AgentEval（github.com/daoxize1/AgentEval）作为记分跑道。** 它的 DAB 集成已具备本方案 §8 原稿要求的全部要点，且隔离质量高于我们在 macOS 宿主机上能自建的水平：

- **泄漏安全是架构性的**：生成沙箱只挂载 sanctioned stores（只读），`ground_truth.csv`/`validate.py` 物理留在宿主机；生成与判分两阶段分离（`run` → `evaluate`）；Docker 网络可隔离。
- pg/mongo **快照灌库**（每数据集一次，不是每 attempt 重灌）；缺快照在规划期拒绝。
- 5 attempts × 断点续跑 × 并发调度 × 官方 `validate.py` 判分 × `use_hints` 开关入报告。
- **Raven harness 现成**（从 `source_dir` 构建 runtime——我们的 bundled plugin 随源码自动带上），且原生支持 deepseek 协议。

需要我们补的三块（改 AgentEval，均为小改动）：

1. **Raven 配置透传**：`build_raven_config` 目前不透传 `plugins`/`tools.disabledTools`/自定义 system prompt，加一个 `raven_config_extra` deep-merge 字段——这是表达「窄工具面 + data_agent 插件配置 + 静态 addendum」的通道。
2. **DAB 任务镜像扩依赖**：runtime Dockerfile 加 duckdb/pymongo/psycopg2（kernel 子进程用任务镜像的 python，不污染 Raven runtime 依赖）。
3. **三桶报告**：在 `aggregate.py` 的 per-attempt 记录上加 flaky 分桶（5/5 稳定 | flaky | 0/5）与 per-dataset 明细、行为 KPI（校验查询占比、探索占比、DB:Python 下推比）。

Raven 仓内只保留一个**轻量调试 runner**（`benchmarks/dataagentbench/debug_run.py`）：单 query 直驱 AgentLoop、本地起库，用于开发数据工具时的快速迭代；**不用于任何上报数字**。evolver 接入（§9）通过把 AgentEval CLI 包成 `EvalBackend` 实现。

要点（不变）：
- **行为 KPI 随分数一起出**：校验查询占比（健康区 ~40%+ [Alkera]）、探索 tool-call 占比（~20%）、DB:Python 下推比、每题成本。分数涨了但 KPI 崩了 = 在走歪路。
- **消融纪律**：预注册（baseline/改动/endpoint/决策规则写死再跑）；≥3 runs；最终配置整体再测一次（组件间有交互效应 [LabRat]）。
- **辅助回归**：DABstep 450 题当本地秒级冒烟（只看掉分不看涨分）；KramaBench 每 milestone 一跑（正交能力：数据湖发现 + pipeline 设计）；上线前做 obscured 自测（改列名/数值/行序，掉分多 = 在背答案 [KramaBench：SOTA 62.81→13.94]）。
- **hints 政策**：用（sanctioned），PR 里如实标注 hints=Yes。

## 9. 与 Evolver 的结合（Raven 的独特优势，谨慎使用）

Raven 自带 benchmark 驱动的 harness 自进化（诊断→设计→K=1 筛→K=3 confirm→sealed test），这是其他 DAB 参赛者都没有的基础设施。用法：

- 实现 `evolve/entry.py` 的 `BenchBundle`（参考 `benchmarks/appworld/evolve/entry.py`，契约见 `docs/specs/evolve-bench-contract.md`）。
- **train/test 切分**：54 题按 dataset 分层切（如 8 数据集 train / 4 数据集 sealed test），sealed 集物理隔离到 finalize 才解封——evolver 已内置 `assert_no_test_leak`。
- **红线延伸**：evolver 产出的候选 patch 也要过泄漏自检（leak_check 进 evolver 的 precheck）；只接受通用性改动（改 addendum 措辞、调工具输出格式、改验证阈值），**拒绝任何 per-dataset 条件分支**——在 verdict prompt 里写明并人工抽查 promoted commits。
- 定位：Stage 3 之后的加速器，不是 Stage 1 的主力（先把确定性的架构收益拿完，再让 evolver 打磨长尾）。

---

## 10. 实施路线

### Stage 0 — 基线与装置（约 1 周）

1. `benchmarks/dataagentbench/` 全套（setup/agent_cli/batch/scoring/report/leak_check）；docker compose 起 pg+mongo；数据下载校验。
2. **基线 A（最小对照组）**：AgentLoop + 仅一个 DuckDB 统一平面 + `python` kernel（无验证层无 addendum）。锚点：fabric-rlm 同配置 ≈ 0.66（$0.06/trial）。
3. **基线 B**：Raven 现状裸跑（exec + 文件工具）。
4. 产出：我们自己的三桶分布 + per-dataset 明细。**这一步的数字决定 Stage 1–3 的预算再分配**（诊断树：flaky>15% → 加码 L3；全灭题是方法论缺失 → L2；找错表/猜错值 → L1）。

验收：270 trial 可断点续跑、trace 完整、泄漏自检 0 告警、拿到基线 Pass@1 与三桶。

### Stage 1 — 证据层 + 工具面（2–3 周，预期最大收益）

1. `raven/plugin/data_agent/` 插件骨架 + 8 工具（§4.2）；
2. DuckDB 统一平面 + Mongo 物化 + READ_ONLY + AST gate；
3. kernel（有状态、沙箱内、崩溃恢复提示）；
4. profile/distinct_values/verify_join/search_values + 离线 Dataset Card；
5. 结果引用传递（derived/*.parquet + handle 回注 SQL 平面）；
6. `semantic_map` 批量抽取算子 v1（小模型批处理 + 物化 TEMP 表）。

验收：消融报告（每个组件单独开关 ×3 runs）；DB:Python 下推比、探索占比进入健康区。

### Stage 2 — 一致性层（2 周）

Tier A/B 自动注入（不给关闭）、SQL notes 搭车告警、工具熔断、loop-breaker 加强、`stop_reason==length` 整批拒绝。
验收：flaky 桶显著收窄（目标 <10%）；`[VERIFY]` 触发统计进 report。

### Stage 3 — 口径与契约（2 周）

addendum v1 + skills 上线（确定性路由）；ambiguity 探针取证流程；`submit_answer` 结构化提交门 + pre-emit 检查 + 确定性 formatter + provenance 强制。
验收：0/5 全灭桶中「口径/格式」类归零；报告新增 assumptions 审计。

### Stage 4 — 加速与外扩（持续）

盲第二求解器（风险触发）；DS-STAR 充分性 verifier（可选消融）；evolver 接入（§9）；KramaBench/Spider2 外扩压测；跨会话口径记忆（decisions.jsonl → EverOS memory backend）；产品向 learn→distill→infer 通道。

每阶段结束：跑 DAB 全量（≥3 runs 口径）+ DABstep 冒烟 + 泄漏自检，写预注册消融报告。

---

## 11. 决策记录（2026-08-12 与 Ethan 确认）

1. **模型选型：已定。** 起步 backbone = `deepseek-v4-flash`（DeepSeek 官方 API，OpenAI 兼容，base `https://api.deepseek.com/v1`；模型 ID 已实测确认）。Key 走环境变量 / 本地 `~/.raven/config.json`，**绝不入库**（benchmarks README 惯例）。两条附注：
   - flash 级模型跑基线便宜、迭代快，但研究结论明确「弱模型下脚手架收益小、强模型下收益大」（Opus 4.6 +0.064 vs GLM-5.2 +0.207）——**每个 Stage 的消融结论在 flash 上得出后，milestone 节点需在一个强模型上复测一次**，防止把 provider-aware 效应（LabRat：同一 grounding 层 Sonnet +8pp / GPT-5.6 −8.7pp）误当通用收益。
   - refusal fallback 链后置（DeepSeek 在 PANCANCER 类临床题上的拒答率待观察，Stage 0 会有数据）。
2. **盲双解载体：v1 用 harness 直起第二个 AgentLoop（方案 b）**，产品化阶段再评估给 `SubagentManager` 加工具集注入（方案 a）。详细论证见本次讨论记录。
3. **`semantic_map` 成本护栏：** 行数硬顶 + 调用前成本预估回显 + 批量打包（每请求 ~100 行）+ 「先过滤再语义」纪律 + 置信度分层复核。默认小模型即 `deepseek-v4-flash` 本身。agnews q3（零净误差）明确接受为「诚实做不出」，不为它烧预算。
4. **skills 注入：已定，评测模式静态注入**（270 trial prompt 逐字节稳定），产品模式走 SkillForge 检索。
5. **投榜：暂不投。** Tuned prompt 政策：见 §11.1。

### 11.1 Tuned prompt（benchmark-informed）政策

DAB 官方定义：*"Tuned prompt ✓ = the up-front prompt is DAB-specific, built from a close study of DAB's task conventions."* 这是**可比性分组，不是违规标记**——与泄漏红线（§1.3）是两回事。

判定实况（从 33 条榜单 note 反推）：维护者的归组非常保守——Alkera 的 addendum 明明是 benchmark-agnostic（不含任何 per-dataset 规则，决定性口径只是复述官方 hints），仍被归入 benchmark-informed；Sentinel/Permute 因为「有针对本 benchmark 建的断言层/证据层」也被归入该组。

**我们的立场：**
- 泄漏红线（§1.3 六条）绝对不碰——这是「作弊与否」的边界。
- 我们的 addendum 只写跨 benchmark 通用的数据工程纪律（内容来源是 4 榜 50+ 方案的共性结论，不是对 DAB 54 题的逆向），且不复述 hints 之外的任何 DAB 口径。**但按维护者的保守标准，若投榜大概率仍会被归 benchmark-informed——接受这一点，不为分组标签牺牲能力。**
- 为保持对 general-purpose 组（最高 0.7433）的可比性，**内部评测始终报两条曲线**：bare 配置（无 addendum、无 skills，只有工具面）和 full 配置。addendum 作为常驻消融开关，两条曲线的差值就是「方法论层」的真实贡献，也顺带回答「我们到底算不算 tuned」。

---

## 附 B：raven-x 分支的可复用性评估（2026-08-13 调研）

`ravenx/raven-x`（github.com/TongLi31/Raven，EverMind 的 coding/data-agent 硬化分支，较 main 领先 85 commit）是一批已落地的 L0 加固。`main...raven-x` 的 513 文件 diff 大头是 main 尚未合并的上游功能（evolver/tracing/importer），真正的**硬化子集约 30 commit**，集中在 `agent/loop/`、`providers/`、`context_engine/`、`agent/tools/`。分支根的 `RAVEN-X-CHANGES.md` 是作者自己的写法说明（含对抗审计与已知缺陷），纳入前必读。

**清晰有益，建议吸收（L0 白嫖，与定律 1 一致）：**
- agent-loop 韧性：溢出/空响应/中途 kill 恢复（`326adcd`）、同调用熔断 + 历史配对修复 + 回合内 checkpoint（`19f37c4`）、in-turn compaction（`34fb2fe`）——data agent 的长回合（迭代 SQL、多源 profiling）最吃这个。
- provider 加固：重试/错误分类/backend probe（`b46f06d`）、网关 prompt-cache（`a95e996`）、litellm 统一 + 修 7 个凭据串号（`16bdca7`）。
- **拒绝不可用 tool-call 参数**（`7323653`）——静默执行被截断的 SQL 是 data agent 头号 footgun，必须 adopt（注意作者自陈缺陷：预算翻倍上限是 no-op、trigger 计数不可观测）。
- session 移出 workspace（`8f1d6c6`）——别在用户数据目录里留 session 垃圾。
- reasoning_content 回传（`9093cfd`/`a7d5674`）——**已落到 feat/dba 工作区**。
- 可执行 tool 报错 + 名字修复（`396fe3a`）、后台任务/exec 输出落盘（`40b1b83`）、选择性 trust 围栏（`e0d372a`）、PYTHONPATH scrub（`1140b19`，作者标记 P0、main 也中招）、todowrite（`19e34eb`）。

**opinionated，不要盲抄：**
- **移除 curator**（`123d666`）——为 bounded 单任务编码回合优化；长会话多源 data agent 可能反而需要 LLM 驱动的历史选择/检索。按我们的会话长度模型再决定，别直接删。
- **coding-only 身份 + SE 纪律 prompt**（`34fb2fe`/`377aafd`）——按 SWE-bench 失败统计调过（作者承认循环论证，`render.py:59-62`），还顺手删了 `message` 工具和 assistant profile。复用 per-model-family 机制（`e555949`），但 data agent 要写自己的 system-prompt profile，不继承编码纪律。
- web 工具默认关 + 去 cron（`30e406d`）——保留 config 开关，重新考虑默认值。
- 完成门内容（`4286cd0` 等）——框架（事件门 + 一次性提醒 + clean-state 验证 + trigger 计数）可复用，但 pytest/tox 识别是编码专属，data agent 的等价物是"查询真跑了没 / 对着库验证了没"。

**结论**：raven-x 的 provider + loop 韧性子集正是我们 L0 想要的加固，应有选择地并入 feat/dba；但它的 context/prompt/工具面朝 coding agent 收敛，与通用 data agent 方向部分冲突，**不整支合并**。Stage 1 开始前单立一个"L0 加固吸收"任务按清单 cherry-pick。

## 附 A：证据速查（写方案时反复引用的关键数字）

| 事实 | 数值 | 来源 |
|---|---|---|
| 通用 coding agent 裸跑 DAB | 0.6103（Pi + Opus 4.6） | DAB 榜 |
| 固定强模型换脚手架的可迁移增益 | +0.13~+0.21 | 受控对照 |
| 自动注入 vs 主动调用验证 | 4036 : 1 | Sentinel |
| 工具面 5→2 后验证使用率 | ×250 | Sentinel |
| 盲双解 | +0.076 @ 3.29× token | fabric-rlm 预注册 A/B |
| LLM-as-judge 自评 | −0.2pp / 净负 / 掉分 | LabRat / fabric-rlm / DS-GURU |
| 双语生成（SQL+Python） | +11.85 | FlexSQL 消融 |
| 执行反馈迭代 | +7.87 EX | AutoLink |
| 校验查询健康占比 | 42–44% | Alkera |
| 探索健康占比 | ~20% tool-call | DAB 论文 |
| SQL 下推 vs 拉回 Python | 19× 成本差、分数还低 7pp | DAB 论文 |
| 单次运行 vs RUNS=3 乐观偏差 | +0.08 | fabric-rlm |
| 最便宜可用基线 | $0.06/trial → 0.6623 | fabric-rlm |
| general-purpose 组当前最高 | 0.7433 | DAB 榜 |
