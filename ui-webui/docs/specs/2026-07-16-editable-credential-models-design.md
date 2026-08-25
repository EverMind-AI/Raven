# Design: Editable per-credential models + custom OpenAI-compatible provider

**Date:** 2026-07-16
**Status:** Approved (design), pending implementation plan
**Area:** `examples/web_ui` credential page (`/credential`) + `src/agentscope` credential/model layers

## 1. Goal

Two user-facing capabilities on the `/credential` page:

1. **Edit "Available Models"** — the per-credential model list, today a read-only
   static catalog, becomes editable (add / edit / delete model cards; display order
   = array order), persisted per user, per credential.
2. **Add a custom "OpenAI-Compatible API"** — a first-class credential type with a
   required `base_url` + `api_key`, whose models are defined entirely by the user
   (no built-in catalog), so custom gateways (e.g. `http://8.141.31.123:3000/v1`
   serving `deepseek-v4-flash`, `glm-5.2`, `hy3`) work without dropping YAML files
   into the installed package.

Both are the same mechanism — a **per-credential model override list** — seen from
two angles.

## 2. Background: current behavior

- **"Available Models"** is a static, package-bundled catalog. `GET /model/?provider=<type>`
  (`app/_router/_model.py`) → `credential_cls.get_chat_model_class().list_models()`
  → `ChatModelBase.list_models()` (`model/_base.py:109`) globs `*.yaml` files next to
  each provider's chat-model class. The list is keyed **by provider type**, not per
  credential or per user; the same list feeds the chat model picker
  (`LlmSelect` via `useAvailableModels`).
- **Custom OpenAI-compatible** is half-supported: `OpenAICredential` already has a
  `base_url` field (`credential/_openai.py:36`), but no way to declare which models
  the endpoint serves — you get the built-in gpt-4o/o3/... catalog regardless.
- **Credentials** are per-user Redis records with a free-form `data: dict`
  (`app/storage/_model/_credential.py`), typed by a `type` discriminator via
  `CredentialFactory`, with full CRUD + resource-sharing + secret-masking already
  wired (`app/_router/_credential.py`, `app/_service/_access.py`).
- **Credential forms are schema-driven**: `CreateCredentialDialog` /
  `EditCredentialDialog` render fields from `credentialApi.schemas()` via `SchemaForm`.
  A new credential type appears automatically with an auto-rendered form.

## 3. Key constraint: import direction is model → credential

`model/` imports `credential/` at module top level (e.g. `model/_base.py:18`
`from ..credential import CredentialBase`; `model/_openai_chat/_model.py:16`
`from ...credential import OpenAICredential`). Therefore **`credential/` must never
import `model/` at module top level** — doing so creates a cycle.

Consequences honored by this design:
- `CredentialBase` stores the override as **plain dicts** (`list[dict] | None`), not
  typed `ModelCard`, so no `model` import at class-definition time.
- Any code that *builds* `ModelCard`s from those dicts does the `model` import
  **lazily, inside a method body** (the established pattern in `credential/_base.py`,
  where `list_models` already imports `model` lazily).

`ChatModelBase.__init__` is a plain class doing `self.credential = credential` with
**no runtime type validation** (`model/_base.py:90`), so a new `CredentialBase`
subclass exposing `api_key` / `base_url` / `organization` drives `OpenAIChatModel`
correctly even though its `__init__` is annotated `credential: OpenAICredential`.

## 4. Data model changes (`src/agentscope`)

### 4.1 `ModelCard.from_config` (`model/_model_card.py`)

Extract the config→card logic from `from_yaml` into a classmethod:

```
@classmethod
def from_config(cls, config: dict, parameter_class: Type[BaseModel]) -> Self: ...
```

`from_yaml` becomes: open file → `yaml.safe_load` → `cls.from_config(config, parameter_class)`.
`from_config` performs the existing merge (compute `parameter_schema` from
`parameter_class.model_json_schema()`, apply thinking/voice/max_tokens auto-filters,
apply `parameter_overrides`). One tolerance added vs. today's `label=config["label"]`:
`from_config` defaults `label` to `name` when the key is absent/None (all bundled YAMLs
carry `label`, so their output is byte-for-byte unchanged) — this lets the UI leave
`label` blank. Otherwise a pure refactor + new entry point.

