# 开发

本文介绍如何从本地源码构建、运行和测试 Raven。项目使用 **Python 与 `uv`** 管理依赖，
使用 `hatchling` 打包。

产品扩展请选择[构建 Agent](building-agent.md)、[构建插件](building-plugin.md)或
[协议与后端集成](protocol-backends.md)。这些指南介绍扩展契约，本页介绍共享仓库工作流。

基准驱动的 harness 实验请先读[Evolver：使用与实验](evolver.md)，包括维护状态
和安全边界。当前 harness 故障的诊断见[轨迹调试与回放](trajectory-debugging.md)。

## 系统依赖 { #system-prerequisites }

`uv` 负责安装 Raven 的 Python 依赖。**LibreOffice** 需要单独安装，用于将幻灯片和其他
受支持的办公文档转换为 PDF。幻灯片引擎依赖这一转换进行渲染、版面测量和预览，网关则展示
生成的 PDF。缺少 LibreOffice 时仍可生成幻灯片文件，但依赖渲染结果的功能不可用，相关集成
测试也会跳过。

```bash
apt install libreoffice                          # Debian / Ubuntu
brew install --cask libreoffice                  # macOS
winget install TheDocumentFoundation.LibreOffice # Windows
```

浏览器工具还需要单独安装 Chromium。请在源码仓库中运行：

```bash
uv sync --all-extras && uv run playwright install chromium
```

使用托管安装时，`install.sh` 会完成浏览器下载。运行 `raven doctor`，可在
**External tools** 一节中查看这两项依赖的状态。

## 安装依赖 { #install-dependencies }

```bash
cd /path/to/raven
uv sync
```

该命令会根据 `uv.lock` 创建或更新 `.venv`，安装全部核心依赖。

可选依赖：

```bash
uv sync --extra channels   # 消息渠道集成（Telegram、Slack 等）
uv sync --extra sandbox    # boxlite 沙箱执行
uv sync --extra tools      # 网页与正文提取工具
uv sync --all-extras       # 安装全部可选依赖
```

## 以可编辑模式安装 { #install-the-package-in-editable-mode }

```bash
uv pip install -e .
```

`uv sync` 默认以可编辑模式安装项目。上面的命令可显式重新安装，让 `raven` 命令继续使用
源码工作树中的代码。

## 运行 CLI { #run-the-cli }

使用 `uv run` 可直接在项目环境中运行命令。下方激活环境的示例适用于 POSIX Shell；
Windows 用户可以使用 `uv run` 方式。

```bash
# 通过 uv run，自动使用 .venv，无需激活：
uv run raven --help

# 或先激活环境：
source .venv/bin/activate
raven --help
```

## 首次配置 { #first-time-setup }

```bash
uv run raven onboard
```

按照向导提示选择模型服务商、配置凭据并选择模型。默认情况下，Raven 将配置保存在
`~/.raven/config.json`，并在配置过程中创建工作区目录。

## 常用命令 { #common-commands }

| 命令 | 说明 |
|---|---|
| `raven tui` | 启动交互式聊天 TUI |
| `raven agent -m "Hello"` | 发送单条消息后退出 |
| `raven gateway` | 启动网关，以及已启用的消息渠道、心跳和定时任务 |
| `raven status` | 查看配置路径、工作区与 API 密钥状态 |
| `raven channels status` | 显示哪些消息渠道已启用 |
| `raven provider login <name>` | 通过 OAuth 登录模型服务商，例如 `openai-codex`、`minimax-global` 或 `minimax-cn` |

Raven 的命令概览请参阅[命令参考](commands.md)。

## 运行测试 { #run-tests }

```bash
uv run pytest tests/
```

测试需要 Python 3.12 或更高版本。渲染幻灯片的测试还需要 LibreOffice，缺少时会跳过。
Pytest 配置位于 `pyproject.toml`，其中异步测试使用 `asyncio_mode = "auto"`。
