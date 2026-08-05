# P1 实施计划 —— gateway web channel

> 目标见 `docs/MIGRATION.md` §5 P1、§4。本文是 P1 的**可执行实施计划**(改哪些文件、新增哪些类、每步验收)。
> 结论先行:**Raven 的 TUI RPC 栈已经把"spine 事件 → 流式 wire 事件"整套做好了,且按 channel 参数化**;P1 真正的新代码只有一个 **WebSocket 传输**。

---

## 进展(2026-07-23,分支 `feat/gateway-web-channel`)

**P1.1 已实现 + 单测通过**(步骤 1–4):
- 配置 `GatewayWebConfig`(`raven/config/schema.py`):`web.{enabled=False, host=127.0.0.1, port=8765, auth_token}`。
- 新增 `raven/web_rpc/{__init__,spine,server}.py`:`build_web` 薄包装 `build_tui(channel="web")`;`WebSocketRpcServer`(aiohttp WS + JSON-RPC + 可选 token 门 + `broadcast` 作 `SubscriptionEmitter.send_frame`)。
- gateway 接线(`raven/cli/gateway_commands.py` `run()`):开关内装配 emitter+dispatcher(system+turn methods)+`build_web`+WS server,挂进 `coros`,`finally` 加 teardown。
- **测试发现并修的坑**:`turn.send` 原来把 `source.channel` 写死默认 `"tui"`,但 web 的 hub outlet 注册在 `"web"` —— hub 按 `source.channel` 路由,会把回复投到不存在的 `"tui"` outlet 而**丢弃**。修法:给 `turn_send`/`register_turn_methods` 加**向后兼容**的 `default_channel`(TUI 仍 `"tui"`,web 传 `"web"`)。→ 见 §4 补注。
- 测试:`tests/web_rpc/test_web_rpc_roundtrip.py`(WS 握手→订阅→send→流式 token.delta 合批→message.complete;auth token 拒绝)+ TUI 回归全绿。`ruff` 干净。
  - 注:`test_turn_wire_shape_conformance.py::test_message_complete_...` 在**本环境是既有失败**(stash 掉我的改动、在干净基线上同样失败),与本次无关。

**P1.1 全链路冒烟已通过(2026-07-23)**:真起 `raven gateway --config <含 gateway.web.enabled=true>` + 真 vLLM(Qwen3.5-9B @ :8001),WS 客户端(`scratchpad/ws_smoke.py`)发消息,完整收到 `message.start → thinking.delta → token.delta(合批) → message.complete`(带 usage)。回复正常("我是 Raven…")。
- **踩坑(重要)**:`raven` 二进制是 `uv tool install` 的**冻结副本**(MIGRATION §8 已警告),改源码不生效 —— 冒烟时 gateway 跑的是旧代码,web 通道不出现。修法:`uv tool install --editable /Evermind/sh_evermind/xuedizhan/Raven --force`(**现已装成 editable**,后续改源码即时生效)。注意:`uv run pytest` 用的是项目 `.venv`(本就是工作树),所以单测早就通过 —— 只有 `raven` CLI 需要 editable。诊断时别被"从 repo 目录跑 `python -c import raven`"误导(CWD 会混入工作树)。

**P1.2 已实现(2026-07-23)—— 但发现分层**:在 gateway `run()` 里把 web spine 提前构建,引入"proactive 目标"变量(web 启用时 = web spine,否则 = gateway spine),把 cron/sentinel/heartbeat/subagent/deep_research 的 `submit`/`hub` 指向它。回归验证:web 启用时正常 turn 仍流式(P1.1 不破);web 关闭时 `pro_* == gw_*`,行为与原版等价(向后兼容)。
- **投递能力分层(重要发现)**:
  - **subagent 结果 → web:干净可用**。`_announce_result` 复用发起那轮 web turn 的 `origin`(channel="web" + 同一 conversation),经 `pro_submit=web_scheduler` 跑、经 web hub 落到**同一个 web 订阅**。
  - **heartbeat → web:干净可用**。`on_heartbeat_execute` 直接 `pro_submit` 一个 `source.channel="web"` 的 turn,经 hub 落到 web outlet(conversation="heartbeat",客户端订阅该 conversation 即收到)。
  - **cron / sentinel → web:仅"submit 已改投",投递未完整**。cron 的 `make_on_cron_job` 用 **面向 IM 的 `resolve_cron_delivery`**(按 `enabled_channels`/`forward_channels`/session 解析目标),"web" 不在 enabled IM 通道里 → 被当 ephemeral,fan-out 不落到 web 订阅;sentinel 同理其 deliverable 的 `source.channel` 未必是 "web"。**要让 cron/sentinel 完整落到 web 订阅,需把 web 作为一个"被识别的投递目标"**(让 delivery 解析认识 web / 把 web 纳入 ChannelManager 语义)——列为 **P1.2 后续 / 或并入 P2**。
  - 安全性:web 关闭时零影响;web 开启且无 IM 通道时,cron 走 ephemeral 分支(不 crash,best-effort)。

