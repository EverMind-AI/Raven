# 使用 Raven

先选择与 Raven 交互的方式，为会话指定正确工作目录，再按需增加 Agent 或服务。
下文使用已安装的命令；源码检出时可在前面加 `uv run`。

## 选择入口 { #choose-a-surface }

| 目标 | 入口 | 下一步指南 |
| --- | --- | --- |
| 终端会话 | `raven tui` | [命令参考](commands.md) |
| 浏览器会话 | `raven web` | [启动 WebUI](webui.md) |
| 在仓库执行单次任务 | `raven agent -m "Summarize this project without editing files"` | [权限与安全](permissions.md) |
| 从消息平台聊天 | 已配置渠道和常驻网关 | [渠道与消息](channels.md) |
| 委派给专用 Agent | Roster 中已配置的 Agent | [Agent 集成](agent-integrations.md) |
| 与专用 Agent 继续协作 | 它的有状态实例 | [与子 Agent 协作](agent-collaboration.md) |
| 操作共享浏览器 | 浏览器工具和 WebUI 面板 | [浏览器协作](browser-collaboration.md) |
| 检索文档段落 | WebUI 知识库页面与会话中的知识库选择；集成使用 Python 库或 RPC | [知识库](knowledge.md) |
| 执行并观察长任务 | 声明 campaign 后的 Raven-Oncall | [长任务托管（Oncall）](oncall.md) |
| 集成编辑器或远程 peer | ACP 或 A2A | [Agent 协议](agent-protocols.md) |

`raven agent` 必须提供消息，不是交互式 REPL；连续会话使用 TUI 或 WebUI。
尚未配置模型服务商时，先读[快速开始](quick-start.md)。

## 选择正确工作目录 { #work-in-the-right-directory }

工作目录保存任务操作的文件；Agent home 保存身份、记忆、技能和 transcript，
不能把它当作项目仓库的同义词。单次任务示例：

```bash
raven agent --workspace /absolute/path/to/project -m "Read the README and summarize the setup steps"
```

这会执行模型轮次，可能产生服务商费用。修改任务应明确可编辑文件和验证方式。
同一 Git 项目中有多个编码任务并行时，宿主的 `multi-agent-coding` skill 会要求各编码
Agent 创建独立 worktree，并汇报路径和分支。协调并发修改前，应检查这些汇报。
该流程见[与子 Agent 协作](agent-collaboration.md#models-modes-and-shared-files)；
手动构建任务图时参阅[DAG 编排](orchestration.md)。

## Raven 的状态存放位置 { #where-raven-keeps-state }

Raven 的运行时状态——配置、命令历史、日志——都放在一个目录下：设置了 `$RAVEN_HOME`
就用它；未设置或为空则用 `~/.raven`。取值可以是绝对路径、裸 `~`，或以 `~/` 开头；
裸 `~` 和 `~/` 前缀都会在各前端展开为用户主目录。`~\` 是同一前缀的 Windows 写法：
Windows 上各前端都会展开，其他平台上只有 TUI 会展开。其他以 `~` 开头的形式（如
`~name`）在 TUI 中不展开，按相对路径处理。相对路径会相对每个进程各自的工作目录解析，
因此不同前端可能指向不同目录；建议使用绝对路径。

## 继续会话 { #continue-a-session }

每次单次 CLI 调用默认创建新会话。确实需要延续上下文时，选择 `--continue`、
`--resume <id-or-prefix>` 或 `--session <full-key>`；三者互斥。

```bash
raven sessions --help
raven agent --continue -m "Expand the previous summary; do not modify files"
```

交互式会话通过 UI 的会话选择器切换。子 Agent 的 `instance` 续接子 Agent 会话，
不是宿主 session id 或 DAG node id，详见[DAG 编排](orchestration.md)。

## 设置思考深度 { #set-the-reasoning-effort }

思考深度是模型每次回答前思考多少。可选的词由当前模型声明：多数模型是 `minimal`、
`low`、`medium`、`high`，厂商提供时还会有 `xhigh`、`max`；有些接口的档位更窄，只收其中
几个（DeepSeek 只收 `low`、`high`、`max`，把 `minimal` 归入 `low`、`medium` 归入
`high`）。档位跟随实际承接请求的服务商，而不只看模型名：同一个模型经网关访问时，用网关
的档位。没有声明任何档位的模型不会出现这一行。各厂商把这些词映射成自己的请求参数
（Anthropic 的自适应思考强度与思考预算，OpenAI 风格接口的 `reasoning_effort` 字段），
没有思考能力的模型会忽略它。不设置时使用厂商自己的默认值。

两处可以设置：

- **对所有会话**：在 WebUI 打开设置页，在「主会话」一行点「参数」，再选「思考深度」；
  或在 `~/.raven/config.json` 里设置 `agents.defaults.reasoningEffort`。第一档
  「厂商默认」会清掉这个键。
- **只对当前会话**：WebUI 模型选择器（点输入框旁的模型标签打开）里的「思考深度」行，
  或 TUI 里的 `/effort <词>`。单独输入 `/effort` 查看当前生效的档位，`/effort default`
  让这个会话重新跟随全局设置。在与某个子智能体实例直聊时这条命令会被拒绝：这个选择
  属于主会话，实例自己没有思考深度可设。

也可以在厂商的 `model_overlay.reasoningEfforts` 里声明某个模型的可用档位
（设置页该厂商的「高级」→「显示名」下的「添加」）。非空声明优先于 LiteLLM 的表，
包括表里标为不支持 reasoning 的路由；留空则回退到表。

单个会话的选择优先于全局设置和该会话的模式。它记录在会话自己的档案里，网关重启后仍在，
恢复会话时一并带回，从下一轮开始生效。当前模型不声明这个档位时，按该模型声明的、低于它的
最高档运行（在一个模型上选了 `max`，切到只到 `high` 的模型，就按 `high` 运行）；只有当它
连更低的档都不声明时，才改为运行该会话模式的档位或全局设置。这个选择会按原词保留，切回
支持它的模型时继续生效。它只影响 Raven 自己的模型调用；子智能体仍按全局默认值运行。

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
