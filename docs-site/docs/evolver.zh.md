# Evolver：使用与实验

Evolver 是独立的、由基准驱动的 Agent harness 改进工具：诊断失败任务、设计候选
修改、执行评估，并将有效修改保留为 Git 提交。它不是生产 Agent 在聊天中改写自身，
也不是记忆召回、SkillForge 检索或 Playbook executor。

本页负责入门和操作；[设计与实现](self-evolution-map.md)继续作为方法论到代码的
详细参考。

## 状态与适用人群 { #status-and-intended-audience }

当前 `evolver/README.md` 标记该目录计划与 vendored fork 一起退役，等待确认。
实现仍然存在；本页记录现有工具，不承诺长期支持。在新系统中依赖它之前，先确认
维护周期。

适用于维护目标 harness、有可重复基准任务和评分、能够在隔离环境中承担重复评估
成本的开发者。日常任务自动化使用[Playbooks](playbooks.md)，一次性组图使用
[DAG 编排](orchestration.md)。

统一 launcher 当前**只注册 AppWorld**。目录中存在其他评估模块，并不意味着它们
已经成为可用启动目标。README 记录了单元测试、假基准端到端测试和真实 AppWorld
smoke，但未记录完整规模的多轮运行或真实基准上的 sealed-test retention 验证。
这是文档中的证据边界，不是生产就绪声明。

## 实验如何运行 { #how-an-experiment-works }

```text
固定根提交 -> cold-start baseline -> 训练失败诊断
    -> 候选子提交 -> 廉价 screen -> 全训练集 confirm
    -> gates -> 选择下一父版本 -> 在限额内循环
    -> 终止 -> sealed-test 报告（如已配置）
```

模型负责诊断、补丁设计和语义解释；代码负责调度、评分、gate、版本关系与检查点。
**WHY** 是失败原因类别，**WHERE** 描述编辑位置，**K** 是每项任务的尝试次数。
默认 screen 为 `K=1`、confirm 为 `K=3`；AppWorld 使用聚焦子集的 Fisher screen
并配合回归 sentinel。

Screen 以较低成本淘汰明显更差的候选，通过不等于晋升。候选需要通过确认 gate，
并在训练分数上超过当前父版本，才能成为下一父版本。有价值的候选也可能只进入
archive，不替换父版本。

比较 baseline 与不断变化的父版本不是一回事。AppWorld 默认冻结 cold-start
baseline。设置 `bench_config.baseline_mode: same_session` 后，在相同时段测量
control，评估成本约翻倍，但能减少 endpoint 漂移偏差。

## 前置条件与运行配置 { #prerequisites-and-a-run-specification }

使用包含 `evolver/` 和基准适配器的源码树、Git，以及通过 `uv` 管理的
项目环境。目标 `repo_root` 必须是包含基准集成的 Git 工作树。将 `base_sha`
固定为真实提交；未提交修改不会作为根版本参与评估。

AppWorld 需要准备：

1. 独立安装的 AppWorld，以及下载完成、非空的 `data/` 目录。Adapter 默认寻找
   `<appworld_data_root>/appworld-venv/bin/appworld`，除非设置了 `APPWORLD_BIN`。
   此环境与 Raven 环境分开。
2. 目标 Agent runtime config JSON，参考仓库 `docs/examples/subject_runtime.json`，
   配置可用的模型凭据。
3. 每行一个真实训练任务 ID，以及可选且不相交的 test ID 文件。占位 ID 会被拒绝。
   若要报告 retention，必须配置 sealed split。
4. Loop 的 driver/design/verdict 模型访问，以及足够的空闲端口、磁盘和评估预算。

在源码树之外创建 `my_run.yaml`，替换下方所有示例路径和根提交：

```yaml
bench: appworld
repo_root: /path/to/Raven
base_sha: REPLACE_WITH_SUBJECT_COMMIT
work_dir: /path/to/evolution/work
funnel:
  k_screen: 1
  k_confirm: 3
  budget:
    max_why_per_round: 1
    candidates_per_why: 1
    recombinations_per_round: 0
  termination:
    patience: 2
    max_rounds: 3
bench_config:
  config_path: /path/to/subject_runtime.json
  appworld_data_root: /path/to/appworld
  train_task_file: /path/to/train.txt
  test_task_file: /path/to/test.txt
  n: 3
  conc: 1
  base_port: 8600
smoke:
  bench_config:
    n: 2
    conc: 1
    base_port: 8700
```

