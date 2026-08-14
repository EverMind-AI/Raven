# Raven Web-Surface Integration Analysis

> 代码级勘探报告(read-only)。目标问题:**web app 能否作为一个长驻 Raven runtime 的 channel/outlet 接入,使 cron 主动触发、sentinel/anticipatory nudge、hook、后台 subagent 全部可暴露,而不只是用户发起的 turn?**
> 所有 file:line 均指向 Raven 仓库(`/Evermind/sh_evermind/xuedizhan/Raven`),勘探时 Raven 版本 `0.1.7`。术语:一个 "surface"(REPL / TUI / gateway)= 三个 spine 件的装配 —— 一个 `Scheduler`、一个 `DeliveryHub`、一个或多个 `Outlet`,由 `build_*` 函数拼装。spine 从不 import channels/cli;依赖永远指向内层。

---

## 1. 持久 runtime 入口

**`AgentLoop.run()` 只是一个 keep-alive,不是 turn 循环。** `raven/agent/loop/main.py:1782-1806`:

```python
async def run(self) -> None:
    """Bring the agent runtime up and stay alive.
    Turns arrive through the spine (run_turn); this coroutine no longer
    drains an inbound bus. It starts the executor / debug server / MCP, then
    idles on self._running ..."""
    self._running = True
    ... await self._start_executor(); await self._start_debug_server(); await self._connect_mcp()
    while self._running:
        await asyncio.sleep(1.0)
```

它自身不做任何工作,只把 sandbox executor / MCP / debug server 撑开。所有 turn 都独立地经由 spine `Scheduler` 进入。`stop()` 翻转 `_running`(`main.py:1851`)。

**启动持久进程的 CLI 命令**(注册于 `raven/cli/commands.py:106-147`):
- **`raven gateway`** —— `raven/cli/gateway_commands.py:114`。真正的长驻多源守护进程:agent loop + channel manager + cron + heartbeat + sentinel。持单例锁(`_gateway_lock.acquire`, `gateway_commands.py:161`),每实例只能跑一个。
- **`raven agent`**(无 `-m`)—— 交互式 REPL,`raven/cli/agent_commands.py:469-615`(`else:` 分支)。
- **`raven tui`** / 裸 `raven` —— 原生 TUI(`commands.py:75-88`, `tui_commands.py`),它 spawn 一个 Node 前端并用 JSON-RPC 通信。

Python 侧**没有 `raven serve`、也没有面向外部客户端的 HTTP/WS API server**。gateway 打开的唯一监听 socket 是一个 liveness-only 健康端点:`gateway_commands.py:541` `asyncio.start_server(_health_handler, "127.0.0.1", port)` 返回 `{"status":"ok"}`。全仓搜 `fastapi|aiohttp|uvicorn|websockets.serve|web.Application` 没有任何在服务 agent API。

**交互式 REPL vs 一次性 `-m`**(都在 `agent_commands.py`):
- `-m` → `run_once()`(`agent_commands.py:398-464`):构建 REPL spine,submit **一个** `USER` `TurnRequest`,await `result()`,拆除。它**不**调 `agent_loop.run()`、**不**调 `cron.start()`、**不**启动 sentinel。`interactive=message is None`(`:356`),所以也无 checkpoint。
- 无 `-m` → `run_interactive()`(`:499-615`):启动 keep-alive `runtime_task = asyncio.create_task(agent_loop.run())`(`:513`),构建 spine,接线 `cron.on_job`(`:540`)、`sentinel dispatcher.set_post(hub.post)`(`:551`)、`await cron.start()`(`:555`)、`await sentinel_runner.start()`(`:560`),然后跑 `run_repl_loop`。

**结论**:主动触发需要持久路径 —— 结构性证实。

---

## 2. spine(scheduler + hub + outlets)

