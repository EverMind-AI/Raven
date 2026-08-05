# CLAUDE.md - ui-webui

Guidance for working inside `ui-webui/`, the web app for chatting with Raven and
configuring it (IM channels, scheduled tasks, third-party sub-agents).

The repo-root `AGENTS.md` still applies, but it targets the **Python `raven`
core** (uv, pytest, Conventional Commits). This subtree is a **JS/TS pnpm
workspace plus a thin Python FastAPI service**: the JS side answers to pnpm rather
than uv, and there is no pytest suite for the frontend. The service is not exempt
though -- it shares raven's uv-managed environment, so `AGENTS.md` section 4 holds
for it: uv provisions those dependencies, never pip. The commit, branch, and
English-only rules apply throughout.

**Read `docs/MIGRATION.md` first.** It is the authoritative handoff: `ui-webui/`
is the RavenX web app migrated into this repo, and the work is a *backend-kernel
swap* (AgentScope kept as the BFF/REST surface; the real `raven` becomes the chat
agent), not just a file move. MIGRATION.md carries the provenance, the phased
roadmap (P0-P4), and the ADRs behind the layout below.

## Layout

| Path | What it is | Runtime |
|---|---|---|
| `frontend/` | React 19 + Vite + Tailwind v4 SPA | Vite dev server `:5173` |
| `service/` | Python FastAPI agent service (AgentScope) | uvicorn `:8000` |
| `docs/` | Migration notes, conventions, specs | - |

`frontend` is the only pnpm-workspace package (`pnpm-workspace.yaml`); `service/`
is a separate Python process, not part of the JS workspace. The Node BFF that
used to sit on `:3000` is gone -- the frontend talks to the service directly.

`service/agentscope/` is the AgentScope framework the service is built on, adopted
into this repo (Apache-2.0, attributed in the root `NOTICES.md`). It is our code
now and is linted with the rest of the repo; only `ruff-format` skips it, because
reformatting would rewrite the whole tree for no functional gain. Its own
conventions are grandfathered through `[tool.ruff.lint.per-file-ignores]`.
Behaviour changes belong in `main.py`, not in that tree.

It is trimmed to what this deployment actually reaches: one workspace backend
(local), one vector store (Qdrant), one blob store (local), and no
tracing / long-term-memory / AG-UI / sandbox-gateway subsystems. Before adding
code there, check whether anything calls it -- and before deleting, note that
`tool/_builtin/_scripts/_glob_helper.py` is reached by *path*
(`importlib.resources` + subprocess), not by import, so an import graph alone
will call it dead. See `service/agentscope/PROVENANCE.md`.

## Commands

```bash
# One-click: Redis + gateway(:8765) + service(:8000) + frontend(:5173), streams logs
../start_webapp.sh              # run from repo root; Ctrl+C stops all
../start_webapp.sh stop|restart|status
# The UI resolves the service itself (VITE_SERVICE_PORT, exported by the script);
# no setup screen. Override in Settings -> Preferences, or set VITE_SERVER_URL.

# JS deps + dev (from ui-webui/)
pnpm install
pnpm dev                        # just Vite (frontend is the only package)
pnpm dev:frontend               # same thing, explicit

# Formatting (Prettier + eslint --fix); husky + lint-staged run these pre-commit
pnpm format
pnpm format:check
```

Package manager is **pnpm only** for the JS side. The Python `service/` has no
environment of its own: it runs on the **same interpreter as raven**, the uv tool
env holding the editable checkout, so one Python serves the CLI, the gateway and
this service. Its extra dependencies are declared in `service/requirements.txt`
and reach that env only through the tool install -- never `pip install`, which
that env has no pip for:

```bash
# from the repo root; re-run after editing either requirements file
uv tool install --force --editable . \
  --with-requirements ui-webui/service/requirements-dev.txt

# the interpreter that command provisions (what start_webapp.sh resolves too)
RAVEN_PY="$(dirname "$(readlink -f "$(command -v raven)")")/python"
cd ui-webui/service && "$RAVEN_PY" main.py
```

