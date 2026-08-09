"""Atomic operations for LLM provider config sections.

This module is the ONLY write path for provider configuration. All entry
points (CLI commands, future wizard, future REPL slash) must call
functions defined here. Direct ``load_config`` / ``save_config`` on the
providers section is forbidden -- see plan rule.

OAuth providers have a separate auth path via
``provider_commands._LOGIN_HANDLERS`` and keep their credentials in files under
``~/.raven/oauth``, not in ``config.json``. ``set_provider_fields`` refuses to
write ``api_key`` for those providers; callers must invoke ``provider login`` for
that. ``reset_provider`` handles both cases: schema-default rewrite for config
fields, plus deleting the credential files when the provider has
``is_oauth=True``.
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
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticUndefined

from raven.config.loader import get_config_path, read_raw_or_raise
from raven.config.schema import ProviderConfig, ProviderEndpoint, ProvidersConfig
from raven.providers.registry import (
    ProviderSpec,
    canonical_provider_name,
    find_by_name,
    names_same_provider,
    normalize_provider_name,
)


def _overlay(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge two spellings of one section, letting a set value beat an unset one.

    Blindly preferring the later spelling let an empty `apiKey` from a
    placeholder section erase the key the user had actually written under the
    other spelling.
    """
    merged = dict(base)
    merged.update({k: v for k, v in overlay.items() if v not in ("", None, [], {})})
    return merged


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


def _listable_provider_names(data: dict[str, Any]) -> list[str]:
    """Declared providers, plus any others the config actually holds.

    A vendor configured by name alone would otherwise be invisible to callers of
    ``list_providers`` -- ``provider list`` and the startup gate that decides
    whether the wizard has to run -- while the runtime routes to it happily.
    Reporting it as unconfigured is what sends a user who has set it up straight
    back into a wizard that declines to configure it.

    """
    declared = _provider_names()
    # Sections are stored camelCase ("azureOpenai"), so compare on the snake form
    # or every declared provider looks like an unknown extra.
    known = {n for name in declared for n in (name, to_camel(name))}
    stored = data.get("providers") or {}
    extra = [
        name
        for name, section in stored.items()
        if isinstance(section, dict)
        and name not in known
        and canonical_provider_name(name) not in known
        and normalize_provider_name(name) not in known
    ]
    return declared + sorted(extra)


def _litellm_knows(name: str, *, authoritative: bool = True) -> bool | None:
    """Whether LiteLLM speaks to this vendor, or None when it cannot be asked.

    Three states, not two. Answering False when LiteLLM is merely unavailable
    would tell the user their provider name is wrong when it may well be right.
    Raising instead was worse: only one of the five call sites caught it, so the
    rest turned an unavailable dependency into a bare traceback. None makes
    every caller confront the third case at the point it has to decide.

    A snapshot of LiteLLM's names answers the common case for free. Importing
    LiteLLM costs about two seconds, which a command that is about to call a
    model pays anyway and a command that only reports configuration should not.
    So ``authoritative=False`` stops at the snapshot -- suitable where a wrong
    answer means one row missing from a report -- while the default falls
    through to LiteLLM itself when the snapshot has no entry, so a vendor added
    in a newer LiteLLM than the snapshot can still be configured.

    Comparison is normalized on both sides: LiteLLM hyphenates a few vendors
    ("nano-gpt") while config sections and model-id prefixes are underscored, so
    an exact match rejects the very spelling this function tells users to write.
    """
    from raven.providers.litellm_provider_names import LITELLM_PROVIDER_NAMES

    normalized = normalize_provider_name(name)
    if normalized in {normalize_provider_name(n) for n in LITELLM_PROVIDER_NAMES}:
        return True
    if not authoritative:
        return False

    from raven.providers.litellm_setup import import_litellm

    try:
        litellm = import_litellm()
        providers = litellm.provider_list
    except Exception:
        return None
    known = {normalize_provider_name(str(getattr(p, "value", p))) for p in providers}
    return normalized in known


