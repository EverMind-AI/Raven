# Sandbox

With sandboxing enabled, shell commands and stdio MCP server processes dispatched
through `BoxliteExecutor` run in a **Boxlite microVM**. The VM has its own kernel,
resource limits, and network policy. Mounted directories remain accessible
according to their configured permissions, including the shared workspace.

## 1. Installation

The sandbox backend (`boxlite`) is an optional dependency. Install it from your
Raven source checkout:

```bash
uv sync --extra sandbox
```

When the backend is `"auto"` or `"boxlite"`, a missing dependency raises
`SandboxInitError`. Raven does not silently fall back to host execution.
`DirectExecutor` is used only when the backend is `"none"` or no sandbox configuration
is supplied.

The `sandbox` extra pins `boxlite==0.9.5` in `pyproject.toml`. Use the pinned
version to keep the backend compatible with Raven's executor implementation.

## 2. Configuration

Add a `sandbox` block under `tools` in `config.json`. The loader accepts JSON,
not YAML. Invalid JSON produces a warning and falls back to defaults, including
`tools.sandbox.backend = "none"`, so check startup warnings before relying on
sandbox isolation:

```json
{
  "tools": {
    "sandbox": {
      "backend": "auto"
    }
  }
}
```

The default backend is `"none"`. Enable `"auto"` or `"boxlite"` to use a microVM.

### Full reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | `"none" \| "auto" \| "boxlite"` | `"none"` | `"none"` runs commands on the host. Both `"auto"` and `"boxlite"` currently select Boxlite and raise `SandboxInitError` if it cannot be initialized. |
| `image` | `str` | `"ubuntu:22.04"` | OCI image used for the microVM root filesystem. |
| `cpus` | `int` | `2` | vCPU count allocated to the VM. |
| `memory_mib` | `int` | `2048` | RAM in MiB. |
| `disk_size_gb` | `int \| null` | `null` | Disk size in GB. `null` = ephemeral (boxlite default, no persistent disk). |
| `allow_net` | `bool \| list[str]` | `true` | `true` = unrestricted; `false` = no network; `["pypi.org", ...]` = domain allowlist. An empty list `[]` is rejected at config validation time — use `false` to disable networking entirely. |
| `extra_volumes` | `list[[host, vm, mode]]` | `[]` | Additional host paths to mount into the VM. Both paths must be absolute. `mode` is `"ro"` or `"rw"`. |
| `default_timeout` | `int` | `120` | Per-`exec()` timeout in seconds when no explicit timeout is passed. |
| `verify_timeout` | `int` | `30` | Timeout in seconds for the startup `echo ok` probe that confirms the VM is responsive. |
| `create_timeout` | `int` | `300` | Timeout in seconds for image pull + VM creation. Increase for large images or slow registries; decrease if images are always pre-pulled. |
| `debug` | `object` | Disabled | Set `debug.enabled` to `true` to enable the local sandbox inspection service used by `raven sandbox`. Ignored when `backend` is `"none"`. |

### Common presets

**Automatic backend selection:** currently selects Boxlite and verifies the VM
at startup. Initialization fails if the backend is unavailable:

```json
{
  "tools": {
    "sandbox": { "backend": "auto" }
  }
}
```

**Explicit Boxlite backend:** selects Boxlite with the same startup checks:

```json
{
  "tools": {
    "sandbox": { "backend": "boxlite" }
  }
}
```

**Custom image, resource limits, and a network allowlist:**

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

**Disable network access in the working VM:**

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

**Disabled (default):**

```json
{
  "tools": {
    "sandbox": { "backend": "none" }
  }
}
```

## 3. How It Works

`SandboxExecutor`, defined in `raven/sandbox/interfaces.py`, provides a shared
interface for two execution backends:

| Implementation | When used | Isolation |
|----------------|-----------|-----------|
| `BoxliteExecutor` | `backend = "auto"` or `"boxlite"` | boxlite microVM — separate kernel, capped resources |
| `DirectExecutor` | `backend = "none"` | None — `asyncio.create_subprocess_shell()` on the host |

The workspace is mounted at `/workspace` with read-write access. Filesystem
tools such as `ReadFileTool` and `WriteFileTool` use the host path directly.
Reading or writing `/workspace/foo.py` inside the VM accesses the same file.

`AgentLoop.__init__` creates the executor object synchronously without starting
a VM. At runtime startup or before a turn, `_start_executor()` calls
`BoxliteExecutor.start()` to create and verify the working VM:

- **`allow_net=True` (default):** Boxlite creates the working VM directly and
  downloads the image if it is not already cached.
- **`allow_net=False` or a domain list:** a temporary VM with unrestricted
  networking first pulls and caches the image. Raven then creates the working
  VM with the configured network restrictions.

Network restrictions apply to the working VM; image preparation still needs
registry access. An unavailable backend or unsupported platform raises
`SandboxInitError` before sandboxed work can run.

## 4. Using `SandboxExecutor` Directly

### 4.1 `BoxliteExecutor`

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

`exec()` returns an `ExecResult` dataclass:

```python
@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int

    def as_text(self, max_chars: int = 10_000) -> str: ...
```

`as_text()` combines stdout, a `STDERR:` block when stderr contains non-whitespace
characters, and an `Exit code: N` line into a single string before truncation.
Output longer than `max_chars` is truncated in the middle, with a
`... (N chars truncated) ...` marker indicating the omitted content. Very small
limits can truncate the exit-code line itself; read `result.exit_code` directly
when the complete exit code is required independently of the formatted output.

**Lifecycle — explicit start/stop:**

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

**Lifecycle — context manager (recommended):**

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

**Timeout:**

```python
# Uses default_timeout (120 s) when timeout=None
result = await executor.exec("sleep 10", timeout=5)
print(result.exit_code)   # -1
print(result.stderr)      # "Command timed out after 5s"
```

**Environment variables:**

```python
result = await executor.exec(
    "echo $MY_VAR",
    env={"MY_VAR": "hello"},
)
print(result.stdout)  # "hello\n"
```

### 4.2 `DirectExecutor`

`DirectExecutor` implements the same `SandboxExecutor` interface but runs commands directly on the host. Use it in tests or when sandboxing is explicitly disabled.

```python
from raven.sandbox.direct_executor import DirectExecutor

async with DirectExecutor() as executor:
    result = await executor.exec("pwd")
    print(result.as_text())
```

`build_executor()` returns a `DirectExecutor` when `backend` is `"none"` or when `sandbox_cfg` is `None`:

```python
from raven.config.paths import get_sandbox_dir
from raven.sandbox import build_executor, SandboxConfig

# No config — returns DirectExecutor
executor = build_executor(None, workspace, sandbox_dir=get_sandbox_dir)

# Explicit "none" — also returns DirectExecutor (no probe, no VM).
# sandbox_dir is never called on this path, so no boxlite home is created.
executor = build_executor(SandboxConfig(backend="none"), workspace, sandbox_dir=get_sandbox_dir)
```

## 5. Injecting an Executor into `ExecTool`

Pass an executor through the optional `executor` parameter. If omitted,
`ExecTool` creates a `DirectExecutor` and runs commands on the host.

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

Sandboxing does not relax command permissions. The tool registry applies the
same denial and approval rules before dispatching commands to either backend.
Because `/workspace` is a read-write mount, deleting a file there also deletes
it on the host.

`ExecTool` enforces its own command and workspace restrictions.
`restrict_to_workspace` applies to both backends: commands that reference paths
outside the workspace are rejected and logged.

## 6. Wiring into `AgentLoop`

Pass `SandboxConfig` when constructing `AgentLoop`. The executor object is
created immediately; the VM starts when the runtime or a turn needs it:

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

**`run_turn()` (spine turn entry — CLI / cron / channels):**

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

The executor lifecycle is managed by `AgentLoop`:

| Method | When called | What it does |
|--------|-------------|-------------|
| `_start_executor()` | Entry of `run_turn()` | Creates and verifies the working VM via `executor.start()`; idempotent — second call is a no-op; raises `SandboxInitError` on failure (the spine turns it into a TurnFailed event) |
| `close_executor()` | Shutdown (also called by `close_mcp()`) | Calls `_executor_stack.aclose()` → `executor.stop()` → bridge tasks cancelled → VM torn down |

**Teardown:**

```python
await loop.close_mcp()   # closes MCP connections and the sandbox executor together
```

`close_mcp()` also calls `close_executor()`. MCP connections must close before
the VM stops because their stdio server processes run inside it. Repeating
`close_executor()` after cleanup has no effect.

**MCP stdio servers:**

When `sandbox.backend` is `"auto"` or `"boxlite"`, stdio MCP servers are launched
**inside the VM** rather than on the host. Raven creates two pairs of `anyio`
memory object streams and passes a receive stream and a send stream to
`ClientSession`. Three asyncio tasks bridge stdout and stdin to those streams
and forward stderr to the application log:

- `_stdout_bridge` — reads VM stdout chunks, buffers until `\n`, parses JSON-RPC, wraps in `SessionMessage`, forwards to read stream
- `_stdin_bridge` — receives `SessionMessage` from write stream, extracts the inner `JSONRPCMessage`, serialises to JSON + newline, writes to VM stdin
- `_stderr_bridge` — reads VM stderr and forwards to application log at `WARNING` level

Stderr is the primary signal for diagnosing MCP server startup failures:

```
WARNING  MCP server stderr [npx]: cannot find module '@scope/server'
WARNING  MCP server stderr [npx]: Error: ENOENT ...
```

Non-JSON stdout lines, such as npm progress messages during `npx -y ...`
startup, are logged at DEBUG level and skipped without interrupting
`ClientSession`. HTTP/SSE MCP servers use remote connections, so this process
bridge does not apply to them.

## 7. Wiring into `SubagentManager`

With sandboxing enabled, the built-in `raven-loop` subagent backend uses its
own sandbox executor for shell commands rather than sharing the parent agent's
VM. This does not place the entire subagent process or its host-side filesystem
tools inside the VM.

External ACP and CLI agents still launch as host processes. Passing a sandbox
executor to a backend does not sandbox that backend's own process or tools;
configure isolation in the external agent separately when required.

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

`_run_subagent()` creates an executor for the subagent's workspace and passes it
to the backend inside `async with executor:`. The VM starts before backend
execution and is cleaned up when the task finishes, including when it fails.
Only operations the backend sends through that executor run inside the VM.

Each VM starts independently. Pre-pulling images can avoid repeated downloads
when many subagents start concurrently; VM creation still has its own cost.

`AgentLoop` passes its `sandbox_config` to `SubagentManager`, so the executors
created for subagent tasks inherit the same configuration:

```python
# In AgentLoop.__init__ (simplified)
self.subagents = SubagentManager(
    ...,
    sandbox_config=sandbox_config,
)
```

## 8. Advanced Configuration

### 8.1 Network policy

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

> **Note:** `allow_net=[]` (empty list) is rejected at config validation time with a `ValueError`. Use `allow_net=False` to disable networking entirely.

When `allow_net` is a domain list or `False`, `BoxliteExecutor.start()` uses a
temporary VM with unrestricted networking to prepare the OCI image, then
creates the working VM with the restricted policy. This adds an image
preparation step before the working VM starts.

### 8.2 Extra volume mounts

Mount additional host paths into the VM as read-only data or read-write scratch space. Both the host path and the VM path must be **absolute**.

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

JSON equivalent:

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

### 8.3 Resource limits

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

Per-call timeout overrides the default:

```python
# This call gets 10 s regardless of default_timeout
result = await executor.exec("sleep 20", timeout=10)
```

## 9. How to Run Tests

### 9.1 Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.12+ | Check with `python3 --version` |
| `uv` | Project package manager; install with `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| boxlite (integration tests only) | macOS Apple Silicon M1+ **or** Linux x86_64/ARM64 with `/dev/kvm` |
| Node.js / npx (MCP roundtrip test only) | Provided by the `node:20-slim` OCI image — no local Node required |

Unit tests mock Boxlite and do not require a running VM or KVM. Use Python 3.12
or newer, as required by the project.

### 9.2 Set up the virtual environment

The project uses `uv` for dependency management.

```bash
# Clone and enter the repo
git clone <repo-url>
cd raven

# Create the virtual environment and install all project dependencies
uv sync
```

`uv sync` creates the project environment and installs the locked dependencies.
Use `uv run` for the commands below; manual activation is not required.

### 9.3 Install dependencies

**Unit tests:** install the core dependencies and development tools:

```bash
# Core project and development tools
uv sync
```

MCP is already a core dependency and does not need to be added separately.

**Integration tests** — additionally require the sandbox optional extra:

```bash
# Install the pinned Boxlite backend
uv sync --extra sandbox
```

Verify the installation:

```bash
# Sandbox package should import cleanly
uv run python -c "from raven.sandbox import build_executor, SandboxConfig; print('sandbox ok')"

