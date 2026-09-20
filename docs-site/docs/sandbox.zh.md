# 沙箱

启用沙箱后，Raven 在 **Boxlite 微虚拟机**中执行 Shell 命令和 stdio MCP 服务进程。
虚拟机具有独立内核、资源限制和网络策略。挂载目录仍按配置的权限开放，其中包括共享工作区。

---

## 1. 安装 { #1-installation }

沙箱后端（`boxlite`）是可选依赖。请在 Raven 源码仓库中安装：

```bash
uv sync --extra sandbox
```

后端设为 `"auto"` 或 `"boxlite"` 时，缺少依赖会抛出 `SandboxInitError`，不会静默回退到宿主机
执行。只有后端设为 `"none"` 或未提供沙箱配置时，才使用 `DirectExecutor`。

`pyproject.toml` 中的 `sandbox` 可选依赖固定为 `boxlite==0.9.5`。请使用固定版本，以保持
后端与 Raven 执行器实现兼容。

---

## 2. 配置 { #2-configuration }

在 `config.json` 的 `tools` 下添加 `sandbox` 配置块：

```json
{
  "tools": {
    "sandbox": {
      "backend": "auto"
    }
  }
}
```

默认后端为 `"none"`。如需使用微虚拟机，请启用 `"auto"` 或 `"boxlite"`。

### 完整参考 { #full-reference }

| 键 | 类型 | 默认值 | 说明 |
|-----|------|---------|-------------|
| `backend` | `"none" \| "auto" \| "boxlite"` | `"none"` | `"none"` 在宿主机上执行命令。`"auto"` 和 `"boxlite"` 当前均选择 Boxlite；初始化失败时抛出 `SandboxInitError`。 |
| `image` | `str` | `"ubuntu:22.04"` | 用作微虚拟机根文件系统的 OCI 镜像。 |
| `cpus` | `int` | `2` | 分配给虚拟机的 vCPU 数量。 |
| `memory_mib` | `int` | `2048` | 内存大小，单位 MiB。 |
| `disk_size_gb` | `int \| null` | `null` | 磁盘大小，单位 GB。`null` 表示临时磁盘（boxlite 默认，无持久化磁盘）。 |
| `allow_net` | `bool \| list[str]` | `true` | `true` 表示不受限；`false` 表示无网络；`["pypi.org", ...]` 表示域名白名单。空列表 `[]` 会在配置校验阶段被拒绝——要完全禁用网络请使用 `false`。 |
| `extra_volumes` | `list[[host, vm, mode]]` | `[]` | 额外挂载进虚拟机的宿主路径。两侧路径都必须是绝对路径。`mode` 取 `"ro"` 或 `"rw"`。 |
| `default_timeout` | `int` | `120` | 未显式传入超时时，单次 `exec()` 的超时秒数。 |
| `verify_timeout` | `int` | `30` | 启动时用 `echo ok` 探测虚拟机是否响应的超时秒数。 |
| `create_timeout` | `int` | `300` | 拉取镜像与创建虚拟机的超时秒数。镜像较大或镜像源较慢时调大；镜像总是预先拉取时可调小。 |
| `debug` | `object` | 禁用 | 将 `debug.enabled` 设为 `true`，启用 `raven sandbox` 使用的本地沙箱检查服务。 |

### 常用预设 { #common-presets }

**自动选择后端：** 当前选择 Boxlite，并在启动时验证虚拟机。后端不可用时初始化失败：

```json
{
  "tools": {
    "sandbox": { "backend": "auto" }
  }
}
```

**显式选择 Boxlite：** 指定 Boxlite 后端，并执行相同的启动检查：

```json
{
  "tools": {
    "sandbox": { "backend": "boxlite" }
  }
}
```

**自定义镜像、资源限制和网络白名单：**

```json
{
  "tools": {
    "sandbox": {
      "backend": "boxlite",
      "image": "python:3.11-slim",
      "cpus": 4,
      "memory_mib": 4096,
      "disk_size_gb": 20,
      "allow_net": ["pypi.org", "files.pythonhosted.org", "api.github.com"],
      "default_timeout": 120
    }
  }
}
```

**禁用工作虚拟机的网络访问：**

```json
{
  "tools": {
    "sandbox": {
      "backend": "boxlite",
      "allow_net": false
    }
  }
}
```

**禁用（默认）：**

```json
{
  "tools": {
    "sandbox": { "backend": "none" }
  }
}
```

---

