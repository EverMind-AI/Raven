# Raven Sandbox — Debug CLI Commands

The `raven sandbox` command group lets you inspect and interact with the boxlite microVMs that an `raven` process is using. It connects to a Unix domain socket exposed by the `SandboxDebugServer` running inside that process, so a running `raven` with debug mode enabled is required.

These commands are intended for development and troubleshooting (poking at a stuck VM, running ad-hoc diagnostics inside a sandbox, attaching an interactive shell). They are not meant to be enabled in production.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Connection model](#2-connection-model)
3. [`sandbox list` / `sandbox ls`](#3-sandbox-list--sandbox-ls)
4. [`sandbox exec`](#4-sandbox-exec)
5. [`sandbox shell`](#5-sandbox-shell)
6. [VM selection (`--vm`)](#6-vm-selection---vm)
7. [Errors and exit codes](#7-errors-and-exit-codes)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Prerequisites

The debug socket is only created when an `raven` process is running with `sandbox.debug.enabled = true`. Add this to your `config.json` / `config.yaml`:

```json
{
  "tools": {
    "sandbox": {
      "backend": "auto",
      "debug": {
        "enabled": true,
        "socket": "sandbox/debug.sock"
      }
    }
  }
}
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `debug.enabled` | `bool` | `false` | Master switch. When `false`, the debug server never starts and the CLI cannot connect. Ignored if `backend = "none"`. |
| `debug.socket` | `str` | `"sandbox/debug.sock"` | Unix socket path. Relative paths are resolved against the Raven data directory; absolute paths are used as-is. The CLI resolves the same value to find the socket. |
| `debug.maxMessageBytes` | `int` | `1048576` | Per-line message cap on the protocol (1 MiB). |

The socket file is created with mode `0600` and is only accessible to the user that started `raven`. Run the CLI as the same user.

---

## 2. Connection model

Each invocation of `raven sandbox <subcommand>` opens a single connection to the debug socket, sends one command, streams responses, and exits. The protocol is newline-delimited JSON with base64-encoded binary payloads. You do not normally need to know this — the CLI handles framing — but it explains the failure modes:

- **Socket missing** → no `raven` process is running, or it was started with `debug.enabled = false`, or the agent process started but its debug server failed to bind (check the agent output for a `[Sandbox debug]` line).
- **Permission denied** → the socket exists but is owned by a different user.
- **Connection refused** → the `raven` process is exiting or has crashed.
- **`Sandbox debug server already has an active client.`** → another `raven sandbox` invocation is currently attached. The server is single-client (see below); wait for the other session to disconnect, or stop it.

The CLI prints a clear message and exits non-zero in each case.

### 2.1 Single-client policy

The debug server accepts **at most one client connection at a time**. A second concurrent connection is rejected immediately with the error above; nothing on the server side is disturbed by it. This is intentional:

- It mirrors how a debugger attaches to a running process: one operator, one session.
- Two CLIs racing on the same VM (`exec` while a `shell` is open, or two `shell`s into one VM) cause confusing interleaving on PTYs and stream multiplexing — easier to forbid than to coordinate.
- It also defends against two `raven` processes pointing the same `debug.socket` path at each other: the second process to start sees the live socket from the first and refuses to bind, rather than silently stealing it. (The first process's `sandbox` CLI keeps working.)

If you need parallel debug sessions, give each agent a distinct `debug.socket` path.

---

## 3. `sandbox list` / `sandbox ls`

List every sandbox VM visible to the running `raven` process. `ls` is an alias for `list`.

```bash
raven sandbox list
raven sandbox ls
```

### Output

A Rich table is printed to stdout with the following columns:

| Column | Meaning |
|--------|---------|
| (first) | `*` if this VM was created by the connected `raven` process (owned), `-` otherwise. |
| `ID` | The VM identifier returned by boxlite. Use this with `--vm` for `exec` / `shell`. |
| `State` | VM state. `running` is highlighted in green; everything else (e.g. `stopped`, `creating`) is dimmed. |
| `Image` | OCI image used for the root filesystem. |
| `CPUs` | vCPU count. |
| `Mem MiB` | Memory allocated in MiB. |
| `Created At` | Creation timestamp, truncated to `YYYY-MM-DD HH:MM:SS`. |

If no VMs are visible, the command prints `No VMs found.` and exits `0`.

### Example

```
                Sandbox VMs
┏━━━┳━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┓
┃   ┃ ID         ┃ State   ┃ Image          ┃ CPUs ┃ Mem MiB ┃ Created At         ┃
┡━━━╇━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━┩
│ * │ vm-abc123  │ running │ ubuntu:22.04   │    2 │    2048 │ 2026-05-08 12:34:56│
│ - │ vm-def456  │ stopped │ python:3.11    │    4 │    4096 │ 2026-05-07 09:12:30│
└───┴────────────┴─────────┴────────────────┴──────┴─────────┴────────────────────┘
```

> **Note**: VMs are not named today, so a `Name` column is intentionally omitted; the codebase has TODOs to restore it once naming is supported.

---

## 4. `sandbox exec`

Run a one-shot command inside a sandbox VM and stream its output back to the terminal.

```bash
raven sandbox exec [--vm VM_REF] CMD [ARG ...]
```

### Options

| Option | Description |
|--------|-------------|
| `--vm VM_REF` | Target VM ID. Optional when exactly one VM is running — the server auto-selects it. Required when multiple VMs are running. |
| `CMD [ARG ...]` | The program and its arguments. Passed through unchanged. Unknown options are forwarded to the program (e.g. `raven sandbox exec ls -la /workspace`). |

The argument list is captured via `allow_extra_args=True` and `ignore_unknown_options=True`, so flags meant for the VM-side program won't be misinterpreted by Typer.

### Output streams

- **stdout** from the VM is written verbatim to the host's stdout (binary-safe, base64-decoded under the hood).
- **stderr** from the VM is written to the host's stderr, **prefixed with `[stderr] `** so the two streams stay distinguishable when redirected together.
- The command exits with the **exit code returned by the VM-side process**. If the server returns an error, the CLI prints `Error: <message>` in red and exits `1`.

### Examples

```bash
# Run a single command in the only running VM
raven sandbox exec uname -a

# Pick a specific VM
raven sandbox exec --vm vm-abc123 ls -la /workspace

# Run a Python one-liner
raven sandbox exec python3 -c "import sys; print(sys.version)"

# Capture only stdout (drop the [stderr] prefixed lines)
raven sandbox exec --vm vm-abc123 sh -c 'cat /etc/os-release; ls /missing' 2>/dev/null
```

### Errors

| Condition | Message | Exit code |
|-----------|---------|-----------|
| No command supplied | `Error: a command to execute is required.` plus a usage hint | `1` |
| Server-side error | `Error: <message>` | `1` |
| VM process succeeded | (no extra output) | exit code from the VM process |

---

## 5. `sandbox shell`

Open a fully interactive PTY-backed shell inside a sandbox VM.

```bash
raven sandbox shell [--vm VM_REF] [--shell /bin/sh]
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--vm VM_REF` | _auto_ | Target VM ID. Auto-selected if exactly one VM is running. |
| `--shell PATH` | `/bin/sh` | Absolute path to the shell binary inside the VM. Use `/bin/bash` on images that ship it. |

### Behavior

The CLI:

1. Sends a `shell` command to the debug server with the chosen VM and shell binary.
2. Waits for the server to return a `ready` message before touching the local terminal. If the server returns `error` instead, the CLI prints `Error: <message>` and exits `1` without modifying terminal state.
3. Saves current `termios` attributes, switches the local TTY into raw mode, and registers a non-blocking reader on `stdin` so every keystroke is forwarded to the VM.
4. Sends the initial terminal size (`rows`, `cols`) via `TIOCGWINSZ`, and re-sends the size on every `SIGWINCH` (window resize). The signal is bounced through the asyncio loop, so `create_task` and writer access happen on the loop thread.
5. Streams the VM's stdout back into the local terminal verbatim (binary-safe).
6. On `exit`, restores terminal attributes, removes the stdin reader, restores the previous `SIGWINCH` handler, closes the socket, and propagates the VM-side exit code as its own.

If the server-side process emits an error mid-session, the CLI restores the terminal **before** printing the red error message, so the message is readable.

### Examples

```bash
# Drop into /bin/sh inside the only running VM
raven sandbox shell

# Use bash and target a specific VM
raven sandbox shell --vm vm-abc123 --shell /bin/bash
```

To exit the shell, run `exit` or press `Ctrl-D` inside the VM, or close the local terminal. The CLI restores your terminal as the connection closes.

> **Tip**: `Ctrl-C` is forwarded to the VM as `0x03` because the local terminal is in raw mode — it interrupts the foreground program inside the VM, **not** the CLI. To kill the CLI itself from outside, send it `SIGTERM` from another terminal.

> **EOF / Ctrl-D**: Interactive `Ctrl-D` is forwarded as a raw `0x04` byte and interpreted as `VEOF` by the VM-side PTY's line discipline (matches `docker exec -it`). When the local stdin is a pipe or file and reaches EOF, the CLI synthesises one final `0x04` byte before disconnecting so the VM-side shell terminates cleanly instead of waiting for input that will never arrive. If a particular image's PTY is configured to suppress `VEOF`, run `exit` instead.

> **Idle disconnect**: Closing the terminal (or hitting Ctrl-C in another terminal) without typing `exit` is now safe — the server detects the client is gone, kills the VM-side shell, and releases the single-client slot. Previous releases could leave an orphan `sh` running inside the VM in this case.

---

## 6. VM selection (`--vm`)

`exec` and `shell` take an optional `--vm` argument:

- **Omit `--vm`** when exactly one VM is running. The server picks it automatically.
- **Pass `--vm <id>`** to target a specific VM by ID (as shown in `sandbox list`).
- The flag also accepts a name in the protocol (`vm_ref`) for forward compatibility, but VMs are unnamed today, so practically only IDs work.

If `--vm` is omitted and zero or more-than-one VMs are running, the server returns an error and the CLI exits `1`.

---

## 7. Errors and exit codes

All commands share the same connection-time error handling:

| Situation | Output | Exit code |
|-----------|--------|-----------|
| Socket file missing | `Debug socket not found at <path>.` plus a hint to enable `sandbox.debug.enabled=true` and to check the agent log for a debug-start failure | `1` |
| Socket exists but unreadable/unwritable | `Cannot connect to debug socket at <path>: permission denied` | `1` |
| Connect failed (`ECONNREFUSED`, `ENOENT`, OS error) | `Cannot connect to debug socket at <path>: <reason>` | `1` |
| Another client already attached | `Error: Sandbox debug server already has an active client. …` | `1` |
| Server returned `{"type": "error", "message": ...}` | `Error: <message>` | `1` |
| `list` succeeded | table or `No VMs found.` | `0` |
| `exec` succeeded | streamed stdout/stderr | exit code of the VM-side process |
| `exec` client disconnected mid-run | (no client output) — server kills the VM-side process | n/a |
| `shell` succeeded | interactive session | exit code of the VM-side shell |

---

## 8. Troubleshooting

**`Debug socket not found at /…/sandbox/debug.sock`**
The `raven` process is not running, or it was started with `sandbox.debug.enabled = false`, or with `sandbox.backend = "none"` (debug mode is intentionally ignored when there are no VMs to inspect — see `agent/loop.py`), or the debug server failed to bind. If the agent is running, look in its log/output for a `[Sandbox debug]` line — most commonly this happens because another `raven` process is already listening on the same socket path. Either stop the other process or set `tools.sandbox.debug.socket` to a different path for this one.

**`Cannot connect to debug socket at … : permission denied`**
The socket is created with mode `0600` and is owned by the user that launched `raven`. Run the CLI from that same user (or via `sudo -u <user>`).

**`Error: a command to execute is required.`**
You called `raven sandbox exec` without a command. Append the program and its arguments after `--vm <id>`.

**`exec` shows no output, then exits `0`**
The command ran successfully and produced nothing. Try `raven sandbox exec sh -c 'echo hi'` to confirm the channel works.

**`shell` leaves the terminal garbled after a crash**
The cleanup path normally restores `termios` and `SIGWINCH`. If the CLI was killed before cleanup ran (e.g. `kill -9`), run `reset` (or `stty sane`) to recover.

**`Message too large.`**
A single protocol message exceeded `sandbox.debug.maxMessageBytes` (default 1 MiB). For `exec`, this only happens with extremely large single chunks of stdout/stderr; redirect output to a file inside the VM instead, or increase `maxMessageBytes`.
