"""Throwaway TLS material and HTTPS endpoints for tests that need a real handshake.

Every key is minted for the test that asks for it, so none lives in the
repository, and every endpoint listens on 127.0.0.1 only.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import http.server
import ipaddress
import json
import socket
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# Already in OpenSSL's canonical form (lowercase, single spaces), so the body of
# its DER encoding is exactly what OpenSSL hashes into the file name a
# directory store finds the root under.
_ROOT_NAME = "raven test root"


@dataclass(frozen=True)
class PrivateCA:
    """A root no public bundle carries, and a certificate it issued for 127.0.0.1."""

    root: Path
    server: Path
    store: Path


def private_ca(directory: Path) -> PrivateCA:
    """Mint the root, the server certificate, and a directory store holding the root.

    ``server`` holds the certificate and its key, the shape ``load_cert_chain``
    takes. ``store`` is laid out the way OpenSSL reads a ``capath`` such as
    ``/etc/ssl/certs``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _ROOT_NAME)])
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(key_cert_sign=True, crl_sign=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(root_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(digital_signature=True), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    root_pem = root.public_bytes(serialization.Encoding.PEM)
    ca = PrivateCA(root=directory / "root.pem", server=directory / "server.pem", store=directory / "store")
    ca.root.write_bytes(root_pem)
    ca.server.write_bytes(
        server.public_bytes(serialization.Encoding.PEM)
        + server_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    ca.store.mkdir()
    (ca.store / f"{_subject_hash(root)}.0").write_bytes(root_pem)
    return ca


class OpenAIModels(http.server.BaseHTTPRequestHandler):
    """Answers ``GET /v1/models`` the way an OpenAI-compatible gateway does."""

    def do_GET(self) -> None:
        body = json.dumps({"object": "list", "data": [{"id": "corp-model", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@contextmanager
def https_endpoint(server: Path, handler: type[http.server.BaseHTTPRequestHandler]) -> Iterator[str]:
    """Serve ``handler`` over HTTPS on 127.0.0.1 and yield its origin."""
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(server)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@contextmanager
def connect_proxy() -> Iterator[str]:
    """A forward proxy on 127.0.0.1 that tunnels ``CONNECT`` and nothing else; yields its URL."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.2)
    stopped = threading.Event()

    def pipe(source: socket.socket, sink: socket.socket) -> None:
        with contextlib.suppress(OSError):
            while chunk := source.recv(65536):
                sink.sendall(chunk)
        for end in (source, sink):
            with contextlib.suppress(OSError):
                end.shutdown(socket.SHUT_RDWR)

    def tunnel(client: socket.socket) -> None:
        with client:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            host, _, port = head.split(b" ", 2)[1].decode().rpartition(":")
            with socket.create_connection((host, int(port))) as upstream:
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                back = threading.Thread(target=pipe, args=(upstream, client), daemon=True)
                back.start()
                pipe(client, upstream)
                back.join()

    def accept() -> None:
        while not stopped.is_set():
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=tunnel, args=(client,), daemon=True).start()

    acceptor = threading.Thread(target=accept, daemon=True)
    acceptor.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        stopped.set()
        acceptor.join()
        listener.close()


def _key_usage(**granted: bool) -> x509.KeyUsage:
    usages = (
        "digital_signature",
        "content_commitment",
        "key_encipherment",
        "data_encipherment",
        "key_agreement",
        "key_cert_sign",
        "crl_sign",
        "encipher_only",
        "decipher_only",
    )
    return x509.KeyUsage(**{usage: granted.get(usage, False) for usage in usages})


def _subject_hash(cert: x509.Certificate) -> str:
    """OpenSSL's ``X509_NAME_hash``: the first four bytes of a SHA-1, little-endian.

    It hashes the name's canonical encoding without the outer SEQUENCE header,
    which for a name already in canonical form is the DER body as written.
    """
    der = cert.subject.public_bytes()
    length = der[1]
    body = der[2:] if length < 0x80 else der[2 + (length & 0x7F) :]
    return f"{int.from_bytes(hashlib.sha1(body).digest()[:4], 'little'):08x}"
