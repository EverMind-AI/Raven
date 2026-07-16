"""Atomic operations for tool config sections under ``tools.*``.

This module is the ONLY write path for tool configuration (today just
``tools.deepResearch``). Entry points -- CLI commands, the onboard wizard --
must call functions here; direct load_config / save_config on the tools
section is forbidden, matching update_channels / update_providers.

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
from pydantic import ValidationError

from raven.config.loader import get_config_path
from raven.config.schema import DeepResearchToolConfig

_SECTION = "deepResearch"  # camelCase alias of ToolsConfig.deep_research


class ConfigReadError(RuntimeError):
    """An existing config file could not be parsed. Callers MUST NOT proceed to
    write: overwriting would replace the user's whole config with just the new
    section (data loss). Only a genuinely-absent file is safe to create fresh."""


def _load_raw(path: Path) -> dict[str, Any]:
    """Read raw JSON. Empty dict ONLY when the file is absent.

    A present-but-unreadable file raises ConfigReadError rather than returning
    {} -- returning {} here and then writing was exactly the bug that wiped a
    real config whose only fault was a JSON syntax error (e.g. // comments).
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ConfigReadError(
            f"{path} is not valid JSON ({exc}). Fix it first (JSON allows no comments or "
            "trailing commas); your config was left unchanged."
        ) from exc


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    """Atomic write: temp-file then os.replace. Preserves indent=2, UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _current(path: Path) -> DeepResearchToolConfig:
    raw = (_load_raw(path).get("tools") or {}).get(_SECTION) or {}
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
    data = _load_raw(path)
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
    data = _load_raw(path)
    data.setdefault("tools", {})[_SECTION] = DeepResearchToolConfig().model_dump(by_alias=True)
    _write_atomic(path, data)
    logger.info("update_tools: deep_research reset to defaults")


__all__ = ["ConfigReadError", "set_deep_research", "get_deep_research", "reset_deep_research"]
