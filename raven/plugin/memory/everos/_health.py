"""Read what a running EverOS server can actually do.

``_server._probe_health`` answers "is the process up". From everos 1.2.1
``/health`` also reports which capabilities the server managed to *build*, and
those are different questions: 1.2.1 boots with ``[llm]`` alone rather than
aborting, so a server whose embedding provider is misconfigured still answers
200 and quietly degrades to keyword-only search. A caller that stops at
"reachable" therefore reports a healthy install that cannot recall anything.

This module only reports what the server says. Deciding whether that contradicts
what the user configured belongs to the caller, which is the side that can read
``everos.toml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from raven.plugin.memory.everos._server import DEFAULT_EVEROS_BASE_URL

# Health is a local, in-process check; a slow answer means the server is wedged,
# and waiting on it would only delay the work it is meant to inform.
HEALTH_TIMEOUT_S = 5.0

# everos.toml section name -> the key /health reports it under. The two differ
# (`embedding` vs `embed`, `multimodal` vs `multimodal_llm`), so a caller that
# reuses the section name reads a present capability as missing.
_SECTION_TO_CAPABILITY = {
    "llm": "llm",
    "embedding": "embed",
    "rerank": "rerank",
    "multimodal": "multimodal_llm",
}

# Nothing works without the llm: extraction needs it, and everos 1.2.1 will not
# even boot without `[llm]` configured.
REQUIRED_SECTIONS = ("llm",)

# Configured but unbuilt, these cost recall quality rather than recall itself --
# the adapter drops to KEYWORD search without embedding, and to the LLM rerank
# lane without rerank. Reported, never treated as a fault.
DEGRADING_SECTIONS = ("embedding", "rerank")


@dataclass(frozen=True)
class CapabilityReport:
    """What ``/health`` said, or why it could not be asked."""

    reachable: bool
    capabilities: dict[str, bool] = field(default_factory=dict)
    error: str = ""

    @property
    def reports_capabilities(self) -> bool:
        """Whether the server is new enough to report capabilities at all.

        Pre-1.2.1 servers return a bare ``{"status": "ok"}``; treating that as
        "everything unavailable" would report a working install as broken.
        """
        return bool(self.capabilities)

    def available(self, section: str) -> bool | None:
        """Whether ``section``'s capability was built, or ``None`` if unknown."""
        return capability_available(self.capabilities, section)


def capability_available(capabilities: dict[str, bool], section: str) -> bool | None:
    """Whether ``section``'s capability was built, per a ``/health`` map.

    ``None`` whenever the map cannot answer -- it is empty (no server reached,
    or one too old to report), or ``/health`` does not cover that section. A
    caller must not read that silence as a negative: doing so condemns a working
    install.
    """
    key = _SECTION_TO_CAPABILITY.get(section)
    if key is None:
        return None
    value = capabilities.get(key)
    return value if isinstance(value, bool) else None


def parse_capabilities(payload: Any) -> dict[str, bool]:
    """The bool-valued subset of a ``/health`` body's ``capabilities`` map.

    ``{}`` whenever the body cannot answer -- absent, malformed, or from a
    pre-1.2.1 server. Shared with the adapter, which reads the same map to pick
    its search lane: parsed two ways, ``raven doctor`` and recall could disagree
    about the same server.
    """
    raw = (payload or {}).get("capabilities") if isinstance(payload, dict) else None
    return {k: v for k, v in raw.items() if isinstance(v, bool)} if isinstance(raw, dict) else {}


def probe_capabilities(base_url: str = DEFAULT_EVEROS_BASE_URL) -> CapabilityReport:
    """Ask a running server what it can do. Never raises."""
    import httpx

    try:
        r = httpx.get(f"{base_url.rstrip('/')}/health", timeout=HEALTH_TIMEOUT_S)
        r.raise_for_status()
        payload = r.json() or {}
    except Exception as exc:
        return CapabilityReport(reachable=False, error=f"{type(exc).__name__}: {exc}")
    return CapabilityReport(reachable=True, capabilities=parse_capabilities(payload))