## 3. 工作原理 { #3-how-it-works }

`SandboxExecutor` 定义在 `raven/sandbox/interfaces.py` 中，为两个执行后端提供统一接口：

| 实现 | 何时使用 | 隔离性 |
|----------------|-----------|-----------|
| `BoxliteExecutor` | `backend = "auto"` 或 `"boxlite"` | boxlite 微虚拟机——独立内核，资源受限 |
| `DirectExecutor` | `backend = "none"` | 无——在宿主机上调用 `asyncio.create_subprocess_shell()` |

工作区以可读写方式挂载到虚拟机的 `/workspace`。`ReadFileTool`、`WriteFileTool` 等文件系统
工具直接操作宿主机路径；虚拟机内读写 `/workspace/foo.py` 时，访问的是同一个文件。

`AgentLoop.__init__` 同步创建执行器对象，此时不会启动虚拟机。运行时启动或处理轮次前，
`_start_executor()` 调用 `BoxliteExecutor.start()` 创建并验证工作虚拟机：

- **`allow_net=True`（默认）：** 直接创建工作虚拟机；如果镜像尚未缓存，Boxlite 会先下载镜像。
- **`allow_net=False` 或域名列表：** 先用网络不受限的临时虚拟机拉取并缓存镜像，再按配置的
  网络限制创建工作虚拟机。

网络限制作用于工作虚拟机，镜像准备阶段仍需访问镜像仓库。后端不可用或平台不受支持时，
会在沙箱任务执行前抛出 `SandboxInitError`。

---

## 4. 直接使用 `SandboxExecutor` { #4-using-sandboxexecutor-directly }

### 4.1 `BoxliteExecutor` { #41-boxliteexecutor }

```python
import asyncio
from pathlib import Path
from raven.config.paths import get_sandbox_dir
from raven.sandbox import build_executor, SandboxConfig

async def main():
    sandbox_cfg = SandboxConfig(
        backend="boxlite",
        image="ubuntu:22.04",
        cpus=2,
        memory_mib=2048,
    )
    workspace = Path("/tmp/my-workspace")
    workspace.mkdir(exist_ok=True)

    # build_executor() returns BoxliteExecutor for backend="auto"/"boxlite"
    # __aenter__ creates and verifies the working VM; raises SandboxInitError if unavailable
    # sandbox_dir tells the sandbox where a backend keeps its state. It is the
    # resolver, not a path: resolving one creates the directory, and this call
    # only reaches it when the backend is not "none".
    async with build_executor(sandbox_cfg, workspace, sandbox_dir=get_sandbox_dir) as executor:
        result = await executor.exec("echo hello from the VM")
        print(result.as_text())
        # → "hello from the VM\n\nExit code: 0"

        result = await executor.exec("python3 --version")
        print(result.as_text())

        # cwd is translated from host path → /workspace/... automatically
        result = await executor.exec("ls -la", cwd=str(workspace))
        print(result.as_text())

asyncio.run(main())
```

`exec()` 返回一个 `ExecResult` 数据类：

```python
@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int

    def as_text(self, max_chars: int = 10_000) -> str: ...
```

`as_text()` 将 stdout、非空的 `STDERR:` 块和 `Exit code: N` 行合并为一个字符串。退出码始终
保留。输出超过 `max_chars` 时，中间内容会被截断，并用 `... (N chars truncated) ...` 标记省略部分。

**生命周期——显式 start/stop：**

```python
from raven.config.paths import get_sandbox_dir
from raven.sandbox import SandboxInitError
from raven.sandbox.boxlite_executor import BoxliteExecutor

executor = BoxliteExecutor(
    image="ubuntu:22.04",
    workspace=Path("/tmp/ws"),
    sandbox_home=get_sandbox_dir("boxlite"),
)

try:
    await executor.start()   # creates and verifies the working VM
except SandboxInitError as e:
    print(f"Sandbox unavailable: {e}")
    raise SystemExit(1)

result = await executor.exec("uname -r")
await executor.stop()    # tears down the working VM
```

**生命周期——上下文管理器（推荐）：**

```python
from raven.sandbox import SandboxInitError

try:
    async with BoxliteExecutor(
        image="ubuntu:22.04", workspace=Path("/tmp/ws"), sandbox_home=get_sandbox_dir("boxlite")
    ) as executor:
        result = await executor.exec("uname -r")
        print(result.stdout)
except SandboxInitError as e:
    print(f"Sandbox unavailable: {e}")
```

