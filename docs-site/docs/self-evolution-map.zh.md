# 自进化映射

**配套文档：** 权威的方法论规格是 `docs/specs/self-evolution-loop-sop.md`，
与代码一同保留在仓库内，未在本站发布。本文回答的是：Raven 中哪些代码实现了 SOP 的每一条款、
哪些偏离是有意为之，以及哪些部分已存在但尚未接线。

**使用方式：** 在改动 `evolver/**` 或 `benchmarks/appworld/evolve/**` 之前，
先在此处定位对应的 SOP 条款；若你的改动改变了某条对应关系，
请在同一个 PR 中更新本文。

## 0. 根本性的架构差异（请先读） { #0-the-fundamental-architectural-difference-read-first }

SOP 的循环（§3 / §8.3）是 **Claude 驱动**的：没有驱动程序——由人打开 Claude，
手工走完七步漏斗，状态持久化在三层文件中，各个组件是一组 CLI 脚本。
Raven 以**程序驱动**的循环实现同一套方法论：
`evolver/orchestrator/loop.py::EvolutionOrchestrator` 是漏斗的确定性驱动器，
SOP 中的 CLI 组件则变成了嵌入循环内的函数。

SOP §8.0 的分工（语义交给模型、确定性算术交给代码）原样保留：
诊断 / 设计 / 判定仍然是 LLM 调用，封装在
`orchestrator/nodes/semantic.py::SemanticNode` 中（解析失败会回灌给模型，做有界的修复重试）。
改变的只是「由谁按下一步的按钮」。

SOP 自己（§8.3）对这条路线的评价是：「打包成一键工具属于大型基准的任务」。
Raven 选择现在就把集成做出来，换取跨窗口交接的自由与方法论的机械化
（见 §5「超出 SOP 的部分」）。

## 1. SOP §0 通用规则 → 实现 { #1-sop-0-general-rules-implementation }

| SOP 条款 | Raven 实现 | 证据（文件 :: 符号） |
|---|---|---|
| Gate0 运行前环境健康检查 | 每轮注入 precheck，冷启动前强制执行一次 | `orchestrator/production.py::build_evolution_orchestrator`（`run_gate0`）；`benchmarks/appworld/evolve/precheck.py::make_appworld_precheck` |
| 基础设施失败阶梯：检测 → 重跑 ≤2 次 → 仍失败则计 0 分 | `eval_with_infra_rerun`，`max_reruns=2`，生成 `_infra_rerun{1,2}` 目录阶梯；KEPT 规则取 infra trial 次数最少的那次测量 | `orchestrator/scoring.py::eval_with_infra_rerun`；`benchmarks/appworld/evolve/adapter.py::ladder_out_dirs / read_kept_out_dir` |
| ★ 分母 = 任务总数；infra 任务绝不剔除，就地计 0 分 | Gate-f 只报告污染清单，绝不缩小分母 | `orchestrator/gates/pipeline.py::run_gates`（注释直接引用 SOP §0） |
| 分工：语义归模型，确定性归代码 | 见上文 §0 | `orchestrator/nodes/semantic.py::SemanticNode` |
| 两种判定：导航用 = K3 均值 > vanilla（入库）；计入战绩 = 配对 2σ（论文口径） | `PairedResult` 将二者保留为两个独立字段：`promoted`（导航）与 `credited_2sigma`（标签）；晋级只读取前者 | `orchestrator/gates/paired.py::PairedResult / paired_lift` |
| 配对 σ：逐任务配对差值的标准差，消除任务间难度差异 | `d_i = rate_c,i − rate_v,i`，`se = stdev(d)/√n`，`z = lift/se` | `orchestrator/gates/paired.py::paired_lift` |
| ★ 密封测试：每轮盲跑，对决策不可见，结束时解封用于留存评估 | `SealedTestRunner.score` 写入驱动器看不到的目录并**返回 None**（任何 test 数字在物理上都无法进入决策路径）；`unseal` 只在循环结束后执行 | `orchestrator/sealed/runner.py::SealedTestRunner / unseal_retention` |
| test 集绝不进入 anchor/train | 比 SOP 的纪律更强：一条机械化断言——发生泄漏时在启动阶段直接抛出 | `orchestrator/sealed/runner.py::assert_no_test_leak`，在 `loop.py` 构造时接入 |
| 纪律：只从 train 轨迹做诊断 | 诊断语料来源只挂在 train 上；test 轨迹没有读取路径（密封 runner 只保存分数） | `orchestrator/scoring.py::EvalBackend.trajectories` |
| 纪律：全程配置一致 | 在启动器层面机械化：`run_meta.json` 记录配置指纹；配置变更则拒绝续跑 | `evolver/launch/state.py::RunMeta.check_config` |

