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

Windows PowerShell 5.1, the version built into Windows, cannot follow that URL's
redirect and stops with `(308) Permanent Redirect`. If you see that error, use the
direct installer URL:

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

### When a download host cannot be reached

The installers, `raven resources`, `raven upgrade`, and the agents and MCP
servers Raven starts through `uvx` or `npx` fetch from their usual hosts first.
When a host cannot be reached at all, the download comes from a mirror in
mainland China instead: hf-mirror.com for Hugging Face, jsDelivr for files on
GitHub, the TUNA mirror for PyPI and LibreOffice, and npmmirror for Node.js,
npm, Playwright and the Python builds uv installs. An HTTP error from the
usual host does not switch hosts, and neither does a certificate that fails
verification. Behind a proxy, the proxy refusing to connect to the host (a 403
or a 407) does not switch hosts either; a proxy that answers with a 5xx, or a
SOCKS proxy that cannot connect, counts as a host that cannot be reached.
uv's own installer and Playwright's Chromium download do not say why they
failed, so the hosts they download from are asked, and the mirror stands in
only when none of them can be reached. The LibreOffice and font downloads,
checked against a pinned digest, try the mirror after their usual hosts.

A host you configured yourself is kept: `HF_ENDPOINT`, a uv index in
`UV_DEFAULT_INDEX` or `uv.toml`, a download source for uv's installer or for
the Pythons uv installs (such as `UV_INSTALLER_GITHUB_BASE_URL` or
`UV_PYTHON_INSTALL_MIRROR`), `PLAYWRIGHT_DOWNLOAD_HOST`, or an npm registry set
anywhere npm reads one. A `uvx` or `npx` launch switches only when the mirror
can be reached, so a package already in its cache still starts offline. For
`uvx`, the mirror goes on that launch's command line, where the tool it starts
never sees it. npx would hand a registry to everything it starts, where it
would replace the registry a project names for itself, so in that case Raven
does not run npx: it installs the package from the mirror into a directory of
its own (`~/.raven/cache/npx`; a Raven node keeps its own under the node's
directory) and starts the package's command from there, in the environment the
launch was given. A package your project or npm's global installs already
provide still starts through npx, and so does a launch in a project whose
`.npmrc` names a registry. Set `RAVEN_NO_MIRROR=1` to keep every download on
its usual host. The switch is read in the environment a launch is given as
well as in Raven's own, and a Raven node is told the host's value, so an agent
that runs on another machine keeps its own host too. Raven's own release files
come from GitHub Releases, which has no mirror.

## Update Raven

Update Raven the same way you installed it. Running an installer again stops
the running WebUI and finishes by starting a new one in the foreground: press
Ctrl-C to stop it, then run `raven web` to keep Raven running in the
background. Your settings and conversations in `~/.raven` are kept.

### Update from the page

When the bottom of the sidebar says a new version is available, click it and
confirm. Raven shows the download's progress, installs the new version,
restarts, and the page reloads by itself. Your settings and conversations are
kept. While a turn, a sub-agent or a question waiting for an answer is still
running, including in an IM channel, the page asks you to let it finish first.
A Raven started by hand with `raven gateway` or `raven web --foreground` has
nothing to restart it, so update that one from the command line below.

### Update a one-line install

Run the same installer again. It installs the newest release over the current
one and starts the WebUI again when it finishes.

To update from the command line instead:

```bash
raven web --stop
raven upgrade
```

On Linux and macOS `raven upgrade` runs the install in the foreground and
returns when it is done. On native Windows it hands the install to a separate
helper and returns at once; wait for the helper's completion message before
going on. Then start Raven again:

```bash
raven web
```

`raven upgrade` installs the newest release but does not restart a Raven that
is already running, so stop the WebUI before you upgrade and start it again
afterwards. To check whether a newer release exists without installing it, run
`raven upgrade --check`.

### Update a source checkout

Pull the latest changes and run the local installer again (`.\install.ps1` in
PowerShell):

```bash
git pull
./install.sh
```

The installer reinstalls the checkout in editable mode, rebuilds the TUI and
WebUI only when their sources have changed, and starts the WebUI again.
`raven upgrade` does not update a source checkout, because the code to update
is the checkout itself.

## Configure the first provider

Run the guided setup after installation:

```bash
raven onboard
```

Follow the prompts to configure a model provider and choose which built-in
agents to enable. You can later add or update providers in the WebUI under
**Settings > Model providers**.

### Behind a corporate proxy or private CA

Raven checks HTTPS certificates against your operating system's certificate
store, so a root certificate your IT department installed there is trusted
without extra settings. If a provider check reports `certificate_untrusted`,
add the root certificate that signs your gateway or proxy to that store.

On WSL2 the Linux distribution keeps its own store and does not see the
certificates installed in Windows. Import the root once inside WSL:

```bash
sudo cp corporate-ca.pem /usr/local/share/ca-certificates/corporate-ca.crt
sudo update-ca-certificates
```

The EverOS memory server and Node-based agents run as separate programs that
read environment variables instead. Export these in your shell profile, not
only in the current terminal, so every process Raven starts receives them:

```bash
export SSL_CERT_FILE=/path/to/ca-bundle.pem
export NODE_EXTRA_CA_CERTS=/path/to/corporate-ca.pem
```

`SSL_CERT_FILE` replaces the whole list of trusted roots, so point it at a
complete bundle that also holds your corporate root, and never at a path that
does not exist: httpx, which model calls and provider checks go through,
refuses to start with one. On Debian, Ubuntu and WSL,
`/etc/ssl/certs/ca-certificates.crt` is that bundle once
`update-ca-certificates` has run. On macOS, build one first:

```bash
cat /etc/ssl/cert.pem corporate-ca.pem > ~/ca-bundle.pem
```

On macOS and Windows the operating system also applies its own certificate
policy; macOS, for example, refuses a server certificate valid for more than
825 days, or one not marked for server authentication, even when it trusts the
root. To go back to each library's own certificate list, export
`RAVEN_NO_SYSTEM_CA=1` in the same profile.

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
