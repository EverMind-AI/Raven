# Development

Build, run, and test Raven from a local source checkout. The project uses
**Python with `uv`** for dependency management and `hatchling` for packaging.

## System prerequisites

`uv` installs Raven's Python dependencies. Install **LibreOffice** separately
to convert slide decks and other supported office documents to PDF. The deck
engine uses this conversion for rendering, layout measurements, and previews;
the gateway displays the resulting PDF. Without LibreOffice, decks can still
be generated, but rendering-dependent steps are unavailable and the relevant
integration tests are skipped.

```bash
apt install libreoffice                          # Debian / Ubuntu
brew install --cask libreoffice                  # macOS
winget install TheDocumentFoundation.LibreOffice # Windows
```

The browser tool also needs a separate Chromium binary. Install it from your
source checkout:

```bash
uv sync --all-extras && uv run playwright install chromium
```

`install.sh` handles the browser download for managed installations.
`raven doctor` reports both dependencies under
**External tools**.

## Install dependencies

```bash
cd /path/to/raven
uv sync
```

This creates or updates `.venv` with every core dependency from `uv.lock`.

Optional extras:

```bash
uv sync --extra channels   # messaging integrations (Telegram, Slack, and others)
uv sync --extra sandbox    # boxlite sandbox execution
uv sync --extra tools      # web and readability tools
uv sync --all-extras       # all of them at once
```

## Install the package in editable mode

```bash
uv pip install -e .
```

`uv sync` installs the project in editable mode by default. The command above
reinstalls it explicitly, keeping the `raven` command linked to your source tree.

## Run the CLI

Use `uv run` to run commands in the project environment. The activation example
below uses a POSIX shell; on Windows, use the `uv run` form.

```bash
# through uv run, which uses .venv without activating it:
uv run raven --help

# or activate the environment first:
source .venv/bin/activate
raven --help
```

## First-time setup

```bash
uv run raven onboard
```

Follow the setup wizard to choose a model provider, configure credentials, and
select a model. By default, Raven stores its configuration in
`~/.raven/config.json` and creates a workspace directory during setup.

## Common commands

| Command | Description |
|---|---|
| `raven tui` | Start the interactive chat TUI |
| `raven agent -m "Hello"` | Send a single message and exit |
| `raven gateway` | Start the gateway with enabled messaging channels, heartbeat, and scheduled tasks |
| `raven status` | Show the config path, workspace, and API key status |
| `raven channels status` | Show which messaging channels are enabled |
| `raven provider login <name>` | Authenticate with an OAuth provider, for example `openai-codex`, `minimax-global`, or `minimax-cn` |

See [Command Reference](commands.md) for an overview of Raven's commands.

## Run tests

```bash
uv run pytest tests/
```

Tests require Python 3.12 or newer. Tests that render slide decks also require
LibreOffice and are skipped when it is unavailable. Pytest settings are defined
in `pyproject.toml`, including `asyncio_mode = "auto"` for asynchronous tests.
