# Agent 主动性评测

两个 benchmark：

- **pbench** — ProactiveAgent reward_data：120 条 one-shot help-or-skip 决策（4 个 category × 30 records，stratified sampling），用来回归"是否过度/不足主动"。
- **longrun** — 6 个 persona × 30 天 LLM-simulator 轨迹，用来评估"长期使用下的 fire 节奏 + restraint"。

两个 benchmark 都可以同时跑 Raven / Hermes / OpenClaw 做横向对比。

## 目录结构

```
proactivity_eval/
├── README.md
├── runners.config.yaml                          系统/agent 路径与 provider 默认
├── data/
│   ├── pbench/test_data.jsonl                   pbench 输入（ProactiveAgent reward_data S1 protocol, vendored）
│   └── longrun/                                 6 persona YAML triples (profile + intents + outcomes)
├── runners/
│   ├── run.py                                   统一入口
│   ├── _common/                                 backends + drivers + shared helpers
│   ├── agents/{raven,hermes,openclaw}/       per-agent config + adapter glue
│   ├── benchmarks/{pbench,longrun}/             per-benchmark config
│   ├── prompts/                                 pbench 模板（统一最小模板：prompts/uniform/）
│   ├── pa_scorecard.py / longrun_scorecard.py   聚合脚本
│   └── README.md                                runner 用法
└── output/                                      JSON + scorecard
```

## 实验结果

### Agent 版本

两张结果表均为 latest 版本实测（pbench 于 2026-07-22 用统一 prompt 重测；longrun 于 2026-07-23 用新 harness + 新计分器全量重跑，三家统一走 OpenRouter qwen3.5-27b）：

| Agent | pbench 主表（2026-07-22，latest） | longrun（2026-07-23，latest） |
|---|---|---|
| **Raven** | `raven 0.1.2` | `raven 0.1.2` |
| **Hermes** | `hermes-agent 0.19.0` @ `9eb7b1a6`（2026-07-20，dev install） | `hermes-agent 0.19.0` @ `9eb7b1a6` |
| **OpenClaw** | `openclaw 2026.6.34`（docker，ghcr digest `25f5bacf5174…`） | `openclaw 2026.6.34`（docker） |


### 结果

**pbench** (N=120 reward_data)：单轮"该不该 surface help"决策。所有 agent 使用**统一最小任务 prompt**（无人设、无决策原则，模板见 `runners/prompts/uniform/`），各自 CLI 黑盒调用，同 backend qwen3.5-27b（OpenRouter）。

| Agent | 调用方式 | TP/FP/TN/FN¹ | Precision | Recall | F1（3 runs mean） | mean/record |
|---|---|---:|---:|---:|---:|---:|
| **Raven** (0.1.2) | `raven agent --message` | 48.0/17.3/33.7/21.0 | 0.735 | **0.696** | **0.715**（0.707–0.725） | 19.4s |
| **OpenClaw** (2026.6.34) | `openclaw agent --local`（docker） | 44.0/16.0/35.0/25.0 | 0.733 | 0.638 | 0.682（0.641–0.722） | 15.4s |
| **Hermes** (v0.19.0) | `hermes chat -q -Q` | 33.7/13.3/37.7/35.3 | 0.717 | 0.488 | 0.579（0.545–0.628） | 25.5s |

¹ 三次独立运行的均值（括号内为 F1 逐轮范围），2026-07-22 实测。分数是**版本快照**，不代表最新版本能力。


**longrun** (6 persona × 30 day)：跨日 anticipatory proactivity，同 backend qwen3.5-27b（OpenRouter）

| 能力维度 | Raven | Hermes | OpenClaw | 含义 |
|---|---|---|---|---|
| **Anticipatory** ⭐<br>(rubric Type A 命中) | **19/43 (44%)** | 1/43 (2%) | 1/43 (2%) | "agent 没被告知就想到该做"——自主注册预判 / Sentinel 主动 surface |
| **Scheduled execution**<br>(delivered **cron** fires, trajectory-derived)² | 109 fires<br>(+87 sentinel anticipatory) | 103 fires<br>(原生 cron)³ | **267 fires**<br>(MCP-gateway, repeat)¹ | user 显式说"X 时提醒"后 agent 是否真的注册并 fire |
| **Reactive Q&A**<br>(rubric Type B 命中) | 6/21 (29%) | **10/21 (48%)** | 5/21 (24%) | user 问问题时 agent 答对率 |
| **Restraint** 🛑<br>(rubric Type C 命中) | 12/21 (57%) | **17/21 (81%)**| 16/21 (76%) | DND / 频率 / 周末 constraint 是否被破坏（不该 fire 时是否克制） |

