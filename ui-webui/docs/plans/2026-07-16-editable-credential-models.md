# Editable Per-Credential Models + Custom OpenAI-Compatible Provider — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users edit a credential's "Available Models" list from the `/credential` page and register a custom "OpenAI-Compatible API" provider whose models they define themselves — without editing package YAML files.

**Architecture:** A credential gains an optional per-credential model-override list (`models: list[dict] | None`, `None` = inherit the static YAML catalog). A new `OpenAICompatibleCredential` type starts with an empty catalog so its models come entirely from the override. Two new endpoints (`GET`/`PUT /credential/{id}/models`) read/replace the override; the frontend adds a `ManageModelsDialog` and points the model picker at the per-credential endpoint. Fully additive: unset `models` reproduces today's behavior exactly.

**Tech Stack:** Python 3.11+, Pydantic v2, FastAPI (backend); React + TypeScript + Vite, Tailwind, shadcn-style UI primitives (frontend); pytest + FastAPI TestClient (backend tests).

## Global Constraints

- **Encapsulation:** internal files/classes/functions are `_`-prefixed; public surface is re-exported through `__init__.py`. Copy verbatim.
- **Import direction is model → credential.** `credential/` MUST NOT import `model/` at module top level. Any `ModelCard`-building code in `credential/` imports `model` **lazily inside the method body**.
- **Lazy imports:** third-party SDKs imported at point of use, never at file top.
- **Docstrings:** English only, `Args:`/`Returns:` template with backtick-typed params.
- **Black line length 79; flake8/pylint/mypy/pre-commit must pass.** Do not skip hooks.
- **Tests:** live in `tests/*_test.py`; assert **whole data structures**; use `AnyString`/`AnyValue` from `tests/utils.py` for nondeterministic fields.
- **Commits:** Conventional Commits (`feat/fix/refactor/test(scope): ...`). End each commit message body with the `Co-Authored-By` trailer the harness requires. Do not commit unless the plan step says to.
- **Override semantics (invariant across all tasks):** `models is None` → inherit static catalog; `models == []` → explicit empty; `models == [...]` → override.

---

## File Structure

**Backend — modify:**
- `src/agentscope/model/_model_card.py` — add `from_config`; `from_yaml` delegates to it.
- `src/agentscope/credential/_base.py` — add `models` field, `_static_model_cards`, `list_effective_models`.
- `src/agentscope/credential/_factory.py` — register `OpenAICompatibleCredential`.
- `src/agentscope/credential/__init__.py` — export `OpenAICompatibleCredential`.
- `src/agentscope/app/_router/_credential.py` — add `GET`/`PUT /{id}/models`.
- `src/agentscope/app/_router/_model.py` — catalog endpoint honors `credential_cls.list_models()`.
- `src/agentscope/app/_router/_schema/_credential.py` — add `ModelCardConfig`, `UpdateCredentialModelsRequest`.
- `src/agentscope/app/_router/_schema/__init__.py` — export the two new schemas.

**Backend — create:**
- `src/agentscope/credential/_openai_compatible.py` — new `OpenAICompatibleCredential`.

**Backend — tests (create):**
- `tests/model_card_from_config_test.py`
- `tests/credential_effective_models_test.py`
- `tests/credential_openai_compatible_test.py`
- `tests/credential_models_router_test.py`

**Frontend — modify:**
- `examples/web_ui/frontend/src/api/client.ts` — add `put`.
- `examples/web_ui/frontend/src/api/credential.ts` — add `listModels`, `updateModels`.
- `examples/web_ui/frontend/src/api/types.ts` — add `ModelCardConfig`, `UpdateCredentialModelsRequest`.
- `examples/web_ui/frontend/src/hooks/useAvailableModels.ts` — fetch per credential.
- `examples/web_ui/frontend/src/pages/credential/index.tsx` — per-credential fetch + Edit button.
- `examples/web_ui/frontend/src/i18n/locales/*` — new keys (en + zh).

**Frontend — create:**
- `examples/web_ui/frontend/src/components/dialog/ManageModelsDialog.tsx`.

---

## Task 1: `ModelCard.from_config` (refactor `from_yaml`)

**Files:**
- Modify: `src/agentscope/model/_model_card.py:72-159`
- Test: `tests/model_card_from_config_test.py`

**Interfaces:**
- Consumes: `ModelCard.from_yaml(yaml_path, parameter_class)` (existing).
- Produces: `ModelCard.from_config(config: dict, parameter_class: Type[BaseModel]) -> ModelCard` — builds a card from a YAML-equivalent dict, computing `parameter_schema` from `parameter_class`. `label` defaults to `config["name"]` when absent/None. Requires `name`, `context_size`, `output_size`.

- [ ] **Step 1: Write the failing tests**

Create `tests/model_card_from_config_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for ModelCard.from_config and its parity with from_yaml."""
import yaml

from agentscope.model import ModelCard, OpenAIChatModel


def test_from_config_matches_from_yaml(tmp_path) -> None:
    """from_config(dict) equals from_yaml(file) for the same config."""
    config = {
        "name": "custom-model",
        "label": "Custom Model",
        "status": "active",
        "input_types": ["text/plain"],
        "output_types": ["text/plain"],
        "context_size": 1024,
        "output_size": 512,
    }
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    from_yaml = ModelCard.from_yaml(str(path), OpenAIChatModel.Parameters)
    from_config = ModelCard.from_config(config, OpenAIChatModel.Parameters)

    assert from_config == from_yaml


def test_from_config_defaults_label_to_name() -> None:
    """A config without a label falls back to the model name."""
    card = ModelCard.from_config(
        {"name": "no-label", "context_size": 8, "output_size": 4},
        OpenAIChatModel.Parameters,
    )
    assert card.label == "no-label"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/model_card_from_config_test.py -v`
Expected: FAIL — `AttributeError: type object 'ModelCard' has no attribute 'from_config'`.

- [ ] **Step 3: Implement `from_config` and delegate `from_yaml` to it**

In `src/agentscope/model/_model_card.py`, replace the whole `from_yaml` classmethod (lines 72-159) with the two methods below. `from_config` holds all the existing merge logic; `from_yaml` becomes a thin file loader.

