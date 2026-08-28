# Raven Developer Guide

## Build & Run from Source

The project uses **Python + `uv`** with `hatchling` as the build backend.

### 1. Install dependencies

```bash
cd /path/to/raven
uv sync
```

This creates/updates `.venv` with all core dependencies from `uv.lock`.

For optional extras:

```bash
uv sync --extra channels   # messaging integrations (Telegram, Slack, etc.)
uv sync --extra sandbox    # boxlite sandbox execution
uv sync --extra tools      # web/readability tools
# or all at once:
uv sync --all-extras
```

### 2. Install the package in editable mode

```bash
uv pip install -e .
```

This wires up the `raven` CLI script to your source code so changes take effect immediately.

### 3. Run the CLI

```bash
# via uv run (uses .venv automatically, no activation needed):
uv run raven --help

# or activate the venv first:
source .venv/bin/activate
raven --help
```

### 4. First-time setup

```bash
uv run raven onboard
```

Creates `~/.raven/config.json` and the workspace directory. Edit the config to add your API key (default provider: OpenRouter — get a key at https://openrouter.ai/keys).

### 5. Common commands

| Command | Description |
|---|---|
| `raven agent` | Start interactive chat session |
| `raven agent -m "Hello"` | Send a single message and exit |
| `raven gateway` | Start full server (all channels + heartbeat + cron) |
| `raven status` | Show config path, workspace, and API key status |
| `raven channels status` | Show which messaging channels are enabled |
| `raven provider login <name>` | Authenticate with an OAuth provider (for example `openai-codex`, `minimax-global`, or `minimax-cn`) |

### 6. Run tests

```bash
uv run pytest tests/
```

Requires Python ≥ 3.11. Test configuration is in `pyproject.toml` (`asyncio_mode = "auto"`).
