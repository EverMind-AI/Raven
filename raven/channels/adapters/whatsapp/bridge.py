"""WhatsApp Node.js bridge: token persistence, build/setup, and the process itself.

The bridge (using @whiskeysockets/baileys) speaks the WhatsApp Web protocol;
this module owns the local process/filesystem side — building it, minting the
shared auth token, and running it as a child of whoever needs it (the channel
adapter under the gateway, or the CLI login). Live process flows are
integration/manual tested.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

DEFAULT_BRIDGE_PORT = 3001
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# What an installed build was compiled from, written next to it after a build.
_FINGERPRINT_FILE = ".source-fingerprint"
_FINGERPRINT_INPUTS = ("package.json", "package-lock.json", "tsconfig.json")

# How a long step (npm install, tsc) shows progress. The adapter owns no terminal;
# the CLI login command installs a spinner here, everything else gets log lines.
ProgressFactory = Callable[[str], AbstractContextManager[None]]


@contextmanager
def _log_progress(label: str) -> Iterator[None]:
    logger.info("  {}", label)
    yield


progress: ProgressFactory = _log_progress


def bridge_token_path() -> Path:
    from raven.config.paths import get_runtime_subdir

    return get_runtime_subdir("whatsapp-auth") / "bridge-token"


def load_or_create_bridge_token(path: Path) -> str:
    """Load a persisted bridge token, or mint and persist one on first use."""
    if path.exists():
        if token := path.read_text(encoding="utf-8").strip():
            return token

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    with suppress(OSError):
        path.chmod(0o600)
    return token


def _find_bridge_source() -> Path | None:
    here = Path(__file__).resolve()
    # here = raven/channels/adapters/whatsapp/bridge.py. The bridge source lives
    # at <package>/bridge in a built wheel (parents[3]) but at the repo root's
    # ./bridge when running from an editable / source checkout (parents[4]).
    candidates = [
        here.parents[3] / "bridge",  # raven/bridge (packaged wheel)
        here.parents[4] / "bridge",  # <repo-root>/bridge (editable / source)
    ]
    return next((c for c in candidates if (c / "package.json").exists()), None)


def source_fingerprint(source: Path) -> str:
    """Digest the bridge sources a build is made of: its manifests and ``src/``."""
    digest = hashlib.sha256()
    files = [source / name for name in _FINGERPRINT_INPUTS]
    files += sorted(p for p in (source / "src").rglob("*") if p.is_file())
    for path in files:
        if not path.is_file():
            continue
        digest.update(path.relative_to(source).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _installed_fingerprint(install_dir: Path) -> str | None:
    try:
        return (install_dir / _FINGERPRINT_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _build_bridge(npm: str, source: Path, install_dir: Path, fingerprint: str) -> None:
    """Compile a copy of ``source`` beside ``install_dir`` and swap it in once it works.

    npm needs a reachable registry and a compiler that agrees with the sources,
    so a rebuild can fail on a machine whose current build runs fine. Staging it
    keeps that failure at "still on the stale build" rather than leaving the
    machine with no ``dist/`` at all until npm works again.
    """
    staging = install_dir.with_name(install_dir.name + ".rebuild")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(source, staging, ignore=shutil.ignore_patterns("node_modules", "dist"))

        logger.info("  Installing dependencies...")
        with progress("npm install (first run: 30-120s)..."):
            subprocess.run([npm, "install"], cwd=staging, check=True, capture_output=True)

        logger.info("  Building...")
        with progress("tsc compile..."):
            subprocess.run([npm, "run", "build"], cwd=staging, check=True, capture_output=True)

        (staging / _FINGERPRINT_FILE).write_text(fingerprint, encoding="utf-8")
        if install_dir.exists():
            shutil.rmtree(install_dir)
        staging.rename(install_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def ensure_bridge_dir() -> Path:
    """Return the built bridge directory, installing/compiling it on first use
    and whenever the installed build came from other sources than this raven
    ships — an upgrade leaves the old ``dist/`` in place otherwise.

    Raises RuntimeError if npm is missing or the bridge source can't be found.
    """
    from raven.config.paths import get_bridge_install_dir

    install_dir = get_bridge_install_dir()
    built = (install_dir / "dist" / "index.js").exists()
    source = _find_bridge_source()
    if not source:
        if built:
            return install_dir  # nothing to compare it against, and nothing to rebuild from
        raise RuntimeError("WhatsApp bridge source not found. Try reinstalling: pip install --force-reinstall raven")

    fingerprint = source_fingerprint(source)
    if built:
        if _installed_fingerprint(install_dir) == fingerprint:
            return install_dir
        logger.info("The installed WhatsApp bridge was built from other sources than this raven ships; rebuilding")

    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm not found. Please install Node.js >= 18.")

    logger.info("Setting up WhatsApp bridge...")
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    _build_bridge(npm, source, install_dir, fingerprint)
    logger.info("WhatsApp bridge ready")
    return install_dir


def bridge_endpoint(bridge_url: str) -> tuple[str, int]:
    """The host and port a ``ws://`` bridge URL points at."""
    parsed = urlparse(bridge_url)
    return parsed.hostname or "localhost", parsed.port or DEFAULT_BRIDGE_PORT


def is_local_bridge(bridge_url: str) -> bool:
    """Whether the URL names this machine, i.e. whether raven may run the bridge
    itself; a remote URL belongs to someone else and is only connected to."""
    host, _ = bridge_endpoint(bridge_url)
    return host in _LOCAL_HOSTS


async def port_is_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Whether something accepts a TCP connection there right now."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()
    return True


async def wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await port_is_open(host, port):
            return True
        await asyncio.sleep(0.25)
    return False


async def spawn_bridge(bridge_dir: Path, token: str, auth_dir: str, port: int) -> asyncio.subprocess.Process:
    """Run the built bridge as a child process. Raises RuntimeError without node.

    Standard streams are inherited rather than piped: the bridge prints the
    pairing QR and its own diagnostics, which belong in the login terminal or in
    the gateway log, and a pipe nobody drains would eventually block the child.
    """
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node not found. Please install Node.js >= 20.")
    env = {**os.environ, "BRIDGE_TOKEN": token, "AUTH_DIR": auth_dir, "BRIDGE_PORT": str(port)}
    return await asyncio.create_subprocess_exec(node, "dist/index.js", cwd=bridge_dir, env=env)


async def terminate_bridge(proc: asyncio.subprocess.Process, timeout: float = 5.0) -> None:
    """Ask the bridge to quit, and kill it if it is still up after ``timeout``."""
    if proc.returncode is not None:
        return
    with suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(Exception):
            await proc.wait()