**超时：**

```python
# Uses default_timeout (120 s) when timeout=None
result = await executor.exec("sleep 10", timeout=5)
print(result.exit_code)   # -1
print(result.stderr)      # "Command timed out after 5s"
```

**环境变量：**

```python
result = await executor.exec(
    "echo $MY_VAR",
    env={"MY_VAR": "hello"},
)
print(result.stdout)  # "hello\n"
```

### 4.2 `DirectExecutor` { #42-directexecutor }

`DirectExecutor` 实现同一套 `SandboxExecutor` 接口，但直接在宿主机上执行命令。适用于测试，或明确禁用沙箱的场景。

```python
from raven.sandbox.direct_executor import DirectExecutor

async with DirectExecutor() as executor:
    result = await executor.exec("pwd")
    print(result.as_text())
```

当 `backend` 为 `"none"` 或 `sandbox_cfg` 为 `None` 时，`build_executor()` 返回 `DirectExecutor`：

```python
from raven.config.paths import get_sandbox_dir
from raven.sandbox import build_executor, SandboxConfig

# No config — returns DirectExecutor
executor = build_executor(None, workspace, sandbox_dir=get_sandbox_dir)

# Explicit "none" — also returns DirectExecutor (no probe, no VM).
# sandbox_dir is never called on this path, so no boxlite home is created.
executor = build_executor(SandboxConfig(backend="none"), workspace, sandbox_dir=get_sandbox_dir)
```

---

## 5. 向 `ExecTool` 注入执行器 { #5-injecting-an-executor-into-exectool }

通过可选参数 `executor` 传入执行器。省略该参数时，`ExecTool` 会创建 `DirectExecutor`，
在宿主机上执行命令。

```python
from pathlib import Path
from raven.config.paths import get_sandbox_dir
from raven.sandbox import build_executor, SandboxConfig
from raven.agent.tools.shell import ExecTool

sandbox_cfg = SandboxConfig(backend="boxlite")
workspace = Path("/tmp/ws")

executor = build_executor(sandbox_cfg, workspace, sandbox_dir=get_sandbox_dir)
await executor.start()   # creates and verifies the working VM; raises SandboxInitError if unavailable

tool = ExecTool(
    working_dir=str(workspace),
    timeout=60,
    executor=executor,          # inject the sandboxed executor
)

# ExecTool.execute() returns a plain string (the formatted output)
output = await tool.execute(command="python3 -c 'print(42)'")
print(output)
# → "42\n\nExit code: 0"

await executor.stop()
```

沙箱不会放宽命令权限。工具注册表在向任一后端派发命令前，都会应用相同的拒绝和审批规则。
由于 `/workspace` 是可读写挂载，在其中删除文件也会删除宿主机上的对应文件。

`ExecTool` 还会执行自身的命令和工作区限制。`restrict_to_workspace` 对两个后端均生效：
引用工作区之外路径的命令会被拒绝并记录。

---

## 6. 接入 `AgentLoop` { #6-wiring-into-agentloop }

创建 `AgentLoop` 时传入 `SandboxConfig`。执行器对象会立即创建，虚拟机则在运行时启动或
轮次处理需要时启动：

```python
from raven.agent.loop import AgentLoop
from raven.sandbox import SandboxConfig

loop = AgentLoop(
    provider=provider,
    workspace=workspace,
    sandbox_config=SandboxConfig(
        backend="boxlite",
        image="python:3.11-slim",
        cpus=2,
        memory_mib=2048,
    ),
)

# run() calls _start_executor() before _connect_mcp(), unconditionally
await loop.run()
```

**`run_turn()`（spine 的轮次入口——CLI / 定时任务 / 消息渠道）：**

```python
from raven.spine import ChatType, Origin, Source, TurnRequest

async def emit(event):  # receives StreamDelta / Text / ToolEvent / ... events
    ...

await loop.run_turn(
    TurnRequest(
        origin=Origin.USER,
        source=Source(channel="cli", chat_id="sandbox-demo", sender_id="user", chat_type=ChatType.DM),
        text="write a hello-world Python script and run it",
        conversation="cli:sandbox-demo",
    ),
    emit,
    lambda: [],  # drain (INJECT/INTERRUPT) — empty for a plain turn
    stream=False,
)
```

执行器的生命周期由 `AgentLoop` 管理：