# boxlite binary should be available (integration tests only)
uv run python -c "import boxlite; print('boxlite ok')"
```

### 9.4 Run unit tests

Unit tests cover `SandboxConfig`, `DirectExecutor`, a mocked `BoxliteExecutor`,
`ExecTool` restrictions, the `AgentLoop` executor lifecycle, and MCP bridge
tasks. They require no VM or KVM access.

```bash
uv run python -m pytest tests/test_sandbox_unit.py -v
```

Use pytest's summary for the current pass and skip counts. Test counts and
runtime depend on the revision and environment.

**Useful flags:**

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

### 9.5 Run integration tests

Integration tests start real Boxlite VMs. They require:

- The sandbox extra, installed with `uv sync --extra sandbox`
- macOS Apple Silicon M1+ **or** Linux with `/dev/kvm` accessible

On Linux without `/dev/kvm`, pytest skips the entire module.

**First run — pre-pull OCI images:**

A session-scoped fixture in `test_sandbox_real_vm.py` prepares `ubuntu:22.04`
and `node:20-slim` before the first test. Boxlite caches downloaded images for
later runs. Preparation time depends on the registry, network, and local cache.

If an image cannot be pulled, pytest skips the tests and reports the reason:

```
SKIPPED  OCI image pull failed for 'ubuntu:22.04' — likely a network issue, not a code bug.
         Fix: check connectivity or pre-pull manually: boxlite pull ubuntu:22.04
```

**Run all integration tests:**

```bash
uv run python -m pytest tests/integration/test_sandbox_real_vm.py -v
```

The MCP roundtrip test (`test_npx_mcp_server_everything`) installs
`@modelcontextprotocol/server-everything` via `npm install -g` inside the `node:20-slim` VM
on each run, then starts the MCP server and validates the full `initialize` +
`list_tools` flow.

**Run unit and integration tests together:**

```bash
uv run python -m pytest tests/test_sandbox_unit.py tests/integration/test_sandbox_real_vm.py -v
```

**Run the full project test suite** (all test files, excluding integration):

```bash
uv run python -m pytest tests/ --ignore=tests/integration/test_sandbox_real_vm.py -q
```

### 9.6 Run a single test

```bash
# A single test case by full name
uv run python -m pytest "tests/test_sandbox_unit.py::TestBoxliteTranslateCwd::test_subdir_translates_correctly" -v

# A single integration test
uv run python -m pytest "tests/integration/test_sandbox_real_vm.py::TestBoxliteStdioMCPRoundtrip::test_npx_mcp_server_everything" -v -s
```

### 9.7 Troubleshooting

**`ModuleNotFoundError: No module named 'boxlite'`**

```bash
uv sync --extra sandbox
```

**`ModuleNotFoundError: No module named 'mcp'`**

Restore the core dependencies in the project environment:

```bash
uv sync
```

**`PanicException: Another BoxliteRuntime is already using directory`**

A Boxlite runtime is already using the same state directory. List the
candidates, then stop the one that owns the directory:

```bash
pgrep -af boxlite   # command lines, so you can tell which process owns it
kill <pid>          # then wait a couple of seconds and retry
```

Kill by pid rather than by pattern: a pattern wide enough to match the runtime
also matches the shell you typed it in. If you need concurrent runtimes, give
them separate sandbox state directories.

**Integration tests skipped on Linux**

Check that `/dev/kvm` exists and is accessible:

```bash
ls -la /dev/kvm
```

If KVM is unavailable or access is denied, ask the machine administrator to
enable virtualization and grant the required device access to your account.

**Image pull timeout (`create_timeout` exceeded)**

The default `create_timeout` is 300 s. On a slow connection, increase it or pre-pull images manually:

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

## 10. Platform Requirements

| Platform | Requirement |
|----------|------------|
| macOS | Apple Silicon M1+, macOS 12+ (uses `Hypervisor.framework`) |
| Linux | x86_64 or ARM64, KVM enabled (`/dev/kvm` accessible to the current user) |
| Windows | x86_64 WSL2 with KVM enabled (`/dev/kvm` accessible to the current user) |

On unsupported platforms, `sandbox.backend = "none"` uses `DirectExecutor`.
Commands then run directly on the host without VM isolation.

Verify boxlite is installed and the sandbox package is importable:

```bash
uv run python -c "import boxlite; print('boxlite ok')"
uv run python -c "from raven.sandbox import build_executor, SandboxConfig; print('sandbox ok')"
```

To verify end-to-end (requires KVM / Apple Silicon):

```bash
uv run python -m pytest tests/integration/test_sandbox_real_vm.py -v
```
