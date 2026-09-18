"""Who may call this host's A2A face.

One configured bearer token, declared on the card as ``http_auth``. Deliberately
not the ``/rpc`` cookie: that authenticates this user's browser to their own
gateway and is minted by a human clicking a nonce, while an A2A caller is a
program in another trust domain. One credential across both faces would make
either one's compromise the other's.

An empty configured token refuses everyone rather than admitting everyone --
a server switched on before its token is set must not be open.
"""

from __future__ import annotations

import hmac

from raven.config.schema import A2aServerConfig

_PREFIX = "bearer "


def is_authorized(config: A2aServerConfig, header_value: str | None) -> bool:
    """Whether an ``Authorization`` header value carries the configured token."""
    if not config.token:
        return False
    if not header_value or not header_value.lower().startswith(_PREFIX):
        return False
    presented = header_value[len(_PREFIX) :].strip()
    return hmac.compare_digest(presented, config.token)
