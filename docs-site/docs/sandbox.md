# Sandbox

With sandboxing enabled, shell commands and stdio MCP server processes run in an
NVIDIA OpenShell sandbox instead of on the host. Each sandbox is a container whose filesystem access, process identity
and network egress are enforced by the kernel (Landlock and seccomp) and by an
OpenShell supervisor outside the container. Mounted directories, including the
shared workspace, stay accessible according to their configured permissions.

Raven talks to a local OpenShell gateway, which creates and deletes the
sandboxes. Raven does not run its own virtual machines.

## 1. Installation

Two pieces are needed: the OpenShell SDK inside Raven's environment, and a
running OpenShell gateway on the same machine.

### 1.1 The SDK

The SDK is the optional `sandbox` extra. From a source checkout:

```bash
uv sync --extra sandbox
```

For an installed Raven, `raven onboard` prints the exact command for its own
interpreter when the SDK is missing. It looks like this:

```bash
uv pip install --python /path/to/raven/python 'openshell<0.2,>=0.1.8; (sys_platform != "win32")'
```

The extra pins `openshell>=0.1.8,<0.2`. The SDK and the gateway are released
together, so keep them on the same minor version.

### 1.2 The gateway

Install OpenShell 0.1.8 with its official installer, pinned to the release tag.
Turn telemetry off before the first start; the packaged gateway reads
`~/.config/openshell/gateway.env`:

```bash
mkdir -p ~/.config/openshell
printf 'OPENSHELL_TELEMETRY_ENABLED=false\n' > ~/.config/openshell/gateway.env
curl -fsSL -o /tmp/openshell-install.sh https://raw.githubusercontent.com/NVIDIA/OpenShell/v0.1.8/install.sh
OPENSHELL_VERSION=v0.1.8 sh /tmp/openshell-install.sh
```

On Linux the installer adds a systemd user service; on macOS it installs with
Homebrew. Both register the gateway with the `openshell` CLI and listen on
`127.0.0.1:17670` only.

Raven mounts the workspace into each sandbox, so the gateway must allow bind
mounts. Write `~/.config/openshell/gateway.toml`:

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

Then restart the gateway and check it:

```bash
systemctl --user restart openshell-gateway        # Linux
brew services restart nvidia/openshell/openshell  # macOS
openshell status                                  # expect: Status: Connected
```

On macOS with colima instead of Docker Desktop, the supervisor cannot reach the
gateway through the container's loopback. If sandboxes stay in
`ControlSupervisorStartFailed`, add this to the Docker driver table:

```toml
supervisor_network_mode = "bridge"
grpc_endpoint = "https://host.docker.internal:17670"
```

Run one gateway replica. Raven sends its exec calls straight to the gateway
its client is bound to, instead of resolving the replica that owns the
sandbox, so a replicated gateway cannot serve them.

## 2. Configuration

Add a `sandbox` block under `tools` in `config.json`. Invalid JSON produces a
warning and falls back to defaults, including `tools.sandbox.backend = "none"`,
so check the startup warnings before relying on sandbox isolation:

```json
{
  "tools": {
    "sandbox": {
      "backend": "openshell"
    }
  }
}
```

With `backend = "openshell"`, a missing SDK or an unreachable gateway raises
`SandboxInitError`. Raven never falls back to the host on its own; host
execution happens only when the backend is `"none"`.

### Full reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | `"none"` \| `"openshell"` | `"none"` | `"none"` runs commands on the host with no isolation. |
| `gateway` | string \| null | `null` | Name of a gateway registered with the `openshell` CLI; `null` uses the active one. |
| `image` | string | `"ubuntu:22.04"` | Container image each sandbox starts from. |
| `cpus` | int | `2` | CPU limit per sandbox. |
| `memoryMib` | int | `2048` | Memory limit per sandbox, in MiB. |
| `extraVolumes` | list | `[]` | Extra bind mounts, each `[host_path, sandbox_path, "ro"\|"rw"]`. |
| `defaultTimeout` | int | `120` | Seconds per command when no explicit timeout is given. |
| `verifyTimeout` | int | `30` | Seconds for the startup probe. |
| `createTimeout` | int | `300` | Seconds for image pull and sandbox creation. |

