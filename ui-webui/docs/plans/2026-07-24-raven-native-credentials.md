# Raven-native Credentials Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework the Credentials page into a raven-native LLM-provider config page that reads raven's provider registry and reads/writes the local `~/.raven/config.json` provider sections.

**Architecture:** A thin FastAPI router in `ui-webui/service` imports `raven` and wraps `raven.config.update_providers` (the sanctioned config API) to expose GET/PUT/test/reset over `/raven/providers`. The frontend Credentials page switches its data source from AgentScope credential schemas to these routes. No `raven/` core changes.

**Tech Stack:** Python 3.11 / FastAPI (service, conda env `ravenx`); React 19 + Vite + Tailwind v4 + shadcn (frontend); `@lobehub/icons` for brand logos; `~icons/solar/*` (unplugin-icons) for control glyphs.

## Global Constraints

- Do NOT modify anything under `raven/` (registry, schema, provider support). Only *import and call* raven's public config API.
- Config file is `~/.raven/config.json`; write path is ONLY via `raven.config.update_providers` functions. Never hand-write the providers section.
- API keys never leave the backend in clear: GET returns `api_key_redacted` only.
- OAuth providers (`openai_codex`, `github_copilot`) reject api_key writes → surface as HTTP 409 telling the user to run `raven provider login`.
- Frontend copy/UI is English; Prettier is tabs / width 4 / single quotes / semicolons / print width 100 (`.prettierrc`).
- Gates (no JS unit runner): `pnpm -C frontend lint` (0 errors) and `pnpm -C frontend build` must pass.
- `custom` provider is labeled **"OpenAI Compatible"** in the UI.

---

### Task 1: Backend router `/raven/providers`

**Files:**
- Create: `ui-webui/service/raven_providers_routes.py`
- Modify: `ui-webui/service/main.py` (mount router unconditionally after `create_app`)

**Interfaces:**
- Consumes (from raven, read-only): `raven.config.update_providers.{list_providers, get_provider_config, provider_field_specs, set_provider_fields, reset_provider, add_provider_model, remove_provider_model, test_provider}`; `raven.providers.registry.find_by_name`.
- Produces (HTTP, consumed by Task 2):
  - `GET /raven/providers` → `{ "providers": ProviderSummary[] }`
  - `GET /raven/providers/{name}` → `ProviderDetail`
  - `PUT /raven/providers/{name}` body `{apiKey?: str, apiBase?: str|null, models?: str[]}` → `{ "ok": true, "previous": {...} }`
  - `POST /raven/providers/{name}/test` → `{ ...test_provider result... }`
  - `POST /raven/providers/{name}/reset` → `{ "ok": true }`
  - `ProviderSummary = {name, displayName, isOauth, isLocal, isGateway, configured, apiKeyRedacted, apiBase, defaultApiBase, defaultModel, envKey, models}`
  - `ProviderDetail = ProviderSummary + {fields: Record<string, {type,default,isSecret,description}>}`

- [ ] **Step 1: Create the router module**

Create `ui-webui/service/raven_providers_routes.py`:

```python
"""REST routes for raven-native LLM provider config (local ~/.raven/config.json).

Unlike raven_config_routes.py (gateway RPC), these import raven directly and
wrap raven.config.update_providers — the sanctioned config read/write API — to
reflect the provider registry and mutate the local config file. WebUI-only:
raven's core provider support is not modified here.

Mounted unconditionally by main.py (reads local config, no gateway needed).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import ValidationError

from raven.config import update_providers as up
from raven.providers.registry import find_by_name


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    """Enrich a list_providers() row with registry defaults + configured models."""
    name = item["name"]
    spec = find_by_name(name)
    cfg = up.get_provider_config(name)  # redacted; models path is "models"
    models = cfg.get("models") or []
    return {
        "name": name,
        "displayName": item["display_name"],
        "isOauth": item["is_oauth"],
        "isLocal": item["is_local"],
        "isGateway": item["is_gateway"],
        "configured": item["configured"],
        "apiKeyRedacted": item["api_key_redacted"],
        "apiBase": item["api_base"],
        "defaultApiBase": getattr(spec, "default_api_base", "") if spec else "",
        "defaultModel": getattr(spec, "default_model", "") if spec else "",
        "envKey": getattr(spec, "env_key", "") if spec else "",
        "models": models,
    }


def build_raven_providers_router() -> APIRouter:
    router = APIRouter(prefix="/raven/providers", tags=["raven-providers"])

    @router.get("")
    async def list_all() -> dict:
        return {"providers": [_summary(it) for it in up.list_providers()]}

    @router.get("/{name}")
    async def get_one(name: str) -> dict:
        try:
            summaries = {it["name"]: it for it in up.list_providers()}
            if name not in summaries:
                raise KeyError(name)
            detail = _summary(summaries[name])
            detail["fields"] = {
                path: {
                    "type": spec["type"],
                    "default": spec["default"],
                    "isSecret": spec["is_secret"],
                    "description": spec["description"],
                }
                for path, spec in up.provider_field_specs(name).items()
            }
            return detail
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown provider '{name}'") from exc

    @router.put("/{name}")
    async def update_one(name: str, body: dict = Body(...)) -> dict:
        fields: dict[str, Any] = {}
        if "apiKey" in body:
            fields["api_key"] = body["apiKey"]
        if "apiBase" in body:
            fields["api_base"] = body["apiBase"]
        if "models" in body:
            fields["models"] = body["models"] or []
        try:
            previous = up.set_provider_fields(name, fields)
            return {"ok": True, "previous": previous}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:  # OAuth provider rejects api_key
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/{name}/test")
    async def test_one(name: str) -> dict:
        try:
            return {"result": up.test_provider(name)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # network / provider error
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/{name}/reset")
    async def reset_one(name: str) -> dict:
        try:
            up.reset_provider(name)
            return {"ok": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
```

- [ ] **Step 2: Mount the router in main.py**

In `ui-webui/service/main.py`, immediately after the `app = create_app(...)` block (before any `if _flag_enabled(...)` gateway block), add:

```python
from raven_providers_routes import build_raven_providers_router

app.include_router(build_raven_providers_router())
```

- [ ] **Step 3: Restart service and verify GET**

Run (from repo root):
```bash
./start_webapp.sh restart
curl -s -H "X-User-ID: demo" http://localhost:8000/raven/providers | python3 -m json.tool | head -40
```
Expected: JSON with a `providers` array of 19 entries; each has `name`, `displayName`, `configured`, `apiKeyRedacted`, `defaultApiBase`, `models`. `custom` present with `displayName` "Custom".

- [ ] **Step 4: Verify PUT round-trips to config.json**

Run:
```bash
curl -s -X PUT -H "X-User-ID: demo" -H "Content-Type: application/json" \
  -d '{"apiBase":"https://example.test/v1","models":["gpt-4o-test"]}' \
  http://localhost:8000/raven/providers/custom
python3 -c "import json,os;d=json.load(open(os.path.expanduser('~/.raven/config.json')));print(d['providers']['custom'])"
```
Expected: PUT returns `{"ok": true, ...}`; the printed `custom` section shows `apiBase='https://example.test/v1'` and `models=['gpt-4o-test']`.

- [ ] **Step 5: Verify OAuth guard returns 409**

Run:
```bash
curl -s -o /dev/null -w "%{http_code}\n" -X PUT -H "X-User-ID: demo" \
  -H "Content-Type: application/json" -d '{"apiKey":"x"}' \
  http://localhost:8000/raven/providers/openai_codex
```
Expected: `409`.

- [ ] **Step 6: Revert the test mutation**

Run:
```bash
curl -s -X POST -H "X-User-ID: demo" http://localhost:8000/raven/providers/custom/reset >/dev/null && echo reset-ok
```
Expected: `reset-ok` (custom section back to schema defaults).