def _provider_schema_cls(name: str, *, authoritative: bool = True) -> type[BaseModel]:
    """Look up the Pydantic class for a provider, e.g. ``'gemini' -> GeminiProviderConfig``.

    ``authoritative=False`` keeps the lookup off the LiteLLM import path; see
    ``_litellm_knows``. Only reporting may pass it.
    """
    field = ProvidersConfig.model_fields.get(name)
    if field is None:
        # A vendor with no spec of ours is still configurable when LiteLLM knows
        # it -- the plain section is all it needs. Anything LiteLLM has never
        # heard of is a typo, and saying so beats writing a section that will
        # never be read.
        if _litellm_knows(name, authoritative=authoritative) is not False:
            # Includes "cannot say": refusing a name we failed to verify would
            # block a valid provider, while accepting one at worst writes a
            # section nothing reads. The lesser harm is to accept.
            return ProviderConfig
        raise KeyError(
            f"Unknown provider '{name}'. Available providers: {sorted(_provider_names())}. "
            "Many more vendors are supported -- try the name the vendor uses for itself."
        )
    ann = _unwrap_optional(field.annotation)
    if not _is_model_class(ann):
        raise KeyError(f"'{name}' is not a provider section. Available providers: {sorted(_provider_names())}")
    return ann


def _provider_spec(name: str) -> ProviderSpec | None:
    """Look up ``ProviderSpec``, or None for a vendor we carry no spec for."""
    return find_by_name(name)


def _provider_aliases(name: str) -> tuple[str, ...]:
    """Former names of ``name`` as they may still appear in config.json."""
    spec = find_by_name(name)
    return spec.name_aliases if spec else ()


def _raw_section(data: dict[str, Any], name: str) -> dict[str, Any]:
    """Read a provider's raw section, folding in any pre-rename section.

    Both names can hold half the settings; taking whichever is non-empty would
    drop the other's fields on the next write. Current name wins per field.
    """
    providers = data.get("providers") or {}
    section: dict[str, Any] = {}
    # Spelling-insensitive, exactly like `ProvidersConfig.get`: a section written
    # as LiteLLM spells it ("nano-gpt") or as this model serializes it
    # ("nanoGpt") is the same section, and the management surface reading only
    # the underscored key is how a key the runtime happily uses became
    # invisible to `provider get/set`.
    for want in (*_provider_aliases(name), name):
        for key, older in providers.items():
            if names_same_provider(key, want) and isinstance(older, dict):
                section = _overlay(section, older)
    return section


def _write_raw_section(data: dict[str, Any], name: str, section: dict[str, Any]) -> None:
    """Write a provider's section under its current name, retiring older keys.

    Retires every key that names the same provider, not just declared aliases:
    `_raw_section` folds spellings together on read, so leaving the old spelling
    behind would produce two sections for one provider -- the next read merges
    them and the loser's fields reappear after being removed.
    """
    providers = data.setdefault("providers", {})
    wanted = (*_provider_aliases(name), name)
    for key in [k for k in providers if any(names_same_provider(k, w) for w in wanted)]:
        providers.pop(key, None)
    providers[name] = section


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


def _redact_nested_model(instance: BaseModel) -> BaseModel:
    """Redact this model's own secret fields, by the same rule as the flat ones.

    ``_flatten_instance`` only recurses into ``BaseModel`` fields, not into a
    ``list[BaseModel]`` field like ``ProviderConfig.endpoints`` -- so a caller
    that walks ``specs`` (field-name keyed) never sees a per-endpoint field and
    can't redact it. This is applied to each list element instead.
    """
    updates = {
        fname: _redact(getattr(instance, fname))
        for fname, finfo in type(instance).model_fields.items()
        if _is_secret_field(fname, finfo)
    }
    return instance.model_copy(update=updates) if updates else instance


#: Copilot's credentials are two files LiteLLM owns, not one: the device-flow
#: access token and the short-lived API key it is exchanged for.
_COPILOT_TOKEN_FILES = ("access-token", "api-key.json")


