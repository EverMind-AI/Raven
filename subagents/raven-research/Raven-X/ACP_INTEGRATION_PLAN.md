# Raven-X ACP 接入 Plan（feat/acp_integration）

> 目标：Raven-X 实现 ACP（Agent Client Protocol）agent 侧，替换 `subagents/raven-research`
> 现在的 `kind: "cli"` + run.py 包装器，让主 raven 通过 `kind: "acp"` 直接驱动。
>
> 状态：plan 第二稿，未开工。第一稿的读码结论经逐条对码复核，其中三条被证伪、一条工作量
> 估错，已在本稿改正（见 §0 修订记录）。基于对主仓
> `/Evermind/sh_evermind/share/wz/dr_repo/gitlab_repo/raven`（下称"主仓"）ACP 两侧实现的读码，
> 主仓关键 commit：`3d3c8713 feat(acp): serve the builtin raven subagent over acp with resumable sessions`。
>
> 所有行号引用均已对码验证，主仓路径相对主仓根，Raven-X 路径相对本仓根。

---

## 0. 修订记录（第一稿 → 本稿）

| # | 第一稿的说法 | 复核结论 | 落在本稿 |
|---|---|---|---|
| 1 | "`loadSession` 决定 backend 眼里这个 agent 是否 stateful（`snapshot.can_resume`）" | **错**。statefulness 读的是 `sessionCapabilities.resume`，不是 `loadSession`；只声明后者会让每个 task 都开新 session，且静默 | §1、§4 Phase 1、**§6 决策 A**（两个都声明、都实现） |
| 2 | 并发问题未提 | **缺**。主仓侧一条 pooled 连接允许多 session 真并发，而 `WebSearchTool` / `WebFetchTool` 的 per-turn 状态是共享实例属性、无 ContextVar 隔离 —— 并发 turn 会互相清状态 | §3、**§6 决策 B**（池固定 1，不可配）、§5 风险 2 |
| 3 | "AgentLoop 的 workspace 在构造期固定" + "workspace 由 session id 派生" | **自相矛盾**。一进程一 engine 时后半句做不到，而且 run.py 那条教训的前提是"每次调用一个新进程"，in-process 不适用 | §3 |
| 4 | "wire event 形状 diff ... 工作量主要在这里" | **估错**。事件名与字段名基本对齐，真正的洞是 3 个且全在 outlet 而非 translator；这反过来推翻了移植 `updates.py` 的选型 | §2、§4 Phase 1 |
| 5 | "本分支刚合的 tool-progress display 必须在 acp 模式下禁用" | 半对。管道场景 `resolve_mode` 已自动返回 `"off"`；真正的污染源是 loguru 默认 sink 与第三方库直写 stdout | §3 |
| 6 | （Phase 2 落地后外部 review 一轮）空 prompt 返回 `end_turn` 且零 chunk，违反"每个完成的 prompt 至少一条 message chunk"（主仓零 chunk = `AcpEmptyTurnError`） | **对，已修**：空 prompt 路径先 `_say` 一条说明再 `end_turn` | `methods._session_prompt` |
| 7 | （同轮 review）"cancel 路径应等 TurnFailed 再 settle" | **拒绝**：会挂死 cancel-before-dispatch 窗口，且违背主仓 5 秒 settle 契约；现象本身记为已知坑 | §5 风险 9、11 |

---

## 1. 消费者是谁：主仓的 ACP client 契约

Raven-X 的 ACP 消费者**不是编辑器（Zed 等），而是主 raven 的 subagent 后端**。
编辑器兼容是免费副产品，不是设计目标。主仓 client 侧的事实（均已对码）：

- **入口**：`raven/agent/subagent/backends/acp_agent.py` + `raven/agent/acp/pool.py`。
  每个 agent 一条**长驻 pooled 连接**（模块级单例，按 launch_key = command/cwd/env 去重），
  session 活在连接内，多个并发任务共享一条连接、按 `params.sessionId` 路由。
- **并发是真并发**：连接上的锁是 per-session（`pool.py:128` `_session_locks`），不是一把全局锁。
  N 个并发 task = 一个 Raven-X 进程里 N 个并发 in-flight turn。**这是接入后最大的形态变化**
  （今天是 N 个 run.py 子进程各跑一个），见 §3 并发一条。
- **只调用五个方法**：`initialize`、`session/new`、`session/load`、`session/prompt`、
  `session/cancel`（notification）。`session/list` / `session/resume` / `session/set_config_option`
  等主仓 server 侧扩展，client 侧一概不调（grep `client.py` / `acp_agent.py` / `pool.py` 无调用点）。
- **能力从握手读，不从配置填**：`raven/agent/acp/capabilities.py` 在首连时存 snapshot 到
  `subagent_acp_capabilities.json`（`get_config_path().parent` 下）。失效机制是 **config digest**
  （`capabilities.py:220`）——agent 自身的 build 变了不会让 snapshot 失效。