---

### Task 2: Frontend API client `ravenProviders.ts`

**Files:**
- Create: `ui-webui/frontend/src/api/ravenProviders.ts`
- Modify: `ui-webui/frontend/src/api/index.ts` (re-export)

**Interfaces:**
- Consumes: `client` from `./client` (`client.get<T>`, `client.put<T>`, `client.post<T>`), Task 1 HTTP routes.
- Produces (for Task 4): `ravenProvidersApi` with `list()`, `get(name)`, `update(name, body)`, `test(name)`, `reset(name)`; types `RavenProviderSummary`, `RavenProviderDetail`, `RavenProviderUpdate`.

- [ ] **Step 1: Create the API module**

Create `ui-webui/frontend/src/api/ravenProviders.ts`:

```typescript
import { client } from './client';

export interface RavenProviderSummary {
	name: string;
	displayName: string;
	isOauth: boolean;
	isLocal: boolean;
	isGateway: boolean;
	configured: boolean;
	apiKeyRedacted: string;
	apiBase: string | null;
	defaultApiBase: string;
	defaultModel: string;
	envKey: string;
	models: string[];
}

export interface RavenProviderDetail extends RavenProviderSummary {
	fields: Record<
		string,
		{ type: string; default: unknown; isSecret: boolean; description: string }
	>;
}

export interface RavenProviderUpdate {
	apiKey?: string;
	apiBase?: string | null;
	models?: string[];
}

export const ravenProvidersApi = {
	list: () => client.get<{ providers: RavenProviderSummary[] }>('/raven/providers'),
	get: (name: string) => client.get<RavenProviderDetail>(`/raven/providers/${name}`),
	update: (name: string, body: RavenProviderUpdate) =>
		client.put<{ ok: boolean }>(`/raven/providers/${name}`, body),
	test: (name: string) => client.post<{ result: unknown }>(`/raven/providers/${name}/test`, {}),
	reset: (name: string) => client.post<{ ok: boolean }>(`/raven/providers/${name}/reset`, {}),
};
```

- [ ] **Step 2: Re-export from the api barrel**

In `ui-webui/frontend/src/api/index.ts`, add (near the other `export * from './ravenConfig';` style lines):

```typescript
export * from './ravenProviders';
export { ravenProvidersApi } from './ravenProviders';
```

(If the file uses a different re-export convention, match it — check `git grep "ravenConfigApi" src/api/index.ts` and mirror that exact line style.)

- [ ] **Step 3: Type-check**

Run:
```bash
cd ui-webui/frontend && pnpm exec tsc -b
```
Expected: exit 0, no errors.

- [ ] **Step 4: Commit**

```bash
git add ui-webui/frontend/src/api/ravenProviders.ts ui-webui/frontend/src/api/index.ts
git commit -m "feat(ui-webui): raven providers api client"
```

---

### Task 3: Extend `ProviderIcon` to raven slugs

**Files:**
- Modify: `ui-webui/frontend/src/components/ProviderIcon.tsx`

**Interfaces:**
- Consumes: `@lobehub/icons` exports (verified present: `OpenAI, Anthropic, Gemini, DeepSeek, Moonshot, Qwen, Ollama, OpenRouter, Groq, Zhipu, Minimax, AiHubMix, SiliconCloud, Volcengine, Azure, Vllm, GithubCopilot`), `~icons/solar/server-square-bold-duotone`.
- Produces: `ProviderIcon` keyed by raven slugs (snake_case: `openai`, `azure_openai`, `openai_codex`, `github_copilot`, etc.).

- [ ] **Step 1: Rewrite the mapping switch**

Replace the imports and switch in `ui-webui/frontend/src/components/ProviderIcon.tsx` with the raven-slug mapping. Full file:

