# OpenAI-API sub-agent type (MiroMind mirothinker deep-research) — design

**Date:** 2026-07-20
**Status:** Approved (design)
**Builds on:** the CLI sub-agent layer (`src/agentscope/subagent/`), the sub-agent
instance model (`2026-07-16-subagent-instances-design.md`), the DAG orchestrator
(`_dag/`, `2026-07-17-subagent-dag-orchestration-design.md`), unified file-based
prompt delivery (`2026-07-19-subagent-file-prompt-delivery-design.md`), and the
per-user credential store (`OpenAICompatibleCredential`,
`2026-07-16-editable-credential-models-design.md`).

## 1. Motivation

The sub-agent system dispatches the unified/leader agent to heterogeneous domain
sub-agents. Today it supports exactly one transport — **a CLI subprocess**:
`CliSubAgentTool.call()` builds an argv from a command template and runs it via
`self._backend.exec_shell(argv, ...)`. Claude Code and Codex are not code, they
are *presets* (field values) over the single `CliSubAgentConfig` schema.

We want to add a **third kind of sub-agent invoked over an OpenAI-compatible HTTP
API** rather than a CLI, and ship **MiroMind `mirothinker-1-7-deepresearch`** — a
hosted deep-research agent — as its first preset. MiroMind exposes an
OpenAI-compatible Chat Completions gateway:

- Base URL `https://api.miromind.ai/v1`, auth `Authorization: Bearer <key>`.
- Models: `mirothinker-1-7-deepresearch` (flagship, 256k ctx / 16k out) and
  `mirothinker-1-7-deepresearch-mini`.
- The endpoint is **OpenAI-compatible** and **stateless** server-side (standard
  chat semantics: the full `messages[]` is sent each call; there is no server
  session — the separate Responses API is the stateful one).
- Beyond the answer, deep-research responses carry extension fields:
  `choices[0].message.reasoning_steps` (thinking / web_search / fetch_url_content
  / execute_python / execute_command / tool_call), a top-level `search_results`
  (citations: title/url/snippet), and `usage.completion_tokens_details.reasoning_tokens`
  + `usage.num_search_queries`. **The official OpenAI SDK drops these unknown
  fields**, so reading them requires the raw response body.

Nothing in `src/agentscope/subagent/` speaks HTTP; the CLI tool is subprocess-bound
end to end. This is genuinely new transport surface. However the existing
concepts map cleanly: an *instance handle* → a client-held conversation id;
*stateful create/resume* → persist and replay `messages[]`; *transcript parsing*
→ parse the JSON response instead of stdout.

## 2. Scope

**In scope (backend):** a new config subclass `OpenAISubAgentConfig`
(`type="openai_subagent"`); a sibling runtime tool `OpenAISubAgentTool` that does
an async HTTP POST instead of `exec_shell`; per-instance stateful `messages[]`
history (file-based, replayed on resume); credential resolution from the per-user
store; factory registration; the `make_subagent_tool_factory` branch and DAG-tool
gate generalization; a built-in MiroMind preset; full tests.

**In scope (frontend):** the `/subagents` page gains a **type selector** (CLI vs
OpenAI), OpenAI-specific fields (credential picker, model, stateful toggle,
system prompt, temperature, max tokens, timeout), mirrored client-side
validation, and preset UX that pre-fills the edit form for OpenAI presets
(because they require a user-chosen credential). TS API types widen to a
discriminated union. i18n strings for the new labels.

**Out of scope / unchanged:** the CLI transport (`_tool.py`, `_transcript.py`,
`_presets.py` CLI presets) is untouched — this change is purely additive. The
Responses API (stateful server-side) is not used. Streaming (SSE) consumption is
not implemented; the call is non-streaming (`stream:false`). `reasoning_steps`
full trace and multimodal/`mcp_servers`/`response_format`/`cache_control`
pass-through are deferred. The credential CRUD, its router, and the DAG template
grammar are unchanged.

## 3. Decisions

