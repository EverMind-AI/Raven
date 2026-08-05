# RavenX → Raven `ui-webui` 迁移与开发交接

> 本目录是把 **RavenX web app** 迁入 Raven 仓库、并将 Raven 深度化为主 agent 的工作基线。
> 本文是**权威交接文档**:讲清"这里有什么、为什么这么放、接下来怎么做"。后续在 Raven 项目里继续开发时,先读本文。

---

## 0. 溯源(provenance)

- 来源:`/Evermind/sh_evermind/xuedizhan/RavenX_demo`(AgentScope 2.0 `v2.0.4` 的 copy + RavenX 层),`main @ c84fd1a5`(2026-07-23)。
- 方式:**非破坏性复制**。RavenX_demo 保持原样,是历史来源与 diff 基准。**未删除源**。
- 排除:`node_modules/`、`dist/`、`__pycache__/`、`*.pyc`、`service/workspaces/`(运行时会话数据)。

---

## 1. 目标(用户明确的五条需求)

1. **改 Raven 源码**:把多智能体编排(sub-agent DAG)接入 Raven(见需求 4 的解耦形态,不再是"焊进内核")。
2. **改 Raven gateway**:加入一个 **web app channel**。
3. **web app 作为 Raven 的 UI**:方便用户交互与配置 Raven。
4. **DAG 与 Raven 内核解耦,作为可选的独立子系统接入**(2026-07-23 定):sub-agent DAG 编排**不融入** Raven 内核,而是移植为一个**自带编排 + 执行、与 Raven 原生 subagent 子系统解耦**的独立子系统,以 **Raven 工具**形式(`run_subagent_dag` 等)供主 agent 调用,且**可选**(可启用/禁用)。**web app 默认启用**该 DAG 工具。它与 AgentScope 工具语义脱钩(入口用 Raven 工具接口暴露),但内部节点执行由 DAG 子系统自理,**不走** `Origin.SUBAGENT`/`scheduler`(有意与原生 subagent 并存两套)。落点见 §5 P3、§9.1。
5. **增强 Raven 原生 subagent 的第三方 agent 调用能力**(2026-07-23 定):Raven 原生 subagent 现在①一次只能调用一个 sub-agent、②只能是 raven agent(不支持第三方)。需加入调用第三方 agent 的能力 —— CLI 方式(claude code / codex)与 OpenAI-API 方式(mirothinker)。此为 Raven 内核能力,**独立于**需求 4 的 DAG 子系统(DAG 子系统自带各自的节点执行器)。落点见 §5 P3b、§9.2。

约束(已拍板):
- **多租户 = 锁死单用户**。全局一个常驻 raven runtime,单身份/单记忆(EverOS),会话间仅用 conversation/session 隔离上下文。
- **transport = gateway 内置 WS,web 后端作客户端**(注:见 §4 的架构决策,若走同进程内嵌则 WS 退化为 in-process 适配器;当前既定方向是 gateway + WS 独立进程)。
- py3.12(Raven)vs py3.11(RavenX)依赖冲突"可解,不作约束"。

---

## 2. 架构决策记录(ADR)

### 2.1 桥接模型已到天花板
原 `RavenBridgeAgent` 每轮 spawn 一个一次性 `raven agent -m`,跑完即退。**hook / cron / sentinel / 后台 subagent 这些主动行为结构性无法暴露** —— 它们只在长驻 runtime(`raven gateway` 或交互式 REPL)里、由持续存活的 `AgentLoop.run()` + spine 调度器驱动,一次性 `-m` 从不启动它们。详见 `raven-gateway-integration-analysis.md` §1、§4。

> 澄清:cron/sentinel 这类**条件触发可暴露**(它们注入可观测的 turn);但 **hook 本身不发事件**,外部只能看到 hook 对 turn 输出的**下游效果**(改写过的 `Text`/短路回复 + `Notice`)。

### 2.2 为什么最终是"A(gateway + web channel)"而非"B(in-process 嵌入)"
曾评估两条路:
- **A**:raven 常驻 gateway,web 作为一个 channel/outlet 接入。
- **B**:ravenx 后端 import raven,进程内常驻一个完整 runtime。