```tsx
import {
	AiHubMix,
	Anthropic,
	Azure,
	DeepSeek,
	Gemini,
	GithubCopilot,
	Groq,
	Minimax,
	Moonshot,
	Ollama,
	OpenAI,
	OpenRouter,
	Qwen,
	SiliconCloud,
	Vllm,
	Volcengine,
	Zhipu,
} from '@lobehub/icons';
import type { ReactNode } from 'react';

import ServerIcon from '~icons/solar/server-square-bold-duotone';

interface ProviderIconProps {
	/** Raven provider slug, e.g. `openai`, `azure_openai`, `custom`. */
	type: string;
	size?: number;
}

// Brand logo for each raven provider slug. `custom` is the generic
// OpenAI-compatible endpoint (uses the OpenAI mark); unknown/`vllm` fall back
// to a neutral server glyph.
export function ProviderIcon({ type, size = 20 }: ProviderIconProps) {
	let inner: ReactNode;
	switch (type) {
		case 'openai':
		case 'custom':
		case 'openai_codex':
			inner = <OpenAI.Avatar size={size} />;
			break;
		case 'anthropic':
			inner = <Anthropic.Avatar size={size} />;
			break;
		case 'gemini':
			inner = <Gemini.Avatar size={size} />;
			break;
		case 'deepseek':
			inner = <DeepSeek.Avatar size={size} />;
			break;
		case 'moonshot':
			inner = <Moonshot.Avatar size={size} />;
			break;
		case 'dashscope':
			inner = <Qwen.Avatar size={size} />;
			break;
		case 'ollama':
			inner = <Ollama.Avatar size={size} />;
			break;
		case 'openrouter':
			inner = <OpenRouter.Avatar size={size} />;
			break;
		case 'groq':
			inner = <Groq.Avatar size={size} />;
			break;
		case 'zhipu':
			inner = <Zhipu.Avatar size={size} />;
			break;
		case 'minimax':
			inner = <Minimax.Avatar size={size} />;
			break;
		case 'aihubmix':
			inner = <AiHubMix.Avatar size={size} />;
			break;
		case 'siliconflow':
			inner = <SiliconCloud.Avatar size={size} />;
			break;
		case 'volcengine':
			inner = <Volcengine.Avatar size={size} />;
			break;
		case 'azure_openai':
			inner = <Azure.Avatar size={size} />;
			break;
		case 'github_copilot':
			inner = <GithubCopilot.Avatar size={size} />;
			break;
		case 'vllm':
			inner = <Vllm.Avatar size={size} />;
			break;
		default:
			inner = <ServerIcon className="size-full text-muted-foreground" />;
	}
	// Uniform, centered box; `[&_svg]:!size-full` overrides the ambient sidebar
	// rule `[&_svg]:size-4` so the lobehub glyph fills its tile at any size.
	return (
		<span
			className="inline-flex shrink-0 items-center justify-center [&_svg]:!size-full"
			style={{ width: size, height: size }}
		>
			{inner}
		</span>
	);
}
```

- [ ] **Step 2: Verify lobehub exports exist**

Run:
```bash
cd ui-webui/frontend
for n in AiHubMix Anthropic Azure DeepSeek Gemini GithubCopilot Groq Minimax Moonshot Ollama OpenAI OpenRouter Qwen SiliconCloud Vllm Volcengine Zhipu; do
  grep -q "as ${n}," node_modules/@lobehub/icons/es/icons.d.ts && echo "$n OK" || echo "$n MISSING"
done
```
Expected: every line ends `OK`. If any `MISSING`, substitute the nearest export (e.g. a generic) and note it.

- [ ] **Step 3: Type-check**