```python
    @classmethod
    def from_config(
        cls,
        config: dict,
        parameter_class: Type[BaseModel],
    ) -> Self:
        """Build a model card from a config dict, merging the parameter
        schema with the parameter class the same way :meth:`from_yaml` does.

        Args:
            config (`dict`):
                The model card config (YAML-equivalent). Requires ``name``,
                ``context_size`` and ``output_size``; ``label`` defaults to
                ``name`` when absent.
            parameter_class (`Type[BaseModel]`):
                The parameter class (e.g., ``OpenAIChatModel.Parameters``).

        Returns:
            `ModelCard`:
                A model card with a computed ``parameter_schema``.
        """
        # Get base schema from parameter class
        base_schema = parameter_class.model_json_schema()
        properties = copy.deepcopy(base_schema.get("properties", {}))

        # Auto-filter: remove thinking parameters if not supported
        output_types = config.get("output_types", [])
        if "application/x-thinking" not in output_types:
            properties.pop("thinking_enable", None)
            properties.pop("thinking_budget", None)

        # Auto-filter: only omni-style models that declare an ``audio/*``
        # output type expose the ``voice`` parameter to the frontend popover.
        if not any(
            isinstance(t, str) and t.startswith("audio/") for t in output_types
        ):
            properties.pop("voice", None)

        # Auto-inject: set max_tokens maximum from output_size
        if "max_tokens" in properties and "output_size" in config:
            properties["max_tokens"]["maximum"] = config["output_size"]

        # Apply parameter_overrides with simple dict merge
        overrides = config.get("parameter_overrides", {})
        for param_name, override in overrides.items():
            if override is None:
                # null means remove
                properties.pop(param_name, None)
                continue

            if isinstance(override, dict):
                # Check for hidden flag
                if override.get("hidden"):
                    properties.pop(param_name, None)
                    continue

                # Simple dict merge: {**base, **override}
                if param_name in properties:
                    properties[param_name] = {
                        **properties[param_name],
                        **override,
                    }

        # Build final parameter schema
        final_schema = {
            "type": "object",
            "properties": properties,
            "required": base_schema.get("required", []),
        }

        # Create ModelCard instance
        return cls(
            name=config["name"],
            label=config.get("label") or config["name"],
            status=config.get("status", "active"),
            deprecated_at=config.get("deprecated_at"),
            input_types=config.get("input_types", ["text/plain"]),
            output_types=config.get("output_types", ["text/plain"]),
            context_size=config["context_size"],
            output_size=config["output_size"],
            parameter_schema=final_schema,
            parameters_overrides=config.get("parameter_overrides", {}),
        )

    @classmethod
    def from_yaml(
        cls,
        yaml_path: str,
        parameter_class: Type[BaseModel],
    ) -> Self:
        """Read a model card from a YAML file and build it via
        :meth:`from_config`.

        Args:
            yaml_path (`str`):
                Path to the YAML file.
            parameter_class (`Type[BaseModel]`):
                The parameter class (e.g., ``OpenAIChatModel.Parameters``).

        Returns:
            `ModelCard`:
                A model card with a merged parameter schema.
        """
        with open(yaml_path, "r", encoding="utf-8") as file:
            config = yaml.safe_load(file)
        return cls.from_config(config, parameter_class)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/model_card_from_config_test.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Guard against regressions in existing model tests**

Run: `pytest tests/model_openai_chat_test.py tests/model_base_test.py -v`
Expected: PASS (no regressions from the refactor).

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/model/_model_card.py tests/model_card_from_config_test.py
git commit -m "refactor(model): extract ModelCard.from_config from from_yaml"
```

---

## Task 2: `CredentialBase` override field + effective-models accessor

**Files:**
- Modify: `src/agentscope/credential/_base.py`
- Test: `tests/credential_effective_models_test.py`

**Interfaces:**
- Consumes: `ModelCard.from_config` (Task 1); `CredentialBase.list_models()` and `CredentialBase.get_chat_model_class()` (both existing).
- Produces:
  - `CredentialBase.models: list[dict] | None = None` — override configs; `None` = inherit catalog.
  - `CredentialBase.list_effective_models(self) -> list[ModelCard]` — override cards if `models` set, else `type(self).list_models()` (the existing static-catalog classmethod, which providers override to change the catalog).

- [ ] **Step 1: Write the failing tests**

Create `tests/credential_effective_models_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for per-credential model overrides on CredentialBase."""
from agentscope.credential import OpenAICredential
from agentscope.model import ModelCard, OpenAIChatModel


def _by_name(cards: list[ModelCard]) -> list[ModelCard]:
    return sorted(cards, key=lambda c: c.name)


def test_effective_models_defaults_to_static_catalog() -> None:
    """models=None yields the provider's built-in YAML catalog."""
    cred = OpenAICredential(api_key="sk-x")
    assert _by_name(cred.list_effective_models()) == _by_name(
        OpenAICredential.get_chat_model_class().list_models(),
    )


def test_effective_models_uses_override() -> None:
    """A set models list overrides the static catalog."""
    cfg = {
        "name": "m1",
        "label": "M1",
        "context_size": 100,
        "output_size": 50,
    }
    cred = OpenAICredential(api_key="sk-x", models=[cfg])
    assert cred.list_effective_models() == [
        ModelCard.from_config(cfg, OpenAIChatModel.Parameters),
    ]


def test_effective_models_empty_list_is_empty() -> None:
    """An explicit empty list shows no models."""
    cred = OpenAICredential(api_key="sk-x", models=[])
    assert cred.list_effective_models() == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/credential_effective_models_test.py -v`
Expected: FAIL — `TypeError`/validation error (`models` unknown) or `AttributeError: 'OpenAICredential' object has no attribute 'list_effective_models'`.

- [ ] **Step 3: Add the field and methods to `CredentialBase`**

In `src/agentscope/credential/_base.py`:

(a) Add the `models` field right after the `name` field (after line 27):

```python
    models: list[dict] | None = Field(
        default=None,
        description=(
            "Per-credential model-card config overrides "
            "(YAML-equivalent dicts). ``None`` means inherit the built-in "
            "static catalog; an empty list means no models."
        ),
    )
```

(b) Add this instance method to `CredentialBase` (place after the existing `list_models` classmethod, which stays unchanged). It falls back to `type(self).list_models()` — the existing static-catalog hook — so a provider changes its catalog by overriding `list_models` (see Task 3). The `model` import stays inside the method body to preserve the model→credential import direction:

```python
    def list_effective_models(self) -> list["ModelCard"]:
        """Return the models this credential actually offers.

        When :attr:`models` is ``None`` the built-in static catalog
        (:meth:`list_models`) is returned; otherwise each override config is
        built into a :class:`ModelCard`.

        Returns:
            `list[ModelCard]`:
                The effective model cards for this credential.
        """
        if self.models is None:
            return type(self).list_models()

        from ..model import ModelCard

        parameter_class = self.get_chat_model_class().Parameters
        return [
            ModelCard.from_config(config, parameter_class)
            for config in self.models
        ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/credential_effective_models_test.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Verify no import cycle was introduced**

Run: `python -c "import agentscope.credential; import agentscope.model; print('ok')"`
Expected: prints `ok` (no `ImportError`/circular import).

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/credential/_base.py tests/credential_effective_models_test.py
git commit -m "feat(credential): add per-credential model override + list_effective_models"
```

