# Sentinel Proactivity — 引入成本分析

**数据来源**：`proactivity-eval/output/longrun/v4a/`，6 personas × 30 模拟天，T=0
**模型**：主 AgentLoop 与 Sentinel Planner **共用同一个 backend** = 本地 vLLM `qwen3.5-27B`（`~/.hermes/config.yaml`，Volcano API gateway）。`commands.py:1230` 中 `ProactivePlanner(provider, provider.get_default_model())`——目前没有独立的 `planner_model` 配置项。
**评估时间**：2026-04-27
**关联文档**：`Proactivity-Plan.md`（设计意图）、`Proactivity-Implementation.md`（as-built）

> 下文表格里的 "qwen-plus / haiku / sonnet / opus 等价" **均为假设性折算**——实跑用本地 qwen 边际成本 ≈ 0（仅 GPU 时间），折算只是为了"换成商用 API 要花多少钱"的横向对比。

---

## TL;DR

- **96.4% 的 tick 在 Planner 之前被 fast-path 短路**（quiet hours + context-signature dedup）。一天 48 个 tick 里只有 **~1.73 次** 真正落到 Planner LLM。
- 折算到商用 API 的 Sentinel 月成本（每用户）：
  | 模型 | $/月/用户 | 关闭 fast-path 的对照 |
  |---|---:|---:|
  | qwen3.6-plus | **$0.08** | $2.15 |
  | claude-haiku-4.5 | **$0.23** | $6.41 |
  | claude-sonnet-4.5 | **$0.69** | $19.22 |
  | claude-opus-4.7 | **$1.15** | $32.04 |
- 6 个 personas × 30 天总计：Planner 调用 **311 次**，nudge 派发 **84 条**（policy 拒掉 1 条），用户 sim 显式反应 16 条（engage 8 / dismiss 7 / ignore 1），其余 81% 既未明确接受也未拒绝。
- 单位经济（Sonnet 等价）：$0.049/派发、$0.52/engaged。

---

## 一、成本来源

Sentinel 引入的 token 来自 4 个 call site：

| 来源 | 频次（v4a 实测，30 天 6 用户） | 是否在本表计入 |
|---|---:|:---:|
| **Planner.decide**（tick 主调用） | 311 次 | ✓ 主要 |
| RoutineLearner | 0（v4a 配置未启用周期触发） | — |

| ProactiveSpawn | 0（无 `action=spawn_agent` 决策） | — |
| 被诱发的主 agent turn（accepted nudge → user reply） | ~8 个 engaged → ≤16 个新 turn | 单列 |

`nudge_inject` 不发起新 LLM 调用，但会让下一条 reply 多出 nudge 文本（这里忽略，因为 inject 全期只命中 1 次）。

---

## 二、计算公式

设单 tick 跑 Planner 的 input/output token 为 $T_{in}$ / $T_{out}$，价格 $p_{in}$ / $p_{out}$（USD/token），fast-path 命中率 $h$，每日 tick 数 $K$（30 min 间隔 → $K=48$）。

**单次 Planner 调用成本**

$$C_{tick} = T_{in} \cdot p_{in} + T_{out} \cdot p_{out}$$

**每日 Sentinel 纯开销**

$$C^{day}_{sentinel} = K \cdot (1-h) \cdot C_{tick} \;+\; \frac{C_{routine\_learner}}{period_{days}} \;+\; N_{spawn} \cdot \overline{C_{spawn\_turn}}$$

**诱发成本**（accepted nudge 引起的主 agent turn）

$$C^{day}_{induced} = N_{accepted} \cdot \overline{C_{agent\_turn}} \;+\; N_{inject} \cdot \Delta T_{inject\_out} \cdot p_{out}$$

**单位经济**

$$C_{per\_dispatch} = \frac{C^{day}_{sentinel}}{N_{dispatched}/day}, \qquad C_{per\_engage} = \frac{C^{day}_{sentinel} + C^{day}_{induced}}{N_{engaged}/day}$$

