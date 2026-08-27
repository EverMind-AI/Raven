"""Atomic operations for tool config sections under ``tools.*``.

This module is the ONLY write path for tool configuration
(``tools.deepResearch``, ``tools.web.search``, ``tools.media.<tool>``). Entry
points -- CLI commands, the onboard wizard, the web UI's tools page -- must call
functions here; direct load_config / save_config on the tools section is
forbidden, matching update_channels / update_providers.

Values land camelCase on disk (``tools.deepResearch.apiKey``) via a Pydantic
validate + ``model_dump(by_alias=True)`` round-trip, so the file never grows a
parallel snake_case key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, ValidationError

from raven.config.loader import ConfigReadError, get_config_path, read_raw_or_raise
from raven.config.schema import DeepResearchToolConfig, MediaToolConfig, WebSearchConfig, WebToolsConfig

_SECTION = "deepResearch"  # camelCase alias of ToolsConfig.deep_research


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    """Atomic write: temp-file then os.replace. Preserves indent=2, UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _current(path: Path) -> DeepResearchToolConfig:
    raw = (read_raw_or_raise(path).get("tools") or {}).get(_SECTION) or {}
    try:
        return DeepResearchToolConfig.model_validate(raw)
    except ValidationError:
        return DeepResearchToolConfig()


def set_deep_research(fields: dict[str, Any], *, config_path: Path | None = None) -> dict[str, Any]:
    """Patch ``tools.deepResearch`` fields (``api_key`` / ``api_base`` / ``model``).

    Validate-then-write: the merged section is validated before anything lands,
    so a bad value raises rather than corrupting the file. Returns
    ``{field: previous_value}`` for caller logging.
    """
    valid = set(DeepResearchToolConfig.model_fields)
    unknown = [k for k in fields if k not in valid]
    if unknown:
        raise KeyError(f"Unknown deep_research field(s) {unknown}. Available: {sorted(valid)}")

    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    working = _current(path).model_dump()
    prev = {k: working.get(k) for k in fields}
    working.update(fields)
    validated = DeepResearchToolConfig.model_validate(working)

    data.setdefault("tools", {})[_SECTION] = validated.model_dump(by_alias=True)
    _write_atomic(path, data)
    return prev


def get_deep_research(*, redact: bool = True, config_path: Path | None = None) -> dict[str, Any]:
    """Return ``tools.deepResearch`` as ``{api_key, api_base, model}``.

    ``api_key`` is redacted by default: ``'****set****'`` when set, ``'(empty)'``
    otherwise.
    """
    inst = _current(config_path or get_config_path())
    key = ("****set****" if inst.api_key else "(empty)") if redact else inst.api_key
    return {"api_key": key, "api_base": inst.api_base, "model": inst.model}


def reset_deep_research(*, config_path: Path | None = None) -> None:
    """Reset ``tools.deepResearch`` to schema defaults (clears the key)."""
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    data.setdefault("tools", {})[_SECTION] = DeepResearchToolConfig().model_dump(by_alias=True)
    _write_atomic(path, data)
    logger.info("update_tools: deep_research reset to defaults")


# ---------------------------------------------------------------------------
# tools.web.search / tools.media.<tool>
#
# Both are read at gateway startup to decide whether a tool is registered at
# all (AgentLoop withholds web_search without a key, and a media tool unless
# its model or key is set), so a write here takes effect on the next restart --
# which is what the caller has to tell the user.

_WEB_SEARCH_PATH = ("web", "search")  # camelCase aliases of ToolsConfig.web.search
_JINA_KEY = "jinaApiKey"  # camelCase alias of ToolsConfig.web.jina_api_key
MEDIA_TOOLS = ("image", "speech", "video")
"""The ``tools.media`` sub-sections, in the order the UI shows them."""