1. **Stateful with client-side history replay** (user choice) — the OpenAI
   sub-agent persists each instance's `messages[]` and replays the full
   transcript on resume, so a handle carries multi-turn context exactly like the
   CLI create/resume model and DAG same-instance chaining. Cost note: replay
   grows the prompt each turn (bounded by the 256k context window). A `stateful`
   flag (default `true`) is retained on the config so the same type can serve
   one-shot use later. (2026-07-20)
2. **Return answer + citations** (user choice) — the reply is
   `choices[0].message.content`; `search_results` (citations) and `usage`
   (`reasoning_tokens`, `num_search_queries`, `total_tokens`) are attached as
   tool-result **metadata**. The verbose step-by-step `reasoning_steps` trace is
   not surfaced. This mandates reading the **raw** JSON response (not the OpenAI
   SDK), so the transport is `httpx` (a core dependency). (2026-07-20)
3. **First-class HTTP sub-agent type (Approach B)** (user choice) — a new config
   subclass + sibling tool, not a CLI-adapter script and not built on
   `OpenAIChatModel`. Rationale: only this keeps the API key in the existing
   secure per-user credential store (never in a stored/UI-visible command
   template), returns extension fields the SDK would drop, and stays additive to
   the CLI path. (2026-07-20)
4. **Full-stack scope incl. frontend** (user choice) — the type is configurable
   in the web UI in one shot, not API-only. (2026-07-20)
5. **API key lives in the credential store, not the config** — the config holds
   only a `credential_id` referencing an `OpenAICompatibleCredential`
   (`base_url` + `api_key: SecretStr`). Nothing sensitive is stored in the
   prototype record or rendered in the UI. (2026-07-20)
6. **Reuse `SubAgentInstanceRecord` + `SessionInstanceRegistry` unchanged** — the
   handle→id mapping and deferred-commit semantics carry over; `agent_id` becomes
   a RavenX-provisioned conversation id (also the history-file key). History
   `messages[]` are stored on disk under the session workdir, not in Redis.
   (2026-07-20)

## 4. Data model — `OpenAISubAgentConfig`

A new subclass of `SubAgentConfigBase`, in `src/agentscope/subagent/_base.py`
alongside `CliSubAgentConfig` (it shares the `id`-only base). It deliberately
carries **none** of the CLI-only fields (`command`, `resume_command`,
`id_source`, `session_id_pattern`, `output_pattern`, `transcript_format`, `env`).

```python
class OpenAISubAgentConfig(SubAgentConfigBase):
    """A sub-agent invoked over an OpenAI-compatible Chat Completions API."""

    type: Literal["openai_subagent"] = "openai_subagent"
    name: str            # tool name; ^[A-Za-z0-9_-]+$ (same rule as CLI)
    description: str     # agent-readable "when to delegate"
    credential_id: str   # ref to a stored OpenAICompatibleCredential; min_length 1
    model: str           # e.g. "mirothinker-1-7-deepresearch"; min_length 1
    stateful: bool = True                 # persist + replay messages[] on resume
    system_prompt: str | None = None      # optional system message, prepended once
    temperature: float | None = None      # 0.0 <= t <= 2.0 when set
    max_tokens: int | None = None         # > 0 when set
    timeout: int = 1200                   # request timeout (s); deep research is slow
```

**Validators** (Pydantic field/model validators, mirroring the strictness of
`CliSubAgentConfig`):
- `name` matches `^[A-Za-z0-9_-]+$`.
- `credential_id` and `model` are non-empty after strip.
- `temperature`, when provided, is within `[0.0, 2.0]`.
- `max_tokens`, when provided, is `> 0`.
- `timeout` is `> 0`.

No `{prompt}`/`{agent_id}` placeholder rules apply (there is no command); the
prompt is delivered as the last user message, not by string substitution, so the
injection-safety concern that motivates the CLI's argv-token discipline does not
arise here.

## 5. Factory registration — `_factory.py`

`SubAgentFactory`'s built-in class list gains `OpenAISubAgentConfig`:

```python
_BUILTIN_CLASSES = [CliSubAgentConfig, OpenAISubAgentConfig]
```

