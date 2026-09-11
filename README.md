<div align="center" id="readme-top">

![Raven banner](https://github.com/user-attachments/assets/6c6f585a-21b6-4e7b-9187-acffe59d0c10)

<p align="center">
  <a href="https://x.com/evermind"><img src="https://img.shields.io/badge/EverMind-000000?labelColor=gray&style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://huggingface.co/EverMind-AI"><img src="https://img.shields.io/badge/HuggingFace-EverMind-F5C842?labelColor=gray&style=for-the-badge&logo=huggingface&logoColor=white" alt="Hugging Face"></a>
  <a href="https://discord.gg/gYep5nQRZJ"><img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdiscord.com%2Fapi%2Fv10%2Finvites%2FgYep5nQRZJ%3Fwith_counts%3Dtrue&query=%24.approximate_presence_count&suffix=%20online&label=Discord&color=404EED&labelColor=gray&style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/EverMind-AI/EverOS/discussions/67"><img src="https://img.shields.io/badge/WeCom-EverMind_Community-07C160?labelColor=gray&style=for-the-badge&logo=wechat&logoColor=white" alt="WeCom"></a>
</p>

[Website](https://raven.evermind.ai) · [中文](README.zh-CN.md)

</div>

<br>

# Raven

Raven is **The Self-Improving Agent Harness**, built on [EverOS](https://github.com/EverMind-AI/EverOS), with opt-in Deep Research for multi-source investigation.

Raven helps agents improve across runs by continuously refining the systems around them: tools, skills, memory, code execution, policies, and working environment. EverOS provides durable user memory, agent memory, and world knowledge across sessions, so successful workflows can evolve into reusable Agent Templates and digital workers.

**Update:** Raven added Deep Research. Enable it with `raven deep-research enable`
to give the agent access to MiroThinker-backed, multi-source research when a
task needs deeper investigation.

> Raven is pre-alpha. Interfaces and configuration may change quickly.

## 📊 Benchmarks

| Benchmark | Raven Result | Comparison |
| --- | --- | --- |
| Efficiency | `56.7%` at 27B; `58.1%` at 397B | Hermes `46.8%` / `47.9%`; `+9.9pp` at 27B |
| Self-evolution | Ranked `#1` on EvoAgentBench | `+6.2pp` over the next result across four methods |
| Proactivity | `0.60` F1 on ProAgentBench | `2.4x` Hermes/OpenClaw at `0.253` |

Results describe the published test configurations; model, task set, and evaluation protocol all affect outcomes.

https://github.com/user-attachments/assets/3c541dae-5852-447f-8ea6-c9877612ad57

## 🚀 Quick Start

### 🧰 Prerequisites

The installer brings its own Python toolchain and Node runtime, so the only
thing to have ready beforehand is one program raven cannot install for you:

| Program | Needed for | Install |
| --- | --- | --- |
| **LibreOffice** | Turning a deck into a PDF, which is how the deck agent renders, measures and previews one, and how any Office document is read as source material. Optional: without it a deck is still built and delivered, but nothing that looks at the rendered page runs. | `apt install libreoffice` / `brew install --cask libreoffice` / `winget install TheDocumentFoundation.LibreOffice` |

`raven doctor` reports whether it was found.

### 📦 Install

Linux, macOS, or WSL2:

```bash
curl -fsSL https://raven.evermind.ai/install.sh | bash
```

Native Windows PowerShell:

```powershell
irm https://raven.evermind.ai/install.ps1 | iex
```

Windows PowerShell 5.1 may reject the redirect. Use the direct installer URL instead:

```powershell
irm https://raw.githubusercontent.com/EverMind-AI/Raven/refs/heads/main/install.ps1 | iex
```

The agent products ship with raven itself: a wheel carries the `agents/`
product tree and copies it out to your raven home on first use, and a source
checkout reads the tree in place. Setup asks about each product and registers
the ones you take up, on the model it is tuned for or on this raven's LLM.
See [`agents/README.md`](agents/README.md).

### 🧭 Onboard and run

```bash
raven
```

That is the whole first run: with nothing configured yet, `raven` walks you through setup and then opens the TUI in the same session. To reconfigure later, run `raven onboard` explicitly.

The bilingual onboarding wizard configures seven areas without requiring manual edits to `~/.raven/config.json`:

1. LLM provider and model
2. Sandbox or execution location
3. Chat channels
4. EverOS long-term memory
5. Web access (pick a search vendor and a page reader, give each its key; the search key is checked with one real query)
6. Sub-agents shipped in this checkout
7. Cold-start import from other AI tools

Step 5 also mirrors every web vendor key into `~/.raven/env` (owner-only) and offers to add one
guarded `source` line to your shell rc, so new shells and the `cli` / `acp` sub-agents --
whose environment is captured from a login shell -- inherit them.

Provider setup includes an in-step connectivity check. Optional steps can be skipped and configured later. If setup is incomplete, run:

```bash
raven doctor
```

### ⬆️ Upgrade

```bash
raven upgrade --check
raven upgrade
```

Upgrades preserve configuration, sessions, and memory. Raven does not update automatically.

## 🔎 Deep Research

Deep Research gives Raven a dedicated path for open-ended questions that require broad web search, source reading, analysis, and multi-source cross-checking. It uses [MiroThinker](https://miromind.ai/) and returns a self-contained answer with inline citations and references.

Configure it during onboarding or later:

```bash
raven deep-research enable
raven deep-research get
```

Once configured, Raven can invoke `deep_research` when a task needs more than a quick lookup. Before a paid, minute-scale run, interactive surfaces ask whether to use Deep Research or regular search for that query.

Delivery adapts to where Raven is running:

- **CLI and TUI:** progress streams inline while Raven searches, reads pages, and runs analysis. The completed report is shown directly without being rewritten by the main model.
- **Gateway channels:** the run continues in the background and the completed report is delivered back to the originating conversation.
- **Local archive:** every completed result is saved under `<workspace>/deep_research/` for later use.

Use regular search for a single fact or URL. Use Deep Research for comparisons, landscape reviews, technical investigations, and questions where source agreement matters.

## 🔬 Tracing

Tracing makes Raven's reasoning path inspectable without sending trace data to a hosted service. Open the local dashboard with:

```bash
raven tracing
```

Each `session.turn` becomes a trace tree containing the work that happened beneath it:

- LLM calls, models, token usage, cost, latency, and errors
- Tool inputs and outputs
- Subagent runs and parent-child relationships
- Skill reads and injections
- Memory recall, storage, extraction, and consolidation
- Large prompts and results stored as out-of-line artifacts

Tracing is enabled by default and is designed to never interrupt Raven's control flow. Spans are stored locally at `~/.raven/traces/logs/audit-spans.log`; set `RAVEN_TRACING_DIR` to move the state directory or `RAVEN_TRACING=0` to disable recording.

The schema follows a small, versioned semantic contract. See the [Tracing Standard API](docs/TRACING_STANDARD_API.md) for span names, attributes, artifact behavior, and extension rules.

## 🧩 Core Systems

| System | What it adds |
| --- | --- |
| **EverOS memory** | Durable user memory, agent memory, and world knowledge across sessions |
| **Context Engine** | Explicit token budgets and a unified assembly pipeline that preserves the most useful context |
| **Proactivity** | Sentinel observations, scheduled work, nudge policy, and deferred decisions |
| **SkillForge** | Built-in, workspace, EverOS, and mirrored skills with retrieval, feedback, and evolution |
| **Evolver** | Reproducible evaluation loops for improving agents and reusable procedures |
| **Agent Templates** | Shareable starting points for specialized digital workers built on the same harness |

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## 🔌 Providers and Gateways

Raven supports API-key, OAuth, local, and OpenAI-compatible providers. The onboarding catalog includes OpenRouter, OpenAI, Anthropic, Gemini, MiniMax, DeepSeek, Z.ai, DashScope, Moonshot, VolcEngine, SiliconFlow, Groq, AiHubMix, Azure OpenAI, GitHub Copilot OAuth, OpenAI Codex OAuth, Ollama, and hosted vLLM.

Twelve gateway adapters connect Raven to Telegram, Slack, Discord, WhatsApp, Matrix, Feishu, WeCom, Mochat, QQ, DingTalk, Email, and WeChat.

```bash
raven channels list
raven channels enable <adapter>
raven gateway
```

## 📋 Command Reference

| Command | Purpose |
| --- | --- |
| `raven` or `raven tui` | Launch the terminal UI |
| `raven agent -m "..."` | Run a one-shot task |
| `raven onboard` | Configure providers, sandboxing, channels, memory, web tool keys, sub-agents, and import |
| `raven status` | Show configuration and runtime status |
| `raven doctor` | Diagnose provider and environment problems |
| `raven tracing` | Open the local trace dashboard |
| `raven tracing compact` | Fold duplicate trace artifacts to reclaim disk space |
| `raven sessions list` | Browse, resume, fork, export, or delete sessions |
| `raven skill list` | Inspect the local SkillForge catalog |
| `raven sentinel status` | Inspect proactive memory and scheduled nudges |
| `raven cron list` | Inspect scheduled jobs |
| `raven gateway` | Run messaging gateways |
| `raven gateway reload` / `status` / `stop` | Drive a running gateway through its control plane (rebuild from config without a restart, inspect the generation, stop gracefully) |
| `raven upgrade` | Upgrade a managed installation |

Run `raven --help` or `raven <command> --help` for the complete CLI surface.

## 🏠 Self-Hosting

Raven can run directly from a checkout or as a single Docker Compose service. The
Compose deployment serves the built page through nginx, keeps the Raven engine
and its child services in one container, and stores durable state in a named
volume.

### 📝 Prerequisites

For a Docker deployment, install Docker Engine and Docker Compose v2. For a
source deployment, install Python 3.12, `uv`, Node.js, and npm. A source
checkout also needs the repository dependencies installed before starting the
engine.

### 🚀 Start the server from source

From the repository root:

```bash
make install-deps
make build-ui
uv run raven web
```

`raven web` opens the local page and leaves the engine running after the
terminal exits. It defaults to `http://127.0.0.1:18792`. Use
`uv run raven web --foreground` when debugging, or `uv run raven web --stop` to
stop the resident engine. The first run can start without a configured model;
add one from **Settings > Models** or run `uv run raven onboard`.

To run only the engine without the browser launcher, use
`uv run raven gateway`.

### 🐳 Start with Docker Compose

The repository Compose setup builds the page and Python environment as part of
the image, so no separate host-side build is required:

```bash
cd docker
docker compose up --build
```

Open <http://127.0.0.1:18793>. The Compose container runs the full `gateway`
engine so providers added from **Settings > Models** are available on the next
turn without restarting.

For the detailed container layout, sign-in flow, provider setup, and operational
notes, see [`docker/README.md`](docker/README.md).

### ⚙️ Configuration

Docker reads committed defaults from [`docker/.env`](docker/.env), then loads
the optional, git-ignored `docker/.env.local` over them.
Put credentials and deployment-specific overrides in `.env.local`, not in the
committed file. Common settings include:

| Variable | Purpose |
| --- | --- |
| `RAVEN_WEB_PORT` | Host port published by Compose (default `18793`) |
| `RAVEN_AUTO_LOGIN` | Automatically sign in local browsers; set to `0` for remote exposure |
| `RAVEN_EXTRAS` | Optional image extras such as `channels`, `tools`, `sandbox`, `browser`, or `eval` |
| `RAVEN_PLUGINS` | Bundled plugins to install in the image, including `everos-memory` |
| `RAVEN_PROVIDER` | Optional provider seeded into `config.json` at container startup |
| `RAVEN_API_KEY` | Optional provider key; local providers may leave it empty |
| `RAVEN_API_BASE` | Optional custom endpoint, sufficient by itself for keyless local providers |

Raven stores its configuration, sessions, workspace, logs, and memory under
`RAVEN_HOME`. The Compose image maps this to `/data` through the `raven-data`
volume. Keep that volume for upgrades and restarts; `docker compose down -v`
deletes it and its data.

### 🛠️ Build a Docker image

Build the image using the Makefile target:

```bash
make docker-build
```

The default tag is `raven:local`. To select a different tag or optional
dependency set:

```bash
make docker-build DOCKER_IMAGE=raven:dev
docker build -t raven:local --build-arg RAVEN_EXTRAS="channels,tools,sandbox" .
```

Run the locally built image through Compose by exporting
`RAVEN_IMAGE=raven:local` (or prefixing the command with that assignment) and
running `docker compose up` from `docker/`. The Makefile shortcut is
`RAVEN_IMAGE=raven:local make docker-up`. Stop the stack with `make docker-down`.

## 📚 Documentation

- [Documentation index](docs/README.md)
- [Developer workflow](docs/dev.md)
- [Tracing Standard API](docs/TRACING_STANDARD_API.md)
- [Sandbox usage](docs/sandbox/usage.md)
- [Memory plugin architecture](docs/memory-plugin-architecture.md)
- [Self-evolution loop mapping](docs/specs/self-evolution-loop-raven-mapping.md)
- [Proactivity implementation](docs/Proactivity-Implementation.md)

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## 🗂️ Repo layout

The top-level packages under `raven/`, in one line each. This list is the
canonical set of commit scopes (see `AGENTS.md`); a change living wholly in a
top-level tree outside `raven/` uses that tree as its scope instead (`agents`,
`evolver`, `ui`, ...). Layer seats (which package may
import which) are recorded under **Layer Seats** in `CONTEXT.md`, routed from
`CONTEXT-MAP.md`; this section only says what each package does.

| Package | What it is |
|---|---|
| `acp` | ACP server side: raven as an agent another host can talk to |
| `acp_client` | ACP client side: raven driving a third-party local agent as a sub-agent backend |
| `agent` | The agent loop, its tools, and sub-agent orchestration |
| `auth` | Authentication and authorization primitives |
| `browser` | Browser automation and its outbound-address policy |
| `channels` | Per-service channel adapters (telegram, discord, feishu, ...) and the channel contract |
| `cli` | The `raven` command-line surface |
| `config` | Config schema, loader, migrations, and update helpers |
| `contracts` | The papers: declared shapes, two promise tiers, no machinery |
| `context_engine` | Context assembly for a turn |
| `core` | Assembly root: the *_stack builders and the admission door |
| `eval_engine` | Evaluation harness |
| `gateway` | Daemon plumbing: channel manager, outlet, live probe, run lock |
| `i18n` | User-facing text in the user's language |
| `importer` | External data import |
| `knowledge` | Knowledge base service |
| `market` | Plugin market: catalog, trust, install, ledger |
| `mcp` | MCP client machinery |
| `memory_engine` | Long-term memory engine |
| `home` | Where raven keeps everything: the one address resolver |
| `observability` | What a raven span means: the attribute vocabulary and the usage it reports |
| `ops` | Machine registry and on-call operations |
| `permissions` | The gate at the tool dispatch door: which calls run, ask, or are refused |
| `playbook` | Playbook runtime |
| `plugins` | Plugin discovery, manifests, registry, and bundled plugins |
| `proactive_engine` | Cron, heartbeat, sentinel: turns raven starts itself |
| `providers` | LLM provider pool and resolution |
| `routing` | Model routing |
| `rpc` | The RPC surface (TUI and tools talk here) |
| `sandbox` | Execution sandboxing |
| `security` | Outbound address policy and prompt-injection fences |
| `session` | Session export and titles |
| `skill_hub` | Skill hub: client, install engine, policy and install audit |
| `spine` | The kernel: submit, lanes, cancel, emit, delivery |
| `templates` | Packaged data assets (no Python) |
| `token_wise` | Token efficiency: cache optimizer, usage tracker |
| `tracing` | Span capture: context, the instrument decorator, the store |
| `trajectory` | Turn trajectory store and verdicts |
| `updates` | The install's own lifecycle: release lookup, upgrade plan and handoff, update nudge |
| `utils` | Shared helpers, including the atomic write primitive |

## 🏗️ Architecture

```text
CLI / TUI / Messaging Gateways
              |
              v
          RPC / Spine
              |
              v
           Agent Loop
      +-------+-------+
      |       |       |
  Providers  Tools  Subagents
      |       |       |
      +--- Context Engine ---+
              |
      +-------+--------+
      |                |
 EverOS Memory     SkillForge
      |                |
      +--- Proactivity + Evolver
```

The Python runtime and React/Ink TUI communicate only through the typed RPC contract. The Spine carries runtime events, while the Agent Loop coordinates providers, tools, context, memory, skills, subagents, and proactive work.

Key directories:

```text
raven/
├── spine/              # Per-turn backbone: submit -> lanes -> emit
├── contracts/          # Papers: the interfaces every shelf implements
├── core/               # Assembly root: build_runtime and the *_stack builders
├── agent/              # Agent loop, tools, hooks, subagents, context builder
├── channels/           # Telegram, Discord, Slack, Matrix, WhatsApp, WeCom, ...
├── gateway/            # Daemon plumbing: channel manager, outlet, generations, lock
├── rpc/                # Python side of the native TUI protocol
├── providers/          # LLM provider adapters
├── context_engine/     # Context assembly and Curator path
├── proactive_engine/   # Sentinel, scheduler, nudges, feedback
├── memory_engine/      # EverOS memory, local skills, SkillForge
├── playbook/           # Stored orchestrations: library, match funnel, executor
├── token_wise/         # Usage tracking and cache placement
├── tracing/            # Span capture (the dashboard lives in cli/tracing_viewer/)
├── observability/      # The span vocabulary a standalone kernel may not hold
├── home.py             # RAVEN_HOME and the config path, resolved once
├── sandbox/            # Isolated command execution
├── security/           # Trust boundaries and network checks
├── cli/                # `raven` command line entry point
└── config/             # Config schema and update helpers

ui-tui/                 # React/Ink native terminal UI
bridge/                 # WhatsApp TypeScript bridge
benchmarks/             # Benchmark adapters, including AppWorld evolver wiring
evolver/                # Benchmark-driven harness self-evolution: a tool over the library, not in the wheel
agents/                 # Product definitions served over ACP: launcher + rendered config + product plugins
plugins-dist/           # Standalone plugin distributions (everos-memory, ppt-engine) on the raven.plugins entry-point group
schemas/                # Editor-facing JSON Schemas exported from the pydantic models (scripts/export_agent_schemas.py)
```

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## 🌐 EverMind Ecosystem

Raven is part of the [EverMind](https://evermind.ai/) open-source ecosystem. Explore [EverOS](https://github.com/EverMind-AI/EverOS), [EverAlgo](https://github.com/EverMind-AI/EverAlgo), [HyperMem](https://github.com/EverMind-AI/HyperMem), [EvoAgentBench](https://github.com/EverMind-AI/EvoAgentBench), [EverMemBench](https://github.com/EverMind-AI/EverMemBench), and [EverMe](https://github.com/EverMind-AI/EverMe).

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## 🤝 Contributing

Issues and pull requests are welcome. Start with the [developer workflow](docs/dev.md), follow [AGENTS.md](AGENTS.md) for repository rules, and use [GitHub Discussions](https://github.com/EverMind-AI/Raven/discussions) for design conversations.

## ⚖️ License

[Apache License 2.0](LICENSE)