B 唯一的理由曾是"raven 要够到 AgentScope 进程里的 DAG runner"。**一旦 DAG 以工具形式活在 Raven 一侧(无论原生融入,还是需求 4 现定的"解耦独立子系统"),AgentScope 进程里就没有 raven 需要够到的东西了** —— 于是干净的 **A** 成为正解,web app 退化为 raven 的纯 UI。这是本次方向的关键自洽点。注:DAG 是"原生融入内核"还是"解耦独立子系统"不改变 A/B 结论;现已确定走**解耦的可选独立子系统**(见 §1 需求 4、§5 P3、§9.1)。

### 2.3 迁移不是"搬文件",是"换后端内核"——必须分层看
web app 依赖一整套 **AgentScope 语义的 REST 面**(前端 api client 实测调用):

```
/agent(+/schema/v2)  /chat  /credential(+/schemas)  /model  /sessions
/subagent(+/presets)  /knowledge_bases/*  /schedule  /tts-model  /workspace/{mcp,skill}
```

| 层 | 现状 | 可移植性 | 迁移动作 |
|---|---|---|---|
| **前端** React | 渲染事件流 + 调 REST | ✅ 高,框架无关 | 已移入 `frontend/`,基本不动 |
| **管理/配置 REST API** | AgentScope `app/`(在 RavenX_demo 的 `src/agentscope/app/`) | ❌ 低,是 AgentScope 核心产品面 | 短期保留 AgentScope 作 pip 依赖;逐页迁移;不并源码 |
| **agent 执行内核**(`/chat/`+SSE) | AgentScope Agent / RavenBridgeAgent | ✅ 可替换 | 换成"连持久 gateway 的 raven 客户端" |

**推论**:短期**不可能**退役 AgentScope 后端而不重写一个大 REST API。务实路径是 **B3 分阶段**(见 §5):保留 AgentScope `app/` 作 web BFF(pip 依赖),只把 `/chat/` 的 agent 换成 raven,复用已写好的 spine→AgentScope 事件翻译,使**前端几乎零改动**,DAG viz 也照旧。

> 另注:现有配置页配的是 AgentScope 的 model/credential/KB,**不等于 Raven 的配置**(channels/cron/sentinel/skills/memory/`~/.raven/config.json`)。"配置 Raven"这条需要**新做面向 Raven 概念的配置页**,不是复用旧页重指向。

> Update (2026-07-28): the chat model picker is now Raven-native and per-session.
> Options come from `/raven/providers` (`~/.raven/config.json`), and the choice is
> stored in raven's own session store as `Session.metadata["model"]` via
> `raven.session.model.set`. AgentScope's `chat_model_config` no longer routes a
> turn: it holds an inert placeholder until the user picks a model and a copy of
> the pick afterwards, kept non-null only to satisfy the check in `_chat.py`. In
> gateway mode `get_model` is replaced by a stub, so no AgentScope credential
> record is required for a chat turn. `agents.defaults.model` stays the fallback
> for sessions with no pick, and for cron / IM / Sentinel.

---

## 3. 本目录结构

```
ui-webui/
├── frontend/          # React+TS+Tailwind v4+React Flow+i18next(移自 examples/web_ui/frontend)
├── service/           # Python web 服务(移自 examples/agent_service)
│   ├── main.py                 #   create_app 接线(gateway 唯一后端)
│   ├── raven_gateway_agent.py  #   gateway WS 客户端(唯一 chat 后端)
│   └── skills/                 #   sub-agent DAG 编排 skill
├── package.json / pnpm-*.yaml  # pnpm workspace(仅前端)
└── docs/
    ├── MIGRATION.md                            # 本文
    ├── raven-gateway-integration-analysis.md   # ★ Raven 侧接入点的代码级勘探(WebOutlet/build_web/gateway seam,含 file:line)
    ├── RAVENX_CONVENTIONS.md                   # 原 RavenX CLAUDE.md(前端 i18n/React Flow/stdin 投递等约定与坑)
    ├── specs/  (14)   # 每个 RavenX 功能的设计文档(见 §7)
    └── plans/  (14)   # 对应的实施计划
```

---

## 4. Raven 侧集成接入点(勘探结论摘要)

完整版见 `raven-gateway-integration-analysis.md`。要点:

- 唯一长驻多源守护进程 = **`raven gateway`**(`raven/cli/gateway_commands.py:114`,内层 `run()` `:355`),已拥有 `agent.run()` keep-alive + cron + heartbeat + sentinel + 单例锁。
- spine = `Scheduler`(`raven/spine/scheduler.py:327` `submit`)+ `DeliveryHub`(`raven/spine/delivery.py:81`,**按 `source.channel` 路由,非广播**)+ `Outlet`(`delivery.py:53-64`)。
- **没有现成 web/HTTP/WS gateway** —— 要新增传输,但**不用碰 spine**。
- 最干净接入:新增 **`WebOutlet`**(`Outlet` + `SupportsStreaming`,照抄 `TuiOutlet` `raven/tui_rpc/spine.py:116`)+ 复用 `SubscriptionEmitter`(`raven/tui_rpc/subscriptions.py:40`)+ `build_web(...)` 装配(照抄 `build_gateway` `raven/cli/_gateway_spine.py:113`),挂进 `raven gateway`。
- 入站:`TurnRequest(origin=USER, source=Source(channel="web", ...))` → `scheduler.submit`。
- **要收 cron/sentinel/heartbeat 输出**:因 hub 按 `source.channel` 路由,必须让这些生产者的 deliverable 的 `source.channel == "web"` —— 用 `make_on_cron_job(default_channel="web")`(`_cron_handler.py:110`)、`dispatcher.set_post(web_hub.post)`(`.../sentinel/executor/dispatcher.py:73`)、heartbeat 目标 `channel="web"`、`agent.subagents.set_submit(web_scheduler.submit)`。

---

## 5. 分阶段路线图(B3)

每阶段可独立验证。

- **P0 机械迁移** —— ✅ **已完成**:`ui-webui/` 已导入 Raven 仓库并 commit(裸导入,未带 RavenX_demo 上游历史;溯源见 §0)。
- **P1 gateway web channel** —— ✅ **核心完成 + 端到端验证**(分支 `feat/gateway-web-channel`)。Raven 侧加了 `WebOutlet`/`build_web`(`raven/web_rpc/`)+ aiohttp WS server,挂进 `raven gateway`(`gateway.web.enabled`);proactive 已改投 web spine。验收达成:本地 WS 端点能 submit/订阅并流式回事件(实测)。
  - **已完成**:WS 通道 + 流式(reply);proactive **submit** 改投 web;subagent 结果 + heartbeat 干净落到 web 订阅。
  - **⏸ 结构性延后(非本阶段能闭环)**:cron/sentinel 的 fan-out 落到 web 订阅 —— 现走 IM 式 `resolve_cron_delivery`,需把 web 做成"一等投递通道"(偏 P4 的"web 作为被识别通道");详见 P1 计划 §2。
- **P2 内核切换** —— ✅ **完成 + 端到端验证**。`service/` 加 `RAVEN_GATEWAY=1` 开关(`raven_gateway_agent.py`:`GatewayClient` + `RavenGatewayAgent`),`/chat/` 走**连持久 gateway 的 WS 客户端** + spine wire→AgentScope 事件翻译。实测:HTTP agent→session→`/chat`→SSE 收到 `REPLY_START/THINKING_*/TEXT_*/REPLY_END`,前端零改动契约(§10)满足。含加固:断连不挂起、shutdown 清理。
  - **⏸ 结构性延后 = P2.3**:proactive/DAG 的 `CUSTOM` 事件翻译(subagent 实例面板 + DAG viz)。**当前 Raven 尚未产生这些事件**(DAG 是 P3、第三方实例是 P3b),无源可译 —— 待 P3/P3b 落地后在翻译层补。
