"""Where a provider's connection material comes from, however the section spells it.

A provider config has carried the same three things -- key, address, headers --
under three different shapes: the flat ``api_key``/``api_base`` every section
has; Gemini's ``api_key_list`` (several keys, one section, sharing the flat
address); and now ``endpoints`` (several full label/key/base/headers groups,
the material S2's rotation and failover will read). Reading any one of them
independently is how the Gemini list came to be declared and never used --
``GeminiProviderConfig.effective_api_key`` reads only the first key, so listing
several kept exactly one of them alive.

``provider_endpoints`` is the one place that resolves the three shapes into a
uniform list, so "every endpoint this section offers" is asked once rather
than re-derived at each call site with its own idea of the precedence.

The three do not mix in one respect: ``endpoints`` set means the flat
``api_key`` and ``api_key_list`` are ignored outright, not merged with the
list -- a partial merge of the key is how a stale flat one would outlive the
endpoint meant to replace it. ``api_base``/``extra_headers`` are different:
an entry inherits the section's flat value for each one it does not name
itself (each falls back independently), the same way
every ``api_key_list`` entry already shares the flat address -- an
``endpoint add`` that only ever set ``--label``/``--api-key`` is otherwise
unable to run at all, address included, while the very config it wrote passes
every other check.

An unconfigured section (no endpoints, no list, no flat key) resolves to one
endpoint holding the empty flat values rather than an empty list. That is what
every existing caller already got from reading the flat fields directly before
this module existed, so returning nothing here would just move the "now what"
onto each of them instead of answering it once.

The gate and the reader must answer the same question the same way:
``raven.providers.auth._present``, which decides whether a section is usable
at all, calls into this module rather than re-deriving the precedence -- a
section with a healthy flat key and a keyless endpoint must fail the gate
exactly as this function hands the empty key to every actual request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResolvedEndpoint:
    """One url/key/header group, whichever shape it was read from."""

    label: str
    api_key: str
    api_base: str | None
    extra_headers: dict[str, str] | None


def _field(section: Any, name: str) -> Any:
    """Read one field off a section, whether it is a schema object or a raw
    mapping -- see ``raven.providers.auth._present`` for why both reach here."""
    return section.get(name) if isinstance(section, dict) else getattr(section, name, None)


def provider_endpoints(section: Any) -> list[ResolvedEndpoint]:
    """Every endpoint ``section`` offers, in the shape it was declared."""
    endpoints = _field(section, "endpoints")
    if endpoints:
        flat_base = _field(section, "api_base")
        flat_headers = _field(section, "extra_headers")
        return [
            ResolvedEndpoint(
                label=_field(endpoint, "label"),
                api_key=_field(endpoint, "api_key") or "",
                # Each falls back to the section's flat value independently --
                # never the key, which must come from the entry itself or not
                # at all (see the module docstring).
                api_base=_field(endpoint, "api_base") or flat_base,
                extra_headers=_field(endpoint, "extra_headers") or flat_headers,
            )
            for endpoint in endpoints
        ]

    key_list = _field(section, "api_key_list")
    if key_list:
        flat_base = _field(section, "api_base")
        flat_headers = _field(section, "extra_headers")
        return [
            ResolvedEndpoint(
                label=f"key-{i}",
                api_key=key,
                api_base=flat_base,
                extra_headers=flat_headers,
            )
            for i, key in enumerate(key_list, start=1)
        ]

    return [
        ResolvedEndpoint(
            label="default",
            api_key=_field(section, "api_key") or "",
            api_base=_field(section, "api_base"),
            extra_headers=_field(section, "extra_headers"),
        )
    ]