| 方法 | 何时调用 | 做什么 |
|--------|-------------|-------------|
| `_start_executor()` | `run_turn()` 入口 | 通过 `executor.start()` 创建并校验工作虚拟机；幂等——第二次调用无副作用；失败时抛出 `SandboxInitError`（spine 会将其转为 TurnFailed 事件） |
| `close_executor()` | 关闭时（`close_mcp()` 也会调用） | 依次执行 `_executor_stack.aclose()` → `executor.stop()` → 取消桥接任务 → 清理虚拟机 |

**清理资源：**

```python
await loop.close_mcp()   # closes MCP connections and the sandbox executor together
```

`close_mcp()` 也会调用 `close_executor()`。由于 stdio MCP 服务进程运行在虚拟机内，必须先
关闭 MCP 连接，再停止虚拟机。清理完成后重复调用 `close_executor()` 不会产生额外影响。

**MCP stdio 服务：**

当 `sandbox.backend` 为 `"auto"` 或 `"boxlite"` 时，stdio MCP 服务在**虚拟机内**启动，而非宿主机。三个 asyncio 桥接任务负责在 boxlite 的流式执行 API 与 `ClientSession` 期望的 `anyio` `MemoryObjectStream` 之间转换：

- `_stdout_bridge`——读取虚拟机 stdout 分片，缓冲至 `\n`，解析 JSON-RPC，包装为 `SessionMessage`，转发到读取流
- `_stdin_bridge`——从写入流接收 `SessionMessage`，取出内层 `JSONRPCMessage`，序列化为 JSON 加换行，写入虚拟机 stdin
- `_stderr_bridge`——读取虚拟机 stderr，以 `WARNING` 级别转发到应用日志

诊断 MCP 服务启动失败时，stderr 是首要信号：

```
WARNING  MCP server stderr [npx]: cannot find module '@scope/server'
WARNING  MCP server stderr [npx]: Error: ENOENT ...
```

stdout 中的非 JSON 行（例如 `npx -y ...` 启动时的 npm 进度信息）会以 DEBUG 级别记录并跳过，
不会中断 `ClientSession`。HTTP/SSE MCP 服务使用远程连接，不经过这一进程桥接机制。

---

## 7. 接入 `SubagentManager` { #7-wiring-into-subagentmanager }

启用沙箱后，每个子 Agent 都在独立的虚拟机中运行，不与父 Agent 共用虚拟机。

```python
from raven.agent.subagent import SubagentManager
from raven.sandbox import SandboxConfig

manager = SubagentManager(
    provider=provider,
    workspace=workspace,
    sandbox_config=SandboxConfig(backend="boxlite"),
)

# spawn() returns immediately; the sub-agent runs in the background
handle = await manager.spawn(task="run the test suite and report failures")
```

`_run_subagent()` 为子 Agent 的工作区创建执行器，并在 `async with executor:` 中运行任务。
虚拟机随任务启动，并在任务结束时清理，包括任务失败的情况。

每个虚拟机都需要独立启动。预先拉取镜像可以避免多个子 Agent 并发启动时重复下载，
但创建虚拟机本身仍有开销。

`AgentLoop` 会将自身的 `sandbox_config` 传给 `SubagentManager`，使子 Agent 继承相同配置：

```python
# In AgentLoop.__init__ (simplified)
self.subagents = SubagentManager(
    ...,
    sandbox_config=sandbox_config,   # same config, isolated VM per sub-agent
)
```

---

## 8. 进阶配置 { #8-advanced-configuration }

### 8.1 网络策略 { #81-network-policy }

```python
from raven.sandbox import SandboxConfig

# Full network access (default)
SandboxConfig(backend="boxlite", allow_net=True)

# No network
SandboxConfig(backend="boxlite", allow_net=False)

# Domain allowlist — only these hosts are reachable from inside the VM
SandboxConfig(
    backend="boxlite",
    allow_net=["pypi.org", "files.pythonhosted.org", "api.github.com"],
)
```

> **注意：** `allow_net=[]`（空列表）会在配置校验阶段以 `ValueError` 被拒绝。要完全禁用网络请使用 `allow_net=False`。

当 `allow_net` 为域名列表或 `False` 时，`BoxliteExecutor.start()` 会先用网络不受限的临时
虚拟机准备 OCI 镜像，再按受限策略创建工作虚拟机。因此，启动工作虚拟机前会增加镜像准备步骤。

### 8.2 额外的卷挂载 { #82-extra-volume-mounts }

把额外的宿主路径挂载进虚拟机，作为只读数据或可读写的临时空间。宿主路径与虚拟机路径都必须是**绝对路径**。