- **P3 DAG 作为解耦的可选独立子系统(需求 4)** —— 把 `service/agentscope/subagent/` 的 DAG 编排移植为一个**与 Raven 内核解耦的独立子系统**:自带编排 + 执行(含各自的节点执行器),以**可选 Raven 工具**(`run_subagent_dag` 等)接入,主 agent 通过工具调用;**web app 默认启用**。**不融入** `raven/agent/subagent/`,**不复用** `Origin.SUBAGENT`/`scheduler`(有意保留为独立子系统、并存两套)。**只与 AgentScope 工具语义脱钩**:入口用 Raven 工具接口重写,而非把 AgentScope 的 `Toolkit`/`ToolResponse`/`Msg` 搬进来;输出经事件翻译回填 viz。详见 §9.1。
- **P3b Raven 原生 subagent 第三方增强(需求 5)** —— ✅ **完成 + 真 claude 验证**。`raven/agent/subagent/backends/`(`RavenLoopBackend` 默认 + `CliAgentBackend` claude/codex + `OpenAIApiBackend` mirothinker)+ `subagents.third_party` 配置 + `SpawnTool.agent` 参 + preset;并发复用现有 spawn。留:有状态/resume 实例。详见 §9.2。
- **P3 DAG 解耦子系统(需求 4)** —— ✅ **核心完成 + 真 claude DAG 验证**。`raven/agent/subagent_dag/`(移植核心 + `LocalFileBackend` + 原生 runner + `run_subagent_dag` 工具),与 spine 解耦,节点执行复用 P3b 适配层,AgentLoop 按配置注册。实测:真 2 节点 claude DAG(A→B 文件传递)。留:web 侧 live viz 事件(接 P2.3)、durable/stateful 实例。详见 §9.1。
- **P4 Raven 配置面** —— 🟩 **架构已立 + 首页面(第三方 subagent)端到端完成**。因 web service 环境无 raven,配置管理落在 **gateway**(有 raven + 活 runtime):`update_subagents.py`(校验+原子写)+ `web_rpc/methods_config.py`(`raven.subagents.{list,set,presets}`,set **热应用**到在跑的 AgentLoop,免重启)← service REST 代理 `raven_config_routes.py` ← 前端页 `pages/subagent/`(路由 `/subagents`;旧 AgentScope 概念的重复 subagent 页已删,两页合一)。全栈 curl 验证过。**剩余**:同模式做其余 Raven 概念页(channels/cron/skills/memory)+ 旧 AgentScope 概念页按需退役。注:`load_runtime_config`→`Config`(有 gateway.web + subagents);`load_raven_config`→`RavenConfig`(不同,无 subagents)。

---

## 6. `raven_bridge_agent.py` 现状(历史记录)

> **已废弃**:bridge 模式与 `raven_bridge_agent.py` 已删除,chat 只走 gateway
> (`raven_gateway_agent.py`)。本节保留作迁移期的历史记录。

P2 会取代这个文件,但其事件翻译逻辑是 P2 的直接蓝本。本次加固 4 处:
1. **多轮上下文**:argv 加 `--session cli:{sid}`(每个会话对应一个固定 raven 会话,首轮建、后续带历史 resume)。
2. **node PATH 去硬编码**:`_newest_node_bin()` 用 glob + 版本感知排序动态发现 nvm node。
3. **dispatch task 预览鲁棒化**:`_classify` task 正则改 `"([^"]*)`,兼容 raven 日志 200 字符截断。
4. **失败可见化**:非零退出且无最终文本时回显最后 15 行合并输出(环形 `tail` 缓冲)。

> 关于结构化输出:raven CLI **没有** codex 式 `--json`(仅 `--markdown`/`--logs`)。桥接靠解析 `--logs` 人类文本。P1/P2 用 spine 事件后即彻底摆脱日志解析。

---

## 7. 设计文档索引(`docs/specs/` + `docs/plans/`)

RavenX 已实现功能的完整设计(强烈建议 P3/P4 前通读相关项):

| 主题 | spec 日期前缀 |
|---|---|
| CLI sub-agent 调度(dispatch 基础) | 2026-07-15-cli-subagent-dispatch |
| sub-agent 实例(CRUD + /subagents 页) | 2026-07-16-subagent-instances |
| 可编辑 credential/model | 2026-07-16-editable-credential-models |
| 会话工作目录 | 2026-07-16-session-working-directory |
| sub-agent id 机制 | 2026-07-17-subagent-id-mechanisms |
| **sub-agent DAG 编排**(P3 核心) | 2026-07-17-subagent-dag-orchestration |
| handle & Codex 精化 | 2026-07-18-subagent-handle-codex-refinements |
| 文件式 prompt 投递 | 2026-07-19-subagent-file-prompt-delivery |
| DAG 节点输出文件 | 2026-07-19-dag-node-output-files |
| OpenAI-API sub-agent(MiroMind) | 2026-07-20-openai-subagent-miromind |
| sub-agent DAG skill | 2026-07-20-subagent-dag-skill |
| **DAG 实时可视化**(viz 复用关键) | 2026-07-21-subagent-dag-live-visualization |
| **可持久化运行态**(reload/重连存活) | 2026-07-21-durable-subagent-run-state |
| 文件交付工具 | 2026-07-21-file-delivery-tool |