¹ OpenClaw 经捆绑的 MCP cron server（gateway 模式，详见 [`data/longrun/README.md`](data/longrun/README.md#3-openclaw-reactive--mcp-gateway-cron-baseline)）注入 `set_reminder` 工具后注册并触发 cron。桥现已支持 `repeat`（daily/weekdays/weekly）。

² **Scheduled execution 只计 cron fire**。衡量"用户显式预约的提醒是否真的注册并触发"，是 cron 的职责；Raven 的 sentinel anticipatory fire 属于 Anticipatory 维，作旁注 `(+N sentinel)` 显示但**不计入**本行。三家分别 109 / 103 / 267 fire（此维由 caregiver 每日服药提醒主导，且受 recurring vs one-shot 注册风格影响，波动大；OC 因 MCP-gateway repeat 明显偏高），说明三家都能可靠触发用户预约——**不作能力排名**。

³ **Hermes 的 suggestions 通道在本 harness 无得分路径**：v0.19 的建议投递是 pull-only（用户主动 pull `hermes suggestions` 才可见）。harness 以 `cron_suggestion` 事件记录每条生成的建议（observability，不代投递、不计分），数量单独统计。 **OpenClaw 嵌入模式的 session 卫生由 harness 代行**：`agent --local` 不做 compaction 也不轮换 session，固定 session 跑 30 天在 2026.6.34 上第 5 天即撞上下文墙（"Context overflow"，其后每轮报错）。harness 按其**产品自身默认**（`session_reset: at_hour 4`）每模拟日轮换 session，跨日记忆走其 workspace MEMORY.md bootstrap。



**四个维度反映的差距：**

- **Anticipatory**：Raven 19/43 (44%) vs Hermes 1/43 (2%) vs OC 1/43 (2%)——在决策归属计分（用户预约的提醒全部排除）、sonnet 强法官下，Raven 有领先的主动提醒倾向。
- **Scheduled execution**（cron fire）：Raven 109 / Hermes 103 / OC 267——三家都能可靠注册并触发用户预约的提醒；此维由 caregiver 每日服药提醒主导、且受 recurring vs one-shot 风格影响，波动大，**不作能力排名**（见脚注 ²）。
- **Reactive Q&A**：Raven 6/21 (29%) / Hermes 10/21 (48%) / OC 5/21 (24%)，sonnet judge 打分。
- **Restraint**：Raven 12/21 (57%) vs Hermes 17/21 (81%) vs OC 16/21 (76%)。**Anticipatory 和 Restraint 是同一硬币的两面**——越主动，越可能在用户不期望的时间打扰到用户（Raven 主动性最高，故 restraint 最低）。


## 用法

> 下方命令均从 repo 根目录运行；`<OUT>` = 输出目录（默认 `benchmarks/proactivity_eval/output/`）。

### pbench

```bash
# smoke（n=10 分层抽样，~3min），默认统一最小 prompt（结果表口径）
uv run python benchmarks/proactivity_eval/runners/run.py \
    --agent raven --benchmark pbench --n 10 --context-mode cold \
    --output <OUT>/pbench-smoke.json

# 全量（n=120，~30-40min）
uv run python benchmarks/proactivity_eval/runners/run.py \
    --agent raven --benchmark pbench --n 120 --context-mode cold \
    --output <OUT>/pbench-n120.json

# hermes 走自带 CLI（结果表口径的 hermes 通道）
uv run python benchmarks/proactivity_eval/runners/hermes_cli_pbench.py \
    --n 120 --concurrency 6 \
    --home-template <含 config.yaml/.env/auth.json 的目录> \
    --output <OUT>/pbench-hermes-cli.json

# 旧的各 agent 人设模板仅用于复现消融附表：加 --prompts-dir benchmarks/proactivity_eval/runners/prompts

# 打分 → markdown 表
uv run python benchmarks/proactivity_eval/runners/pa_scorecard.py \
    --ec-agent-cold <OUT>/pbench-n120.json \
    --output <OUT>/pbench-n120-scorecard.md
```

### longrun

`run.py` 默认把轨迹写到 `<OUT>/longrun/`，`longrun_scorecard.py` 默认读写同一目录。
要对存放在别处的快照打分，给 scorecard 传 `--output-dir` 即可，无需搬文件
（`run.py` 也有自己的 `--output-dir`，决定轨迹落盘位置）。

```bash
# smoke（单 persona，1 天，~5min），默认输出到 <OUT>/longrun/
uv run python benchmarks/proactivity_eval/runners/run.py \
    --agent raven --benchmark longrun --case parent-01 --day-limit 1

# 全量（6 persona × 30 天，耗时数小时）
uv run python benchmarks/proactivity_eval/runners/run.py \
    --agent raven --benchmark longrun --all


# 单个 persona×agent 打分（读 <OUT>/longrun/longrun-<persona>-<agent>-trajectory.jsonl）
uv run python benchmarks/proactivity_eval/runners/longrun_scorecard.py \
    --persona parent-01 --agent raven

# 全部 persona 打分 + 逐 persona 对比 + 跨 persona×agent 能力表
# （产出本 README 上面那张 longrun 结果表：<OUT>/longrun/aggregate-scorecard.md）
uv run python benchmarks/proactivity_eval/runners/longrun_scorecard.py \
    --all --compare --aggregate

# 只重新生成 aggregate 表（已有 *-scorecard.json 时不重跑评分）
uv run python benchmarks/proactivity_eval/runners/longrun_scorecard.py --aggregate

# 对非默认目录的快照打分/聚合（无需搬文件）
uv run python benchmarks/proactivity_eval/runners/longrun_scorecard.py \
    --aggregate --output-dir <OUT>/<snapshot>
```

详见 `runners/README.md`。