**待办**:
- P1.2 后续:让 cron/sentinel 的投递被 web 订阅收到(web 作为可识别投递目标)。
- 运行时 proactive 证据:heartbeat(短 interval)或 subagent(需模型触发 spawn)端到端确认 —— 本次未跑(静态 + 回归已验证 submit 改投正确)。

---

## 0. 验收标准(P1 完成的定义)

一个挂在 `raven gateway` 里的**本地 WS 端点**,满足:
1. 客户端可 `turn.send` → 起一个 `source.channel="web"` 的 USER turn;
2. 服务端把该 turn 的 spine 事件**流式**推给订阅者:`message.start` / `token.delta` / `thinking.delta` / `tool.start` / `tool.complete` / `message.complete` / `error`;
3. (P1.2)cron / sentinel / heartbeat / subagent 的输出(`source.channel="web"`)也能到达该端点。

**冒烟验收**:一个 WS 客户端连上 → `turn.subscribe` → `turn.send "你好"` → 收到流式回复直到 `message.complete`;再验证一个 cron/sentinel 事件出现在同一订阅。

---

## 1. 关键发现:几乎整套复用 TUI RPC 栈

TUI 侧(`raven/tui_rpc/`)已实现我们需要的一切,且已按 `channel` 参数化:

| 组件 | 位置 | 作用 | P1 复用方式 |
|---|---|---|---|
| `TuiOutlet(channel, emitter)` | `tui_rpc/spine.py:116` | Deliverable→wire(`Reasoning`→thinking.delta / `ToolEvent`→tool.* / `Text`/流→token.delta) | 直接复用(channel="web"),或起个 `WebOutlet` 别名 |
| `SubscriptionEmitter(send_frame)` | `tui_rpc/subscriptions.py:40` | 每会话订阅 + 16ms 合批 + 溢出保护 | 直接复用 |
| `build_tui(agent_loop, emitter, channel, on_turn_end, ...)` | `tui_rpc/spine.py:250` | 流式 runner(`stream=True`)+ render-barrier sink + `message.complete` | 直接复用(或 `build_web` 薄包装) |
| `register_turn_methods(dispatcher, emitter, scheduler, turn_ids)` | `tui_rpc/methods/turn.py:266` | `turn.{send,subscribe,unsubscribe,cancel}` | 直接复用 |
| `register_system_methods` | `tui_rpc/methods/system.py` | `system.hello`/ping/version 握手 | 直接复用 |
| `Dispatcher` | `tui_rpc/dispatcher.py` | JSON-RPC 2.0 路由 | 直接复用 |

**唯一新增**:传输层。TUI 的 `RpcServer`(`tui_rpc/server.py:39`)走 **TCP-loopback + 换行分隔 JSON + auth token**(供 Node 子进程连);而 MIGRATION §1 约束 web 走 **gateway 内置 WS,web 后端作 WS 客户端**。所以 P1 = 写一个 WS 版的 `RpcServer`,其余照搬。

---

## 2. 架构决策(需拍板 1 处)

### 决策 A(推荐):独立 `build_web` spine,与 `build_gateway` 并存,共享同一 `agent_loop`

- **为什么不复用 gateway 的那一套 scheduler/hub**:gateway 的 runner(`GatewayTurnRunner`,`_gateway_spine.py:44`)是 **`stream=False`** —— proactive 回复是"一条 `Text`",不是 token 流;而 web 要 token 流式(`stream=True`)。一个 scheduler 只有一个 runner、一个 sink,无法同时服务"非流式 IM/proactive"与"流式 web"。
- **为什么可以并存**:`agent_loop` 的 `run_turn` 已被 gateway 的 `user_pool>1` 依赖为并发安全(每轮工具态 turn-local,见 `_gateway_spine.py:142-144`)。两个 scheduler 各自 lane 化、各自 `OriginPools`,并发安全,只是并发上限是两者之和(可接受,单用户)。
- 决策 B(否决):把 `WebOutlet` 注册进 gateway 现有 `gw_hub` —— 卡在 runner 非流式 + sink 语义冲突,否决。

