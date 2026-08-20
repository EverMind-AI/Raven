# Raven-X 蜂群集成分支（feat/swarm_integration）使用说明

> 场景：**外部主 agent 是编排者，Raven-X 是被派活的子 agent（工人）**。
> 本分支为此定制了默认行为（下表）；评测主线 `raven-x` 分支保持评测口径，两条线的优化
> 会从 `raven-x` 定期单向合入本分支。**集成侧的调整请对本分支发 PR，不要直接推。**

## 本分支与评测主线（raven-x）的行为差异

| 行为 | 本分支（蜂群集成） | raven-x（评测主线） |
|---|---|---|
| web_fetch / web_search | **默认开**（web_fetch 免钥；web_search 需在配置里给搜索 key） | 默认关（断网评测口径） |
| 五道完成护栏 | **默认全关**——非编码任务（解释代码、问答）不被"改代码专用"的门误拦；编码任务用环境变量逐个打开 | 一次性任务默认全开 |
| stdout | **干净**：就是模型回复，可直接当结果消费；仅护栏真触发时多一行 `gate_triggers: ...` | 每轮固定打一行计数 |
| 长期记忆（EverOS） | **默认开**——但只在配好记忆模型 key 时生效，没配 key 自动降级为"无记忆"（零延迟零报错） | 默认关（防跨题串味；评测 config 必须钉 `"memory": {"backend": null}`） |
| 定时任务服务（CronService） | agent 路径**已整个移除**（个人助理遗产，编码工人用不到，也不再在配置目录旁留空 `cron/`） | 同（已合入） |

护栏环境变量（语义：设 `0/false/no/off/disabled` 强制关，其他值强制开）：
`RAVEN_REQUIRE_REAL_TEST_EVIDENCE`（测试证据）、`RAVEN_VERIFY_BEFORE_COMPLETE`（完成前验证）、
`RAVEN_GATE_STALE`（证据过期）、`RAVEN_GATE_RED`（红灯质询）、`RAVEN_GATE_EMPTY_DIFF`（空补丁证伪）。

```bash
# 给"改代码"类任务打开护栏（问答/调研任务什么都不用设）：
RAVEN_REQUIRE_REAL_TEST_EVIDENCE=1 RAVEN_GATE_EMPTY_DIFF=1 raven agent ...
```

## 怎么启动

```bash
raven agent \
  --config /path/to/raven_config.json \   # 模型端点等配置；配置文件所在目录 = raven 的"家"
  --workspace /path/to/被操作的仓库 \
  --session cli:<工人唯一名> \             # 多轮交互的关键，见下文
  --message "任务描述" \
  --no-markdown
```

产出两样：**workspace 里被改的文件** + **stdout 的回复文字**。
raven 的自身状态（会话轨迹、缓存）都放在配置文件所在目录下，**不往 workspace 写任何自己的文件**；
会话轨迹在 `<配置目录>/sessions/<转义的workspace路径>/cli/*.jsonl`。

最小配置示例（OpenAI 兼容端点）：

```json
{
  "agents": {"defaults": {"model": "<模型名>", "provider": "custom",
                           "maxTokens": 32768, "contextWindowTokens": 262143}},
  "providers": {"custom": {"apiKey": "<key>", "apiBase": "https://<端点>/v1",
                            "models": ["<模型名>"]}},
  "tools": {"restrictToWorkspace": true, "sandbox": {"backend": "none"}}
}
```

## 调用契约

1. **默认无状态**：每次调用铸造全新会话；续做必须两次传同一个 `--session`。
2. **结果协议自己定**：建议任务提示词里约定"把结论写到指定路径的结果文件"，主 agent 只认文件、
   stdout 仅作日志；本分支 stdout 已是干净回复，直接消费也可以。
3. **`ask_user` 不会挂死**：无人值守下没有问答通道，模型调它会立刻收到错误字符串，不阻塞。
   建议派活提示词里明说"无人值守，含糊处自行选合理默认并在结果中说明"。工人真想问的问题，
   放在收尾回复里问，主 agent 下一轮带答案进来（见下节）。
4. **它是写死的编码身份**：能答疑、解释代码，但行为模式是软件工程师；
   按"编码 + 代码问答"工人使用。

## 多轮交互（和 claude-code 同款模式）

一次调用 = 一轮；**同一个 `--session cli:<名>` 贯穿多次调用 = 多轮对话**。

```
主 agent                              Raven-X (--session cli:worker-auth)
   │  ① -m "修复登录超时bug"
   │ ───────────────────────────────▶ 自主干几十步
   │  ◀─────────────────────────────  stdout: "已修复。但发现两个超时字段语义重叠,用哪个?"
   │  ② -m "用 connect_timeout,补上单测"
   │ ───────────────────────────────▶ 带着第①轮全部上下文继续
   │  ◀─────────────────────────────  stdout: 最终汇总
```

机制要点：

- **存储**：一个会话一个 JSONL 文件，锚在配置目录、按 workspace 路径分桶。
  **坑：同一个 session 名 + 不同 workspace 路径 = 两个不相干的会话**，续话时 workspace 必须逐字符一致。
- **会话名**：传完整形式 `cli:<唯一名>` 最稳；**蜂群里别用 `--continue`**（并行工人下是竞态的）。
- **第二次调用装回什么**：全部对话历史（上限最近 500 条消息；大工具输出只存头 12k + 尾 4k 字符）；
  历史超 token 预算时从最老的整轮开始丢；系统提示词每次重新组装（仓库的 AGENTS.md 变了会生效）。
