# Raven Sandbox — User Manual

Sandbox execution runs every shell command and stdio MCP server process inside an isolated **boxlite microVM**, preventing them from touching the host filesystem, network, or kernel outside the declared boundaries.

---

## Table of Contents

1. [Installation](#1-installation)
2. [Configuration](#2-configuration)
3. [How It Works](#3-how-it-works)
4. [Using `SandboxExecutor` Directly](#4-using-sandboxexecutor-directly)
   - [BoxliteExecutor](#41-boxliteexecutor)
   - [DirectExecutor](#42-directexecutor)
5. [Injecting an Executor into `ExecTool`](#5-injecting-an-executor-into-exectool)
6. [Wiring into `AgentLoop`](#6-wiring-into-agentloop)
7. [Wiring into `SubagentManager`](#7-wiring-into-subagentmanager)
8. [Advanced Configuration](#8-advanced-configuration)
   - [Network policy](#81-network-policy)
   - [Extra volume mounts](#82-extra-volume-mounts)
   - [Resource limits](#83-resource-limits)
9. [How to Run Tests](#9-how-to-run-tests)
   - [Prerequisites](#91-prerequisites)
   - [Set up the virtual environment](#92-set-up-the-virtual-environment)
   - [Install dependencies](#93-install-dependencies)
   - [Run unit tests](#94-run-unit-tests)
   - [Run integration tests](#95-run-integration-tests)
   - [Run a single test](#96-run-a-single-test)
   - [Troubleshooting](#97-troubleshooting)
10. [Platform Requirements](#10-platform-requirements)

---

## 1. Installation

The sandbox backend (`boxlite`) is an optional dependency:

```bash
pip install raven[sandbox]
```

Without this extra, the fallback `DirectExecutor` is always used regardless of config, and no microVM is started.

The `sandbox` extra pins `boxlite==0.8.2`. Boxlite's Python API changed in minor versions; the pin prevents silent breakage on upgrades. Revisit when 0.9.x is available.

---

## 2. Configuration

Add a `sandbox` block inside `tools` in your `config.json` / `config.yaml`:

```json
{
  "tools": {
    "sandbox": {
      "backend": "auto"
    }
  }
}
```

`backend: "none"` is the default — existing deployments are unaffected until you opt in.

### Full reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | `"none" \| "auto" \| "boxlite"` | `"none"` | `"none"` → host (no isolation); `"auto"` → probe and use best available; `"boxlite"` → force boxlite. Both `"auto"` and `"boxlite"` probe availability at startup and raise `SandboxInitError` on failure. |
| `image` | `str` | `"ubuntu:22.04"` | OCI image used for the microVM root filesystem. |
| `cpus` | `int` | `2` | vCPU count allocated to the VM. |
| `memory_mib` | `int` | `2048` | RAM in MiB. |
| `disk_size_gb` | `int \| null` | `null` | Disk size in GB. `null` = ephemeral (boxlite default, no persistent disk). |
| `allow_net` | `bool \| list[str]` | `true` | `true` = unrestricted; `false` = no network; `["pypi.org", ...]` = domain allowlist. An empty list `[]` is rejected at config validation time — use `false` to disable networking entirely. |
| `extra_volumes` | `list[[host, vm, mode]]` | `[]` | Additional host paths to mount into the VM. Both paths must be absolute. `mode` is `"ro"` or `"rw"`. |
| `default_timeout` | `int` | `120` | Per-`exec()` timeout in seconds when no explicit timeout is passed. |
| `verify_timeout` | `int` | `30` | Timeout in seconds for the startup `echo ok` probe that confirms the VM is responsive. |
| `create_timeout` | `int` | `300` | Timeout in seconds for image pull + VM creation. Increase for large images or slow registries; decrease if images are always pre-pulled. |

### Common presets

**Auto-detect (recommended):** creates and verifies the working VM at startup, fail fast if unavailable:

```json
{
  "tools": {
    "sandbox": { "backend": "auto" }
  }
}
```

**Force boxlite:** same startup verification, useful when multiple backends exist in the future:

```json
{
  "tools": {
    "sandbox": { "backend": "boxlite" }
  }
}
```

**Production — custom image, resource limits, network allowlist:**

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

**Air-gapped — no network access:**

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

---

## 3. How It Works

`SandboxExecutor` is an abstract base class in `raven/sandbox/interfaces.py`. Two implementations ship:

| Implementation | When used | Isolation |
|----------------|-----------|-----------|
| `BoxliteExecutor` | `backend = "auto"` or `"boxlite"` | boxlite microVM — separate kernel, capped resources |
| `DirectExecutor` | `backend = "none"` | None — `asyncio.create_subprocess_shell()` on the host |

The workspace directory is **volume-mounted** into the VM at `/workspace` (read-write). Filesystem tools (`ReadFileTool`, `WriteFileTool`, etc.) continue to operate on the host path directly; when a sandboxed command reads or writes `/workspace/foo.py`, it accesses the same file.

The executor object is constructed **synchronously** during `AgentLoop.__init__` (pure Python, no VM yet). When `_start_executor()` is called on the first `run()` / `process_direct()`, `BoxliteExecutor.start()` **eagerly** creates and verifies the working VM:

- **`allow_net=True` (default)** — working VM is created directly; boxlite pulls the image on first use. One cold-start (~2–5 s on a warm image).
- **`allow_net=False` or domain list** — a small throwaway VM starts first with unrestricted network to pull and cache the image, then the working VM is created with the restricted network policy. Two cold-starts in sequence.

If boxlite is missing or the platform is unsupported, `SandboxInitError` is raised before the agent loop starts, and `run()` / `process_direct()` surface a clean error message to the caller rather than an unhandled exception traceback.

---

## 4. Using `SandboxExecutor` Directly

### 4.1 `BoxliteExecutor`

```python
import asyncio
from pathlib import Path
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
    async with build_executor(sandbox_cfg, workspace) as executor:
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

`as_text()` concatenates stdout, a `STDERR:` block (if non-empty), and `Exit code: N` into a single string. The exit-code line is always present. When the output exceeds `max_chars` the middle is truncated with a `... (N chars truncated) ...` marker.

**Lifecycle — explicit start/stop:**

```python
from raven.sandbox import SandboxInitError
from raven.sandbox.boxlite_executor import BoxliteExecutor

executor = BoxliteExecutor(
    image="ubuntu:22.04",
    workspace=Path("/tmp/ws"),
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
    async with BoxliteExecutor(image="ubuntu:22.04", workspace=Path("/tmp/ws")) as executor:
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
from raven.sandbox import build_executor, SandboxConfig

# No config — returns DirectExecutor
executor = build_executor(None, workspace)

# Explicit "none" — also returns DirectExecutor (no probe, no VM)
executor = build_executor(SandboxConfig(backend="none"), workspace)
```

---

## 5. Injecting an Executor into `ExecTool`

`ExecTool` accepts an optional `executor` parameter. When omitted it falls back to a freshly constructed `DirectExecutor` (backward-compatible default).

```python
from pathlib import Path
from raven.sandbox import build_executor, SandboxConfig
from raven.agent.tools.shell import ExecTool

sandbox_cfg = SandboxConfig(backend="boxlite")
workspace = Path("/tmp/ws")

executor = build_executor(sandbox_cfg, workspace)
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

When a `BoxliteExecutor` is injected, the regex deny-list (`rm -rf`, fork bombs, etc.) is **skipped** — the microVM provides real kernel-level isolation, making host-side pattern matching redundant. The deny-list remains active for `DirectExecutor` as a best-effort guard. `restrict_to_workspace` path-boundary checks are enforced **regardless** of the executor — even when sandboxed, commands referencing paths outside the workspace are blocked and logged.

---

## 6. Wiring into `AgentLoop`

`AgentLoop` constructs the executor synchronously in `__init__` and starts it lazily before the first message is processed. Pass a `SandboxConfig` when constructing the loop:

```python
from raven.agent.loop import AgentLoop
from raven.sandbox import SandboxConfig

loop = AgentLoop(
    bus=bus,
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

`close_mcp()` calls `close_executor()` internally — they share a lifecycle because stdio MCP server processes run inside the VM and must be stopped before the VM is torn down. Calling `close_executor()` separately afterwards is a no-op (idempotent).

**MCP stdio servers:**

When `sandbox.backend` is `"auto"` or `"boxlite"`, stdio MCP servers are launched **inside the VM** rather than on the host. Three asyncio bridge tasks translate between boxlite's streaming execution API and the `anyio` `MemoryObjectStream` pairs that `ClientSession` expects:

- `_stdout_bridge` — reads VM stdout chunks, buffers until `\n`, parses JSON-RPC, wraps in `SessionMessage`, forwards to read stream
- `_stdin_bridge` — receives `SessionMessage` from write stream, extracts the inner `JSONRPCMessage`, serialises to JSON + newline, writes to VM stdin
- `_stderr_bridge` — reads VM stderr and forwards to application log at `WARNING` level

Stderr is the primary signal for diagnosing MCP server startup failures:

```
WARNING  MCP server stderr [npx]: cannot find module '@scope/server'
WARNING  MCP server stderr [npx]: Error: ENOENT ...
```

Non-JSON lines on stdout (e.g. npm download progress during `npx -y ...` startup) are silently skipped — they are logged at DEBUG level and do not interrupt the `ClientSession`. HTTP/SSE MCP servers are unaffected — they make remote calls and involve no local processes.

---

## 7. Wiring into `SubagentManager`

Each sub-agent gets its **own isolated VM instance** — stronger isolation than sharing the parent's VM.

```python
from raven.agent.subagent import SubagentManager
from raven.sandbox import SandboxConfig

manager = SubagentManager(
    provider=provider,
    workspace=workspace,
    bus=bus,
    sandbox_config=SandboxConfig(backend="boxlite"),
)

# spawn() returns immediately; the sub-agent runs in the background
handle = await manager.spawn(task="run the test suite and report failures")
```

Internally, `_run_subagent()` calls `build_executor(self._sandbox_config, self.workspace)` and wraps the mini agent loop in `async with executor:`, so the VM is started when the sub-agent begins and torn down when it finishes — regardless of whether the task succeeds or raises.

Each subagent VM incurs its own cold-start (~2–5 s). For workloads that spawn many subagents concurrently, consider pre-pulling the image (`pip install raven[sandbox]` + running the integration tests once) to eliminate the image-pull component of that cost.

When `AgentLoop` creates a `SubagentManager` it passes down the same `sandbox_config`, so the inheritance is automatic:

```python
# In AgentLoop.__init__ (simplified)
self.subagents = SubagentManager(
    ...,
    sandbox_config=sandbox_config,   # same config, isolated VM per sub-agent
)
```

---

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

When `allow_net` is restricted (domain list or `False`), `BoxliteExecutor.start()` pre-pulls the OCI image using a throwaway VM with unrestricted networking before creating the working VM with the restricted policy. This adds one extra cold-start (~2–5 s) to initial startup.

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
result = await executor.exec("pip install numpy", timeout=10)
```

---

## 9. How to Run Tests

### 9.1 Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.11+ | Check with `python3 --version` |
| [uv](https://docs.astral.sh/uv/) | Preferred package manager; install with `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| boxlite (integration tests only) | macOS Apple Silicon M1+ **or** Linux x86_64/ARM64 with `/dev/kvm` |
| Node.js / npx (MCP roundtrip test only) | Provided by the `node:20-slim` OCI image — no local Node required |

Unit tests have **no** boxlite or KVM requirement and run on any machine where Python 3.11+ is available.

---

### 9.2 Set up the virtual environment

The project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
# Clone and enter the repo
git clone <repo-url>
cd raven

# Create the virtual environment and install all project dependencies
uv sync
```

If you don't have `uv`, you can use plain `pip` instead:

```bash
python3 -m venv .venv
source .venv/bin/activate     # macOS / Linux
# .venv\Scripts\activate      # Windows (PowerShell)
```

---

### 9.3 Install dependencies

**Unit tests** — no extras needed beyond `uv sync`:

```bash
# Core project + dev tools (pytest, pytest-asyncio, etc.)
uv sync

# The mcp package is required for the three MCP bridge unit tests;
# without it those tests auto-skip.
uv add --dev "mcp>=1.0"
```

**Integration tests** — additionally require the sandbox optional extra:

```bash
# Install boxlite (pinned to 0.8.2) and anyio
uv sync --extra sandbox
```

Verify the installs:

```bash
# Sandbox package should import cleanly
uv run python -c "from raven.sandbox import build_executor, SandboxConfig; print('sandbox ok')"

# boxlite binary should be available (integration tests only)
uv run python -c "import boxlite; print('boxlite ok')"
```

---

### 9.4 Run unit tests

Unit tests cover `SandboxConfig`, `DirectExecutor`, `BoxliteExecutor` (mocked), `ExecTool` guard logic, `AgentLoop` executor lifecycle, and the MCP bridge tasks. They run anywhere — no VM or KVM required.

```bash
uv run python -m pytest tests/test_sandbox_unit.py -v
```

Expected output (with `mcp` installed):

```
57 passed in ~8s
```

Expected output (without `mcp` installed — 3 bridge tests skip):

```
54 passed, 3 skipped in ~8s
```

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

---

### 9.5 Run integration tests

Integration tests start real boxlite VMs. They require:
- `pip install raven[sandbox]` (or `uv sync --extra sandbox`)
- macOS Apple Silicon M1+ **or** Linux with `/dev/kvm` accessible

On Linux without `/dev/kvm` the entire file is **automatically skipped** — no failure, no action needed.

**First run — pre-pull OCI images:**

A session-scoped fixture in `test_sandbox_integration.py` pre-pulls all required images
(`ubuntu:22.04` and `node:20-slim`) before the first test. On a fast connection this takes
~30–60 s on first run and is instant on subsequent runs (images are cached by boxlite).

If the pull fails (slow network, registry unavailable) the tests skip with a clear message:

```
SKIPPED  OCI image pull failed for 'ubuntu:22.04' — likely a network issue, not a code bug.
         Fix: check connectivity or pre-pull manually: boxlite pull ubuntu:22.04
```

**Run all integration tests:**

```bash
uv run python -m pytest tests/test_sandbox_integration.py -v
```

Expected output:

```
tests/test_sandbox_integration.py::TestBoxliteExecutorIntegration::test_exec_echo              PASSED
tests/test_sandbox_integration.py::TestBoxliteExecutorIntegration::test_exec_timeout          PASSED
tests/test_sandbox_integration.py::TestBoxliteExecutorIntegration::test_exec_cwd              PASSED
tests/test_sandbox_integration.py::TestBoxliteExecutorIntegration::test_volume_mount_file_visible_in_vm  PASSED
tests/test_sandbox_integration.py::TestBoxliteExecutorIntegration::test_lifecycle_context_manager        PASSED
tests/test_sandbox_integration.py::TestBoxliteStdioMCPRoundtrip::test_npx_mcp_server_everything          PASSED

6 passed in ~55s
```

The MCP roundtrip test (`test_npx_mcp_server_everything`) installs
`@modelcontextprotocol/server-everything` via `npm install -g` inside the `node:20-slim` VM
on each run (~15 s), then starts the MCP server and validates the full `initialize` +
`list_tools` flow.

**Run unit and integration tests together:**

```bash
uv run python -m pytest tests/test_sandbox_unit.py tests/test_sandbox_integration.py -v
```

**Run the full project test suite** (all test files, excluding integration):

```bash
uv run python -m pytest tests/ --ignore=tests/test_sandbox_integration.py -q
```

---

### 9.6 Run a single test

```bash
# A single test case by full name
uv run python -m pytest "tests/test_sandbox_unit.py::TestBoxliteTranslateCwd::test_subdir_translates_correctly" -v

# A single integration test
uv run python -m pytest "tests/test_sandbox_integration.py::TestBoxliteStdioMCPRoundtrip::test_npx_mcp_server_everything" -v -s
```

---

### 9.7 Troubleshooting

**`ModuleNotFoundError: No module named 'boxlite'`**

```bash
uv sync --extra sandbox
# or
pip install "raven[sandbox]"
```

**`ModuleNotFoundError: No module named 'mcp'`** (3 bridge tests skip or fail)

```bash
uv add --dev "mcp>=1.0"
```

**`PanicException: Another BoxliteRuntime is already using directory`**

A previous test run or debug session left a boxlite process running. Kill it:

```bash
pkill -f boxlite
# wait 2 seconds, then re-run
```

**Integration tests skipped on Linux**

Check that `/dev/kvm` exists and is accessible:

```bash
ls -la /dev/kvm
# If missing, enable KVM in your VM/hypervisor settings or on bare metal:
sudo modprobe kvm_intel   # or kvm_amd
sudo chmod 666 /dev/kvm
```

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

---

## 10. Platform Requirements

| Platform | Requirement |
|----------|------------|
| macOS | Apple Silicon M1+, macOS 12+ (uses `Hypervisor.framework`) |
| Linux | x86_64 or ARM64, KVM enabled (`/dev/kvm` accessible to the current user) |
| Windows | x86_64 WSL2 with KVM enabled (`/dev/kvm` accessible to the current user) |

On unsupported platforms, set `sandbox.backend = "none"` (the default) to use `DirectExecutor` transparently.

Verify boxlite is installed and the sandbox package is importable:

```bash
python -c "import boxlite; print('boxlite ok')"
python -c "from raven.sandbox import build_executor, SandboxConfig; print('sandbox ok')"
```

To verify end-to-end (requires KVM / Apple Silicon):

```bash
uv run python -m pytest tests/test_sandbox_integration.py -v
```