**Scheduler**(`raven/spine/scheduler.py:313`)是唯一入口:`submit(req: TurnRequest) -> TurnHandle`(`:327`)。它把每个请求路由到 per-conversation `Lane`(`:64`,按需创建 `:337-340`),Lane 串行化 turn。Origin 决定并发池而非路由:`OriginPools`(`:43`)有一个 USER 信号量和一个共享 **system** 信号量供 `SENTINEL, CRON, HEARTBEAT, SUBAGENT`(`_SYSTEM_ORIGINS`, `:30`;`for_origin`, `:54`)。所有 origin 是**同一个** `TurnRequest` 类型(`raven/spine/turn.py:44`),带一个 `Origin` 枚举字段(`turn.py:9-17`);Scheduler 统一接收。`submit` 必须从 scheduler 的 home loop 调用(`:328`)。

**输出词汇表**(`raven/spine/events.py`):一个 turn emit `RunnerEvent = ToolEvent | Text | MediaOut | StreamDelta | Reasoning | Notice`(`events.py:113`),别名 `Deliverable`(`:115`)。生命周期事件 `TurnStarted/TurnFailed/TurnEnded`(`:38-57`)只由 worker emit,runner 不 emit(在 `scheduler.py:240-243` 强制)。每个事件带 `source: Source | None` 和 `conversation_id`。

**hub** = `DeliveryHub`(`raven/spine/delivery.py:81`)。它是一个**per-channel 路由器,不是广播总线**:per outlet 一个有界队列 + 一个串行 worker,按 `outlet.name` 键入(`register`, `:102`),把每个 deliverable 路由到 `name == out.source.channel` 的那个 outlet(`_enqueue`, `:130-151`;无匹配 outlet 则 "no outlet for channel; dropping" `:134-135`)。入口:`dispatch`(`:108`, turn deliverable)、`post`(`:111`, 非 turn 事件如 Sentinel 菜单)、`close_stream`(`:118`)、`wait_idle`(`:220`, 渲染屏障)。**这是集成最重要的路由事实(见 §6)。**

**Outlet 协议**(`delivery.py:53-64`):

```python
@runtime_checkable
class Outlet(Protocol):
    name: str
    capabilities: Capabilities
    async def deliver(self, out: Deliverable) -> None: ...
```

可选流式 opt-in `SupportsStreaming`(`delivery.py:45-50`, `send_stream_chunk(...)`);hub 只对**同时**实现它且声明 `capabilities.streaming` 的 outlet 流式(`:173`, `:183`)。

**runner seam**(`raven/spine/runner.py:32`):`TurnRunner.run(req, emit, drain) -> TurnOutcome`。agent loop 实现它(`AgentTurnRunner`, `raven/agent/spine_runner.py`);`emit` push `RunnerEvent`,由 lane 打上 source/conversation_id(`scheduler.py:239-252`)再转给 sink。

**现有 outlet —— 没有 HTTP/WS/gateway outlet。** 存在三个:
- `ChannelOutletAdapter`(`raven/channels/outlet.py:19`)—— 包一个 IM channel;把 `Text`/`MediaOut` 经 `channel.send(...)` 渲染,吞掉流式事件。非流式(`Capabilities(streaming=False)`, `:33`)。
- `CliOutlet`(`raven/cli/_repl_spine.py:30`)—— 渲染到终端;非流式。
- `RpcOutlet`(`raven/rpc/spine.py:116`)—— **流式**(`Capabilities(streaming=True)`, `:128`);把每个 spine 事件映射为 wire 事件(`token.delta`, `thinking.delta`, `tool.start/complete`),经 `SubscriptionEmitter`。**这是最接近 web surface 的现有类比。**

---

## 3. Channels

子系统在 `raven/channels/`。**`ChannelManager`**(`raven/channels/manager.py:47`)从声明式 spec 构造启用的 adapter(`_init_channels`, `:56`;读 `config.channels.<name>.enabled`),start/stop 它们,报告 `enabled_channels`(`:133`)。它只做构造 + 生命周期 —— "Outbound delivery is the spine's DeliveryHub/Outlet ... inbound is each channel's Intake -> scheduler.submit"(`manager.py:3-6`)。