---

## Task 3: `OpenAICompatibleCredential` type + registration

**Files:**
- Create: `src/agentscope/credential/_openai_compatible.py`
- Modify: `src/agentscope/credential/_factory.py`, `src/agentscope/credential/__init__.py`
- Test: `tests/credential_openai_compatible_test.py`

**Interfaces:**
- Consumes: `CredentialBase` (Task 2), `OpenAIChatModel` (existing), `CredentialFactory` (existing).
- Produces: `OpenAICompatibleCredential` — `type == "openai_compatible_credential"`, required `api_key: SecretStr` + `base_url: str`, optional `organization`; `get_chat_model_class()` → `OpenAIChatModel`; `list_models()` → `[]` (empty built-in catalog); registered in `CredentialFactory` and exported.

- [ ] **Step 1: Write the failing tests**

Create `tests/credential_openai_compatible_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for the OpenAI-compatible custom credential type."""
import pytest

from agentscope.credential import (
    CredentialFactory,
    OpenAICompatibleCredential,
)
from agentscope.model import ModelCard, OpenAIChatModel


def test_registered_in_factory() -> None:
    """The factory resolves the new discriminator to the new class."""
    cls = CredentialFactory.get_credential_class(
        "openai_compatible_credential",
    )
    assert cls is OpenAICompatibleCredential


def test_chat_model_class_is_openai() -> None:
    """It reuses the OpenAI chat model client path."""
    assert OpenAICompatibleCredential.get_chat_model_class() is OpenAIChatModel


def test_empty_catalog_by_default() -> None:
    """No built-in catalog — models come from the override only."""
    cred = OpenAICompatibleCredential(
        api_key="sk-x",
        base_url="http://host/v1",
    )
    assert cred.list_effective_models() == []


def test_override_models_listed() -> None:
    """A configured model shows up in the effective list."""
    cfg = {
        "name": "deepseek-v4-flash",
        "label": "deepseek-v4-flash",
        "context_size": 1024000,
        "output_size": 1024000,
    }
    cred = OpenAICompatibleCredential(
        api_key="sk-x",
        base_url="http://host/v1",
        models=[cfg],
    )
    assert cred.list_effective_models() == [
        ModelCard.from_config(cfg, OpenAIChatModel.Parameters),
    ]


def test_base_url_is_required() -> None:
    """base_url has no default — omitting it is a validation error."""
    with pytest.raises(Exception):
        OpenAICompatibleCredential(api_key="sk-x")


def test_from_dict_roundtrip() -> None:
    """The factory deserializes a stored dict to the typed instance."""
    cred = CredentialFactory.from_dict(
        {
            "type": "openai_compatible_credential",
            "api_key": "sk-x",
            "base_url": "http://host/v1",
        },
    )
    assert isinstance(cred, OpenAICompatibleCredential)
    assert cred.base_url == "http://host/v1"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/credential_openai_compatible_test.py -v`
Expected: FAIL — `ImportError: cannot import name 'OpenAICompatibleCredential'`.

- [ ] **Step 3: Create the credential class**

Create `src/agentscope/credential/_openai_compatible.py`:

```python
# -*- coding: utf-8 -*-
"""The OpenAI-compatible custom credential."""
from typing import Literal, Type, TYPE_CHECKING

from pydantic import ConfigDict, Field, SecretStr

from ._base import CredentialBase

if TYPE_CHECKING:
    from ..model import ChatModelBase, ModelCard


class OpenAICompatibleCredential(CredentialBase):
    """A credential for any OpenAI-compatible API endpoint.

    Unlike :class:`OpenAICredential`, ``base_url`` is required and the
    built-in model catalog is empty: the models this endpoint serves are
    supplied entirely through the per-credential ``models`` override.
    """

    model_config = ConfigDict(
        title="OpenAI-Compatible API",
    )

    type: Literal["openai_compatible_credential"] = (
        "openai_compatible_credential"
    )
    """The credential type."""

    api_key: SecretStr = Field(
        description="The API key for the OpenAI-compatible endpoint.",
        title="API Key",
    )
    """The API key."""

    base_url: str = Field(
        title="API Base URL",
        description=(
            "The base URL of the OpenAI-compatible endpoint, e.g. "
            "``http://host:port/v1``."
        ),
    )
    """The required base URL."""

    organization: str | None = Field(
        default=None,
        title="Organization",
        description="Optional organization ID.",
    )
    """The optional organization ID."""

    @classmethod
    def get_chat_model_class(cls) -> Type["ChatModelBase"]:
        """Return the OpenAIChatModel class."""
        from ..model import OpenAIChatModel

        return OpenAIChatModel

    @classmethod
    def list_models(cls) -> list["ModelCard"]:
        """Custom endpoints have no built-in catalog — models come from the
        per-credential override only.

        Returns:
            `list[ModelCard]`:
                Always an empty list.
        """
        return []
```

- [ ] **Step 4: Register and export the class**

(a) In `src/agentscope/credential/_factory.py`, add the import (keep alphabetical grouping near the other credential imports, after `_openai`):

```python
from ._openai_compatible import OpenAICompatibleCredential
```

and add `OpenAICompatibleCredential` to the `_classes` list (after `OpenAICredential`):

```python
    _classes: list[Type[CredentialBase]] = [
        AnthropicCredential,
        DashScopeCredential,
        DeepSeekCredential,
        GeminiCredential,
        MoonshotCredential,
        OllamaCredential,
        OpenAICredential,
        OpenAICompatibleCredential,
        XAICredential,
    ]
```

(b) In `src/agentscope/credential/__init__.py`, add the import (after the `_openai` import) and the `__all__` entry (after `"OpenAICredential"`):

```python
from ._openai_compatible import OpenAICompatibleCredential
```
```python
    "OpenAICompatibleCredential",
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/credential_openai_compatible_test.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/credential/_openai_compatible.py src/agentscope/credential/_factory.py src/agentscope/credential/__init__.py tests/credential_openai_compatible_test.py
git commit -m "feat(credential): add OpenAI-compatible custom credential type"
```

---

## Task 4: `GET`/`PUT /credential/{id}/models` endpoints + schemas