Run: `cd ui-webui/frontend && pnpm exec tsc -b`
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add ui-webui/frontend/src/components/ProviderIcon.tsx
git commit -m "feat(ui-webui): map ProviderIcon to raven provider slugs"
```

---

### Task 4: Rework the Credentials page

**Files:**
- Modify (rewrite): `ui-webui/frontend/src/pages/credential/index.tsx`

**Interfaces:**
- Consumes: `ravenProvidersApi`, `RavenProviderSummary`, `RavenProviderDetail` (Task 2); `ProviderIcon` (Task 3); shadcn `Sidebar*`, `Button`, `Input`, `Label`, `Badge`, `Skeleton`; `toast` from `sonner`; `useTranslation` if used elsewhere (optional — English literals acceptable per existing page).
- Produces: the raven-native Credentials page (route `/credential`, already wired in the router — no routing change).

- [ ] **Step 1: Replace the page component**

Rewrite `ui-webui/frontend/src/pages/credential/index.tsx` in full:

```tsx
import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';

import { ravenProvidersApi } from '@/api';
import type { RavenProviderSummary } from '@/api';
import { ProviderIcon } from '@/components/ProviderIcon';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
	Sidebar,
	SidebarContent,
	SidebarGroup,
	SidebarGroupContent,
	SidebarGroupLabel,
	SidebarHeader,
	SidebarMenu,
	SidebarMenuButton,
	SidebarMenuItem,
} from '@/components/ui/sidebar';
import { Skeleton } from '@/components/ui/skeleton';
import Plus from '~icons/solar/add-square-linear';
import Trash2 from '~icons/solar/trash-bin-minimalistic-bold-duotone';

// `custom` is a generic OpenAI-compatible endpoint; label it as such.
function labelFor(p: RavenProviderSummary): string {
	if (p.name === 'custom') return 'OpenAI Compatible';
	return p.displayName;
}

function kindOf(p: RavenProviderSummary): 'gateway' | 'local' | 'oauth' | 'direct' {
	if (p.isOauth) return 'oauth';
	if (p.isLocal) return 'local';
	if (p.isGateway) return 'gateway';
	return 'direct';
}

const GROUP_ORDER: Array<{ kind: ReturnType<typeof kindOf>; title: string }> = [
	{ kind: 'direct', title: 'Direct providers' },
	{ kind: 'gateway', title: 'Gateways' },
	{ kind: 'local', title: 'Local' },
	{ kind: 'oauth', title: 'OAuth' },
];

