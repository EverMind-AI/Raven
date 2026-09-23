# 使用 Raven

先选择与 Raven 交互的方式，为对话指定正确工作目录，再按需增加 Agent 或服务。
下文使用已安装的命令；源码检出时可在前面加 `uv run`。

## 选择入口 { #choose-a-surface }

| 目标 | 入口 | 下一步指南 |
| --- | --- | --- |
| 终端对话 | `raven tui` | [命令参考](commands.md) |
| 浏览器对话 | `raven web` | [启动 WebUI](webui.md) |
| 在仓库执行单次任务 | `raven agent -m "Summarize this project without editing files"` | [权限与安全](permissions.md) |
| 从消息平台聊天 | 已配置渠道和常驻网关 | [渠道与消息](channels.md) |
| 委派给专用 Agent | Roster 中已配置的 Agent | [Agent 集成](agent-integrations.md) |
| 与专用 Agent 继续协作 | 它的有状态实例 | [与子 Agent 协作](agent-collaboration.md) |
| 操作共享浏览器 | 浏览器工具和 WebUI 面板 | [浏览器协作](browser-collaboration.md) |
| 检索文档段落 | 知识库 Python 库或 RPC 客户端 | [知识库](knowledge.md) |
| 执行并观察长任务 | 声明 campaign 后的 Raven-Oncall | [长任务托管（Oncall）](oncall.md) |
| 集成编辑器或远程 peer | ACP 或 A2A | [Agent 协议](agent-protocols.md) |

`raven agent` 必须提供消息，不是交互式 REPL；连续对话使用 TUI 或 WebUI。
尚未配置模型服务商时，先读[快速开始](quick-start.md)。

## 选择正确工作目录 { #work-in-the-right-directory }

工作目录保存任务操作的文件；Agent home 保存身份、记忆、技能和 transcript，
不能把它当作项目仓库的同义词。单次任务示例：

```bash
raven agent --workspace /absolute/path/to/project -m "Read the README and summarize the setup steps"
```

这会执行模型轮次，可能产生服务商费用。修改任务应明确可编辑文件和验证方式。
并行编码 Agent 不会自动获得独立 worktree，应划分文件所有权或使用不同工作副本。

## 继续对话 { #continue-a-conversation }

每次单次 CLI 调用默认创建新会话。确实需要延续上下文时，选择 `--continue`、
`--resume <id-or-prefix>` 或 `--session <full-key>`；三者互斥。

```bash
raven sessions --help
raven agent --continue -m "Expand the previous summary; do not modify files"
```

交互式会话通过 UI 的对话选择器切换。子 Agent 的 `instance` 续接子 Agent 对话，
不是宿主 session id 或 DAG node id，详见[DAG 编排](orchestration.md)。

## 有目的地增加能力 { #add-capability-deliberately }

可复用指令用 skill，外部工具用 MCP，运行时贡献用 plugin。需要独立身份和执行配置
时使用专用 Agent，需要协调有依赖的工作时使用 DAG 或 playbook。A2A 面向独立运营
的 peer，不是本地 plugin 的另一种名称。

[技能、记忆与扩展](skills-and-extensions.md)解释这些选择。定时或主动任务请读
[主动提醒与跟进](proactivity.md)，并保持相应常驻宿主运行。

## 检查结果与排障 { #check-results-and-diagnose-problems }

先使用 `raven status` 和 `raven doctor`。模型已配置、Agent 已列出或握手成功，
都不代表真实任务成功。应检查最终回答、生成文件、相关测试或任务证据。

任务停滞时，先检查审批请求、子 Agent 就绪状态、模型错误和运行进度，再决定重试；
重试可能重复外部副作用。使用 `raven tracing`、`raven trajectory --help` 查诊断，
分享前先脱敏私人材料。[轨迹调试与回放](trajectory-debugging.md)说明如何保存证据，
以及如何在不重复录制的外部动作的情况下重现 harness 行为。