**Files:**
- Modify: `src/agentscope/app/_router/_schema/_credential.py`, `src/agentscope/app/_router/_schema/__init__.py`, `src/agentscope/app/_router/_credential.py`
- Test: `tests/credential_models_router_test.py`

**Interfaces:**
- Consumes: `CredentialFactory.from_dict`, `credential.list_effective_models()` (Task 2), `ResourceAccessService.resolve_credential/resolve_for_edit`, `StorageBase.get_credential/upsert_credential`, `ListModelsResponse` (existing).
- Produces:
  - `ModelCardConfig` (Pydantic) — one editable model config; `context_size`/`output_size` are `> 0`.
  - `UpdateCredentialModelsRequest { models: list[ModelCardConfig] | None }`.
  - `GET /credential/{credential_id}/models -> ListModelsResponse`.
  - `PUT /credential/{credential_id}/models -> ListModelsResponse`.

- [ ] **Step 1: Write the failing tests**

Create `tests/credential_models_router_test.py`:

```python
# -*- coding: utf-8 -*-
"""Router tests for per-credential model GET/PUT (FastAPI TestClient)."""
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from agentscope.app._router import credential_router
from agentscope.app.storage import CredentialRecord
from agentscope.model import ModelCard, OpenAIChatModel

HEADERS = {"X-User-ID": "u1"}


class _FakeStorage:
    """In-memory credential storage keyed by (user_id, id)."""

    def __init__(self) -> None:
        self._records: dict[tuple, CredentialRecord] = {}

    async def upsert_credential(self, user_id, credential) -> str:
        self._records[(user_id, credential.id)] = CredentialRecord(
            id=credential.id,
            user_id=user_id,
            data=credential.model_dump(mode="json"),
        )
        return credential.id

    async def get_credential(self, user_id, credential_id):
        return self._records.get((user_id, credential_id))


class _FakeAccess:
    """Owner-only access service stand-in."""

    def __init__(self, storage: _FakeStorage) -> None:
        self._storage = storage

    async def resolve_credential(self, viewer_id, credential_id):
        rec = await self._storage.get_credential(viewer_id, credential_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="not found")
        return rec

    async def resolve_for_edit(self, viewer_id, kind, credential_id):
        rec = await self._storage.get_credential(viewer_id, credential_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="not found")
        return viewer_id, rec


def make_client() -> TestClient:
    app = FastAPI()
    storage = _FakeStorage()
    app.state.storage = storage
    app.state.resource_access_service = _FakeAccess(storage)
    app.include_router(credential_router)
    return TestClient(app)


def _create_compatible(client: TestClient) -> str:
    body = {
        "data": {
            "type": "openai_compatible_credential",
            "api_key": "sk-x",
            "base_url": "http://h/v1",
            "name": "gw",
        },
    }
    resp = client.post("/credential/", json=body, headers=HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()["credential_id"]


class CredentialModelsRouterTest(unittest.TestCase):
    """GET/PUT /credential/{id}/models."""

    def test_custom_credential_starts_empty(self) -> None:
        client = make_client()
        cid = _create_compatible(client)
        resp = client.get(f"/credential/{cid}/models", headers=HEADERS)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"models": [], "total": 0})

    def test_put_then_get_roundtrip(self) -> None:
        client = make_client()
        cid = _create_compatible(client)
        cfg = {
            "name": "deepseek-v4-flash",
            "label": "deepseek-v4-flash",
            "context_size": 1024000,
            "output_size": 1024000,
        }
        expected = ModelCard.from_config(
            cfg,
            OpenAIChatModel.Parameters,
        ).model_dump(mode="json")

        put = client.put(
            f"/credential/{cid}/models",
            json={"models": [cfg]},
            headers=HEADERS,
        )
        self.assertEqual(put.status_code, 200)
        self.assertEqual(put.json(), {"models": [expected], "total": 1})

        got = client.get(f"/credential/{cid}/models", headers=HEADERS)
        self.assertEqual(got.json(), {"models": [expected], "total": 1})

    def test_put_null_reverts_to_catalog(self) -> None:
        client = make_client()
        cid = _create_compatible(client)
        client.put(
            f"/credential/{cid}/models",
            json={"models": [
                {"name": "x", "context_size": 8, "output_size": 4},
            ]},
            headers=HEADERS,
        )
        rev = client.put(
            f"/credential/{cid}/models",
            json={"models": None},
            headers=HEADERS,
        )
        self.assertEqual(rev.status_code, 200)
        # openai_compatible has an empty static catalog
        self.assertEqual(rev.json(), {"models": [], "total": 0})

    def test_put_invalid_model_is_422(self) -> None:
        client = make_client()
        cid = _create_compatible(client)
        bad = client.put(
            f"/credential/{cid}/models",
            json={"models": [
                {"name": "x", "context_size": 0, "output_size": 10},
            ]},
            headers=HEADERS,
        )
        self.assertEqual(bad.status_code, 422)

    def test_get_unknown_credential_is_404(self) -> None:
        client = make_client()
        resp = client.get("/credential/nope/models", headers=HEADERS)
        self.assertEqual(resp.status_code, 404)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/credential_models_router_test.py -v`
Expected: FAIL — the `/models` routes 404 / not found (endpoints not defined yet).

- [ ] **Step 3: Add the request schemas**

In `src/agentscope/app/_router/_schema/_credential.py`, add the `datetime` import at the top and the two classes at the end:

```python
from datetime import datetime
from typing import Literal
```
```python
class ModelCardConfig(BaseModel):
    """One editable model-card config (YAML-equivalent)."""

    name: str = Field(description="The model id, e.g. ``deepseek-v4-flash``.")
    label: str | None = Field(
        default=None,
        description="Display label. Defaults to ``name`` when omitted.",
    )
    status: Literal["active", "deprecated", "sunset"] = Field(
        default="active",
        description="The model status.",
    )
    deprecated_at: datetime | None = Field(
        default=None,
        description="The deprecation date, if any.",
    )
    input_types: list[str] = Field(
        default=["text/plain"],
        description="Supported input MIME types.",
    )
    output_types: list[str] = Field(
        default=["text/plain"],
        description="Supported output MIME types.",
    )
    context_size: int = Field(gt=0, description="The context window size.")
    output_size: int = Field(gt=0, description="Max output tokens.")
    parameter_overrides: dict = Field(
        default_factory=dict,
        description="Advanced per-parameter schema overrides.",
    )


class UpdateCredentialModelsRequest(BaseModel):
    """Replace a credential's model override list.

    ``None`` reverts to the built-in static catalog; an empty list means
    the credential offers no models.
    """

    models: list[ModelCardConfig] | None = Field(
        default=None,
        description="The new model override list, or ``None`` to inherit.",
    )
```