$$\text{wasted ratio} = \frac{N_{dismiss}}{N_{dispatched}}$$

---

## 三、v4a 实测参数

### 3.1 Tick 路由分布（6 personas × 30 天 = 8640 ticks）

| route | 含义 | 计数 | 占比 | 是否 LLM 调用 |
|---|---|---:|---:|:---:|
| `fast_path_skip` | quiet_hours 或 context 未变 | **8329** | **96.4%** | ✗ |
| `skip` | LLM 跑了，决定不打扰 | 226 | 2.6% | ✓ |
| `nudge` | LLM 决定推送 → policy 通过 | 77 | 0.89% | ✓ |
| `defer` | 延后到 session 空闲 | 6 | 0.07% | ✓ |
| `inject` | 挂到下一条 reply 末尾 | 1 | 0.01% | ✓ |
| `nudge_denied` | LLM 决定推送 → policy 拒绝 | 1 | 0.01% | ✓ |
| **Planner LLM 调用合计** |  | **311** | **3.6%** |  |

逐 persona 分布：

| persona | ticks | fast_path | planner 调用 | fast% | nudges 派发 | engage | dismiss | ignore |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| caregiver-01 | 1440 | 1376 | 64 | 95.6% | 6 | 5 | 0 | 0 |
| dev-01 | 1440 | 1376 | 64 | 95.6% | 26 | 0 | 4 | 0 |
| freelancer-01 | 1440 | 1400 | 40 | 97.2% | 7 | 0 | 0 | 0 |
| parent-01 | 1440 | 1386 | 54 | 96.2% | 19 | 3 | 2 | 1 |
| student-01 | 1440 | 1397 | 43 | 97.0% | 5 | 0 | 1 | 0 |
| team-lead-01 | 1440 | 1394 | 46 | 96.8% | 14 | 0 | 0 | 0 |
| **TOTAL** | **8640** | **8329** | **311** | **96.4%** | **77** | **8** | **7** | **1** |

注：`nudges 派发` 列只数 route=nudge（不含 defer/inject）。total nudge 决策含 defer 6 + inject 1 + denied 1 = 85，实际 dispatched 84。

### 3.2 Token 估算

ContextAssembler 实测（dev-01 day29 checkpoint）：

| 组件 | chars | est. tokens |
|---|---:|---:|
| `SYSTEM_PROMPT`（中文为主） | 2638 | ~1150 |
| `PLANNER_TOOL` JSON schema（英文 + 嵌入中文 enum 描述） | 5086 | ~1460 |
| `MEMORY.md` slice | 556 | ~370 |
| `HISTORY.md` 尾部 | 174 | ~120 |
| 活跃 session（最近 5 条） | ~1300 | ~480 |
| Routines + 时间头 + NudgePolicy state + last_decision | ~300 | ~120 |
| **$T_{in}$ 总计** |  | **~3700** |

**$T_{out}$**：60% skip（`{action, reason, score}` ≈ 80 tok）+ 40% nudge（含 `topic_tag` + 中文 `nudge_message` ≈ 250 tok），加权平均 **~150 tok**。

> 这是基于字符数 + qwen-tokenizer 经验比例（中文 ~1.5 chars/tok、英文 ~3.5 chars/tok）的估算。要拿到精确值，给 `UsageTracker.after_llm_call` 加一个 `call_site` contextvar tag，就能从 `~/.raven/telemetry/usage-*.jsonl` 直接 group-by 拉出来——目前 Planner 和主 AgentLoop 的 usage 在同一份文件里没法区分。

### 3.3 价格表（USD per million tokens）