`plans/` 下有一一对应的实施计划。

---

## 8. 工程注意事项

- **git 历史**:本次是裸 copy。若要保留你在 web_ui/agent_service 上的提交历史,在 Raven 仓库用
  `git filter-repo --path examples/web_ui --path examples/agent_service`(在 RavenX_demo 的一个克隆上)抽出带历史子集,再 `git subtree add` 进 `ui-webui/`。不要用 `cp` 顶替历史。
- **依赖**:`service/` 依赖 `agentscope` 包(即 RavenX_demo 的 `src/agentscope`)。要么 pip 安装 agentscope,要么把 RavenX_demo 作可编辑依赖。前端/BFF 是 pnpm workspace。
- **raven 可编辑安装**(改 Raven 源码即时生效):`uv tool install --editable /Evermind/sh_evermind/xuedizhan/Raven --force`(当前 raven 是 `uv tool install` 冻结副本,改源码不生效)。
- **运行时(旧栈)**:conda env `ravenx`;redis(docker `ravenx-redis`:6379);service `uvicorn main:app --port 8001`;前端 `pnpm dev :5173`。详见 `start_webapp.sh` 与 `docs/RAVENX_CONVENTIONS.md`。
- **两套 sub-agent 系统(方向已改,2026-07-23)**:早期设想 P3 让 Raven 原生 subagent 与 RavenX DAG **合一**;**现已改为** —— DAG 作为**解耦的可选独立子系统**保留(有意与原生 subagent **并存两套**,不合并),原生 subagent 另行增强第三方 agent 调用(见 §1 需求 4/5、§5 P3/P3b、§9)。
- **前端坑**(见 RAVENX_CONVENTIONS.md):i18n JSON 用定点文本编辑(勿 `json.dump` 重排);React Flow 按值签名 memo、勿每渲染新建节点数组、计时放叶子组件。
- **前端验证**(无 JS 单测):`pnpm -C frontend lint`(0 error)+ `pnpm -C frontend build`。

---

## 9. 需求 4 / 5:DAG 解耦为独立子系统 + 原生 subagent 第三方增强(P3 硬约束)

### 9.1 需求 4:DAG 作为与 Raven 解耦的可选独立子系统

**要解决的问题**:`service/agentscope/subagent/` 里的编排工具是 **AgentScope 工具**——它们继承/返回 AgentScope 的类型(`ToolResponse`、`Msg`、`Toolkit` 注册),由 AgentScope 的 agent loop 调用。直接搬进 Raven 会把 AgentScope 工具运行时一并拖入,并与需求 4"解耦"相悖。

**目标形态(2026-07-23 定)**:DAG 编排作为一个**独立子系统**移植进 Raven ——

- **自带编排 + 执行**:保留 RavenX 的 DAG runner / 图 / placeholders / 节点执行器 / 持久态,**不复用** Raven `Origin.SUBAGENT` + `scheduler`,**不融入** `raven/agent/subagent/`(有意与原生 subagent 并存两套)。
- **以可选 Raven 工具接入**:入口(`run_subagent_dag` 等)用 **Raven 工具接口**重写(见 `raven/agent/` 工具定义 + `raven/plugin/` 贡献机制),与 AgentScope 工具语义脱钩;主 agent 把它当普通工具调。
- **可选 + web 默认启用**:通过 config / 插件开关控制;web app 的默认配置启用它。
- **viz 保住**:DAG 子系统的进度 / 实例事件经 spine→AgentScope 事件翻译层出到前端,viz 与实例面板零改动。

| RavenX(AgentScope 工具) | Raven 落点(需求 4) |
|---|---|
| `run_subagent_dag` 等编排工具入口 | Raven 原生工具接口重写(可选,web 默认开) |
| DAG runner / scheduler / 状态 / placeholders | **移植为独立子系统自带**(不复用 Raven scheduler) |
| 节点执行器(raven / claude code / codex / mirothinker) | 随子系统移植;第三方执行器与需求 5 共享适配层 |
| 实例注册表 / 进度事件 | 子系统事件源 → 翻译层出 AgentScope viz 事件 |
| 文件式 prompt 投递 / 节点输出文件 / 文件交付 | Raven workspace 服务承载 |