这是小规模连线示例，不是统计上充分的实验。Test 文件也应保持很小：`n` 只限制
训练任务数量。只有不需要 sealed-test 报告时才省略 `test_task_file`。
Smoke 配置也可设置 `test_task_ids` 或独立的 `test_task_file`。

相对路径按配置文件目录解析。省略 `base_sha` 时，每次加载都会解析目标当前 HEAD；
HEAD 后续变化可能触发恢复时的配置漂移检查。固定原始 SHA 可以避免歧义。

省略 `models` 时，各 loop 角色都使用 Raven 配置的模型。如需定制，参考
`docs/examples/evolve_appworld.yaml` 添加 `models.driver`、`models.design` 和
`models.verdict` provider 配置。它们与 `bench_config.config_path` 中的目标
Agent 模型独立。不要把凭据写入共享 YAML；支持的 provider 配置可引用 API key
环境变量。

## 先检查，再 smoke，最后正式运行 { #check-smoke-then-run }

在 Raven 源码树根目录执行：

```bash
uv run python -m evolver check --config /path/to/my_run.yaml
uv run python -m evolver check --config /path/to/my_run.yaml --smoke
uv run python -m evolver run --config /path/to/my_run.yaml --smoke
uv run python -m evolver status --config /path/to/my_run.yaml --smoke
```

`check` 校验配置、模型设置、基准文件、可编辑路径和环境。
**它不是离线 dry run：** AppWorld 默认 precheck 会向目标 endpoint 发送一个小型
completion 探测，可能产生费用，但不会运行基准 trial。

`--smoke` 使用 `<work_dir>_smoke`。内置默认值将运行缩减为一个 WHY、一个候选、
一轮、无重组和 `K=1` confirm。之后再应用用户的 `smoke` overlay，因此这些限制
可以被覆盖。它不会自动缩减所有基准自有任务列表。即便 smoke 也可能调用真实模型、
执行候选代码并创建提交。

检查 smoke 工件、选择适当任务集和限额后，再启动完整实验：

```bash
uv run python -m evolver run --config /path/to/my_run.yaml
uv run python -m evolver status --config /path/to/my_run.yaml
```

不要让多个 runner 同时使用同一 `work_dir`。Status 只检查状态，不暴露 sealed
test 分数。

## 正确理解 gates { #read-the-gates-correctly }

| 检查 | 实际含义 | 重要限制 |
| --- | --- | --- |
| Gate-f | 报告受基础设施故障影响的测量；上游可重试挽救 trial | 剩余故障按未通过计入，不删除任务 |
| Gate-b | 提供 instrumentation 时，只在候选 beacon 触发的任务上归因 | 无 instrumentation 时不执行该检查；beacon 不是沙箱 |
| Gate2 | 计算候选与 control 的配对提升 | Navigator 改进与 `credited_2sigma` 是独立结果 |

报告的分母不能缩成“干净任务”或“beacon 触发任务”。全训练分数、归因子集和统计
credit 回答不同问题。晋升候选可能仍有 `credited_2sigma: false`，不能据此宣称
取得统计显著性提升。

AppWorld 路径上的 Python 候选修改通常要求 `activation_beacon()`。
Presence-level 归因仅证明某段带标记代码执行过，不证明特定机制造成了收益。
引用结果前检查 diff 和 beacon 位置。

## Sealed test 与终结 { #sealed-test-and-finalization }

训练轨迹用于诊断、设计和选择；test ID 不能与 train/anchor 重叠。Sealed-test
分数不进入决策路径。这是实验协议，不是阻止任意代码访问的操作系统权限屏障。

AppWorld launcher 将 sealed evaluation 接到最终收尾。最终交付版本按
**训练分数**选择，不能遍历候选挑选最高 test 分数。`retention.json` 报告交付
版本、train/test 曲线、配对证据和 retention：test lift 除以 train lift；
train lift 不为正时无定义。

自然终止会终结运行。已有完成轮次时，可提前结束：

```bash
uv run python -m evolver finalize --config /path/to/my_run.yaml --yes
```