**存在的 channel**(`raven/channels/adapters/`):whatsapp, telegram, discord, feishu, mochat, dingtalk, email, slack, qq, matrix, wecom, weixin(枚举于 `_GATEWAY_IM_CHANNELS`, `gateway_commands.py:66-79`)。加上 surface 内部伪 channel `cli`(CliOutlet)与 `tui`(RpcOutlet)。**没有 `web`/`http`/`gateway` channel。**

**`Channel` 协议**(`raven/channels/contract.py:25-34`):`name`, `capabilities`, `start()`, `stop()`, `send(chat_id, content, media)`。可选 `SupportsLogin`(`:37`)。channel 导出 `ChannelSpec`(`:44`)含一个延迟 `factory`;registry 发现它们(`manager.py:63` `discover_specs()`)。

**channel 如何接到 spine** —— 两个独立方向:
- **Outbound**:gateway 为每个 channel 注册一个 `ChannelOutletAdapter(channel)` 到 hub(`_gateway_spine.py:138-139`)。回复路由到它当且仅当 `Text.source.channel == channel.name`。
- **Inbound**:每个 channel 拥有一个 `Intake`(`raven/channels/intake.py:19`)。gateway 为每个 channel 接线 `_ch.intake.set_submit(_inbound_dispatch)`(`gateway_commands.py:533-534`)。`Intake.publish`(`intake.py:56-103`)做 allowlist 检查,再构建 `TurnRequest(origin=Origin.USER, source=Source(channel=...))` 并调已接线的 submit。

**Node `bridge/` 是 WhatsApp 专用,不是通用 Python API。** `bridge/src/server.ts:32` `BridgeServer` 是一个 **WebSocket server**,绑 `127.0.0.1:3001`(`index.ts:27`, `server.ts:49-51`),token 鉴权(`server.ts:81`),拒绝浏览器 Origin 头(`server.ts:52-59`)。**Python 侧是 WS 客户端**:`whatsapp` channel adapter(`raven/channels/adapters/whatsapp/`, bridge client 在 `bridge.py`)连到这个 Node 进程,后者用 `@whiskeysockets/baileys` 说 WhatsApp Web 协议。这个 bridge 不连任何 Python HTTP/WS agent server —— Python raven runtime 没有。

(另外 TUI 用 `raven/rpc/server.py:39` `RpcServer` —— JSON-RPC 2.0 over TCP-loopback 到 spawn 的 Node TUI 子进程,token 鉴权 `:184-194`。这是 TUI 前端传输,不是公开 API。)

---

## 4. 主动引擎 / cron / sentinel

所有主动生产者都通过与 user turn 相同的 `Scheduler.submit` / `DeliveryHub.post` 把 turn 注入**运行中的** runtime。

**Cron**(`raven/proactive_engine/schedulers/cron/service.py` `CronService`;handler 工厂 `raven/cli/_cron_handler.py:102` `make_on_cron_job`)。fire 时,`on_cron_job`(`_cron_handler.py:162`)构建 `TurnRequest(origin=Origin.CRON, ..., conversation=f"cron:{job.id}")`(`:225-230`)并 `await submit(req).result()`(`:232`)。gateway 在 `gateway_commands.py:389-399` 接线(`cron.on_job = make_on_cron_job(... submit=gw_scheduler.submit ...)`)并启动 tick loop `await cron.start()`(`:536`)。REPL 在 `agent_commands.py:540` 和 `:555` 接线。

**Sentinel**(`raven/proactive_engine/sentinel/`)。nudge 经 hub 的非 turn `post` 投递,刻意**不**重跑 agent:`NudgeDispatcher.dispatch`(`.../executor/dispatcher.py:77-119`)向每个 target post 一个 `Text`,带 `source.extras._sentinel_origin=True`。`set_post` 晚绑 `hub.post`(`dispatcher.py:73`);gateway 在 `gateway_commands.py:438-439` 接线。Sentinel 菜单选择执行与 "supersede" turn 则走 `scheduler.submit`(`gateway_commands.py:440-446`)。Sentinel 用 `await sentinel_runner.start()`(`:539`)启动。

