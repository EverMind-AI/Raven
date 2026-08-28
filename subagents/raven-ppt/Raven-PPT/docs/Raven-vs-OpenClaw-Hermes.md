# Raven 竞争定位分析

> 对比 OpenClaw / Hermes Agent — 汇报版

---

## TL;DR

**Raven 不是"再做一个 Agent 框架"，而是把行业公认的四大痛点（上下文爆炸、Token 浪费、被动响应、Skill 静态）在**架构层面**做成可插拔的一等公民。Hermes 在这些点上要么只覆盖一部分，要么停留在口号层面；OpenClaw 是 Claude Code fork 路线，偏 IDE/编码场景。我们的差异化在"架构先行"而非"特性堆砌"。**

---

## 一图对比

| 维度 | OpenClaw | Hermes Agent | **Raven** |
|:-----|:---------|:-------------|:-------------|
| **定位** | Claude Code fork，IDE/编码伙伴 | 一体化消费级 Agent 产品（CLI + 多平台 IM） | **可插拔 Agent 框架**，四大支柱为一等公民 |
| **上下文管理** | Claude Code 原生策略 | 反应式压缩（`ContextCompressor`，阈值触发摘要丢弃）| **Curator**：Agent 自主调度 + 归档可恢复 + 11 个 internal tools（见下文） |
| **主动性** | 无（被动响应为主） | `queue_prefetch()` 仅被动拉取；`insights.py` 仅拉取式报告 | **Sentinel** 推送式主动性（Monitor + Rule/LLM Evaluator + 反打扰策略） |
| **节省 Token** | Claude Code cache 行为 | Cache "System + 3"；保守规则路由；用量追踪 | **TokenWise** 6 类策略协同：动态 cache 断点 + 三阶段 Tool Result 生命周期 + 可学习路由 + 预算闭环 |
| **Skill 自进化** | 静态 skills | **README 声称**自进化，开源代码仅见静态加载 | **SkillForge** 完整闭环：Detect→Draft→Execute→Feedback→Evolve→Retire，含版本管理与回滚 |
| **架构解耦** | 紧耦合 CLI/IDE | 单进程多平台（Gateway 模式） | 四大支柱通过 ABC 接口定义，配置驱动开关 |
| **代码规模** | 重（Claude Code 体量）| Python 93% + TS，40+ 工具 | 精简基座（~1400 LOC 核心）+ 新特性增量 |
| **国产模型** | 需自接 | OpenRouter 代理为主 | **原生**：DeepSeek、Qwen、Moonshot、Zhipu、DashScope 已在 providers 中 |
| **现有成熟度** | 成熟 | 成熟（有用户） | **Pre-alpha**（Tier 1 骨架已完成，测试绿） |

*OpenClaw 条目基于公开资料与 Hermes README 的引用推断，如需正式对外请先核验*

---

## 我们的三大核心差异化

### 1. Curator：业内唯一"Agent 驱动"的上下文管理

**Hermes 的做法**：`ContextCompressor` — 一套固定的 4 阶段（裁剪工具输出 → 保护边界 → 中段摘要 → 增量更新）。触发式、被动、不可恢复。

**我们的做法**：Curator 是一个**独立的小模型 Agent**（默认 gemini-2.5-flash），有自己的 11 个内部工具：

```
curator_check_budget          — 理解当前 token 压力
curator_archive_messages      — 无损归档到磁盘
curator_retrieve_archived     — 按需取回历史
curator_search_history        — 检索 history.jsonl
curator_read_memory           — 读 MEMORY/SOUL/USER
curator_set_relevance         — 手动调整消息重要性
curator_update_working_state  — 更新目标/open threads/决策
curator_build_context         — 终态工具，声明最终上下文
...
```

**关键差异**：
- Hermes 一旦摘要就**不可恢复**，锯齿型 token 曲线（峰 32K → 谷 16K → 回升）
- Curator **归档可召回**，token 曲线平稳（~20-21K），消息条数可持续增长

**基准数据**（来自 Curator 技术方案）：

| Benchmark | Legacy (对照) | Curator |
|:----------|:--------------|:--------|
| PinchBench 连续模式，16K 窗口，9 任务 | 86.0% | **95.9%** |
| DeepResearch-Bench-II，32K 窗口，10 任务 | 43.2% | **49.6%** |

在最难的 `task_21_comprehension` 上，Legacy 掉到 11%，Curator 保持 100%。

### 2. 主动性：行业的真空地带

我们做了硬核尽调——**公开代码层面，Hermes 和 OpenClaw 都没有推送式主动性**：