def _oauth_token_path(provider_name: str) -> Path:
    """Resolve where this provider's credential lives under Raven's own directory.

    Every family is asked of the module that writes it, so this cannot drift from
    what a login leaves behind. Deriving it a second time here is what wrote
    ``openai_codex`` under one name and read it under another -- and each family
    has a way to override its directory, so a second derivation is wrong exactly
    when a user has taken one.
    """
    from raven.providers.auth import credential_files

    return credential_files(provider_name)[0]


def _copilot_token_dir() -> Path:
    """The directory LiteLLM's Copilot authenticator reads and writes.

    ``import_litellm`` points ``GITHUB_COPILOT_TOKEN_DIR`` at Raven's own
    directory before LiteLLM is imported; reading the same variable here keeps
    one answer even when a user has set it themselves.
    """
    from raven.config.paths import get_oauth_dir

    token_dir = os.environ.get("GITHUB_COPILOT_TOKEN_DIR")
    return Path(token_dir).expanduser() if token_dir else get_oauth_dir() / "github_copilot"


def _oauth_credentials_present(provider_name: str) -> bool:
    """Are this provider's credentials on disk and readable as credentials?

    A file at the right path is not evidence: a truncated write passes
    ``exists()`` and fails on the first request. What each family's own reader
    accepts is the answer.

    Expiry is not part of it -- an expired token with a refresh token is usable.
    Nor is acceptance, which takes a request: that is ``provider test``.
    """
    if provider_name in {"minimax_global", "minimax_cn"}:
        from raven.providers.minimax_oauth import load_token

        return load_token("global" if provider_name == "minimax_global" else "cn") is not None

    if provider_name == "openai_codex":
        from raven.providers.chatgpt_token import stored_credentials

        return stored_credentials() is not None

    if provider_name == "github_copilot":
        # LiteLLM stores the device token as bare text, so "parses" is only
        # "holds something".
        try:
            return bool(_oauth_token_path(provider_name).read_text(encoding="utf-8").strip())
        except OSError:
            return False

    return _oauth_token_path(provider_name).exists()


def oauth_credential_files(provider_name: str) -> list[Path]:
    """Every file a sign-in for this provider can leave behind.

    Two callers, one list. Disconnect has to clear all of them -- Copilot's API key
    outlives the access token it came from, so deleting the token alone leaves a
    working credential -- and a sign-in has to restrict all of them, for the same
    reason in the other direction.
    """
    from raven.providers.auth import credential_files

    return credential_files(provider_name)


# ---------------------------------------------------------------------------
# Public API: reflection
# ---------------------------------------------------------------------------


def provider_field_specs(name: str) -> dict[str, dict[str, Any]]:
    """Reflect a provider schema into a flat ``path -> spec`` map.

    Each entry has keys: ``type``, ``default``, ``is_secret``, ``description``.
    Used by CLI parsers, the ``provider show`` command, and ``get_provider_config``
    to know which fields exist and which to redact.
    """
    name = canonical_provider_name(name)
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

    out: list[dict[str, Any]] = []
    for fname in _listable_provider_names(data):
        try:
            # Reporting: a stale snapshot costs one row here, whereas importing
            # LiteLLM to render a table costs every caller two seconds.
            cls = _provider_schema_cls(fname, authoritative=False)
        except (KeyError, RuntimeError):
            # A key we cannot resolve to a provider is a typo, and listing is a
            # read-only report: skipping it keeps `provider list` and the startup
            # gate working, where raising would leave the user unable to start
            # Raven or even reach the wizard that would fix the file. RuntimeError
            # covers "LiteLLM unavailable, so we cannot say" -- reporting is not
            # the place to fail on that either.
            continue
        # Through _raw_section, so a provider still stored under its pre-rename
        # key reads as configured here and not just at runtime.
        section = _raw_section(data, fname)
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
        endpoints = list(getattr(instance, "endpoints", []) or [])

        # One rule for every gate: this used to accept a Gemini section holding
        # only `api_key_list` that routing then skipped and startup refused.
        from raven.providers.auth import credential_status

        configured = credential_status(fname, instance, spec=spec, include_external=True).ok
        if is_oauth:
            api_key_redacted = "OAuth token" if configured else "(empty)"
        elif is_local:
            api_key_redacted = "(not needed for local)" if not api_key else "****set****"
        elif api_key or api_key_list:
            api_key_redacted = "****set****"
        elif endpoints and any(ep.api_key for ep in endpoints):
            api_key_redacted = f"****set**** ({len(endpoints)} endpoints)"
        else:
            api_key_redacted = "(empty)"

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
    name = canonical_provider_name(name)
    cls = _provider_schema_cls(name)
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    raw_section = _raw_section(data, name)

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
        elif redact_secrets and isinstance(val, list) and val and isinstance(val[0], BaseModel):
            out[path_key] = [_redact_nested_model(item) for item in val]
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
    name = canonical_provider_name(name)
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

    if spec and spec.is_oauth:
        forbidden = [k for k in fields if field_specs[k]["is_secret"]]
        if forbidden:
            raise RuntimeError(
                f"Provider '{name}' uses OAuth — cannot set credential fields "
                f"{forbidden} directly. Run: raven provider login "
                f"{name.replace('_', '-')}"
            )

    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    raw_section = _raw_section(data, name)

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
        if path_key == "models" and isinstance(coerced, list):
            # The third way a model id gets written down, and the one that used
            # to skip the contract: `provider set --models x` stored a bare id
            # while the picker and the wizard stored a qualified one. Identity
            # still matched, so nothing broke -- which is exactly how the two
            # spellings coexisted last time, until a delete silently matched
            # neither.
            from raven.providers.wire import stored_model_id

            coerced = [stored_model_id(name, str(m)) for m in coerced]
        prev[path_key] = _set_nested(path_key, coerced, working)

    validated = cls.model_validate(working)

    _write_raw_section(data, name, validated.model_dump(by_alias=True))
    _write_atomic(path, data)
    return prev