## 2. SOP §1 冷启动 → 实现 { #2-sop-1-cold-start-implementation }

| SOP 条款 | Raven 实现 | 证据 |
|---|---|---|
| vanilla 在 train 全集上跑 K=3，形成厚账本 | `backend.cold_start()` 必须返回非空的稳定性账本；基线以 vanilla 输出目录冻结播种 | `orchestrator/loop.py`（构造处）；`benchmarks/appworld/evolve/run.py`（`seed_label="van0"`、`cold_start_k`） |
| 失败图谱覆盖 ≥ 7 个 WHY 类别 | `diagnose_round(min_why_classes=7)` | `orchestrator/nodes/diagnose.py::diagnose_round` |
| WHY × WHERE 分类法；可为新基准归纳生成 | `TaxonomySpec` 加两阶段的 `induce_taxonomy`；归纳失败会显式报错，绝不静默借用其他基准的表 | `orchestrator/nodes/taxonomy.py` |

## 3. SOP §2 七步漏斗 → 实现 { #3-sop-2-seven-step-funnel-implementation }

| SOP 步骤 | Raven 实现 | 证据 |
|---|---|---|
| ① 第 1 轮使用冷启动图谱；第 2 轮起对子节点重新诊断（已触发 / 翻转 / 仍失败），追加进活的图谱 | `_diagnosed_parents` 防止重复诊断；`merge_failure_maps` 跨轮累积并持久化；翻转记录在 `_flips` 中，附带伤害回放摘录（回归的任务实际是怎么坏的） | `orchestrator/loop.py`（轮次主体）；`orchestrator/production.py::outcome_hook` |
| ② 1–2 个 WHY × 2–3 个候选，受预算上限约束；由环境变量控制、默认关闭 | WHY 选择默认走 driver 模式（由模型挑选；公式作为回退并留影子日志）；`Budget(max_why_per_round × candidates_per_why)` 在代码中强制；appworld 线路按节点传入 `activation_env` | `benchmarks/appworld/evolve/editor.py::driver_select_whys / rerank_whys`；`orchestrator/config.py::Budget`；`benchmarks/appworld/evolve/adapter.py` |
| ③ 免费剪枝：beacon_guard + preflight | beacon：编辑器拒绝没有 `activation_beacon` 的 python 改动（硬性）；preflight：`make_zero_hit_preflight` 零命中剪枝，**默认关闭**（见 §6 ①） | `benchmarks/appworld/evolve/editor.py`；`orchestrator/production.py::make_zero_hit_preflight`；`benchmarks/appworld/evolve/run.py`（`zero_hit_preflight=False`） |
| ④ 应用改动、创建子节点；path_guard 守护内核；git 持久化 | 先改后提交：改动以父节点的真实 git 子提交落地，工作树不受影响，并机械地返回变更路径 | `evolver/tree/git_ops.py::commit_files_as_child`；`evolver/applier/path_guard.py` |
| ⑤a K=1 锚点宽松筛选；计算 σ_screen；1.5σ 处淘汰；三档 | 三个分桶 clear_win / within_band / cull，只有 cull 会被拦；σ 公式与 SOP 一致，由 `select_anchor` 从账本中给出；AppWorld 线路使用聚焦子集的 Fisher 探测变体（同样是宽松通过：只淘汰显著更差者） | `orchestrator/nodes/screen.py::screen_candidate`；`evolver/scheduler/anchor_selection.py::select_anchor / simple_anchor`；`orchestrator/gates/strategies.py::FocusedFisherGate` |
| ⑤a 锚点构成：亲和度占多数 + 破冰任务 + 哨兵任务，且 ⊂ train | 三种角色均已实现；哨兵守卫引入了 SOP 没有的分层（稳定任务用均值守卫，脆弱任务用 Fisher，避免被噪声误杀）；亲和度的数据来源尚未接线（§6 ③） | `evolver/scheduler/anchor_selection.py`；`orchestrator/gates/strategies.py`（哨兵守卫） |
| ⑤ 借用机制（SOP 标注为 [defer]） | 模块已存在（与上游逐字节一致），尚未接线（§6 ②） | `evolver/scheduler/tree_aware_bandit.py` |
| ⑤b 幸存者：全集 × K=3 确认 | `k_confirm=3`，确认阶段跑完整 train 集 | `orchestrator/gates/strategies.py` |
| ⑥ 三道护盾，顺序为 Gate-f → Gate-b → Gate2 | `run_gates` 正是这个顺序；缺少埋点数据时 Gate-b **放行**（绝不冤枉一个没埋点但诚实的候选）；上报分数始终使用全集的固定分母，因此 Gate-b 的子集均值绝无可能冒充成绩 | `orchestrator/gates/pipeline.py::run_gates`；`orchestrator/production.py`（感知 beacon 的 `fired_source`） |
| Gate-b 数据链 | 写入侧：编辑器强制内联 beacon → 每次尝试生成 beacon 目录；读取侧：对确认目录与 infra 阶梯的兄弟目录取并集。诚实说明：归因只到「存在」层面——Gate-b 证明的是候选代码中*某个* beacon 在某任务上执行过，而非该 beacon 位于机制的触发条件之内；无条件放置的 beacon 会把 Gate-b 降级为空操作（晋级仍需在全 train 集上取胜） | `evolver/activation/ledger.py::beacon_workspace / mark_beacons_enabled`（由 `benchmarks/appworld/batch.py` 调用）；`evolver/activation/ledger.py::read_fired_tasks` |
| ⑦ 库中最优者成为新父节点；全军覆没则保留旧父节点并重新诊断 | `beat_vanilla` 耐心信号加贪心的父节点选择 | `orchestrator/loop.py` |
| 终止条件：连续 10 轮无人超过 vanilla（比较对象是 vanilla，不是上一个父节点）或 20 轮上限；绝不查看 test | `TerminationTracker(patience=10, max_rounds=20)`，信号定义为战胜**固定的** vanilla；外加一项 SOP 没有的保护：出错的轮次不消耗耐心（`max_consecutive_errors` 是独立的兜底） | `orchestrator/termination.py` |
| 四种节点状态 | 实为超集：`pruned_inert / pruned_at_screen / pruned_at_confirm / promoted_to_baseline / errored / blocked_l1 / archived-methodology-failure`；惰性死亡还会写入按 WHY 归档的历史，供设计器从中学习 | `evolver/tree/node.py::NodeStatus`；`orchestrator/production.py::inert_hook` |
| WHERE 由产物机械绑定，绝不采信自我声明 | `bind_where` 从实际改动的文件推导杠杆；自我声明的 `patch_where` 仅留在账本中供审计，绝不决定归档坐标（4 级粒度，见 §6 ④） | `orchestrator/archive.py::bind_where / cell_of` |

