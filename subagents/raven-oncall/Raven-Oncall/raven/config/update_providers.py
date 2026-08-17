"""Atomic operations for LLM provider config sections.

This module is the ONLY write path for provider configuration. All entry
points (CLI commands, future wizard, future REPL slash) must call
functions defined here. Direct ``load_config`` / ``save_config`` on the
providers section is forbidden -- see plan rule.

OAuth providers (``openai_codex`` / ``github_copilot``) have a separate
auth path via ``provider_commands._LOGIN_HANDLERS`` and store tokens via
``oauth_cli_kit``, not in ``config.json``. ``set_provider_fields`` refuses
to write ``api_key`` for those providers; callers must invoke
``provider login`` for that. ``reset_provider`` handles both cases:
schema-default rewrite for config fields, plus unlinking the
``oauth_cli_kit`` token file when the provider has ``is_oauth=True``.
"""

from __future__ import annotations

import json
import os
import typing
from pathlib import Path
from typing import Any, Union

import httpx
from loguru import logger
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

from raven.config.loader import get_config_path, read_raw_or_raise
from raven.config.schema import ProvidersConfig
from raven.providers.registry import ProviderSpec, find_by_name

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    """Atomic write: temp-file then os.replace. Preserves indent=2, UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _unwrap_optional(annotation: Any) -> Any:
    """Strip ``Optional[X]`` / ``X | None`` down to ``X``."""
    import types as _types

    origin = typing.get_origin(annotation)
    if origin is Union or origin is getattr(_types, "UnionType", None):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _is_model_class(ann: Any) -> bool:
    return isinstance(ann, type) and issubclass(ann, BaseModel)


def _provider_names() -> list[str]:
    """Return provider field names declared on ``ProvidersConfig``."""
    out: list[str] = []
    for fname, finfo in ProvidersConfig.model_fields.items():
        ann = _unwrap_optional(finfo.annotation)
        if _is_model_class(ann):
            out.append(fname)
    return out


def _provider_schema_cls(name: str) -> type[BaseModel]:
    """Look up the Pydantic class for a provider, e.g. ``'gemini' -> GeminiProviderConfig``."""
    field = ProvidersConfig.model_fields.get(name)
    if field is None:
        raise KeyError(f"Unknown provider '{name}'. Available providers: {sorted(_provider_names())}")
    ann = _unwrap_optional(field.annotation)
    if not _is_model_class(ann):
        raise KeyError(f"'{name}' is not a provider section. Available providers: {sorted(_provider_names())}")
    return ann


def _provider_spec(name: str) -> ProviderSpec:
    """Look up ``ProviderSpec`` from the registry (raises if absent)."""
    spec = find_by_name(name)
    if spec is None:
        raise KeyError(f"No registry entry for provider '{name}'. Add a ProviderSpec to raven/providers/registry.py.")
    return spec


def _annotation_str(ann: Any) -> str:
    """Compact type string for the ``show`` command."""
    ann = _unwrap_optional(ann)
    origin = typing.get_origin(ann)
    if origin is typing.Literal:
        return "Literal"
    if origin is list:
        args = typing.get_args(ann)
        return f"list[{_annotation_str(args[0])}]" if args else "list"
    if origin is dict:
        args = typing.get_args(ann)
        if args and len(args) == 2:
            return f"dict[{_annotation_str(args[0])}, {_annotation_str(args[1])}]"
        return "dict"
    if hasattr(ann, "__name__"):
        return ann.__name__
    return str(ann)


_SECRET_EXACT = {"token", "secret", "password", "api_key"}
_SECRET_SUFFIXES = ("_token", "_secret", "_key", "_password")

# Names that should be redacted but neither match _SECRET_EXACT nor end in a
# secret suffix. Today this only covers Gemini's ``api_key_list`` (suffix is
# ``_list``, not ``_key``). Delete entries here as schema.py grows the
# ``json_schema_extra={"secret": True}`` marker on the underlying fields.
_KNOWN_SECRET_FIELDS: set[str] = {"api_key_list"}


def _is_secret_field(field_name: str, field_info: Any) -> bool:
    """Detect secret fields, in priority order:

    1. Explicit: ``field_info.json_schema_extra.get('secret') is True``
    2. Patch set: ``_KNOWN_SECRET_FIELDS`` (workaround for fields the
       suffix heuristic misses, e.g. Gemini's ``api_key_list``).
    3. Exact match (``token`` / ``secret`` / ``password`` / ``api_key``).
    4. Suffix match (``_token`` / ``_secret`` / ``_key`` / ``_password``).
    """
    extra = getattr(field_info, "json_schema_extra", None)
    if isinstance(extra, dict) and extra.get("secret") is True:
        return True
    if field_name in _KNOWN_SECRET_FIELDS:
        return True
    if field_name in _SECRET_EXACT:
        return True
    return any(field_name.endswith(suf) for suf in _SECRET_SUFFIXES)


def _coerce_value(value: Any, annotation: Any) -> Any:
    """Pre-Pydantic coercion for CLI string inputs.

    Identical behavior to ``update_channels._coerce_value`` — handles bool /
    int / float / list / dict surfaces so the same ``--flag value`` UX works
    for both groups.
    """
    if not isinstance(value, str):
        return value

    base = _unwrap_optional(annotation)

    if base is bool:
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
        return value

    if base is int:
        try:
            return int(value)
        except ValueError:
            return value

    if base is float:
        try:
            return float(value)
        except ValueError:
            return value

    origin = typing.get_origin(base)
    if origin is list:
        v = value.strip()
        if v.startswith("[") and v.endswith("]"):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in value.split(",") if item.strip()]

    if origin is dict:
        v = value.strip()
        if v.startswith("{") and v.endswith("}"):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                pass
        return value

    return value


def _field_default(field_info: Any) -> Any:
    """Resolve a Pydantic FieldInfo's effective default (call factory if any)."""
    if field_info.default_factory is not None:
        try:
            return field_info.default_factory()
        except Exception:
            return None
    if field_info.default is PydanticUndefined:
        return None
    return field_info.default


def _flatten_fields(cls: type[BaseModel], prefix: str = "") -> dict[str, dict[str, Any]]:
    """Flatten provider schema fields to ``path -> spec`` (no nesting today, but
    kept consistent with ``update_channels`` so the same CLI parser works)."""
    out: dict[str, dict[str, Any]] = {}
    for fname, finfo in cls.model_fields.items():
        ann = _unwrap_optional(finfo.annotation)
        path = f"{prefix}{fname}"
        if _is_model_class(ann):
            out.update(_flatten_fields(ann, prefix=f"{path}."))
            continue
        description = finfo.description or ""
        origin = typing.get_origin(ann)
        if origin is typing.Literal and not description:
            choices = ", ".join(str(a) for a in typing.get_args(ann))
            description = f"Choices: {choices}"
        out[path] = {
            "type": _annotation_str(ann),
            "default": _field_default(finfo),
            "is_secret": _is_secret_field(fname, finfo),
            "description": description,
        }
    return out


def _flatten_instance(instance: BaseModel, prefix: str = "") -> dict[str, Any]:
    """Flatten a Pydantic instance to ``path -> value``."""
    out: dict[str, Any] = {}
    for fname in type(instance).model_fields:
        val = getattr(instance, fname)
        path = f"{prefix}{fname}"
        if isinstance(val, BaseModel):
            out.update(_flatten_instance(val, prefix=f"{path}."))
        else:
            out[path] = val
    return out


def _walk_nested_path(model_cls: type[BaseModel], dotted_key: str) -> tuple[type[BaseModel], str]:
    """Walk ``a.b.c`` through nested ``BaseModel`` classes."""
    segs = dotted_key.split(".")
    cls: type[BaseModel] = model_cls
    for seg in segs[:-1]:
        finfo = cls.model_fields.get(seg)
        if finfo is None:
            raise KeyError(f"Unknown nested field '{seg}' in {cls.__name__}")
        ann = _unwrap_optional(finfo.annotation)
        if not _is_model_class(ann):
            raise KeyError(f"Field '{seg}' in {cls.__name__} is not a nested model")
        cls = ann
    leaf = segs[-1]
    if leaf not in cls.model_fields:
        raise KeyError(f"Unknown field '{leaf}' in {cls.__name__}")
    return cls, leaf


def _set_nested(dotted_key: str, value: Any, target: dict[str, Any]) -> Any:
    """Set ``target[a][b][...][leaf] = value``; return previous value."""
    segs = dotted_key.split(".")
    cursor = target
    for seg in segs[:-1]:
        nxt = cursor.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[seg] = nxt
        cursor = nxt
    prev = cursor.get(segs[-1])
    cursor[segs[-1]] = value
    return prev


def _redact(value: Any) -> Any:
    """Redact a single value or list of values."""
    if value in (None, "", [], {}):
        return "(empty)"
    if isinstance(value, list):
        return ["****set****" for _ in value]
    return "****set****"


def _oauth_token_path(provider_name: str) -> Path:
    """Resolve the on-disk token file path written by ``oauth_cli_kit``.

    Honors the ``OAUTH_CLI_KIT_TOKEN_PATH`` override the kit itself respects,
    so tests can point at ``tmp_path`` without touching real user data.
    """
    override = os.environ.get("OAUTH_CLI_KIT_TOKEN_PATH")
    if override:
        return Path(override)
    try:
        from platformdirs import user_data_dir
    except ImportError:
        return Path.home() / ".local" / "share" / "oauth-cli-kit" / "auth" / f"{provider_name}.json"
    base_dir = Path(user_data_dir("oauth-cli-kit", appauthor=False))
    return base_dir / "auth" / f"{provider_name}.json"


# ---------------------------------------------------------------------------
# Public API: reflection
# ---------------------------------------------------------------------------


def provider_field_specs(name: str) -> dict[str, dict[str, Any]]:
    """Reflect a provider schema into a flat ``path -> spec`` map.

    Each entry has keys: ``type``, ``default``, ``is_secret``, ``description``.
    Used by CLI parsers, the ``provider show`` command, and ``get_provider_config``
    to know which fields exist and which to redact.
    """
    cls = _provider_schema_cls(name)
    return _flatten_fields(cls)


# ---------------------------------------------------------------------------
# Public API: read
# ---------------------------------------------------------------------------


def list_providers(*, config_path: Path | None = None) -> list[dict[str, Any]]:
    """Reflect every provider declared on ``ProvidersConfig`` + current status.

    Returns one dict per provider:

    - ``name``               registry / config field name
    - ``display_name``       human-readable label from the registry
    - ``is_oauth`` / ``is_local`` / ``is_gateway``  registry flags
    - ``configured``         True iff key set (or token file present for OAuth,
                             or api_base set for local)
    - ``api_key_redacted``   ``****set****`` / ``(empty)`` / ``(not needed for local)``
    - ``api_base``           current value (or ``None`` if untouched)
    """
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    raw_providers = data.get("providers") or {}

    out: list[dict[str, Any]] = []
    for fname in _provider_names():
        cls = _provider_schema_cls(fname)
        section = raw_providers.get(fname) or {}
        try:
            instance = cls.model_validate(section)
        except ValidationError:
            instance = cls()

        spec = find_by_name(fname)
        is_oauth = bool(spec and spec.is_oauth)
        is_local = bool(spec and spec.is_local)
        is_gateway = bool(spec and spec.is_gateway)
        display_name = spec.label if spec else fname.replace("_", " ").title()

        api_key = getattr(instance, "api_key", "") or ""
        api_base = getattr(instance, "api_base", None)
        api_key_list = list(getattr(instance, "api_key_list", []) or [])

        if is_oauth:
            configured = _oauth_token_path(fname).exists()
            api_key_redacted = "OAuth token" if configured else "(empty)"
        elif is_local:
            configured = bool(api_base) or bool(api_key)
            api_key_redacted = "(not needed for local)" if not api_key else "****set****"
        else:
            configured = bool(api_key) or bool(api_key_list)
            api_key_redacted = "****set****" if configured else "(empty)"

        out.append(
            {
                "name": fname,
                "display_name": display_name,
                "is_oauth": is_oauth,
                "is_local": is_local,
                "is_gateway": is_gateway,
                "configured": configured,
                "api_key_redacted": api_key_redacted,
                "api_base": api_base,
            }
        )
    return out


def get_provider_config(
    name: str,
    *,
    redact_secrets: bool = True,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Return one provider's configuration as a flat ``path -> value`` dict.

    Secret fields render as ``'****set****'`` / ``'(empty)'`` by default. Pass
    ``redact_secrets=False`` to get plaintext (used by ``test_provider`` to
    actually call the provider's ``/v1/models`` endpoint).
    """
    cls = _provider_schema_cls(name)
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    raw_section = (data.get("providers") or {}).get(name) or {}

    try:
        instance = cls.model_validate(raw_section)
    except ValidationError:
        instance = cls()

    specs = provider_field_specs(name)
    flat = _flatten_instance(instance)
    out: dict[str, Any] = {}
    for path_key, spec in specs.items():
        val = flat.get(path_key)
        if redact_secrets and spec["is_secret"]:
            out[path_key] = _redact(val)
        else:
            out[path_key] = val
    return out


# ---------------------------------------------------------------------------
# Public API: write
# ---------------------------------------------------------------------------


def set_provider_fields(
    name: str,
    fields: dict[str, Any],
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Patch specific fields on a provider. Returns ``{path: previous_value}``.

    Raises:
        KeyError: unknown provider name or unknown field path.
        RuntimeError: attempting to set ``api_key`` / ``api_key_list`` on an
            OAuth provider — callers should use ``provider login`` instead.
        ValidationError: a field value violates the provider's Pydantic schema.
    """
    if not fields:
        return {}

    cls = _provider_schema_cls(name)
    spec = _provider_spec(name)
    field_specs = provider_field_specs(name)

    unknown = [k for k in fields if k not in field_specs]
    if unknown:
        raise KeyError(
            f"Unknown field(s) {unknown} for provider '{name}'. Available fields: {sorted(field_specs.keys())}"
        )

    if spec.is_oauth:
        forbidden = [k for k in fields if field_specs[k]["is_secret"]]
        if forbidden:
            raise RuntimeError(
                f"Provider '{name}' uses OAuth — cannot set credential fields "
                f"{forbidden} directly. Run: raven provider login "
                f"{name.replace('_', '-')}"
            )

    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    raw_section = (data.get("providers") or {}).get(name) or {}

    try:
        current = cls.model_validate(raw_section)
    except ValidationError:
        current = cls()

    working = current.model_dump()

    prev: dict[str, Any] = {}
    for path_key, raw_val in fields.items():
        leaf_cls, leaf_field = _walk_nested_path(cls, path_key)
        leaf_info = leaf_cls.model_fields[leaf_field]
        coerced = _coerce_value(raw_val, leaf_info.annotation)
        prev[path_key] = _set_nested(path_key, coerced, working)

    validated = cls.model_validate(working)

    data.setdefault("providers", {})
    data["providers"][name] = validated.model_dump(by_alias=True)
    _write_atomic(path, data)
    return prev


def reset_provider(
    name: str,
    *,
    config_path: Path | None = None,
) -> None:
    """Restore a provider to schema defaults. Key preserved; values reset.

    Two cleanup paths run automatically, dispatched on ``ProviderSpec.is_oauth``:

    1. **Config fields** — always rewritten to whatever a fresh Pydantic
       instance produces (``api_key=""``, ``api_base=None``, ``vertex=False``
       for Gemini, ``api_key_list=[]`` etc.). For OAuth providers those are
       already at defaults, so the write is a no-op for them but harmless.

    2. **OAuth token file** (``is_oauth=True``) — unlinked from disk so the
       user is effectively logged out. Path resolution follows
       ``oauth_cli_kit``'s own convention (honoring the
       ``OAUTH_CLI_KIT_TOKEN_PATH`` env override). Idempotent: ``missing_ok``
       so reset can run multiple times without raising.

    Callers don't need to know which case applies — one mental model covers
    both API-key and OAuth providers.
    """
    cls = _provider_schema_cls(name)
    spec = _provider_spec(name)

    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    data.setdefault("providers", {})
    data["providers"][name] = cls().model_dump(by_alias=True)
    _write_atomic(path, data)

    if spec.is_oauth:
        token_path = _oauth_token_path(name)
        try:
            token_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "update_providers: failed to unlink OAuth token {}: {}",
                token_path,
                exc,
            )

    logger.info("update_providers: {} reset to defaults", name)


def _load_provider_models(name: str, data: dict[str, Any]) -> tuple[type, list[str]]:
    cls = _provider_schema_cls(name)
    section = (data.get("providers") or {}).get(name) or {}
    try:
        instance = cls.model_validate(section)
    except ValidationError:
        instance = cls()
    return cls, list(getattr(instance, "models", []) or [])


def add_provider_model(
    name: str,
    model: str,
    *,
    config_path: Path | None = None,
) -> list[str]:
    """Append ``model`` to a provider's curated ``models`` list (idempotent).

    Returns the new model list. Raises KeyError for an unknown provider.
    """
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    cls, models = _load_provider_models(name, data)
    if model not in models:
        models.append(model)
        section = (data.get("providers") or {}).get(name) or {}
        section["models"] = models
        validated = cls.model_validate(section)
        data.setdefault("providers", {})
        data["providers"][name] = validated.model_dump(by_alias=True)
        _write_atomic(path, data)
    return models


def remove_provider_model(
    name: str,
    model: str,
    *,
    config_path: Path | None = None,
) -> list[str]:
    """Remove ``model`` from a provider's curated ``models`` list (no-op if absent).

    Returns the new model list. Raises KeyError for an unknown provider.
    """
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    cls, models = _load_provider_models(name, data)
    if model in models:
        models = [m for m in models if m != model]
        section = (data.get("providers") or {}).get(name) or {}
        section["models"] = models
        validated = cls.model_validate(section)
        data.setdefault("providers", {})
        data["providers"][name] = validated.model_dump(by_alias=True)
        _write_atomic(path, data)
    return models


# ---------------------------------------------------------------------------
# Public API: credential health check
# ---------------------------------------------------------------------------


# Maps HTTP status → user-facing status keyword used by the CLI hint table.
_HTTP_STATUS_MAP: dict[int, str] = {
    200: "valid",
    401: "invalid_key",
    402: "no_credits",
    403: "invalid_key",
    429: "rate_limited",
}


def test_provider(
    name: str,
    *,
    timeout_s: int = 10,
    config_path: Path | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Verify a provider's credentials via a free GET request to ``/v1/models``.

    Why ``/v1/models`` rather than a chat completion (same rationale as
    hermes-agent's ``doctor._probe_apikey_provider``):

    - Zero token cost — metadata endpoint, not LLM-generated content.
    - No charge to the user, doesn't burn inference quota.
    - Supported by virtually every OpenAI-compatible provider (the 18 we
      ship today).
    - No "which test model?" maintenance burden.

    Behavior:

    1. Look up the provider's ``api_key`` (or OAuth access token via
       ``oauth_cli_kit.get_token()`` for OAuth providers) and ``api_base``
       (falling back to ``ProviderSpec.default_api_base`` when unset).
    2. ``GET {api_base}/v1/models`` with ``Authorization: Bearer {key}``.
    3. Map status code → keyword (see ``_HTTP_STATUS_MAP``). Unknown codes
       render as ``http_{code}``. Network errors → ``network_error``.

    Returns a dict, never raises. ``transport`` is injectable so unit tests
    can mount an ``httpx.MockTransport`` without touching real network.
    """
    import time

    try:
        spec = _provider_spec(name)
    except KeyError as exc:
        return {
            "ok": False,
            "status": "unknown_provider",
            "elapsed_ms": 0,
            "http_status": None,
            "models_count": None,
            "model_ids": None,
            "error": str(exc),
        }

    cfg = get_provider_config(name, redact_secrets=False, config_path=config_path)
    api_key = cfg.get("api_key") or ""
    api_base = cfg.get("api_base") or spec.default_api_base or ""

    if spec.is_oauth:
        try:
            from oauth_cli_kit import get_token
        except ImportError:
            return {
                "ok": False,
                "status": "oauth_token_missing",
                "elapsed_ms": 0,
                "http_status": None,
                "models_count": None,
                "model_ids": None,
                "error": "oauth_cli_kit not installed",
            }
        try:
            token = get_token()
        except Exception as exc:
            return {
                "ok": False,
                "status": "oauth_token_missing",
                "elapsed_ms": 0,
                "http_status": None,
                "models_count": None,
                "model_ids": None,
                "error": str(exc),
            }
        if not (token and getattr(token, "access", None)):
            return {
                "ok": False,
                "status": "oauth_token_missing",
                "elapsed_ms": 0,
                "http_status": None,
                "models_count": None,
                "model_ids": None,
                "error": "no OAuth token stored",
            }
        api_key = token.access

    if not api_key and not spec.is_local:
        return {
            "ok": False,
            "status": "not_configured",
            "elapsed_ms": 0,
            "http_status": None,
            "models_count": None,
            "model_ids": None,
            "error": "api_key is empty",
        }

    if not api_base:
        return {
            "ok": False,
            "status": "not_configured",
            "elapsed_ms": 0,
            "http_status": None,
            "models_count": None,
            "model_ids": None,
            "error": "api_base is empty and provider has no default",
        }

    url = api_base.rstrip("/") + "/models"
    if "/v1" not in api_base:
        url = api_base.rstrip("/") + "/v1/models"

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    start = time.monotonic()
    client_kwargs: dict[str, Any] = {"timeout": timeout_s}
    if transport is not None:
        client_kwargs["transport"] = transport

    try:
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status": "network_error",
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "http_status": None,
            "models_count": None,
            "model_ids": None,
            "error": str(exc),
        }

    elapsed_ms = int((time.monotonic() - start) * 1000)
    status_keyword = _HTTP_STATUS_MAP.get(resp.status_code, f"http_{resp.status_code}")

    models_count: int | None = None
    model_ids: list[str] | None = None
    if resp.status_code == 200:
        try:
            payload = resp.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, list):
                models_count = len(data)
                ids: list[str] = []
                for item in data:
                    if isinstance(item, dict):
                        mid = item.get("id") or item.get("name")
                        if isinstance(mid, str) and mid:
                            ids.append(mid)
                model_ids = ids
        except Exception:
            models_count = None
            model_ids = None

    return {
        "ok": resp.status_code == 200,
        "status": status_keyword,
        "elapsed_ms": elapsed_ms,
        "http_status": resp.status_code,
        "models_count": models_count,
        "model_ids": model_ids,
        "error": None if resp.status_code == 200 else f"HTTP {resp.status_code}",
    }


__all__ = [
    "provider_field_specs",
    "list_providers",
    "get_provider_config",
    "set_provider_fields",
    "reset_provider",
    "test_provider",
]