def serves_default_model(name: str, *, config_path: Path | None = None) -> bool:
    """Is this provider the one that answers the configured default model?

    Resetting it clears the credential and leaves the model id behind, so the
    config still names a provider that can no longer answer -- and the startup
    gate reads that as "not set up" and runs the wizard. Both doors that reset a
    provider ask this so they can say so before it happens.
    """
    from raven.config.loader import load_config

    try:
        config = load_config(config_path) if config_path else load_config()
        model = config.agents.defaults.model
        return bool(model) and config.get_provider_name(str(model)) == canonical_provider_name(name)
    except Exception:
        # A config that cannot be resolved has no default worth protecting, and
        # this is a warning path: it must never be the reason a reset fails.
        return False


def reset_provider(
    name: str,
    *,
    config_path: Path | None = None,
) -> None:
    """Restore a provider to schema defaults. Key preserved; values reset.

    Two cleanup paths run automatically, dispatched on ``ProviderSpec.is_oauth``:

    1. **Config fields** — always rewritten to whatever a fresh Pydantic
       instance produces (``api_key=""``, ``api_base=None``,
       ``api_key_list=[]`` for Gemini, etc.). For OAuth providers those are
       already at defaults, so the write is a no-op for them but harmless.

    2. **OAuth credential files** (``is_oauth=True``) — unlinked from disk so the
       user is effectively logged out. Every file a sign-in can leave behind
       goes, Copilot's API key included. Idempotent: ``missing_ok`` so reset can
       run multiple times without raising.

    Callers don't need to know which case applies — one mental model covers
    both API-key and OAuth providers.
    """
    name = canonical_provider_name(name)
    cls = _provider_schema_cls(name)
    spec = _provider_spec(name)

    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    _write_raw_section(data, name, cls().model_dump(by_alias=True))
    _write_atomic(path, data)

    if spec and spec.is_oauth:
        try:
            if name in {"minimax_global", "minimax_cn"}:
                from raven.providers.minimax_oauth import delete_token

                delete_token("global" if name == "minimax_global" else "cn")

            for stale in oauth_credential_files(name):
                stale.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "update_providers: failed to unlink OAuth token for {}: {}",
                name,
                exc,
            )

    logger.info("update_providers: {} reset to defaults", name)