- **statefulness 与 load 是两个独立的 capability，别混**：

  | 读什么 | 从哪来 | 决定什么 |
  |---|---|---|
  | `snapshot.can_resume` | `"resume" in agentCapabilities.sessionCapabilities`（`capabilities.py:298`，**键存在即为真**，`{"resume": false}` 也算真） | `AcpAgentBackend.is_stateful`（`acp_agent.py:494`）→ 是否去 registry 查已知 session、是否 commit handle |
  | `snapshot.can_load` | `bool(agentCapabilities.loadSession)`（`capabilities.py:300`） | 查到已知 session 后，是否真的发 `session/load` |

  `_open_session` 的门是**先 `is_stateful`，才轮到 `can_load`**。只声明 `loadSession: true`
  而不声明 `sessionCapabilities.resume` → `is_stateful=False` → 每个 task 都走 `session/new`，
  且主仓侧不报错，只表现为"research 每次都重新问一遍澄清问题"。
  处置见 §6 决策 A：两个都声明、都实现。
- **权限一律自动批准**：`raven/agent/acp/permissions.py` —— unattended 场景没有 operator，
  `session/request_permission` 按 kind 顺序 `allow_always > allow_once > ...` 自动选。
  **不要**设计阻塞式的交互问答；elicitation 不会被声明。
- **答案 = `agent_message_chunk` 的累积**（`acp_agent.py:60` `_ANSWER_UPDATES`）；
  `agent_thought_chunk` 进思考流；`tool_call` / `tool_call_update` 是 **message 边界**
  （`acp_agent.py:68` `_BREAKING_UPDATES`）并进 tracing/activity 转录。
- **空 turn = 失败**：没有产出任何 message chunk 的 turn 被当 `AcpEmptyTurnError`
  （`acp_agent.py:619`），带 stderr tail + 本轮被拒方法名上报。
  CLI 模式"answerless 也 exit 0"的宽容不存在了（run.py 自述过这条宽容）。
- **stopReason 不是 `end_turn` 会被标记为残缺回复**：`_finished` 会在文末追加
  `[raven] ... stopped before finishing` 并占用 `max_output_chars` 预算。所以我们的失败路径
  用 `end_turn` + content 落地（§3），而不是自造 stopReason。
- **方言按 `agentInfo.name` 子串匹配**（`acp_dialects/dialect_for` → `_for_name` 用 `key in lowered`）：
  名字里含 "claude"/"codex" 会被套错方言。我们的握手名定为 **`raven-x-research`**
  （稳定、避开关键子串，走 spec 基类方言）。
  注意这与 `subagent.json` 里的注册名 `"Raven-Research"` 是**两个不同的东西**：dialect 只看前者，
  §4 Phase 3 改配置时不要误改另一个。
- **subagent 配置形状**（`raven/agent/subagent/test_state.py:49` `_ACP_FIELDS`）：acp 条目只有
  `command / cwd / env / ready_timeout_ms / timeout`（JSON 里是 camelCase `readyTimeoutMs`），
  外加 `description` / `owns` 等通用字段。
  `resumeCommand` / `transcriptFormat` / `maxOutputChars` 这些 cli 字段全部作废；
  `idSource: provisioned`（现配置里有）不在 `_ACP_FIELDS` 内，是否为通用字段待 Phase 3 确认。

## 2. 实现策略

### §2.1 不引外部 SDK（不变）

放弃"引 PyPI `agent-client-protocol` 0.x"的方案，理由：

1. 主仓实现已经覆盖 questions / tool_kinds / redact / replay / stdio framing，直接可用；
2. 零新依赖（合规上省一次依赖审计；0.x SDK schema 迭代快，锁版本也躲不开追赶）；
3. 协议层与主仓说同一种"方言"，行为一致性免费拿到。

### §2.2 翻译层落在 spine，不落在 wire dict（本稿改选型）

第一稿计划移植主仓 `raven/acp/updates.py`（872 行），它解析的是**序列化之后的 wire event dict**。
复核后改为：**协议层照抄，翻译层重写在 spine 事件层**——写一个 `AcpOutlet` +
`_make_acp_sink`，对照本仓 `raven/tui_rpc/spine.py` 的 `TuiOutlet` / `_make_tui_sink`，
直接翻译 typed `Deliverable` / `TurnEvent`，不经 dict 中转。

改的理由，按权重：

1. **主仓选 dict 层的理由在 Raven-X 不成立。** `updates.py` 首段自陈：那个 sink 要同时截住
   subscription emitter + 三个 broker + MCP event bridge + system methods，所以必须坐在
   `build_rpc_stack` 的出口。本稿 §3 已决定第一版不服务 questions / permissions，也没有
   MCP bridge 需求 —— 唯一要截的就是 turn 事件流本身。
2. **要补的东西正好全在"被 drop"那一栏。** 主仓自己写下的代价是 "this parses a dict that was
   serialised one layer up, and whatever `RpcOutlet` dropped is dropped for good"。本仓
   `TuiOutlet` drop 掉的恰好是三样 ACP 需要的：

   | 洞 | 事实 | 后果 |
   |---|---|---|
   | `ok` | `ToolEvent`（`raven/spine/events.py:64`）**没有 `ok` 字段**，`tool.complete` payload 只有 `tool_call_id`/`result_preview`/`truncated`；而主仓 `_tool_call_update` 读 `payload.get("ok", True)`（`raven/acp/updates.py:414`） | 每个**失败**的 tool call 都会被标成 `completed`，转录静默说谎 |
   | `Notice` | `TuiOutlet` 注释明写 "Notice / MediaOut: eaten"，没有 `notice` wire event | 第一稿 Phase 1 的 "`NoticeKind.PROGRESS`→`tool_call_update` 中间态"**按移植的路子做不出来**；主仓自己也吃掉 PROGRESS/TOOL_HINT，连抄都没得抄 |
   | `MediaOut` | 同上被吃 | `_media` 分支移植过去是死代码 |

   这三样都得改 outlet 才能补；既然要改 outlet，dict 中转层就只剩成本没有收益。