- [ ] **Step 4: Export the new schemas**

In `src/agentscope/app/_router/_schema/__init__.py`, extend the `from ._credential import (...)` block and `__all__`:

```python
from ._credential import (
    CreateCredentialRequest,
    CreateCredentialResponse,
    UpdateCredentialRequest,
    ListCredentialsResponse,
    ListCredentialSchemasResponse,
    ModelCardConfig,
    UpdateCredentialModelsRequest,
)
```
Add to `__all__` (in the Credential group):
```python
    "ModelCardConfig",
    "UpdateCredentialModelsRequest",
```

- [ ] **Step 5: Add the endpoints**

In `src/agentscope/app/_router/_credential.py`, extend the schema import block to include `ListModelsResponse` and `UpdateCredentialModelsRequest`:

```python
from ._schema import (
    CreateCredentialRequest,
    CreateCredentialResponse,
    ListCredentialsResponse,
    ListCredentialSchemasResponse,
    ListModelsResponse,
    UpdateCredentialModelsRequest,
    UpdateCredentialRequest,
)
```

Append these two endpoints to the end of the file (`ResourceKind`, `get_current_user_id`, `get_resource_access_service`, `get_storage`, `CredentialFactory`, `ResourceAccessService`, `StorageBase`, and `status`/`APIRouter` are already imported at the top of the file):

```python
@credential_router.get(
    "/{credential_id}/models",
    response_model=ListModelsResponse,
    summary="List the effective models for a credential",
)
async def list_credential_models(
    credential_id: str,
    user_id: str = Depends(get_current_user_id),
    access: ResourceAccessService = Depends(get_resource_access_service),
) -> ListModelsResponse:
    """Return the models a credential offers — its override list when set,
    otherwise the built-in static catalog.

    Args:
        credential_id (`str`): The credential to inspect.
        user_id (`str`): Injected authenticated user ID.
        access (`ResourceAccessService`): Injected access service; resolves
            own and shared-readable credentials.

    Returns:
        `ListModelsResponse`: The effective model cards.

    Raises:
        `HTTPException`: 404 if the credential is not visible to the caller.
    """
    record = await access.resolve_credential(user_id, credential_id)
    credential = CredentialFactory.from_dict(record.data)
    models = credential.list_effective_models()
    return ListModelsResponse(models=models, total=len(models))


@credential_router.put(
    "/{credential_id}/models",
    response_model=ListModelsResponse,
    summary="Replace the model override list for a credential",
)
async def update_credential_models(
    credential_id: str,
    body: UpdateCredentialModelsRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    access: ResourceAccessService = Depends(get_resource_access_service),
) -> ListModelsResponse:
    """Replace a credential's model override list and return the effective
    models. ``models=None`` reverts to the built-in catalog.

    Args:
        credential_id (`str`): The credential to update.
        body (`UpdateCredentialModelsRequest`): The new override list.
        user_id (`str`): Injected authenticated user ID.
        storage (`StorageBase`): Injected storage backend.
        access (`ResourceAccessService`): Injected access service; enforces
            the edit permission and resolves the owning user.

    Returns:
        `ListModelsResponse`: The recomputed effective model cards.

    Raises:
        `HTTPException`: 404 if not visible; 403 if visible but read-only.
    """
    owner_id, _ = await access.resolve_for_edit(
        user_id,
        ResourceKind.CREDENTIAL,
        credential_id,
    )
    record = await storage.get_credential(owner_id, credential_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Credential {credential_id!r} not found.",
        )
    credential = CredentialFactory.from_dict(record.data)
    if body.models is None:
        credential.models = None
    else:
        credential.models = [
            m.model_dump(mode="json") for m in body.models
        ]
    await storage.upsert_credential(owner_id, credential)
    models = credential.list_effective_models()
    return ListModelsResponse(models=models, total=len(models))
```

Also add `HTTPException` to the FastAPI import at the top of the file if it is not already present:

```python
from fastapi import APIRouter, Depends, HTTPException, status
```

Finally, make the existing provider-catalog endpoint honor the same static-catalog hook so a custom OpenAI-compatible type reports an empty catalog (not the built-in OpenAI list) — the "import built-in catalog" button relies on this. In `src/agentscope/app/_router/_model.py`, change line 39 from:

```python
    models = credential_cls.get_chat_model_class().list_models()
```
to:
```python
    models = credential_cls.list_models()
```

This is behavior-identical for built-in providers (the base `list_models` delegates to the chat model) and returns `[]` for `OpenAICompatibleCredential` (which overrides `list_models` in Task 3).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/credential_models_router_test.py -v`
Expected: PASS (5 passed).

- [ ] **Step 7: Run the full credential/model test set + lint**

Run:
```bash
pytest tests/credential_effective_models_test.py tests/credential_openai_compatible_test.py tests/credential_models_router_test.py tests/model_card_from_config_test.py -v
pre-commit run --files src/agentscope/app/_router/_credential.py src/agentscope/app/_router/_model.py src/agentscope/app/_router/_schema/_credential.py src/agentscope/app/_router/_schema/__init__.py src/agentscope/credential/_base.py src/agentscope/credential/_openai_compatible.py src/agentscope/credential/_factory.py src/agentscope/credential/__init__.py src/agentscope/model/_model_card.py
```
Expected: all tests PASS; pre-commit reports no failures.

- [ ] **Step 8: Commit**

```bash
git add src/agentscope/app/_router/_credential.py src/agentscope/app/_router/_model.py src/agentscope/app/_router/_schema/_credential.py src/agentscope/app/_router/_schema/__init__.py tests/credential_models_router_test.py
git commit -m "feat(app): add GET/PUT credential model override endpoints"
```

---

## Task 5: Frontend API layer — `put`, `listModels`, `updateModels`, types

**Files:**
- Modify: `examples/web_ui/frontend/src/api/client.ts`, `examples/web_ui/frontend/src/api/credential.ts`, `examples/web_ui/frontend/src/api/types.ts`

**Interfaces:**
- Consumes: `GET`/`PUT /credential/{id}/models` (Task 4); `ListModelResponse`, `ModelCard` (existing TS types).
- Produces:
  - `client.put<T>(path, body?, params?, options?)`.
  - `credentialApi.listModels(credentialId) -> Promise<ListModelResponse>`.
  - `credentialApi.updateModels(credentialId, models: ModelCardConfig[] | null) -> Promise<ListModelResponse>`.
  - `ModelCardConfig` TS interface.

- [ ] **Step 1: Add `put` to the client**

In `examples/web_ui/frontend/src/api/client.ts`, add a `put` method to the exported `client` object (mirror `patch`, place right after it):

```typescript
	put: <T>(
		path: string,
		body?: unknown,
		params?: Record<string, string>,
		options?: { silent?: boolean },
	) => request<T>(path, { method: 'PUT', body, params, silent: options?.silent }),