## 4. SOP §3 持久化 → 实现 { #4-sop-3-persistence-implementation }

| SOP 层 | Raven 对应物 |
|---|---|
| findings 工作日志 | `<work_dir>/findings.md`（每轮一节，记录驱动器的判定） |
| 跨会话状态 | journal（`orchestrator/state/journal.py`，崩溃续跑时回放已完成的轮次）加 `history.json`（按 WHY 归档的尝试历史） |
| Box 持久产物 | `failure_map.json`（跨轮的活图谱）/ `nodes/<id>.json`（节点账本：身份 + git 锚点 + 最终状态 + 闸门统计）/ 每轮的输出目录（逐任务结果） |

git 与节点账本这对双重真相来源（SOP §3.1）同构地保留了下来：
代码状态存放在 git 提交中（`commit_files_as_child` 产生真实的 SHA），
树的谱系存放在 `nodes/*.json` 中，两者通过 `git_commit_sha` 对齐。

## 5. 超出 SOP 的部分 { #5-where-we-exceed-the-sop }

- **密封测试 runner 已机械化。** SOP §8.2/§9.2 自己记下了它最锋利的欠债：
  「暂不建设；过渡期依靠纪律；评审者会质疑这一点」。Raven 的 `SealedTestRunner` 加
  `assert_no_test_leak` 正是 SOP 所要求的那种机制隔离——上游最锋利的方法论欠债在这里并不存在。
- **单个候选崩溃不会拖垮一整轮：** `errored` 状态，加上出错轮次不消耗耐心
  （`max_consecutive_errors` 作为独立兜底）；SOP 未覆盖此项。
- **QD 归档与重组：** 按（WHERE × WHY）分格的精英库，加上跨格的重组候选
  （`orchestrator/archive.py`）——一种超出 SOP 的探索机制；闸门与口径不变。
- **惰性死亡反馈回路**（2026-07）：一个独立的 `pruned_inert` 状态，惰性死亡写入历史，
  设计提示词区分「触发条件从未命中」与「机制被否决」，
  并对惰性死亡施加温和的 WHY 衰减（`0.55^n_fail × 0.85^n_inert`）。
- **伤害回放：** 当某个候选弄坏了一个任务时，该回归任务在此候选下的真实轨迹摘录
  会被喂给下一次设计尝试；SOP 只要求记录翻转次数。