3. **删掉一整类风险。** 第一稿 §5 风险 2（wire 形状漂移 → 静默丢事件）由此从根上消失，
   而不是"靠合同测试钉住"。typed dataclass 改字段名会在导入期炸，dict 改 key 只会静默少一个事件。

代价，明写：放弃与主仓的"同一份 translator"红利，主仓 `updates.py` 后续的修正要人工跟。
`redact.py` / `tool_kinds.py` 是纯函数，照样原样移植复用。

### §2.3 移植清单（源 → 目标都在 `raven/acp/`）

| 主仓文件 | 处置 |
|---|---|
| `stdio.py`（framing）、`protocol.py`、`redact.py`、`tool_kinds.py` | 原样移植 |
| `methods.py`（AcpMethods 路由） | 移植骨架，**已实现为直连 `Scheduler.submit`**（不走 dispatcher `turn.send`：那条路挂在 SubscriptionEmitter 上，与 §2.2 的 spine 选型冲突；`turn.send` 的两个真正需要的守卫——每 session 单 turn、一切出口有 stopReason——由 `AcpSessions` 与 sink 承担）。砍掉的扩展方法见 §6 决策 A |
| `updates.py`（UpdateTranslator） | **不移植**。改写为 spine 层 `AcpOutlet` + `_make_acp_sink`（§2.2）。保留其两条不变量与 `_text_chunk` / `_tool_call` / `_usage_update` 的**映射规则**作为参照 |
| `capabilities.py`、`config_options.py` | 移植，capability 声明按 §6 决策 A 裁剪 |
| `questions.py`（server 侧 broker） | **已移植**（`raven/acp/questions.py`，见 §3 修订）：`QuestionBroker` 复用 TUI 的 notification-out / request-in 形态，capability 门控 |
| `permissions.py`（server 侧 broker） | 暂不移植（消费者自动批准一切 permission），留 TODO |
| `replay.py`（session/load 回放） | Phase 2 移植，接 Raven-X 的 `SessionManager` + `session_resume` |
| `server.py`（run loop：每帧一 task、EOF 时 settle-then-await 的关停） | 移植骨架，engine 构建换成 `raven/cli/tui_commands.py:316` `_build_tui_agent_loop` 的等价物 |

## 3. 关键设计决策

- **stdout 卫生是第一破坏源**：stdout 只放协议帧。入口第一件事复用
  `cli/_log_file.py` 的 `redirect_loguru_to_file` / `_strip_tty_stream_handlers`。
  更正第一稿：tool-progress display（f5ea92c）在 acp 路径上**根本不被构造**
  （它属于 REPL 的渲染栈，`raven acp` 不装配它），无需禁用；
  真正的污染源是 loguru 默认 sink、第三方库直写 stdout、以及任何 `print`——
  由 `claim_stdout`（fd 级接管：块内 fd 1 即 stderr，协议独占原 fd 的 dup）兜底。
  验收靠测试而非靠推理：断言 stdout 的每一个字节都能被 framing parser 消费完。
  stderr 语义升级：会进主仓 journal、空 turn 时作为诊断带回，
  所以日志走文件、**致命错误刻意留一份到 stderr**。
- **prompt 永不用 JSON-RPC error 回答**（主仓 `updates.py` 不变量）：
  失败的 turn 仍以 `stopReason: "end_turn"` 结束，失败原因以 message content 落地
  （不自造 stopReason —— 非 `end_turn` 会被消费者标成残缺回复，见 §1）。
  配合"空 turn = 失败"，要保证一切路径（gate 短路、provider 错误、取消）最终有 chunk。
- **ask_user / 澄清问题：协议级问答按能力协商，handoff 是永久回退**（修订：原决策为
  "不做协议级问答"，在 `askUser.delivery = "tool"` 落地后放开）。客户端在 initialize 的
  `clientCapabilities._meta.raven.askUser` 声明它会渲染问题 UI 时，问题以
  `session/update`（`sessionUpdate: "ask_user_request"`）发出、客户端调 `_raven/clarify_respond`
  作答，答案作为 tool result 回到**同一 turn**（`raven/acp/questions.py`；与 TUI 同构的
  notification-out / request-in，不引入 agent 发起请求的应答关联管道）。
  未声明能力、或 `askUser.delivery = "handoff"` 时，维持原产品形态：DR gate 把澄清问题
  **作为该轮回复**（`agent_message_chunk`）发出，用户答案作为下一个 `session/prompt`
  进同一 session。原决策记录的风险仍然成立——对没有 UI 的客户端做阻塞问答只会换来
  codex 式的整轮 cancel，这正是能力门控存在的原因。handoff 路径的前提不变：同一
  session 真的被复用，即 §6 决策 A 必须落地。
