# 命令参考

根据任务查找对应命令。各命令的帮助信息包含可用的子命令和选项。

| 命令 | 用途 |
| --- | --- |
| `raven` 或 `raven tui` | 启动终端界面 |
| `raven web` | 打开 WebUI，并让 Raven 在后台运行 |
| `raven web --stop` | 停止 WebUI 后台服务 |
| `raven agent -m "..."` | 从命令行执行单次任务 |
| `raven onboard` | 配置模型服务商、沙箱、消息渠道、记忆、网络工具凭据、子 Agent 和数据导入 |
| `raven status` | 查看配置与运行状态 |
| `raven doctor` | 诊断模型服务商和运行环境问题 |
| `raven --version` | 查看已安装的 Raven 版本 |
| `raven upgrade --check` / `raven upgrade` | 检查更新，或升级托管安装的 Raven |
| `raven agents new <name>` | 使用 Raven 的模块化模板创建专用 Agent |
| `raven acp` | 通过标准输入输出将 Raven 作为 ACP Agent 提供服务 |
| `raven a2a enable` / `raven a2a disable` | 启用或关闭网关挂载的 A2A 入口；之后重启宿主 |
| `raven a2a serve` | 启动独立 A2A HTTP 监听器，默认本机回环端口 8710 |
| `raven sessions` | 创建、列出、派生、导出或删除会话；使用 `resume` 解析会话键 |
| `raven playbook` | 创建、校验、管理和运行可复用的 Agent 工作流 |
| `raven provider` | 配置模型服务商与端点、完成认证、测试连通性并选择当前模型 |
| `raven channels` | 列出、配置、认证、启用或禁用消息渠道 |
| `raven gateway` | 启动网关及已配置的服务 |
| `raven gateway status` / `raven gateway reload` / `raven gateway stop` | 查看网关状态、重新加载配置，或有序停止网关 |
| `raven serve` | 启动 WebSocket RPC 服务，并在 WebUI 构建产物可用时提供页面 |
| `raven skill` | 浏览 SkillForge 技能、查看内容、屏蔽或解除屏蔽技能，以及移除已安装的技能包 |
| `raven plugins` | 列出已安装的插件和当前记忆后端 |
| `raven plugin auth <server>` | 为 MCP 服务完成认证或刷新 OAuth 授权 |
| `raven mcp bridge <socket-path>` | 通过标准输入输出，将子 Agent 的 MCP 连接桥接到宿主管理的服务 |
| `raven import` | 预览并导入其他 AI 工具的数据、查看进度或停止导入 |
| `raven deep-research` | 配置、查看或重置 MiroThinker 研究集成 |
| `raven cron` | 创建、查看、运行、启用、禁用或删除定时任务 |
| `raven sentinel` | 配置主动行为，查看关注事项、例行任务、决策和提醒 |
| `raven ops connection` | 注册和列出机器；`add` 默认探测连接，`doctor` 校验注册表条目 |
| `raven sandbox` | 列出沙箱虚拟机、执行命令或打开 Shell；需要设置 `tools.sandbox.debug.enabled=true` |
| `raven tracing` | 打开本地追踪面板 |
| `raven tracing compact` | 合并重复的追踪产物，回收磁盘空间 |
| `raven trajectory` | 保存、回放、脱敏、标注和保留执行轨迹，用于调试 |

运行 `raven --help` 查看命令列表，或运行 `raven <command> --help` 查看指定命令的详细说明。
从源码检出运行时，在命令前加 `uv run`。

## 按任务查文档 { #find-documentation-by-task }

| 任务 | 阅读 |
| --- | --- |
| 选择对话或单次工作流 | [使用 Raven](using-raven.md) |
| 配置随 Raven 发布或外部 Agent | [Agent 集成](agent-integrations.md) |
| 继续或引导专用任务 | [与子 Agent 协作](agent-collaboration.md) |
| 与 Agent 共享浏览器工作 | [浏览器协作](browser-collaboration.md) |
| 保存并检索文档 | [知识库](knowledge.md) |
| 执行并持续观察长任务 | [长任务托管（Oncall）](oncall.md) |
| 配置消息机器人 | [渠道与消息](channels.md) |
| 集成编辑器或远程 Agent | [Agent 协议](agent-protocols.md) |
| 并行执行任务或查看任务图历史 | [DAG 编排](orchestration.md) |
| 保存并运行可复用工作流 | [Playbooks](playbooks.md) |
| 通过基准评估 harness 修改 | [Evolver：使用与实验](evolver.md) |
| 配置审批与无人值守访问 | [权限与安全](permissions.md) |
| 添加知识、工具或 Agent | [技能与扩展](skills-and-extensions.md) |
| 部署常驻宿主 | [自托管](self-hosting.md) |
| 创建 Agent 或插件 | [构建 Agent](building-agent.md)、[构建插件](building-plugin.md) |
| 保存、脱敏和回放失败 | [轨迹调试与回放](trajectory-debugging.md) |

使用站点搜索查找主题，通过 `--help` 确认命令语法。
可选协议功能是否可用，应查看握手时返回的能力声明。

在仓库根目录中，可同时搜索文档和实现：

```bash
rg -n 'ACP|A2A|run_subagent_dag' docs-site/docs raven tests
```

`run_subagent_dag`、`dag_status`、`cancel_dag` 和 `a2a_send` 是模型工具，不是 Shell
子命令。参数与行为请查对应指南。