### 4.2 `CredentialBase` gains an override list + effective-models accessor (`credential/_base.py`)

- New field:
  ```
  models: list[dict] | None = Field(
      default=None,
      description="Per-credential model-card config overrides (YAML-equivalent "
                  "dicts). None = inherit the built-in static catalog.",
  )
  ```
- New **instance** method `list_effective_models(self) -> list[ModelCard]`:
  - `self.models is None` → `type(self).list_models()` (the **existing** classmethod, which
    is the static-catalog hook — a provider changes its catalog by overriding it).
  - otherwise → `[ModelCard.from_config(c, self.get_chat_model_class().Parameters) for c in self.models]`
  - **Lazy import of `model` inside the body.**
- `list_models` (classmethod) is left as the static-catalog hook. The `GET /model/?provider=`
  endpoint switches to `credential_cls.list_models()` (behavior-identical for built-ins,
  empty for the custom type — see §4.3, §5.2).

### 4.3 New credential type `OpenAICompatibleCredential` (`credential/_openai_compatible.py`)

```
class OpenAICompatibleCredential(CredentialBase):
    model_config = ConfigDict(title="OpenAI-Compatible API")
    type: Literal["openai_compatible_credential"] = "openai_compatible_credential"
    api_key: SecretStr                       # required
    base_url: str                            # required (the point of the type)
    organization: str | None = None

    @classmethod
    def get_chat_model_class(cls): from ..model import OpenAIChatModel; return OpenAIChatModel

    @classmethod
    def list_models(cls): return []            # starts blank; models come from override
```

- TTS / embedding default to empty / `None` (inherited from `CredentialBase`), so a
  bare custom endpoint advertises no bogus TTS/embedding models. (Out of scope:
  custom embedding/TTS endpoints.)
- Registered in `CredentialFactory._classes` and exported from `credential/__init__.py`.

## 5. API changes (`src/agentscope/app`)

### 5.1 New request schema (`_router/_schema/_credential.py` + `__init__` export)

```
class ModelCardConfig(BaseModel):
    name: str
    label: str | None = None            # defaults to name at build time
    status: Literal["active","deprecated","sunset"] = "active"
    deprecated_at: datetime | None = None
    input_types: list[str] = ["text/plain"]
    output_types: list[str] = ["text/plain"]
    context_size: int = Field(gt=0)
    output_size: int = Field(gt=0)
    parameter_overrides: dict = {}

class UpdateCredentialModelsRequest(BaseModel):
    models: list[ModelCardConfig] | None   # None = revert to static catalog
```

`ListModelsResponse` (existing, `_router/_schema/_model.py`) is reused for responses.

### 5.2 New endpoints (`_router/_credential.py`)

- **`GET /credential/{credential_id}/models`** → `ListModelsResponse`
  - `record = await access.resolve_credential(user_id, credential_id)` (own or shared;
    404 if neither). Model cards are not secret, so shared creds may list models.
  - `cred = CredentialFactory.from_dict(record.data)`; return
    `ListModelsResponse(models=cred.list_effective_models(), total=len(...))`.