- **DR mode 不需要 ACP session modes**：一进程一配置。`raven acp` 起来时按 config
  （raven-research 的 config.json `drFlow` 固定开启）装配 AgentLoop，
  与主仓 preset 哲学一致（codex 用 env 一次性定死 mode）。
- **并发：一个进程 N 个 in-flight turn，闸门必须显式定。**
  `build_tui` 走 `OriginPools(user=user_pool, system=system_pool)`，
  而 `user_pool` 默认 **1**（`raven/tui_rpc/spine.py:255,279`），是**进程级 semaphore**。
  ACP prompt 走 `Origin.USER`，所以照默认接完 = N 个并发 research turn 排队串行，
  而 research turn 是分钟级的 —— 相对今天 N 个子进程真并行是实质回退。
  两条相关事实：主仓自己的 `raven acp` 也是 `user_pool=1`（`raven/rpc/bootstrap.py:187`
  未传池参数），主仓为 subagent direct chat 专门加了 `direct_pool=8`
  （`raven/rpc/spine.py:553`），而本仓 `OriginPools` 根本没有 direct 池。
  但**不能靠调大池来解决**：`WebSearchTool` / `WebFetchTool` 的 per-turn 状态是共享实例
  属性（`raven/agent/tools/web.py`，全文件无 ContextVar），并发 turn 会互相清 saturation /
  evidence round / seen 集合与预算。所以池**固定 1**，见 §6 决策 B
  —— 以及其后的“决策 B 的后续”，那里改成了一个 session 一套 loop。
  另需审一遍其余进程级共享状态（`tui_rpc/methods/turn.py` 模块级 `_active_turns`、
  memory、token_wise、tracing）。
- **`cwd` / workspace 语义**（更正第一稿的自相矛盾）：
  一进程一 engine（主仓 `server.py` 明写 "one engine per process"），
  AgentLoop 的 workspace 在构造期固定，**不可能**按 session id 派生。
  （“决策 B 的后续”改了后半句：一 session 一 engine 之后，engine 是在
  `session/new` 时才构造的，所以**文件工具的根**确实按 session id 派生了
  —— `<workspace>/acp_workspaces/<chat_id>`。但 `workspace` 本身仍然共享且
  仍在构造期固定，因为 system prompt 的记忆段和 skill 目录都从它读；
  两者的区别见 `CONTEXT.md` 的 Session File Root。）
  run.py 之所以要 workspace-per-session-id，前提是"每次调用都是一个新进程"
  且 `get_data_dir()` 从 config 文件父目录推导、没有单独的 session 目录旋钮
  （run.py:216-223）—— in-process 时这条教训不适用：`SessionManager` 本来就按
  `<workspace>/sessions/<channel>/<chat_id>.jsonl` 分文件，`chat_id = ACP sessionId`
  已经把历史隔开了。
  `session/new` / `session/load` 收到的 `cwd` 记 warning 后忽略。
  **Phase 0 要查的是：除 session JSONL 之外还有什么是 workspace 级的**
  （报告产物、memory、tracing 落盘路径）—— 那些才是并发会话真会撞的东西，
  查完再决定是否需要 per-session 子目录。
  （盘点结果，评审补的一项：**Checkpoint 的 shadow repo**
  `<workspace>/.raven/shadow.git` 也是 workspace 级的，而且是并发下最会撞的一个 ——
  `interactive=True` 让每个 session engine 各建一个 `CheckpointService`，却都指向同一个
  git-dir，并发 turn 收尾会同时 `git add -A` / `commit`：轻则一方报
  `could not lock config file` 降级成 `(None, [])`，重则即使 git 串行化了，
  每次快照 stage 的仍是整棵共享工作树，把别的 session 的改动算进本 turn 的
  `edited_files`，进而写进下一 turn 的 recovery prompt。做法是让快照跟着
  **写入根**走（`file_workspace`，即文件/exec/media 工具的根），而不是跟着共享
  `workspace` 走 —— 其他入口 `file_workspace` 就是 `workspace`，行为不变。
  memory backend 是进程内一份、按 session_id 分 key，写入闸门共享有界；
  tracing 未在本期改动范围内。）
- **答案语义：ACP turn 跑非流式（`stream=False`），答案 = turn 最终提交的一条 `Text`**。
  真 LLM e2e 实测：`stream=True` 下 token 流承载模型整个 turn 说出的一切——工具调用前的
  叙述、DR verify/finalize 对答案的重述——报告在 message 流里出现了两次；而消费者把
  `agent_message_chunk` 累积当答案（`_ANSWER_UPDATES`），这会直接污染报告。
  非流式下 `run_turn` 只发一条 closing `Text`（"nothing streamed" 才发，`main.py:4504`），
  即 turn 最终提交文本——与 run.py 今天从 session JSONL 读到的是同一个东西，
  且与被测量的产品面（`raven agent -m`、REPL）同为非流式代码路径。
  代价：无增量答案与 reasoning deltas；tool_call 活动与 progress notice（→ thought chunk）
  照常流出，客户端仍能看到研究在进行。`AcpOutlet` 的流式路径保留但休眠。
