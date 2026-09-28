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

## 更新 Raven { #update-raven }

按安装时的方式更新 Raven。再次运行安装脚本时，它会先停止正在运行的 WebUI，
最后在前台启动新的 WebUI：按 Ctrl-C 停止后，运行 `raven web` 即可让 Raven
在后台持续运行。`~/.raven` 中的设置和会话都会保留。

### 在页面上更新 { #update-from-the-page }

左栏底部出现新版本提示时，点它并确认即可。Raven 会显示下载进度，装好新版本后自动
重启，页面也会自己刷新，设置和会话都会保留。如果还有回合、子智能体或待回答的问题
在进行（包括 IM 渠道里的），页面会提示你等它们结束再升级。用 `raven gateway` 或
`raven web --foreground` 手动启动的 Raven 没有守护进程来重启它，请用下面的命令行
方式更新。

### 更新一行命令安装的 Raven { #update-a-one-line-install }

再运行一次同一个安装命令即可。它会在当前版本之上安装最新发布版，完成后重新启动
WebUI。

也可以用命令行更新：

```bash
raven web --stop
raven upgrade
```

在 Linux 和 macOS 上，`raven upgrade` 会在前台完成安装后才返回。在原生 Windows 上，
它会把安装交给一个独立的辅助进程并立即返回：请等它打印完成信息后再继续。然后再
启动 Raven：

```bash
raven web
```

`raven upgrade` 会安装最新发布版，但不会重启正在运行的 Raven，所以要先停止
WebUI，升级完成后再启动。如只想检查是否有新版本而不安装，请运行
`raven upgrade --check`。

### 更新源码安装的 Raven { #update-a-source-checkout }

拉取最新代码后，再运行一次本地安装脚本（PowerShell 中为 `.\install.ps1`）：

```bash
git pull
./install.sh
```

安装脚本会以可编辑模式重新安装该检出目录，仅在源码有变动时重新构建 TUI 和
WebUI，并重新启动 WebUI。`raven upgrade` 不会更新源码安装，
因为要更新的代码就是检出目录本身。

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