This makes it round-trip through storage (`from_dict` dispatches on the `type`
discriminator) and appear in `GET /subagent/schemas` (each class contributes its
`model_json_schema()`), so the frontend can render its form from the schema and
the router accepts it with no route changes. `SubAgentConfigBase` /
`SubAgentConfigType` union references are widened accordingly.

## 6. Runtime — `OpenAISubAgentTool`

New file `src/agentscope/subagent/_openai_tool.py`. It is a `ToolBase` with the
**same `call()` signature the DAG runner uses**, so it is DAG-compatible with no
runner changes:

```python
async def call(
    self,
    prompt: str,
    instance: str | None = None,
    prompt_file: str | None = None,
    output_file: str | None = None,
) -> AsyncGenerator[ToolChunk, None]:
```

Construction (by the tool factory, §7):

```python
OpenAISubAgentTool(
    config=cfg,                 # the OpenAISubAgentConfig
    base_url=base_url,          # resolved from the credential
    api_key=api_key,            # plaintext secret, resolved from the credential
    organization=organization,  # optional, from the credential
    backend=backend,            # workspace backend (file I/O for prompt/output/history)
    cwd=workdir,                # session workdir
    registry=SessionInstanceRegistry(storage, session_id),
)
```

### 6.1 Prompt materialization (unchanged invariant)

Identical to the CLI tool after the file-prompt-delivery change: if `prompt_file`
is `None`, write the inline `prompt` to `.ravenx_prompt_<uuid>.md` via the
backend (audit file); then read the file back and use its contents as the user
message text. A read/write failure yields a terminal `ERROR` chunk before any
network call or state mutation.

### 6.2 Create vs resume (reuses `SessionInstanceRegistry`)

Same shape as `CliSubAgentTool`:
- `handle = instance or str(uuid.uuid4())`.
- `registry.lookup(handle)` → if found, **resume** (`agent_id` from the
  registry, `action="resume"`); else **create** (`agent_id = str(uuid.uuid4())`,
  `action="create"`).
- The handle→`agent_id` binding is `registry.commit(handle, agent_id, name)`d
  **only after a successful create** (deferred persistence — a failed create
  never poisons the handle), matching the CLI guarantee.
- When `stateful=False`, no lookup/commit occurs; every call is independent and
  `agent_id` is a fresh uuid used only for the response metadata.

### 6.3 History (file-based, under the session workdir)

`messages[]` are persisted per instance as a flat JSON file keyed by `agent_id`:
`join_path(cwd, f".ravenx_openai_{agent_id}.json")`, written/read via the
workspace backend (mirrors the CLI's `.ravenx_prompt_<uuid>.md`; keeps Redis
light; leaves an auditable on-disk transcript).

- **Create:** `history = ([{ "role": "system", "content": system_prompt }] if
  system_prompt else []) + [{ "role": "user", "content": prompt_text }]`.
- **Resume:** read the history file (missing or unparseable ⇒ log a warning and
  start from the `system_prompt` seed), then append `{ "role": "user",
  "content": prompt_text }`.
- After a successful response, append `{ "role": "assistant", "content": content }`
  and write the file back.
- When `stateful=False`, history is `([system] +) [user]`, never persisted.

### 6.4 HTTP call

Lazy-import `httpx` at point of use (consistent with the module's no-top-level
third-party-import convention, even though `httpx` is core):

```python
url = base_url.rstrip("/") + "/chat/completions"
headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
if organization:
    headers["OpenAI-Organization"] = organization
body = {"model": model, "messages": history, "stream": False}
if temperature is not None:
    body["temperature"] = temperature
if max_tokens is not None:
    body["max_tokens"] = max_tokens

async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as http:
    resp = await http.post(url, headers=headers, json=body)
```

Non-streaming (`stream:false`) so `content`, `search_results`, and `usage` all
arrive in one JSON body — the natural fit for "answer + citations".

### 6.5 Response parsing → `ToolResponse`