What a sandbox may reach is not set here: it is part of the permission set,
under `permissions.network`. See [Network policy](#41-network-policy).

### Migrating from the boxlite backend

The boxlite microVM backend is retired. Configs written for it still load,
and the first load rewrites them once, with a notice for each change:

- `backend: "boxlite"` and `backend: "auto"` become `"openshell"`. Both
  meant boxlite before, so a machine without OpenShell refuses to start a
  sandbox rather than falling back to the host.
- `debug` and `diskSizeGb` are removed.
- `raven sandbox list/exec/shell` is gone. Use the OpenShell CLI instead; see
  [Inspecting sandboxes](#35-inspecting-sandboxes).
- The old boxlite state under Raven's data directory (`sandbox/boxlite`) is no
  longer used and can be deleted.

## 3. How It Works

`SandboxExecutor` (`raven/sandbox/interfaces.py`) has two implementations:

| Implementation | When used | Isolation |
|----------------|-----------|-----------|
| `OpenShellExecutor` | `backend = "openshell"` | One OpenShell sandbox per executor |
| `DirectExecutor` | `backend = "none"` | None; commands run on the host |

### 3.1 One sandbox per agent

The main agent loop owns one executor for its lifetime. Every subagent run and
every DAG node gets an executor of its own, so each has its own sandbox: its
files, processes, network rules and audit records are separate from the
others'. A sandbox is created when its executor starts and deleted when it
stops.

Deleting a sandbox takes several seconds on the gateway, so `stop()` only
starts the deletion. When Raven shuts down it waits up to 20 seconds for
pending deletions. A sandbox left behind by a crash carries the labels
`raven.owner` and `raven.pid`, and the next Raven on the same host and user
deletes it when it starts its first sandbox.

### 3.2 Mounts and identity

The workspace is bind mounted at `/workspace` with read-write access. File
tools such as `read_file` and `write_file` use the host path directly, so
`/workspace/foo.py` inside the sandbox is the same file. Raven also mounts:

- the agent home at `/agent-home` when it lies outside the workspace;
- the plugin data root at `/plugin-data` when a plugin package runs a stdio
  MCP server;
- every entry of `extraVolumes`.

The sandbox replaces OpenShell's default filesystem policy: system paths
(`/bin`, `/usr`, `/lib`, `/etc`, `/proc`, `/var/log`, `/dev/urandom`) are
read-only, `/tmp` and the mounts are writable, and everything else is
unreachable.

On a non-root host the sandbox runs as the same user and group as Raven, so
files it writes in the workspace belong to you. OpenShell refuses to run a
workload as root, so on a root host the sandbox runs as the image's user (UID
1000 for an image that declares none), and that user must be able to write the
workspace. Raven checks this when a sandbox starts and stops with a clear
error if it cannot.

### 3.3 Commands

Each command runs as `sh -c <command>` through the gateway, in the sandbox path
that corresponds to the requested host directory. When a command exceeds its
timeout, OpenShell stops it, and Raven reports exit code `-1` with
`Command timed out after Ns`, the same result the host executor gives.
Cancelling a turn cancels the gateway call that runs the command.

### 3.4 MCP stdio servers

With the sandbox enabled, stdio MCP servers start inside the sandbox rather
than on the host. Their command, arguments, environment values and working
directory are translated from host paths to sandbox paths; a working directory
outside every mount is refused rather than replaced. Raven bridges the server's
stdin and stdout over the gateway, and forwards its stderr to the log:

```
WARNING  MCP server stderr [npx]: cannot find module '@scope/server'
```

Non-JSON stdout lines, such as npm progress during `npx -y ...`, are skipped.
HTTP and SSE MCP servers connect directly and are not affected.

### 3.5 Inspecting sandboxes

Raven's sandboxes are named `rv` followed by twelve hex digits. Use the
OpenShell CLI to look at them:

```bash
openshell sandbox list
openshell sandbox exec -n rv1a2b3c4d5e6f -- ls -la /workspace
openshell sandbox connect rv1a2b3c4d5e6f   # interactive shell
openshell logs rv1a2b3c4d5e6f              # supervisor log, including denied connections
```

## 4. Advanced Configuration

### 4.1 Network policy

OpenShell denies all egress that no rule allows, and checks each connection
against the hostname it was made to. The rules come from `permissions.network`,
the permission set every agent follows; see
[Permissions: Network](permissions.md#network) for the rules, and for how a
blocked connection is approved while a command runs.

The default, `open`, opens every hostname of three or more labels
(`api.github.com`, `files.pythonhosted.org`, `www.example.com`) on every port,
plus a list of common two-label names on ports 22, 80, 443, 8080, 8443 and
9418: package registries (`pypi.org`, `npmjs.org`, `crates.io`,
`rubygems.org`, ...), code hosts (`github.com`, `gitlab.com`, ...), container
registries (`docker.io`, `ghcr.io`, ...) and model APIs. Any other two-label
name, such as `example.com`, is blocked until a rule or an approval opens it.
OpenShell has no rule that allows every hostname (NVIDIA/OpenShell#3856), so
this is the closest open setting it supports. `closed` opens only what a rule
allows.

These rules relay TLS without terminating it, so egress runs at full speed.
Whatever the policy, OpenShell always blocks loopback, link-local addresses
and the cloud metadata address `169.254.169.254`.

Raven's own web tools run on the host and are not affected by this policy. It
applies to commands in the sandbox (`pip`, `npm`, `git`, `curl`) and to stdio
MCP servers.

A rule or an approval reaches a running sandbox before its next command. The
gateway delivers a policy change by polling, which takes about 10 seconds. Push
delivery makes it about a second: add this line to
`~/.config/openshell/gateway.env` and restart the gateway.

```
OPENSHELL_CONFIG_DELIVERY_MODE=push
```

`tools.sandbox.allowNet` is retired. The first load moves it into
`permissions.network`: `true` is the open default, `false` is closed, a host
list allows those hosts on ports 80 and 443 with the default closed, and a
list with `"*"` keeps the open default and adds the other hosts.

### 4.2 Extra volume mounts

```json
{
  "extraVolumes": [
    ["/data/datasets", "/datasets", "ro"],
    ["/home/me/cache", "/cache", "rw"]
  ]
}
```

Both paths must be absolute. The gateway must allow bind mounts (section 1.2).
Use host directories outside `/tmp`: the packaged Linux gateway runs with
systemd `PrivateTmp`, so it cannot see the host's `/tmp`, and a mount from
there fails with `bind source path does not exist`.

### 4.3 Resources and timeouts

```json
{ "cpus": 4, "memoryMib": 4096, "defaultTimeout": 300 }
```

`cpus` and `memoryMib` become the container's limits. A tool call may still
pass its own timeout, which overrides `defaultTimeout` for that call.

## 5. Performance

Measured on Linux 6.8 with Docker 29 and OpenShell 0.1.8:

| Operation | Cost |
|-----------|------|
| Create to ready | about 2 s; about 4 s for 8 created at once |
| Command round trip | about 20 ms |
| Delete | about 5 s on the gateway, in the background for Raven |
| Memory per idle sandbox | about 31 MiB on the host |
| CPU per sandbox creation | about 1.5 CPU-seconds |

Opening a file in a sandbox costs about ten times what it costs in a plain
container, because OpenShell mediates the call. Workloads that open many small
files, such as installing dependencies or `git add` on a large tree, run
several times slower than on the host.

## 6. How to Run Tests

Unit tests need neither OpenShell nor a gateway:

```bash
uv run --frozen --all-extras pytest tests/test_sandbox_unit.py \
  tests/test_sandbox_openshell_executor.py tests/test_sandbox_mounts.py -v
```

The integration tests drive a real local gateway. They need an image with `sh`,
`python3` and `curl`, and a workspace root outside `/tmp`:

```bash
RAVEN_OPENSHELL_IT=1 \
RAVEN_OPENSHELL_IT_IMAGE=my-image:latest \
uv run --frozen --all-extras pytest tests/integration/test_openshell_sandbox_real_gateway.py -v -s
```

`RAVEN_OPENSHELL_IT_ROOT` overrides where the test workspaces live,
`RAVEN_OPENSHELL_IT_HOST` names a reachable HTTPS host, and
`RAVEN_OPENSHELL_IT_UNLISTED_APEX` names a two-label host outside the default
list.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `No sandbox backend available` | The SDK is not in Raven's environment | Run the install command from section 1.1 |
| `Cannot create an OpenShell sandbox` | No gateway answers | `openshell status`; restart the gateway |
| `did not become ready: ... bind source path does not exist` | Mount source under `/tmp`, or bind mounts disabled | Move the workspace out of `/tmp`; check `gateway.toml` |
| `ConfigurationInvalid` on start | The gateway rejected the policy | `openshell logs <name>` |
| `cannot write the workspace` | Raven runs as root, so OpenShell's sandbox user does not own the workspace | Run Raven as a non-root user, or give the workspace to that UID |
| `curl: (7) ... Couldn't connect` within milliseconds | The network policy blocked the host | Approve it when Raven asks, or add a rule under `permissions.network` |

## 8. Platform Requirements

| Platform | Support |
|----------|---------|
| Linux x86_64 / arm64 | Kernel 6.2 or newer with Landlock ABI 3 enabled, Docker 28 or newer |
| macOS Apple Silicon | Docker Desktop, or colima with the settings in section 1.2 |
| Windows | Not supported; the `sandbox` extra installs nothing there |

The OpenShell SDK is published for Linux and macOS only. Upstream also lists
Windows (WSL 2 + Docker Desktop) as experimental and keeps native Windows on
its own branch, on a compute driver Raven does not drive, so Raven offers no
sandbox backend on a Windows host. A Raven that runs *inside* WSL 2 is a Linux
host and can use the backend, with the same kernel and Docker requirements as
the table above.

A root host cannot run the agent's sandbox: OpenShell refuses a root workload,
so the sandbox runs as the user of the image and cannot write a root-owned
workspace. Run Raven as a non-root user that owns the workspace. The design
engine's isolated rendering is the exception, because the directory it mounts
is a throwaway of its own.

Check a Linux host with:

```bash
uname -r
cat /sys/kernel/security/lsm   # must include landlock
python3 -c "import ctypes;print(ctypes.CDLL(None,use_errno=True).syscall(444,None,0,1))"   # Landlock ABI, need >= 3
docker version --format '{{.Server.Version}}'
```

A container inherits its host's kernel, so a Raven running inside a container
on an older kernel cannot use the sandbox either. The gateway must run on the
same machine as Raven, because the workspace is bind mounted by host path.
