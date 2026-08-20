# 统一 agent 注册表：把内置 agent 也放到表上

**状态**：设计稿，待评审。
**前置**：`2026-08-11-external-agent-registry-design.md`（外部 agent 并表，已实现在 main，`b4ae7a0`）。

## 1. 目标

现状：`subagents.thirdParty[]` 上有 3 种 kind（`cli` / `acp` / `openai`），内置 raven subagent 不在表上——它是 `spawn(task=...)` 不传 `agent` 时的默认分支，无名字，不出现在 roster 里。

本期：加 `kind: "builtin"`，让一张表登记所有可调的 agent。**不新增任何字段。**

不做：不改 spawn / DAG 调度；不改现有三种 kind 的字段；不动能力快照机制（builtin 无需测量，快照表不加来源列）；不给 builtin 加可配项（见 §7）。

## 2. 一个语义：调用方式

| 表回答的问题 | 由什么回答 |
|---|---|
| 有什么可以调 | `name` + `description` + `enabled`，投影成 roster 给模型读（§4） |
| 怎么调 | `kind` 决定造哪个 backend，该 kind 的字段给参数（§3） |

`kind` 是表上唯一的分类维度：`builtin` / `cli` / `acp` / `openai`。

`preset` 不是维度，是溯源字段：记这行从哪个内置 preset 生成，供 UI 区分改过名的 preset 条目和手写条目，运行时不读。

## 3. 全表字段

| 字段 | builtin | cli | acp | openai | 含义 |
|---|---|---|---|---|---|
| `name` | ✅ | ✅ | ✅ | ✅ | 模型在 `spawn(agent=...)` / DAG 节点 `subagent` 里打的字符串。全表唯一（§5.2） |
| `kind` | ✅ | ✅ | ✅ | ✅ | 见 §2 |
| `description` | ✅ | ✅ | ✅ | ✅ | roster 行。acp 留空则用握手报的 agentInfo |
| `enabled` | ✅ | ✅ | ✅ | ✅ | 是否出现在 roster。只在拼 roster 时读，不从探测结果推导。builtin 行不得为 false（§5.1） |
| `preset` | — | ✅ | ✅ | ✅ | 溯源 |
| `timeout` | — | ✅ | ✅ | ✅ | 一次任务上限。null = 无自动限制，靠人手停 |
| `max_output_chars` | — | 30000 | 30000 | 128000 | 回传多少输出 |
| `command` | — | ✅ | ✅ | — | cli：argv 模板，`{prompt}` = 任务文本作单个 argv token，`{prompt_file}` = prompt 文件路径，都没有则走 stdin。acp：启动 server，一个连接起一次，不是一次任务起一次 |
| `cwd` / `env` | — | ✅ | ✅ | — | 子进程工作目录与环境变量 |
| `resume_command` | — | ✅ | — | — | 设了它就是 stateful；instance 句柄绑到 CLI 自己的 session id，用 `{agent_id}` 代入 |
| `id_source` | — | ✅ | — | — | `provisioned` = raven 生成 id 传进去；`derived` = CLI 生成、raven 从 transcript 读回 |
| `session_id_pattern` / `output_pattern` | — | ✅ | — | — | 上面"读回"用的正则 |
| `transcript_format` | — | ✅ | — | — | `text` / `codex_jsonl` / `claude_stream_json` / `openclaw_json` / … |
| `stateful` | — | ✅ | ❌ | ✅ | 声明；null 时从 `resume_command` 派生，显式值必须与之一致 |
| `reads_local_files` | — | ✅ | ❌ | ✅ | 能否递本机路径。false 时 DAG 节点必须传文件内容 |
| `ready_timeout_ms` | — | — | ✅ | — | 握手预算，默认 30000 |
| `base_url` / `model` / `api_key` | — | — | — | ✅ | 端点 |
| `system_prompt` / `temperature` / `max_tokens` | — | — | — | ✅ | 请求参数 |

`—` = 该 kind 不适用。`❌` = 明确禁止（`ACP_UNSUPPORTED_FIELDS`）：这类事实来自 `initialize` 握手，不许有第二个来源。

builtin 一行只有前 4 个字段。它没有 `timeout` / `max_output_chars`，与今天的内置 subagent 一致（自动超时是被刻意去掉的）。

## 4. 模型看见 4 个

`AgentMeta` 是表投影给模型的全部；"怎么调"的字段一个都不给它。

| 投影字段 | builtin | cli | acp | openai |
|---|---|---|---|---|
| `name` | 表 | 表 | 表 | 表 |
| `description` | 表 | 表 | 表，空则用握手 | 表 |
| `stateful` | 恒 false | 从 `resume_command` 派生 | 从握手 `can_resume` | 恒 false |
| `reads_local_files` | 恒 true | 表（声明） | 从握手 | 恒 false |

能力一律派生，不许在 builtin / acp 上手填：同一个 ACP 协议下 codex `can_fork=false` 而 hermes / opencode / claude_code 都是 true，手填的字段必然在某一家上是错的。派生只写一处，因为有三个消费者（spawn manager、DAG roster、DAG 能力预检）。

## 5. 两条新规矩

### 5.1 `agent` 未指定 = 表上那行 builtin

本期只有一行 builtin（`name: "raven"`），所以不需要 `default` 标记。不采纳"强制模型显式选 agent"：会让现存 prompt 全部失效，换不来东西。

builtin 行不得 `enabled: false`——否则 roster 里没有它而默认路径仍然走它。写入期硬拒，加载期告警并当 true。

### 5.2 名字唯一性

`name` 是模型选 agent 的唯一 key，重名的后果是静默的"后者胜"，而 builtin + preset + 手写三来源同时供给后必然发生。今天没有这个校验（provider endpoint 有 `_unique_endpoint_labels`，这张表没有）。

| 时机 | 行为 | 理由 |
|---|---|---|
| 加载期 | 告警 + 丢弃后来者 | 硬拒会让整个 `Config` 校验失败、raven 起不来，而能修它的 UI 在起不来的 config 后面 |
| 写入期 | 硬拒 | 调用方握着这个值，能对错误做出反应 |

去空白、小写后比较。

## 6. 兼容

| 存量 | 处理 |
|---|---|
| 表上无 builtin 行的 config | 加载期补一条 `{name: "raven", kind: "builtin"}`，不落盘 |
| `spawn(agent=null)` 的现有调用 | 语义不变 |

## 7. 增量（本期不做）

| 增量 | 触发条件 |
|---|---|
| builtin 行可配 `model` / `tools` / `prompt`；多条 builtin 行；`default` 标记 | 真要预置多个内置角色（coding / research 之类）时 |
| builtin 的 `reads_local_files` 从 `tools` 派生 | 同上。在没有 `tools` 字段之前，派生没有输入 |
| `openai` → `http` + `protocol` 字段 | 接第二种 HTTP 协议（A2A、ACP over HTTP）时。在那之前 `openai` 已准确说明协议，改名是纯迁移成本 |
