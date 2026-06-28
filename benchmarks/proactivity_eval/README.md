# Agent 主动性评测

两个 benchmark：

- **pbench** — ProactiveAgent reward_data：120 条 one-shot help-or-skip 决策（4 个 category × 30 records，stratified sampling），用来回归"是否过度/不足主动"。
- **longrun** — 6 个 persona × 30 天 LLM-simulator 轨迹，用来评估"长期使用下的 fire 节奏 + restraint"。

两个 benchmark 都可以同时跑 Raven / Hermes / OpenClaw 做横向对比。

## 目录结构

```
proactivity_eval/
├── README.md
├── FINDINGS-v12.md                              历史 baseline 数据（旧版本对照）
├── runners.config.yaml                          系统/agent 路径与 provider 默认
├── data/
│   ├── pbench/test_data.jsonl                   pbench 输入（ProactiveAgent reward_data S1 protocol, vendored）
│   └── longrun/                                 6 persona YAML triples (profile + intents + outcomes)
├── runners/
│   ├── run.py                                   统一入口
│   ├── _common/                                 backends + drivers + shared helpers
│   ├── agents/{raven,hermes,openclaw}/       per-agent config + adapter glue
│   ├── benchmarks/{pbench,longrun}/             per-benchmark config
│   ├── prompts/                                 pbench 各 agent 模板
│   ├── pa_scorecard.py / longrun_scorecard.py   聚合脚本
│   └── README.md                                runner 用法
└── output/                                      JSON + scorecard
```

## 实验结果

### Agent 版本
| Agent | Version / 包版本 | Date |
|---|---|---|
| **Raven** | `raven 0.1.0` | 2026-04-28 |
| **Hermes** | `hermes-agent 0.10.0` | 2026-04-18 | 
| **OpenClaw** | `openclaw 2026.2.1` | 2026-02-03 |

### 结果

**pbench** (N=120 reward_data)：单轮"该不该 surface help"决策，同 backend qwen3.5-27B。

| Agent | TP | FP | TN | FN | Precision | Recall | F1 | mean/record |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **Raven** | 27 | 4 | 47 | 42 | 0.871 | 0.391 | **0.540** | 11.0s |
| **Hermes** (v0.10) | 10 | 0 | 51 | 59 | **1.000** | 0.145 | 0.253 | 17.1s |
| **OpenClaw** | 10 | 0 | 51 | 59 | **1.000** | 0.145 | 0.253 | 24.7s |

Hermes / OpenClaw 完美 `Precision=1.000` 是**架构性保守**——只在 very confident 时说 yes；代价是 `Recall=0.145` 远低于 Raven 的 0.391

**longrun** (6 persona × 30 day)：跨日 anticipatory proactivity，同 backend qwen3.5-27B

| 能力维度 | Raven | Hermes | OpenClaw | 含义 |
|---|---|---|---|---|
| **Anticipatory** ⭐<br>(rubric Type A 命中) | **19/43 (44%)** | 0/43 | 0/43 | "agent 没被告知就想到该做"——只有 L3 Sentinel 能做 |
| **Scheduled execution**<br>(delivered **cron** fires, trajectory-derived)³ | **109 fires**<br>(+155 sentinel anticipatory) | 115 fires<br>(原生 cron) | 61 fires²<br>(MCP-gateway) | user 显式说"X 时提醒"后 agent 是否真的注册并 fire |
| **Reactive Q&A**<br>(rubric Type B 命中) | 15/21 (71%) | **18/21 (86%)** | 11/21 (52%) | user 问问题时 agent 答对率 |
| **Restraint** 🛑<br>(rubric Type C 命中)¹ | 10/21 (48%)<br>31/62 pts | **16/21 (76%)**<br>49/62 pts | **16/21 (76%)**<br>49/62 pts | DND / 频率 / 周末 constraint 是否被破坏（不该 fire 时是否克制） |

¹ longrun 结果数据源（均以当前 `longrun_scorecard.py` 同版重打分，确保口径一致）；同 backend qwen3.5-27B。