def _load_provider_models(name: str, data: dict[str, Any]) -> tuple[type, list[str]]:
    cls = _provider_schema_cls(name)
    section = _raw_section(data, name)
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
    from raven.providers.wire import merge_key

    name = canonical_provider_name(name)
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    cls, models = _load_provider_models(name, data)
    # By identity, not by string: the same model written two ways used to land
    # in the list twice, and neither entry could then be removed by the other's
    # spelling.
    if merge_key(name, model) not in {merge_key(name, m) for m in models}:
        models.append(model)
        section = _raw_section(data, name)
        section["models"] = models
        validated = cls.model_validate(section)
        _write_raw_section(data, name, validated.model_dump(by_alias=True))
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
    from raven.providers.wire import merge_key

    name = canonical_provider_name(name)
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    cls, models = _load_provider_models(name, data)
    # Whatever spelling the caller holds removes every spelling of that model:
    # the write paths used to disagree, so a list could hold one model twice.
    target = merge_key(name, model)
    if target in {merge_key(name, m) for m in models}:
        models = [m for m in models if merge_key(name, m) != target]
        section = _raw_section(data, name)
        section["models"] = models
        validated = cls.model_validate(section)
        _write_raw_section(data, name, validated.model_dump(by_alias=True))
        _write_atomic(path, data)
    return models


def _load_provider_endpoints(name: str, data: dict[str, Any]) -> tuple[type, list[ProviderEndpoint]]:
    """Deliberately lets ValidationError out instead of falling back to ``cls()``.

    A section that no longer validates (say, a hand-edited duplicate label)
    already stops ``Config.model_validate`` -- Raven will not start on it. The
    endpoint commands are the user's likeliest self-rescue there, and a swallow
    here made them see an empty list and then *write it back*, wiping every
    real endpoint in the section. A loud error names the problem instead.
    """
    cls = _provider_schema_cls(name)
    section = _raw_section(data, name)
    instance = cls.model_validate(section)
    return cls, list(getattr(instance, "endpoints", []) or [])


def add_provider_endpoint(
    name: str,
    *,
    label: str,
    api_key: str = "",
    api_base: str | None = None,
    extra_headers: dict[str, str] | None = None,
    config_path: Path | None = None,
) -> list[ProviderEndpoint]:
    """Add or replace one entry in a provider's ``endpoints`` list, keyed by ``label``.

    ``label`` is the idempotency key ``ProviderEndpoint`` declares it as: an
    existing entry with that label is replaced wholesale, not merged field by
    field, so re-running this with a rotated ``api_key`` is how the rotation
    gets written. A new label appends.

    Returns the new endpoint list. Raises KeyError for an unknown provider.
    """
    name = canonical_provider_name(name)
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    cls, endpoints = _load_provider_endpoints(name, data)

    new_endpoint = ProviderEndpoint(label=label, api_key=api_key, api_base=api_base, extra_headers=extra_headers)
    updated = [new_endpoint if ep.label == label else ep for ep in endpoints]
    if not any(ep.label == label for ep in endpoints):
        updated.append(new_endpoint)

    section = _raw_section(data, name)
    section["endpoints"] = [ep.model_dump(by_alias=True) for ep in updated]
    validated = cls.model_validate(section)
    _write_raw_section(data, name, validated.model_dump(by_alias=True))
    _write_atomic(path, data)
    return updated


def remove_provider_endpoint(
    name: str,
    label: str,
    *,
    config_path: Path | None = None,
) -> list[ProviderEndpoint]:
    """Remove one endpoint by ``label`` (no-op if absent, mirrors ``remove_provider_model``).

    Returns the new endpoint list. Raises KeyError for an unknown provider.
    """
    name = canonical_provider_name(name)
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    cls, endpoints = _load_provider_endpoints(name, data)

    remaining = [ep for ep in endpoints if ep.label != label]
    if len(remaining) != len(endpoints):
        section = _raw_section(data, name)
        section["endpoints"] = [ep.model_dump(by_alias=True) for ep in remaining]
        validated = cls.model_validate(section)
        _write_raw_section(data, name, validated.model_dump(by_alias=True))
        _write_atomic(path, data)
    return remaining