**Heartbeat**(`HeartbeatService`):`on_heartbeat_execute` submit `TurnRequest(origin=Origin.HEARTBEAT, ...)` 并 await `result()`(`gateway_commands.py:409-420`)。

**Subagents**:结果回注 submit `Origin.SUBAGENT` turn —— `agent.subagents.set_submit(gw_scheduler.submit)`(`gateway_commands.py:448`;REPL `agent_commands.py:535`)。

**一次性 `-m` 无法暴露这些的确认**:`-m` 的 `run_once()`(`agent_commands.py:398`)从不调 `cron.start()`、从不调 `sentinel_runner.start()`、从不启动 `agent_loop.run()`;它 submit 一个 turn 就退出。cron/sentinel/heartbeat 只存在于 gateway 与交互式 REPL 路径,那里才有 keep-alive `agent.run()` 与 tick loop。

---

## 5. Hooks

`raven/agent/hook/` —— `AgentHook` ABC(`base.py:126`)、`AgentHookContext`(`:60`)、`HookDecision`(`:96`),由 `CompositeHook`(`composite.py`)组合、`adapters.py` 适配。阶段(默认全 no-op):`before_user_inbound`(`base.py:145`)、`before_iteration`(`:164`)、`before_execute_tools`(`:177`)、`after_iteration`(`:189`)、`after_send`(`:205`)。

**Hook 不 emit 到 spine/outlet 流。** 它们靠返回值起作用(`base.py:96-118`):`short_circuit_result`(中止并用作回复)或 `modified_content`(改写外发文本,如 Sentinel `NudgeInjector` 在 `after_send` 追加 nudge)。它们在 AgentLoop turn *内部*、LLM 调用前后运行,对外部 surface 的影响只是间接的 —— 作为 turn 正常的 `Text` deliverable(可能被改过)经 hub 路由。`events.py` 里没有 hook 事件类型,也没有 hook→outlet 路径。所以 **web surface 无法直接观测 hook 触发,只能看到它对 turn 所 emit 的 `Text`/短路回复的下游效果。**(唯一骑在流上的带外信号类型是 `Notice`, `events.py:105`,由 runner emit,不是 hook。)

---

## 6. 最干净的集成接入点

**核心约束**:`DeliveryHub` 把每个 deliverable 路由到 `name == out.source.channel` 的那个 outlet(`delivery.py:130-151`);它**不是广播总线**。所以一个名为 `"web"` 的单一 outlet 默认只收到 `source.channel == "web"` 的 deliverable。web 客户端的 USER turn 没问题(你在 intake 打上 `channel="web"`)。但 CRON turn 跑在 `conversation="cron:<job_id>"`、带 job 自己的 source channel;SENTINEL/heartbeat nudge 带它们解析出的 target channel —— 除非你让它们变成 `"web"`,否则都不是。这是"收全部 turn 输出"的关键。

**推荐接入点 —— 一个新的流式 `Outlet` + 一个新的 surface 装配,托管在 `raven gateway` 守护进程内。** 具体:

1. **实现一个 `WebOutlet`,against `raven.spine.delivery.Outlet`**(`delivery.py:53-64`),几乎完全照抄 `RpcOutlet`(`raven/rpc/spine.py:116-189`)—— 声明 `Capabilities(streaming=True)` 并实现 `SupportsStreaming.send_stream_chunk`,把每个 spine 事件 push 给浏览器客户端。**原样复用 `SubscriptionEmitter`**(`raven/rpc/subscriptions.py:40`)作为 per-conversation 到 WS/SSE 连接的扇出 —— 它已经合并 `token.delta` 并管理有界的 per-subscriber 队列。你的 web 传输(你新增的 WS/SSE server)替换 `RpcServer` 的 JSON-RPC-over-TCP;emitter 的帧形状与传输无关。