`agentscope` sits inside `service/`, so it is imported via `sys.path[0]` with no
`PYTHONPATH` and no install step -- confirm which copy is in play (should print
the in-tree path):

```bash
cd ui-webui/service && "$RAVEN_PY" \
  -c "import agentscope,os; print(os.path.dirname(agentscope.__file__))"
```

Chat-agent backend: there is exactly one. `/chat/` is a WS client
(`RavenGatewayAgent`) to a persistent `raven gateway`, translating spine-wire
events into AgentScope events, and it is what the P4 config admin requires.
`service/main.py` wires it unconditionally -- no env switch selects it, and the
legacy one-shot bridge and the AgentScope leader fallback are both gone. A
`raven` on PATH with `gateway.web.enabled=true` is therefore a hard requirement;
`start_webapp.sh` fails fast when it is missing.

## Gate frontend changes (no JS unit-test runner)

```bash
pnpm -C frontend lint           # eslint: 0 errors required (pre-existing warnings ok)
pnpm -C frontend build          # tsc -b + vite build
```

## Frontend notes

- Stack: React 19, React Router 7, Tailwind v4 (`@tailwindcss/vite`), shadcn +
  Radix, `lucide-react` icons, `@xyflow/react` (React Flow) for the DAG view,
  i18next, framer-motion.
- `vite.config.ts`: `@` aliases `src/`; there is no dev proxy (the frontend calls
  the service directly at its resolved base URL); `path`/`next/navigation` are
  shimmed via `src/lib/*-shim.ts` - don't remove those aliases.
- Prettier is **tabs, width 4, single quotes, semicolons, print width 100**
  (`.prettierrc`) - match it; a stray reformat is diff noise.
- `src/api/` holds the typed REST clients (`ravenConfig.ts` drives the Raven
  config pages); `src/pages/raven-channels|raven-cron|subagent` are the P4
  config pages backed by `service/raven_config_routes.py`. `/subagents` is
  Raven-backed too now, not an AgentScope page.
- i18n: edit `src/i18n/locales/{en,zh}.json` with **targeted** text edits, never a
  full `json.dump` (reformats compact objects). Count strings use i18next
  `_one`/`_other`.
- React Flow: memoize layout on a value *signature*, never hand freshly-built node
  arrays each render (causes flicker); keep any per-tick clock in a leaf component.
- The sidebar logo and the favicon are **two different images**, not copies:
  sidebar = `frontend/src/assets/images/raven_logo.svg` (imported by
  `AppSidebar.tsx`), favicon = `frontend/public/favicon.svg` (referenced by
  `index.html`). Maintain them independently.

## Service notes

- `service/main.py` builds the FastAPI app (AgentScope `create_app`) with Redis
  storage and an in-memory message bus; it mounts `build_raven_config_router()`
  (prefix `/raven`) only when the gateway is enabled.
- The P4 config pages (channels/cron/sub-agents) go through a **proxy**: the web
  env has no `raven`, so config admin is forwarded over the gateway WebSocket
  (`GatewayClient` in `raven_gateway_agent.py`) to the live runtime, where it is
  validated and **hot-applied** to the running `AgentLoop` (no restart).
- The AgentScope-concept REST pages (credential/model-card, knowledge, agent
  schema) are inherited from RavenX and are being reworked into Raven-concept
  pages page-by-page - see MIGRATION.md section 5 (P4) and section 10 (the
  frontend "zero-change" event contract to preserve when touching event
  translation).

More background: `docs/MIGRATION.md` (read first),
`docs/raven-gateway-integration-analysis.md` (Raven-side integration seams with
`file:line`), `docs/RAVENX_CONVENTIONS.md`, `docs/specs/` + `docs/plans/`.
