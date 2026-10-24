"""Fetch the package an ``npx`` launch names before its handshake, in a process nothing here ends.

``npx -y <spec>`` downloads ``<spec>`` into npm's npx cache on first use and only
then starts it, so the first launch of an npx-run adapter spends what should be its
handshake downloading -- 288 MB for the Claude Code adapter. npm writes the cache
entry's own ``package.json`` last, after every package is unpacked, and an agent is
ended with SIGKILL, which npm cannot catch to roll its install back. A launch ended
in between leaves the entry holding the package and no ``package.json``, and from
npm 11.2 every later ``npx`` of an exact-version spec finds the package, skips the
install, fails to read that ``package.json`` and exits 254 (measured on npm 11.18.0,
2026-10-23). The entry never recovers on its own; `incomplete_entry` reads npm's
report of one.

So a launch fetches first, with an ``npx`` of its own that installs the package and
runs nothing (``node -e 0``), and starts the agent once that has finished. Nothing
here ever signals the fetch: it runs in a session of its own, without the host's
mark or the wrapper that ties an agent to its host (`leftovers`), writes to an
anonymous file rather than to a pipe a dying host would close under it, and is a
plain ``Popen`` because an asyncio subprocess transport kills a child it still holds
when it closes. A caller that stops waiting -- a timeout, a cancelled connect, Raven
exiting -- leaves it to finish, and the next launch finds the package complete.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import IO, NamedTuple

from loguru import logger

from raven.acp_client import leftovers
from raven.acp_client.protocol import AcpConnectionError
from raven.utils.commands import command_argv

_NPX_NAMES = frozenset({"npx", "npx.cmd", "npx.exe"})
_YES = frozenset({"-y", "--yes"})

# A registry package by its npm name, `name` or `@scope/name`, with an optional exact
# version or tag. npx names anything else by something other than the word itself (a
# directory or tarball by its resolved path, a git or GitHub spec by what it resolves
# to), and a range or a word no npm name spells is left to the launch as it stands.
_REGISTRY_SPEC = re.compile(
    r"^(?!.*\.(?:tgz|tar|tar\.gz)$)(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*(?:@[a-z0-9][a-z0-9.+_-]*)?$",
    re.IGNORECASE,
)

# npm 11.2+ reading the package.json of an npx entry that has none.
_INCOMPLETE_ENTRY = re.compile(
    r"Could not read package\.json: Error: ENOENT: no such file or directory, open "
    r"'(?P<directory>[^'\n]*[\\/]_npx[\\/](?P<key>[0-9a-f]{16}))[\\/]package\.json'"
)

_POLL_S = 0.1
_NPM_CONFIG_TIMEOUT_S = 15.0
_STDERR_TAIL = 400

_cache_roots: dict[tuple[tuple[str, str], ...], Path | None] = {}
_fetches: dict[str, asyncio.Task[None]] = {}


class IncompleteEntry(NamedTuple):
    """An npx cache entry left holding its package and no ``package.json``."""

    directory: str
    key: str

    @property
    def clear_command(self) -> str:
        """npm's own command for removing this one entry, which npm 11.2 brought with the failure it fixes."""
        return f"npm cache npx rm {self.key}"


def fetchable_package(argv: Sequence[str]) -> str | None:
    """The one registry package ``argv`` has npx fetch and run, or ``None`` when the fetch is npx's own.

    Only ``npx [-y|--yes]... <spec> [args...]``, the shape every preset uses. Any
    other option before the spec can change what npx resolves (``--registry``,
    ``--package``), and the fetch has to resolve exactly what the launch will, so a
    launch carrying one keeps fetching inside its handshake as it always has.
    """
    if len(argv) < 2 or os.path.basename(argv[0]).lower() not in _NPX_NAMES:
        return None
    for word in argv[1:]:
        if word in _YES:
            continue
        if word.startswith("-"):
            return None
        return word if _REGISTRY_SPEC.match(word) else None
    return None


def cache_key(spec: str) -> str:
    """The name npx gives the cache entry of a launch of ``spec`` alone: its sha512, cut to 16 hex digits."""
    return hashlib.sha512(spec.encode("utf-8")).hexdigest()[:16]


def incomplete_entry(text: str) -> IncompleteEntry | None:
    """The npx entry npm reports holding a package and no ``package.json``, if ``text`` is that report."""
    match = _INCOMPLETE_ENTRY.search(text or "")
    return IncompleteEntry(match["directory"], match["key"]) if match else None


async def ensure_fetched(
    name: str, command: str | Sequence[str], *, env: Mapping[str, str] | None, cwd: str | None
) -> None:
    """Have npx hold the package a launch of ``command`` names, fetching it first when it does not.

    Returns at once for a command that is not a plain npx launch
    (`fetchable_package`) and for a package whose npx entry is complete. Otherwise
    it waits for the fetch -- the one already running for that entry, if any -- and
    raises :class:`AcpConnectionError` carrying npm's own words when the fetch fails.
    ``env`` is the launch's own, laid over the login shell's as the launch lays it.
    A caller cancelled while it waits stops waiting; the fetch goes on.
    """
    try:
        argv = command_argv(command) if isinstance(command, str) else [str(word) for word in command]
    except ValueError:
        return
    spec = fetchable_package(argv)
    if spec is None:
        return
    fetch_env = await asyncio.to_thread(_fetch_env, env)
    root = await asyncio.to_thread(_npm_cache_root, fetch_env)
    entry = root / "_npx" / cache_key(spec) if root is not None else None
    if entry is not None and _complete(entry):
        return
    loop = asyncio.get_running_loop()
    key = str(entry) if entry is not None else f"{spec}\0{fetch_env.get('PATH', '')}"
    job = _fetches.get(key)
    if job is None or job.done() or job.get_loop() is not loop:
        job = loop.create_task(_fetch(name, argv[0], spec, env=fetch_env, cwd=cwd), name=f"npx-fetch-{spec}")
        _fetches[key] = job
        job.add_done_callback(partial(_forget, key))
    await asyncio.shield(job)


