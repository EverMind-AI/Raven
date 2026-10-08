"""Which certificate store the process verifies TLS against, and telling a refused certificate apart."""

from __future__ import annotations

import http.server
import socket
import ssl
import threading

import httpx
import pytest
import truststore

from raven.security.tls import is_untrusted_certificate, use_system_ca
from tests._tls import OpenAIModels, https_endpoint, private_ca


@pytest.fixture
def process_ssl():
    """Hand ``ssl`` back as it was: what these change belongs to the whole process."""
    yield
    truststore.extract_from_ssl()


def test_contexts_built_afterwards_verify_with_the_operating_system(monkeypatch, process_ssl) -> None:
    monkeypatch.delenv("RAVEN_NO_SYSTEM_CA", raising=False)

    use_system_ca()

    assert isinstance(ssl.create_default_context(), truststore.SSLContext)


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_opting_out_leaves_ssl_as_it_was(monkeypatch, process_ssl, value: str) -> None:
    monkeypatch.setenv("RAVEN_NO_SYSTEM_CA", value)

    use_system_ca()

    assert not isinstance(ssl.create_default_context(), truststore.SSLContext)


@pytest.mark.parametrize("value", ["", "0", "false", "off"])
def test_a_value_that_does_not_say_yes_is_not_an_opt_out(monkeypatch, process_ssl, value: str) -> None:
    monkeypatch.setenv("RAVEN_NO_SYSTEM_CA", value)

    use_system_ca()

    assert isinstance(ssl.create_default_context(), truststore.SSLContext)


def _failure(url: str) -> httpx.HTTPError:
    with httpx.Client(timeout=5, trust_env=False) as client:
        try:
            client.get(url)
        except httpx.HTTPError as exc:
            return exc
    raise AssertionError(f"{url} answered")


def test_a_refused_certificate_is_told_apart_from_the_other_ways_a_connection_fails(tmp_path) -> None:
    """Each failure here is a real one, raised by a real connection attempt."""
    ca = private_ca(tmp_path / "pki")
    with https_endpoint(ca.server, OpenAIModels) as origin:
        refused_certificate = _failure(f"{origin}/v1/models")

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    refused_connection = _failure(f"https://127.0.0.1:{closed}/v1/models")

    plain = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OpenAIModels)
    threading.Thread(target=plain.serve_forever, daemon=True).start()
    try:
        not_tls = _failure(f"https://127.0.0.1:{plain.server_address[1]}/v1/models")
    finally:
        plain.shutdown()
        plain.server_close()

    assert is_untrusted_certificate(refused_certificate)
    assert not is_untrusted_certificate(refused_connection)
    assert not is_untrusted_certificate(not_tls)
