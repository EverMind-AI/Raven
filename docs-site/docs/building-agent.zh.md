# 构建 Agent

需要在共享 runtime 上拥有独立身份、工具配置和执行策略时，构建 Raven Agent。
接入已有外部产品则见[Agent 集成](agent-integrations.md)。

## 先生成骨架再定制 { #scaffold-before-customizing }

先配置宿主模型服务商，预览文件后再创建：

```bash
raven agents new example-agent --dry-run
raven agents new example-agent
```

第二条命令在 `$RAVEN_HOME/agents/example-agent` 写入新目录，校验 manifest、
编译生成的 Python、扫描 discovery，并默认启动 ACP initialize smoke check。
检查会启动 runtime、可能写运行状态，但不是完整模型任务。

名称采用小写 kebab-case，不能与已发布或已发现 Agent 身份冲突。`--display` 修改
显示名，`--here` 写入 `./agents/`，`--no-smoke` 跳过启动检查，`--register` 显式
固定配置条目。虽然运行时支持其他后端，scaffold 当前只支持 `--kind acp`。

不要对已有目录重复执行创建命令来升级。应阅读生成 README 并修改定义。缺少 provider
凭据导致的拒绝，与协议或插件加载失败不同。

## 理解生成文件 { #understand-the-generated-files }

| 文件 | 职责 |
| --- | --- |
| `subagent.json` | 委派身份、描述、所有权和启动命令 |
| `run.py` | 渲染配置并启动已安装的 Raven ACP server |
| `config.json` | 基于共享 runtime 的 Agent 设置 |
| `.env.example` | 记录 secret slot 和存储覆盖项 |
| `install.py` | 自定义位置或固定 Agent 名册条目的可选注册 |
| `plugins/<id>/raven-plugin.toml` | 声明 Agent 的运行时贡献 |
| 插件 Python package | 实现工具和 hook 工厂 |
| `README.md` | 针对生成 Agent 的说明 |

Discovery 替换 manifest 中的 `{PYTHON}` 和 `{SUBAGENT_DIR}`。保留可移植占位符，
不要写死当前机器的源码路径。填写描述和 ownership 的 TODO；委派模型据此决定何时选择 Agent。

不要仅为修改 UI 标签就变更 `name` 或 memory identity，已有会话和调用记录引用这些身份。

## 配置与状态 { #configuration-and-state }

Launcher 在 state root 下渲染私有配置。公开定义不能含秘密，应使用文档中的环境或
`.env` slot，并把私有文件排除在 git 外。没有 Agent 自有 key 时可继承宿主 provider
配置；自有 key 模式还要配置 provider/model 端点，key 本身不是完整模型绑定。

区分三个位置：

- **Working directory：** 调用方提供的任务文件。
- **State root：** 渲染配置和产品工作状态。
- **ACP home：** 子引擎身份、会话和 skill pool。

默认存储在定义目录之外。子 Agent ACP home 不能被宿主传入的工作目录包含；
使用生成的存储覆盖项前应检查边界。

插件专用值位于 `plugins.config["<plugin-id>"]`。Launcher 会注入插件根目录，不要
猜一个相对路径再添加第二套 discovery。可选 ACP modes 是未来轮次的配置档位，
不能替代工具权限配置。

## 实现案例：Raven-Code { #case-study-raven-code }

Raven-Code 不复制核心 loop 就实现专用行为。Launcher 渲染配置并发现
`agents/raven-code/plugins/code-flow/`。插件通过[构建插件](building-plugin.md)
中的契约提供工具、loop hook 和 session observer。

| 职责 | 插件 `code_flow/` 包内位置 | 边界 |
| --- | --- | --- |
| 项目指令 | `flow.py` iteration hook | 绑定工作目录文件和剩余 prompt 预算 |
| 读取版本跟踪 | `tools/read_state.py`、`tools/filesystem.py` | Session ledger，不是全局读取授权 |
| 持久清单 | `tools/todo.py` 与 flow/session 生命周期 | 保存记录是事实来源；绑定实际 session |
| 并发提醒 | `sessions.py` 与 iteration hook | 共享目录证据，不是排他所有权 |
| Git 报告 | `manifest.py` 输出 `raven.harnessManifest` 元数据 | 未知/共享事实不能变成“可集成” |
| Session 清理 | Manifest 的 session observer | 只移除受影响 session 的状态 |

