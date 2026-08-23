# P2 实施计划 —— 内核切换:service 的 /chat agent 改为 gateway WS 客户端

> 目标见 `docs/MIGRATION.md` §5 P2、§10。把 `service/` 里 `/chat/` 的 agent 从 `RavenBridgeAgent`(每轮 spawn 一次性 `raven agent -m`、解析日志)换成**连持久 `raven gateway` web 通道的 WS 客户端** + spine wire → AgentScope 事件翻译。
> 前置:P1 的 web 通道(`ws://127.0.0.1:8765/ws`,已验证)。

---

## 0. 验收标准

1. `service/` 起一个新 agent 类(替 `RavenBridgeAgent`),`reply_stream` 通过 WS 连到持久 gateway,发一轮消息、把回来的 spine wire 事件翻译成 AgentScope 事件流(`ReplyStart → Thinking*/ToolCall*/ToolResult*/Text* → ReplyEnd`),**前端零改动**。
2. 主动触发(cron/sentinel/heartbeat/subagent)**首次能出现在 web UI**(P1.2 已把它们改投 web spine;P2 把 web 客户端接上,proactive 事件经同一订阅流到前端)。
3. 多轮上下文靠固定 session_key(每个 AgentScope 会话 ↔ 一个 gateway conversation)。

**冒烟**(不依赖完整前端):用 ravenx 环境 python 实例化新 agent 类,喂一条消息,打印翻译出的 AgentScope 事件;再验证 message.complete 收尾。

---

## 1. 环境(已探明)

- service 跑在 **`ravenx` conda env**:`/root/miniconda3/envs/ravenx/bin/python`,`agentscope 2.0.4`(site-packages),`aiohttp 3.14.1`(作 WS 客户端;无 `websockets`)。
- gateway 侧 web 通道 wire 协议见 P1 计划 §4:`system.hello` / `turn.subscribe` / `turn.send` / 事件通知 `{method:"event", params:{subscription_id, event:{type,payload}}}`,`event.type ∈ {message.start, token.delta, thinking.delta, tool.start, tool.complete, message.complete, error}`。

## 2. 设计

### 2.1 `GatewayClient`(共享,单连接多路复用)
- `connect()`:开 WS → (可选)首帧 auth token → `system.hello` → 起后台 read loop。
- read loop:逐帧 `json.loads`;有 `id` → resolve 对应 pending future(RPC 响应);`method=="event"` → 按 `params.subscription_id` 投到对应 `asyncio.Queue`。
- `call(method, params)`:发带 id 的请求,await future。
- `subscribe(session_key) -> (sub_id, Queue)`:`turn.subscribe`,建队列。每会话订阅一次并缓存。
- `send_turn(session_key, content) -> turn_id`:`turn.send`(省略 channel,gateway 默认 `default_channel="web"`)。
- 重连:best-effort(断线重连 + 重订阅);v1 可先做基本重连或标 TODO。

### 2.2 `RavenGatewayAgent`(duck-typed,替 `RavenBridgeAgent`)
- 与 `RavenBridgeAgent` 同构造签名(`**_ignore` 吃掉 AgentScope agent kwargs),`name`/`state`。
- `session_key = self.state.session_id`(每实例一会话,复用 gateway 同一 conversation → 多轮上下文)。
- `reply_stream(inputs)`:
  1. `yield ReplyStartEvent(session_id, reply_id, name)`
  2. 确保 client 连接 + 该 session 已订阅
  3. `send_turn(session_key, 用户文本)`
  4. drain 该 session 的队列,翻译 wire→AgentScope,直到 `message.complete`/`error`,最后 `yield ReplyEndEvent`

### 2.3 wire → AgentScope 事件翻译(块生命周期)
维护"当前打开块"(thinking/text/tool),转场时关旧开新。映射:
| wire event | AgentScope 事件 |
|---|---|
| `message.start` | (可忽略;ReplyStart 已发) |
| `thinking.delta {text}` | 首次:`ThinkingBlockStart`+`Delta`;续:`Delta` |
| `token.delta {text}` | 首次:`TextBlockStart`+`Delta`;续:`Delta` |
| `tool.start {tool_call_id,name,arguments}` | `ToolCallStart`+`ToolCallDelta(delta=json(arguments))`+`ToolCallEnd`+`ToolResultStart` |
| `tool.complete {tool_call_id,result_preview,truncated}` | `ToolResultTextDelta(delta=result_preview)`+`ToolResultEnd(state=SUCCESS, metadata=...)` |
| `message.complete` | 关当前块 → `ReplyEndEvent(COMPLETED)` |
| `error {code,message,reason}` | 关块 → 一个 Text 块显示错误 → `ReplyEndEvent` |