- Hermes `memory_provider.py` 有 `queue_prefetch()` 钩子——但它是**下一 turn 被动使用**，不是主动推送给用户
- Hermes `rate_limit_tracker.py` 的 80% 阈值只**打 log**，不通知用户
- Hermes `insights.py` 是**拉取式**报告，用户要求了才生成

Sentinel 是**真正的推送式主动性**：
- 6 类 Monitor（Idle / Cron / Workspace / Memory / FollowUp / Task）订阅事件总线
- 双层决策：规则层零成本过滤 + LLM 层（gemini-flash 3s 超时）处理模糊场景
- **反打扰策略**：`max_nudges_per_hour`、`quiet_hours`、`cooldown_on_dismiss`（这是最容易被忽视的部分）
- 通过事件总线注入，和主 Agent Loop 解耦，崩溃不影响主流程

### 3. Skill 自进化：真闭环 vs 口号

Hermes README 声称：
> "Autonomous skill creation after complex tasks. Skills self-improve during use."

我们读了 `skill_commands.py` 和 `skill_utils.py` 的实际开源代码——**未发现对应实现**。Explore agent 的原话："The code explicitly states it intentionally avoids the tool registry and heavy dependency chain. This is a lightweight metadata reader, not an autonomous evolution system."

**我们设计的是真闭环**：

```
Detect (对话结束判断) → Create (LLM 生成 SKILL.md) → Draft 状态
   ↓
首次成功执行 → Active 状态
   ↓
Execute 跟踪 (turns_used / tokens / outcome / feedback)
   ↓
success_rate < 0.7 持续 3 次 → 触发 Evolve
   ↓
Opus 4.6 重写（只增不删）→ 版本 +1，保留历史快照
   ↓
新版本表现下降 → 自动回滚
   ↓
闲置 90 天 → Deprecated，再 30 天 → Retired
```

每一步都有明确的数据结构、触发条件、失败保护。这是**工程级的闭环**，不是 README 里的一句承诺。

---

## 诚实的短板

**功能覆盖不足。** Hermes 有 40+ 工具、多平台 Gateway 已可用、语音转写、自然语言排程。Raven 当前（Tier 1）只完成骨架——我们继承自 nanobot 的基础工具链完整但数量少于 Hermes。

**成熟度不足。** Hermes 已有用户在跑；Raven 是 pre-alpha。前 6 个 Tier 都执行完也需要时间。

**赌注。** 我们赌的是：**架构层面的根本差异化 > 特性数量的短期优势**。理由是：
- Hermes 把主动性、自进化这些都当"特性标签"在宣传，一旦用户发现名不副实，信任崩盘
- 一旦 Curator 跑通（PinchBench 数据可验证），所有做上下文工程的团队都会关注
- 四大支柱都是**可被独立价值证明**的，不用赌"全家桶"成功

---

## 战略建议

### 继续重投入

| 项 | 理由 |
|:---|:-----|
| **Curator Slow Path 完整实现**（Tier 3）| 这是整个产品的灵魂，也是唯一一个我们已有**可复现基准**的差异化点 |
| **TokenWise Cache 优化**（Tier 2，立即）| 零风险、75% input cost 立省、可开源直接看到效果 |
| **SkillForge 真闭环**（Tier 5）| 这是 Hermes 吹但没做的，做出来就是差异化护城河 |

### 不必盲目追赶

| 项 | 理由 |
|:---|:-----|
| 40+ 内置工具 | 长尾工具价值有限，MCP 生态会把这些工作外包出去 |
| 多 IM 平台覆盖 | nanobot 已有 Telegram/Discord/Slack/Feishu/DingTalk/WhatsApp/WeChat/QQ/Matrix/Email，够用 |
| 语音转写 | 与 Agent 能力弱相关，调第三方即可 |
| 学习型智能路由 | 规则层已经吃掉 80% 收益，ML 层投入产出比低 |

### 可选加分项

- **国产模型原生支持**：nanobot 基座已有 DeepSeek/Qwen/Moonshot/Zhipu/DashScope 的 provider，在国内市场是实质优势
- **基准透明度**：Curator 的 PinchBench 基准公开可验，这是技术信任度的重要来源

---

## 一句话结论

> **Hermes 是"全家桶产品"，OpenClaw 是"编码伙伴"，Raven 是"架构级的 Agent 框架"——瞄准的是做严肃 Agent 产品的团队，而非终端消费者。**

四大支柱中，Curator 已有可量化证据，其余三个的技术方案完整度均高于行业公开资料。执行到位的话，**Curator + SkillForge 这两个点就足以支撑起差异化。**

---

*本文档基于 2026-04-15 的技术尽调。OpenClaw 对比条目基于公开资料，建议正式对外前再做一次源码层交叉验证。*