> 判定标准:P3 完成后,DAG 子系统作为可选组件可整体启停;`import agentscope` 不出现在 Raven 侧的 DAG 入口工具实现里(AgentScope 仅存在于 web BFF `service/` 的事件翻译与 REST 面)。

### 9.2 需求 5:Raven 原生 subagent 的第三方 agent 调用

**现状**:Raven 原生 subagent(`raven/agent/subagent/`)只会 spawn **raven 自己的** agent,不支持第三方。根因在 `manager.py:146` 的 `_run_subagent_inner`:它**写死**了"子 agent = 一个 in-process raven loop"(自建 `ToolRegistry` + 反复 `provider.chat_with_retry`,`:156-224`)。并发结构本身已具备(`spawn()` fire-and-forget + `Semaphore(max_concurrent=4)`),只是执行后端唯一。

**目标**:给原生 subagent 加入第三方 agent 调用(CLI:claude code / codex;OpenAI-API:mirothinker),复用 RavenX 已实现的执行器思路。

**确定实现方案(2026-07-23,领导确认)—— 落点在 `raven/agent/subagent/`,核心改 `manager.py`**:

1. **引入执行器(后端)抽象**:把"子 agent 怎么执行"从写死的 raven loop 抽成可切换后端。定义一个 `SubagentBackend` 协议(建议新增 `raven/agent/subagent/backends/`):
   - `RavenLoopBackend` —— 现 `_run_subagent_inner` 的 raven-loop 逻辑平移进来(默认后端,行为不变)。
   - `CliAgentBackend` —— shell 出去调 claude code / codex:文件式 prompt 投递 + 两种 id 策略(`provisioned`/`derived`)+ transcript 解析(正则 / `parse_codex_jsonl`),移植自 `ref:_tool.py` / `ref:_transcript.py`。
   - `OpenAIApiBackend` —— httpx 调 mirothinker:读原始 JSON + 客户端侧 `messages[]` 历史重放,移植自 `ref:_openai_tool.py`。
2. **`_run_subagent_inner` 改为**:按"要调哪个 agent"选后端并委托;不再直接跑 raven loop。
3. **`spawn()` 的并发/回注结构保持不变**(`manager.py:74` fire-and-forget + `Semaphore(4)` + `_announce_result` 经 `Origin.SUBAGENT` 回注,`:279`)——第三方调用因此**自动获得后台并发能力(默认 4,可调)**。这就是并行方案 C 的"原生 spawn 并行"路径。
4. **工具层配套**(另一文件,`raven/agent/tools/spawn.py:25` `SpawnTool`):入参从 `{task, label}` 扩为可指定"调哪个 agent"(`agent`/`type` + 第三方 agent 配置清单);默认不填 = raven loop(向后兼容)。
5. **第三方 agent 注册来源**:一份"已配置第三方 agent"清单(命令模板 / API 配置),读取来源与格式在 P3b 计划里定(候选:`~/.raven/config.json` 下新增节 或复用 dispatch registry)。

**共享底座**:CLI adapter + OpenAI-API adapter 与 §9.1 的 DAG 子系统**共用同一第三方适配层**;区别只在调度——需求 5 走 `SubagentManager` 后台并发(**复用** `Origin.SUBAGENT`/`scheduler`),DAG 走自带 ready-set 调度(**不走** scheduler)。

> 详细实施步骤见 `docs/plans/2026-07-23-p3b-native-subagent-third-party.md`。

### 9.3 勘探落点(2026-07-23,已补全)

以下 file:line 分别指向 `service/agentscope/subagent/`(RavenX 源,记作 `ref:`)与 `/Evermind/sh_evermind/xuedizhan/Raven/raven/`(Raven 源)。