² OpenClaw 经捆绑的 MCP cron server（gateway 模式，详见 [`data/longrun/README.md`](data/longrun/README.md#3-openclaw-reactive--mcp-gateway-cron-baseline)）注入 `set_reminder` 工具后能注册并触发 cron（61 fires）。OC 的 cron 为**一次性**（非 recurring），需 agent 每天重新注册；以 caregiver（每日 3 药）为例，OC 前 ~15 天逐日 re-arm，此后停止重新注册、cron fire 干涸（trajectory 仍为完整 30 天、对话正常）。对照 Hermes 把 3 药注册为 recurring job，05-27~30 无对话仍照常 fire。OC 的 61 是其一次性模型下未能维持循环提醒的**欠发下界，非克制**。

³ **Scheduled execution 只计 cron fire**（`longrun_scorecard.py::_count_delivered_fires` 的 `total = cron`）。这一维衡量"用户显式预约的提醒是否真的注册并触发"，是 cron 的职责；Raven 的 sentinel anticipatory fire 属于 Anticipatory 维，作旁注 `(+N sentinel)` 显示但**不计入**本行。


**四个维度反映的差距：**

- **Anticipatory**：EC 19 vs Hermes 0 vs OC 0 是**架构级差距**——L3 Sentinel 是唯一能在用户没显式提示时主动 surface 的层。Hermes/OC 永远是 0（cron 不管原生还是 MCP-gateway，都只能执行已注册的 job，不能"预知"）。
- **Scheduled execution**（cron fire，见 ³）：EC 109 / Hermes 115 / OC 61。整个 longrun cron 应有量 ≈ 100–110，EC 的 109 落在合理区间，OC 61 偏低（欠注册）。EC 另有 155 次 sentinel anticipatory fire，归在 Anticipatory 维、不计入本行。
- **Reactive Q&A**：EC 15/21 (71%) / Hermes 18/21 (86%) / OC 11/21 (52%)。同 backend qwen-27B 下 Hermes 最稳；OC 在多个 persona 的问答上回退（freelancer 0/4、parent 2/4）。
- **Restraint**：EC 10/21 vs Hermes 16/21 vs OC 16/21。**Anticipatory 和 Restraint 是同一硬币的两面**——会主动 fire 的 agent 同时也更容易撞到 quiet_hours / bedtime / 周末 窗口。Hermes / OC 拿到接近满分（16/21）的代价是 **Anticipatory=0**——这是 "几乎不主动 ⇒ 几乎不违例" 的廉价分，**只看 Restraint 单维会奖励 always-hold agent**，必须和 Anticipatory / Scheduled execution 联合判读。


## 用法

### pbench

```bash
# Smoke (n=10 stratified, ~3 min)
uv run python benchmarks/proactivity_eval/runners/run.py \
    --agent raven --benchmark pbench --n 10 --context-mode cold \
    --output benchmarks/proactivity_eval/output/pbench-smoke.json

# Full (n=120, ~30-40 min)
uv run python benchmarks/proactivity_eval/runners/run.py \
    --agent raven --benchmark pbench --n 120 --context-mode cold \
    --output benchmarks/proactivity_eval/output/pbench-n120.json

# Scoring → markdown table
uv run python benchmarks/proactivity_eval/runners/pa_scorecard.py \
    --ec-agent-cold benchmarks/proactivity_eval/output/pbench-n120.json \
    --output benchmarks/proactivity_eval/output/pbench-n120-scorecard.md
```

### longrun

`run.py` writes trajectories to `output/longrun/` by default, and
`longrun_scorecard.py` reads/writes the same dir by default. To score a
snapshot stored elsewhere (e.g. `output/post-C-cleanup-d30/`), pass
`--output-dir` to the scorecard — no need to move files. (`run.py` has
its own `--output-dir` for choosing where trajectories land.)

```bash
# Smoke (single persona, 1 day, ~5 min) — output → output/longrun/ by default
uv run python benchmarks/proactivity_eval/runners/run.py \
    --agent raven --benchmark longrun --case parent-01 --day-limit 1

# Full (all 6 personas × 30 days; expect hours)
uv run python benchmarks/proactivity_eval/runners/run.py \
    --agent raven --benchmark longrun --all

# Score one persona×agent (reads output/longrun/longrun-<persona>-<agent>-trajectory.jsonl)
uv run python benchmarks/proactivity_eval/runners/longrun_scorecard.py \
    --persona parent-01 --agent raven

# Score all personas + per-persona comparison + 跨 persona × agent capability 表
# (产出本 README 上面那张 longrun 结果表的 markdown：output/longrun/aggregate-scorecard.md)
uv run python benchmarks/proactivity_eval/runners/longrun_scorecard.py \
    --all --compare --aggregate

# 只重新生成 aggregate 表（已有 *-scorecard.json 时不重跑评分）
uv run python benchmarks/proactivity_eval/runners/longrun_scorecard.py --aggregate

# Score / aggregate a snapshot in a non-default dir (no file moving)
uv run python benchmarks/proactivity_eval/runners/longrun_scorecard.py \
    --aggregate --output-dir benchmarks/proactivity_eval/output/post-C-cleanup-d30
```

详见 `runners/README.md`。

