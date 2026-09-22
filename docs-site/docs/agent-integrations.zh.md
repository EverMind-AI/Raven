# Agent 集成

本页说明如何接入和使用已有 Agent。如果要构建新的 Raven Agent，请读[构建 Agent](building-agent.md)；
如果要实现 ACP、CLI、HTTP 或其他后端，请读[协议与后端集成](protocol-backends.md)。协议消息和
生命周期请读[Agent 协议](agent-protocols.md)。

## Agent 来源与运行方式 { #agent-origin-and-runtime }

“Raven Agent”和“外部 Agent”描述 Agent 的提供方；`kind` 描述 Raven 使用的运行后端。
两者是独立维度：随 Raven 发布的 Agent 也可能通过 ACP 运行，外部 Agent 也可能通过 ACP、CLI
或 HTTP 接入。

| 集成类型 | 运行配置 | 执行边界 |
| --- | --- | --- |
| 进程内后端 | `kind: builtin` | 宿主进程中的 Raven loop |
| 随 Raven 发布的 Agent | 发现到的目录、`kind: acp` | 启动器用 Agent 自身配置启动 Raven |
| 本地外部 Agent | `subagents.agents` 中的 `kind: acp` 或 `kind: cli` | 另一产品的进程和原生策略 |
| OpenAI-compatible HTTP Agent | `subagents.agents` 中的 `kind: openai` | 远程端点；此后端没有本地工具循环 |
| A2A peer | `a2a.peers` 与 `a2a_send` | 独立宿主，不属于子 Agent roster |

`spawn` 与 DAG 节点使用同一 roster。节点必须使用当前工具实际公布的名称或 worker
label；熟悉的产品名本身不会安装、启用或认证 Agent。

## Raven 原生 Agent { #raven-native-agents }

| Agent | 适合任务 | 关键边界 |
| --- | --- | --- |
| Raven-Code | 编码、调试与验证 | 会真实修改文件；并行写入应划分文件，todo 完成只是模型声明，不是测试证据。 |
| Raven-Design | 视觉设计、评审和演示文稿 | 需要 design/media 就绪；deck 请求会路由到隐藏的 Raven-PPT。 |
| Raven-Oncall | 执行并观察多轮或远程任务 | 需要目标机器、预算和停止条件；首次响应不代表任务完成。 |
| Raven-PPT | 制作 `.pptx` deck | 通常通过 Raven-Design 进入；需要 deck engine、模板、渲染和媒体条件。 |
| Raven-Research | 实时网页研究和有来源报告 | 需要 research profile 与凭据；不是通用编码或聊天委派 Agent。 |

五个定义不一定对应五个可见名册条目。随 Raven 发布的 Raven-PPT manifest 隐藏在 Raven-Design
的路由后；操作者本地 manifest 可能不同。路由还考虑声明的需求、附件和 tier，因此任务
不总是在与可见条目同名的实现上运行。

Agent 从目录发现，包括用户拥有的 `$RAVEN_HOME/agents/`。就绪状态取决于实际启动
解释器中的 runtime 和声明的 engine wheel。修改定义后重启常驻宿主，确保重建 live
roster。不要假定修改显示名就创建了新身份。可通过 `raven onboard` 或 WebUI 的 Agent 设置
完成配置。

从 TUI、WebUI 或已配置的消息渠道发起任务时，在请求中写明目标 roster 条目，并说明工作目录、
允许的副作用和预期证据。使用 DAG 或 Playbook 时，为每个节点设置当前工具实际公布的
Agent 名称或 worker label。当前 roster 中不存在的显示名不会选择 Agent。

### 调用五个原生 Agent { #calling-the-five-native-agents }

