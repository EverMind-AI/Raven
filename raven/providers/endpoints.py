"""Where a provider's connection material comes from, however the section spells it.

A provider config has carried the same three things -- key, address, headers --
under three different shapes: the flat ``api_key``/``api_base`` every section
has; Gemini's ``api_key_list`` (several keys, one section, sharing the flat
address); and now ``endpoints`` (several full label/key/base/headers groups,
the material :class:`~raven.providers.endpoint_rotor.EndpointRotorProvider`
rotates and fails over across). Reading any one of them independently is how a
declared list comes to be unused: ``GeminiProviderConfig.effective_api_key``
reads only the first key, so listing several keeps exactly one alive.

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

``provider_endpoints`` itself never reads the environment: the gate runs on
the routing hot path and decides from declarations alone, and a display
surface must not fail on a variable either. A caller that is about to send a
request, probe a credential or lend one asks
:func:`provider_endpoints_for_request`, which resolves a flat ``apiKeyEnv``
into the key such a request would carry.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
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


def _api_key_env_name(section: Any) -> str:
    """The variable ``apiKeyEnv`` names, off a schema object or a raw mapping.

    Raw config mappings spell the field camelCase; a mapping already carrying
    the schema spelling is accepted too, as ``provider_endpoints`` does for
    every other field.
    """
    if isinstance(section, dict):
        value = section.get("api_key_env") or section.get("apiKeyEnv")
    else:
        value = getattr(section, "api_key_env", None)
    return str(value or "")


def uses_flat_key(section: Any) -> bool:
    """Whether the flat fields are this section's key source.

    ``endpoints`` or ``api_key_list`` set means the flat key is ignored
    outright, and ``apiKeyEnv`` names only the flat one -- it is not a second
    source for the list shapes.
    """
    return not (_field(section, "endpoints") or _field(section, "api_key_list"))


def resolve_api_key_env(section: Any, *, provider: str = "") -> str:
    """The value ``apiKeyEnv`` names, or ``""`` when it names none.

    Raises ``MissingCredentialsError`` when the variable is named but unset or
    empty, so the failure names the variable rather than reaching the API as
    an auth error. ``provider`` travels into the error for the structured
    diagnostics ``rpc.bootstrap`` emits; the section itself does not know the
    name it is stored under.
    """
    from raven.providers.auth import MissingCredentialsError

    name = _api_key_env_name(section)
    if not name:
        return ""
    value = os.environ.get(name, "")
    if not value:
        raise MissingCredentialsError(
            f"the environment variable {name!r} that apiKeyEnv names is not set",
            provider=provider,
            remedy=f"Export {name}, or clear apiKeyEnv and set apiKey instead.",
        )
    return value


def provider_endpoints_for_request(section: Any, *, provider: str = "") -> list[ResolvedEndpoint]:
    """``provider_endpoints`` with a flat ``apiKeyEnv`` resolved to its value.

    The boundary for a caller about to make a request, probe a credential or
    lend one. Explicit ``endpoints``/``api_key_list`` keep their own keys and
    come back untouched; a flat section with no ``apiKeyEnv`` is unchanged.
    Raises ``MissingCredentialsError`` for a named-but-unset variable -- the
    build is where that refusal belongs, naming the variable.
    """
    endpoints = provider_endpoints(section)
    if not uses_flat_key(section):
        return endpoints
    value = resolve_api_key_env(section, provider=provider)
    if not value:
        return endpoints
    return [replace(endpoint, api_key=value) for endpoint in endpoints]
