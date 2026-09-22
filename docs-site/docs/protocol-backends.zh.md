# 协议与后端集成

实现适配器、后端或协议客户端时从本页开始。操作者配置见[Agent 集成](agent-integrations.md)
和[渠道与消息](channels.md)；协议方法与部署限制见[Agent 协议](agent-protocols.md)。

## 选择扩展点 { #choose-the-extension-point }

| 需求 | 扩展位置 | 不要混淆为 |
| --- | --- | --- |
| 本地 Agent 已支持 ACP | ACP 名册条目/预设 | 新传输实现 |
| CLI 有特殊 transcript | CLI 配置，必要时增加 parser | ACP server |
| 远程服务接受 chat completions | OpenAI-compatible Agent 配置 | 能使用本地工具的后端 |
| 远程 Agent 发布 Agent Card | A2A client/server 集成 | `kind: a2a` 名册条目 |
| 消息平台需要 bot | Channel adapter | 子 Agent 后端 |
| 服务提供工具/资源 | MCP | Agent 间任务协议 |
| 产品需要定制轮次行为 | Plugin/participant | 共享 Agent Loop 的 fork |

现有后端能表达需求时优先复用。Backend kind 是经过校验的封闭集合，在 plugin manifest
里加一个自定义字符串不会注册新后端。

## 遵守运行时契约 { #respect-runtime-contracts }

装配根为 `raven/core/runtime.py:build_runtime`。运行时使用者依赖契约，入口和产品
适配器负责装配或翻译工作。保持 Kernel 不依赖 CLI、RPC、ACP 和产品代码。

| 契约或接口 | 源码 |
| --- | --- |
| 子 Agent 执行 | `raven/contracts/subagent_backend.py` |
| 工具执行 | `raven/contracts/tool.py` |
| 渠道与登录 | `raven/contracts/channel.py`，由 `raven/channels/contract.py` 重导出 |
| Turn 输入、事件、调度 | `raven/spine/` |
| 产品 UI 线格式 schema | `rpc-schema/openrpc.json` |
| 后端条目校验 | `raven/config/schema.py` |
| 后端构造 | `raven/agent/subagent/backends/__init__.py` |

Canonical names 见 `CONTEXT-MAP.md` 和 `CONTEXT.md`。后端 session、宿主 conversation、
A2A task 与 DAG node id 生命周期不同，应显式映射，避免结果或凭据跨对话。

## 实现子 Agent 后端 { #implement-a-sub-agent-backend }

`SubagentBackend.run` 接收 task 文本及 keyword-only 上下文，包括 `task_id`、
`workspace`、`executor`、`session_key`、`instance`、`provider`、`model`、`mcps`、
`mcp_grant`、`mode`、`authored_task`、`on_delta`。使用真实 Protocol 签名，不要复制子集。

契约要求返回最终文本，失败时抛异常。Manager 拥有通知、共享信号量和速率限制；后端
不要另建 dispatch 配额，也不要在没有最终答案时返回看似成功的句子。

实现并测试：

- 区分启动/readiness 和逐任务超时。
- 支持时绑定和续接原生 session id。
- 本地文件能力应符合事实，不能从 Agent 名推断。
- 显式 MCP 选择和 credential scope，不隐式共享秘密。
- 只有传输实际提供 delta 时才设置 `streams`/`on_delta`。
- 取消、进程清理、输出截断和非零退出。

新增 backend kind 需要同步修改 schema、构造、能力、配置写入和回归测试；目前不是
通用第三方 plugin 注册入口。仅 parser 的修改通常可留在
`raven/agent/subagent/backends/transcript.py` 并补 fixtures。

## 安全集成 ACP { #integrate-acp-safely }

ACP client 使用可选功能前先协商。Prompt 等待期间并发处理请求和通知；停止读取的
客户端无法回答权限或 elicitation 请求。Stdout 只承载协议，持续读取子进程 stderr。

测试 initialize、会话创建、prompt update、最终 stop reason、load/resume、取消、
未知能力和异常帧。不要声明只会返回“未实现”的方法。模型/mode 切换应作用于对应会话
的下一轮，而不是共享连接上的全部会话。

连接池下每会话 MCP 尤其敏感。能力声明本身不证明会话间隔离，因此 Raven 另有
`sessionMcp` 策略。应验证并发会话使用不同 server selection，也要测试权限请求
无人回答和客户端断开。

打包 schema fixtures 和 `tests/test_acp_schema.py` 检查帧形状。当前出站客户端
自动批准对方提供的权限选项，集成应说明这一信任模型，不能承诺人工逐操作审批。

## 安全集成 A2A { #integrate-a2a-safely }

使用协议 SDK 的请求/响应形状。当前绑定是 A2A 1.0 JSON-RPC，不是早期斜线式方法名。
流式事件使用 `StreamResponse`，非流式 send 使用 `SendMessageResponse`；
除了 message/artifact 文本，还要读取 task status 文本。

分别测试公开 card、已认证 RPC 和可选 extended card。凭据匹配预期 origin；验证
跨 origin 接口和重定向时，不应向其他目标发送凭据。不要向 peer 暴露内部 traceback。

测试真实本地 client/server 往返并 stub turn，不要两边只各用手写 JSON fixture，
否则可能漏掉 envelope 分歧。任务状态当前在内存中，不要宣称持久化恢复或 push
notifications。独立和网关托管的问题路由不同，详见协议指南。

## 增加消息适配器 { #add-a-messaging-adapter }

适配器位于 `raven/channels/adapters/<name>/`，`spec.py` 导出包含 display name、
lazy factory、capabilities 和 config schema 的 `ChannelSpec`。Discovery 扫描这些
package，不要仅为列出渠道就导入可选平台 SDK。

宿主拥有 channel socket 字段：`enabled`、`allow_from`、`workspace`。Spec 声明
适配器专用字段以及 secret/required 标记。`dispense_channel_config` 向 factory
提供经过 admission 的视图；CLI/UI 配置复用同一声明，不要再维护一套字段列表。

实现 `start`、`stop`、`send`，入站消息经过 `Intake`。下载不可信附件或发送 reaction
前检查 sender/group 策略。保留路由元数据和会话身份，由 gateway 接通 submit 与投递。

只声明实际实现的能力。交互登录要求 `SupportsLogin`，streaming 要求对应 protocol，
capability proof 会检查这些声明。附件能力需要真实 send 和错误处理，不只是一个 flag。
当前网关 channel outlet 仍不提供流式输出。

不连接真实账户，先测试发送者允许/拒绝、群策略、媒体、缺少凭据、重连/关闭及投递失败；
之后单独进行得到明确授权的真实账户验收。

## 验证与交付 { #verification-and-handoff }

代表性仓库检查：

```bash
uv run pytest tests/test_subagent_third_party.py tests/test_acp_schema.py tests/test_a2a_interop.py -q
uv run pytest tests/test_channels_contract.py tests/test_channels_registry.py tests/test_channels_intake.py tests/test_channels_outlet.py -q
```

还应运行对应 adapter/backend 测试；代码修改需运行仓库 lint 和 import contracts。
准确说明验证过的供应商版本、操作和托管入口。Mock 测试不等于真实认证测试。Fixture
不能包含凭据、二维码数据或原始私有 transcript。