## 6. 有意的偏离与尚未接线的部分（如实清单） { #6-deliberate-deviations-and-unwired-parts-honest-list }

1. **零命中 preflight 默认关闭**（`zero_hit_preflight=False`，2026-07 决定）。
   理由：Gate-b 已经不给从未触发的机制记功（不存在正确性漏洞）；preflight 只节省预算，
   却带有小幅误剪风险；TRIGGER_REGEX 声明是可选的，实际剪枝率未知。
   先收集数据（在某次运行中开启，检查历史里的 `pruned_inert` 条目），再决定默认值。
   SOP §2 把 ③ 标注为 [now]；这是一处有意的偏离。
2. **借用机制未接线**（SOP §2 ⑤，标注为 [defer]）：`tree_aware_bandit` 已存在，
   但编排器不调用它。AppWorld 的 train 集跑全量是负担得起的——
   这与 SOP 的「小基准不需要它」一致；任务集大时再接线。
3. **亲和度锚点缺少数据来源：** `select_anchor(affinity)` 接受该参数，
   但 appworld 一侧没有触发密度的来源（上游的 `affinity_picker.py` 未移植）。
   当前锚点由破冰任务 + 哨兵任务 + 边界任务构成；筛选仍然有效，只是信息效率略低。
4. **WHERE 的机械绑定只有 4 级**（prompt/runtime/mixed/edit），
   比判定 schema 的 14 个类别更粗。对 QD 分格已经够用；
   若需要细粒度的杠杆统计，可细化 `_lever_of_path`。自我声明值与机械值都在账本中，
   目前没有对账告警（属于可选的可观测性增强）。
5. **`pruned_inert` 在语义上与 SOP 的状态一致**，但实现方式不同：
   SOP 通过 preflight CLI 判定，我们通过嵌入循环的 preflight 判定（默认关闭，见 ①）。

## 6.5 统一入口（2026-07 加入） { #65-the-unified-entry-added-2026-07 }

SOP §8.3 的「手工编排」已被 `python -m evolver run --config <yaml>` 取代：
这是一条命令驱动的状态机，依次执行冷启动 → 各轮 → 终止 → 解封，
任何中断之后都可续跑（产物即状态：trial 文件 / journal / meta 戳记，三层真相）。
配置漂移与解封的单向性都在 `run_meta.json` 中机械化
（即 SOP §0 同一制式纪律的代码化）。
基准通过 `docs/specs/evolve-bench-contract.md` 中的契约接入（该文档与代码一同
保留在仓库内）；实现位于 `evolver/launch/` 与 `evolver/cli.py`。

## 7. SOP 组件 ↔ Raven 组件速查 { #7-sop-parts-raven-parts-quick-reference }

| SOP §8.1 中引用的组件 | Raven 对应物 | 形态差异 |
|---|---|---|
| `analysis/proxy_features.py` | `evolver/analysis/proxy_features.py` | 逐字节一致 |
| `analysis/failure_map_builder.py` | `evolver/analysis/failure_map_builder.py` | 逐字节一致 |
| `activation/preflight.py`（CLI） | `orchestrator/production.py::make_zero_hit_preflight` | CLI → 嵌入式；正则变体，默认关闭 |
| `analysis/stability_bucket.py` | `evolver/analysis/stability_bucket.py` | 逐字节一致 |
| `scheduler/bandit_tasks.py` | `evolver/scheduler/bandit_tasks.py` | 逐字节一致 |
| `scheduler/anchor_selection.py` | `evolver/scheduler/anchor_selection.py` | 同源，另加我们的 `simple_anchor` |
| `scheduler/affinity_picker.py` | **无对应物**（§6 ③） | — |
| `scheduler/tree_aware_bandit.py` | `evolver/scheduler/tree_aware_bandit.py` | 同源，未接线（§6 ②） |
| `activation/gate_audit.py`（CLI） | `orchestrator/gates/pipeline.py::run_gates`（Gate-b 已嵌入） | CLI → 嵌入式 |
| `analysis/paired_significance.py`（CLI） | `orchestrator/gates/paired.py::paired_lift`；留存评估在 `sealed/runner.py::unseal_retention` | CLI → 嵌入式 |
| `aggregate_keq3` / `gate0_ctrf_audit`（外部评估引擎） | `benchmarks/appworld/evolve/eval.py / adapter.py`（K=3 聚合加 infra 阶梯） | 跨仓库契约 → 同仓库模块 |
| `tree/*` | `evolver/tree/`（node/store/git_ops） | 同源，另加我们的 `commit_files_as_child` / `read_file_at` |