- **turn 语义**：每 session 单飞行 turn（沿用 `tui_rpc/methods/turn.py` 的槽位模式）；
  `session/prompt` 挂起直到 sink 收到 `TurnEnded`/`TurnFailed` → 映射 `end_turn` / 失败内容；
  `session/cancel` → `TurnHandle.cancel()` → `stopReason: "cancelled"`
  （注意 tui 里 cancelled 的 `TurnFailed` 是静默的 —— `error` 事件由 `turn.cancel` 自己发，
  ACP 侧要在 prompt 响应里补上）。
- **每个入站帧独立 task**（主仓 `server.py`）：`session/prompt` 挂起期间必须能读到
  `session/cancel`，inline 处理会让取消永远不可达。

## 4. 分期

### Phase 0 — 骨架、卫生与倒序验收
1. 新包 `raven/acp/`（按 §2.3 移植）+ `raven/cli/acp_commands.py` 提供 `raven acp`
   （与主仓命令同名），注册进 `commands.py`。
2. stdout/stderr 卫生（§3 第一条）+ stdout 字节纯净度测试。
3. **倒序验收（新增，用来把 §6 决策 A 的风险提前到第一天）**：先做一个只答 `initialize`
   的空壳 server，用主仓 `probe.py` 的 acp 探测打一次，确认落盘的
   `subagent_acp_capabilities.json` 里 `canResume` / `canLoad` 取值与预期一致，
   再往里填 turn 逻辑。否则 A 类错误要等到 Phase 2 验收才暴露。
   **注册成一个一次性的临时 agent 名**（不是 `raven-research`），探完即删该配置行与
   snapshot 文件 —— 否则会给真名留下一份 `canLoad=false` 的缓存，正是决策 A 末条要避免的。
4. workspace 级资源盘点（§3 workspace 一条最后一段）。

### Phase 1 — 核心回路
- `initialize`：`agentInfo.name = "raven-x-research"`，promptCapabilities 仅 text。
  capability 按 §6 决策 A：`loadSession: true` + `sessionCapabilities: {"resume": {}}`，
  不声明 `list` / `close` / `delete` / `fork`。
  **但 Phase 1 期间不把 `kind: "acp"` 注册进主仓**（决策 A 末条）：本期的验收全部走本仓
  stub client，主仓的第一次探测留到 Phase 2 之后，从而绕开 config-digest 型 snapshot 缓存。
- 装配 Scheduler 时 `user_pool` 固定 1（§6 决策 B），并在构造处写明原因。
  （已被“决策 B 的后续”取代：现在是 `acp.userPool`，默认 2，非 per-session 注册表时启动即 fail loud。）
- `session/new`：铸 session id，映射 `Source(channel="acp", chat_id=<sessionId>)`。
- `session/prompt`：构造 `TurnRequest(Origin.USER)` 提交 Scheduler，per-session future 等 turn 终结。
- `session/cancel`。
- **`AcpOutlet` + `_make_acp_sink`**（§2.2，对照 `tui_rpc/spine.py`）：
  - `StreamDelta` / 非流式 `Text` → `agent_message_chunk`
  - `Reasoning` → `agent_thought_chunk`
  - `ToolEvent(START)` → `tool_call`（`kind`/`locations`/`title` 用移植的 `tool_kinds.py` + `redact`）
  - `ToolEvent(COMPLETE)` → `tool_call_update`。**`status` 需要一个 `ok` 的来源**：
    `ToolEvent` 现在没有这个字段（`raven/spine/events.py:64`），要么按注册表的失败约定
    （model-facing 文本以 `Error` 开头，`cli/_progress_line.py:101` `looks_failed` 已有同款判断）
    在 outlet 里判，要么给 `ToolEvent` 加字段。前者零侵入，先走前者。
  - `Notice(PROGRESS/TOOL_HINT)` → **`agent_thought_chunk`**（修正第一稿的
    "`tool_call_update` 中间态"：`Notice` 不携带 `tool_call_id`，构不成合法的
    `tool_call_update`；进 thought 流既可见、又不会被消费者当答案文本累积——
    进 `agent_message_chunk` 会污染 research 答案）。这是 outlet 的实际改动项，不是移植项。
  - `TurnEnded` → `usage_update`（映射到 `context_used`/`context_max`/`cost_usd` 三元组，
    对照主仓 `_usage_update`）+ `stopReason: end_turn`；`TurnFailed` → content + `end_turn`。
- 空 turn 不变量 + 失败以 content 落地。

### Phase 2 — session/load（resume）【已实现】
- `raven/acp/replay.py`：主仓 `replay.py` 的移植，**重新键控到本仓的存储形状**
  （对码 `_save_turn` + 真实 session 文件验证）：文本在 `content` 而非 `text`；
  `tool_calls` 是 OpenAI 形（`function.name` + `arguments` JSON 串，兼容平铺形）；
  本仓不写 `diff`/`notice`，相应分支不带过来。
- `methods.py`：`_session_load`（replay 先于响应上线）与 `_session_resume`
  （= load 减 replay，同一 `_reopen` 助手）。存在性判定走**引擎自己的**
  `SessionManager.peek`（fresh manager 无缓存，load 后的 turn 会看不见），
  且**只认 `acp:` 前缀的 id**——引擎 manager 也存着 `cli:` 会话，
  放行等于把终端转录从这个面读出去。