```python
SandboxConfig(
    backend="boxlite",
    extra_volumes=[
        # [host_path, vm_path, mode]
        ["/Users/alice/datasets",  "/data",    "ro"],   # read-only dataset
        ["/tmp/sandbox-cache",     "/cache",   "rw"],   # writable scratch
    ],
)
```

等价的 JSON 写法：

```json
{
  "tools": {
    "sandbox": {
      "backend": "boxlite",
      "extra_volumes": [
        ["/Users/alice/datasets", "/data", "ro"],
        ["/tmp/sandbox-cache",    "/cache", "rw"]
      ]
    }
  }
}
```

### 8.3 资源限制 { #83-resource-limits }

```python
SandboxConfig(
    backend="boxlite",
    cpus=4,
    memory_mib=8192,
    disk_size_gb=50,        # omit or set null for ephemeral disk (default)
    default_timeout=300,    # 5-minute default per command
    create_timeout=600,     # increase for large images or slow registries
)
```

单次调用的超时会覆盖默认值：

```python
# This call gets 10 s regardless of default_timeout
result = await executor.exec("sleep 20", timeout=10)
```

---

## 9. 如何运行测试 { #9-how-to-run-tests }

### 9.1 前置条件 { #91-prerequisites }

| 要求 | 说明 |
|-------------|-------|
| Python 3.12+ | 运行 `python3 --version` 检查版本 |
| `uv` | 项目使用的包管理器；安装命令为 `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| boxlite（仅集成测试需要） | macOS Apple Silicon M1+ **或** 带 `/dev/kvm` 的 Linux x86_64/ARM64 |
| Node.js / npx（仅 MCP 往返测试需要） | 由 `node:20-slim` OCI 镜像提供——本地无需安装 Node |

单元测试通过模拟 Boxlite 运行，无需启动虚拟机或访问 KVM。请使用项目要求的 Python 3.12
或更高版本。

---

### 9.2 准备虚拟环境 { #92-set-up-the-virtual-environment }

项目使用 `uv` 管理依赖。

```bash
# Clone and enter the repo
git clone <repo-url>
cd raven

# Create the virtual environment and install all project dependencies
uv sync
```

`uv sync` 会创建项目环境并安装锁定的依赖。下方命令均通过 `uv run` 运行，无需手动激活环境。

---

### 9.3 安装依赖 { #93-install-dependencies }

**单元测试：** 安装核心依赖和开发工具：

```bash
# Core project and development tools
uv sync
```

MCP 已是核心依赖，无需单独添加。

**集成测试**——还需要 sandbox 可选组件：

```bash
# Install the pinned Boxlite backend
uv sync --extra sandbox
```

验证安装结果：

```bash
# Sandbox package should import cleanly
uv run python -c "from raven.sandbox import build_executor, SandboxConfig; print('sandbox ok')"

# boxlite binary should be available (integration tests only)
uv run python -c "import boxlite; print('boxlite ok')"
```

---

### 9.4 运行单元测试 { #94-run-unit-tests }

单元测试覆盖 `SandboxConfig`、`DirectExecutor`、模拟的 `BoxliteExecutor`、`ExecTool` 的限制、
`AgentLoop` 执行器生命周期和 MCP 桥接任务，无需虚拟机或 KVM。

```bash
uv run python -m pytest tests/test_sandbox_unit.py -v
```

通过和跳过的测试数量以 pytest 当前运行的汇总为准。测试数量和耗时会随代码版本与运行环境变化。

**常用参数：**

```bash
# Stop on first failure
uv run python -m pytest tests/test_sandbox_unit.py -x

# Run a specific test class
uv run python -m pytest tests/test_sandbox_unit.py::TestSandboxConfigValidators -v

# Show log output (useful for DEBUG-level bridge tracing)
uv run python -m pytest tests/test_sandbox_unit.py -v -s

