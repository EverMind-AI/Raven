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

The three do not mix. ``endpoints`` set means the flat fields and
``api_key_list`` are both ignored outright, not merged with the list -- a
partial merge is how a stale flat key would outlive the endpoint meant to
replace it. ``api_key_list`` without ``endpoints`` still shares the flat
``api_base``/``extra_headers``: those were never plural, so there is nothing
to choose between for them.

An unconfigured section (no endpoints, no list, no flat key) resolves to one
endpoint holding the empty flat values rather than an empty list. That is what
every existing caller already got from reading the flat fields directly before
this module existed, so returning nothing here would just move the "now what"
onto each of them instead of answering it once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raven.config.schema import ProviderConfig


@dataclass(frozen=True)
class ResolvedEndpoint:
    """One url/key/header group, whichever shape it was read from."""

    label: str
    api_key: str
    api_base: str | None
    extra_headers: dict[str, str] | None


def provider_endpoints(section: "ProviderConfig") -> list[ResolvedEndpoint]:
    """Every endpoint ``section`` offers, in the shape it was declared."""
    if section.endpoints:
        return [
            ResolvedEndpoint(
                label=endpoint.label,
                api_key=endpoint.api_key,
                api_base=endpoint.api_base,
                extra_headers=endpoint.extra_headers,
            )
            for endpoint in section.endpoints
        ]

    key_list = getattr(section, "api_key_list", None)
    if key_list:
        return [
            ResolvedEndpoint(
                label=f"key-{i}",
                api_key=key,
                api_base=section.api_base,
                extra_headers=section.extra_headers,
            )
            for i, key in enumerate(key_list, start=1)
        ]

    return [
        ResolvedEndpoint(
            label="default",
            api_key=section.api_key,
            api_base=section.api_base,
            extra_headers=section.extra_headers,
        )
    ]