显式 finalize 前停止正在执行的 runner。此操作解封结果并把运行标记为终结，
不是暂停。没有配置 test split 时也会标记终结，但没有 test-retention 报告。
若 unseal 在写标记前评分失败，修复环境后重试；若标记已存在但报告缺失，
`finalize` 可重建报告。

`run --force` 可以绕过配置漂移和 unseal 保护，但不能恢复诚实的 sealed 实验，
也不会自动让变更后的测量变得可比。看过结果后，应优先使用新的工作目录和未查看的
holdout。

## 预算、中断与输出 { #budget-interruption-and-outputs }

主要成本是目标 Agent trial：baseline 约需 `训练任务数 × K` 次尝试，每个通过
screen 的候选增加一轮全训练 confirm，此外还有 screen、重试、driver 调用和可选
test 评分。候选数和轮次上限不是严格的货币支出上限。

默认每轮两个 WHY、每个三个候选，最多一个重组候选，`patience: 10`、
`max_rounds: 20`。Patience 统计没有任何全训练候选超过固定 vanilla baseline
的轮次，不只是没有新冠军的轮次。连续异常轮次有独立停止保护。

终结前中断并重新运行同一命令，会从 trial 工件和已完成轮次检查点恢复。
已完成 trial 会复用；基础设施重试链仍可能重新评估受污染的工作。这不是对中断的
模型调用进行指令级恢复。

| `work_dir` 下的工件 | 用途 |
| --- | --- |
| `run_meta.json` | 有效配置指纹和终结标记 |
| `journal/rounds.jsonl` | 已完成轮次检查点与父版本关系 |
| `nodes/*.json` | 候选 commit SHA、状态和 gate 统计 |
| `findings.md` | 面向人的轮次日志 |
| `failure_map.json` | 累积失败诊断和 flip 信息 |
| `runs/` | AppWorld trial 结果，包括 baseline/confirm 证据 |
| `sealed/` 和 `retention.json` | 封存测量与最终报告（如已配置） |

候选是真实 Git 子提交；目标 working tree 不会自动替换成晋升版本。审阅记录的
SHA，按正常 Git 工作流保留需要的提交、执行测试，并另外取得部署批准。
Evolver 晋升不等于合并或发布。

## 安全与排障 { #security-and-troubleshooting }

在一次性容器或 VM 中运行，使用限定凭据。可编辑路径保护和不可变测量内核限制
候选捕获范围，但 design 步骤不是文件系统/网络沙箱。候选代码以 scorer 权限执行；
内存篡改、访问基准 oracle 仍是威胁模型限制。相信分数前人工检查小型候选 diff。
轨迹可能包含隐私数据，并会发送给配置的模型。

| 现象 | 建议 |
| --- | --- |
| 缺 AppWorld 数据、binary 或 runtime config | 修正路径与安装/下载，再运行 `check` |
| 占位或重叠 task ID | 使用真实且不相交的 split 文件 |
| Whitelist 前缀无匹配或候选为空 | 检查固定根版本是否存在目标文件，并查看捕获修改 |
| Endpoint 探测或端口检查失败 | 先修复服务或释放本次运行的端口，再支付 trial 成本 |
| 配置漂移导致拒绝恢复 | 恢复原配置/根 SHA，或建立独立实验 |
| 运行已 unseal | 阅读最终工件；不要绕过保护用 test 结果调参 |
| 没有 retention 报告 | 检查是否配置 test split，以及最终收尾是否成功 |

## 扩展基准与查看内部实现 { #extend-a-benchmark-or-inspect-internals }

新的 launcher 目标实现 `build(ctx) -> BenchBundle`，并注册到
`evolver/launch/registry.py`。它需要 scorer、带 infra 标记的 `TaskEval` 结果读取器、
诊断轨迹、不相交的 split 和可编辑路径规则。Cold-start closure 必须在 trial
级别幂等；昂贵评估放入 closure，不要在 `status` 也会调用的 bundle 构造时运行。

从 `docs/specs/evolve-bench-contract.md` 和
`benchmarks/appworld/evolve/entry.py` 入手。SOP 位于
`docs/specs/self-evolution-loop-sop.md`，设计说明位于
`evolver/orchestrator/DESIGN.md`。

[自进化映射](self-evolution-map.md)记录有意保留的缺口，包括未接入 borrowing、
缺少 affinity 数据和可选 zero-hit preflight。它用于模块级变更参考，不代表所有
SOP 能力都已启用。