export default function CredentialPage() {
	const [providers, setProviders] = useState<RavenProviderSummary[]>([]);
	const [loading, setLoading] = useState(true);
	const [selected, setSelected] = useState<string | null>(null);

	const refresh = useCallback(async () => {
		setLoading(true);
		try {
			const res = await ravenProvidersApi.list();
			setProviders(res.providers);
			setSelected((cur) => cur ?? res.providers[0]?.name ?? null);
		} catch {
			// client.ts already toasts on error
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		void refresh();
	}, [refresh]);

	const configured = providers.filter((p) => p.configured);
	const current = providers.find((p) => p.name === selected) ?? null;

	return (
		<div className="flex h-full">
			<Sidebar collapsible="none" className="w-72 border-r">
				<SidebarHeader className="gap-1 px-4 pt-4">
					<h1 className="text-lg font-semibold">Credentials</h1>
					<p className="text-sm text-muted-foreground">Raven model API providers</p>
				</SidebarHeader>
				<SidebarContent>
					{loading ? (
						<div className="flex flex-col gap-2 p-4">
							{Array.from({ length: 6 }).map((_, i) => (
								<Skeleton key={i} className="h-8 rounded" />
							))}
						</div>
					) : (
						<>
							{configured.length > 0 && (
								<SidebarGroup>
									<SidebarGroupLabel>Configured ({configured.length})</SidebarGroupLabel>
									<SidebarGroupContent>
										<SidebarMenu>
											{configured.map((p) => (
												<SidebarMenuItem key={p.name}>
													<SidebarMenuButton
														isActive={selected === p.name}
														onClick={() => setSelected(p.name)}
													>
														<ProviderIcon type={p.name} size={18} />
														<span className="min-w-0 flex-1 truncate">{labelFor(p)}</span>
													</SidebarMenuButton>
												</SidebarMenuItem>
											))}
										</SidebarMenu>
									</SidebarGroupContent>
								</SidebarGroup>
							)}
							{GROUP_ORDER.map(({ kind, title }) => {
								const rows = providers.filter((p) => kindOf(p) === kind);
								if (rows.length === 0) return null;
								return (
									<SidebarGroup key={kind}>
										<SidebarGroupLabel>{title}</SidebarGroupLabel>
										<SidebarGroupContent>
											<SidebarMenu>
												{rows.map((p) => (
													<SidebarMenuItem key={p.name}>
														<SidebarMenuButton
															isActive={selected === p.name}
															onClick={() => setSelected(p.name)}
														>
															<ProviderIcon type={p.name} size={18} />
															<span className="min-w-0 flex-1 truncate">
																{labelFor(p)}
															</span>
															{p.configured && (
																<Badge variant="secondary" className="px-1 py-0 text-[10px]">
																	set
																</Badge>
															)}
														</SidebarMenuButton>
													</SidebarMenuItem>
												))}
											</SidebarMenu>
										</SidebarGroupContent>
									</SidebarGroup>
								);
							})}
						</>
					)}
				</SidebarContent>
			</Sidebar>
			<div className="min-w-0 flex-1 overflow-auto p-6">
				{current ? (
					<ProviderForm key={current.name} provider={current} onSaved={refresh} />
				) : (
					<div className="flex h-full items-center justify-center text-muted-foreground">
						Select a provider
					</div>
				)}
			</div>
		</div>
	);
}

interface ProviderFormProps {
	provider: RavenProviderSummary;
	onSaved: () => void | Promise<void>;
}

function ProviderForm({ provider, onSaved }: ProviderFormProps) {
	const [apiKey, setApiKey] = useState('');
	const [apiBase, setApiBase] = useState(provider.apiBase ?? '');
	const [models, setModels] = useState<string[]>(provider.models);
	const [newModel, setNewModel] = useState('');
	const [saving, setSaving] = useState(false);

	useEffect(() => {
		setApiKey('');
		setApiBase(provider.apiBase ?? '');
		setModels(provider.models);
		setNewModel('');
	}, [provider]);

	const save = async () => {
		setSaving(true);
		try {
			const body: { apiKey?: string; apiBase?: string | null; models?: string[] } = {
				apiBase: apiBase || null,
				models,
			};
			// Only send api_key when the user typed a new one (avoid clobbering).
			if (apiKey) body.apiKey = apiKey;
			await ravenProvidersApi.update(provider.name, body);
			toast.success(`Saved ${provider.displayName}`);
			await onSaved();
		} catch {
			// client.ts toasts
		} finally {
			setSaving(false);
		}
	};

	const addModel = () => {
		const m = newModel.trim();
		if (m && !models.includes(m)) setModels([...models, m]);
		setNewModel('');
	};

	const label = provider.name === 'custom' ? 'OpenAI Compatible' : provider.displayName;

	return (
		<div className="mx-auto flex max-w-2xl flex-col gap-6">
			<div className="flex items-center gap-3">
				<ProviderIcon type={provider.name} size={32} />
				<h2 className="text-xl font-semibold">{label}</h2>
			</div>

			{provider.isOauth ? (
				<div className="rounded-lg border bg-muted/40 p-4 text-sm text-muted-foreground">
					This provider uses OAuth. Run <code>raven provider login {provider.name}</code> in a
					terminal to authenticate. Status: {provider.apiKeyRedacted}.
				</div>
			) : (
				<div className="flex flex-col gap-2">
					<Label htmlFor="apiKey">API Key</Label>
					<Input
						id="apiKey"
						type="password"
						placeholder={
							provider.isLocal
								? '(not needed for local)'
								: provider.apiKeyRedacted === '****set****'
									? 'Leave blank to keep current key'
									: `Enter ${provider.envKey || 'API key'}`
						}
						value={apiKey}
						onChange={(e) => setApiKey(e.target.value)}
					/>
				</div>
			)}

			<div className="flex flex-col gap-2">
				<Label htmlFor="apiBase">Base URL</Label>
				<Input
					id="apiBase"
					placeholder={provider.defaultApiBase || 'Default'}
					value={apiBase}
					onChange={(e) => setApiBase(e.target.value)}
				/>
			</div>

			<div className="flex flex-col gap-2">
				<Label>Models</Label>
				<div className="flex flex-wrap gap-2">
					{models.map((m) => (
						<Badge key={m} variant="secondary" className="gap-1">
							{m}
							<button
								type="button"
								onClick={() => setModels(models.filter((x) => x !== m))}
								aria-label={`Remove ${m}`}
							>
								<Trash2 className="size-3" />
							</button>
						</Badge>
					))}
					{models.length === 0 && (
						<span className="text-sm text-muted-foreground">
							No models added{provider.defaultModel ? ` (e.g. ${provider.defaultModel})` : ''}
						</span>
					)}
				</div>
				<div className="flex gap-2">
					<Input
						placeholder={provider.defaultModel || 'model name'}
						value={newModel}
						onChange={(e) => setNewModel(e.target.value)}
						onKeyDown={(e) => {
							if (e.key === 'Enter') {
								e.preventDefault();
								addModel();
							}
						}}
					/>
					<Button type="button" variant="outline" onClick={addModel}>
						<Plus className="size-4" /> Add
					</Button>
				</div>
			</div>

			<div>
				<Button onClick={save} disabled={saving}>
					{saving ? 'Saving...' : 'Save'}
				</Button>
			</div>
		</div>
	);
}
```

Note: verify the existing page's export style (`export default` vs named) with `git grep "credential/index" ui-webui/frontend/src` / the router import. If the router imports a named `CredentialPage`, keep the name and add the matching export; if it imports default, keep `export default`. Match the existing import exactly so routing is unchanged.

- [ ] **Step 2: Format**

Run: `cd ui-webui/frontend && pnpm exec prettier --write src/pages/credential/index.tsx`

- [ ] **Step 3: Lint + typecheck gate**

Run:
```bash
cd ui-webui/frontend
pnpm exec tsc -b && pnpm exec eslint src/pages/credential/index.tsx src/api/ravenProviders.ts src/components/ProviderIcon.tsx
```
Expected: tsc exit 0; eslint 0 errors (run `eslint --fix` if only import-order errors appear, then re-check).

- [ ] **Step 4: Build gate**

Run: `cd ui-webui/frontend && pnpm build`
Expected: `built in ...`, exit 0.

- [ ] **Step 5: Browser verification**

With the app running (`./start_webapp.sh restart`), open `/credential`:
- Left rail shows Configured group (if any) + Direct/Gateways/Local/OAuth groups covering all 19 providers with centered brand icons.
- Selecting `custom` shows heading "OpenAI Compatible".
- Editing Base URL + adding a model + Save → toast success; reload → values persist (confirm via `python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.raven/config.json')))['providers']['custom'])"`).
- Selecting `openai_codex` shows the OAuth note (no key input).

- [ ] **Step 6: Commit**

```bash
git add ui-webui/frontend/src/pages/credential/index.tsx
git commit -m "feat(ui-webui): rework credentials page onto raven providers"
```

---

## Notes for the executor

- Commits in the steps follow Conventional Commits, but per repo AGENTS.md §3.4 do NOT run `git commit` unless the user has authorized committing for this work. If unauthorized, complete the code + gates and leave the commit steps unrun.
- `~/.raven/config.json` is shared with the raven CLI; running raven picks up changes on its next run (bridge mode spawns a fresh `raven` per turn).
- If `raven` is not importable in the service env, Task 1 import fails at startup — that indicates a non-local/prod web env; the gateway-RPC fallback (out of scope here) would be needed.
