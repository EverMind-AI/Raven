# TokenWise 可执行方案

> 面向 Tier 2 / Tier 4 的落地计划。基于代码库实测的事实,不是设计草稿。

## 当前状态(事实,已核对)

- `TokenStrategy` ABC(两个 hook `before_llm_call` / `after_llm_call`)已在 `raven/core/interfaces.py:248` 定好
- `TokenWiseConfig`(6 策略的配置块)已在 `raven/config/raven.py:161` 落好
- `ModelRouter` 已在 `raven/routing/router.py` 能跑,`loop.py:620` 已在用 `select_model_id()`
- `LiteLLMProvider._apply_cache_control`(`providers/litellm_provider.py:126`)已有**简化版**缓存注入 — 只在最后一条 msg/tool 打一个断点
- LLM 调用**唯一入口** = `raven/agent/loop.py:267`(`self.provider.chat_with_retry(...)`)
- `raven/token_wise/` 目录存在但为空,只有一个空 `__init__.py`

---

## 一、整体架构

```
raven/token_wise/
├── __init__.py              # 导出 StrategyRegistry, install_from_config
├── registry.py              # StrategyRegistry: 多策略串联、before/after 链
├── pricing.py               # 单一可信价源: 调 litellm.cost_per_token + 手动兜底
├── usage_tracker.py         # 策略1: UsageTracker
├── cache_optimizer.py       # 策略2: CacheOptimizer (dominant win)
├── tool_result_lifecycle.py # 策略3: ToolResultLifecycle (三阶段)
├── smart_router.py          # 策略4: SmartRouter (复用现有 ModelRouter)
├── skill_lazy_loader.py     # 策略5: SkillLazyLoader
├── budget_alerter.py        # 策略6: BudgetAlerter
└── install.py               # from_config(cfg) -> StrategyRegistry
```

**集成点**(一处改动即可点亮所有策略):

```python
# agent/loop.py:267 附近
# 之前:
response = await self.provider.chat_with_retry(messages=messages, tools=tool_defs, model=effective_model)

# 之后:
msgs, tools, model_chosen = await self.strategies.before_llm_call(messages, tool_defs, effective_model)
response = await self.provider.chat_with_retry(messages=msgs, tools=tools, model=model_chosen)
usage = _extract_usage_snapshot(response, model_chosen, session_key)
await self.strategies.after_llm_call(response.__dict__, usage)
```

`AgentLoop.__init__` 新增参数 `strategies: StrategyRegistry | None = None`;`None` 时注入一个 no-op registry,100% 向后兼容。

---

## 二、StrategyRegistry(核心骨架)

**文件** `raven/token_wise/registry.py`

```python
class StrategyRegistry:
    def __init__(self, strategies: list[TokenStrategy]):
        self._s = strategies

    async def before_llm_call(self, messages, tools, model):
        for s in self._s:
            messages, tools, model = await s.before_llm_call(messages, tools, model)
        return messages, tools, model

    async def after_llm_call(self, response, usage):
        for s in self._s:
            try:
                await s.after_llm_call(response, usage)
            except Exception as e:
                logger.warning("TokenStrategy {} after_llm_call failed: {}", s.name, e)
```

- `before_llm_call` 失败**不**吞异常(错误的 messages 会导致 provider 400,应该直接抛)
- `after_llm_call` 失败吞异常(统计/预算故障不能阻止主流程)

**register order(重要)**:`SmartRouter → ToolResultLifecycle → SkillLazyLoader → CacheOptimizer → UsageTracker → BudgetAlerter`

理由:

1. 先决定 model(因为不同 model 决定是否支持 cache)
2. 再瘦身 messages(tool result 剪枝、skill 过滤)
3. 再打 cache 断点(必须是 messages/tools 的终态)
4. 计费与预算放最后(after hook 顺序)

---

## 三、各策略实施细则

### 策略 1: `UsageTracker` — 最先做,零风险

**文件** `usage_tracker.py`(~80 LOC)

**职责**

- 只实现 `after_llm_call`
- 累加到 `self.per_session: dict[str, UsageSnapshot]` 和 `self.global_daily: dict[date, UsageSnapshot]`
- 每 N 次调用刷一次磁盘:`~/.raven/telemetry/usage-YYYY-MM-DD.jsonl`(append-only)

**成本计算** 走 `pricing.py`:

```python
def estimate_cost(model, input_tok, output_tok, cache_read, cache_write) -> float:
    try:
        import litellm
        p, c = litellm.cost_per_token(model=model, prompt_tokens=input_tok, completion_tokens=output_tok)
        # Anthropic cache: read = 10% base, write = 125% base
        return p + c + (cache_read * p / input_tok * 0.1 if input_tok else 0) + ...
    except Exception:
        return _FALLBACK_PRICING.get(model, (0, 0))  # 0 = unknown, logged once
```

**验收** ≥ 100 次真实调用后,`cat ~/.raven/telemetry/usage-*.jsonl | jq` 能看到 prompt/completion/cache tokens、估计成本、model 都有;误差 vs `litellm.completion_cost()` < 2%。

---

### 策略 2: `CacheOptimizer` — 最大收益(Anthropic 最多省 75% input cost)

**文件** `cache_optimizer.py`(~150 LOC)

**职责** 替换 `LiteLLMProvider._apply_cache_control` 的简化实现。Anthropic 最多**4 个** `cache_control: {type: "ephemeral"}` 断点,放置策略:

| Breakpoint | 位置 | 理由 |
|---|---|---|
| #1 | tools 列表末尾 | tools schema 基本不变 |
| #2 | system prompt 末尾(role=system 最后一个 text block) | system + SOUL + USER + MEMORY 拼装后变化低 |
| #3 | history 中**倒数第二轮**(user→assistant→tool 成对)末尾 | 最远且依旧稳定的 turn |
| #4 | history 末尾**当前用户消息**之前 | 最新可缓存状态 |

**实现要点**