- **Transport / HTTP errors** (`httpx.TimeoutException`, connection errors,
  non-2xx): parse the standard error envelope
  `{"error": {"code", "message", "type"}}` when present and surface a terminal
  `ERROR` chunk `"Sub-Agent HTTP error (<status>/<code>): <message>"`; otherwise
  a generic error with the exception text. No history is written and, on create,
  nothing is committed.
- **Success:** `content = data["choices"][0]["message"]["content"]`;
  `finish_reason` in `{"error", "cancelled"}` (or missing/empty `content`) ⇒
  terminal `ERROR` chunk. Otherwise a terminal success chunk:
  - the **full** `content` is written to `output_file` when provided (DAG
    downstream) — untruncated, exactly as the CLI tool persists its full output;
    the **in-context returned `text`** is `content` truncated at
    `_MAX_OUTPUT_CHARS` (128000) with the same `"\n... (output truncated)"`
    suffix the CLI tool uses.
  - `metadata = { "instance": handle, "agent_id": agent_id, "action": action,
    "model": model, "citations": data.get("search_results") or [],
    "usage": { "reasoning_tokens": …, "num_search_queries": …,
    "total_tokens": … } }`.

The `check_permissions()` method mirrors `CliSubAgentTool`'s (the tool performs
an outbound network call + local file writes under the session workdir).

## 7. Wiring — `_agent_tools.py`

`make_subagent_tool_factory` already receives `storage` and, per turn, the
`(user_id, session_id)`; it iterates `storage.list_subagents(user_id)` and builds
one tool per valid config. Two changes:

1. **Branch the construction gate** (currently
   `if isinstance(config, CliSubAgentConfig)` at `_agent_tools.py:104`):
   - CLI config → `CliSubAgentTool` (unchanged).
   - `OpenAISubAgentConfig` → resolve the credential, then build
     `OpenAISubAgentTool`. Resolution:
     `rec = await storage.get_credential(user_id, config.credential_id)`; if
     `None` → log a warning and **skip** the tool (same graceful degradation the
     factory already uses for malformed configs) — the agent simply won't see a
     tool it can't authenticate. Otherwise
     `cred = CredentialFactory.from_dict(rec.data)`; if `cred` is not an
     `OpenAICompatibleCredential` → warn + skip; else pass
     `base_url=cred.base_url`, `api_key=cred.api_key.get_secret_value()`,
     `organization=cred.organization`. Because the factory re-runs each turn, an
     edited credential takes effect on the next turn.
2. **Generalize the DAG-tool gate:** the `SubAgentDagTool` is appended today when
   "any CLI tools exist"; change it to "any sub-agent tools exist" (CLI or
   OpenAI). OpenAI instances honor the `call(prompt, instance, prompt_file,
   output_file)` contract, so the DAG runner orchestrates and same-instance-chains
   them with no runner change.

## 8. Presets — `_presets.py`

Add a MiroMind preset (flagship). `credential_id` ships empty — the user must
attach their own `OpenAICompatibleCredential` (the frontend routes OpenAI presets
through the pre-filled edit form; see §9.4):

```python
{
    "preset_id": "miromind_deepresearch",
    "label": "MiroThinker Deep Research (MiroMind)",
    "data": {
        "type": "openai_subagent",
        "name": "miro_deepresearch",
        "description": (
            "Delegate a deep-research question to MiroMind "
            "mirothinker-1-7-deepresearch (web search, code execution, "
            "tool use). Returns a sourced report with citations. Stateful: "
            "reuse an instance handle to continue the same research thread."
        ),
        "credential_id": "",
        "model": "mirothinker-1-7-deepresearch",
        "stateful": True,
        "system_prompt": None,
        "temperature": None,
        "max_tokens": None,
        "timeout": 1200,
    },
}
```

Only the flagship ships as a preset. The `-mini` variant
(`model="mirothinker-1-7-deepresearch-mini"`) is not shipped; a user who wants it
creates an `openai_subagent` config by hand (same shape, different `model`).

`list_subagent_presets()` returns these alongside the Claude/Codex presets.

