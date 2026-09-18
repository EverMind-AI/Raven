# 仓库布局

共享的 Python 运行时位于 `raven/`，Agent 定义、插件发行包、前端和开发工具与其并列存放。

主要目录：

```text
raven/                 # 共享运行时、功能引擎，以及 CLI/RPC/ACP 接口层
agents/                # 由已安装的 Raven 与插件组装而成的专用 Agent
plugins-dist/          # everos-memory、design-engine 和 ppt-engine 发行包
ui-web/                # 浏览器界面，桌面窗口也使用同一页面
ui-tui/                # React/Ink 终端界面
rpc-schema/            # 交互式客户端共用的 OpenRPC 契约
schemas/               # 生成的 Agent 与插件 JSON Schema
bridge/                # WhatsApp TypeScript 桥接服务
evolver/               # 由基准评估驱动的 harness 自进化工具
benchmarks/            # 基准适配器与评估集成
docker/                # 容器部署与 Compose 配置
tests/                 # 单元测试、集成测试和架构契约测试
scripts/               # 构建、打包、代码生成与仓库检查
docs/                  # 安装、开发与设计文档
```

下列运行时包和模块构成 `raven/` 下的标准提交 scope。修改 `raven/` 之外的内容时，使用 [`commitlint.config.cjs`](https://github.com/EverMind-AI/Raven/blob/main/commitlint.config.cjs) 中对应的目录或发行包 scope；提交规则见 [`AGENTS.md`](https://github.com/EverMind-AI/Raven/blob/main/AGENTS.md)。

| 包或模块 | 职责 |
|---|---|
| `acp` | ACP 服务端接口：向外部 Agent 宿主提供 Raven |
| `acp_client` | ACP 客户端、能力协商和第三方 Agent 事件适配 |
| `agent` | Agent Loop、Harness 模块、工具执行与子 Agent 编排 |
| `auth` | 认证与授权基础组件 |
| `browser` | 浏览器自动化、会话管理与导航检查 |
| `channels` | 消息适配器及其共享渠道契约 |
| `cli` | 命令行入口、配置向导与服务启动器 |
| `config` | 配置 schema、加载、迁移、准入校验与受控更新 |
| `contracts` | 契约定义（Paper）：运行时组件共享的接口与数据结构 |
| `context_engine` | 上下文组装、token 预算与会话压缩 |
| `core` | 装配根（Assembly Root）：运行时世代及其组件组装 |
| `eval_engine` | 任务完成判断、迭代反馈与工具审计的评估 hook |
| `gateway` | 渠道生命周期、运行时世代切换、事件投递与进程协调 |
| `home` | 统一解析 `RAVEN_HOME` 和配置路径（`home.py`） |
| `i18n` | 语言目录、翻译与提示词本地化 |
| `importer` | 从其他 AI 工具进行冷启动导入 |
| `knowledge` | 用户知识库的文档导入、索引与检索 |
| `market` | PlugHub 目录、信任检查、安装及贡献项账本 |
| `mcp` | MCP 服务连接与工具集成 |
| `memory_engine` | 记忆召回与整合、本地技能及 SkillForge 检索 |
| `observability` | 追踪 span 语义、属性提取与用量归属 |
| `ops` | 本地和远程机器注册表及执行传输 |
| `permissions` | 工具调用决策：允许、请求批准或拒绝 |
| `playbook` | 可复用工作流库及其校验、生成与执行 |
| `plugins` | 插件清单、发现、贡献项注册与内置插件 |
| `proactive_engine` | Sentinel 事件处理、cron 调度、心跳与主动决策 |
| `providers` | LLM 适配器、服务商池与模型到服务商的绑定 |
| `routing` | 任务分类，以及根据质量和成本选择模型 |
| `rpc` | 共享的类型化 RPC 方法、流式事件与网关控制接口 |
| `sandbox` | 隔离执行、虚拟机生命周期与调试工具 |
| `security` | 出站地址策略与提示词注入防护 |
| `session` | 会话存储、会话解析、标题与对话记录导出 |
| `skill_hub` | SkillHub 搜索、技能获取、技能包安装与安装策略 |
| `spine` | 轮次调度、并发通道、取消与事件投递 |
| `templates` | 随包发布的工作区文件、提示词包与 Agent 脚手架模板 |
| `token_wise` | Token 用量、计价、提示词缓存与效率策略 |
| `tracing` | Span 采集、埋点、追踪存储与产物管理 |
| `trajectory` | 执行轨迹包、回放、脱敏、结果标注与回归测试记录 |
| `updates` | 版本发现、升级规划、安装交接与更新提示 |
| `utils` | 共享工具函数，包括原子文件写入 |