def list_provider_endpoints(name: str, *, config_path: Path | None = None) -> list[dict[str, Any]]:
    """List a provider's ``endpoints``, ``api_key`` redacted for display.

    Returns one dict per endpoint: ``label``, ``api_key`` (``****set****`` /
    ``(empty)``, same rule as every other secret field), ``api_base``,
    ``extra_headers``. Raises KeyError for an unknown provider.
    """
    name = canonical_provider_name(name)
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    _, endpoints = _load_provider_endpoints(name, data)
    return [
        {
            "label": ep.label,
            "api_key": _redact(ep.api_key),
            "api_base": ep.api_base,
            "extra_headers": ep.extra_headers,
        }
        for ep in endpoints
    ]


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

    1. Look up the provider's ``api_key`` or provider-specific OAuth access
       token and ``api_base``
       (falling back to ``ProviderSpec.default_api_base`` when unset).
    2. ``GET {api_base}/v1/models`` with ``Authorization: Bearer {key}``.
    3. Map status code → keyword (see ``_HTTP_STATUS_MAP``). Unknown codes
       render as ``http_{code}``. Network errors → ``network_error``.

    Returns a dict, never raises. ``transport`` is injectable so unit tests
    can mount an ``httpx.MockTransport`` without touching real network.
    """
    name = canonical_provider_name(name)

    try:
        spec = _provider_spec(name)
        cfg = get_provider_config(name, redact_secrets=False, config_path=config_path)
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

    api_key = cfg.get("api_key") or ""
    api_base = cfg.get("api_base") or (spec.default_api_base if spec else "") or ""
    derived_api_base = False

    # Before the token fetch below, which asks a question this backend does not
    # answer: its catalogue is the credential check.
    if spec and spec.name == "openai_codex":
        return _probe_codex_catalog(timeout_s=timeout_s)

    if spec and spec.name == "github_copilot":
        return _probe_copilot_seat(timeout_s=timeout_s, transport=transport)

    if spec and spec.is_oauth:
        try:
            if spec.name in {"minimax_global", "minimax_cn"}:
                from raven.providers.minimax_oauth import get_token

                token = get_token("global" if spec.name == "minimax_global" else "cn")
                oauth_access = token.access
                api_base = token.resource_url
            else:
                # A new family must add its own branch: defaulting to another
                # family's reads the wrong credential and can start its device flow.
                raise RuntimeError(f"{spec.label} has no credential check here yet")
        except ImportError:
            return {
                "ok": False,
                "status": "oauth_token_missing",
                "elapsed_ms": 0,
                "http_status": None,
                "models_count": None,
                "model_ids": None,
                "error": "OAuth support is not installed",
            }
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
        if not oauth_access:
            return {
                "ok": False,
                "status": "oauth_token_missing",
                "elapsed_ms": 0,
                "http_status": None,
                "models_count": None,
                "model_ids": None,
                "error": "no OAuth token stored",
            }
        api_key = oauth_access

    if not api_key and not (spec and spec.is_local):
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
        # Asked here and not above: the branches in between return for the
        # families whose credential check is not an HTTP ping, and one of them
        # is Copilot -- whose driver starts a GitHub device flow when LiteLLM is
        # asked to resolve it. Deriving eagerly put that flow before the branch
        # that avoids it and hung `provider test github-copilot` on that login.
        # Derived rather than declared, and tracked as such: a 404 from an
        # address we guessed says the vendor has no models route there, while a
        # 404 from one the user typed is a typo they need to see.
        api_base = _litellm_api_base(spec)
        derived_api_base = bool(api_base)

    if not api_base:
        # No address, and for most of these there is nothing the user could have
        # supplied: the endpoint is compiled into the vendor's SDK, so there is
        # no `/models` to ping. Reporting `not_configured` told seven correctly
        # configured providers they were not set up, and pointed at a key they
        # had already set. Say what is true instead -- the credential is present
        # and this probe cannot reach the vendor.
        needs_user_address = bool(spec and (spec.is_local or spec.name == "azure_openai"))
        return {
            "ok": False,
            "status": "not_configured" if needs_user_address else "no_probe_endpoint",
            "elapsed_ms": 0,
            "http_status": None,
            "models_count": None,
            "model_ids": None,
            "error": (
                "api_base is empty and provider has no default"
                if needs_user_address
                else "credential present; this vendor publishes no models endpoint to ping"
            ),
        }

    url = api_base.rstrip("/") + "/models"
    if "/v1" not in api_base:
        url = api_base.rstrip("/") + "/v1/models"

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    if spec and spec.name in {"minimax_global", "minimax_cn"} and api_key:
        headers["x-api-key"] = api_key

    result = _probe_models_endpoint(url, headers, timeout_s=timeout_s, transport=transport)
    if derived_api_base and result.get("status") == "http_404":
        # The address LiteLLM sends completions to is not always where the
        # catalogue lives -- DeepSeek's is `/beta`, which has no `/models`. A 404
        # never says anything about the credential, so reporting a failure here
        # would be the same lie in a new spelling.
        return {
            **result,
            "ok": False,
            "status": "no_probe_endpoint",
            "error": "credential present; this vendor publishes no models endpoint to ping",
        }
    return result


def _litellm_api_base(spec: Any) -> str:
    """The endpoint LiteLLM would send this vendor's request to, or "".

    Asked rather than tabulated: LiteLLM already knows, because it is the thing
    that does the sending, and a second copy of these addresses is a second thing
    to keep current. It answers for four of the ten providers that ship no
    default; the rest compile the address into the vendor SDK and there is
    nothing to return.
    """
    if spec is None:
        return ""
    if spec.is_oauth:
        # Asked before the id is built, because building it can hide the answer:
        # `wire_model` strips the provider name entirely for the codex and azure
        # shapes, so the guard below would be handed a bare "probe-model" and see
        # nothing to object to. An OAuth provider's credential check is its own
        # flow, never a models ping, so there is nothing here for it either way.
        return ""

    from raven.providers.rates import _may_prompt
    from raven.providers.wire import stored_model_id, wire_model

    # Asked of the stored form, not the wire form, for the same reason.
    stored = stored_model_id(spec.name, "probe-model")
    if _may_prompt(stored):
        # Resolving one of these resolves its credentials on the way, and with no
        # token file that prints a device code and blocks. One answer to "can this
        # be handed to LiteLLM" for every caller -- see providers.rates.
        return ""
    try:
        from raven.providers.litellm_setup import import_litellm

        _, _, _, base = import_litellm().get_llm_provider(model=wire_model(stored, spec=spec))
    except Exception:
        return ""
    return base or ""


def _probe_models_endpoint(
    url: str,
    headers: dict[str, str],
    *,
    timeout_s: float,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    """GET a models endpoint and report the result in the probe's vocabulary.

    What to ask and what to send is the caller's answer. A vendor that refuses the
    request every other one accepts is the reason there is more than one caller;
    how the answer is reported is the same for all of them.
    """
    import time

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


def _probe_copilot_seat(*, timeout_s: float, transport: httpx.BaseTransport | None) -> dict[str, Any]:
    """Verify a Copilot seat the way its backend accepts being asked.

    Nothing about this one fits the generic probe. The endpoint arrives with the
    credential rather than from the registry, so the generic path answered
    "api_base is empty and provider has no default" without ever asking. And the
    endpoint refuses a request carrying only an ``Authorization`` header: the
    driver sends a set of editor headers on every call, which is what tells the
    backend an editor is asking, so one header would report a working seat as a
    bad credential. Both come from the driver rather than from a guess here.
    """
    if not _oauth_credentials_present("github_copilot"):
        return {
            "ok": False,
            "status": "oauth_token_missing",
            "elapsed_ms": 0,
            "http_status": None,
            "models_count": None,
            "model_ids": None,
            "error": "no credentials found -- run `raven provider login github-copilot`",
        }

    from raven.providers.litellm_setup import import_litellm

    import_litellm()  # points the authenticator at raven's OAuth directory
    from litellm.llms.github_copilot.authenticator import Authenticator
    from litellm.llms.github_copilot.common_utils import get_copilot_default_headers

    try:
        authenticator = Authenticator()
        # Exchanges the stored device token for an API key when the cached one has
        # expired, so this is where a revoked seat surfaces.
        api_key = authenticator.get_api_key()
        api_base = authenticator.get_api_base()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return {
            "ok": False,
            "status": "oauth_token_missing",
            "elapsed_ms": 0,
            "http_status": None,
            "models_count": None,
            "model_ids": None,
            "error": str(exc),
        }

    if not api_key:
        return {
            "ok": False,
            "status": "oauth_token_missing",
            "elapsed_ms": 0,
            "http_status": None,
            "models_count": None,
            "model_ids": None,
            "error": "no OAuth token stored",
        }

    if not api_base:
        return {
            "ok": False,
            "status": "oauth_token_missing",
            "elapsed_ms": 0,
            "http_status": None,
            "models_count": None,
            "model_ids": None,
            "error": "the credential names no API endpoint -- run `raven provider login github-copilot`",
        }

    return _probe_models_endpoint(
        api_base.rstrip("/") + "/models",
        get_copilot_default_headers(api_key),
        timeout_s=timeout_s,
        transport=transport,
    )


def _probe_codex_catalog(*, timeout_s: float) -> dict[str, Any]:
    """Verify a Codex credential the way the backend will accept being asked.

    The generic probe requests ``{api_base}/v1/models``, which this backend does
    not serve: a valid OAuth credential came back refused, reading as a bad key.
    Its catalogue endpoint is both the credential check and the answer to what the
    account may use.
    """
    import time

    from raven.providers.chatgpt_token import stored_credentials
    from raven.providers.codex_catalog import account_models, reset_cache

    start = time.monotonic()
    reset_cache()  # a probe reports on now, not on what a picker asked minutes ago

    if stored_credentials() is None:
        return {
            "ok": False,
            "status": "oauth_token_missing",
            "elapsed_ms": 0,
            "http_status": None,
            "models_count": None,
            "model_ids": None,
            "error": "no credentials found -- run `raven provider login openai-codex`",
        }

    # Strict, so a failure is reported rather than flattened into an empty list --
    # and sorted into the one thing the user can do about it. The recovery menus
    # offer signing in again on one status and Retry on the other, so a credential
    # problem reported as a network one strands the user on a screen that cannot
    # fix it. Two ways to arrive at "sign in again": the token could not be
    # produced at all, and the endpoint refused the one that was.
    #
    # The split is not clean and does not claim to be -- the driver wraps a network
    # failure during refresh in the same error as a revoked token -- so it sorts by
    # which action has a chance of helping, and both messages name both causes.
    try:
        models = account_models(timeout=timeout_s, strict=True)
    except RuntimeError as exc:
        return {
            "ok": False,
            "status": "oauth_token_missing",
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "http_status": None,
            "models_count": None,
            "model_ids": None,
            "error": str(exc),
        }
    except httpx.HTTPStatusError as exc:
        refused = exc.response.status_code in (401, 403)
        return {
            "ok": False,
            "status": "oauth_token_missing" if refused else "network_error",
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "http_status": exc.response.status_code,
            "models_count": None,
            "model_ids": None,
            "error": (
                f"the account refused this credential ({exc.response.status_code}) -- "
                "run `raven provider login openai-codex`"
                if refused
                else str(exc)
            ),
        }
    except Exception as exc:  # noqa: BLE001 - reported, not raised
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
    if not models:
        return {
            "ok": False,
            "status": "oauth_token_missing",
            "elapsed_ms": elapsed_ms,
            "http_status": None,
            "models_count": 0,
            "model_ids": [],
            "error": "the account offers no models -- the credential may have been revoked; "
            "run `raven provider login openai-codex`",
        }

    return {
        "ok": True,
        "status": "valid",
        "elapsed_ms": elapsed_ms,
        "http_status": 200,
        "models_count": len(models),
        "model_ids": list(models),
        "error": None,
    }


__all__ = [
    "provider_field_specs",
    "list_providers",
    "get_provider_config",
    "set_provider_fields",
    "reset_provider",
    "add_provider_endpoint",
    "remove_provider_endpoint",
    "list_provider_endpoints",
    "test_provider",
]