## 9. Frontend — `/subagents` page + API types

### 9.1 TS API types (`src/api/types.ts`)

`SubAgentData` becomes a discriminated union on `type`:

```ts
export interface CliSubAgentData {
  type: 'cli_subagent';
  name: string; description: string; command: string;
  resume_command?: string | null;
  id_source?: 'provisioned' | 'derived';
  session_id_pattern?: string | null; output_pattern?: string | null;
  transcript_format?: 'text' | 'codex_jsonl';
  cwd?: string | null; env?: Record<string, string> | null; timeout?: number;
}
export interface OpenAISubAgentData {
  type: 'openai_subagent';
  name: string; description: string;
  credential_id: string; model: string;
  stateful?: boolean; system_prompt?: string | null;
  temperature?: number | null; max_tokens?: number | null; timeout?: number;
}
export type SubAgentData = CliSubAgentData | OpenAISubAgentData;
```

`SubAgentPreset`, `SubAgentView` reference the union unchanged.

### 9.2 Form state + type selector

`FormState` gains `type: 'cli_subagent' | 'openai_subagent'` and the OpenAI
fields (`credential_id`, `model`, `stateful`, `system_prompt`, `temperature`,
`max_tokens`; `timeout` is shared). The type selector is shown **only on create**
(a segmented control / select); on edit the type is fixed to the record's
`data.type` and the selector is read-only. `EMPTY_FORM` defaults `type` to
`cli_subagent` (preserving current behavior).

### 9.3 Conditional fields + credential picker

- `type === 'cli_subagent'` → the existing command / resume_command / cwd / env
  fields (unchanged).
- `type === 'openai_subagent'` → **credential picker**, model, stateful toggle,
  system prompt, temperature, max tokens.
  - The credential picker reuses `useCredentials()` (`credentialApi.list()`),
    filtered to `data.type === 'openai_compatible_credential'`, rendered as a
    `<select>` of credential id → label; an empty list shows a hint linking to
    `/credential`.
  - Model is a text input pre-filled from the preset
    (`mirothinker-1-7-deepresearch`), optionally augmented with a `<datalist>`
    from `credentialApi.listModels(credential_id)` when the chosen credential
    exposes a model catalog.
- `timeout` is shared by both.

`toForm` reads `view.data.type` and populates the matching branch; `toPayload`
emits `{ type: 'cli_subagent', … }` or `{ type: 'openai_subagent', name,
description, credential_id, model, stateful, system_prompt|null,
temperature|null, max_tokens|null, timeout }`.

### 9.4 Validation + preset UX

- Client-side `canSubmit` branches on type: CLI keeps the existing
  `{prompt}`/`{agent_id}` command checks; OpenAI requires non-empty `name`,
  `description`, `credential_id`, and `model` (plus range checks mirroring the
  backend for temperature/max_tokens).
- `addFromPreset`: for `preset.data.type === 'openai_subagent'`, **open the
  pre-filled create form** (so the user picks a credential) instead of the
  current blind `create()`; CLI presets keep one-click add.

### 9.5 List badge + i18n

- The stateful badge in the prototype list currently keys off `resume_command`;
  generalize to `sa.data.resume_command || sa.data.stateful`.
- Add en/zh i18n keys for: type selector + option labels, credential label +
  empty hint, model label, stateful label, system-prompt label, temperature
  label, max-tokens label, and any OpenAI-specific hints.

## 10. Testing

Backend (mirrors the existing fake-transport pattern — a fake `httpx` client
returning canned JSON is the HTTP analog of `_CannedBackend`):

- **`subagent_openai_config_test.py`** (new): `OpenAISubAgentConfig` validators —
  name regex, empty `credential_id`/`model` rejected, temperature range,
  max_tokens/timeout bounds, `stateful` default, round-trip through
  `SubAgentFactory.from_dict`.