> 复用 `raven_bridge_agent.py` §2c 的事件形状(块 start/delta/end、reply_id 贯穿),只是**事件源从"解析日志"换成"结构化 wire 事件"**——更干净、无正则。
> subagent 实例面板(`subagent_instance_updated`)与 DAG viz 事件:P1.2 后续把 proactive/DAG 事件经 web 订阅带出后,在此翻译层补 `CUSTOM` 事件(见 MIGRATION §10 契约)。v1 先做 reply 主链路。

### 2.4 接线(main.py)
- 新开关 `RAVEN_GATEWAY=1`(与 `RAVEN_BRIDGE` 并列/互斥):`create_app(custom_agent_cls=RavenGatewayAgent)`。
- gateway WS 地址/ token 从 env(`RAVEN_GATEWAY_WS_URL`,默认 `ws://127.0.0.1:8765/ws`)。
- 需要一个常驻的 `raven gateway`(web 启用)在跑。

## 3. 文件改动
**新增** `service/raven_gateway_agent.py`:`GatewayClient` + `RavenGatewayAgent` + 翻译。
**修改** `service/main.py`:`RAVEN_GATEWAY` 开关接线。

## 进展(2026-07-23)

- **P2.1 已实现 + 验证**:`service/raven_gateway_agent.py`(`GatewayClient` 多路复用 WS + `RavenGatewayAgent` 翻译 + `aclose`)。ravenx python 驱动 `reply_stream` 连真 gateway,翻译出正确的 AgentScope 事件流。
- **P2.2 已实现 + 端到端验证**:`main.py` 加 `RAVEN_GATEWAY=1` 开关(优先于 `RAVEN_BRIDGE`)。真起 **redis + gateway(web)+ service(uvicorn)**,用 HTTP 客户端走 agent→session→`/chat`→SSE,收到 `REPLY_START → THINKING_BLOCK_* → TEXT_BLOCK_* → REPLY_END`,回复正常。**前端零改动契约(§10)满足**。

### 运行时关键事实(踩坑记录,后续起栈必看)
1. **service 用的 python**:`/Evermind/sh_evermind/share/wuzhengwei/conda/miniconda3/envs/ravenx/bin/python`(有 fastapi 0.139 + agentscope + aiohttp)。**不是** `/root/miniconda3/envs/ravenx`(那个没 fastapi)。有两个同名 `ravenx` env,别搞混。
2. **必须 `PYTHONPATH=/Evermind/sh_evermind/xuedizhan/RavenX_demo/src`**:pip 装的 agentscope 2.0.4 缺 `IsolationPolicy` 等 RavenX 增强;service 要跑在 RavenX_demo 的 agentscope 源码上(bridge README 的 `PYTHONPATH=src`)。
3. **每个 session 必须有 `chat_model_config`**:`ChatService.run` 对每轮 turn 校验 session 的 chat model 配置(`_chat.py:466`),否则 404 `No model configuration found`。**即使 `RavenGatewayAgent` 不用 model**(真 model 在 gateway 里)。用现成 credential `d84103d2…`(Local vLLM Qwen3.5-9B)绑到 session:`POST /sessions/ {agent_id, chat_model_config:{type, credential_id, model, parameters}}`。`get_model` 从 credential 取模型类,`type` 只要非空串即可。
4. 起 gateway 需 editable 的 `raven`(见 P1 计划)+ `gateway.web.enabled=true` 的 config。

## 4. 分步(每步独立可验)
- **P2.1**(核心,不依赖完整前端):`raven_gateway_agent.py`(client + agent + 翻译)。冒烟:ravenx python 脚本实例化 `RavenGatewayAgent`(造 `AgentState(session_id=...)`)→ `reply_stream("你好")` → 打印 AgentScope 事件到 `ReplyEnd`。需一个 web 启用的 gateway 在跑。
- **P2.2**:接进 `main.py`(`RAVEN_GATEWAY=1`)+ 起完整栈(redis + service + 前端),浏览器实测多轮 + 工具调用渲染。
- **P2.3**:proactive/DAG 的 `CUSTOM` 事件翻译(依赖 P1.2 后续把这些事件经 web 订阅带出)。

## 5. 风险 / 注意
- **多路复用正确性**:一个 WS 连接服务多会话;事件按 `subscription_id` 路由到会话队列。turn 与订阅是会话级(非 turn 级)——一轮 `reply_stream` drain 到本轮 `message.complete` 即止(用 turn_id 或"下一个 message.complete"界定)。
- **单飞 in-flight**:gateway 的 `turn.send` 对同一 session 有"一次一个 turn"约束(-32003);service 侧保证串行(reply_stream 本就是一轮一轮)。
- **重连**:gateway 重启 / 断线 → 客户端需重连 + 重订阅;v1 至少不崩(断线时本轮报错收尾)。
- **proactive 归属**:P1.2 已知 cron/sentinel 投递到 web 订阅未完整;P2 客户端就位后,可在此层或 gateway 侧把 web 作为一等投递目标一并解决(§MIGRATION P1.2 后续)。