# Filter by test name substring
uv run python -m pytest tests/test_sandbox_unit.py -k "translate_cwd"
```

---

### 9.5 运行集成测试 { #95-run-integration-tests }

集成测试会启动真实的 Boxlite 虚拟机，需要：

- 通过 `uv sync --extra sandbox` 安装沙箱可选依赖
- macOS Apple Silicon M1+ **或** 可访问 `/dev/kvm` 的 Linux

Linux 环境中若不存在 `/dev/kvm`，pytest 会跳过整个测试模块。

**首次运行——预拉取 OCI 镜像：**

`test_sandbox_real_vm.py` 使用会话级 fixture，在首个测试前准备 `ubuntu:22.04` 和
`node:20-slim` 镜像。Boxlite 会缓存已下载的镜像供后续运行使用。准备时间取决于镜像仓库、
网络和本地缓存。

如果镜像拉取失败，pytest 会跳过测试并报告原因：

```
SKIPPED  OCI image pull failed for 'ubuntu:22.04' — likely a network issue, not a code bug.
         Fix: check connectivity or pre-pull manually: boxlite pull ubuntu:22.04
```

**运行全部集成测试：**

```bash
uv run python -m pytest tests/integration/test_sandbox_real_vm.py -v
```

MCP 往返测试（`test_npx_mcp_server_everything`）每次运行都会在 `node:20-slim` 虚拟机内，
通过 `npm install -g` 安装 `@modelcontextprotocol/server-everything`，随后启动 MCP 服务，
验证完整的 `initialize` 和 `list_tools` 流程。

**同时运行单元测试与集成测试：**

```bash
uv run python -m pytest tests/test_sandbox_unit.py tests/integration/test_sandbox_real_vm.py -v
```

**运行项目完整测试套件**（全部测试文件，不含集成测试）：

```bash
uv run python -m pytest tests/ --ignore=tests/integration/test_sandbox_real_vm.py -q
```

---

### 9.6 运行单个测试 { #96-run-a-single-test }

```bash
# A single test case by full name
uv run python -m pytest "tests/test_sandbox_unit.py::TestBoxliteTranslateCwd::test_subdir_translates_correctly" -v

# A single integration test
uv run python -m pytest "tests/integration/test_sandbox_real_vm.py::TestBoxliteStdioMCPRoundtrip::test_npx_mcp_server_everything" -v -s
```

---

### 9.7 排障 { #97-troubleshooting }

**`ModuleNotFoundError: No module named 'boxlite'`**

```bash
uv sync --extra sandbox
```

**`ModuleNotFoundError: No module named 'mcp'`**

在项目环境中重新同步核心依赖：

```bash
uv sync
```

**`PanicException: Another BoxliteRuntime is already using directory`**

已有 Boxlite 运行时占用了相同的状态目录。先列出候选进程，再停止占用该目录的那一个：

```bash
pgrep -af boxlite   # 带完整命令行，便于辨认占用者
kill <pid>          # 等待两秒后重试
```

请按 pid 结束进程，不要按模式匹配：能匹配到运行时的模式，同样会匹配到你输入该命令的那个
shell。如需同时运行多个实例，请为它们配置独立的沙箱状态目录。

**集成测试在 Linux 上被跳过**

检查 `/dev/kvm` 是否存在且可访问：

```bash
ls -la /dev/kvm
```

如果 KVM 不可用或当前账号没有访问权限，请联系机器管理员启用虚拟化，并为账号配置所需的
设备访问权限。

**镜像拉取超时（超过 `create_timeout`）**

`create_timeout` 默认为 300 秒。网络较慢时可调大，或手动预拉取镜像：

```bash
# Pre-pull via a one-off Python script
uv run python -c "
import asyncio, boxlite
async def pull(img):
    async with boxlite.SimpleBox(image=img, cpus=1, memory_mib=256): pass
for img in ['ubuntu:22.04', 'node:20-slim']:
    print(f'Pulling {img}...')
    asyncio.run(pull(img))
    print(f'  done')
"
```

---

## 10. 平台要求 { #10-platform-requirements }

| 平台 | 要求 |
|----------|------------|
| macOS | Apple Silicon M1+、macOS 12+（使用 `Hypervisor.framework`） |
| Linux | x86_64 或 ARM64，启用 KVM（当前用户可访问 `/dev/kvm`） |
| Windows | 启用 KVM 的 x86_64 WSL2（当前用户可访问 `/dev/kvm`） |

在不受支持的平台上，`sandbox.backend = "none"` 会使用 `DirectExecutor`。
此时命令直接在宿主机上执行，不具备虚拟机隔离。

验证 boxlite 已安装且 sandbox 包可导入：

```bash
uv run python -c "import boxlite; print('boxlite ok')"
uv run python -c "from raven.sandbox import build_executor, SandboxConfig; print('sandbox ok')"
```

端到端验证（需要 KVM / Apple Silicon）：

```bash
uv run python -m pytest tests/integration/test_sandbox_real_vm.py -v
```
