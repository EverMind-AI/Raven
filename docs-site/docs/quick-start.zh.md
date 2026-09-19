<p class="em-eyebrow">首次运行</p>

# 快速开始

<p class="em-standfirst">安装 Raven，配置模型服务商，开始使用 WebUI。</p>

## 安装 Raven { #install-raven }

根据操作系统选择安装方式。安装脚本会配置 Raven 的托管环境，让你可以在终端直接使用
`raven` 命令。

### Linux、macOS 或 WSL2 { #linux-macos-or-wsl2 }

```bash
curl -fsSL https://raven.evermind.ai/install.sh | bash
```

### Windows PowerShell { #windows-powershell }

```powershell
irm https://raven.evermind.ai/install.ps1 | iex
```

如果 Windows PowerShell 5.1 无法处理重定向，请使用直连安装地址：

```powershell
irm https://raw.githubusercontent.com/EverMind-AI/Raven/refs/heads/main/install.ps1 | iex
```

### 从源码安装 { #from-a-source-checkout }

如需开发 Raven 或体验尚未发布的改动，请克隆仓库并运行本地安装脚本：

```bash
git clone https://github.com/EverMind-AI/Raven.git
cd Raven
./install.sh
```

运行本地 `install.sh` 会以可编辑模式安装 Raven 及其内置插件，直接使用工作树中的代码，
并从同一份源码构建 TUI 和 WebUI。通过管道运行安装脚本时，即使当前目录是克隆的仓库，
也会安装已发布的 wheel 包。如需在管道安装时指定本地源码目录，请设置
`RAVEN_LOCAL_SRC=<dir>`。

## 配置第一个模型服务商 { #configure-the-first-provider }

安装后运行配置向导：

```bash
raven onboard
```

按照向导提示配置模型服务商，并选择要启用的内置 Agent。之后可以在 WebUI 的
**设置 > 模型服务商（Settings > Model providers）**中添加或修改配置。

## 启动 Raven { #start-raven }

启动 WebUI，同时在后台运行 Raven 引擎：

```bash
raven web
```

本地页面地址是 `http://127.0.0.1:18792`。停止后台服务：

```bash
raven web --stop
```

Docker 部署以及从源码构建、启动服务的步骤，请参阅[自托管](self-hosting.md)。