- capability 同步翻转：`loadSession: true` + `sessionCapabilities: {"resume": {}}`
  （§6 决策 A 的最终形状，`list`/`close`/`delete`/`fork` 不声明）。
- `raven acp` 增加 `--config`（run.py Phase 3 以 `--config <rendered>` 拉起，
  与 `raven agent` 同形）。
- stateful 是 raven-research 产品形态的前提，本期落地后接入才算完成；
  已由真 LLM e2e 验证（跨进程 load + 澄清→回答同 session 续跑）。

### Phase 3 — 接入件改造（主仓侧）
- `subagents/raven-research/subagent.json`：`kind: "cli"` → `"acp"`，
  `command` 改为拉起 Raven-X 的 acp 入口，加 `readyTimeoutMs`；
  删 `resumeCommand` / `transcriptFormat` / `maxOutputChars`。
  确认 `idSource: provisioned` 在 acp kind 下是否仍被读取（不在 `_ACP_FIELDS` 内）。
  注册名 `"Raven-Research"` 与握手名 `raven-x-research` 是两回事，别改错（§1 末）。
- run.py 缩成薄 launcher：只保留 `.env` → config 的 secret 注入，然后 exec acp 入口
  （凭据继续不落 config 文件，遵守密钥管理红线）。
- 用主仓 `probe.py` 的 acp 探测 + capabilities snapshot 验收注册结果。

### 测试
- 合同测试：移植主仓 `tests/test_acp_*.py` 体系（`acp_stub_server.py`、`acp_frames.py`、
  `test_acp_stdio.py`、`test_acp_replay.py` 优先）。`test_acp_updates.py` 因 §2.2 改选型，
  改写为对 `AcpOutlet` 的 spine 事件级测试：每个 `Deliverable` 子类一个 case，
  断言产出的 `session/update` 帧符合官方 schema。
- **capability 断言（对应 §6 决策 A）**：断言 `initialize` 响应里
  `sessionCapabilities` 与 `loadSession` 的组合，能让主仓 `_handshake`（对码）算出
  `can_resume=True` 且 `can_load=True`。这条测试是防 A 类回归的唯一闸门。
- **dialect 断言**：`dialect_for(<我们的 initialize 响应>)` 返回 spec 基类。
- **stdout 纯净度**：子进程拉起 `raven acp`，跑完一轮，断言 stdout 全部字节被 framing 消费完。
- **并发闸门**（已按“决策 B 的后续”改口径）：断言 `user_pool > 1` 在**非** per-session
  注册表上启动即 fail loud；断言两个**不同** session 并发 prompt 时确实并行（总墙钟约等于
  最慢的一个，且各自跑在自己的 engine 上）；断言同一 session 上的第二个 prompt 仍被
  `TurnAlreadyRunningError` 拒。同时断言进程级状态不互相踩 —— 现在具体是
  `WebSearchTool` / `WebFetchTool` 的 per-turn 状态每 session 一份，
  而 memory 写入闸门每进程一份。
- 冒烟：对照 `tests/integration/test_tui_rpc_production_smoke.py` 写 acp 版
  （子进程拉起 `raven acp`，走 initialize → new → prompt → update 全链）。
- e2e：主仓 spawn Raven-X 跑完整"澄清 → 回答 → follow-up"一轮，验证 stateful 路径。

## 5. 风险与坑（按代价排序）

1. **statefulness 声明错位**：只声明 `loadSession` 会让 `is_stateful=False`，每个 task 开新
   session，**且不报任何错**，只表现为 research 反复问同样的澄清问题。
   闸门：§4 测试的 capability 断言 + Phase 0 倒序验收。
2. **有人把并发池调大**：`OriginPools(user=1)` 必须固定（§6 决策 B）——`WebSearchTool` /
   `WebFetchTool` 的 per-turn 状态是共享实例属性，无 ContextVar 隔离，两个并发 turn 会互相
   清对方的 saturation / evidence round / seen 集合与预算。功能上不报错，只是分布被污染，
   且 §0.2 意义上无从记录。闸门：不提供旋钮 + §4 并发测试断言 >1 时 fail loud。
   代价（N 个 research task 串行）见 §6 决策 B 末段与 §7。
   该串行化已由 per-session loop 解除，见“决策 B 的后续”；这条风险现在指的是
   在**非** per-session 注册表上调大池子，闸门是启动时 fail loud。
3. **stdout 污染**：一行日志就打碎 framing。Phase 0 完成前不接任何真实 turn。
4. **`ok` 缺失导致转录说谎（双向）**：status 由 `_looks_failed` 启发式（结果预览以
   `Error` / `{"error"` 开头）推断，两个方向都会错——不符合注册表约定的失败被标
   `completed`；一个正常抓取的、恰好以 "Error" 开头的页面被误标 `failed`。对主消费者
   status 是纯展示（答案不取决于它），只影响转录准确性。根治是给 `ToolEvent` 加 `ok`
   字段（侵入 spine，暂不做）。闸门：outlet 级测试里放一个失败 tool 的 case。