- 只对 `provider_spec.supports_prompt_caching == True` 的 model 动作
- `before_llm_call` 返回前深 copy 要改的 content block(不污染历史)
- 边界检查:如果 msgs < 4,退化到 2 个断点(#1 tools + #2 system)
- 与现有 `_apply_cache_control` 共存:给 `LiteLLMProvider` 加一个 `disable_auto_cache_control: bool = False` 开关,CacheOptimizer 启用时 provider 自动关掉它(由 `install.py` 负责注入设置)

**验收**

- 跑 3 轮同一会话,观察 `after_llm_call` 拿到的 `cache_read_tokens > 0` 且逐轮递增
- 手写 10 条会话回放 → `estimated_cost_usd` 比 baseline 下降 40% 以上
- **关键防退化测试**:`test_cache_breakpoints_invariant_for_unchanging_system_prompt` — 第 2、3 次调用的 prefix 哈希必须和第 1 次完全一致(否则 Anthropic 会缓存未命中)

---

### 策略 3: `ToolResultLifecycle` — 省历史 token 30-60%

**文件** `tool_result_lifecycle.py`(~200 LOC)

**三阶段**(由 `ToolResultLifecycleConfig` 里的两个 knob 控制,默认 3/10):

```
新 tool result ────── 前 N=3 轮:  原样保留 (FULL)
                 ↓
          4..M=10 轮:  用 summary_model 异步压缩到 ≤ 200 tokens (SUMMARY)
                 ↓
             > M 轮:   替换为 placeholder + 原始路径指针 (ARCHIVED)
```

**实现要点**

- `before_llm_call` 扫 messages,按 `role == "tool"` 统计 distance from end(只按 tool-turn 数,不按 message 索引)
- FULL 不动;SUMMARY 状态从一个持久化 cache 里读(`~/.raven/tool_summaries/<hash>.json`),未命中则塞回 FULL 并启动后台异步摘要任务(这轮用不上,下轮 upgrade)
- ARCHIVED 写入 `session.messages` 本体前先把**原文**挂到 `session.metadata["archived_tool_results"][tool_call_id]`,`curator_retrieve_archived` 工具能取回
- 绝不动最近 `full_retention_turns` 轮的 tool_result,防止 in-flight 多步工具链断掉
- 只在 session **持久化时**替换;LLM 上下文里替换是一致的

**验收**

- 构造一个 20 turn、每 turn 5KB tool output 的会话,第 21 turn LLM 的 input_tokens 应在 cap ≈ (full_retention × 5KB + summary_retention × 200_tokens + 固定开销) 之内(±10%)
- 归档后的 tool_call_id 可以被 `curator_retrieve_archived` 工具在同一 session 内取回(字节级相等)

---

### 策略 4: `SmartRouter` — 薄适配器

**文件** `smart_router.py`(~60 LOC)

**决策**:**不重写** `raven/routing/router.py` 的 `ModelRouter`(已经在用),只写一个 strategy 适配器:

```python
class SmartRouter(TokenStrategy):
    def __init__(self, router: ModelRouter | None, config: SmartRoutingConfig):
        self._router = router
        self._cfg = config

    async def before_llm_call(self, messages, tools, model):
        if not self._cfg.enabled or self._router is None:
            return messages, tools, model
        # Only route on the first LLM call of a turn (when last user msg is fresh)
        last_user = _last_user_message(messages)
        if last_user is None:
            return messages, tools, model
        chosen = await self._router.select_model_id(last_user)
        return messages, tools, chosen or model
```

**去重当前行为**:`loop.py:618-622` 自己在调 router。改为**只有 SmartRouter 策略启用时 loop 才调**。合并成一处逻辑,避免两边都在路由导致双倍推理调用。这需要一个小重构:把 `loop.py:618-622` 的 router 调用**删掉**,让 SmartRouter 成为唯一来源。

**验收**

- 打开 SmartRouter 跑 `PinchBench direct`,对 `category=light` 的 task 实际 called model 比例 ≥ 80% 属 light tier
- 关闭 SmartRouter 行为与 Tier 1 完全一致(回归测试用 mock router + 断言 router.select 没被调用)

---

### 策略 5: `SkillLazyLoader`

**文件** `skill_lazy_loader.py`(~120 LOC)

**职责** 只在 `before_llm_call` 里改 system prompt(messages[0]),不动 history。

**实现**

1. 拿当前所有 skills 的 `(name, summary)` 和当前 `last_user_message`
2. 关键词匹配(Jaccard on lowercased tokens)+ top-K(K=5)作为 Fast Path
3. 如果匹配度低(top score < 0.15),回退到 "always" skills only
4. 把原 system prompt 里形如 `<skills>...</skills>` 的 block 替换成过滤后的子集
5. LLM 调 skill 时,`loop.py` 已有的 skill 加载流程不变(通过 tool 拿完整 skill)

**不做 LLM-based relevance**:关键词 + Jaccard 就能吃掉 70% 收益;LLM relevance 留给 Tier 4 之后。

**验收**

- mock 30 个 skills,user 消息 "帮我查一下邮件","relevant" skill 有且只有 `email_helper`,`skill_lazy_loader.enabled` 前后对比 system prompt token 数降幅 ≥ 50%

---

### 策略 6: `BudgetAlerter`

**文件** `budget_alerter.py`(~80 LOC)

**职责**

- 只实现 `after_llm_call`
- 依赖 `UsageTracker` 已经累加好的数据(通过构造时注入 `tracker: UsageTracker`)
- 三档阈值:
  - `spend >= warn_at_usd`:logger.warning + 向 bus 发一个 `TriggerEvent(type="budget_warn")`(future Sentinel 接住)
  - `spend >= hard_limit_usd`:抛 `BudgetExceededError`,被 `chat_with_retry` 外层捕获后返回错误 response
  - `input_tokens >= warn_at_input_tokens`:同警告

**关键**:硬限默认 $2 可能过低,改成 `hard_limit_usd: float | None = None`(None = 不硬限);**警告永远开,硬限默认关**。

**验收**

- 构造 accumulated.estimated_cost_usd 跨过 warn 阈值 → 看到日志 + bus event 一次(后续不再触发,用 edge-trigger)
- 硬限触发时,`loop.py._run_agent_loop` 必须 graceful 停止并返回 "预算已超限" 给用户,而不是崩溃

---

## 四、`pricing.py`(共享基础设施)

**必须有且唯一**,否则 UsageTracker 和 BudgetAlerter 会各自估一份数据漂移:

```python
# ~60 LOC
def estimate_cost_usd(model: str, input_tok: int, output_tok: int,
                     cache_read: int = 0, cache_write: int = 0) -> float | None:
    """Return None if model unknown (caller should log & continue)."""
    ...

_FALLBACK_PRICING: dict[str, tuple[float, float]] = {
    "z-ai/glm-4.5-air": (0.13e-6, 0.85e-6),
    # 其余从 evaluation/PinchBench/direct/raven_executor.py 的 _fallback_pricing 搬过来统一
}
```

**顺带清理**:`evaluation/PinchBench/direct/raven_executor.py:311` 的 `_fallback_pricing` 应该改为 `from raven.token_wise.pricing import _FALLBACK_PRICING`,消除重复维护。

---

## 五、配置加载 & 安装点

**文件** `raven/token_wise/install.py`(~40 LOC):

```python
def install_from_config(cfg: TokenWiseConfig, router: ModelRouter | None) -> StrategyRegistry:
    if not cfg.enabled:
        return StrategyRegistry([])
    strategies: list[TokenStrategy] = []
    if cfg.smart_routing.enabled:
        strategies.append(SmartRouter(router, cfg.smart_routing))
    if cfg.tool_result_lifecycle.enabled:
        strategies.append(ToolResultLifecycle(cfg.tool_result_lifecycle))
    if cfg.skill_lazy_loading:
        strategies.append(SkillLazyLoader())
    if cfg.cache_optimization:
        strategies.append(CacheOptimizer(max_breakpoints=cfg.max_cache_breakpoints))
    tracker = UsageTracker() if cfg.usage_tracking else None
    if tracker:
        strategies.append(tracker)
    if (cfg.budget.warn_at_usd or cfg.budget.hard_limit_usd) and tracker:
        strategies.append(BudgetAlerter(tracker, cfg.budget))
    return StrategyRegistry(strategies)
```

**调用** 在 `cli/commands.py` 启动 `AgentLoop` 处,传给 `AgentLoop(..., strategies=install_from_config(...))`。

---

## 六、测试矩阵

每个策略必交付 3 类测试,放在 `tests/test_token_wise_<strategy>.py`:

| 测试类 | 目的 |
|---|---|
| `test_<strategy>_noop_when_disabled` | 关开关时 `before/after_llm_call` 是完全透传 |
| `test_<strategy>_happy_path` | 典型输入下产生预期效果(mock provider.chat) |
| `test_<strategy>_failure_does_not_crash` | 策略内部异常不影响主循环(after hook)/ 异常被正确抛出(before hook) |

**集成测试一个**:`tests/test_token_wise_integration.py`

- 6 策略全开,mock provider,跑 5 轮对话
- 断言: 第 2 轮起 `cache_read_tokens > 0`; SmartRouter 被调用;第 11 轮 tool_result 被 summarize;超预算时 raise

---

## 七、实施顺序(6 步,每步独立可 ship)

| 步 | 工作包 | 风险 | 独立价值 |
|:-:|---|:-:|---|
| 1 | `pricing.py` + `UsageTracker` + `StrategyRegistry` + `loop.py` hook 接线 | 低 | 立刻有可观测性 |
| 2 | `CacheOptimizer` + 停用 provider 自带 cache_control | 中 | **最大 ROI:input cost 降 40-75%** |
| 3 | `BudgetAlerter`(依赖 step 1) | 低 | 防炸;可对外讲 |
| 4 | `SmartRouter` + 重构 `loop.py:618-622` | 中 | `eco` profile 可验证收益 |
| 5 | `SkillLazyLoader` | 低 | system prompt 瘦身 |
| 6 | `ToolResultLifecycle`(后台摘要 + 归档存储) | 高 | 长会话可持续 |

**每步**: 必须过 `ruff check` + 全部既有测试 + 新测试 + 开关默认 False(step 2 除外,cache 默认 True 已在 config 里)。

---

## 八、与 Curator 的边界(避免职责重叠)

- `ToolResultLifecycle` **只剪最近活跃会话的 message 列表**,不管跨会话的历史归档(那是 Curator 的活)
- `ToolResultLifecycle` 归档时写 `session.metadata["archived_tool_results"]`,Curator 从那里读,不重复建索引
- `CacheOptimizer` 不依赖 Curator 的 working state,独立工作;Curator 的 `curator_build_context` 返回的 `AssembledContext` 作为 CacheOptimizer 的输入(Curator 保证 prefix 稳定,CacheOptimizer 保证实际打上 cache_control)

---

## 一句话

**先做 step 1(UsageTracker + 接线)和 step 2(CacheOptimizer)这两步就能吃掉 80% 的 token 收益**,剩下四步按配置默认 off 增量交付,每步都是一个独立 PR、独立测试、独立可观测指标。