```

- [ ] **Step 2: Add the `ModelCardConfig` type**

In `examples/web_ui/frontend/src/api/types.ts`, add right after the `ModelCard` interface (after line 531):

```typescript
/** Editable per-credential model config (mirrors backend ModelCardConfig). */
export interface ModelCardConfig {
	name: string;
	label?: string | null;
	status?: 'active' | 'deprecated' | 'sunset';
	deprecated_at?: string | null;
	input_types?: string[];
	output_types?: string[];
	context_size: number;
	output_size: number;
	parameter_overrides?: Record<string, unknown>;
}
```

- [ ] **Step 3: Add the credential model endpoints**

In `examples/web_ui/frontend/src/api/credential.ts`, extend the imports and the `credentialApi` object:

```typescript
import type {
	CreateCredentialRequest,
	CreateCredentialResponse,
	CredentialListResponse,
	CredentialView,
	CredentialSchemasResponse,
	ListModelResponse,
	ModelCardConfig,
	UpdateCredentialRequest,
} from './types';
```

Add these two methods to the `credentialApi` object (after `delete`):

```typescript
	listModels: (credentialId: string) =>
		client.get<ListModelResponse>(`/credential/${credentialId}/models`),

	updateModels: (credentialId: string, models: ModelCardConfig[] | null) =>
		client.put<ListModelResponse>(`/credential/${credentialId}/models`, { models }),
```

- [ ] **Step 4: Typecheck**

Run: `cd examples/web_ui/frontend && pnpm build`
Expected: `tsc -b` completes with no type errors (build succeeds).

- [ ] **Step 5: Commit**

```bash
git add examples/web_ui/frontend/src/api/client.ts examples/web_ui/frontend/src/api/credential.ts examples/web_ui/frontend/src/api/types.ts
git commit -m "feat(webui): add credential model list/update API bindings"
```

---

## Task 6: Point `useAvailableModels` at the per-credential endpoint

**Files:**
- Modify: `examples/web_ui/frontend/src/hooks/useAvailableModels.ts`

**Interfaces:**
- Consumes: `credentialApi.listModels` (Task 5).
- Produces: unchanged public shape (`{ groups, loading, error, refetch }`) — only the per-credential model source changes, so `LlmSelect` reflects overrides and custom endpoints with no change to `LlmSelect` itself.

- [ ] **Step 1: Swap the model fetch to per-credential**

In `examples/web_ui/frontend/src/hooks/useAvailableModels.ts`, change the import (drop `modelApi`, keep `credentialApi`):

```typescript
import { credentialApi } from '@/api';
```

and replace the inner `modelApi.list(type)` call with the per-credential call (lines 33-38):

```typescript
						try {
							const { models } = await credentialApi.listModels(credential.id);
							result[type].push({ credential, models });
						} catch {
							result[type].push({ credential, models: [] });
						}
```

- [ ] **Step 2: Typecheck**

Run: `cd examples/web_ui/frontend && pnpm build`
Expected: build succeeds, no type errors, no unused-import lint error for `modelApi`.

- [ ] **Step 3: Commit**

```bash
git add examples/web_ui/frontend/src/hooks/useAvailableModels.ts
git commit -m "feat(webui): source chat model picker from per-credential models"
```

---

## Task 7: `ManageModelsDialog` component

**Files:**
- Create: `examples/web_ui/frontend/src/components/dialog/ManageModelsDialog.tsx`

**Interfaces:**
- Consumes: `credentialApi.listModels/updateModels`, `modelApi.list` (import-catalog action), `ModelCard`, `ModelCardConfig`; UI primitives `Dialog*`, `Button`, `Input`, `Textarea`, `Label`, `Separator`; `useTranslation`.
- Produces: `ManageModelsDialog` React component with props `{ open, onOpenChange, credential, providerType, onSaved }`. Saving calls `credentialApi.updateModels` and invokes `onSaved()`.

- [ ] **Step 1: Create the component**

Create `examples/web_ui/frontend/src/components/dialog/ManageModelsDialog.tsx`:

```tsx
import { Loader2, Plus, Trash2, Save, DownloadCloud } from 'lucide-react';
import { useEffect, useState } from 'react';

import { credentialApi, modelApi } from '@/api';
import type { CredentialView, ModelCard, ModelCardConfig } from '@/api';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Separator } from '@/components/ui/separator';
import { useTranslation } from '@/i18n/useI18n';

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	credential: CredentialView;
	providerType: string;
	onSaved?: () => void;
}

/** Convert a fetched ModelCard into the editable config shape. */
function cardToConfig(card: ModelCard): ModelCardConfig {
	return {
		name: card.name,
		label: card.label,
		status: card.status,
		deprecated_at: card.deprecated_at,
		input_types: card.input_types,
		output_types: card.output_types,
		context_size: card.context_size,
		output_size: card.output_size,
		parameter_overrides: card.parameters_overrides,
	};
}

function emptyConfig(): ModelCardConfig {
	return {
		name: '',
		label: '',
		status: 'active',
		input_types: ['text/plain'],
		output_types: ['text/plain'],
		context_size: 128000,
		output_size: 4096,
		parameter_overrides: {},
	};
}

