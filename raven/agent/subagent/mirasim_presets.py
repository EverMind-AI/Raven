"""Presets for the agents Mirasim runs, one row per agent, each reached through Raven's own shim.

Mirasim (https://mirasim.ai) launches third-party coding agents on the accounts
and model routes its user configured, from the Mirasim host running on the
machine. A row here is that agent as Mirasim runs it -- its model menu is
Mirasim's for that agent, the platform's models and the user's own keys beside
the agent's own -- which is a different agent to delegate to than the same
agent's own row in :mod:`raven.agent.subagent.presets`, launched on its own login.
That is why the two coexist: not two transports for one install, which the preset
table rules out, but two ways of paying for and routing one tool.

The command runs :mod:`raven.acp.mirasim` on the interpreter Raven itself runs
on, so ``{PYTHON}`` is expanded when a preset is read (see
:func:`raven.agent.subagent.presets.third_party_subagent_presets`). ``-P`` keeps
the launch directory -- the caller's working directory, which may itself be a
checkout of this repository -- off ``sys.path``, so the adapter that answers is the
installed one.

Measured 2026-10-12 against Mirasim 0.0.465, which listed these nine as
installed, beside ``echo``: a stub that answers with its prompt, which is not an
agent to delegate to. The labels are the ones it reports -- 0.0.537's host lists
the same nine under the same labels, plus OpenCode, which Raven already offers
on its own -- so a row reads the way the agent does in Mirasim.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from raven.agent.subagent.vendored_agents import _resolved_python
from raven.utils.commands import command_argv, resolve_subagent_command

MIRASIM_AGENTS: dict[str, tuple[str, str, str]] = {
    "mirasim_claude": ("claude", "Claude Code", "Anthropic's agentic coding tool"),
    "mirasim_codex": ("codex", "Codex", "OpenAI's coding agent"),
    "mirasim_kimi": ("kimi", "Kimi Code", "Moonshot AI's coding assistant"),
    "mirasim_antigravity": ("antigravity", "Antigravity", "Google's agentic coding tool"),
    "mirasim_qwen": ("qwen", "Qwen Code", "Alibaba's Qwen coding assistant"),
    "mirasim_grok": ("grok", "Grok Build", "xAI's coding agent"),
    "mirasim_dsh": ("dsh", "DSH", "DeepSeek Harness, a coding agent on DeepSeek models"),
    "mirasim_pi": ("pi", "Pi", "A coding agent CLI with read, bash, edit and write tools"),
    "mirasim_zcode": ("zcode", "ZCode CLI", "Z.ai's ZCode coding agent"),
}
"""Preset key -> (Mirasim agent id, Mirasim's label for it, what it is)."""

MIRASIM_SHIM_PRESETS = frozenset(MIRASIM_AGENTS)

MIRASIM_EXECUTABLE = "mirasim"
"""Mirasim's CLI, as it is named on a PATH the owner put it on; the adapter never runs it (see :func:`presence`)."""
MIRASIM_INSTALL_HINT = "the Mirasim app from https://mirasim.ai/download"


def _row(key: str, agent: str, label: str, what: str) -> dict[str, Any]:
    return {
        "name": f"{label}-Mirasim",
        "preset": key,
        "kind": "acp",
        "description": f"{what}, run through Mirasim.",
        "command": f"{{PYTHON}} -P -m raven.acp.mirasim --agent {agent}",
        # The first session waits on the host listing its agents, which it
        # answers progressively: measured at about two seconds, with the rows
        # probing every agent CLI on the machine. The budget covers the
        # adapter's own limits instead -- reaching the host (15 s), then the
        # agent list and the menus (60 s), twice when the first list says the
        # agent is not installed -- so a slow host is refused by the adapter,
        # saying why, rather than timed out as an agent that hung.
        "readyTimeoutMs": 150000,
        # The Mirasim host takes no MCP servers for one session, so nothing
        # handed over could reach the agent. False is the one declaration that
        # withholds them and also tells the roster none arrive, so a DAG node's
        # `mcps` is reported as ignored rather than assumed. The withheld note,
        # which shows only when a row's own `mcps` names servers, gives
        # isolation as its reason: the one the field was written for.
        "sessionMcp": False,
    }


MIRASIM_PRESETS: dict[str, dict[str, Any]] = {
    key: _row(key, agent, label, what) for key, (agent, label, what) in MIRASIM_AGENTS.items()
}

INSTALLED_QUERY = "{PYTHON} -P -m raven.acp.mirasim --installed"

INSTALLED_NO_HOST_EXIT = 3
"""The exit status of ``--installed`` when no Mirasim host could be reached to ask."""

HOST_ENV = "RAVEN_MIRASIM_HOST"
HOST_ENV_KEYS = (HOST_ENV, "MIRASIM_HOME")
"""The variables the adapter finds its host by (``raven.acp.mirasim.host``).

A row whose ``env`` sets one looks for a host of its own, which a question asked
in the login shell's environment says nothing about."""

INSTALLED_TIMEOUT_S = 60.0
"""How long the question may take: the host listing its agents, under the
presets' own ``readyTimeoutMs`` for the same work."""

DESKTOP_PORT = 4970
"""The loopback port the installed desktop app's host listens on, and the adapter's third place to look.

Spelled here as well as in ``raven.acp.mirasim.host`` (pinned equal by a test): this package may not import
the adapter, a served surface, and the probe that reads these presets has to find the host without it."""

LAUNCHER = ("bin", "mirasim")
"""Where Mirasim writes its CLI under its state directory, at every start of its host.

It puts that directory on no PATH (measured on macOS against 0.0.571 to 0.0.585: nothing in the shell rc
files, nothing under ``/etc/paths.d``, no command of its own that would), so the launcher is the sign that
Mirasim is installed when no host is up, and ``mirasim`` on the login shell's PATH is one the owner made."""

LAUNCHER_SUFFIXES = ("", ".cmd", ".exe")
"""The names the launcher may go by: bare on POSIX, and with the extension a Windows command carries."""

REMOTE_SERVERS = ("servers",)
REMOTE_START = ("current", "start.sh")
"""What a Mirasim server installed on this machine over ssh leaves under :func:`remote_root` while it is
installed: the versions delivered, and the start script of the one in use. The desktop client that
delivers it (read off its 0.0.587 bundle) runs the server from there, keeps ``run/server.pid``,
``run/server.sock`` and ``run/server.version`` while it is up, and removes those three when it stops it;
a server that is killed, or loses its machine to a reboot, leaves them behind."""

Presence = Literal["running", "installed", "absent"]


def mirasim_home(env: Mapping[str, str]) -> Path:
    """Mirasim's state directory as the adapter reads it: ``MIRASIM_HOME``, else ``~/.mirasim``."""
    named = env.get("MIRASIM_HOME", "").strip()
    return Path(named).expanduser() if named else Path.home() / ".mirasim"


def remote_root() -> Path:
    """Where Mirasim's desktop client installs and runs its server on a machine it reaches over ssh."""
    return Path.home() / ".mirasim-remote"


def remote_socket() -> Path:
    """Where the host Mirasim runs on a machine it reaches over ssh listens, as the adapter looks for it."""
    return remote_root() / "run" / "server.sock"


def remote_install() -> Path | None:
    """A sign that a Mirasim server was installed on this machine over ssh, or ``None``.

    The start script of the server in use, else a delivered version, else what a server that died left
    under ``run/`` (:data:`REMOTE_START`, :data:`REMOTE_SERVERS`); none of it is a host to reach.
    """
    root = remote_root()
    start = root.joinpath(*REMOTE_START)
    if start.is_file():
        return start
    with contextlib.suppress(OSError):
        for versions in (root.joinpath(*REMOTE_SERVERS),):
            if any(versions.iterdir()):
                return versions
    if (root / "run" / "server.sock").exists() or (root / "run" / "server.pid").is_file():
        return root
    return None


def launcher(home: Path) -> Path | None:
    """The CLI Mirasim wrote under ``home``, by whichever name this platform gives it, or ``None``."""
    base = home.joinpath(*LAUNCHER)
    for suffix in LAUNCHER_SUFFIXES:
        candidate = base.with_name(base.name + suffix)
        if candidate.is_file():
            return candidate
    return None


def token_file(env: Mapping[str, str]) -> Path:
    """The access token the desktop app's host writes for its local clients, where the adapter reads it."""
    return mirasim_home(env) / "run" / f"local-{DESKTOP_PORT}.token"


def desktop_token(env: Mapping[str, str]) -> str | None:
    """The token in that file, read as the adapter reads it: stripped, and a blank file is no token."""
    try:
        token = token_file(env).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def _listening(port: int) -> bool:
    """Whether something answers on this computer's loopback ``port``.

    The token file alone cannot say: the host removes it when it leaves and
    leaves it behind when it is killed (measured against 0.0.571: a SIGTERM
    empties ``run/``, a SIGKILL does not), so the file is evidence of an
    install, and only an open port of a host.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _answers(sock: Path) -> bool:
    """Whether a host answers at the unix socket ``sock``: a bounded connect, as :func:`_listening` for the port.

    The file alone cannot say here either: a server that is killed, or loses its machine to a reboot,
    leaves its socket on disk, and Mirasim's own start script treats that as the ordinary case -- it
    removes the socket before every start and counts the server running only with its pid alive beside
    one. So the file is evidence of an install, and only a connect that is accepted is a host.
    """
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        return False
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)
            probe.connect(str(sock))
            return True
    except OSError:
        return False


def presence(env: Mapping[str, str], *, path: str | None) -> tuple[Presence, str]:
    """Whether Mirasim is on this computer, as the adapter would find its host, and where.

    ``running`` when there is a host to reach, in the order the adapter looks: one named in
    ``RAVEN_MIRASIM_HOST``, the socket of a server Mirasim runs here over ssh when it answers, or the
    desktop app's port with its token written and something listening. ``installed`` when Mirasim is
    here but no host is up: the launcher under its home, ``mirasim`` on ``path`` (the login shell's
    PATH), a server installed over ssh, or a token file nothing answers behind. ``absent`` otherwise.
    The adapter itself runs no executable, so none is asked after: what a row needs is the host, and what
    its reader needs to hear is which of the three this is. ``env`` is the environment the row's adapter
    would be launched in.
    """
    if env.get(HOST_ENV, "").strip():
        return "running", f"the host named in {HOST_ENV}"
    sock = remote_socket()
    with contextlib.suppress(OSError):
        if sock.is_socket() and _answers(sock):
            return "running", str(sock)
    home = mirasim_home(env)
    token_written = token_file(env).is_file()
    if desktop_token(env) is not None and _listening(DESKTOP_PORT):
        return "running", f"127.0.0.1:{DESKTOP_PORT}"
    if (found := launcher(home)) is not None:
        return "installed", str(found)
    if on_path := shutil.which(MIRASIM_EXECUTABLE, path=path or None):
        return "installed", on_path
    if (remote := remote_install()) is not None:
        return "installed", str(remote)
    if token_written:
        return "installed", str(home)
    return "absent", ""


async def installed_agents(*, path: str | None) -> frozenset[str] | None:
    """The agents Mirasim has installed, asked once through the adapter; ``None`` when it could not say.

    Asked before the boot handshakes the presets nobody has connected yet. One
    whose agent Mirasim lacks refuses its session, the boot keeps no preset's
    failure, and so it was handshaken again at every start for nothing. With no
    host running at all, none is installed as far as a handshake could tell --
    each would fail on that same missing host -- so the answer is empty. A host
    that does not answer in time is ``None``, which leaves it to the handshake.

    Asked in the environment the presets' adapters are launched in, the login
    shell's, so a ``RAVEN_MIRASIM_HOST`` set there is the one both read (a row
    that sets one in its own ``env`` is answered for by its own handshake).
    ``path`` is the login shell's PATH, the one the presets' own check for
    ``mirasim`` resolves against.
    """
    from raven.agent.subagent.backends.env import login_shell_env

    argv = command_argv(resolve_subagent_command(INSTALLED_QUERY, python=_resolved_python(), subagent_dir=""))
    login = await asyncio.to_thread(login_shell_env)
    env = {**login, "PATH": path} if path else dict(login)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        logger.debug("Mirasim was not asked which agents it has: {}", exc)
        return None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), INSTALLED_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.debug("Mirasim did not say which agents it has within {:.0f}s", INSTALLED_TIMEOUT_S)
        return None
    if proc.returncode == INSTALLED_NO_HOST_EXIT:
        logger.debug("Mirasim lists no agents: {}", err.decode("utf-8", "replace").strip())
        return frozenset()
    if proc.returncode != 0:
        logger.debug("Mirasim did not say which agents it has: {}", err.decode("utf-8", "replace").strip())
        return None
    try:
        listed = json.loads(out)
    except ValueError:
        return None
    if not isinstance(listed, list) or not all(isinstance(agent, str) for agent in listed):
        return None
    return frozenset(listed)


__all__ = [
    "DESKTOP_PORT",
    "HOST_ENV",
    "HOST_ENV_KEYS",
    "INSTALLED_QUERY",
    "LAUNCHER",
    "LAUNCHER_SUFFIXES",
    "MIRASIM_AGENTS",
    "MIRASIM_EXECUTABLE",
    "MIRASIM_INSTALL_HINT",
    "MIRASIM_PRESETS",
    "MIRASIM_SHIM_PRESETS",
    "REMOTE_SERVERS",
    "REMOTE_START",
    "desktop_token",
    "installed_agents",
    "launcher",
    "mirasim_home",
    "presence",
    "remote_install",
    "remote_root",
    "remote_socket",
    "token_file",
]