Manifest 有意替换 `read_file`、`edit_file` 等名称。
应公布实际替换工具的参数 schema，不能沿用旧工具面的说明。

两个配置开关独立：`plugins.config["code-flow"].enabled` 控制提醒/报告；
同一个 slice 中的 `tools.enabled` 控制替换工具。插件模型中两者默认 false，
Raven-Code launcher 为自身渲染启用。即使提醒关闭，只要工具启用，
checklist/read-ledger 仍需要正确绑定。

Flow 追加项目指令，不替换宿主身份，也不重写用户原始文本。
Launcher 的 `CODE_PROJECT_FILES` 选择项目文件，`off` 表示不读取。
可信指令加载不能变成越过绑定项目的任意访问。

新 coding Agent 从 scaffold 和小型已审核工具集开始，复用契约而不是复制产品。
应测试：

- 未读即编辑，以及其他写入者修改后的编辑；
- 两个 session 共享目录；
- 后续轮次或上下文裁剪后恢复 checklist；
- Session 删除与 runtime replacement；
- Git 信息不可用和非 Git 目录；
- 使用沙箱时，宿主 exec 替换应拒绝接管。

相关测试包括 `tests/test_agents_code_flow_read_state.py`、
`test_agents_code_flow_todo.py`、`test_agents_code_flow_project_files.py`、
`test_agents_code_flow_manifest.py` 和 `test_agents_code_tools_plugin.py`。
[使用说明](agent-integrations.md#working-safely-with-raven-code)介绍操作者能从这些机制推断什么。

## 增加工具与行为 { #add-tools-and-behaviour }

从生成的 greeting tool 和 hook 工厂开始。工具实现 `name`、`description`、
`parameters`、`async execute`；在 manifest 声明，并先独立测试，再暴露给模型。

生成 hook 展示 loop timing 契约。当前随 Raven 发布的 Agent 也使用较窄的 `AgentParticipant`
契约表达逐轮判断，应按行为选接口，不要复制整个 Agent Loop。详见[构建插件](building-plugin.md)。

贡献应限制在该 Agent 的配置内。宿主导入插件不能意外在无关会话启用专用工具。
不要为使 discovery 生效而向共享 runtime 添加产品专有导入；Raven 将定义目录视为
数据，通过插件接口加载贡献。

## 需要时打包 engine { #package-an-engine-when-needed }

独立发行 harness 时，可以预览 wheel 形式：

```bash
raven agents new example-engine-agent --engine-wheel --dry-run
```

去掉 `--dry-run` 会创建 Agent 定义与 engine project。Engine manifest 放在 Python
package 内，并加入 `raven.plugins` entry-point group。Agent 声明 engine package
和发行包；子解释器能导入之前，readiness 保持 false。

在目标项目/环境中使用 `uv` 管理依赖。Raven 应解析到已验证的源码树或发行包；
公共索引上不带限定的 `raven` 名称可能是另一个项目。不要照搬旧设计记录中的裸安装
命令。仓库环境设置见[开发](development.md)。

## 验证、发现与运行 { #verify-discover-and-operate }

1. 使用实际启动解释器校验生成 manifest 并导入各工厂。
2. 检查 ACP initialize 和预期工具 roster，不只看进程退出。
3. 运行有界只读任务，再验证所需修改、输出或会话续接。
4. 测试缺少凭据/engine、权限拒绝、取消和插件激活失败。
5. 修改定义后重启常驻宿主，验证能选择并调用该 Agent。

Home tree 内定义无需 pin 即可发现。树外定义应有意使用 scaffold 注册选项或生成的
installer。移动后固定的名册条目可能仍指向旧目录，应更新固定配置，不能假定自动发现会修复它。

仓库依据位于 `agents/BUILDING.md`、`raven/cli/agents_commands.py` 及 `schemas/`
中的 schema 导出。维护者可运行：

```bash
uv run pytest tests/test_cli_agents_commands.py tests/test_subagent_registry.py -q
```

这些测试覆盖 scaffold 与 registry，不替代新 Agent 的真实验收任务。
