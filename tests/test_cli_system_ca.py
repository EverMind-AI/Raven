"""The CLI verifies TLS against the operating system's certificate store.

A company gateway, or a proxy that inspects TLS, presents a certificate signed by
a root that IT installs in the operating system; no bundle a Python package
ships carries it. Which store a client trusts is settled before the first client
is built, so these launch raven the two ways it is launched -- the console
script, and ``python -m raven`` as the web supervisor starts its gateway -- each
in a fresh interpreter.

OpenSSL's directory store, relocated by ``SSL_CERT_DIR``, stands in for the
operating system's: it is the store Linux verifies against. The shell also names
a bundle of public roots in ``SSL_CERT_FILE``, so a client that reads those
variables itself, as httpx does, cannot pass by taking ``SSL_CERT_DIR`` on its
own.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import certifi
import pytest

from raven.config.update_providers import set_provider_fields
from tests._tls import OpenAIModels, https_endpoint, private_ca

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="the stand-in store is the one Linux verifies against")

_CONSOLE_SCRIPT = (
    "import sys; from importlib.metadata import entry_points; "
    "sys.argv[0] = 'raven'; sys.exit(entry_points(group='console_scripts')['raven'].load()())"
)
LAUNCHES = {
    "console script": [sys.executable, "-c", _CONSOLE_SCRIPT],
    "python -m raven": [sys.executable, "-P", "-m", "raven"],
}


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    """The test's own environment, minus anything that would point raven elsewhere."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.lower().endswith("_proxy") and not key.startswith(("SSL_CERT_", "RAVEN_"))
    }
    env.update(HOME=str(tmp_path / "user"), RAVEN_HOME=str(tmp_path / "home"), RAVEN_NO_UPDATE_CHECK="1")
    return env


@pytest.fixture
def corporate(tmp_path):
    """The environment of a shell whose gateway only the operating system vouches for."""
    ca = private_ca(tmp_path / "pki")
    with https_endpoint(ca.server, OpenAIModels) as origin:
        set_provider_fields(
            "custom", {"api_key": "k", "api_base": f"{origin}/v1"}, config_path=tmp_path / "home" / "config.json"
        )
        yield {**_isolated_env(tmp_path), "SSL_CERT_FILE": certifi.where(), "SSL_CERT_DIR": str(ca.store)}


def _provider_test(launch: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*launch, "provider", "test", "custom", "--timeout", "5"],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.slow
@linux_only
@pytest.mark.parametrize("launch", LAUNCHES.values(), ids=LAUNCHES.keys())
def test_a_root_only_the_system_store_holds_is_trusted(corporate, launch, tmp_path) -> None:
    done = _provider_test(launch, corporate, tmp_path)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "custom OK" in done.stdout


@pytest.mark.slow
@linux_only
@pytest.mark.parametrize("launch", LAUNCHES.values(), ids=LAUNCHES.keys())
def test_opting_out_leaves_the_probe_on_its_bundled_roots(corporate, launch, tmp_path) -> None:
    done = _provider_test(launch, {**corporate, "RAVEN_NO_SYSTEM_CA": "1"}, tmp_path)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "certificate_untrusted" in done.stdout


@pytest.mark.slow
def test_trust_is_settled_before_the_cli_builds_a_context(tmp_path) -> None:
    """aiohttp makes its default context when it is imported, and importing the
    CLI reaches it, so a switch made after that import would leave every aiohttp
    session on the context built before it. The probe reads that context back.
    """
    probe = (
        "import sys\n"
        "sys.argv = ['raven', '--help']\n"
        "from raven.cli import entry\n"
        "try:\n"
        "    entry.run()\n"
        "except SystemExit:\n"
        "    pass\n"
        "import aiohttp.connector, truststore\n"
        "print('built after the switch:', isinstance(aiohttp.connector._SSL_CONTEXT_VERIFIED, truststore.SSLContext))\n"
    )
    done = subprocess.run(
        [sys.executable, "-P", "-c", probe],
        env=_isolated_env(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert "built after the switch: True" in done.stdout, done.stdout[-2000:] + done.stderr[-2000:]


def test_the_entry_settles_trust_then_hands_the_process_to_the_cli(monkeypatch) -> None:
    """The in-process half of the entry's contract: the switch, then the CLI.

    The subprocess tests above show what that order buys; this one runs the
    entry in the test process, where its own lines can be measured.
    """
    from raven.cli import commands, entry
    from raven.security import tls

    calls: list[str] = []
    monkeypatch.setattr(tls, "use_system_ca", lambda: calls.append("trust"))
    monkeypatch.setattr(commands, "run", lambda: calls.append("cli"))

    entry.run()

    assert calls == ["trust", "cli"]
