# Agent 协议：ACP 与 A2A

配置 Raven 专用 Agent、Claude Code 或其他已有产品，请先读[Agent 集成](agent-integrations.md)；
实现传输或后端，请读[协议与后端集成](protocol-backends.md)。

Raven 既能为其他宿主提供服务，也能调用其他 Agent。选择接口时，应根据谁管理进程、
什么内容需要越过边界来判断，而不只是看名称里有没有“Agent”。本页描述仓库中的实际实现。

## 选择接口 { #choose-an-interface }

| 需求 | 接口 | 边界 |
| --- | --- | --- |
| 编辑器或本地宿主启动 Raven | Agent Client Protocol（ACP）服务端 | 子进程、stdio JSON-RPC、会话 |
| Raven 启动本地 Agent | ACP 客户端或 CLI 后端 | 配置中的子 Agent 进程 |
| 独立运行的 Agent 调用 Raven | Agent2Agent（A2A）服务端 | HTTP JSON-RPC、任务、可选 SSE |
| Raven 向远程 peer 委托工作 | `a2a_send` | Agent Card 发现与 HTTP 请求 |
| 浏览器或终端 UI 控制 Raven | Raven RPC | `rpc-schema/openrpc.json` 中的产品契约 |
| Agent 需要外部工具或资源 | MCP | 工具与资源访问，不是 Agent 任务协议 |

ACP 不是 HTTP 端点，A2A 也不替代本地子 Agent roster（名册）。
[DAG 编排](orchestration.md)在请求到达后协调任务，与传递请求的协议是两个层次。

## 通过 ACP 提供 Raven 服务 { #serve-raven-over-acp }

先按[快速开始](quick-start.md)配置模型服务商。ACP 宿主随后启动已安装的
`raven acp`。源码检出时可使用：

```bash
uv run raven acp
uv run raven acp --config /absolute/path/to/config.json
```

这是两种可选启动方式，不是连续操作步骤。命令从 stdin 等待按行分隔的 JSON-RPC；
stdout 只用于协议帧。日志写入 `<config dir>/logs/acp.log`，每 10 MB 轮转，保留三份。
警告和错误也会写入 stderr。启动包装脚本不要向 stdout 打印横幅。

### 会话生命周期与能力 { #session-lifecycle-and-capabilities }

1. 发送 `initialize`，包含 `protocolVersion: 1` 和客户端能力。
2. 发送 `session/new`，包含绝对路径 `cwd` 和 `mcpServers`（没有时传 `[]`）。后续请求使用返回的 `sessionId`。
3. 发送 `session/prompt`，执行期间持续接收 `session/update`，并处理服务端发来的权限或问题请求。
4. 等待 prompt 响应，或用 `session/cancel` 停止轮次；之后再向该会话发送下一条 prompt。

