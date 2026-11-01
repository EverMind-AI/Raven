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

Windows 自带的 Windows PowerShell 5.1 无法跟随这个地址的重定向，会报错
`(308) Permanent Redirect`。遇到这个错误时，请使用直连安装地址：

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

### 下载源连不上时 { #when-a-download-host-cannot-be-reached }

安装脚本、`raven resources`、`raven upgrade`，以及 Raven 通过 `uvx` 或 `npx` 启动的
智能体和 MCP 服务，都会先从默认地址下载。默认地址完全连不上时，改从中国大陆的镜像下载：
Hugging Face 用 hf-mirror.com，GitHub 上的文件用 jsDelivr，PyPI 和 LibreOffice 用清华
TUNA 镜像，Node.js、npm、Playwright 以及 uv 安装的 Python 用 npmmirror。默认地址返回
HTTP 错误或证书校验失败时都不会切换。经过代理时，代理拒绝连接该地址（403 或 407）同样
不会切换；代理返回 5xx，或 SOCKS 代理连不通，都算作连不上。uv 自带的安装脚本和
Playwright 下载 Chromium 时说不清失败原因，所以会再探测它们下载用的那几个地址，全都连不上
时才改从镜像下载；LibreOffice 和字体文件有固定的摘要校验，默认地址都失败后会改从镜像下载。

你自己配置的下载地址会保留：`HF_ENDPOINT`，写在 `UV_DEFAULT_INDEX` 或 `uv.toml` 里的 uv
索引，为 uv 安装脚本或 uv 安装的 Python 指定的下载源（如 `UV_INSTALLER_GITHUB_BASE_URL`、
`UV_PYTHON_INSTALL_MIRROR`），`PLAYWRIGHT_DOWNLOAD_HOST`，以及 npm 能读到的任何一处
registry 设置。`uvx` 和 `npx` 只在镜像连得上时才切换，所以缓存里已有的包离线时照样能启动。
`uvx` 的镜像只写在这次启动的命令行上，它启动的工具看不到。npx 会把 registry 传给它启动的
所有进程，在那里会盖过项目自己指定的 registry，所以这种情况下 Raven 不运行 npx：它从镜像把
包装进自己的目录（`~/.raven/cache/npx`；Raven 节点会放在节点自己的目录下），再从那里启动包的
命令，环境仍是这次启动原本的环境。项目里或 npm 全局已经装好的包仍由 npx 启动，项目的 `.npmrc`
指定了 registry 时也一样。设置 `RAVEN_NO_MIRROR=1` 可让所有下载都只使用默认地址。这个开关在
启动自带的环境里也会被读到，Raven 节点也会收到宿主机的这个值，所以跑在另一台机器上的智能体
同样只用自己的地址。Raven 自己的发布文件来自 GitHub Releases，没有镜像。

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

拉取最新代码后，再运行一次本地安装脚本。Linux 和 macOS：

```bash
git pull
./install.sh
```

Windows 上先停掉页面，再拉取代码并运行 PowerShell 安装脚本。如果 `git pull`
失败，先修复再运行安装脚本：

```powershell
raven web --stop
git pull
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
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

### 公司代理或私有 CA { #behind-a-corporate-proxy-or-private-ca }

Raven 用操作系统的证书库校验 HTTPS 证书，IT 部门装进系统的根证书无需额外设置即可
被信任。如果服务商检测报告 `certificate_untrusted`，请把签发网关或代理证书的根证书
加入该证书库。

根证书没法导入时（自建的网关、临时虚拟机上的开发证书），服务商配置节可以改为
跳过校验：在 `~/.raven/config.json` 的对应配置节上设置 `tls_verify: false`。该节的
模型调用、**设置 > 模型服务商** 里的 `/v1/models` 检测，以及知识库经该服务商发出的
向量化（embedding）请求，都会直接接受网关的证书，不再要求任何证书库收留它。只对
单个配置节、且在可信网络里这样用：跳过校验意味着接受那台主机上应答的任何证书。

WSL2 中的 Linux 发行版有自己的证书库，看不到 Windows 里安装的证书。在 WSL 内导入
一次根证书：

```bash
sudo cp corporate-ca.pem /usr/local/share/ca-certificates/corporate-ca.crt
sudo update-ca-certificates
```

EverOS 记忆服务和基于 Node 的 Agent 作为独立程序运行，读取的是环境变量。请在 shell
配置文件里导出下面两项，而不是只在当前终端里设置，这样 Raven 启动的每个进程都能拿到：

```bash
export SSL_CERT_FILE=/path/to/ca-bundle.pem
export NODE_EXTRA_CA_CERTS=/path/to/corporate-ca.pem
```

`SSL_CERT_FILE` 会替换整份受信任根证书列表，所以要指向一份同时包含公司根证书的完整
证书包，而且这个路径必须存在：模型调用和服务商检测所用的 httpx 遇到不存在的路径会直接
无法启动。在 Debian、Ubuntu 和 WSL 中，运行过 `update-ca-certificates` 之后，
`/etc/ssl/certs/ca-certificates.crt` 就是这样的证书包。在 macOS 上需要先生成一份：

```bash
cat /etc/ssl/cert.pem corporate-ca.pem > ~/ca-bundle.pem
```

在 macOS 和 Windows 上，操作系统还会套用自己的证书策略；例如 macOS 会拒绝有效期超过
825 天、或未标明用于服务器身份验证的服务器证书，即使它信任签发该证书的根证书。如需改回
各个库自带的证书列表，在同一个配置文件里导出 `RAVEN_NO_SYSTEM_CA=1`。

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