- **`PUT /credential/{credential_id}/models`** → `ListModelsResponse`
  - `owner_id, _ = await access.resolve_for_edit(user_id, ResourceKind.CREDENTIAL, credential_id)`
    (403/404 semantics identical to existing update/delete).
  - Load owner's raw record via `storage.get_credential(owner_id, credential_id)`,
    `CredentialFactory.from_dict`, set
    `.models = [m.model_dump(mode="json") for m in body.models]` (or `None`),
    `storage.upsert_credential(owner_id, cred)`. `mode="json"` keeps `deprecated_at`
    an ISO string so the record serializes cleanly into Redis and re-parses on read.
  - **Validation:** before writing, build each card via `ModelCard.from_config` (using
    the credential's chat-model `Parameters`) → invalid config raises **422**.
  - Return the recomputed effective list.

- **`GET /model/?provider=`** is **kept** (the editor's "import built-in catalog" action),
  but its handler switches from `get_chat_model_class().list_models()` to
  `credential_cls.list_models()` so the custom type reports an empty catalog rather than
  the built-in OpenAI list. Behavior-identical for built-in providers.

**Override semantics:** `null` = inherit static catalog; `[]` = explicit empty list;
`[...]` = override.

## 6. Frontend changes (`examples/web_ui/frontend`)

### 6.1 API + types (`src/api/credential.ts`, `src/api/types.ts`)

- `credentialApi.listModels(id)` → `GET /credential/${id}/models` → `ListModelResponse`
- `credentialApi.updateModels(id, models)` → `PUT /credential/${id}/models`
- `ModelCardConfig` TS type mirroring §5.1.

### 6.2 Per-credential model fetch

- `useAvailableModels` (`src/hooks/useAvailableModels.ts`): fetch models per credential
  via `credentialApi.listModels(credential.id)` instead of `modelApi.list(type)`. The
  chat picker (`LlmSelect`) then reflects overrides and custom endpoints.
- Credential detail panel (`src/pages/credential/index.tsx`): "Available Models" section
  fetches per credential.

### 6.3 `ManageModelsDialog` (new — `src/components/dialog/ManageModelsDialog.tsx`)

- Opened from an **Edit** button on the "Available Models" header, shown only when
  `credential.editable`.
- Lists the current effective model configs; per-row edit / delete; "+ Add model".
- Per-model form exposes the **full ModelCard (YAML-level) fields**: name, label, status,
  deprecated_at, input_types (multi), output_types (multi), context_size, output_size,
  and an advanced `parameter_overrides` JSON editor.
- "Import built-in catalog" action (built-in providers only): loads `modelApi.list(type)`
  cards as editable starting configs.
- Save → `credentialApi.updateModels(id, configs)` → refetch panel + picker.

### 6.4 Custom provider form — no bespoke code

`OpenAICompatibleCredential` appears automatically in the "Add provider" list and its
create/edit form renders from its JSON schema via `SchemaForm`. Only i18n / labels needed.

### 6.5 i18n (`src/i18n/locales`)

New keys (en + zh) for: edit-models button, add/delete/import actions, per-field labels,
dialog title/description, and the custom-provider display strings.

## 7. Testing (`tests/*_test.py`)

TDD; whole-structure assertions; `AnyString` / `AnyValue` from `tests/utils.py` for
nondeterministic fields (e.g. generated ids).

- `ModelCard.from_config` parity: a config dict yields the same `ModelCard` as the
  equivalent YAML via `from_yaml` (golden regression).
- `CredentialBase.list_effective_models`: `None` → static catalog; `[...]` → override
  cards; `[]` → empty.
- `OpenAICompatibleCredential`: empty by default; honors override; `get_chat_model_class`
  is `OpenAIChatModel`; registered in `CredentialFactory`.
- Router: `GET /credential/{id}/models` (own + shared) and `PUT` happy path, `PUT` with
  invalid config → 422, edit on read-only/shared → 403, unknown id → 404.

## 8. End-to-end verification

Boot the service + web UI (per `ravenx-webui-run-setup`), add an "OpenAI-Compatible API"
credential (base_url = the gateway), add models (`deepseek-v4-flash`, ...), confirm they
appear in **Available Models** *and* the **chat model picker**, and that one chat turn
against the endpoint succeeds.

## 9. Footprint & compatibility

- Backend: edits to `credential/_base.py`, `credential/_factory.py`, `credential/__init__.py`,
  `model/_model_card.py`, `app/_router/_credential.py`, `app/_router/_model.py`,
  `app/_router/_schema/_credential.py` (+ `__init__` export); new file
  `credential/_openai_compatible.py`.
- Frontend: new `ManageModelsDialog.tsx`; edits to `pages/credential/index.tsx`,
  `hooks/useAvailableModels.ts`, `api/credential.ts`, `api/types.ts`, locale files.
- **Fully additive:** when `models` is unset (`None`), every existing behavior is
  identical — same static catalog, same picker, same serialized records.
