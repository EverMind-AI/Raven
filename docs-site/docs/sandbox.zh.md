# 沙箱

启用沙箱后，Shell 命令和 stdio MCP 服务进程会在 NVIDIA OpenShell 沙箱中运行，不再直接在宿主机上执行。
每个沙箱是一个容器，它能访问哪些文件、以什么身份运行、能连哪些网络，由内核（Landlock 和 seccomp）
和容器外的 OpenShell supervisor 强制执行。挂载目录（包括共享工作区）仍按配置的权限开放。

Raven 通过本机的 OpenShell gateway 创建和删除沙箱，自己不再运行虚拟机。

## 1. 安装 { #1-installation }

需要两样东西：Raven 环境里的 OpenShell SDK，以及同一台机器上运行着的 OpenShell gateway。

### 1.1 SDK { #11-the-sdk }

SDK 是可选依赖 `sandbox`。在源码仓库中安装：

```bash
uv sync --extra sandbox
```

对于已安装的 Raven，缺少 SDK 时 `raven onboard` 会打印出针对它自身解释器的安装命令，形如：

```bash
uv pip install --python /path/to/raven/python 'openshell>=0.1.8,<0.2'
```

这个可选依赖锁定 `openshell>=0.1.8,<0.2`。SDK 和 gateway 同步发布，请让两者保持在同一个小版本。

### 1.2 gateway { #12-the-gateway }

用官方安装脚本安装 OpenShell 0.1.8，并锁定到发布标签。首次启动前先关闭遥测，打包版 gateway 会读取
`~/.config/openshell/gateway.env`：

```bash
mkdir -p ~/.config/openshell
printf 'OPENSHELL_TELEMETRY_ENABLED=false\n' > ~/.config/openshell/gateway.env
curl -fsSL -o /tmp/openshell-install.sh https://raw.githubusercontent.com/NVIDIA/OpenShell/v0.1.8/install.sh
OPENSHELL_VERSION=v0.1.8 sh /tmp/openshell-install.sh
```

Linux 上安装脚本会添加一个 systemd 用户服务，macOS 上则通过 Homebrew 安装。两者都会把 gateway 登记到
`openshell` CLI，并且只监听 `127.0.0.1:17670`。

Raven 会把工作区挂载进每个沙箱，所以 gateway 必须允许 bind mount。写入
`~/.config/openshell/gateway.toml`：

```toml
[openshell]
version = 2

[openshell.gateway]
compute_driver = "docker"

[openshell.drivers.docker]
allow_driver_config = true
enable_bind_mounts = true

[openshell.drivers.docker.resource_admission]
enabled = false
```

然后重启 gateway 并检查：

```bash
systemctl --user restart openshell-gateway        # Linux
brew services restart nvidia/openshell/openshell  # macOS
openshell status                                  # 应显示：Status: Connected
```

在 macOS 上用 colima 而不是 Docker Desktop 时，supervisor 无法通过容器的回环地址连到 gateway。
如果沙箱一直停在 `ControlSupervisorStartFailed`，在 Docker 驱动那一节加上：

```toml
supervisor_network_mode = "bridge"
grpc_endpoint = "https://host.docker.internal:17670"
```

## 2. 配置 { #2-configuration }

在 `config.json` 的 `tools` 下添加 `sandbox` 块。JSON 无效时会打出警告并回退到默认值，其中包括
`tools.sandbox.backend = "none"`，所以依赖沙箱隔离之前请先检查启动警告：

```json
{
  "tools": {
    "sandbox": {
      "backend": "openshell"
    }
  }
}
```

`backend = "openshell"` 时，缺少 SDK 或连不上 gateway 都会抛出 `SandboxInitError`。
Raven 不会自行退回宿主机执行；只有 backend 为 `"none"` 时才在宿主机上运行。

### 完整配置项 { #full-reference }

