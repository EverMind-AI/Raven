# scripts/boxlite_cli.py — Boxlite Image & VM Manager

CLI for managing boxlite OCI images and VMs from the command line.

---

## Setup

**Requirements:** Python 3.11+, [uv](https://docs.astral.sh/uv/)

```bash
# From the repo root — install all deps including the sandbox/boxlite extra
uv sync --extra sandbox
```

Verify:

```bash
uv run python -c "import boxlite; print(boxlite.__version__)"
# → 0.8.2
```

Platform requirements:
- **macOS:** Apple Silicon M1+, macOS 12+
- **Linux:** x86_64/ARM64 with KVM enabled and `/dev/kvm` accessible
- Windows: x86_64 WSL2 with KVM enabled and /dev/kvm accessible

---

## Usage

All commands are run from the repo root:

```bash
uv run python scripts/boxlite_cli.py [--home-dir PATH] <resource> <action> [options]
```

`--home-dir` overrides the boxlite runtime home directory (DB, images, layers).
It applies to every subcommand. Useful for inspecting VMs created by another
boxlite instance — for example, the boxlite runtime raven uses, which lives
under raven's data dir rather than the default `~/.boxlite`:

```bash
# Inspect raven-managed VMs
EC_HOME=$(uv run python -c \
  'from raven.config.paths import get_sandbox_dir; print(get_sandbox_dir("boxlite"))')
uv run python scripts/boxlite_cli.py --home-dir "$EC_HOME" vm ls
```

Precedence: `--home-dir` > `BOXLITE_HOME` env var > `~/.boxlite` (default).

---

## Image Commands

### `image ls` — List cached images

```bash
uv run python scripts/boxlite_cli.py image ls
```

```
IMAGE                 MANIFEST             CACHED AT            SIZE
----------------------------------------------------------------------------
debian:bookworm-slim  sha256:26d52380dd92  2026-04-24T06:17:12  26.8 MiB
python:slim           sha256:78a8215e9f35  2026-04-24T06:17:14  41.6 MiB
ubuntu:22.04          sha256:0124b5388c7c  2026-04-28T03:58:46  26.3 MiB
node:20-slim          sha256:10fc5f5f33cb  2026-04-28T04:17:03  67.9 MiB
```

---

### `image pull <image>` — Pull an image into the local cache

```bash
uv run python scripts/boxlite_cli.py image pull ubuntu:22.04
uv run python scripts/boxlite_cli.py image pull python:3.11-slim
uv run python scripts/boxlite_cli.py image pull ghcr.io/owner/repo:tag
```

Pulls by creating a minimal short-lived VM. The image is cached at the active home dir (`~/.boxlite/` by default, or `--home-dir` if set) and reused on subsequent pulls (instant) and VM creates.

#### Private registry authentication

Pass credentials with `--username` / `--password`, or via environment variables.
Credentials are written to `~/.docker/config.json` (the standard OCI credential store) and reused for all future pulls from the same registry without re-supplying them.

```bash
# CLI flags
uv run python scripts/boxlite_cli.py image pull ghcr.io/myorg/myimage:latest \
  --username myuser \
  --password ghp_xxxxxxxxxxxx

# Environment variables (safer — not visible in process list)
export BOXLITE_REGISTRY_USERNAME=myuser
export BOXLITE_REGISTRY_PASSWORD=ghp_xxxxxxxxxxxx
uv run python scripts/boxlite_cli.py image pull ghcr.io/myorg/myimage:latest

# Docker Hub private image
uv run python scripts/boxlite_cli.py image pull myorg/private-app:latest \
  --username myuser \
  --password dckr_pat_xxxxxxxxxxxx
```

> **Security note:** Prefer env vars over CLI flags. CLI flags appear in `ps` output and shell history; env vars do not.

Common registry auth keys written to `~/.docker/config.json`:

| Registry | Auth key |
|----------|----------|
| Docker Hub | `https://index.docker.io/v1/` |
| GitHub Container Registry | `ghcr.io` |
| Google Artifact Registry | `<region>-docker.pkg.dev` |
| AWS ECR | `<account>.dkr.ecr.<region>.amazonaws.com` |
| Self-hosted | `registry.example.com` or `registry.example.com:5000` |

---

### `image rm <image>` — Remove a cached image

```bash
uv run python scripts/boxlite_cli.py image rm ubuntu:22.04
```

Removes the image from the local cache and cleans up orphaned layer files. Layers shared with other cached images are preserved.

**Blocked if any VM (running or stopped) references the image:**

```
Error: 'ubuntu:22.04' is referenced by 1 VM(s): my-vm
Remove those VMs first, or pass --force to remove anyway.
```

Remove the VMs first, or use `--force` to remove anyway:

```bash
uv run python scripts/boxlite_cli.py image rm ubuntu:22.04 --force
```

---

## VM Commands

### `vm ls` — List all VMs

```bash
uv run python scripts/boxlite_cli.py vm ls
```

```
ID                          NAME      STATE       IMAGE         CPUS  MEM(MiB)
------------------------------------------------------------------------------
01JJNH8ABC123DEF456GHI789J  dev-box   Running     ubuntu:22.04  2     2048
01JJNH8XYZ987UVW654RST321Q  ci-node   Stopped     node:20-slim  1     1024
```

---

### `vm create` — Create a VM

```bash
# Minimal — create with defaults (2 vCPUs, 2048 MiB RAM, ephemeral disk)
uv run python scripts/boxlite_cli.py vm create --image ubuntu:22.04

# With a name and custom resources
uv run python scripts/boxlite_cli.py vm create \
  --image ubuntu:22.04 \
  --name dev-box \
  --cpus 4 \
  --memory 4096

# With a persistent disk
uv run python scripts/boxlite_cli.py vm create \
  --image python:3.11-slim \
  --name py-worker \
  --disk 20

# Create and boot immediately
uv run python scripts/boxlite_cli.py vm create \
  --image ubuntu:22.04 \
  --name dev-box \
  --start
```

VMs are created in a stopped state by default. Use `--start` to boot immediately, or call `vm start` afterwards.

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--image` | required | OCI image reference |
| `--name` | — | Optional unique name |
| `--cpus` | 2 | vCPU count |
| `--memory` | 2048 | RAM in MiB |
| `--disk` | ephemeral | Persistent disk in GB |
| `--start` | false | Boot immediately after creation |

---

### `vm start <id_or_name>` — Start a stopped VM

```bash
uv run python scripts/boxlite_cli.py vm start dev-box
uv run python scripts/boxlite_cli.py vm start 01JJNH8ABC123DEF456GHI789J
```

---

### `vm stop <id_or_name>` — Stop a running VM

```bash
uv run python scripts/boxlite_cli.py vm stop dev-box
```

The VM's state is preserved. Use `vm start` to restart it.

---

### `vm rm <id_or_name>` — Remove a VM

```bash
uv run python scripts/boxlite_cli.py vm rm dev-box
```

Fails if the VM is running. Use `--force` to stop it first:

```bash
uv run python scripts/boxlite_cli.py vm rm dev-box --force
```

---

### `vm shell <id_or_name>` — Open an interactive shell in a running VM

```bash
uv run python scripts/boxlite_cli.py vm shell dev-box
uv run python scripts/boxlite_cli.py vm shell 01JJNH8ABC123DEF456GHI789J
```

Attaches a full PTY interactive shell session to the running VM — similar to `docker exec -it <container> bash`. Your terminal is put into raw mode so keystrokes go directly to the VM.

```bash
# Use bash instead of the default /bin/sh
uv run python scripts/boxlite_cli.py vm shell dev-box --shell /bin/bash
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--shell` | `/bin/sh` | Shell binary to run inside the VM |

**Exit:** Type `exit` or press `Ctrl-D` to end the session. The terminal is fully restored afterwards.

**Terminal resize:** The PTY is resized automatically when you resize your terminal window (SIGWINCH forwarding).

**Requirement:** Stdin must be an interactive TTY. Piped or non-TTY use will print an error and exit.

> **Note:** `scripts/boxlite_cli.py` holds the boxlite runtime lock (`<home>/lock`) while running.
> `vm shell` therefore cannot be used at the same time as another process holding the lock for
> the same home dir — it is intended for standalone debugging of VMs created with
> `vm create --start`. To inspect raven-managed VMs, point `--home-dir` at raven's
> sandbox dir (see "Setup"); even then, only one process at a time may hold the lock.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BOXLITE_HOME` | `~/.boxlite` | Override the boxlite data directory (overridden by `--home-dir`) |
| `BOXLITE_REGISTRY_USERNAME` | — | Registry username for `image pull` |
| `BOXLITE_REGISTRY_PASSWORD` | — | Registry password / token for `image pull` |

```bash
BOXLITE_HOME=/custom/path uv run python scripts/boxlite_cli.py image ls
# or, equivalently and with higher precedence:
uv run python scripts/boxlite_cli.py --home-dir /custom/path image ls
```