### proactive → web 路由(§4 要点),分两步降风险

- P1.1:先只落 **submit/subscribe**(web turn 自己的流)。不动 proactive。
- P1.2:把 proactive 输出指向 **web** spine:`make_on_cron_job(default_channel="web")`、`sentinel dispatcher.set_post(web_hub.post)`、heartbeat target `channel="web"`、`agent.subagents.set_submit(web_scheduler.submit)`。
  - 注意:这会把 proactive 从 IM/cli **改投到 web**。因"单用户 + web 为主"是已拍板约束,这是**预期行为**;用 `config.gateway.web.enabled` 开关控制:开则 web 接管 proactive,关则维持现状。

---

## 3. 文件改动清单

### 新增 `raven/web_rpc/`(镜像 `tui_rpc/`,尽量 re-export)
- `__init__.py`
- `spine.py` —— `WebOutlet`(可 `WebOutlet = TuiOutlet` 或薄子类)+ `build_web(agent_loop, emitter, *, channel="web", on_turn_end=None, readback_texts=None, user_pool, system_pool)`(内部直接调 `build_tui(..., channel="web", ...)` 或复制其装配)。返回 `(scheduler, hub, turn_ids, teardown)`。
- `server.py` —— `WebSocketRpcServer`:基于 **aiohttp**(`aiohttp` 是 raven 核心依赖,见 `pyproject.toml`;`web.AppRunner` 可在 gateway 现有 asyncio loop 内起服务,最省)。职责:
  - 绑 `127.0.0.1:<port>`(单用户,不对外);
  - 每连接:可选首帧 auth token(照抄 `RpcServer` 的 token 门,`server.py:184`);
  - 读 WS text 帧 → `json.loads` → `dispatcher.dispatch(frame)` → 回 `send_frame`;
  - `SubscriptionEmitter` 的 `send_frame` = 向当前连接 `ws.send_str(json.dumps(frame))`(单连接即可满足单用户;多连接则广播)。
- 复用 `tui_rpc` 的 `Dispatcher` / `SubscriptionEmitter` / `register_turn_methods` / `register_system_methods`(直接 import,不复制)。

### 修改
- `raven/config/schema.py` —— `GatewayConfig` 加 `web: GatewayWebConfig`,字段 `{enabled: bool=False, host: str="127.0.0.1", port: int=8765, auth_token: str|None=None}`。
- `raven/cli/gateway_commands.py` `run()` —— 在 `build_gateway(...)` 之后、`await asyncio.gather(coros)` 之前:
  ```python
  if config.gateway.web.enabled:
      from raven.web_rpc.server import WebSocketRpcServer
      from raven.web_rpc.spine import build_web
      from raven.tui_rpc.dispatcher import Dispatcher
      from raven.tui_rpc.subscriptions import SubscriptionEmitter
      from raven.tui_rpc.methods.turn import register_turn_methods, clear_active
      from raven.tui_rpc.methods.system import register_system_methods

      web_server = WebSocketRpcServer(host=..., port=..., auth_token=...)
      emitter = SubscriptionEmitter(send_frame=web_server.broadcast)
      web_scheduler, web_hub, web_turn_ids, web_teardown = build_web(
          agent, emitter, on_turn_end=clear_active,
          user_pool=config.gateway.user_pool, system_pool=config.gateway.system_pool,
      )
      dispatcher = Dispatcher()
      register_system_methods(dispatcher)
      register_turn_methods(dispatcher, emitter=emitter, scheduler=web_scheduler, turn_ids=web_turn_ids)
      web_server.bind(dispatcher)
      # P1.2(开关内):proactive 改投 web
      #   cron.on_job = make_on_cron_job(..., submit=web_scheduler.submit, default_channel="web")
      #   sentinel_runner.dispatcher.set_post(web_hub.post) / task_discoverer.set_submit(web_scheduler.submit)
      #   agent.decision_consumer.executor.set_submit(web_scheduler.submit)
      #   agent.subagents.set_submit(web_scheduler.submit)
      #   on_heartbeat_execute 用 channel="web"
      coros.append(web_server.serve_forever())
  ```
  并在 `finally` 加 `if web_teardown: await web_teardown()` + `await web_server.stop()`。

