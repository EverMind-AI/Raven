"""WhatsApp Node.js bridge: token persistence, build/setup, and login spawn.

The bridge (using @whiskeysockets/baileys) speaks the WhatsApp Web protocol;
this module owns the local process/filesystem side — building it, minting the
shared auth token, and launching the QR-login run. Live process flows are
integration/manual tested.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path

from loguru import logger
from rich.console import Console

_console = Console()


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


def ensure_bridge_dir() -> Path:
    """Return the built bridge directory, installing/compiling it on first use.

    Raises RuntimeError if npm is missing or the bridge source can't be found.
    """
    from raven.config.paths import get_bridge_install_dir

    install_dir = get_bridge_install_dir()
    if (install_dir / "dist" / "index.js").exists():
        return install_dir

    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm not found. Please install Node.js >= 18.")

    here = Path(__file__).resolve()
    candidates = [here.parent.parent.parent / "bridge", here.parent.parent.parent.parent / "bridge"]
    source = next((c for c in candidates if (c / "package.json").exists()), None)
    if not source:
        raise RuntimeError(
            "WhatsApp bridge source not found. Try reinstalling: pip install --force-reinstall raven"
        )

    logger.info("Setting up WhatsApp bridge...")
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    if install_dir.exists():
        shutil.rmtree(install_dir)
    shutil.copytree(source, install_dir, ignore=shutil.ignore_patterns("node_modules", "dist"))

    logger.info("  Installing dependencies...")
    with _console.status("[cyan]npm install (first run: 30-120s)...", spinner="dots"):
        subprocess.run([npm, "install"], cwd=install_dir, check=True, capture_output=True)

    logger.info("  Building...")
    with _console.status("[cyan]tsc compile...", spinner="dots"):
        subprocess.run([npm, "run", "build"], cwd=install_dir, check=True, capture_output=True)

    logger.info("WhatsApp bridge ready")
    return install_dir


def run_login(bridge_dir: Path, token: str, auth_dir: str) -> bool:
    """Spawn `npm start` for the interactive QR login; blocks until it exits."""
    npm = shutil.which("npm")
    if not npm:
        logger.error("npm not found. Please install Node.js.")
        return False
    env = {**os.environ, "BRIDGE_TOKEN": token, "AUTH_DIR": auth_dir}
    try:
        subprocess.run([npm, "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError:
        return False
    return True