| 模型 | input | output | 来源 |
|---|---:|---:|---|
| qwen3.6-plus | 0.325 | 1.95 | 阿里云国际站 |
| claude-haiku-4.5 | 1.00 | 5.00 | Anthropic 公开价 |
| gpt-5 | 1.25 | 10.00 | OpenAI 公开价 |
| claude-sonnet-4.5 | 3.00 | 15.00 | Anthropic 公开价 |
| claude-opus-4.7 | 5.00 | 25.00 | Anthropic 公开价 |

本地 vLLM `qwen3.5-27B`（v4a 实跑）边际成本 ≈ 0，主要消耗 GPU 时间——下文表格用 API 等价折算便于横向对比。

---

## 四、成本结果

### 4.1 单次 Planner 调用

$T_{in}$ = 3700, $T_{out}$ = 150

| 模型 | $C_{tick}$ |
|---|---:|
| qwen3.6-plus | $0.00150 |
| claude-haiku-4.5 | $0.00445 |
| gpt-5 | $0.00613 |
| claude-sonnet-4.5 | $0.01335 |
| claude-opus-4.7 | $0.02225 |

### 4.2 每用户每日 Sentinel 纯开销

实际 Planner 调用率：$311 / (6 \times 30) = 1.73$ 次/天/用户。

$$C^{day}_{sentinel} = 1.73 \cdot C_{tick}$$

| 模型 | 每日 | 每月 |
|---|---:|---:|
| qwen3.6-plus | $0.0026 | **$0.08** |
| claude-haiku-4.5 | $0.0077 | **$0.23** |
| gpt-5 | $0.0106 | **$0.32** |
| claude-sonnet-4.5 | $0.0231 | **$0.69** |
| claude-opus-4.7 | $0.0385 | **$1.15** |

### 4.3 Fast-path 价值（counterfactual：每 tick 都跑 Planner，48 calls/day）

| 模型 | 每日（无 fast-path） | 每月 | fast-path 节省 |
|---|---:|---:|---:|
| qwen3.6-plus | $0.0718 | $2.15 | $2.07/月 |
| claude-haiku-4.5 | $0.2136 | $6.41 | $6.18/月 |
| gpt-5 | $0.2940 | $8.82 | $8.50/月 |
| claude-sonnet-4.5 | $0.6408 | $19.22 | $18.53/月 |
| claude-opus-4.7 | $1.0680 | $32.04 | $30.89/月 |

> **96.4% 的 tick 不进 LLM**，把 Sentinel 的边际成本压到 base agent 月支出的 1-2% 量级。两条 fast-path 规则（hard quiet_hours + blake2b 上下文签名 dedup）是迄今最高 ROI 的优化。

### 4.4 单位经济（Sentinel 纯开销）

每用户 30 天总开销 $C^{30d} = 51.83 \cdot C_{tick}$（每用户 51.83 次 Planner 调用）。分母用 84（=77 immediate + 6 deferred + 1 injected，policy 拒掉的 1 条不算）：

| 模型 | $/dispatched (n=84/6=14) | $/engaged (n=8/6≈1.33) | wasted% (dismiss/dispatch) |
|---|---:|---:|---:|
| qwen3.6-plus | $0.0055 | $0.058 | **8.3%** |
| claude-haiku-4.5 | $0.0165 | $0.173 | 8.3% |
| gpt-5 | $0.0227 | $0.238 | 8.3% |
| claude-sonnet-4.5 | $0.0494 | $0.519 | 8.3% |
| claude-opus-4.7 | $0.0824 | $0.865 | 8.3% |

> Sonnet 等价折算下，每条 engage 的 nudge 摊到 ~$0.52 的 Sentinel 开销——对消费级聊天 agent 偏贵，但对 to-B 助理（按席位计费）完全消化得动。

### 4.5 诱发成本（accepted nudge 触发的主 agent turn）

8 engaged across 180 persona-days = **0.044 次/天/用户**。假设每个 engage 引出 1.5 个主 agent turn（每 turn $T_{in}$ ≈ 6000、$T_{out}$ ≈ 400）：