| 调用入口 | 支持情况 | 说明 |
| --- | --- | --- |
| 宿主通过 `spawn` 委派 | 五个 Agent 均支持，前提是已启用且 ready | 从 TUI、WebUI 或已配置渠道请求；使用实际公布的 roster 名称。Raven-PPT 通常通过 Raven-Design 路由进入。 |
| 有状态 instance 对话 | 支持并公布 stateful session 的 ACP Agent | 通过 WebUI instance 面板或对应 RPC 继续对话；依赖 live capability snapshot，不要仅凭配置推断 resume 支持。 |
| `run_subagent_dag` 节点 | 当前 roster 公布的 Agent | 在 `subagent` 中填写 roster 名称；生成的 Worker Table label 只在当前轮次有效，不能用于存储的 Playbook。 |
| Playbook | 已注册的 roster Agent | Playbook 最终使用同一个 DAG registry；常规 Raven-PPT 流程应使用 Raven-Design。 |
| 直接通过 stdio 调用 ACP | 五个 Agent 均支持 | 启动产品 launcher，发送按行分隔的 JSON-RPC。Raven-Research 默认提供 ACP，不使用 `--acp` 参数。 |
| 单次 CLI hosting | 仅 Raven-Code | 使用 launcher 的 `--task` 或 `--prompt-file`；`--session` 可继续 CLI 对话。 |
| 消息渠道 | 五个 Agent 均可被宿主间接委派 | 消息先进入 Raven 宿主，再使用相同 roster 和权限进行委派。 |
| 直接通过 A2A 调用单个原生 Agent | 不支持 | A2A 暴露的是 Raven 宿主，不是五个独立的原生 Agent endpoint。 |

前四种是宿主层的委派方式；只有直接 ACP 和 Raven-Code CLI hosting 会显式启动原生产品
launcher。readiness probe 成功或收到委派回执，都不代表任务已完成；仍需检查结果、文件、
测试或报告产物。

### 最小 ACP 调用 { #minimal-acp-invocation }

随 Raven 发布的每个 Agent 都由自己的 launcher 启动。使用对应命令：

| Agent | Launcher 命令 | 说明 |
| --- | --- | --- |
| Raven-Code | `python agents/raven-code/run.py --acp` | 也支持单次 CLI hosting。 |
| Raven-Design | `python agents/raven-design/run.py --acp` | 需要 design engine。 |
| Raven-Oncall | `python agents/raven-oncall/run.py --acp` | 运维任务需要已注册的目标机器访问。 |
| Raven-PPT | `python agents/raven-ppt/run.py --acp` | 通常通过 Raven-Design 路由；需要 PPT engine。 |
| Raven-Research | `python agents/raven-research/run.py` | 默认就是 ACP hosting；该 launcher 没有 `--acp` 参数。 |

使用已安装 Raven 和 Agent 所需 engine wheel 的解释器。常规制作 deck 时，应通过
Raven-Design 请求，而不是直接选择隐藏的 Raven-PPT 路由。

以下以 Raven-Code 为例：

```bash
python /absolute/path/to/Raven/agents/raven-code/run.py --acp
```

