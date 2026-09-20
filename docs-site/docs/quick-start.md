<p class="em-eyebrow">First run</p>

# Quick Start

<p class="em-standfirst">Install Raven, configure a model provider, and start using the WebUI.</p>

## Install Raven

Choose the installer for your operating system. It sets up a managed Raven
environment and adds the `raven` command to your shell.

### Linux, macOS, or WSL2

```bash
curl -fsSL https://raven.evermind.ai/install.sh | bash
```

### Windows PowerShell

```powershell
irm https://raven.evermind.ai/install.ps1 | iex
```

If Windows PowerShell 5.1 rejects the redirect, use the direct installer URL:

```powershell
irm https://raw.githubusercontent.com/EverMind-AI/Raven/refs/heads/main/install.ps1 | iex
```

### From a source checkout

To develop Raven or try unreleased changes, clone the repository and run the
installer locally:

```bash
git clone https://github.com/EverMind-AI/Raven.git
cd Raven
./install.sh
```

Running the local `install.sh` installs Raven and its bundled plugins in
editable mode, so they use your working tree. The TUI and WebUI are also built
from that checkout. Piping the installer to a shell installs the published
wheel, even from inside a clone. To select a local source directory for a piped
installation, set `RAVEN_LOCAL_SRC=<dir>`.

## Configure the first provider

Run the guided setup after installation:

```bash
raven onboard
```

Follow the prompts to configure a model provider and choose which built-in
agents to enable. You can later add or update providers in the WebUI under
**Settings > Model providers**.

## Start Raven

Launch the WebUI with the Raven engine running in the background:

```bash
raven web
```

The local page opens at `http://127.0.0.1:18792`. To stop the background
service, run:

```bash
raven web --stop
```

See [Self-Hosting](self-hosting.md) for Docker deployment and instructions for
building and running the server from source.
