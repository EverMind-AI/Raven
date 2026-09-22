# 构建插件

Raven 插件通过清单文件和 Python 工厂函数扩展运行时，提供工具、钩子或后台服务。
它与 Skill、MCP 服务及 Codex 插件使用不同的接口。插件代码在 Raven 进程中执行，
应只加载可信来源的插件；清单文件不提供沙箱隔离。

## 选择贡献类型 { #choose-a-contribution }

| `plugin.contributes` 下的分组 | 用途 |
| --- | --- |
| `tools` | 模型可调用动作 |
| `hooks` | Loop timing hook 或支持的 participant 工厂 |
| `memory_backends` | 记忆存储/召回实现 |
| `services` | 常驻宿主生命周期工作 |
| `tool_gates` | 插件专用工具判断 |
| `session_observers` | 会话生命周期观察 |
| `onboard` | 插件记忆后端的设置步骤 |

使用 `raven/contracts/` 的现有契约，不要导入 CLI/RPC 内部实现。Tool Gate 不是平台
Permission Gate，不替代其授权决策。

## 从可用模板开始 { #start-with-a-working-template }

默认 Agent scaffold 包含目录形式的插件：

```bash
raven agents new example-agent --dry-run
```

按[构建 Agent](building-agent.md)创建后，定制生成工具/hook。目录如下：

```text
plugins/example-agent-flow/
  raven-plugin.toml
  example_agent_flow/
    __init__.py
    plugin.py
    tools/
      __init__.py
      hello.py
```

对应 manifest：

```toml
[plugin]
id = "example-agent-flow"
version = "0.1.0"
display_name = "Example Agent Flow"
enabled_by_default = true

[[plugin.contributes.tools]]
name = "example_agent_hello"
factory = "example_agent_flow.tools.hello:make_hello"

[[plugin.contributes.hooks]]
name = "example_agent_flow"
factory = "example_agent_flow.plugin:make_hook"
```

目录发现只读取清单，不导入插件代码。通过 entry point 发现插件时，
`importlib.resources.files()` 会为查找包内清单而导入该包的 `__init__.py`，
因此不要在此文件中启动服务或执行其他初始化操作。
激活阶段才加载 `module.path:callable` 指向的工厂；清单有效不代表工厂一定能加载或工具已注册。

## 工厂与配置 { #factories-and-configuration }

工厂接收 `raven.plugins.context` 中的 `PluginContext`：

- `config` 只包含该插件经过准入检查的配置。
- `services` 提供契约声明的宿主服务接口。
- `logger` 已绑定插件身份。

工具工厂返回 `Tool`，或返回 `None` 放弃注册。工具提供稳定名称、描述、JSON-schema
参数，以及返回文本或 `ToolResult` 的异步执行。复用生成的 greeting 示例，不要发明
另一套工厂签名。

配置切片位于 `plugins.config["example-agent-flow"]`。声明的 schema 字段在 admission
检查；空 schema 是直通，不代表任意值自动得到校验，使用前应校验自己的设置。
未知配置键可能警告后透传。

新工具名加插件/Agent 前缀。使用内置同名工具会有意替换内置实现；两个插件贡献同名工具
会冲突。Activation 失败后宿主仍可能继续运行并记录警告，因此既要检查启动，也要检查
实际工具 roster。

## Hook 与逐轮 participant { #hooks-and-per-turn-participants }

`AgentHook` timing phases 为 `before_user_inbound`、`before_iteration`、
`before_execute_tools`、`after_iteration`、`terminal_answerless`、`after_send`。
生成示例只覆盖 `after_send`，其他阶段直通。工具收窄属于 `before_iteration`，不是任意阶段。

面向 Agent 判断，当前 `AgentParticipant` paper 提供只读 `StepView` 输入和可选
verbs，如 `intake`、`select_tools`、`advise`、`system_addendum`、`review`、
`salvage`、`outbound`、`archive`。宿主拥有 timing，并通过 hook contribution 路径
适配支持的 participant 工厂。Participant 每轮构建，避免轮次状态泄漏到其他并发对话。

选择前读 `raven/contracts/participant.py` 和 `raven/contracts/loop_hooks.py`。
遵守契约版本/tier 边界，不要直接写 loop transcript 或深入 scheduler。

## 生命周期与打包 { #lifecycle-and-packaging }

Service 实现 `raven/contracts/services.py` 中的生命周期。Assembly 构造贡献，
常驻宿主负责 start/stop。不要在 import 时启动后台任务，也不要由 service 修改已装配
runtime。既测试启动，也测试关闭与失败。

目录发现扫描 `<root>/<plugin-id>/raven-plugin.toml`，包括 `plugins.dirs` 提供的根。
重复插件 ID 的优先级从高到低为：内置、用户目录、项目/额外目录、entry point。
未检查实际生效副本前，不要把重复 id 当作版本选择机制。

独立安装的 engine 使用 wheel scaffold。Entry point 指向 package，manifest 必须打包在其中：

```toml
[project.entry-points."raven.plugins"]
example-agent-engine = "example_agent_engine"
```

安装 enabled-by-default engine 后，该环境中的每个 Raven 进程都可能发现它。
保留 engine 模板的显式配置门，让工具只为预期 Agent 注册。依赖用 `uv` 管理，运行
状态和秘密不要放进发行包。

## 验证清单 { #verification-checklist }

1. 通过 `PluginManifest.from_toml_path` 解析 manifest。
2. 在实际 runtime 环境导入工厂，覆盖 enabled、disabled 和错误配置。
3. 不使用真实模型，测试工具参数/结果以及拒绝或 rollback 路径。
4. 经实际托管入口验证工具 roster，并执行一个低风险真实任务。
5. 测试两个并发对话、service 关闭和缺少依赖。

维护者回归检查：

```bash
uv run pytest tests/test_plugin_manifest.py tests/test_plugin_registry.py tests/test_plugin_tools.py tests/test_plugin_hooks.py -q
```

Schema 导出位于 `schemas/`。开发流程和仓库检查见[开发](development.md)，权限另见
[权限与安全](permissions.md)。
