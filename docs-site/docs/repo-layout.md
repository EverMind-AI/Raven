# Repo Layout

The shared Python runtime is in `raven/`. The repository also contains agent
definitions, plugin distributions, frontends, and development tools.

Key directories:

```text
raven/                 # Shared runtime, feature engines, and CLI/RPC/ACP surfaces
agents/                # Specialized agents assembled from installed Raven and plugins
plugins-dist/          # everos-memory, design-engine, and ppt-engine distributions
ui-web/                # Browser UI, also used by the desktop window
ui-tui/                # React/Ink terminal UI
rpc-schema/            # Shared OpenRPC contract for interactive clients
schemas/               # Generated agent and plugin JSON Schemas
bridge/                # WhatsApp TypeScript bridge
evolver/               # Benchmark-driven harness self-evolution tooling
benchmarks/            # Benchmark adapters and evaluation integrations
docker/                # Container deployment and Compose configuration
tests/                 # Unit, integration, and architecture contract tests
scripts/               # Build, packaging, code generation, and repository checks
docs/                  # Engineering references and design specifications
docs-site/             # Bilingual user documentation and site configuration
```

The runtime packages and modules below define the canonical commit scopes for
changes under `raven/`. For changes elsewhere, use the relevant directory or
distribution scope defined in `commitlint.config.cjs`. `AGENTS.md` defines the
repository's commit conventions.

| Package or module | Responsibility |
|---|---|
| `a2a` | A2A server interface for serving Raven to peer agents over the Agent2Agent protocol |
| `a2a_client` | A2A client for calling configured remote agents, and the outbound origin boundary |
| `acp` | ACP server interface for exposing Raven to external agent hosts |
| `acp_client` | ACP client, capability negotiation, and adapters for third-party agent events |
| `agent` | Agent Loop, Harness Modules, tool execution, and subagent orchestration |
| `auth` | Authentication and authorization primitives |
| `browser` | Browser automation, session management, and navigation checks |
| `channels` | Messaging adapters and their shared channel contract |
| `cli` | Command-line entry points, setup, and service launchers |
| `config` | Configuration schemas, loading, migrations, admission, and controlled updates |
| `contracts` | Papers: declared interfaces and data shapes shared across runtime components |
| `context_engine` | Context assembly, token budgets, and conversation compaction |
| `core` | Assembly Root: runtime Generations and component assembly |
| `eval_engine` | Evaluation hooks for task completion, iteration feedback, and tool auditing |
| `gateway` | Channel lifecycle, runtime generation swaps, event delivery, and process coordination |
| `home` | Shared `RAVEN_HOME` and configuration-path resolution (`home.py`) |
| `i18n` | Language catalogs, translations, and prompt localization |
| `importer` | Cold-start import from other AI tools |
| `knowledge` | Document ingestion, indexing, and retrieval for user knowledge bases |
| `market` | PlugHub catalog, trust checks, installation, and contribution ledgers |
| `mcp` | MCP server connections and tool integration |
| `memory_engine` | Memory recall and consolidation, local skills, and SkillForge retrieval |
| `observability` | Span semantics, attribute extraction, and usage attribution |
| `ops` | Local and remote machine registry and execution transports |
| `permissions` | Tool-call decisions: allow, request approval, or deny |
| `playbook` | Reusable workflow library, validation, generation, and execution |
| `plugins` | Plugin manifests, discovery, contribution registry, and bundled plugins |
| `proactive_engine` | Sentinel event processing, cron scheduling, heartbeat, and proactive decisions |
| `providers` | LLM adapters, provider pool, and model-to-provider binding |
| `routing` | Task classification and model selection by quality and cost |
| `rpc` | Shared typed RPC methods, streaming events, and the gateway control interface |
| `sandbox` | Isolated execution, VM lifecycle, and debugging tools |
| `security` | Outbound address policy and prompt-injection fences |
| `session` | Conversation storage, session resolution, titles, and transcript export |
| `skill_hub` | SkillHub search, skill retrieval, bundle installation, and install policy |
| `spine` | Turn scheduling, concurrency lanes, cancellation, and event delivery |
| `templates` | Packaged workspace files, prompt packs, and agent scaffolding templates |
| `token_wise` | Token usage, pricing, prompt caching, and efficiency strategies |
| `tracing` | Span capture, instrumentation, trace storage, and artifact management |
| `trajectory` | Execution bundles, replay, redaction, outcome labels, and regression cassettes |
| `updates` | Release discovery, upgrade planning, installation handoff, and update notices |
| `utils` | Shared utilities, including atomic file writes |