5. **`loadSession` 声明与实现脱节**：声明了但回放不完整 → 主 raven 把残缺历史当完整上下文，
   follow-up 答非所问。处置是排期而非补救：Phase 2 落地前不让主仓探测我们
   （§6 决策 A 末条），因为 snapshot 失效靠 config digest，事后翻 flag 很难让它重探。
6. **turn 终结事件重复/丢失**：cancel + sink 失败事件是正常时序不是 bug，
   照抄主仓 `_Turn` 的"恰好一个终结事件 resolve prompt、第二个 no-op"门。
7. **`agentInfo.name` 撞方言**：只在命名评审时防一次即可，测试里断言 `dialect_for` 返回 spec 基类。
8. **翻译层与主仓分叉**（§2.2 自选的代价）：主仓 `updates.py` 后续修正要人工跟，
   没有测试能替我们发现"主仓改了映射规则"。
9. **cancel 后的尾巴帧窗口**（已知、有意保留）：`session/cancel` 无条件立即 settle
   prompt，被取消 turn 已入 hub 队列的 `tool_call_update` 等帧可能在 `"cancelled"`
   响应之后上线；若客户端紧接着在同 session 发下一个 prompt，天真的 UI 客户端可能把
   尾巴帧归到新 turn 头上。主消费者 cancel 即弃整个 task，不受影响。
   **不要**改成"等 sink 的 TurnFailed(cancelled) 到达再 settle"：(a) cancel 落在
   `begin_turn` 与 scheduler 接单之间时 TurnFailed 永远不来，等待挂死 prompt
   （`_session_cancel` docstring 记录的窗口）；(b) 卡在不可取消阻塞里的工具会让
   TurnFailed 迟到超过主仓 `_CANCEL_SETTLE_S = 5.0`，settle 超窗 → registry unbind →
   下一次 dispatch 开 fresh session，研究上下文全丢。立即 settle 正是保证永远在
   5 秒窗口内的设计。将来若要收紧帧序，方向是"settle 后丢弃同 turn 的迟到
   deliverable"（per-turn epoch），而非推迟 settle。
10. **`write_frame` 是事件循环上的阻塞写**：客户端停止读 stdout、管道缓冲写满
    （约 64KB）时 `flush()` 卡住整个 loop——连 `session/cancel` 都读不进来。主消费者
    持续读，实际风险低；出现真实场景再引入写线程/队列，不预付复杂度。
11. **cancel 到 settle 的时延是硬契约**（§9 的另一面）：我们侧 lane cancel → settle
    本身即时，风险在工具——一个卡在不可中断阻塞里的 `exec` / `web_fetch` 会吃掉主仓的
    5 秒窗口，代价是 fresh session。Phase 3 验收时加一条 "cancel→settle 时延" 检查。

## 6. 已定决策

### 决策 A — statefulness：`sessionCapabilities.resume` + `loadSession`，两个都声明、都实现

`_open_session` 有两道门，缺一不可：

```
is_stateful (= "resume" in sessionCapabilities)  ->  才会去 registry 查已知 session
can_load    (= loadSession)                      ->  查到之后才会真的发 session/load
```

**为什么不"声明但不实现"**：

1. `sessionCapabilities` 是 spec 字段（主仓注释：an empty object is how the schema spells
   "supported"），声明它是对整个生态的承诺，不只是骗过 raven 的 probe。
2. 主仓 `probe.py` 会把能力打给人看（`raven/agent/acp/capabilities.py:411-413` 拼
   `resume`/`load` 列表），撒谎会让 `raven subagent test` 报告一个调用即 method-not-found 的能力。
3. 它一定会被发现：空 turn 的诊断路径专门收集 `refused agent requests` 并回报
   （`acp_agent.py:619` 附近）—— 谎话正好在最难排查的场景浮出来。

**为什么成本几乎为零**（第一稿把这条估错了）：对码主仓 `raven/acp/methods.py`，
`_session_resume` 就是 `_session_load` **减掉 replay 循环**。两者都 `_call("session.resume")`、
都比对 id 并在不匹配时抛 `-32002`、都 `_bind_workdir`、都建/更新 `AcpSession` + subscription、
都 `_announce_commands`。所以实现顺序是 **Phase 2 先做 `session/load`，`session/resume`
是同一个函数减掉 replay**，约十几行。

**为什么不改主仓 `is_stateful` 为 `can_resume or can_load`**：语义上那样最正确
（`resume` 键在这里被当成 statefulness 标志用，真机制是 `session/load`），但那是改主仓
**client 侧**，会同时改变 claude_code / codex / opencode / hermes / openclaw 五个 agent 的
handle 绑定行为。为一个 agent 动所有 agent 的共用判据，收益不对等。若日后主仓愿意收，
可作为独立提案。

**两条实现约束**：

- 只声明 `resume`，**不**跟着主仓声明 `list` / `close` / `delete`。client 从不调它们，
  declare 就是承诺，最小面积更诚实。`fork` 同理不声明（`can_fork` 也是键存在即为真）。
