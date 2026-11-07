"""The frontmatter flag rules shared by the skill registry and the importer.

The registry owns the meaning of ``always`` and of the ``metadata``
namespace. The package importer must ask the same question of the same
parser, not re-derive it from the text: a second grammar drifts from the one
that decides, and a third-party copy could then keep the flag the registry
reads.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

_NAMESPACES = ("raven", "nanobot", "openclaw")
_ALWAYS_TRUTHY = {"true", "1", "yes"}
_ALWAYS_KNOWN = _ALWAYS_TRUTHY | {"false", "0", "no", ""}


def nested_metadata(raw: object) -> dict:
    """Extract the Raven-namespaced metadata from a ``metadata`` value.

    The value is a mapping, or a JSON string, or the mapping YAML reads the
    same one-line JSON as. Lookup order (first match wins): ``raven`` >
    ``nanobot`` > ``openclaw``. Returns ``{}`` on any failure.
    """
    if isinstance(raw, dict):
        data = raw
    elif not isinstance(raw, str) or not raw:
        return {}
    else:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(data, dict):
        return {}
    for key in _NAMESPACES:
        if key in data and isinstance(data[key], dict):
            return data[key]
    return {}


def parse_always_value(raw: object) -> bool:
    """Strict always parser (R3). Only explicit truthy values count."""
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        lower = raw.strip().lower()
        if lower not in _ALWAYS_KNOWN:
            log.warning(
                "Unrecognized 'always' value '%s' — treated as false. Expected true/false/yes/no/1/0.",
                raw,
            )
        return lower in _ALWAYS_TRUTHY
    return False


def resolve_always(frontmatter: dict, nested: dict) -> bool:
    """Resolve the always flag from nested metadata (priority) or frontmatter.

    R3 fix: ``"false"`` is now correctly treated as ``False`` instead of the
    old behavior where ``bool("false") == True``.
    """
    nested_val = nested.get("always")
    if nested_val is not None:
        return parse_always_value(nested_val)
    return parse_always_value(frontmatter.get("always"))


__all__ = ["nested_metadata", "parse_always_value", "resolve_always"]