- **同一个 session 的多次调用必须串行**（编排层保证）；两个工人同时改同一个仓库的隔离也是编排层职责。
- 中途实时问答（干到一半停下来问、不结束本轮）目前做不到，需要给 `ask_user` 接阻塞式双向通道；
  建议先用轮次边界模式，真实撞到"歧义中途才发现"的案例再谈。

## 长期记忆（EverOS）

`memory.backend` 在本分支默认 `"everos"`。**默认值只在"能工作"时生效**：没配记忆模型 key 时
自动降级为无记忆（不起服务、不拖慢、不报错）；而在 raven config 里**显式**写了 `"backend": "everos"`
则严格执行——起不来就响亮报错（一次性任务直接失败退出），声明过的意图不被静默取消。

### 本地模式（默认）

不配 `base_url` 时 raven 自动拉起自带的 everos 服务（本机 18791 端口，v1 接口）。
服务启动就需要**两个模型 key**（LLM 抽取 + embedding 检索，缺一个都拒绝启动；rerank 可选）。
两种输入方式，**都不写进 raven 的 config.json**：

1. 向导（推荐）：`raven onboard` 第 4 步，逐项收集后写入 `<配置目录>/everos/everos.toml`
   （`[llm]` / `[embedding]` 小节的 `model` / `api_key` / `base_url`）。
2. 环境变量（无人值守部署）：`EVEROS_LLM__API_KEY` / `EVEROS_LLM__BASE_URL` / `EVEROS_LLM__MODEL`
   与 `EVEROS_EMBEDDING__API_KEY` / `EVEROS_EMBEDDING__BASE_URL` / `EVEROS_EMBEDDING__MODEL`，
   环境变量优先于 toml。任意 OpenAI 协议端点均可。

### 云端 / 远端模式

指向一个已在运行的 EverOS 服务时，配置写在 raven config.json 的插件小节：

```json
{
  "plugins": {"config": {"everos-memory": {
    "base_url": "https://<服务地址>",
    "api_version": "v2",
    "api_key": "<服务的访问token>"
  }}}
}
```

- `api_version`：**托管 Cloud 只挂 `/api/v2`，自带服务只挂 `/api/v1`**（默认 v1）；配错时 404
  会报出该改哪个旋钮。
- `api_key` 也可用环境变量 `EVEROS_API_KEY` 传，避免落盘。
- 配了 `base_url` 就不做本地 key 降级检查——远端服务的凭据与本地模型 key 无关。

### 隔离语义（对蜂群重要）

- 检索**默认钉在当前 `--session`**：工人 A 的记忆不会串进工人 B 的上下文
  （插件配置 `scope_recall_to_session: false` 可放开为跨会话记忆，个人助理场景用）。
- 存储按 workspace 路径派生 `project_id` 分桶：不同仓库的记忆物理隔离。
- 抽取是服务端异步的（`flush_every_turns` 默认 0，不做每轮无效 flush）；
  同 session 内的召回不受抽取滞后影响（带 session 过滤的检索会返回未抽取的缓冲尾巴）。

## 蜂群规模化

- **掐住它自己的裂变**：内部有 `spawn`（派生内部子 agent）工具，默认并发 4、每小时 30；
  配置项 `maxConcurrentSubagents` / `maxSubagentSpawnsPerHour` 按预算掐小。
  内部子 agent 的 workspace 自动继承主循环的。
- **多实例并行安全，但一实例一个配置目录**：会话写入有跨进程文件锁；日志/缓存也锚在配置目录下，分开最干净。
- **预算与"被杀"**：单次调用的预算旋钮是 `maxToolIterations`（迭代轮数），没有 token 预算旋钮；
  被超时/强杀的进程不会有任何收尾输出，统计时和"正常结束"分开归类。

## 部署与换模型

- **必须用 site-install 语义打包**（依赖装进随附解释器自身的 site-packages），
  **不要用设 PYTHONPATH 的方式启动**——那会把 raven 的依赖泄进它替任务跑的所有子进程。
- **建议开 `restrictToWorkspace`**：文件工具限制在 workspace 内，多工人共享机器防越界互写。
- **换模型 = 改两处**：`agents.defaults.model`（模型名）+ 对应 `providers.<厂商>` 小节的
  `apiBase`（端点地址），两处一起改。模型名不决定端点；缺 `apiBase` 时底层会静默回落到
  `OPENAI_BASE_URL` 甚至官方 openai 端点。主循环、内部子 agent、压缩摘要三者自动跟随主模型。
  按模型名联动的还有：提示词模板按模型家族选、`generation.model_overrides` 按模型名子串匹配参数覆盖。
- **配置完看一次真实请求**（开 `--logs` 或抓端点日志）：配置写了不等于生效。
  DeepSeek 官方端点 thinking 模式的 reasoning_content 回传 400 问题本分支已修复
  （行为自适应 + 模型名钉死双条件）。

## 协作约定

- 本分支按 `swarm-v0.x` tag 发布快照；集成侧改动请从 tag 切分支、以 PR 形式回到本分支。
- 评测主线（raven-x）的热区是 `raven/agent/loop/` 的护栏区域和 `raven/context_engine/`，
  集成改动尽量避开；要动 CLI 参数或配置 schema（双方都碰的接口层）先打招呼。