Launcher 会渲染该 Agent 的配置，并通过 stdin/stdout 启动 `raven acp`。每行发送一个
JSON-RPC 对象；日志和 wrapper 输出不能写入 stdout。进程启动后，最小可用的请求序列是：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}
{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/absolute/path/to/project","mcpServers":[]}}
{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"<session-id-from-session-new>","prompt":[{"type":"text","text":"Reply with one sentence; do not edit files."}]}}
```

| 字段或参数 | 含义 |
| --- | --- |
| `--acp` | 通过 stdio 提供 ACP 服务，不是 HTTP 监听器。 |
| `python` | 使用已安装 Raven 和 Agent 所需 engine wheel 的解释器。 |
| `protocolVersion` | 握手时请求的 ACP 版本；当前 Raven 使用 `1`。 |
| `clientCapabilities` | 调用方能够处理的能力；最小声明可以是 `{}`。 |
| `cwd` | 当前 session 的绝对任务工作目录，不是 Agent 的 ACP home。 |
| `mcpServers` | 当前 session 的 stdio MCP 附件；不需要时传 `[]`。 |
| `sessionId` | `session/new` 返回的标识，后续 prompt 继续复用。 |
| `prompt` | 内容 block 数组；最小 block 包含 `type: "text"` 和 `text`。 |

服务端可能在这些请求之间发送 `session/update` 通知。客户端应持续读取，并在等待最终
prompt 响应前处理权限或问题请求。完整生命周期和协商能力见[Agent 协议](agent-protocols.md#serve-raven-over-acp)。

### 任务提示模板 { #task-prompt-templates }

使用实际公布的 roster 名称，并写明任务范围、允许的副作用和预期证据。以下一句话模板
只是起点，不代表任务已经可以运行或已经完成：

| Agent | 示例请求 |
| --- | --- |
| Raven-Code | `Raven-Code：检查 src/parser.py 中的 parser；只编辑 parser 文件；运行对应测试；报告命令输出。不提交、不推送。` |
| Raven-Design | `Raven-Design：评审这个 dashboard 的层级和可访问性；提出具体视觉修改，并将反馈保存到项目工作区。` |
| Raven-Oncall | `Raven-Oncall：在已注册的 lab machine 上运行参数 sweep；预算两小时；停止失败 worker；比较每轮结果并报告最终产物。` |
| Raven-PPT | `Raven-PPT：根据这些笔记制作产品评审 .pptx；常规流程优先使用 Raven-Design；逐页渲染并返回未解决问题。` |
| Raven-Research | `Raven-Research：使用过去一年发布的来源比较这些方案；为重要结论附 URL，并将报告写入指定工作区文件。` |

对每个 Agent，都应在真实任务前检查 readiness，并在任务后检查最终回答、生成文件和执行证据。
ACP readiness 或 roster 中列出条目，只能证明进程可以启动，不能证明任务已成功完成。

### Raven-Code：工作区与验证边界 { #raven-code-workspace-and-verification }

Raven-Code 的 `code-flow` 提供的不只是身份 prompt。每个任务都应绑定项目目录，明确文件所有权、
允许的副作用和验证命令；完成后检查实际文件、命令输出和 Harness Manifest。

| 范围 | 边界 |
| --- | --- |
| 项目指令 | `AGENTS.md`、`CLAUDE.md` 和 `CONTEXT.md` 从工作目录读取，受配置选择、路径范围和 prompt 预算约束；它们不是 Agent home 的 bootstrap 文件。 |
| 文件与工作区状态 | 文件工具跟踪 session 读取过的版本；启用 read-before-edit 时，未读或被外部改变的版本会被拒绝。共享 checkout 会看到相同 HEAD 和修改；并发提醒与 read ledger 不是文件锁或 worktree 隔离，完整写入也不普遍受保护。 |
| 任务状态 | `todo` 按 session 持久化；完成状态是模型声明，不是测试证据。已有 session 会恢复清单，新任务应使用真正的新 session；宿主原地 `/new` 保留 session key，可能不会重置这份产品清单。 |
| Git 证据 | Harness Manifest 通过 ACP response metadata 报告修改、提交、阻碍和共享工作目录归属；不要根据模型说“就绪”推断 Git 事实。 |
| 产品配置 | Raven-Code launcher 会启用产品配置。仅在别处安装 `code-flow` 不会启用所有能力；提醒/报告与替换工具面有独立开关。 |

请求示例：“阅读项目指令，只在分配给你的文件内修复 parser bug，运行对应测试，
报告剩余修改或阻碍。不提交、不推送。”

追问协作见[与子 Agent 协作](agent-collaboration.md)，后台长任务见[长任务托管（Oncall）](oncall.md)。
启动器和 harness 细节见[实现案例](building-agent.md#case-study-raven-code)。

## 接入外部 Agent { #connect-external-agents }

以下是当前源码声明的预设，表示支持的配置路径，**不代表**每个供应商版本或你的本地
安装都已通过真实任务验证。

| 预设 key | 传输与启动方式 | 使用前检查 |
| --- | --- | --- |
| `claude_code` | 通过 `npx` 启动 ACP 适配器 | 适配器下载/runtime 和子 Agent 认证 |
| `codex` | 通过 `npx` 启动 ACP 适配器 | 预设设定 `INITIAL_AGENT_MODE=agent-full-access`，需评估宿主和网络访问 |
| `opencode` | `opencode acp` | 当前预设以 `sessionMcp: false` 阻止每会话 MCP 下发 |
| `hermes` | `hermes acp --accept-hooks` | 原生认证；该标志接受之前未见的 Shell hook |
| `openclaw` | `openclaw acp` | 自身网关、认证；启动使用较长握手等待 |
| `mirothinker` | OpenAI-compatible HTTP | 端点、模型、key；预设关闭有状态重放 |

GitHub Copilot、Qwen Code、CodeBuddy、Qoder、Grok Build、Kimi Code 和 Pi 也有预设。
适配器版本与精确命令维护在 `raven/agent/subagent/presets.py` 和
`raven/agent/subagent/acp_registry_presets.py`，优先使用已安装版本的预设，不要自行猜 flags。

判断兼容性时区分三个结论：

- **已声明预设：** Raven 有配置模板。
- **握手/探测成功：** 当前安装能启动并报告能力。
- **任务已验证：** 真实任务验证了认证、工具和输出。

手写集成需要对所有适用层级分别检查。

## 接入与验证 { #connect-and-verify }

1. 按外部 Agent 对应版本安装并认证，确保其可执行文件在常驻 Raven 进程的 `PATH` 中。
2. 通过 WebUI Agent 设置配置，或向宿主配置的 `subagents.agents` 添加配置条目；保留其他条目和设置。
3. 检查配置和 readiness 结果。ACP probe 可能启动进程，shim 可能下载代码；完整任务测试可能消耗模型额度。
4. 先运行“只用一句话回答，不编辑文件”等低风险任务，再分别验证文件、工具或 resume 能力。
5. 检查权限与隔离后，才扩大委派范围。

使用已安装 Hermes 可执行文件的最小 ACP 示例：

```json
{
  "subagents": {
    "agents": [
      {
        "name": "Hermes Agent",
        "preset": "hermes",
        "kind": "acp",
        "enabled": true,
        "command": "hermes acp --accept-hooks",
        "cwd": "/absolute/path/to/project",
        "readyTimeoutMs": 30000,
        "timeout": 600
      }
    ]
  }
}
```

这是合并片段，不是完整配置替换；hook 标志具有上文说明的安全影响。ACP `command`
启动 server，不应包含 `{prompt}`、`{prompt_file}` 或 `{agent_id}`。
`readyTimeoutMs` 限制握手，`timeout` 限制任务；后者省略时默认没有自动期限。

CLI 后端的 `command` 是 argv 模板。`{prompt}` 作为一个参数替换，`{prompt_file}`
指向 prompt 文件，无占位符时走 stdin。`resumeCommand` 必须包含 `{agent_id}`；
id 分配方式和对话记录解析必须符合该 CLI。不要把这些字段复制到 ACP 配置条目。

HTTP 后端配置 `baseUrl`、`model` 和凭据，而不是命令。宿主重放可提供历史，但不等于
远程原生会话。此后端不能读取本地路径或接收 MCP 下发。OpenAI-compatible Agent
端点与修改 Raven 主模型 provider 是两件事。

## 文件、状态与凭据 { #files-state-and-credentials }

| 位置或 handle | 职责 |
| --- | --- |
| 用户工作目录 | 任务获准读取或修改的文件 |
| 宿主 Agent home | 宿主身份、会话、技能和记忆 |
| Agent state root | Launcher 渲染配置与 Agent 工作状态 |
| Agent ACP home | 子引擎身份、transcript 和 skill pool |
| `instance` | 在后端支持时续接子 Agent 会话 |
| Node id | 寻址宿主对话中记录的任务和输出 |

CLI 的 `readsLocalFiles` 是对其执行环境的声明。ACP 能力通过协商并由适配器解释。
HTTP 后端没有本地文件访问，应传有界内容而不是宿主路径。只有 Agent 真正支持相应
记忆路径时才配置 memory identity；添加元数据不会让外部产品自动共享 Raven 记忆。

凭据不要放进 prompt 和共享 manifest。A2A 凭据按 origin 而不是显示名匹配；peer
列表不是出站防火墙，详见[A2A 指南](agent-protocols.md#call-a-remote-a2a-peer)。

## 安全与排障 { #security-and-troubleshooting }

出站 ACP 自动优先选择对方提供的 allow 选项。父级 permission mode 不为每个外部
Agent 内部操作提供人工审批。无人值守委派前，应检查原生权限、账户权限、可写挂载、
hook 和网络访问。

| 现象 | 检查 |
| --- | --- |
| Agent 不在可选 roster | Discovery、enabled/hidden、路由、readiness |
| Engine 不可用 | Wheel 是否在子进程解释器里，而不只是另一虚拟环境 |
| 握手通过但任务失败 | 模型凭据、工具前置条件、子进程 stderr |
| CLI 输出缺失或乱码 | 退出状态、transcript 格式、输出解析 |
| Instance 无法续接 | Resume 契约与协商出的会话能力 |
| MCP 选择未下发 | 后端支持和 `sessionMcp` 隔离策略 |
| 远程 Agent 无法打开文件 | 是否把宿主路径误当作内容传输 |

图失败见[DAG 编排](orchestration.md)，权限与隔离见[权限与安全](permissions.md)。