**(a) RavenX DAG 的 AgentScope 耦合面(要脱钩的就这些,很窄)**
- 工具外壳:`ToolBase` / `ToolChunk` / `TextBlock` / `ToolResultState` —— `ref:_tool.py:10,16-17`、`ref:_openai_tool.py:9-17`、`ref:_dag/_tool.py:7-13`、`ref:_dag/_runner.py:10-11`。
- 权限自动放行:`PermissionDecision(behavior=ALLOW)` —— `ref:_tool.py:489`、`ref:_dag/_tool.py:220`。
- 文件/执行后端(duck-typed):`BackendBase`/`LocalBackend` 的 `exec_shell(argv,cwd,timeout)->{stdout,stderr,exit_code,ok()}`、`read_file`/`write_file`(bytes)、`file_exists`、`join_path`、`abspath(path,cwd=)` —— `ref:_tool.py:339-377`、`ref:_dag/_store.py:49,89-112,135`、`ref:_dag/_render.py:105,166`。
- **可直接搬的纯核心(零 AgentScope import)**:`_graph.py` / `_placeholders.py` / `_paths.py` / `_render.py` / `_store.py` / `_projection.py` / `_errors.py`。只有 `_runner.py`(message/logging)与三个 tool 文件带耦合。→ **需求 4 的解耦成本低:核心整搬,只重写"工具外壳 + 后端适配"两层。**

**(b) Raven 工具接口(重写入口的目标形态)**
- `class Tool(ABC)` —— `raven/agent/tools/base.py:7`,须实现 `name`/`description`/`parameters`(JSON Schema dict)/`async execute(**kwargs) -> str`;`timeout_seconds`、`blocking_interaction` 两个类级开关。
- **结构差异(关键)**:Raven 工具**返回纯 `str`,无富结果对象**,且**工具自身不发事件**(事件由 `AgentLoop` 在调用前后包裹,`raven/agent/loop/main.py:1586-1622`)。→ DAG 的 `dag_run_started`/`dag_node_updated`/… **不能靠 `run_subagent_dag` 的返回值带出**;沿用 RavenX 的 `ProgressPublisher = async (name, dict) -> None` 回调(`ref` 里已是此形状),在 web BFF 侧绑到 message bus / SSE。这与"DAG 不走 scheduler"自洽——进度是纯回调,不依赖 spine。
- 注册:`ToolRegistry.register`(`raven/agent/tools/registry.py:25`);插件工厂 `make_*(ctx)->Tool`(`raven/plugin/registry.py:286`)。
- **插件 vs 源码内建的抉择**:官方插件 `ServiceLocator` 目前只给 `workspace`(`raven/plugin/context.py:29`),不给 scheduler/submit。因需求 4 明确 **DAG 不走 scheduler**,DAG 子系统作为**纯工具 + 自带执行**,插件形态完全够用(只需 `workspace`);无需扩宽 `ServiceLocator`。

**(c) 后端适配(需求 4 的第二层重写)**
- RavenX 的 `BackendBase` 契约要用 Raven 侧等价物实现:文件 I/O + `exec_shell`。Raven 原生 subagent 已有沙箱执行器 `build_executor`(见 `raven/agent/subagent/manager.py:137`)可作参照/复用,把 DAG 的节点 shell-out 和文件式消息传递落到 Raven workspace/sandbox 上(保住"走 backend 抽象、非 host `open()`"这条不变量)。

**(d) 需求 5:原生 subagent 第三方增强的 seam + 共享适配层**
- 现状 seam:`SubagentManager`(`raven/agent/subagent/manager.py:29`)的 `_run_subagent_inner`(`:146`)当前把"子 agent"实现为**一个 in-process raven loop**(`provider.chat_with_retry`,自建 `ToolRegistry`);`SpawnTool`(`raven/agent/tools/spawn.py:25`)是入口;结果经 `Origin.SUBAGENT` 重注入(`manager.py:279-291`)。→ 需求 5 = 在这里引入**执行器抽象**,让一个 spawned sub-agent 可以是 CLI(claude/codex)或 OpenAI-API(mirothinker),而不只是 raven loop。
- **共享第三方适配层**(§9.1 与 §9.2 复用同一层):
  - CLI adapter —— subprocess + prompt 投递(RavenX 已统一为**总是落文件再 inline 到 `{prompt}`**,`ref` spec `2026-07-19-subagent-file-prompt-delivery`;不再有 `{prompt_file}` 占位符)+ 两种 id 策略 `provisioned`/`derived` + transcript 解析(正则 / `parse_codex_jsonl`,`ref:_transcript.py`)。
  - OpenAI-API adapter —— `httpx` 读原始 JSON + 客户端侧 `messages[]` 历史重放(`ref:_openai_tool.py`;spec `2026-07-20-openai-subagent-miromind`)。