| 键 | 类型 | 默认值 | 说明 |
|-----|------|---------|-------------|
| `backend` | `"none"` \| `"openshell"` | `"none"` | `"none"` 在宿主机上运行命令，没有隔离。 |
| `gateway` | 字符串 \| null | `null` | 在 `openshell` CLI 中登记的 gateway 名称；`null` 表示使用当前激活的那个。 |
| `image` | 字符串 | `"ubuntu:22.04"` | 每个沙箱启动所用的容器镜像。 |
| `cpus` | 整数 | `2` | 每个沙箱的 CPU 上限。 |
| `memoryMib` | 整数 | `2048` | 每个沙箱的内存上限，单位 MiB。 |
| `extraVolumes` | 列表 | `[]` | 额外的 bind mount，每项为 `[宿主路径, 沙箱路径, "ro"\|"rw"]`。 |
| `defaultTimeout` | 整数 | `120` | 未指定超时时每条命令的秒数。 |
| `verifyTimeout` | 整数 | `30` | 启动探测的秒数。 |
| `createTimeout` | 整数 | `300` | 拉取镜像并创建沙箱的秒数。 |

沙箱能访问哪些地址不在这里设置，它属于权限的一部分，写在 `permissions.network` 下，见
[网络策略](#41-network-policy)。

### 从 boxlite 后端迁移 { #migrating-from-the-boxlite-backend }

boxlite 微虚拟机后端已经退役。为它写的配置仍然能加载，第一次加载时会自动改写一次，每处改动都有提示：

- `backend: "boxlite"` 和 `backend: "auto"` 改成 `"openshell"`。这两个值以前都指 boxlite，所以没装
  OpenShell 的机器会拒绝启动沙箱，不会退回到宿主机上执行。
- `debug` 和 `diskSizeGb` 会被删掉。
- `raven sandbox list/exec/shell` 已移除，请改用 OpenShell CLI，见[查看沙箱](#35-inspecting-sandboxes)。
- Raven 数据目录下旧的 boxlite 状态目录（`sandbox/boxlite`）不再使用，可以删除。

## 3. 工作原理 { #3-how-it-works }

`SandboxExecutor`（`raven/sandbox/interfaces.py`）有两个实现：

| 实现 | 何时使用 | 隔离方式 |
|----------------|-----------|-----------|
| `OpenShellExecutor` | `backend = "openshell"` | 每个执行器一个 OpenShell 沙箱 |
| `DirectExecutor` | `backend = "none"` | 无，命令在宿主机上运行 |

### 3.1 每个智能体一个沙箱 { #31-one-sandbox-per-agent }

主智能体循环在整个生命周期里持有一个执行器。每次 subagent 运行、每个 DAG 节点都有自己的执行器，
因而各有一个沙箱：它们的文件、进程、网络规则和审计记录彼此分开。执行器启动时创建沙箱，停止时删除沙箱。

在 gateway 上删除一个沙箱要好几秒，所以 `stop()` 只发起删除就返回。Raven 关闭时最多等待 20 秒，
让未完成的删除做完。因崩溃而遗留的沙箱带有 `raven.owner` 和 `raven.pid` 两个标签，同一台机器、
同一用户下的下一个 Raven 在启动第一个沙箱时会把它删掉。

### 3.2 挂载与运行身份 { #32-mounts-and-identity }

工作区以读写方式挂载到 `/workspace`。`read_file`、`write_file` 等文件工具直接使用宿主路径，
所以沙箱里的 `/workspace/foo.py` 就是同一个文件。Raven 还会挂载：

- 智能体主目录不在工作区内时，挂载到 `/agent-home`；
- 插件包运行 stdio MCP 服务时，把插件数据根目录挂载到 `/plugin-data`；
- `extraVolumes` 里的每一项。

沙箱会替换掉 OpenShell 默认的文件系统策略：系统路径（`/bin`、`/usr`、`/lib`、`/etc`、`/proc`、
`/var/log`、`/dev/urandom`）只读，`/tmp` 和各挂载点可写，其余路径一律不可访问。

在非 root 宿主上，沙箱以和 Raven 相同的用户和组运行，所以它在工作区写出的文件归你所有。
OpenShell 不允许以 root 身份运行工作负载，所以在 root 宿主上，沙箱以镜像指定的用户运行
（镜像没有声明用户时为 UID 1000），这个用户必须能写工作区。Raven 在沙箱启动时会检查这一点，
写不了就直接给出明确的错误。

### 3.3 命令 { #33-commands }

每条命令都以 `sh -c <命令>` 的形式经 gateway 执行，工作目录是所请求宿主目录在沙箱里对应的路径。
命令超时时由 OpenShell 终止，Raven 报告退出码 `-1` 和 `Command timed out after Ns`，
与宿主机执行器的结果一致。取消一轮对话时，会取消执行该命令的 gateway 调用。

### 3.4 MCP stdio 服务 { #34-mcp-stdio-servers }

启用沙箱后，stdio MCP 服务在沙箱内启动，而不是在宿主机上。它们的命令、参数、环境变量值和工作目录
都会从宿主路径翻译成沙箱路径；工作目录不在任何挂载点内时会被拒绝，不会被悄悄替换成别的目录。
Raven 经 gateway 桥接服务的 stdin 和 stdout，并把 stderr 转到日志：

```
WARNING  MCP server stderr [npx]: cannot find module '@scope/server'
```

非 JSON 的 stdout 行（例如 `npx -y ...` 启动时 npm 的进度输出）会被跳过。HTTP 和 SSE 类型的
MCP 服务直接连接，不受影响。

### 3.5 查看沙箱 { #35-inspecting-sandboxes }

Raven 的沙箱名为 `rv` 加十二位十六进制数字。用 OpenShell CLI 查看：

```bash
openshell sandbox list
openshell sandbox exec -n rv1a2b3c4d5e6f -- ls -la /workspace
openshell sandbox connect rv1a2b3c4d5e6f   # 交互式 shell
openshell logs rv1a2b3c4d5e6f              # supervisor 日志，包括被拒绝的连接
```

## 4. 高级配置 { #4-advanced-configuration }

### 4.1 网络策略 { #41-network-policy }

OpenShell 拒绝所有没有规则放行的出网，并按每个连接所访问的域名来判断。规则来自
`permissions.network`，也就是所有智能体共用的那套权限；规则怎么写、命令运行中被拦下的连接
怎么放行，见[权限：网络](permissions.md#network)。

默认值 `open` 放行所有三段及以上的域名（如 `api.github.com`、`files.pythonhosted.org`、
`www.example.com`）的全部端口，另外在 22、80、443、8080、8443、9418 端口放行一份常用的两段
域名：包仓库（`pypi.org`、`npmjs.org`、`crates.io`、`rubygems.org` 等）、代码托管（`github.com`、
`gitlab.com` 等）、容器镜像仓库（`docker.io`、`ghcr.io` 等）和模型 API。其他两段域名（例如
`example.com`）会被拦下，直到有规则或授权放行它。OpenShell 没有放行任意域名的规则
（NVIDIA/OpenShell#3856），这是它目前支持的最接近“全部放行”的设置。`closed` 只放行规则允许的。

这些规则转发 TLS 时不解密，所以出网速度不受影响。无论策略如何，OpenShell 始终禁止访问回环地址、
链路本地地址和云元数据地址 `169.254.169.254`。

Raven 自己的网页工具在宿主机上运行，不受此策略影响。策略作用于沙箱里的命令（`pip`、`npm`、
`git`、`curl`）和 stdio MCP 服务。

规则或授权会在运行中的沙箱执行下一条命令之前生效。网关默认用轮询下发策略改动，大约要 10 秒；
改用推送大约 1 秒：在 `~/.config/openshell/gateway.env` 里加上这一行，然后重启网关。

```
OPENSHELL_CONFIG_DELIVERY_MODE=push
```

`tools.sandbox.allowNet` 已停用。首次加载时会自动搬到 `permissions.network`：`true` 对应开放
默认，`false` 对应关闭，主机列表表示默认关闭、只在 80 和 443 端口放行这些主机，带 `"*"` 的列表
表示保持开放默认，再加上其余主机。

### 4.2 额外挂载 { #42-extra-volume-mounts }

```json
{
  "extraVolumes": [
    ["/data/datasets", "/datasets", "ro"],
    ["/home/me/cache", "/cache", "rw"]
  ]
}
```

两个路径都必须是绝对路径，gateway 也必须允许 bind mount（见 1.2 节）。宿主目录请放在 `/tmp` 之外：
打包版 Linux gateway 以 systemd `PrivateTmp` 运行，看不到宿主的 `/tmp`，从那里挂载会报
`bind source path does not exist`。

### 4.3 资源与超时 { #43-resources-and-timeouts }

```json
{ "cpus": 4, "memoryMib": 4096, "defaultTimeout": 300 }
```

`cpus` 和 `memoryMib` 会成为容器的资源上限。工具调用仍可传入自己的超时，该次调用以它为准，
覆盖 `defaultTimeout`。

## 5. 性能 { #5-performance }

在 Linux 6.8、Docker 29、OpenShell 0.1.8 上实测：

| 操作 | 开销 |
|-----------|------|
| 创建到就绪 | 约 2 秒；同时创建 8 个约 4 秒 |
| 命令往返 | 约 20 毫秒 |
| 删除 | gateway 上约 5 秒，对 Raven 来说在后台完成 |
| 每个空闲沙箱的内存 | 宿主机上约 31 MiB |
| 每次创建沙箱的 CPU | 约 1.5 CPU 秒 |

在沙箱里打开一个文件的开销约为普通容器的十倍，因为 OpenShell 要介入这次调用。
打开大量小文件的工作（例如安装依赖、对大目录执行 `git add`）会比在宿主机上慢好几倍。

## 6. 运行测试 { #6-how-to-run-tests }

单元测试既不需要 OpenShell，也不需要 gateway：

```bash
uv run --frozen --all-extras pytest tests/test_sandbox_unit.py \
  tests/test_sandbox_openshell_executor.py tests/test_sandbox_mounts.py -v
```

集成测试会驱动本机真实的 gateway。它需要一个带有 `sh`、`python3` 和 `curl` 的镜像，
以及一个位于 `/tmp` 之外的工作区根目录：

```bash
RAVEN_OPENSHELL_IT=1 \
RAVEN_OPENSHELL_IT_IMAGE=my-image:latest \
uv run --frozen --all-extras pytest tests/integration/test_openshell_sandbox_real_gateway.py -v -s
```

`RAVEN_OPENSHELL_IT_ROOT` 指定测试工作区的位置，`RAVEN_OPENSHELL_IT_HOST` 指定一个可访问的 HTTPS 主机，
`RAVEN_OPENSHELL_IT_UNLISTED_APEX` 指定一个不在默认清单里的两段域名。

## 7. 故障排查 { #7-troubleshooting }

| 现象 | 原因 | 处理 |
|---------|-------|-----|
| `No sandbox backend available` | Raven 环境里没有 SDK | 运行 1.1 节的安装命令 |
| `Cannot create an OpenShell sandbox` | 没有 gateway 响应 | `openshell status`，重启 gateway |
| `did not become ready: ... bind source path does not exist` | 挂载源在 `/tmp` 下，或 bind mount 未开启 | 把工作区移出 `/tmp`；检查 `gateway.toml` |
| 启动时报 `ConfigurationInvalid` | gateway 拒绝了策略 | `openshell logs <名称>` |
| `cannot write the workspace` | root 宿主，且沙箱用户不拥有工作区 | 以非 root 用户运行 Raven，或把工作区交给该 UID |
| 几毫秒内就报 `curl: (7) ... Couldn't connect` | 网络策略拦下了该主机 | Raven 询问时放行，或在 `permissions.network` 里加一条规则 |

## 8. 平台要求 { #8-platform-requirements }

| 平台 | 支持情况 |
|----------|---------|
| Linux x86_64 / arm64 | 内核 6.2 或更新，且启用 Landlock ABI 3；Docker 28 或更新 |
| macOS Apple Silicon | Docker Desktop，或按 1.2 节设置的 colima |
| Windows | 不支持；在 Windows 上 `sandbox` 可选依赖不会安装任何东西 |

在 Linux 主机上检查：

```bash
uname -r
cat /sys/kernel/security/lsm   # 必须包含 landlock
python3 -c "import ctypes;print(ctypes.CDLL(None,use_errno=True).syscall(444,None,0,1))"   # Landlock ABI，需 >= 3
docker version --format '{{.Server.Version}}'
```

容器沿用宿主机的内核，所以运行在旧内核宿主上的容器里的 Raven 同样用不了沙箱。gateway 必须和 Raven
运行在同一台机器上，因为工作区是按宿主路径 bind mount 的。
