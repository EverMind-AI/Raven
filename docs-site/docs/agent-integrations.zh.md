# Agent 集成

本页说明如何接入现有 Agent。创建新的 Raven Agent 请读[构建 Agent](building-agent.md)；
协议消息和生命周期请读[Agent 协议](agent-protocols.md)。

## 区分来源与传输 { #distinguish-origin-from-transport }

“Raven Agent”和“外部 Agent”描述谁提供 Agent；`kind` 描述 Raven 如何运行它。
这是两个维度：Raven 自身的专用 Agent 同样使用 ACP。

| 集成 | 配置 | 执行边界 |
| --- | --- | --- |
| 进程内后端 | `kind: builtin` | 宿主进程中的 Raven loop |
| 随 Raven 发布的专用 Agent | 发现到的目录、`kind: acp` | 启动器用 Agent 自身配置启动 Raven |
| 本地外部 Agent | `subagents.agents` 中的 `kind: acp` 或 `kind: cli` | 另一产品的进程和原生策略 |
| OpenAI-compatible HTTP Agent | `subagents.agents` 中的 `kind: openai` | 远程端点；此后端没有本地工具循环 |
| A2A peer | `a2a.peers` 与 `a2a_send` | 独立宿主，不属于子 Agent roster |

`spawn` 与 DAG 节点使用同一 roster。节点必须使用当前工具实际公布的名称或 worker
label；熟悉的产品名本身不会安装、启用或认证 Agent。

## 随 Raven 发布的 Agent { #shipped-raven-agents }

| Agent | 用途 | 配置关注点 |
| --- | --- | --- |
| Raven-Code | 编码、调试与验证 | 真实文件修改；并行写入应划分文件 |
| Raven-Design | 视觉设计与演示文稿 | Design engine 和媒体服务就绪 |
| Raven-Oncall | 执行并持续观察运维工作 | 目标机器访问与长任务 |
| Raven-PPT | PowerPoint 生产 | Deck engine、模板和渲染依赖 |
| Raven-Research | 研究与证据收集 | Research profile、工具和模型凭据 |

五个定义不一定对应五个可见名册条目。随 Raven 发布的 Raven-PPT manifest 隐藏在 Raven-Design
的路由后；操作者本地 manifest 可能不同。路由还考虑声明的需求、附件和 tier，因此任务
不总是在与可见条目同名的实现上运行。

Agent 从目录发现，包括用户拥有的 `$RAVEN_HOME/agents/`。就绪状态取决于实际启动
解释器中的 runtime 和声明的 engine wheel。修改定义后重启常驻宿主，确保重建 live
roster。不要假定修改显示名就创建了新身份。

## 安全使用 Raven-Code { #working-safely-with-raven-code }

Raven-Code 通过 `code-flow` 提供产品行为，不只是更换身份 prompt。
应指定项目目录和验证任务，再检查证据：

1. `AGENTS.md`、`CLAUDE.md`、`CONTEXT.md` 等项目指令从工作目录读取，
   受配置选择、路径范围和 prompt 预算约束，不是 Agent home 中的 bootstrap 文件。
2. 文件工具跟踪本 session 读取过的版本。启用 read-before-edit 时，
   未读或被外部改变的版本会被拒绝，应重读并协调，而不是盲目重试。
3. `todo` 清单按 session 持久化。完成状态只是模型声明，不是测试结果，
   应检查实际命令和输出。
4. Harness Manifest 通过 ACP response metadata 报告 Git 事实，
   包括修改、提交、阻碍和共享工作目录归属，不根据模型说“就绪”来判断。

请求示例：“阅读项目指令，只在分配给你的文件内修复 parser bug，运行对应测试，
报告剩余修改或阻碍。不提交、不推送。”

两个 session 共享目录时，会看到相同 HEAD 和修改。并发提醒和 read ledger
不是文件锁，也不会自动隔离 worktree。共享 manifest 不能归因为单一 session 的成果，
完整写入也不普遍受 read-before-edit 保护。

继续已有 session 会恢复清单。新任务应使用真正的新 session；
宿主原地 `/new` 保留 session key，不保证重置这份独立的产品清单。

Raven-Code launcher 会启用其产品配置。仅在别处安装 `code-flow` 不代表
启用所有能力：提醒/报告与替换工具面有独立开关。
详见[实现案例](building-agent.md#case-study-raven-code)。
追问协作见[与子 Agent 协作](agent-collaboration.md)，后台长任务见[长任务托管（Oncall）](oncall.md)。

## 外部预设 { #external-presets }

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