export function ManageModelsDialog({
	open,
	onOpenChange,
	credential,
	providerType,
	onSaved,
}: Props) {
	const { t } = useTranslation();
	const [rows, setRows] = useState<ModelCardConfig[]>([]);
	const [loading, setLoading] = useState(false);
	const [saving, setSaving] = useState(false);

	useEffect(() => {
		if (!open) return;
		setLoading(true);
		credentialApi
			.listModels(credential.id)
			.then((res) => setRows(res.models.map(cardToConfig)))
			.catch(() => setRows([]))
			.finally(() => setLoading(false));
	}, [open, credential.id]);

	const update = (i: number, patch: Partial<ModelCardConfig>) =>
		setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));

	const remove = (i: number) => setRows((prev) => prev.filter((_, idx) => idx !== i));

	const add = () => setRows((prev) => [...prev, emptyConfig()]);

	const importCatalog = async () => {
		const { models } = await modelApi.list(providerType);
		setRows(models.map(cardToConfig));
	};

	const parseList = (raw: string): string[] =>
		raw
			.split(',')
			.map((s) => s.trim())
			.filter(Boolean);

	const handleSave = async () => {
		setSaving(true);
		try {
			const payload: ModelCardConfig[] = rows.map((r) => ({
				...r,
				label: r.label || r.name,
			}));
			await credentialApi.updateModels(credential.id, payload);
			onOpenChange(false);
			onSaved?.();
		} finally {
			setSaving(false);
		}
	};

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent className="!w-[720px] !max-w-[720px] max-h-[80vh] overflow-y-auto">
				<DialogHeader>
					<DialogTitle>{t('manage-models.title')}</DialogTitle>
					<DialogDescription>{t('manage-models.description')}</DialogDescription>
				</DialogHeader>

				{loading ? (
					<div className="flex justify-center py-8">
						<Loader2 className="size-5 animate-spin" />
					</div>
				) : (
					<div className="flex flex-col gap-y-4">
						{rows.map((row, i) => (
							<div key={i} className="flex flex-col gap-y-2 rounded-lg border p-3">
								<div className="flex items-center justify-between">
									<span className="text-xs font-semibold text-muted-foreground">
										{t('manage-models.model')} #{i + 1}
									</span>
									<Button
										size="icon-sm"
										variant="destructive"
										onClick={() => remove(i)}
									>
										<Trash2 />
									</Button>
								</div>
								<div className="grid grid-cols-2 gap-3">
									<div className="flex flex-col gap-y-1">
										<Label>{t('manage-models.name')}</Label>
										<Input
											value={row.name}
											onChange={(e) => update(i, { name: e.target.value })}
											placeholder="deepseek-v4-flash"
										/>
									</div>
									<div className="flex flex-col gap-y-1">
										<Label>{t('manage-models.label')}</Label>
										<Input
											value={row.label ?? ''}
											onChange={(e) => update(i, { label: e.target.value })}
										/>
									</div>
									<div className="flex flex-col gap-y-1">
										<Label>{t('manage-models.contextSize')}</Label>
										<Input
											type="number"
											value={row.context_size}
											onChange={(e) =>
												update(i, { context_size: Number(e.target.value) })
											}
										/>
									</div>
									<div className="flex flex-col gap-y-1">
										<Label>{t('manage-models.outputSize')}</Label>
										<Input
											type="number"
											value={row.output_size}
											onChange={(e) =>
												update(i, { output_size: Number(e.target.value) })
											}
										/>
									</div>
									<div className="flex flex-col gap-y-1">
										<Label>{t('manage-models.inputTypes')}</Label>
										<Input
											value={(row.input_types ?? []).join(', ')}
											onChange={(e) =>
												update(i, { input_types: parseList(e.target.value) })
											}
										/>
									</div>
									<div className="flex flex-col gap-y-1">
										<Label>{t('manage-models.outputTypes')}</Label>
										<Input
											value={(row.output_types ?? []).join(', ')}
											onChange={(e) =>
												update(i, { output_types: parseList(e.target.value) })
											}
										/>
									</div>
								</div>
							</div>
						))}
						<div className="flex gap-x-2">
							<Button variant="outline" size="sm" onClick={add}>
								<Plus className="size-3.5" />
								{t('manage-models.add')}
							</Button>
							<Button variant="outline" size="sm" onClick={importCatalog}>
								<DownloadCloud className="size-3.5" />
								{t('manage-models.import')}
							</Button>
						</div>
					</div>
				)}

				<Separator />
				<DialogFooter>
					<Button variant="ghost" onClick={() => onOpenChange(false)} disabled={saving}>
						{t('common.cancel')}
					</Button>
					<Button onClick={handleSave} disabled={saving}>
						{saving ? (
							<Loader2 className="size-3.5 animate-spin" />
						) : (
							<Save className="size-3.5" />
						)}
						{t('common.save')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
```

- [ ] **Step 2: Typecheck**

Run: `cd examples/web_ui/frontend && pnpm build`
Expected: build succeeds. If `Input`/`Label`/`Separator` import paths differ, fix per the actual files in `src/components/ui/`. (Note: this task's `t(...)` keys are added in Task 9; typecheck does not depend on them.)

- [ ] **Step 3: Commit**

```bash
git add examples/web_ui/frontend/src/components/dialog/ManageModelsDialog.tsx
git commit -m "feat(webui): add ManageModelsDialog for editing available models"
```

---

## Task 8: Wire per-credential fetch + Edit button into the credential page

**Files:**
- Modify: `examples/web_ui/frontend/src/pages/credential/index.tsx`

**Interfaces:**
- Consumes: `credentialApi.listModels` (Task 5), `ManageModelsDialog` (Task 7).
- Produces: the DetailPanel's "Available Models" fetches per credential; an Edit button (visible when `credential.editable`) opens `ManageModelsDialog` and refetches on save.

- [ ] **Step 1: Switch the DetailPanel model fetch to per-credential + add the editor**

In `examples/web_ui/frontend/src/pages/credential/index.tsx`:

(a) Add imports at the top:
```tsx
import { ManageModelsDialog } from '@/components/dialog/ManageModelsDialog';
```
(`credentialApi` is already imported; `Pencil` is already imported from `lucide-react`.)

(b) In `DetailPanel`, add manage-dialog state and a reusable model loader, and replace the `modelApi.list(type)` call. Replace the existing `useEffect` block (lines 169-187) and add state (near lines 163-165):

```tsx
	const [manageOpen, setManageOpen] = useState(false);

	const loadModels = useCallback(() => {
		if (!type) return;
		setModelsLoading(true);
		Promise.all([
			credentialApi
				.listModels(credential.id)
				.then((res) => res.models)
				.catch(() => [] as ModelCard[]),
			ttsModelApi
				.list(type)
				.then((res) => res.models)
				.catch(() => [] as TTSModelCard[]),
		])
			.then(([chatModels, tts]) => {
				setModels(chatModels);
				setTtsModels(tts);
			})
			.finally(() => setModelsLoading(false));
	}, [credential.id, type]);

	useEffect(() => {
		loadModels();
	}, [loadModels]);
```

Add `useCallback` to the React import at the top of `DetailPanel`'s module if not already imported (the page already imports `useCallback` at file scope — reuse it). Remove the now-unused `modelApi` import from the file's top import (`import { credentialApi, ttsModelApi } from '@/api';`).

(c) Add an Edit button on the "Available Models" header. Replace the header `<h3>` (lines 273-275) with:

```tsx
				<div className="flex items-center justify-between">
					<h3 className="text-sm font-semibold">
						{t('credential.availableModels')}({models.length})
					</h3>
					{credential.editable && (
						<Button size="sm" variant="outline" onClick={() => setManageOpen(true)}>
							<Pencil className="size-3.5" />
							{t('manage-models.edit')}
						</Button>
					)}
				</div>
```

(d) Render the dialog at the end of the DetailPanel's returned JSX, just before the closing `</div>` of the root panel container:

```tsx
			<ManageModelsDialog
				open={manageOpen}
				onOpenChange={setManageOpen}
				credential={credential}
				providerType={type ?? ''}
				onSaved={loadModels}
			/>
```

- [ ] **Step 2: Typecheck + lint**

Run: `cd examples/web_ui/frontend && pnpm build && pnpm lint`
Expected: build + lint succeed, with no unused-import warnings (`modelApi` removed).

- [ ] **Step 3: Commit**

```bash
git add examples/web_ui/frontend/src/pages/credential/index.tsx
git commit -m "feat(webui): edit available models per credential on the credential page"
```

---

## Task 9: i18n keys (en + zh)

**Files:**
- Modify: `examples/web_ui/frontend/src/i18n/locales/*` (locate the en + zh locale files)

**Interfaces:**
- Consumes: nothing.
- Produces: the `manage-models.*` keys used in Tasks 7 & 8, plus `common.save` if missing.

- [ ] **Step 1: Find the locale files and the existing key style**

Run: `ls examples/web_ui/frontend/src/i18n/locales && grep -rn "availableModels\|\"save\"\|common" examples/web_ui/frontend/src/i18n/locales | head`
Expected: identifies the en + zh files and whether `common.save` already exists. Follow the existing nesting/format exactly (flat vs nested).

- [ ] **Step 2: Add the English keys**

In the English locale, add a `manage-models` group (match the file's structure — nested object shown here):

```json
"manage-models": {
	"title": "Manage Available Models",
	"description": "Add, edit, or remove the models this credential offers. Leave empty to fall back to the built-in catalog.",
	"edit": "Edit",
	"add": "Add model",
	"import": "Import built-in catalog",
	"model": "Model",
	"name": "Model ID",
	"label": "Display label",
	"contextSize": "Max context",
	"outputSize": "Max output",
	"inputTypes": "Input types (comma-separated)",
	"outputTypes": "Output types (comma-separated)"
}
```
Add `"save": "Save"` under the `common` group if it is not already present.

- [ ] **Step 3: Add the Chinese keys**

In the Chinese locale, add the mirrored group:

```json
"manage-models": {
	"title": "管理可用模型",
	"description": "为该凭证添加、编辑或删除模型。留空则回退到内置目录。",
	"edit": "编辑",
	"add": "添加模型",
	"import": "导入内置目录",
	"model": "模型",
	"name": "模型 ID",
	"label": "显示名称",
	"contextSize": "最大上下文",
	"outputSize": "最大输出",
	"inputTypes": "输入类型（逗号分隔）",
	"outputTypes": "输出类型（逗号分隔）"
}
```
Add `"save": "保存"` under the `common` group if it is not already present.

- [ ] **Step 4: Typecheck**

Run: `cd examples/web_ui/frontend && pnpm build`
Expected: build succeeds; the ManageModelsDialog and Edit button render translated strings (verified visually in Task 10).

- [ ] **Step 5: Commit**

```bash
git add examples/web_ui/frontend/src/i18n/locales
git commit -m "feat(webui): i18n for manage-models dialog (en + zh)"
```

---

## Task 10: End-to-end verification (manual smoke)

**Files:** none (verification only).

**Interfaces:**
- Consumes: the full stack from Tasks 1-9.

- [ ] **Step 1: Boot the stack**

Follow the recorded run setup (`ravenx-webui-run-setup`): Redis up, backend service on its port, `pnpm dev` for the frontend (`:5173`). Set username + server URL in the UI.

- [ ] **Step 2: Add a custom OpenAI-compatible credential**

On `/credential`, "Add provider" → **OpenAI-Compatible API**. Fill `base_url` (e.g. `http://8.141.31.123:3000/v1`) + `api_key` + a name. Create.
Expected: the credential appears under "Configured"; "Available Models" shows the empty state (no bundled catalog).

- [ ] **Step 3: Add models via the editor**

Click **Edit** on "Available Models" → **Add model** → enter `deepseek-v4-flash` (context/output sizes) → optionally **Add model** for `glm-5.2`, `hy3` → **Save**.
Expected: the models render as cards in "Available Models".

- [ ] **Step 4: Confirm the models are selectable in chat**

Open a chat, open the model picker (`LlmSelect`).
Expected: the custom credential's group lists `deepseek-v4-flash` (and any others added). Select it and send one message.
Expected: the turn completes against the endpoint (a real response or a clear provider error — not a "model not found in catalog" failure).

- [ ] **Step 5: Confirm built-in provider editing + revert**

For an existing OpenAI/DashScope credential, open **Edit** → **Import built-in catalog** → add one new model → **Save** → confirm it appears. Then **Edit** → remove all rows → **Save** with an empty list, reopen: models list is empty. (A future "reset to catalog" affordance can send `null`; the empty-save path confirms override persistence.)

- [ ] **Step 6: Final backend regression sweep**

Run:
```bash
pytest tests/model_card_from_config_test.py tests/credential_effective_models_test.py tests/credential_openai_compatible_test.py tests/credential_models_router_test.py -v
pre-commit run --files $(git diff --name-only main -- 'src/**/*.py' 'tests/**/*.py')
```
Expected: all PASS; pre-commit clean.

---

## Self-Review Notes

- **Spec §4.1 (from_config)** → Task 1. **§4.2 (models field, _static_model_cards, list_effective_models)** → Task 2. **§4.3 (OpenAICompatibleCredential + registration)** → Task 3. **§5 (schemas + endpoints)** → Task 4. **§6.1 (API+types)** → Task 5. **§6.2 (useAvailableModels)** → Task 6. **§6.3 (ManageModelsDialog)** → Task 7. **§6.4 (schema-driven provider form)** → verified in Task 10 Step 2 (no code needed). **§7 (tests)** → Tasks 1-4. **§8 (e2e)** → Task 10. **§6.5 (i18n)** → Task 9.
- **Type consistency:** `ModelCardConfig` fields match across backend schema (Task 4), TS type (Task 5), and the dialog (Task 7); `list_effective_models` / `list_models` (overridden) / `from_config` names are used identically in Tasks 2-4; the `GET /model/` handler and `list_effective_models` share the `list_models` static-catalog hook; `credentialApi.listModels/updateModels` names match across Tasks 5-8.
- **Override semantics** (`None`/`[]`/`[...]`) are consistent between Task 2 (build), Task 4 (endpoint), and Task 10 (verify).