- 注意:需求 5 属 Raven 内核,**可复用** `Origin.SUBAGENT`/`scheduler`(与需求 4 的 DAG 子系统相反);两者只共享"第三方 agent 适配层"这一底座,不共享调度。

> **一句话判定**:P3 完成后,`import agentscope` 不出现在 Raven 侧任何 DAG 入口工具 / 第三方适配层实现里;AgentScope 只剩在 web BFF(`service/`)承载事件翻译 + REST 面。DAG 子系统与原生 subagent 是**两套并存**的系统,仅共用第三方执行器适配层。

---

## 10. 前端"零改动"契约(P2/P3 的输出规格,勘探自 `frontend/`)

前端消费 `@agentscope-ai/agentscope` SDK 的 `AgentEvent` union。**只要后端(现在是 AgentScope,P2 后是 spine→AgentScope 翻译层)持续按下列契约发事件,前端与 viz 一行都不用改**。这是 P2 事件翻译层的验收清单,也是 §9.1 DAG viz"保住"的定义。

**传输**:`GET /sessions/{sid}/stream?agent_id=` 的 SSE(fetch-based,带 `X-User-ID` 头,非原生 `EventSource`);`:` 开头帧为心跳跳过。触发是 `POST /chat/`(fire-and-forget,事件走 SSE 回)。消费在 `frontend/src/hooks/useMessages.ts` `processEvent`,SDK accumulator `appendEvent` 把 reply 内容事件折叠成 `thinking/text/tool_call/tool_result/data` 块。

**A. reply 生命周期 + 内容事件**(SDK `EventType`):`REPLY_START` / `REPLY_END` / `DATA_BLOCK_START|DELTA|END`(`audio/*` 走音频管线),其余 reply 事件交 `appendEvent`。这正是 `raven_bridge_agent.py` 现在发的那套(`ReplyStart/End`、`Thinking*`、`ToolCall*`、`ToolResult*`、`TextBlock*`)——**P2 直接复用**。

**B. `CUSTOM` 事件**(按 `custom.name` 路由,payload 必须精确):
| name | payload | 用途 |
|---|---|---|
| `team_updated` | — | 团队/成员面板 |
| `state_updated` | `{tasks_context, permission_context}` | 任务/权限态 |
| `subagent_require_user_confirm` / `subagent_user_confirm_result` | `SubagentHitlEntry`(键 `worker_session_id`+`reply_id`) | subagent HITL |
| `dag_run_started` | `{run_id, nodes:[{id,subagent,instance,depends_on}], created_at}` | DAG 建图 |
| `dag_node_updated` | `{run_id, node, status, started_at, ended_at}` | 节点状态 |
| `dag_run_completed` | `{run_id, manifest}` | 终态权威 manifest |
| `subagent_instance_updated` | `{handle, status, transport, agent_id, prototype, action}` | 实例面板 |

**C. `run_subagent_dag` 的 tool_result metadata** 必须带 `DagManifest` 形状:`{run_id, files[], summary, terminal_outputs}`(`frontend/src/components/dag/deriveDag.ts:40-46`)——reload 后 viz 靠它重建(优先 manifest,live 覆盖 `byNode`)。

**前端相关文件**(改则在此):事件消费 `hooks/useMessages.ts`;DAG viz `components/dag/{DagGraph,layoutDag,deriveDag}.tsx` + `components/chat/{ChatViewport,DagRunsContext,SubagentInstancesContext}.tsx` + `components/chat/tool-renderers/RunSubagentDagRenderer.tsx`;实例面板 `components/subagent/{SubagentInstanceMonitor,deriveInstances}.tsx`。

**REST 面全集**(P4 逐页迁移的对象,前端 `src/api/` 实测):`/agent`(+`/schema/v2`)、`/chat`、`/sessions`(+`/messages`、`/stream`、`/interrupt`、`/files/download{,-archive}`)、`/credential`(+`/schemas`、`/{id}/models`)、`/model`、`/tts-model`、`/subagent`(+`/presets`)、`/knowledge_bases/*`、`/schedule`、`/workspace/{mcp,skill}`。**承载 AgentScope 概念、P4 要重做的**:credential/model-card、knowledge(embedding model 固定不可改)、agent schema(`context_config`/`react_config`/`invite_config`)。
