"""Which Node.js an npm-installed agent is launched with, and whether it is new enough.

An agent installed with ``npm i -g`` is a script whose first line is
``#!/usr/bin/env node``, so the Node.js that runs it is whichever ``node``
comes first on the PATH it is launched with -- not the one it was installed
with. A machine with an older Node.js ahead of the right one (an nvm default
left on 18, a Homebrew formula nobody upgraded) runs the agent on that, and the
agent dies at import time with nothing that names Node.js. Measured
2026-09-24 on Qwen Code 0.24.4 under Node 18.20.8: ``SyntaxError: The
requested module 'node:fs' does not provide an export named 'openAsBlob'``,
exit 1, while its ``package.json`` asks for ``>=22.0.0``.

So the verdict is read off facts rather than off that sentence: the Node.js the
launch PATH resolves, its version, and the range the agent's own package
declares. The sentence changes with every release; the declaration is the
agent's own word.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_ENGINES_FLOOR = re.compile(r">=\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_NODE_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class NodeTooOld:
    """A Node.js older than the agent declares it needs, and how to replace it."""

    needs: str
    """The floor the agent's ``engines.node`` declares, as written short: ``22``, ``20.19``."""
    found: str
    """The version of the Node.js the launch PATH resolves, without the ``v``."""
    node: str
    """That Node.js's path, as the PATH resolved it."""
    upgrade: str | None
    """A command that upgrades that Node.js, or ``None`` when its installer is not recognised."""


def node_too_old(exe: str, path: str) -> NodeTooOld | None:
    """The Node.js ``exe`` would run on from ``path``, when it is older than ``exe`` declares.

    ``None`` whenever any fact is missing -- ``exe`` is not a Node.js script,
    its package declares no floor, or no ``node`` answers ``--version`` -- so a
    caller never names Node.js on a guess. Runs ``node --version``, so it
    belongs off the event loop.
    """
    script = shutil.which(exe, path=path)
    if script is None:
        return None
    real = os.path.realpath(script)
    node = _interpreter(real, path)
    floor = _declared_floor(real)
    if node is None or floor is None:
        return None
    found = _version_of(node)
    if found is None or found >= floor:
        return None
    return NodeTooOld(
        needs=_short(floor),
        found=".".join(str(part) for part in found),
        node=node,
        upgrade=_upgrade_command(node, floor[0]),
    )


def _interpreter(script: str, path: str) -> str | None:
    """The ``node`` a script's shebang names, resolved the way the kernel and ``env`` would."""
    try:
        with open(script, "rb") as handle:
            first = handle.readline(256).decode("utf-8", "replace")
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    words = first[2:].split()
    if words and os.path.basename(words[0]) == "env":
        words = [word for word in words[1:] if not word.startswith("-")]
        return shutil.which("node", path=path) if words and words[0] == "node" else None
    return words[0] if words and os.path.basename(words[0]) == "node" else None


def _declared_floor(script: str) -> tuple[int, int, int] | None:
    """The ``engines.node`` floor of the package ``script`` belongs to, from its ``package.json``."""
    here = Path(script).parent
    for folder in (here, *list(here.parents)[:3]):
        manifest = folder / "package.json"
        if not manifest.is_file():
            continue
        try:
            engines = json.loads(manifest.read_text(encoding="utf-8")).get("engines") or {}
        except (OSError, ValueError, AttributeError):
            return None
        match = _ENGINES_FLOOR.search(str(engines.get("node") or ""))
        if match is None:
            return None
        major, minor, patch = (int(part or 0) for part in match.groups())
        return major, minor, patch
    return None


def _version_of(node: str) -> tuple[int, int, int] | None:
    try:
        answer = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    match = _NODE_VERSION.match(answer.stdout.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def _short(version: tuple[int, int, int]) -> str:
    """``22.0.0`` as ``22``, ``20.19.0`` as ``20.19``: the floor as a person would say it."""
    major, minor, patch = version
    if patch:
        return f"{major}.{minor}.{patch}"
    return f"{major}.{minor}" if minor else str(major)


def _upgrade_command(node: str, major: int) -> str | None:
    """How to move the Node.js at ``node`` past ``major``, when its installer shows in its path.

    nvm keeps every version under ``~/.nvm/versions/node/``, and a new default is
    what the login shell -- which Raven launches agents from -- picks up. A
    Homebrew ``node`` lives under ``Cellar/node/``; a pinned ``node@NN`` formula
    does not, and is left without a command, as is anything else, rather than
    handed an upgrade for an installer it does not use.
    """
    real = os.path.realpath(node)
    if "/.nvm/versions/node/" in real:
        return f"nvm install {major} && nvm alias default {major}"
    if "/Cellar/node/" in real:
        return "brew upgrade node"
    return None


__all__ = ["NodeTooOld", "node_too_old"]