以下是 initialize **之后**发送的会话创建示例：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "session/new",
  "params": {"cwd": "/absolute/path/to/project", "mcpServers": []}
}
```

| 能力 | 当前行为 |
| --- | --- |
| Prompt 内容 | 文本、图像、嵌入式文本上下文；不声明音频支持 |
| `session/load` | 返回前通过 update 重放已存历史 |
| `session/resume` | 重新打开会话，不重放历史 |
| 会话管理 | 支持列出、关闭和删除；关闭不等于删除 |
| 模型选择 | 读取 `configOptions`，通过 `session/set_config_option` 为下一轮切换模型 |
| 模式 | 读取返回的 modes，通过 `session/set_mode` 为下一轮应用已声明的执行档位 |
| 每会话 MCP | stdio 服务仅对该会话可见；不声明 HTTP/SSE 支持 |
| 轮次引导 | Raven 扩展 `_raven/session/steer`，仅在声明支持时使用；不是标准 ACP 方法 |

Load 和 resume 同样要求 `cwd`。没有声明任何模式的部署不能处理 `session/set_mode`。
不可用的 MCP 附件会被丢弃并记录警告；创建会话成功不代表每个请求的工具都已连接。
Raven 使用自身的文件与命令工具，而不是客户端未保存的编辑器缓冲区或 `terminal/*` 服务。

### 审批、问题与重放 { #approvals-questions-and-replay }

Raven 作为 ACP **服务端**时，通过 `session/request_permission` 请求工具审批。
一般问题在客户端声明支持 form 时使用 `elicitation/create`；对于不支持表单的客户端，
则退回到类似权限请求的形式。Prompt 等待期间必须继续读取请求和通知；只等最终响应
可能使交互无法继续。

Raven 作为 ACP **客户端**时，无人值守地执行委派 Agent，并自动选择对方提供的
权限选项，优先 `allow_always`，其次 `allow_once`。这是一项重要信任边界，**不等于
逐操作人工审批**。启用前应配置子 Agent 自身的策略与隔离。详见[权限与安全](permissions.md)。

历史重放最多包含最新 500 条消息，每条文本最多 16 KiB。多模态历史可能仅重放为文本，
不会重建原附件。ACP 出站 payload 会脱敏可识别凭据，但脱敏不是完整的秘密扫描器。

## 通过 A2A 提供 Raven 服务 { #serve-raven-over-a2a }

网关挂载的 A2A 入口默认关闭，须显式启用：

```bash
raven a2a enable
```

命令写入 `a2a.server.enabled`，并在缺少时生成 `a2a.server.token`。重启已有的
`raven gateway` 或 `raven web` 进程后才会挂载路由，不要在同一端口启动第二个网关。
关闭时运行 `raven a2a disable` 并重启；token 会保留。

也可以运行独立监听器：

```bash
raven a2a serve --host 127.0.0.1 --port 8710
```

独立服务命令本身就是显式启用，不要求 `enabled: true`，但仍要求配置中有非空 token。
先运行 `a2a enable` 是配置 token 的一种方法；注意这也会让网关在下次重启时启用 A2A。
两种托管方式都拒绝在子 Agent 进程内启动。

### Agent Card 与认证 { #agent-cards-and-authentication }

| 路由或方法 | 访问条件 | 返回内容 |
| --- | --- | --- |
| `GET /.well-known/agent-card.json` | 公开 | 协议版本、接口 URL、通用 skill、认证要求 |
| `POST /a2a`（默认路径） | Bearer token | A2A JSON-RPC 方法 |
| `GetExtendedAgentCard` | 已认证且服务声明支持 | 编排能力，以及描述本地 roster 的 Raven 扩展 |

公开 card 有意隐藏已安装的子 Agent。只有托管方提供 roster 时才有扩展 card；独立服务
当前不提供 roster。扩展中的名册是参考信息，不是一组可直接远程调用的子 Agent；应向
宿主说明所需结果。

JSON-RPC 要求 `A2A-Version: 1.0` 和 `Authorization: Bearer <configured token>`。
空 token 会拒绝所有 RPC 调用。能获取公开 card 不代表认证成功，也不代表模型服务可用。
默认路径可通过 `a2a.server.path` 修改。

独立监听器可以用以下只读请求检查公开 card：

```bash
curl --fail http://127.0.0.1:8710/.well-known/agent-card.json
```

将监听器限制在私有接口，或置于有认证的 TLS 终止代理之后。Bearer token 是共享的
操作者级访问，不是逐用户授权，也不是沙箱；还需要限制网络和子 Agent 的权限。

### 任务、流式传输与限制 { #tasks-streaming-and-limits }

支持的 RPC 方法为 `SendMessage`、`SendStreamingMessage`、`GetTask`、`ListTasks`、
`CancelTask`、`SubscribeToTask` 和 `GetExtendedAgentCard`。较早的斜线式方法名
（例如 `message/send`）不适用于此实现。消息要求 `messageId`、`ROLE_USER` 等 role 和 `parts`。

入站任务执行一个 Raven 轮次，从 submitted 进入 working，通常以 completed 或 failed
结束。轮次提出问题时进入 input-required；使用相同 `taskId` 的 `SendMessage` 回答仍在
等待的轮次，不会新建另一个轮次。这是进程内等待，不是持久化检查点；复杂表单问题不会
被完整重现为远程表单 UI。此 A2A 问题 broker 接入独立托管；网关挂载的 A2A 保留
网关已有的问题 broker，不应假定远程 peer 能在那里完成相同的问题往返。

流式方法返回 SSE `StreamResponse` 封装。非流式 `SendMessage` 返回包含 task 或
message 的 `SendMessageResponse`。当前 Raven executor 将最终文本放在 completed
task 的 status message 中，不承诺逐 token 输出或生成文件的 artifact 投递。客户端
连接其他 peer 时，也应理解 message 和 artifact 事件中的文本。

任务状态保存在**内存**中，重启即丢失。不支持 push notifications，默认声明的输入和
输出为 `text/plain`。虽然暴露了 `CancelTask`，canceled 状态不代表外部副作用已撤销，
也不保证每个子进程都停止。轮次失败时返回固定的安全消息，详细异常留在服务端日志。

## 调用远程 A2A peer { #call-a-remote-a2a-peer }

将凭据写入宿主配置，不要写进模型 prompt。将以下片段合并进已有配置，不要替换模型设置：

```json
{
  "a2a": {
    "peers": [
      {
        "origin": "https://agent.example.test",
        "authScheme": "bearer",
        "credential": "REPLACE_WITH_PEER_TOKEN"
      }
    ]
  }
}
```

`a2a_send` 接收 `card_url` 和 `message`，发现 card、发送文本任务并汇集响应文本。
它不暴露多轮任务管理 API，也不会自动回答远端 input-required 任务。

凭据按 card URL 的 **origin**（协议、主机、端口）匹配。未配置的 origin 不附带凭据，
但不会因此自动被网络层阻止：peer 列表是凭据映射，不是出站防火墙。只调用用户指定或
已经信任的 peer。客户端不跟随重定向，并在发送任务前拒绝跨 origin 的 JSON-RPC
接口。若确实需要另一个 origin，应使用该接口 origin 上的 card 和对应 peer 配置。

## 排障与实现索引 { #troubleshooting-and-implementation-map }

| 现象 | 优先检查 |
| --- | --- |
| ACP 返回无效 JSON | 包装脚本是否污染 stdout；诊断应写 stderr |
| ACP 会话创建失败 | 先 initialize，并提供有效的绝对路径 `cwd` |
| ACP 在工具调用后停住 | 是否持续读取并回答权限或问题请求 |
| 附加的 MCP 工具缺失 | 握手能力和 ACP 连接警告 |
| A2A card 可读但 RPC 返回 401 | 非空 bearer token；公开 card 本身无需认证 |
| A2A 报版本不匹配 | 发送 `A2A-Version: 1.0` |
| A2A 回复看起来为空 | 除 message/artifact parts 外，还要读取 status message |
| A2A 设置未生效 | 启用或关闭后重启服务进程 |

实现入口：`raven/acp/capabilities.py`、`raven/acp/methods.py`、
`raven/acp_client/permissions.py`、`raven/a2a/card.py`、`raven/a2a/routes_aiohttp.py`、
`raven/a2a/runtime.py` 和 `raven/a2a_client/client.py`。CLI help 可用于查启动方式，
但部分 ACP 帮助文字早于会话模式和每会话 MCP 功能；支持情况以协商能力和方法实现为准。