def _subtree(data: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """The raw ``tools.<keys...>`` mapping, or ``{}`` if any level is missing.

    A level that holds something other than a mapping is treated as absent
    rather than raising: this reads a hand-edited file, and the validate step
    below is what decides whether the *values* are usable.
    """
    node: Any = data.get("tools")
    for key in keys:
        if not isinstance(node, dict):
            return {}
        node = node.get(key)
    return node if isinstance(node, dict) else {}


def _current_subtree(data: dict[str, Any], keys: tuple[str, ...], cls: type[BaseModel]) -> BaseModel:
    try:
        return cls.model_validate(_subtree(data, keys))
    except ValidationError:
        return cls()


def _patch_subtree(
    keys: tuple[str, ...],
    cls: type[BaseModel],
    fields: dict[str, Any],
    config_path: Path | None,
    label: str,
) -> dict[str, Any]:
    """Validate-then-write one ``tools.*`` subtree. Returns ``{field: previous}``.

    Only the addressed subtree is replaced, never its parent: ``tools.web`` also
    holds ``jinaApiKey`` and ``proxy``, and ``tools.media`` holds ``proxy`` and
    ``outputSubdir``, none of which this call is about. Writing back a validated
    parent would silently reset whichever of those the user had set.
    """
    valid = set(cls.model_fields)
    unknown = [k for k in fields if k not in valid]
    if unknown:
        raise KeyError(f"Unknown {label} field(s) {unknown}. Available: {sorted(valid)}")

    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    working = _current_subtree(data, keys, cls).model_dump()
    prev = {k: working.get(k) for k in fields}
    working.update(fields)
    validated = cls.model_validate(working)

    node = data.setdefault("tools", {})
    if not isinstance(node, dict):  # a non-mapping "tools" cannot be patched into
        node = data["tools"] = {}
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = node[key] = {}
        node = child
    node[keys[-1]] = validated.model_dump(by_alias=True)
    _write_atomic(path, data)
    return prev


def set_web_search(fields: dict[str, Any], *, config_path: Path | None = None) -> dict[str, Any]:
    """Patch ``tools.web.search`` fields (``api_key`` / ``max_results``)."""
    return _patch_subtree(_WEB_SEARCH_PATH, WebSearchConfig, fields, config_path, "web_search")


def get_web_search(*, redact: bool = True, config_path: Path | None = None) -> dict[str, Any]:
    """Return ``tools.web.search`` as ``{api_key, max_results}``.

    ``api_key`` is redacted by default: ``'****set****'`` when set, ``'(empty)'``
    otherwise -- the same two markers ``get_deep_research`` uses.
    """
    data = read_raw_or_raise(config_path or get_config_path())
    inst = _current_subtree(data, _WEB_SEARCH_PATH, WebSearchConfig)
    key = ("****set****" if inst.api_key else "(empty)") if redact else inst.api_key
    return {"api_key": key, "max_results": inst.max_results}


def get_serper_api_key(*, redact: bool = True, config_path: Path | None = None) -> str:
    """Return just ``tools.web.search.apiKey``, redacted by default.

    ``get_web_search`` already returns it inside the section; a caller that
    wants only the credential would have to subscript it back out, which is the
    read ``test_provider_auth_method``'s credential invariant flags at the call
    site. Reading it here keeps that read in the one module the invariant
    already sanctions for tool credentials, and pairs with
    ``get_jina_api_key``.
    """
    data = read_raw_or_raise(config_path or get_config_path())
    inst = _current_subtree(data, _WEB_SEARCH_PATH, WebSearchConfig)
    return ("****set****" if inst.api_key else "(empty)") if redact else inst.api_key


def set_jina_api_key(key: str, *, config_path: Path | None = None) -> str:
    """Set ``tools.web.jinaApiKey``. Returns the previous value.

    A leaf write, unlike every other setter here, because the narrowest node
    that holds this value is a string. The subtree a patch would have to
    validate is ``tools.web``, and ``Base`` declares no ``extra``, so pydantic
    drops unknown fields: round-tripping that node to change one string would
    delete whatever the user hand-added under it and materialise defaults for
    ``proxy`` and ``search`` besides.
    """
    validated = WebToolsConfig(jina_api_key=key).jina_api_key
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    tools = data.setdefault("tools", {})
    if not isinstance(tools, dict):
        tools = data["tools"] = {}
    web = tools.get("web")
    if not isinstance(web, dict):
        web = tools["web"] = {}
    prev = web.get(_JINA_KEY)
    web[_JINA_KEY] = validated
    _write_atomic(path, data)
    return prev if isinstance(prev, str) else ""


def get_jina_api_key(*, redact: bool = True, config_path: Path | None = None) -> str:
    """Return ``tools.web.jinaApiKey``, redacted by default.

    Redaction uses the same two markers as ``get_web_search`` / ``get_media``.
    """
    data = read_raw_or_raise(config_path or get_config_path())
    raw = _subtree(data, ("web",)).get(_JINA_KEY)
    key = raw if isinstance(raw, str) else ""
    return ("****set****" if key else "(empty)") if redact else key


def _media_path(tool: str) -> tuple[str, str]:
    if tool not in MEDIA_TOOLS:
        raise KeyError(f"Unknown media tool '{tool}'. Available: {list(MEDIA_TOOLS)}")
    return ("media", tool)


def set_media(tool: str, fields: dict[str, Any], *, config_path: Path | None = None) -> dict[str, Any]:
    """Patch ``tools.media.<tool>`` (``api_key`` / ``api_base`` / ``model``).

    Setting either ``model`` or ``api_key`` is what registers the tool at the
    next start; clearing both withdraws it again.
    """
    return _patch_subtree(_media_path(tool), MediaToolConfig, fields, config_path, f"media.{tool}")


def get_media(tool: str, *, redact: bool = True, config_path: Path | None = None) -> dict[str, Any]:
    """Return ``tools.media.<tool>`` as ``{api_key, api_base, model}``."""
    keys = _media_path(tool)
    data = read_raw_or_raise(config_path or get_config_path())
    inst = _current_subtree(data, keys, MediaToolConfig)
    key = ("****set****" if inst.api_key else "(empty)") if redact else inst.api_key
    return {"api_key": key, "api_base": inst.api_base, "model": inst.model}


__all__ = [
    "ConfigReadError",
    "MEDIA_TOOLS",
    "get_deep_research",
    "get_jina_api_key",
    "get_media",
    "get_serper_api_key",
    "get_web_search",
    "reset_deep_research",
    "set_deep_research",
    "set_jina_api_key",
    "set_media",
    "set_web_search",
]