- **`subagent_openai_tool_test.py`** (new): with a fake HTTP transport —
  - stateless-shaped create: asserts request URL/headers (Bearer)/body
    (`model`, `messages=[…user]`, `stream:false`) and that the reply is
    `content` with `citations`/`usage` metadata;
  - stateful create then resume: history file is written, resume replays prior
    turns (assert the second request's `messages[]` includes the earlier
    user+assistant turns), `registry.commit` fires only after a successful
    create;
  - `system_prompt` prepended once;
  - `prompt_file` supplied → its contents become the user message;
    `output_file` supplied → the **full** content is written to it while the
    returned `text` is truncated at `_MAX_OUTPUT_CHARS` (assert both);
  - error paths: non-2xx with `{"error":{…}}` body, timeout, `finish_reason:
    error`, missing content → terminal `ERROR`, no history write / no commit on
    create.
  - Use `AnyString`/`AnyValue` for `agent_id`, timestamps, and file paths.
- **`subagent_factory_test.py`**: `OpenAISubAgentConfig` registered; schema list
  includes `openai_subagent`.
- **`subagent_presets_test.py`**: the MiroMind flagship preset is present, with
  the correct id/label, and `from_dict` round-trips the preset `data` (with a
  placeholder `credential_id` for validation, since the shipped preset's is
  empty).
- **`subagent_agent_tools_test.py`**: an `openai_subagent` config with a resolved
  credential builds an `OpenAISubAgentTool`; a missing/wrong-type credential
  skips the tool with a warning; the `SubAgentDagTool` is appended when only
  OpenAI tools exist.

Frontend: manual verification of the `/subagents` page — type selector,
credential picker, OpenAI-preset-opens-prefilled-form, save/edit round-trip,
validation gating. (No JS test harness is established in the repo for this page;
follow existing convention.)

## 11. Files touched

**Backend (new):**
- `src/agentscope/subagent/_openai_tool.py` — `OpenAISubAgentTool`.
- `tests/subagent_openai_config_test.py`, `tests/subagent_openai_tool_test.py`.

**Backend (modified):**
- `src/agentscope/subagent/_base.py` — add `OpenAISubAgentConfig` + validators.
- `src/agentscope/subagent/_factory.py` — register the new class.
- `src/agentscope/subagent/_agent_tools.py` — construction branch (credential
  resolution) + generalized DAG-tool gate.
- `src/agentscope/subagent/_presets.py` — MiroMind flagship preset.
- `src/agentscope/subagent/__init__.py` — export `OpenAISubAgentConfig`
  (+ `OpenAISubAgentTool` if the package exposes tools).
- `tests/subagent_factory_test.py`, `tests/subagent_presets_test.py`,
  `tests/subagent_agent_tools_test.py` — extend for the new type.

**Frontend (modified):**
- `examples/web_ui/frontend/src/api/types.ts` — discriminated union.
- `examples/web_ui/frontend/src/pages/subagent/index.tsx` — type selector,
  conditional fields, credential picker, validation, preset UX, badge.
- `examples/web_ui/frontend/src/i18n/locales/en.json`, `zh.json` — new keys.

**Unchanged:** the CLI transport (`_tool.py`, `_transcript.py`), the instance
registry + record, the DAG runner/graph/render/placeholder grammar, the
credential module + router, and the sub-agent router routes/schemas
(`data: dict` already defers validation to the factory).

## 12. Risks & notes

- **Long deep-research calls.** Non-streaming with `timeout=1200s`; a slow
  research run could hit the timeout or an upstream idle-timeout. Accepted for
  v1; SSE streaming (consuming heartbeats, aggregating the final content) is a
  documented follow-up if timeouts prove common.
- **History growth / cost.** Stateful replay resends the whole transcript each
  turn (bounded by the 256k window). This is the user-chosen trade-off for
  multi-turn context; the `stateful=false` escape hatch exists.
- **Secret handling.** The plaintext key exists only in process memory for the
  duration of a call (resolved per turn from `SecretStr`); it is never written to
  the config, the history file, logs, or tool metadata.
- **Preset with empty `credential_id`.** Enforced by the frontend pre-fill flow;
  a direct API POST of the raw preset would fail validation (`credential_id`
  required) — intended.