2. **加一个 `build_web(...)` 装配**,仿 `build_gateway`(`raven/cli/_gateway_spine.py:113`)/ `build_rpc_spine`(`raven/rpc/spine.py:250`):构建 `DeliveryHub`,把 `WebOutlet` 以固定名(如 `"web"`)注册,并构建带 web sink 的 `Scheduler`(照抄生命周期处理自 `_make_gateway_sink`, `_gateway_spine.py:80-110`,它 fire `on_turn_complete` 并投递 `TurnFailed` 错误文本)。返回 `(scheduler, hub, teardown)`。

3. **入站**:web 用户消息到达时,构建 `TurnRequest(origin=Origin.USER, source=Source(channel="web", chat_id=<client/session id>, ...), conversation=<session key>)` 并调 `scheduler.submit`。复用 gateway 的 `_inbound_dispatch` 逻辑(`gateway_commands.py:501-531`)处理 `/stop`、`ask_user` 回复路由(`QuestionBroker`)与 mid-turn `BusyPolicy.INJECT`。

4. **要同时收到 CRON + SENTINEL + heartbeat 输出**,选定目标路由模型:最简单的是**把 WebOutlet 注册为那些生产者已使用的 channel 名的 outlet**,和/或把生产者指向 web channel —— 即接线 `cron.on_job = make_on_cron_job(..., submit=web_scheduler.submit, default_channel="web", ...)`(`_cron_handler.py:102`, `default_channel` 在 `:110`)、`sentinel_runner.dispatcher.set_post(web_hub.post)`(`dispatcher.py:73`)、heartbeat `on_execute` 目标 `channel="web"`、`agent.subagents.set_submit(web_scheduler.submit)`。因为 hub 按 `source.channel` 路由,保证 deliverable 到达 web outlet 的可靠方式就是让该 deliverable 的 `source.channel` 是 `"web"` —— 上面的 `default_channel`/target 解析旋钮控制这点。

**托管在哪**:扩展 **`raven gateway`** 命令(`raven/cli/gateway_commands.py:114`),因为它是唯一真正的长驻多源守护进程(已拥有 `agent.run()`、cron、heartbeat、sentinel 和单例锁)。在其 `run()` 协程内、现有 `build_gateway` 调用(`gateway_commands.py:380-388`)与健康 server(`:541`)旁,加 `build_web` 调用和你的 WS/SSE server,并把 `coros.append(web_server.serve_forever())` 加入最后的 `asyncio.gather`(`:549-556`)。若 web 与 IM channel 要在一个守护进程共存,要么给 gateway 两组 outlet(注册 per-IM `ChannelOutletAdapter` 和你的 `WebOutlet`)在一个 hub 上,要么像 TUI 那样把 web surface 作为主 outlet 跑。

**要实现的确切扩展点**:
- Outlet 协议:`raven/spine/delivery.py:53-64`(`Outlet`)+ `:45-50`(`SupportsStreaming`)。
- 照抄模型:`RpcOutlet` `raven/rpc/spine.py:116`;`SubscriptionEmitter` `raven/rpc/subscriptions.py:40`。
- surface 装配照抄:`build_gateway` `raven/cli/_gateway_spine.py:113`(多源,readback + sources map)与 `build_rpc_spine` `raven/rpc/spine.py:250`(流式 sink)。
- submit 入口:`Scheduler.submit` `raven/spine/scheduler.py:327`,配 `TurnRequest` `raven/spine/turn.py:44` 与 `Origin` `turn.py:9`。
- 主动接线旋钮:`make_on_cron_job` `raven/cli/_cron_handler.py:102`;`NudgeDispatcher.set_post` `.../sentinel/executor/dispatcher.py:73`;gateway 接线块 `gateway_commands.py:438-448`。
- 托管命令:`raven/cli/gateway_commands.py:114`(`gateway`),内层 `run()` `:355`。

**净结论**:没有现成的 web/HTTP/WS gateway 可扩展 —— 你新增一个传输,但**不需要碰 spine**。in-grain 的干净做法是一个新 `WebOutlet`(Outlet + SupportsStreaming)加一个 `build_web` 装配,接进 `raven gateway` 守护进程,入站经 `Scheduler.submit`、出站经 `DeliveryHub`,完全镜像 TUI 和 gateway surface 已有的接法。
