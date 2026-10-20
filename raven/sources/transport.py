"""The one place an outbound call to a registered source leaves the machine.

The policy has to be consulted before the request, not after it, and it has
to be consulted in every place a request is made. There used to be more than
one place: the runtime's own web tool and the research flow's web tool each
built their own vendor requests and each issued their own HTTP calls. A rule
checked in one of them was a rule the other did not have.

So the request is made here, and the two callers reach it through a client
that carries the name of the source it is calling. A new vendor therefore
cannot be added without deciding what it is allowed to be used for, because
the moment its requests go through here, a source with no row in the registry
is refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.sources.optins import has_opt_in
from raven.sources.policy import SourceRefusedError, consult

if TYPE_CHECKING:
    import httpx

__all__ = ["PolicyClient", "refusal_text", "send"]


def refusal_text(refused: SourceRefusedError) -> tuple[str, str]:
    """The ``error`` and ``detail`` a refused source produces, for the model and the user.

    ``error`` is the same string for every refusal of a source, so a loop's
    failure streak counts them as one cause rather than many. ``detail``
    carries what was hit and what the user has to do, because a model cannot
    fix a licence on its own.
    """
    error = f"{refused.source} refused by source policy"
    detail = (
        f"{refused.reason}. Tell the user: {refused.remedy}. "
        f"The source's terms are at {_citation_for(refused.source)}."
    )
    return error, detail


def _citation_for(source: str) -> str:
    """Where the terms are, for the message the user reads."""
    from raven.sources.policy import policy_for

    policy = policy_for(source)
    return policy.citation if policy else "the source's own terms"


async def send(
    client: "httpx.AsyncClient",
    method: str,
    url: str,
    *,
    source: str,
    use: str = "call",
    **kwargs: Any,
) -> "httpx.Response":
    """Make one request to a registered source, if its terms allow it.

    Raises :class:`~raven.sources.policy.SourceRefusedError` before the
    request is made, so a refused source never sees the request.
    """
    consult(source, use=use, has_opt_in=has_opt_in(source, use))
    if method.upper() == "GET":
        return await client.get(url, **kwargs)
    return await client.post(url, **kwargs)


class PolicyClient:
    """The client a source's requests go through, so every one of them is checked.

    It answers the two verbs the vendor requests use and nothing else, on
    purpose: a wrapper that grew a third verb would be a wrapper nobody
    remembered to route through.
    """

    def __init__(self, client: "httpx.AsyncClient", source: str, *, use: str = "call") -> None:
        self._client = client
        self._source = source
        self._use = use

    async def get(self, url: str, **kwargs: Any) -> "httpx.Response":
        """One GET to the source."""
        return await send(self._client, "GET", url, source=self._source, use=self._use, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> "httpx.Response":
        """One POST to the source."""
        return await send(self._client, "POST", url, source=self._source, use=self._use, **kwargs)
