# Raven 🦞

> An agent framework designed around four pillars: **intelligent context management**, **proactivity**, **token efficiency**, and **skill self-evolution**.

English | [简体中文](README.zh-CN.md)

Raven is a ground-up redesign of the agent runtime — built on a battle-tested base (forked from the MIT-licensed [nanobot](https://github.com/HKUDS/nanobot) project) and extended with opinionated solutions to the four hardest problems every serious agent product eventually hits:

1. **上下文管理 · Context Management** — a *Curator* engine that autonomously decides what stays in the context window, archives the rest losslessly, and retrieves on demand.
2. **主动性 · Proactivity** — a *Sentinel* subsystem that runs alongside the agent loop, watches events, and decides when the agent should reach out first (without being annoying).
3. **节省 Token · Token Efficiency** — a *TokenWise* layer of cross-cutting strategies: prompt cache placement, tool-result lifecycle management, smart model routing, and real-time budget tracking.
4. **Skill 自进化 · Skill Self-Evolution** — a *SkillForge* closed loop: auto-detect reusable patterns from conversations, version and track skill performance, and evolve skills based on execution feedback.

---

## Status

**Pre-alpha**, under active development — APIs change without notice. The base runtime and all four feature engines have landed in code; maturity varies per engine.

| Layer | Status |
|------|--------|
| Base agent runtime (forked from nanobot) | ✅ Functional — CLI, channels, tools, scheduling, providers |
| Spine — per-turn backbone — + TUI-RPC terminal front-end | ✅ Functional |
| Context Management — Curator engine | ✅ Implemented (`legacy` + `curator` paths) |
| Proactivity — Sentinel + Scheduler | ✅ Implemented |
| Token Efficiency — TokenWise strategies | ✅ Implemented (tracking + cache on by default) |
| Skill Self-Evolution — SkillForge | ✅ Implemented |
| Eval engine (L3 task judge) | 🚧 Partial |

---

## Why Raven

Most open-source agent frameworks stop at "LLM + tools + loop." That works until you hit production, at which point:

- Context gets fat, the window overflows, and you start losing information — so you summarize, which loses more information.
- Every turn re-sends the same system prompt, the same skill summaries, the same tool definitions — burning tokens.
- The agent waits passively for instructions. It never says "hey, I noticed the deploy is stuck" or "you asked me to remind you about X."
- Skills are static markdown files. If the instructions don't match a new edge case, the skill just fails silently forever.

Raven takes each of these head-on. The four pillars are not add-ons — they are the framework.

---

## Architecture

Every turn flows through the **Spine** — a single backbone with one entry (`submit`) and one exit (`emit`), where per-conversation *lanes* are the unit of ordering and cancellation. The Spine is deliberately point-to-point, not a broadcast bus.

```
   Channels            ┌──────────────────────────────┐
   telegram, discord,  │            Spine             │
   slack, matrix, …    │   submit ─▶ per-conv lanes   │
        ▲              │              └─▶ emit        │
        │   TUI-RPC    └───────────────┬──────────────┘
        ▼   (terminal)                 │ one turn
   ┌──────────────┐          ┌─────────▼──────────┐
   │ front-ends   │          │     Agent loop     │
   └──────────────┘          │   tools · skills   │
                             └─────────┬──────────┘
        ┌───────────────┬─────────────┼──────────────┬───────────────┐
   ┌────▼─────┐   ┌──────▼──────┐ ┌────▼─────┐  ┌──────▼──────┐  ┌─────▼─────┐
   │ Context  │   │  Proactive  │ │TokenWise │  │   Memory    │  │   Eval    │
   │ Engine   │   │  Engine     │ │strategies│  │   Engine    │  │  Engine   │
   │ Curator/ │   │ Sentinel +  │ │cache·    │  │ SkillForge· │  │ L3 task   │
   │ legacy   │   │ Scheduler   │ │route·track│ │ EverOS·     │  │ judge     │
   └──────────┘   └─────────────┘ └──────────┘  │ consolidate │  └───────────┘
                                                 └─────────────┘
                             ┌────────────────┐
                             │  LLM Providers │
                             │ Anthropic/OAI/ │
                             │  Gemini / OR … │
                             └────────────────┘
```

**Design principle: pluggable engines behind config.** Each feature engine plugs in through config, and the novel ones default to off — a fresh install behaves like the base agent until you opt in (`context.engine = "legacy"`, `sentinel.enabled = false`, …). Engines coordinate through the Spine and explicit handoffs in the agent loop, not by importing one another.

### Repo layout

```
raven/
├── spine/              # Per-turn backbone: submit → per-conversation lanes → emit
├── agent/              # Agent loop, tools, hooks, subagents, context builder
├── channels/           # Platform adapters (telegram, discord, slack, matrix, whatsapp, …)
├── tui_rpc/            # Terminal front-end protocol (Request/Response + Notification)
├── providers/          # LLM provider adapters (Anthropic, OpenAI, Gemini, …)
├── context_engine/     # Context layer — legacy + Curator (Fast / Slow / Fail-Safe paths)
├── proactive_engine/   # Proactivity — Sentinel (event-driven) + Scheduler (cron / heartbeat)
├── memory_engine/      # Memory + skills — consolidation, SkillForge, EverOS, skill_local
├── eval_engine/        # L3 task judge / cognition coordination
├── token_wise/         # TokenWise strategies — usage tracking, cache placement, routing
├── routing/            # Model routing
├── skill_hub/          # Client for the remote skill marketplace
├── plugin/             # Plugin foundation
├── session/            # Session management (append-only JSONL)
├── auth/               # Authentication & authorization primitives
├── security/           # Network access control
├── sandbox/            # Isolated command execution (microVM / boxlite)
├── cli/                # `raven` command-line entry point
├── config/             # Config schema + feature blocks
├── templates/          # Default SOUL.md / USER.md / AGENTS.md
└── utils/              # Shared helpers
```

The repo also ships a `ui-tui/` package (the React/Ink terminal front-end that talks to `tui_rpc/`) and a `bridge/` (WhatsApp TypeScript bridge).

---

## Quick Start

### Requirements

- Python **3.11+**
- An API key for at least one LLM provider (Anthropic, OpenAI, OpenRouter, Gemini, DeepSeek, etc.)

### Install

```bash
git clone https://github.com/EverMind-AI/raven.git
cd Raven
pip install -e .
```

For channel integrations (Telegram, Discord, Slack, WhatsApp, …):

```bash
pip install -e ".[channels]"
```

For development (tests, linting):

```bash
pip install -e ".[dev]"
```

### Bootstrap your workspace

```bash
raven onboard
```

This creates `~/.raven/config.json` and a workspace at `~/.raven/workspace/` with default `SOUL.md`, `USER.md`, and `AGENTS.md` templates.

### Add your API key

Edit `~/.raven/config.json`:

```json
{
  "providers": {
    "anthropic": { "api_key": "sk-ant-..." }
  },
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-6"
    }
  }
}
```

### Chat

```bash
raven agent -m "Hello, who are you?"
```

Or interactive mode:

```bash
raven agent
```

### Run as a gateway (for chat platforms)

Enable a channel in config (`channels.telegram.enabled = true`, add token), then:

```bash
raven gateway
```

---

## Configuration

Raven's config extends the base agent config with feature blocks. All novel features default to **off** — a fresh install behaves exactly like the base agent until you opt in.

```json
{
  "agents":   { "defaults": { "model": "anthropic/claude-opus-4-6" } },
  "channels": { "telegram": { "enabled": false } },
  "providers": { "anthropic": { "api_key": "sk-ant-..." } },

  "context": {
    "engine": "legacy",
    "fast_path_threshold": 0.60,
    "curator_model": "gemini-2.5-flash"
  },

  "sentinel": {
    "enabled": false,
    "monitors": [],
    "nudge_policy": {
      "max_nudges_per_hour": 3,
      "quiet_hours": [23, 7]
    }
  },

  "token_wise": {
    "enabled": true,
    "usage_tracking": true,
    "cache_optimization": true,
    "smart_routing": { "enabled": false }
  },

  "skill_forge": {
    "enabled": false,
    "stats_tracking": true,
    "auto_detect": false,
    "auto_evolve": false
  }
}
```

See `raven/config/` for the full schema with every field documented.

### Enabling skill self-evolution

Completed `user → assistant` turns flow into a local extraction
pipeline that buffers them per session and **detects task boundaries
across turns** before distilling anything. A cheap per-turn classifier
asks "did the user just start a new task?"; only when a boundary is
found (or the session ends) does the buffered segment get compressed
into an `AgentCase`. Short exchanges, pure chit-chat with no tool
calls, or in-progress turns are dropped before any LLM call. Cases
below the quality floor stop there; the rest go through skill
extraction. Output lands in `<workspace>/.cache/skills.db` plus
materialized `SKILL.md` files at `<workspace>/skills/everos/<id>/`,
which the local BM25 pool picks up automatically. No external services
— just SQLite + your existing LLM provider.

```json
{
  "skill_forge": {
    "enabled": true,
    "evolve_model": "claude-opus-4-6",
    "detect_model": "gemini-2.5-flash",
    "everos": {
      "enabled": true
    }
  }
}
```

Two models drive the pipeline:

- `skill_forge.evolve_model` — heavyweight LLM used to distill
  `AgentCase`s and rewrite skills. Defaults to the active agent model
  when unset; pin a stronger model here for higher-quality rewrites.
- `skill_forge.detect_model` — cheap classifier for the per-turn
  boundary detector (multi-turn task split). Runs on every accumulated
  turn, so a small fast model (default `gemini-2.5-flash`) is
  intentional.

### Configuring media generation (image / speech / video)

Three media tools call [OpenRouter](https://openrouter.ai) to generate
media. They are **opt-in per tool**: a tool is exposed to the agent only
when you give it a `model` or an `api_key` under `tools.media.<tool>`.
Configuring OpenRouter as your chat provider alone does **not** enable
them — the agent never sees image/speech/video until you ask for it.

```json
{
  "providers": { "openrouter": { "api_key": "sk-or-..." } },
  "tools": {
    "media": {
      "image":  { "model": "google/gemini-2.5-flash-image" },
      "speech": { "model": "openai/gpt-audio-mini" },
      "video":  { "model": "kwaivgi/kling-v3.0-std" },
      "proxy": null,
      "output_subdir": "generated"
    }
  }
}
```

- **Key** — each configured tool defaults a missing key to
  `providers.openrouter.api_key`, so usually you set just a `model` to
  switch a tool on. Override per tool with `tools.media.<tool>.api_key`
  to use a separate key.
- `image_generate` — text-to-image (and image editing) via Nano Banana
  (`google/gemini-2.5-flash-image`). Saves a PNG under the workspace.
- `text_to_speech` — speech synthesis via `openai/gpt-audio-mini`.
  Outputs WAV with zero dependencies; mp3/opus/flac require `ffmpeg` on
  PATH and fall back to WAV when it is absent.
- `video_generate` — text-to-video via Kling (`kwaivgi/kling-v3.0-std`),
  an async job that takes a while and **requires postpaid billing /
  credits on your OpenRouter account**.

Generated files land in `<workspace>/<output_subdir>` (default
`generated/`). Set `tools.media.proxy` to route media calls through an
HTTP/SOCKS proxy.

---

## The Four Pillars

### 1. Context Management — the *Curator* engine

The context layer (`context_engine/`) is pluggable, with two implementations:

- **`legacy`** *(default)* — the base agent's `ContextBuilder` + Consolidation. When the prompt approaches the context window, old messages are summarized into memory notes and moved out of live context (lossy).
- **`curator`** — an internal, bounded agent loop that manages the window. Under pressure it archives messages **losslessly** to disk, retrieves them when relevant, and uses internal tools (`curator_check_budget`, `curator_archive_messages`, `curator_retrieve_archived`, `curator_build_context`, …) to compose the final window. A two-tier design:
  - **Fast Path** (history under the pressure threshold, default 60%): zero-LLM pass-through.
  - **Slow Path** (under pressure): a small-model agent loop (`gemini-2.5-flash` by default) decides what stays, validated by a deterministic assembler.
  - **Fail-Safe**: if the Slow Path errors or yields no valid plan, a deterministic Python fallback (protected + most-relevant + most-recent) produces a valid context.

### 2. Proactivity — the *Sentinel* subsystem

Proactivity lives in `proactive_engine/`, with two trigger paths:

- **Sentinel** *(event-driven)* — an attention pipeline (attention producers → predictor → trigger policy → executor → feedback) that decides when the agent should reach out unprompted.
- **Scheduler** *(time-driven)* — cron jobs and heartbeat.
- **Nudge Policy** — anti-spam guardrails: `max_nudges_per_hour`, `quiet_hours`, `min_interval_seconds`, cooldown on dismiss.
- A proactive action enters the agent loop as a turn of its own, routed as proactive context.

### 3. Token Efficiency — the *TokenWise* layer

TokenWise (`token_wise/`) is a set of cross-cutting `TokenStrategy` hooks, each individually enabled.

| Strategy | What it does | Typical saving |
|---------|--------------|----------------|
| `UsageTracker` | Records every LLM call's tokens + cost | — (observability) |
| `CacheOptimizer` | Places Anthropic `cache_control` breakpoints optimally | up to 75% input cost |
| `SystemAndTailCacheStrategy` | Alternative cache placement (system + rolling tail), for A/B against `CacheOptimizer` | (benchmark) |
| `SmartRouter` | Routes simple tasks to cheaper models (`haiku`, `gemini-flash`) | 40-70% per-request |

### 4. Skill Self-Evolution — *SkillForge*

SkillForge (`memory_engine/skill_forge/`) treats skills as procedural memory and runs a closed loop: `Detect → Create → Execute → Feedback → Evaluate → Evolve → Retire`.

- Skills live at `<workspace>/skills/<id>/SKILL.md` with enriched YAML frontmatter including `version`, `stats`, and `evolution_log`.
- **Detect**: a small-model check decides whether a conversation segment contains a reusable multi-step procedure worth saving (see *Enabling skill self-evolution* above).
- **Draft → Active gate**: auto-created skills start as `draft` and stay out of the skills summary until they succeed at least once, preventing noise.
- **Evolve**: when `success_rate` drops below a threshold over enough invocations, a stronger model rewrites the skill, preserving working logic; the previous version is snapshotted.
- **Retire**: long-unused skills are deprecated, then retired to archive.

A `skill_hub/` client can additionally pull skills from a remote marketplace.

---

## Development

### Run tests

```bash
uv run pytest -v
```

The suite spans 200+ test files covering the spine, the agent loop, channels, the feature engines, the config schema, and the CLI.

### Layout conventions

- **Engines coordinate through the Spine and explicit handoffs** — feature engines don't import one another directly.
- **Fail-safes are mandatory** — every component that calls an LLM has a deterministic fallback. No feature should crash the turn.
- **New features default off** — anything novel ships with `enabled = false`; only cheap, well-understood strategies (cache optimization, usage tracking) are on by default.

### Coding style

- Python 3.11+, `from __future__ import annotations` where helpful
- `uv` is the only package manager (`uv add`, `uv run`, `uv sync`)
- Ruff for linting; type hints throughout (`Literal`, `Protocol` where appropriate)
- Tests use `pytest` with `pytest-asyncio` (asyncio mode `auto`)

See `CLAUDE.md` for the full contribution constraints (branch naming, commit format, dependency and test rules).

---

## Credits & License

Raven is MIT-licensed. The base agent runtime (under `raven/agent/`, `raven/channels/`, `raven/cli/`, `raven/config/`, `raven/providers/`, `raven/routing/`, `raven/session/`, `raven/templates/`, `raven/utils/`) originated from the MIT-licensed [nanobot](https://github.com/HKUDS/nanobot) project by HKUDS. See `LICENSE` and `NOTICES.md` for details.

The feature engines (`context_engine/`, `proactive_engine/`, `token_wise/`, `memory_engine/`, `eval_engine/`, `spine/`, plus config extensions) are new to Raven.

Inspiration from the broader ecosystem — including [hermes-agent](https://github.com/NousResearch/hermes-agent) (Nous Research), [Letta / MemGPT](https://github.com/letta-ai/letta), and [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk) — informed the design.

---

## Contributing

Raven is in pre-alpha. APIs will change. If you're interested in contributing:

1. Open an issue before starting work so we can align on direction.
2. Read `CLAUDE.md` and match the layout conventions above.
3. Add tests alongside your change.
4. Document your module's contract in its docstring and update the relevant section of this README.

---

*Raven is built by EverMind.*