| 模型 | $C_{agent\_turn}$ | $C^{day}_{induced}$（用户级） |
|---|---:|---:|
| qwen3.6-plus | $0.00273 | $0.0002 |
| claude-sonnet-4.5 | $0.0240 | $0.0016 |
| claude-opus-4.7 | $0.0400 | $0.0027 |

诱发成本相对 Sentinel 自身开销可以忽略（≈7%）——因为 v4a 里用户 sim engage 频率本来就低。如果未来 engage 率升到 30%，诱发成本会与 Sentinel 开销相当。

---

## 五、关键观察 + 待办

### 5.1 关键观察

1. **Fast-path 命中率 96.4% 远超此前估计的 50%**——quiet_hours（每 24h 占 6-7h）+ context-signature dedup（连续 ticks 的 memory/history/sessions 改动很少）联合作用比预期强。这是 Sentinel 单用户成本可控的主因。
2. **Nudge 派发率非常低**：0.43 次/天/用户，平均 ~2.3 天才有一次主动消息。说明 Planner+NudgePolicy 整体偏保守（设计意图）。
3. **engage 率 10.4%（8/77）**——但 81% 的 dispatched nudge 没有被 sim 明确反应，真实人类用户的反应漏斗会和这里很不一样。caregiver-01 在 5/7 上 engage（71%），dev-01 在 0/4 全 dismiss——picker bias 高度依赖 persona × topic 匹配。
4. **Opus 等价折算下 $1.15/月/用户的 Sentinel 开销**——Opus 4.7 降价后已经在 to-C 也吃得动的范围内（之前 $3.5/月 是按旧价 $15/$75 算的）。但**当前实现里 Planner 和主 AgentLoop 共用 `provider.get_default_model()`**，没法分别路由——把 Planner 路由到 haiku 或 qwen3.6-plus 仍能再省一个数量级，需要先在 `RavenConfig.sentinel` 加 `planner_model` 字段并在 `commands.py:1230` 处使用。这是 TokenWise `RouterStrategy` 该接管的场景。

### 5.2 测量精度待办

1. **`UsageTracker` 加 `call_site` contextvar tag**（≤30 行改动），Planner / RoutineLearner / spawn / 主 agent / inject_reply 五种 call site 分别 group-by。本报告的 token 数靠 chars × 经验比例估算，误差大概 ±15%。
2. **trajectory.jsonl 里 `sentinel_tick` 事件直接夹带 `usage` 字段**，做到 per-tick 精确成本。当前需要按时间戳从 telemetry 跨文件 join，工程成本不低。
3. **fast-path 拆分计数**：现在 `fast_path_skip` 一个标签把 quiet_hours 和 context-dedup 混在一起，应分别统计——前者是硬约束（不可优化），后者是软优化（可调）。

---

## 附：复算一遍（最小工作量）

```bash
# 1. 拉 v4a trajectory，按 route 计数：
python3 -c "
import json
from collections import Counter
from pathlib import Path
base = Path('proactivity-eval/output/longrun/v4a')
xt = Counter()
for f in base.glob('longrun-*-raven-trajectory.jsonl'):
    for line in open(f):
        d = json.loads(line)
        if d.get('kind') == 'sentinel_tick':
            xt[d.get('route','?')] += 1
print(xt)
"

# 2. 套公式（替换你自己的 p_in / p_out）：
python3 -c "
T_in, T_out = 3700, 150
p_in, p_out = 3.0, 15.0   # Sonnet 4.5 USD/MTok
C_tick = (T_in*p_in + T_out*p_out) / 1e6
calls_per_day = 311 / (6*30)
print(f'Sentinel \${calls_per_day*C_tick:.4f}/day  \${calls_per_day*C_tick*30:.2f}/mo')
"
```

数据原片在 `proactivity-eval/output/longrun/v4a/`（gitignored），需要复现可从 `proactivity-eval/data/longrun/` 的 fixtures 重跑——参见 `proactivity-eval/README.md`。
