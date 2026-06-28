# Raven 🦞

> 围绕四大支柱设计的 Agent 框架:**智能上下文管理**、**主动性**、**Token 效率**、**Skill 自进化**。

[English](README.md) | 简体中文

Raven 是对 Agent 运行时的一次自底向上的重新设计 —— 基于经过实战验证的基座(Fork 自 MIT 协议的 [nanobot](https://github.com/HKUDS/nanobot) 项目),针对每个严肃 Agent 产品最终都会撞上的四大难题,给出有明确主张的解决方案:

1. **上下文管理 · Context Management** —— *Curator* 引擎自主决定哪些消息留在上下文窗口,其余无损归档,按需检索。
2. **主动性 · Proactivity** —— *Sentinel* 子系统与主 Agent 循环并行运行,监听事件,决定何时由 Agent 主动开口(且不惹人烦)。
3. **节省 Token · Token Efficiency** —— *TokenWise* 层的一组跨切面策略:Prompt 缓存优化、工具结果生命周期管理、智能模型路由、实时预算追踪。
4. **Skill 自进化 · Skill Self-Evolution** —— *SkillForge* 闭环:从对话中自动识别可复用模式,为 Skill 做版本管理与性能追踪,并基于执行反馈持续进化。

---

## 当前状态

**Pre-alpha**,活跃开发中。

| 层级 | 状态 |
|------|------|
| 基础 Agent 运行时(Fork 自 nanobot) | ✅ 可用 —— CLI、Channels、Tools、Cron、Providers |
| 核心抽象(`raven/core/`) | ✅ Tier 1 完成 —— 接口、事件总线、配置 |
| 特性支柱:Curator 上下文引擎 | 🚧 已设计,尚未实现 |
| 特性支柱:Sentinel 主动性 | 🚧 已设计,尚未实现 |
| 特性支柱:TokenWise 效率 | 🚧 已设计,部分实现 |
| 特性支柱:SkillForge 自进化 | 🚧 已设计,部分实现 |

实施计划见下文 [Roadmap](#roadmap)。

---

## 为什么是 Raven

大多数开源 Agent 框架都止步于 "LLM + 工具 + 循环"。这套在生产前够用,但一到生产就会遇到:

- 上下文越来越臃肿,窗口溢出,信息开始丢失 —— 于是做摘要,信息进一步丢失。
- 每一轮都在重复发送同样的 System Prompt、同样的 Skill 摘要、同样的工具定义 —— 烧 Token。
- Agent 只会被动等指令,从不说 "嘿,我发现部署卡住了" 或 "你让我提醒你的 X 来了"。
- Skill 是静态 Markdown 文件,遇到新的边缘 case 匹配不上,就永远静默失败。

Raven 针对这四个问题各给出正面解法。**四大支柱不是可有可无的附加项,它们就是这个框架本身。**

---

## 架构

```
┌──────────────────────────────────────────────────────────────────┐
│                            Event Bus                              │
│  (inbound, outbound, generic pub/sub for BusEvent / TriggerEvent) │
└─────┬───────────┬───────────┬──────────┬──────────┬───────────────┘
      │           │           │          │          │
┌─────▼─────┐┌────▼────┐┌─────▼────┐┌────▼────┐┌────▼─────┐
│  Channels ││  Agent  ││ Sentinel ││TokenWise││SkillForge│
│   Layer   ││  Loop   ││(proactive)││(token  ││(evolving)│
│           ││         ││          ││ hooks) ││          │
│ telegram  ││ tools   ││ monitors ││ cache  ││  stats   │
│ discord   ││ session ││ evaluator││ routing││  evolve  │
│ slack …   ││ memory  ││ nudges   ││ tracker││  detect  │
└───────────┘└────┬────┘└──────────┘└────────┘└──────────┘
                  │
         ┌────────▼─────────┐
         │  ContextEngine   │  ← 可插拔
         │                  │      legacy = 基线
         │   [legacy]       │      curator = 自主
         │   [curator]      │
         └────────┬─────────┘
                  │
         ┌────────▼─────────┐
         │   LLM Providers  │
         │ Anthropic / OAI  │
         │  Gemini / OR …   │
         └──────────────────┘
```

**设计原则:通过接口解耦。** 每个特性支柱都在 `raven/core/interfaces.py` 中以抽象基类形式定义,实现通过配置挂载。你完全可以设 `context.engine = "legacy"`、`sentinel.enabled = false`,此时 Raven 就等价于基础 Agent —— 也可以逐个开启支柱。

### 仓库结构

```
raven/
├── core/               # 抽象接口 + 通用事件总线
│   ├── events.py       # BusEvent, EventType, TriggerEvent
│   ├── event_bus.py    # EventBus (inbound/outbound + pub/sub)
│   └── interfaces.py   # ContextEngine, Monitor, SkillHandler, TokenStrategy
│
├── context/            # Curator —— 智能上下文管理
├── sentinel/           # Sentinel —— 主动监听器与 Nudge
├── token_wise/         # TokenWise —— 缓存、路由、裁剪、预算
├── skill_forge/        # SkillForge —— Skill 自动识别、进化、退役
│
├── agent/              # 基础 Agent 循环、工具、Skill、记忆
├── bus/                # 基础 MessageBus (InboundMessage / OutboundMessage)
├── channels/           # 平台集成(telegram, discord, slack, …)
├── cli/                # `raven` 命令行入口
├── config/             # 配置 schema(基础)+ Raven 特性块
├── cron/               # 定时任务
├── memory/             # 双层记忆(MEMORY.md + HISTORY.md)
├── providers/          # LLM Provider 适配
├── routing/            # 基于 PinchBench 基准的模型路由
├── session/            # 会话管理(append-only JSONL)
├── skills/             # 内置 Skill
├── templates/          # 默认 SOUL.md / USER.md / AGENTS.md
└── utils/              # 共享工具
```

---

## 快速开始

### 环境要求

- Python **3.11+**
- 至少一个 LLM Provider 的 API Key(Anthropic、OpenAI、OpenRouter、Gemini、DeepSeek 等)

### 安装

```bash
git clone https://github.com/EverMind-AI/raven.git
cd Raven
pip install -e .
```

需要 Channel 集成(Telegram、Discord、Slack、WhatsApp 等):

```bash
pip install -e ".[channels]"
```

开发环境(测试、Lint):

```bash
pip install -e ".[dev]"
```

### 初始化工作空间

```bash
raven onboard
```

会创建 `~/.raven/config.json`,并在 `~/.raven/workspace/` 下生成默认的 `SOUL.md`、`USER.md`、`AGENTS.md` 模板。

### 配置 API Key

编辑 `~/.raven/config.json`:

```json
{
  "providers": {
    "anthropic": { "api_key": "sk-ant-..." }
  },
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-6"
    }
  }
}
```

### 开始对话

```bash
raven agent -m "你好,你是谁?"
```

或交互模式:

```bash
raven agent
```

### 作为网关运行(对接聊天平台)

在配置中启用 Channel(例如 `channels.telegram.enabled = true` 并填入 token),然后:

```bash
raven gateway
```

---

## 配置

Raven 的配置在基础 Agent 配置之上增加了四个特性块。**所有新特性默认关闭** —— 新装 Raven 的行为与基础 Agent 完全一致,直到你主动启用。

```json
{
  "agents":   { "defaults": { "model": "anthropic/claude-opus-4-6" } },
  "channels": { "telegram": { "enabled": false } },
  "providers": { "anthropic": { "api_key": "sk-ant-..." } },

  "context": {
    "engine": "legacy",
    "fast_path_threshold": 0.60,
    "curator_model": "gemini-2.5-flash"
  },

  "sentinel": {
    "enabled": false,
    "monitors": [],
    "nudge_policy": {
      "max_nudges_per_hour": 3,
      "quiet_hours": [23, 7]
    }
  },

  "token_wise": {
    "enabled": true,
    "usage_tracking": true,
    "cache_optimization": true,
    "smart_routing": { "enabled": false }
  },

  "skill_forge": {
    "enabled": false,
    "stats_tracking": true,
    "auto_detect": false,
    "auto_evolve": false
  }
}
```

完整字段定义见 `raven/config/raven.py`。

### 启用 Skill 自进化

完成的 `user → assistant` 轮次会以 session 为单位攒在本地抽取 pipeline 的
缓冲里,**先做跨轮的任务边界检测,再决定要不要蒸馏**。每一轮都会触发一次
轻量分类器,问一句"用户是不是刚开了个新任务";只有检测到边界(或 session
结束)时,缓冲里的这一段才会被压成 `AgentCase`。太短的对话、没有工具调用
的纯聊天、还没结束的轮次,会在任何 LLM 调用之前直接丢掉。低于质量门槛的
case 停在这里,通过的继续进 skill 抽取。结果落到
`<workspace>/.cache/skills.db`,物化的 `SKILL.md` 写到
`<workspace>/skills/everos/<id>/`,本地 BM25 池自动 pickup。无需外部
服务,只用 SQLite + 现有 LLM provider。

```json
{
  "skill_forge": {
    "enabled": true,
    "evolve_model": "claude-opus-4-6",
    "detect_model": "gemini-2.5-flash",
    "everos": {
      "enabled": true
    }
  }
}
```

Pipeline 用到两个模型:

- `skill_forge.evolve_model` —— 重量级 LLM,负责把 `AgentCase` 蒸馏成
  skill、以及对已有 skill 做重写。不设置时回落到当前 agent model;想要
  更高质量的重写就在这里钉一个更强的模型。
- `skill_forge.detect_model` —— 每轮 boundary detector(多轮任务切分)
  用的轻量分类器。因为它会在每个累计轮次上运行,默认 `gemini-2.5-flash`
  这种小而快的模型是有意为之。

### 配置媒体生成(图片 / 语音 / 视频)

三个媒体工具通过 [OpenRouter](https://openrouter.ai) 生成媒体,**按工具
逐个 opt-in**:只有当你在 `tools.media.<tool>` 下给某个工具配了 `model`
或 `api_key`,它才会暴露给 agent。**仅把 OpenRouter 配成 chat provider
并不会启用它们** —— 不主动配置,agent 永远看不到图片/语音/视频工具。

```json
{
  "providers": { "openrouter": { "api_key": "sk-or-..." } },
  "tools": {
    "media": {
      "image":  { "model": "google/gemini-2.5-flash-image" },
      "speech": { "model": "openai/gpt-audio-mini" },
      "video":  { "model": "kwaivgi/kling-v3.0-std" },
      "proxy": null,
      "output_subdir": "generated"
    }
  }
}
```

- **Key** —— 已配置的工具若没单独设 key,会回落到
  `providers.openrouter.api_key`,所以通常只需配一个 `model` 就能开启某个
  工具。想用独立的 key,就在 `tools.media.<tool>.api_key` 单独覆盖。
- `image_generate` —— 文生图(及图片编辑),用 Nano Banana
  (`google/gemini-2.5-flash-image`)。在 workspace 下保存 PNG。
- `text_to_speech` —— 语音合成,用 `openai/gpt-audio-mini`。零依赖输出
  WAV;mp3/opus/flac 需要 PATH 上有 `ffmpeg`,没有时回退 WAV。
- `video_generate` —— 文生视频,用 Kling(`kwaivgi/kling-v3.0-std`),
  异步任务、耗时较长,且**需要 OpenRouter 账户开通后付费 / credits**。

生成的文件落在 `<workspace>/<output_subdir>`(默认 `generated/`)。设置
`tools.media.proxy` 可让媒体调用走 HTTP/SOCKS 代理。

---

## 四大支柱详解

### 1. 上下文管理 —— *Curator* 引擎

`ContextEngine` 抽象基类定义了可插拔的上下文层。规划了两个实现:

- **`legacy`** *(默认)* —— 基础 Agent 的 `ContextBuilder` + Consolidator。当 Prompt 接近上下文窗口上限时,老消息会被摘要并移出活跃上下文。
- **`curator`** *(进行中)* —— 一个自主管理自身上下文的 Agent。在压力下,它会将消息无损归档到磁盘、需要时再检索回来,并通过 11 个内部工具(`curator_check_budget`、`curator_archive_messages`、`curator_retrieve_archived`、`curator_build_context` 等)组装最终窗口。采用快慢双路设计:
  - **Fast Path**(历史 < 60% 预算):零 LLM 直通,仅注入工作态。
  - **Slow Path**(压力态):运行一个小模型 Agent 循环(默认 `gemini-2.5-flash`)决定保留什么。
  - **Fail-Safe**:若 Slow Path 的 LLM 失败或超时,一个确定性 Python 降级方案会产出有效上下文。

设计文档给出的基准目标:
- **PinchBench 连续评测,16K 窗口**:Legacy 86% → Curator 96% 整体成功率。
- **DeepResearch-Bench-II,32K 窗口,10 项研究任务**:Legacy 43% → Curator 50% 整体成功率。

### 2. 主动性 —— *Sentinel* 子系统

Sentinel **与主 Agent 循环并行**运行,订阅事件总线,决定 Agent 何时无需提示即可主动发声。关键组件:

- **Monitors**(`Monitor` ABC)—— 订阅事件类型,条件触发时产出 `TriggerEvent`。规划中的监听器:`IdleMonitor`、`CronMonitor`、`WorkspaceMonitor`、`MemoryMonitor`、`FollowUpMonitor`、`TaskMonitor`。
- **Evaluator** —— 双层决策。规则层以零成本处理高确定性 case;可选的 LLM Evaluator 用小模型(`gemini-2.5-flash`,3 秒超时)判定模糊场景。
- **Nudge Policy** —— 反骚扰护栏:`max_nudges_per_hour`、`quiet_hours`、`min_interval_seconds`、被 Dismiss 后的冷却。
- **注入** —— Nudge 通过事件总线以普通 `InboundMessage`(`sender_id="sentinel"`)进入 Agent 循环,被路由为主动式上下文。

### 3. Token 效率 —— *TokenWise* 层

TokenWise 是一组跨切面的 `TokenStrategy` 钩子,而非单一模块。每个策略都可独立开关。

| 策略 | 作用 | 典型节省 |
|------|------|----------|
| `UsageTracker` | 记录每次 LLM 调用的 tokens 和成本 | —(可观测性) |
| `CacheOptimizer` | 在 Anthropic 请求中合理放置 `cache_control` 断点 | input 成本最多降 75% |
| `ToolResultLifecycle` | 三阶段裁剪:保留最近 / 摘要中段 / 归档远端 | 30-60% 历史 tokens |
| `SmartRouter` | 简单任务路由到廉价模型(`haiku`、`gemini-flash`) | 单请求降 40-70% |
| `SkillLazyLoader` | 只注入与当前消息相关的 Skill 摘要 | 10-30% system prompt |
| `BudgetAlerter` | 按会话/按天的可配置支出上限告警或阻断 | —(护栏) |

### 4. Skill 自进化 —— *SkillForge*

SkillForge 是一个闭环:`Detect → Create → Execute → Feedback → Evaluate → Evolve → Retire`。

- Skill 存放在 `~/.raven/skills/<skill-name>/SKILL.md`,YAML frontmatter 扩展了 `version`、`stats`、`evolution_log`。
- **Detect**:会话结束时,小模型判断该对话是否包含值得保存的可复用多步流程。
- **Draft → Active 门槛**:自动创建的 Skill 以 `draft` 状态起步,直到至少成功一次才进入 Skill 摘要,避免噪声。
- **Execute 追踪**:每次 Skill 调用都记录 `turns_used`、`tokens_consumed`、`outcome` 和 `user_feedback`(显式或隐式)。
- **Evolve**:当 `success_rate` 在 `>= 10` 次调用内跌破阈值(默认 0.70),由强模型(`claude-opus-4-6`)重写该 Skill —— 保留可用逻辑并追加改进;版本递增,旧版本快照保留。
- **Retire**:闲置 `retirement_idle_days` 天(默认 90)的 Skill 被标为 `deprecated`;再闲置 30 天 → `retired`(移入归档)。

---

## Roadmap

Raven 分六个 Tier 构建,按依赖关系与风险排序:

| Tier | 范围 | 状态 |
|------|------|------|
| **1** | 骨架:仓库 Fork、核心接口、事件总线、配置 | ✅ 完成 |
| **2** | 低风险高价值:使用量追踪、缓存优化、预算告警、Skill 统计 | 🚧 进行中 |
| **3** | Curator Fast Path + Archive + 11 内部工具 + Slow Path | 规划中 |
| **4** | TokenWise 高阶:工具结果生命周期、智能路由(规则层)、Skill 懒加载 | 规划中 |
| **5** | SkillForge 闭环:识别、自动创建、执行追踪、进化、退役 | 规划中 |
| **6** | Sentinel:Monitors、Evaluator、Nudge Policy、注入 | 规划中 |

每个 Tier 的实施细节详见 [development docs](docs/)(补充中)。

---

## 开发

### 运行测试

```bash
cd Raven
PYTHONPATH=. pytest -v
```

Tier 1 提供 24 个测试,覆盖包骨架、事件总线 pub/sub 契约、四个抽象接口、配置 schema。

### 布局约定

- **`core/interfaces.py` 不放任何运行时逻辑** —— 只保留 ABC、dataclass、类型别名。避免循环导入。
- **特性模块之间不直接互相 import** —— 通过事件总线或 Agent 循环中的显式交接通信。
- **Fail-safe 是强制要求** —— 每个调用 LLM 的组件都必须有确定性降级。任何特性都不得导致主 Agent 循环崩溃。
- **新特性默认关闭** —— 任何新增能力都以 `enabled = false` 发布;仅低成本、成熟的策略(缓存优化、使用量追踪)默认开启。

### 代码风格

- Python 3.11+,合适处使用 `from __future__ import annotations`
- Ruff Lint(`ruff check raven tests`)
- 全量类型标注(`Literal`、`Protocol` 按需使用)
- 测试使用 `pytest` 搭配 `pytest-asyncio`(asyncio mode `auto`)

---

## 致谢与许可

Raven 采用 MIT 许可。基础 Agent 运行时(涉及 `raven/agent/`、`raven/bus/`、`raven/channels/`、`raven/cli/`、`raven/config/{loader,paths,schema}.py`、`raven/cron/`、`raven/memory/`、`raven/providers/`、`raven/routing/`、`raven/session/`、`raven/skills/`、`raven/templates/`、`raven/utils/`)来自 HKUDS 的 MIT 许可项目 [nanobot](https://github.com/HKUDS/nanobot),完整许可见 `LICENSE`。

四大特性支柱(`context/`、`sentinel/`、`token_wise/`、`skill_forge/`,以及 `core/` 接口与配置扩展)是 Raven 的新增内容。

接口设计参考了生态内若干项目 —— [hermes-agent](https://github.com/NousResearch/hermes-agent)(Nous Research)、[Letta / MemGPT](https://github.com/letta-ai/letta)、[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk) 等。

---

## 贡献

Raven 处于 pre-alpha 阶段,API 会持续演进。如果你有兴趣贡献:

1. 动手前先开 Issue,我们对齐一下方向。
2. 遵循上面的布局约定。
3. 补测试 —— Tier 1 的 `tests/test_tier1_skeleton.py` 是模板。
4. 在模块 docstring 中说明其契约,并同步更新 README 对应章节。

---

*Raven 由 EverMind 打造。*