- **不要经历 false -> true 的翻转。** snapshot 失效靠 config digest
  （`raven/agent/acp/capabilities.py:220`），agent 自身 build 变了不会重探。本 plan 的分期
  本来就是 Phase 2（load/resume）在 Phase 3（主仓注册 `kind: "acp"`）之前 —— 严格按这个顺序，
  主仓从第一次探测起看到的就是最终能力，不存在 stale snapshot 要处理。
  **推论：Phase 1 结束时不要把 `kind: "acp"` 注册进主仓**，冒烟/合同测试全部走本仓自己的
  stub client。

### 决策 B — 并发池：固定 1，不做成可配置的数字

`user_pool=1` 不是保守调参，是当前 `AgentLoop` 唯一正确的值。

**根据**：`raven/agent/tools/web.py` 里**一个 ContextVar 都没有**。`WebSearchTool` 的 per-turn
状态全是实例属性 —— `self._prior` / `self._searches` / `self._retry_budget` /
`self._snippet_seen` / `self._result_seen` / `self._snippet_repeat_marks` /
`self._evidence_round` / `self._saturation`（见 `web.py:319` `start_turn` 的函数体）；
`WebFetchTool`（`web.py:930`）同样式，携带 retry budget。
而 `run_turn` 在每轮开头对这些**共享实例**调 `start_turn()`
（`raven/agent/loop/main.py:3975-3991`）。

于是一个 AgentLoop 上两个并发 turn：

- turn B 的 `start_turn()` 清掉 turn A 飞行中的 saturation 状态、evidence round、seen 集合；
- 两个 turn 共用一份 search count 和 retry budget；
- saturation 的 stop flag 会替一个没饱和的 turn 关掉搜索。

对照 `MessageTool`：它**是** ContextVar 隔离的，docstring 明说为了并发
（`raven/agent/tools/message.py:15-18,48`）。说明这套隔离是逐个工具手工做的，
而 DR 最吃紧的那个工具没做。

**而且这不止是 bug，是 measurement 问题**：按 AGENTS.md §0.2，凡改变"模型读到什么 /
哪些 turn 被重采样"的都要 bump `drFlow.version`。并发串台会改变生成分布，
且 `arm_env.json` 记不下任何痕迹。

**结论**：

- 固定 1，并在代码里写明它为什么是 1 —— 否则下一个人会把它当调参项。
- **不暴露数字旋钮。** `RAVEN_ACP_USER_POOL=4` 调大了不会报错，只会静默污染 dr 状态，
  是最糟的一类配置项。特别注意 `env` 在 `_ACP_FIELDS` 里，主仓**有能力**从 `subagent.json`
  塞变量进来；只要定义了这个名字，就一定有人会去设。
- 若确需逃生阀：做成**只接受 1、>1 时启动即 fail loud 并打印原因**，
  或门控在"per-session AgentLoop 已落地"之后。宁可 fail loud，不要静默串台。
- 真要恢复并行，正确形状是**一个 session 一个 AgentLoop**，不是调大 semaphore。
  前置是把 `WebSearchTool` / `WebFetchTool` 的 per-turn 状态搬进 ContextVar 或做成
  per-turn 对象 —— 独立一期，且本身就是 §0.2 意义上的分布变更。
- 旁证：主仓自己的 `raven acp` 也是 `user_pool=1`（`raven/rpc/bootstrap.py:187` 不传池参数）。
  现在看这不是疏忽，是同一个约束。

**代价明写**：pool=1 意味着 N 个并发 research task 串行，相对今天 N 个 run.py 子进程
真并行是回退。

#### 决策 B 的后续（已落地，见 `raven/acp/loops.py`）

上面的论证仍然成立，但两点按实际改了：

1. **§7 的待确认已有答案：会并发。** 所以决策 B 末段那一期已经做了 ——
   `AcpLoops` 按 conversation id 一个 session 一套 `AgentLoop`（一套 tools + flow 对象），
   provider / `SessionManager` / memory backend / token_wise 仍进程内一份。
2. **"前置是搬 ContextVar"这句是错的。** per-session loop 之后每个 session 持**自己的**
   tool 实例，跨 session 并发不共享任何对象；同 session 内并发被两道闸挡着
   （`AcpSessions.begin_turn` 抛 `TurnAlreadyRunningError`，宿主侧 `session_lock` 更在前面）。
   ContextVar 化只在"同一 session 内并发多 turn"时才需要，而协议层本来就禁止 ——
   因此它可以独立排期，甚至永远不做。

"数字旋钮"那条按原文的逃生阀形状实现：`acp.userPool`（默认 2）+ `acp.maxLoops`，
且 `build_acp` 在 `user_pool > 1` 而注册表不是 per-session 时**启动即 fail loud**。
`AcpLoops.single()` 就是那个非 per-session 形状。

## 7. 待确认（不阻塞开工）

- ~~**DR 场景会不会并发 spawn / DAG 并行发 research 任务？**~~ **会。** 已按决策 B 末段
  做成 per-session AgentLoop，见上面"决策 B 的后续"。
- `idSource: provisioned` 在 acp kind 下是否仍被读取（不在 `_ACP_FIELDS` 内）—— Phase 3 确认。
- ~~workspace 级资源盘点结果（§3 workspace 一条）~~ 见 §3 该条末段的盘点结果：
  文件工具根与 Checkpoint 的 shadow repo 都已按 session 分开；tracing 落盘路径未改。
