# 命令参考

| 命令 | 用途 |
| --- | --- |
| `raven` 或 `raven tui` | 启动终端界面 |
| `raven web` | 打开 WebUI，并让 Raven 在后台运行 |
| `raven web --stop` | 停止 WebUI 后台服务 |
| `raven agent -m "..."` | 执行单次任务 |
| `raven onboard` | 配置模型服务商、沙箱、消息渠道、记忆、网络工具密钥、子 Agent 和数据导入 |
| `raven status` | 查看配置与运行状态 |
| `raven doctor` | 诊断模型服务商和运行环境问题 |
| `raven --version` | 查看已安装的 Raven 版本 |
| `raven upgrade --check` / `raven upgrade` | 检查更新，或升级由安装工具管理的 Raven |
| `raven agents new <name>` | 使用 Raven 的模块化模板创建专用 Agent |
| `raven acp` | 通过标准输入输出将 Raven 作为 ACP Agent 提供服务 |
| `raven sessions` | 创建、列出、派生、导出或删除会话；使用 `resume` 解析会话键 |
| `raven playbook` | 创建、校验、管理和运行可复用的 Agent 工作流 |
| `raven provider` | 配置模型服务商与端点、完成认证、测试连通性并选择当前模型 |
| `raven channels` | 列出、配置、认证、启用或禁用消息渠道 |
| `raven gateway` | 运行消息网关 |
| `raven gateway status` / `raven gateway reload` / `raven gateway stop` | 查看运行中的网关、重新加载配置，或平稳停止网关 |
| `raven serve` | 运行无界面的 WebSocket RPC 服务，并在页面已构建时提供 WebUI |
| `raven skill` | 浏览 SkillForge 技能、查看内容、屏蔽或解除屏蔽技能，以及移除已安装的技能包 |
| `raven plugins` | 列出已安装的插件和当前记忆后端 |
| `raven plugin auth <server>` | 为 MCP 服务完成认证或刷新 OAuth 授权 |
| `raven mcp bridge <socket-path>` | 通过标准输入输出，将子 Agent 的 MCP 连接桥接到宿主管理的服务 |
| `raven import` | 预览并导入其他 AI 工具的数据、查看进度或停止导入 |
| `raven deep-research` | 配置、查看或重置 MiroThinker 研究集成 |
| `raven cron` | 创建、查看、运行、启用、禁用或删除定时任务 |
| `raven sentinel` | 配置主动行为，查看关注事项、例行任务、决策和提醒 |
| `raven ops connection` | 注册、列出本地或远程机器，并检查连通性 |
| `raven sandbox` | 列出沙箱虚拟机、执行命令或打开 Shell；需要设置 `sandbox.debug=true` |
| `raven tracing` | 打开本地追踪面板 |
| `raven tracing compact` | 合并重复的追踪产物，回收磁盘空间 |
| `raven trajectory` | 保存、回放、脱敏、标注和保留执行轨迹，用于调试 |

运行 `raven --help` 或 `raven <command> --help` 查看完整命令说明。