---

## 4. Wire 协议(照抄 TUI,给 P2 翻译层一个稳定目标)

**Client→Server(JSON-RPC 请求)**:
- `system.hello` → 握手
- `turn.subscribe {session_key}` → `{subscription_id}`
- `turn.send {session_key, content, channel?, chat_id?, sender_id?}` → `{turn_id, accepted}`(turn 起在 `conversation=session_key`,`source.channel="web"`)
  - **channel 必须解析成 `"web"`**:服务端 `register_turn_methods(default_channel="web")` 已保证客户端**省略** `channel` 时默认 `"web"`(与 hub 的 `web` outlet 对齐);客户端也可显式传 `"web"`。传别的值会导致回复被 hub 丢弃。
- `turn.cancel {session_key}` → `{cancelled}`
- `turn.unsubscribe {subscription_id}` → `{unsubscribed}`

**Server→Client(通知)**:`{"jsonrpc":"2.0","method":"event","params":{"subscription_id","event":{"type","payload"}}}`,`event.type ∈ {message.start, token.delta, thinking.delta, tool.start, tool.complete, message.complete, error}`。与 TUI 完全一致(`tui_rpc/spine.py:131-189`、`methods/turn.py`)。

> 注:这套是 **spine 原生 wire 事件**,不是 AgentScope 事件。把它翻译成前端要的 AgentScope `AgentEvent`(见 MIGRATION §10)是 **P2** 的活,在 `service/` 侧做。P1 只保证这套 wire 事件流出。

---

## 5. 分步实施(每步独立可验)

1. **config**:加 `GatewayWebConfig` + 默认值。验:`raven`/`raven gateway --help` 正常加载,`config.gateway.web.enabled` 默认 False。
2. **`web_rpc/spine.py`**:`WebOutlet` + `build_web`。单测:`build_web(fake_loop, emitter)` 返回四元组;register/teardown 不抛。
3. **`web_rpc/server.py`**:aiohttp WS + dispatcher 接线 + auth。单测:起服务→WS 连接→`system.hello` 握手→`turn.subscribe`→`turn.send`(接一个 fake streaming agent_loop)→收到 `message.start`…`token.delta`…`message.complete`。
4. **gateway 接线(P1.1)**:开关内装配 + teardown。手验:`config.gateway.web.enabled=true` 起 `raven gateway`;用 scratchpad 里的 WS 客户端脚本 `turn.send "hi"` 收到流式回复。
5. **proactive→web(P1.2)**:开关内把 cron/sentinel/heartbeat/subagent 指向 web spine。验:设一个 `channel="web"` 的 cron job,到点后 web 订阅收到 `cron`-origin turn 的回复。

---

## 6. 验证 / 工具

- 新测 `tests/web_rpc/`(镜像 `tests/tui_rpc/` 的用例)。`uv run pytest tests/web_rpc -x`。
- `ruff check raven`。
- **可编辑安装**让 gateway 用上改动源码:`uv tool install --editable /Evermind/sh_evermind/xuedizhan/Raven --force`。
- scratchpad 冒烟脚本:一个 `aiohttp`/`websockets` WS 客户端跑 §4 的握手+send+订阅。

---

## 7. 风险 / 注意

- **双 scheduler 共用一个 agent_loop**:并发安全(per-turn tool state turn-local,gateway 已依赖),但两套 `OriginPools` 独立,总并发 = 之和;单用户下可接受。
- **WS 库选型**:选 **aiohttp**(核心依赖、可在现有 loop 内起、无需子进程)。备选 starlette+uvicorn(也是依赖)。避免 `websockets`(仅 discord extra,未必装)。
- **安全边界**:绑 `127.0.0.1` + 可选首帧 token(照抄 `RpcServer` token 门);web BFF 作客户端携带 token。
- **不碰 spine**:P1 全程复用 spine 既有 API(`scheduler.submit` / `hub` / `Outlet`),零 spine 改动;符合 §4 结论。
- **P0 未收尾**:`ui-webui/` 仍是 untracked 裸 copy;P1 改的是 Raven 源码(`raven/`),与 `ui-webui/` 无关,可并行推进,但正式提交前需按 §8 决定 git 归置。
