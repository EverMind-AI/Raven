"""Which certificates the process trusts for TLS: the operating system's store.

A company-hosted gateway, or a proxy that inspects TLS, presents a certificate
signed by a root that IT installs in the operating system. No bundle a Python
package ships carries it. httpx, LiteLLM and the OpenAI SDK verify against
certifi's bundle unless told otherwise, while aiohttp, urllib and websockets read
OpenSSL's defaults, so one process trusted different roots depending on which
client made a call. Injecting truststore hands verification to the operating
system for every context built afterwards. A bundle a caller loads into a context
(certifi, or ``SSL_CERT_FILE``) is still honored, so every root trusted before
stays trusted, though on macOS and Windows the system verifier also applies its
own certificate policy.

Process-wide by construction, so this is decided once, at the entry point,
before anything builds a context. aiohttp builds its default one when it is
imported. A server-side context built afterwards is truststore's as well, and on
macOS and Windows it then verifies the client's chain, refusing a client that
sends none; raven serves no TLS of its own.
"""

from __future__ import annotations

import os
import ssl

OPT_OUT_ENV = "RAVEN_NO_SYSTEM_CA"
_TRUTHY = {"1", "true", "yes", "on"}


def opted_out() -> bool:
    """Whether ``RAVEN_NO_SYSTEM_CA`` leaves every client verifying the way it does on its own."""
    return os.environ.get(OPT_OUT_ENV, "").strip().lower() in _TRUTHY


def use_system_ca() -> None:
    """Verify TLS against the operating system's store from now on, unless opted out."""
    if opted_out():
        return
    import truststore

    truststore.inject_into_ssl()


def is_untrusted_certificate(exc: BaseException) -> bool:
    """Whether ``exc`` comes from a server certificate that failed verification.

    httpx raises its own ConnectError over the handshake's
    ``ssl.SSLCertVerificationError``, so the cause chain is walked. The message
    is no guide: the macOS and Windows verifiers word the failure in their own
    terms, none of which say CERTIFICATE_VERIFY_FAILED.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False