def _complete(entry: Path) -> bool:
    """Whether ``entry`` holds its ``package.json``. One that cannot be looked into is fetched, and npm says why."""
    try:
        return (entry / "package.json").is_file()
    except OSError:
        return False


def _forget(key: str, job: asyncio.Task[None]) -> None:
    if _fetches.get(key) is job:
        del _fetches[key]
    if not job.cancelled():
        job.exception()


def _fetch_env(env: Mapping[str, str] | None) -> dict[str, str]:
    from raven.agent.subagent.backends.env import login_shell_env

    return leftovers.unmarked({**login_shell_env(), **(env or {})})


def _npm_cache_root(env: Mapping[str, str]) -> Path | None:
    """The cache npm uses for a process started with ``env``: ``npm_config_cache``, else npm's own answer.

    npm also reads the setting from its config files, so short of the variable the
    answer is asked of npm, once per environment, and the platform's default stands
    in when npm cannot say.
    """
    settings = {key.lower(): value for key, value in env.items() if key.lower().startswith("npm_config_")}
    if settings.get("npm_config_cache"):
        return Path(settings["npm_config_cache"])
    memo = tuple(sorted((key, env.get(key, "")) for key in ("PATH", "HOME", "USERPROFILE", "LOCALAPPDATA"))) + tuple(
        sorted(settings.items())
    )
    if memo not in _cache_roots:
        _cache_roots[memo] = _ask_npm_for_its_cache(env) or _default_cache_root(env)
    return _cache_roots[memo]


def _ask_npm_for_its_cache(env: Mapping[str, str]) -> Path | None:
    from raven.agent.subagent.backends.env import resolve_program

    npm = shutil.which("npm", path=env.get("PATH"))
    if npm is None:
        return None
    query = {**env, "npm_config_update_notifier": "false"}
    try:
        done = subprocess.run(
            resolve_program([npm, "config", "get", "cache"], query, batch_files=True),
            env=query,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_NPM_CONFIG_TIMEOUT_S,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.debug("npx fetch: `npm config get cache` did not answer: {}", exc)
        return None
    answer = done.stdout.strip().splitlines()
    return Path(answer[-1].strip()) if done.returncode == 0 and answer and answer[-1].strip() else None


def _default_cache_root(env: Mapping[str, str]) -> Path | None:
    if sys.platform == "win32":
        local = env.get("LOCALAPPDATA")
        return Path(local) / "npm-cache" if local else None
    home = env.get("HOME")
    return Path(home) / ".npm" if home else None


async def _fetch(name: str, npx: str, spec: str, *, env: dict[str, str], cwd: str | None) -> None:
    from raven.agent.subagent.backends.env import resolve_program

    argv = resolve_program([npx, "-y", f"--package={spec}", "--", "node", "-e", "0"], env, batch_files=True)
    try:
        log = tempfile.TemporaryFile()
    except OSError as exc:
        logger.warning(
            "acp agent {!r}: cannot fetch {} ahead of its start, so npx fetches it there: {}", name, spec, exc
        )
        return
    try:
        try:
            proc = await asyncio.to_thread(_spawn, argv, env, cwd, log)
        except (OSError, ValueError) as exc:
            raise AcpConnectionError(f"acp agent {name!r}: cannot start {npx!r}: {exc}") from exc
        logger.info("acp agent {!r}: fetching {} with npx before its first start (pid {})", name, spec, proc.pid)
        try:
            while (returncode := proc.poll()) is None:
                await asyncio.sleep(_POLL_S)
        except BaseException:
            # Nobody is waiting any more, and the fetch must still finish: a thread
            # of its own reaps it, so it is neither killed nor left a zombie.
            threading.Thread(target=proc.wait, name=f"npx-fetch-{proc.pid}", daemon=True).start()
            raise
        if returncode != 0:
            log.seek(0)
            said = log.read().decode("utf-8", errors="replace")
            tail = said.strip()[-_STDERR_TAIL:] or "<empty>"
            raise AcpConnectionError(
                f"acp agent {name!r}: npx could not fetch {spec} (exit {returncode}); stderr tail: {tail}",
                stderr=said,
                returncode=returncode,
            )
        logger.info("acp agent {!r}: fetched {}", name, spec)
    finally:
        log.close()


def _spawn(argv: list[str], env: dict[str, str], cwd: str | None, log: IO[bytes]) -> subprocess.Popen[bytes]:
    windows = sys.platform == "win32"
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        env=env,
        cwd=cwd,
        start_new_session=not windows,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if windows
        else 0,
    )


__all__ = ["IncompleteEntry", "cache_key", "ensure_fetched", "fetchable_package", "incomplete_entry"]
