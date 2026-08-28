# Raven-native Credentials page — design

Date: 2026-07-24
Status: approved (pending spec review)

## Goal

Rework the Credentials page from an AgentScope-concept page into a **raven-native**
LLM-provider config page: read the available providers from raven's own registry,
and read/write the user's **local raven model config** (`~/.raven/config.json`,
`providers` section).

The project's direction is raven-as-main-agent, so provider/API-key config must
target raven's config, not AgentScope's credential store.

## Scope

**In scope (this step — WebUI only):**
- New `ui-webui/service` routes that reflect raven's provider registry and
  read/write `~/.raven/config.json` via raven's public config API.
- Rework the frontend Credentials page to consume those routes.
- Brand icons via `@lobehub/icons` (color variants).

**Out of scope (explicitly):**
- No changes to `raven/` core (registry, provider support, schema). We only
  *use* raven's existing public config API.
- No gateway-RPC path (that needs raven-side handlers → touches raven).
- Anthropic-Compatible / Gemini-Compatible custom endpoints: raven has only one
  custom slot (`custom` = OpenAI-compatible). Only **OpenAI Compatible** ships
  this step (maps to raven `custom`). The other two are deferred to a later step
  that would add raven schema slots.

## Key facts (verified)

- Config file: `~/.raven/config.json`, `providers.<slug>` where each section is
  `ProviderConfig { api_key, api_base, extra_headers, models[] }` (JSON keys are
  camelCase: `apiKey`, `apiBase`, `extraHeaders`, `models`); `gemini` also has
  `api_key_list`.
- 19 providers in `raven.providers.registry.PROVIDERS`; the config field names
  match `ProvidersConfig` (snake_case in Python: `custom`, `azure_openai`,
  `anthropic`, `openai`, `openrouter`, `deepseek`, `groq`, `zhipu`, `dashscope`,
  `vllm`, `gemini`, `moonshot`, `minimax`, `aihubmix`, `ollama`, `siliconflow`,
  `volcengine`, `openai_codex`, `github_copilot`).
- `raven` is importable in the service's conda env (`ravenx`); `raven` CLI is on
  PATH (v0.1.8).
- `raven.config.update_providers` is the sanctioned public API and already
  exposes everything needed:
  - `list_providers()` → per-provider `{name, display_name, is_oauth, is_local,
    is_gateway, configured, api_key_redacted, api_base}`
  - `get_provider_config(name)` → full current section (incl. `models`)
  - `provider_field_specs(name)` → field metadata for form rendering
  - `set_provider_fields(name, fields)` → patch (raises on OAuth api_key,
    validates against schema)
  - `reset_provider(name)`, `add_provider_model(name, model)`,
    `remove_provider_model(name, model)`, `test_provider(name)`

## Architecture

### Backend — `ui-webui/service/raven_providers_routes.py`

A thin FastAPI router (prefix `/raven/providers`) wrapping `update_providers`.
It imports `raven` directly (no subprocess, no gateway):

- `GET /raven/providers`
  → `list_providers()`, enriched per item with `default_api_base`,
  `default_model`, `env_key` from the registry spec, and `models` from
  `get_provider_config(name)`. api_key is never returned in clear (only
  `api_key_redacted`).
- `GET /raven/providers/{name}`
  → full field specs (`provider_field_specs`) + current values (redacted key)
  for the detail form.
- `PUT /raven/providers/{name}`
  → body `{apiKey?, apiBase?, models?}` → `set_provider_fields(name, {...})`.
  Maps the OAuth `RuntimeError` to HTTP 409 with a "use `raven provider login`"
  message; `KeyError` → 404; `ValidationError` → 422.
- `POST /raven/providers/{name}/test` → `test_provider(name)` (connectivity).
- `POST /raven/providers/{name}/reset` → `reset_provider(name)`.

Mounted in `service/main.py` unconditionally (independent of the gateway flag,
since it reads local config directly).

Notes:
- Model dump uses snake→camel already handled by raven's `Base` alias generator;
  the router normalizes I/O to camelCase for the frontend.
- The running raven picks up config on its next run/turn (bridge mode spawns a
  fresh `raven` per turn); no restart wiring needed here.

### Frontend — rework `pages/credential/index.tsx`

- Data source switches from `credentialApi.schemas()` to a new
  `ravenProvidersApi` (`src/api/ravenProviders.ts`) calling the routes above.
- Left rail: **Configured** group first (providers with `configured: true`),
  then the rest grouped by kind — Direct vendors / Gateways / Local / OAuth —
  derived from `is_gateway` / `is_local` / `is_oauth`.
- Each row: `ProviderIcon` (extended to map raven slugs → lobehub brand icons,
  color variants) + display name.
- Right pane: selected provider form — API Key (password, shows
  `api_key_redacted` state), Base URL (placeholder = `default_api_base`), Models
  (add/remove chips, placeholder = `default_model`). OAuth providers
  (`openai_codex`, `github_copilot`) hide the key input and show a
  "run `raven provider login`" note.
- `custom` is labeled **"OpenAI Compatible"** in the UI.
- Icons keep the `[&_svg]:!size-full` centering fix already in `ProviderIcon`.

### ProviderIcon slug → lobehub mapping (raven slugs)

openai→OpenAI, anthropic→Anthropic, gemini→Gemini, deepseek→DeepSeek,
moonshot→Moonshot, dashscope→Qwen, ollama→Ollama, openrouter→OpenRouter,
groq→Groq, zhipu→Zhipu, minimax→Minimax, aihubmix→AiHubMix (or generic),
siliconflow→SiliconCloud, volcengine→Volcengine (or generic),
azure_openai→Azure, vllm→(generic), custom→OpenAI (compatible),
openai_codex→OpenAI, github_copilot→GithubCopilot. Any missing lobehub export
falls back to the neutral server glyph. (Exact export names verified against the
installed `@lobehub/icons` during implementation.)

## Testing / gates

No JS unit runner. Gate with:
- `pnpm -C frontend lint` (0 errors) and `pnpm -C frontend build` (tsc + vite).
- Backend: manual `GET /raven/providers` returns the 19 providers with current
  config; `PUT` mutates `~/.raven/config.json` and a re-`GET` reflects it;
  OAuth provider `PUT` with apiKey returns 409.
- Browser check on the reworked page (icons centered, form reads/writes).

## Risks / open items

- Prod web env may not have `raven` importable (CLAUDE.md notes the gateway
  proxy exists for that reason). This step targets the local dev setup where
  raven is importable; a gateway-RPC fallback is a future option.
- Concurrent edits to `config.json` (CLI + web) — `set_provider_fields` writes
  atomically; last-writer-wins is acceptable for local single-user dev.
