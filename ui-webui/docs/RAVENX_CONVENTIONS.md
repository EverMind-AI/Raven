# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project context

This repo is a working copy of **AgentScope 2.0** (`agentscope`, currently `v2.0.4`), a production-oriented agent framework. It is the base on which the **RavenX** multi-agent system is being built: a single unified agent entry point that dispatches to heterogeneous domain sub-agents, where those sub-agents use different internal architectures and are preferably invoked via the command line. The framework primitives below are the plug-in points for that layer — see "Where RavenX sub-agents attach".

## Commands

```bash
# Setup (Python 3.11+ required)
uv pip install -e ".[dev]"     # dev extra pulls in the full deps + test/lint tooling
pre-commit install

# Lint / format — black (line length 79), flake8, pylint, mypy, docstring checks
pre-commit run --all-files

# Tests
pytest tests                                        # full suite
pytest tests/agent_basic_test.py                    # one file
pytest tests/agent_basic_test.py::ClassName::test_x # one test
coverage run -m pytest tests && coverage report -m  # what CI runs

# Run the reference agent service (needs Redis on localhost:6379)
cd examples/agent_service && python main.py         # FastAPI backend on :8000
cd examples/web_ui && pnpm install && pnpm dev       # web UI (separate terminal)
./start_webapp.sh                                    # one-click: redis + raven gateway(:8765) + service(:8000) + UI(:5173)

# Web UI frontend (examples/web_ui/frontend) — NO JS unit-test runner; gate FE changes on:
pnpm -C examples/web_ui/frontend lint                # eslint: 0 errors required (~18 pre-existing warnings are ok)
pnpm -C examples/web_ui/frontend build               # tsc -b + vite build
```

Test files are named `tests/*_test.py`. Optional dependencies are grouped as extras in `pyproject.toml` (`models`, `service`, `workspace`, `rag`, `mem0`, `reme`, ...); `dev` includes everything.

## Architecture

The installable package is `src/agentscope/`. **All internal module files, classes, and helpers are `_`-prefixed**; the public API is what each subpackage re-exports through its `__init__.py`. To learn a subpackage's surface, read its `__init__.py` first — not the `_xxx.py` implementation files.

**`agent/_agent.py` — the unified `Agent`.** A single `Agent` class implements a streaming reason–act (ReAct) loop. The two entry points are `reply()` (returns a final `Msg`) and `reply_stream()` (yields `AgentEvent`s). An agent is *composed*, not subclassed: it takes a `model` (`ChatModelBase`), a `toolkit` (`Toolkit`), a list of `middlewares`, config objects (`ModelConfig`/`ContextConfig`/`ReActConfig`), a `PermissionEngine`, and an optional context `offloader`. Behavior is customized by inserting **middlewares**, which wrap each phase (reply, reasoning, acting, model-call, system-prompt, compress-context) using a chain-of-responsibility pattern (`execute_chain` → `next_handler`). Prefer a middleware over editing the loop.

**`tool/` — the `Toolkit`.** `Toolkit` is the single registry for tools, MCP tools, and skills. A tool is a `ToolBase` subclass defining `name`, `description`, `input_schema`, and returning a `ToolResponse`. Built-ins are Claude-Code-style: `Bash`, `Read`/`Write`/`Edit`, `Glob`/`Grep`, plus the `Task*` planning tools. `MCPTool` and `FunctionTool` adapt external tools into the toolkit. Tools execute against a pluggable `BackendBase`/`LocalBackend`, which is how they run inside a sandbox.

**`model/` + `formatter/` — provider abstraction.** Each provider (Anthropic, DashScope, DeepSeek, Gemini, Ollama, OpenAI chat + response, xAI, Moonshot) has a `*ChatModel` class and a matching formatter that converts AgentScope `Msg` objects into that provider's wire format. Provider SDKs are imported lazily.

**`app/` — the multi-tenant service and the built-in team model.** `create_app()` (in `app/_app.py`) builds a FastAPI service with per-tenant / per-session isolation. This is where AgentScope's own multi-agent story lives: a **leader agent dynamically spawns worker sub-agents** through the team tools in `app/_tool/` — `TeamCreate`, `AgentCreate` (spawns a worker and delivers a `prompt` as its first message, so the worker starts immediately), `AgentInvite`, `TeamSay` (the *only* inter-member communication channel), `TeamDelete`. A worker's role and permissions come from a `SubAgentTemplate` (system-prompt template + `PermissionContext`), registered via `create_app(custom_subagent_templates=[...])` — see `examples/agent_service/main.py`. The app is wired from `storage` (Redis), a `message_bus` (in-memory or Redis), a `workspace_manager`, and an optional `knowledge_base_manager` (RAG).

**`workspace/` — isolated execution.** Sandbox backends for tool/code execution: local, Docker, E2B, OpenSandbox, Daytona, K8s, plus an MCP gateway and an offload protocol for long-running/background tools.

**`middleware/` — cross-cutting hooks.** Budget limiting, long-term memory (Mem0, ReMe), RAG, OpenTelemetry tracing, and TTS are all implemented as middlewares.

**`mcp/` — external processes.** `MCPClient` connects the agent to MCP servers. `StdioMCPConfig(command=..., args=...)` launches an external server as a **subprocess** (the reference service wires up `@playwright/mcp` this way); `HttpMCPConfig` connects over HTTP.

### Where RavenX sub-agents attach

For the unified-entry → CLI-invoked domain sub-agent goal, three integration points exist, in rough order of directness:

1. **MCP over stdio** (`StdioMCPConfig`) — wrap each external/domain agent CLI as an MCP server subprocess and register it on the leader's `Toolkit`. Cleanest fit for "different architecture, command-line invoked".
2. **Custom `ToolBase` / `Bash`** — wrap a CLI directly as a tool when it isn't (or can't be) an MCP server.
3. **Team `AgentCreate`** — for sub-agents that are themselves AgentScope agents coordinated by a leader in the `app/` service.

**`raven` as the main agent (the only mode).** The service drives every chat turn through `RavenGatewayAgent`, a WebSocket client to a persistent `raven gateway` web channel, translating spine-wire events into AgentScope events. The legacy log-parsing bridge and the AgentScope leader fallback have both been removed, along with the `RAVEN_BRIDGE` switch. raven delegates to heterogeneous sub-agents through its own config (`subagents.thirdParty` in `~/.raven/config.json`).

## Conventions (enforced in review and CI)

- **Encapsulation:** internal files/classes/functions are `_`-prefixed and exposed only through `__init__.py`.
- **Lazy imports:** third-party libraries (anything not in `[project.dependencies]`) must be imported at their point of use, never at file top. For base-class imports use the factory pattern (`def get_xxx_cls(): from x import Base; class Y(Base): ...; return Y`).
- **Docstrings:** English only, following the strict `Args:`/`Returns:` template with backtick-typed params; use reStructuredText (`.. note::`, `.. code-block::`) for rich content.
- **Dependencies:** new deps go into the *correct* `pyproject.toml` extra; do not add non-core deps to the minimal `dependencies` list.
- **Tests:** assertions should compare the **whole data structure** (not field-by-field); for random/nondeterministic fields use `AnyString`/`AnyValue` from `tests/utils.py`.
- **PR titles:** Conventional Commits — `feat/fix/docs/ci/refactor/test/chore/perf/style/build/revert(scope): description`.
- Do not skip pre-commit hooks or disable checks file-wide; fix the code instead.
- **Sub-agent / dispatch CLI prompts are never a bare argv token** (argv flag-injection; also breaks `-`-leading & multi-line prompts). Deliver on **stdin** by omitting `{prompt}` from the command template (the dispatch code then feeds stdin — e.g. claude `-p`, codex `exec`); `raven` has no stdin, so bind the value with the single-token `--message={task}` `=` form.
- **Frontend (`web_ui`):** edit i18n `src/i18n/locales/*.json` with *targeted* text edits — a full `json.dump` reformats compact inline objects (diff noise); count strings use i18next `_one`/`_other`. React Flow: memoize layout on a value *signature*, never hand it freshly-built node arrays each render (causes flicker); keep any per-tick clock in a leaf component.
