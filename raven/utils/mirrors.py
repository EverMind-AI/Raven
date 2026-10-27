"""Second hosts for downloads whose first host cannot be reached at all.

Measured from a mainland-China home line with no proxy: huggingface.co does not
answer, because its name resolves to an address that drops every packet, and
raw.githubusercontent.com times out more often than it serves. Each has a mirror
inside the country that serves the same bytes, and so do the registries and
download hosts the installers and the package runners rely on.

A mirror is only ever a second attempt. The default host is asked first, and an
answer from it, an error included, is final. Only a failure to reach it -- a
name that does not resolve, no route to it, a connection refused, reset or left
unanswered, a proxy that could not reach it either -- moves a download on
(:func:`unreachable`, which states the rule in full). A host the operator
chose, such as ``HF_ENDPOINT``, ``UV_DEFAULT_INDEX`` or an npm registry, is
never replaced. ``RAVEN_NO_MIRROR=1`` turns all of it off.

install.sh, install.ps1 and the upgrade helper cannot import this module, so
each one carries its own copy of the hosts it needs, and of the rule. Tests
hold the copies equal: the hosts by value, the rule by running every copy
against the same hosts and proxies.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from loguru import logger

OPT_OUT_ENV = "RAVEN_NO_MIRROR"
_TRUTHY = {"1", "true", "yes", "on"}

#: The default hosts asked before anything is redirected: npm's registry for
#: npx launches and raven's own npm installs, PyPI for uvx, and the Python
#: builds the installers and the upgrade helper fetch through uv. Any HTTP
#: answer from one proves it can be reached.
NPM_DEFAULT_REGISTRY = "https://registry.npmjs.org/"
PYPI_DEFAULT_INDEX = "https://pypi.org/simple/"
PYTHON_DEFAULT_DOWNLOADS = "https://releases.astral.sh/"

#: huggingface.co's mirror, for the deepdoc models.
HF_ENDPOINT = "https://hf-mirror.com"
#: Files in GitHub repositories, through jsDelivr, byte for byte.
JSDELIVR_GITHUB = "https://cdn.jsdelivr.net/gh"
#: PyPI's mirror.
PYPI_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
#: The npm registry's mirror.
NPM_REGISTRY = "https://registry.npmmirror.com"
#: The Python builds uv downloads when no interpreter on the machine will do.
PYTHON_DOWNLOADS = "https://registry.npmmirror.com/-/binary/python-build-standalone"


def opt_out(env: Mapping[str, str] | None = None) -> bool:
    """Whether the switch that turns mirrors off is set.

    Read the way raven's other opt-outs are read, so the installer and the
    `raven resources` it starts agree on what a value means.

    The environment of the launch counts as well as this process's own: a user
    who sets the switch in an agent's own environment, or in the profile of the
    machine an agent runs on, means it there. Both are read whatever the case
    of the name: Windows does not tell one from the other, and a switch set in
    another case turns the fallback off here as well as on the machine it
    travels to.
    """
    if env is not None and _truthy(_env_value(env, OPT_OUT_ENV)):
        return True
    return _truthy(_env_value(os.environ, OPT_OUT_ENV))


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether a download may fall back to a mirror at all.

    The switch is read as :func:`opt_out` reads it.
    """
    return not opt_out(env)


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    """The value ``name`` holds in ``env``, whatever its case, or None.

    Windows does not tell one from the other, and npm reads its own variables
    in either case.
    """
    wanted = name.lower()
    for key, value in env.items():
        if key.lower() == wanted:
            return value
    return None


def _truthy(value: str | None) -> bool:
    """Whether a value says yes, as raven's other opt-outs read one."""
    return (value or "").strip().lower() in _TRUTHY


#: What a socket says of a host no route leads to; Python raises these as a
#: plain OSError, where a refused or reset connection is a ConnectionError.
_NO_ROUTE = frozenset(
    getattr(errno, name) for name in ("ENETUNREACH", "EHOSTUNREACH", "ENETDOWN", "EHOSTDOWN") if hasattr(errno, name)
)
#: What a socket says when the machine itself refuses the connection (a
#: sandbox, a local firewall): a plain OSError, where a refused or reset
#: connection is a ConnectionError.
_REFUSED_LOCALLY = frozenset(getattr(errno, name) for name in ("EACCES", "EPERM") if hasattr(errno, name))


def unreachable(exc: BaseException) -> bool:
    """Whether ``exc`` says a host could not be reached, rather than that it answered.

    A connection that was refused, reset or never completed counts, as do a
    name that did not resolve, a network with no route to the host and a
    connection the machine itself refused (a sandbox or a local firewall). An
    HTTP error does not, because the host spoke, and a mirror would only
    repeat the answer or serve something the host has withdrawn. A certificate
    that fails verification does not either: whatever re-signs this host's
    traffic re-signs the mirror's too, so falling back would only hide the
    reason. A proxy that could not be used at all counts: it was never asked.

    Through a proxy the same holds for the proxy, and its answer to the request
    for a tunnel speaks for the host. A 5xx says the proxy could not reach the
    host, which counts. Any other refusal -- a 403 for a host its rules block,
    a 407 for credentials it was not given -- is final, like an error from the
    host itself: the mirror would be asked through the same proxy, and would
    only route around its rules. A SOCKS proxy that could not connect counts,
    whatever reason it gives: PowerShell has the reason only in an exception's
    text, and urllib and Windows PowerShell 5.1 cannot use a SOCKS proxy at all.

    The cause chain is walked because libraries re-raise a failed connection
    as an error of their own (huggingface_hub's LocalEntryNotFoundError, httpx's
    ConnectError over the socket's, urllib's URLError with the socket's error as
    its reason).

    Standard library only, with httpx's errors added once a caller has loaded
    httpx: no other process can have raised one, and a Raven node runs without
    httpx installed.
    """
    import socket
    import ssl

    from raven.security.tls import is_untrusted_certificate

    chain = _chain(exc)
    # Before anything else: httpx reports a refused certificate as a
    # ConnectError, which on its own would read as a connection that failed.
    if any(is_untrusted_certificate(error) for error in chain):
        return False
    # A SOCKS proxy whose support is not installed: httpx says so with an
    # ImportError and never asks the proxy, so the host behind it was not
    # reached. The copies that cannot use a SOCKS proxy at all -- urllib and
    # the Windows PowerShell -- answer the same.
    if any(isinstance(error, ImportError) and "socksio" in str(error).lower() for error in chain):
        return True
    network: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, socket.gaierror, ssl.SSLError)
    httpx = sys.modules.get("httpx")
    if httpx is not None:
        network += (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError)
    for error in chain:
        status = _tunnel_status(error)
        if status is not None:
            return status >= 500
        if isinstance(error, network):
            return True
        if isinstance(error, OSError) and error.errno in _NO_ROUTE:
            return True
        # A refusal of the machine's own: the socket was never opened. It
        # counts only where it came from the network, which is what urllib
        # wraps: a refusal of a local file is not this.
        if isinstance(error, OSError) and error.errno in _REFUSED_LOCALLY and _from_the_network(chain, error):
            return True
    return False


def _from_the_network(chain: Sequence[BaseException], error: BaseException) -> bool:
    """Whether ``error`` is the reason of a URL the network was asked for.

    urllib reports a refused connect as a URLError whose reason is the
    socket's error; a refusal of a local file is the OSError itself.
    """
    import urllib.error

    for other in chain:
        if other is error:
            continue
        if isinstance(other, urllib.error.URLError) and other.reason is error:
            return True
    return False


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and every error it was raised from or wraps, the nearest first:
    its cause and context, and urllib's reason."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        reason = getattr(current, "reason", None)
        pending += [e for e in (current.__cause__, current.__context__, reason) if isinstance(e, BaseException)]
    return chain


def _tunnel_status(exc: BaseException) -> int | None:
    """The status a proxy refused a tunnel with, when that refusal is what ``exc`` reports.

    Each library words it its own way: http.client, under urllib, raises a
    plain OSError reading ``Tunnel connection failed: 403 Forbidden``, and
    httpx a ProxyError whose message is the proxy's status line. A ProxyError
    without one is a SOCKS proxy's.
    """
    httpx = sys.modules.get("httpx")
    if httpx is not None and isinstance(exc, httpx.ProxyError):
        found = re.match(r"(\d{3}) ", str(exc))
    elif type(exc) is OSError:
        found = re.match(r"Tunnel connection failed: (\d{3})\b", str(exc))
    else:
        return None
    return int(found.group(1)) if found else None


#: How long a probe's verdict stands. A gateway starts many agents, and each
#: would otherwise pay for the same question; a network that comes back is
#: noticed within this long.
_VERDICT_TTL = 600.0
#: How long a probe waits. It runs before a package runner starts, so a probe
#: that waited out a dropped connection would hold the launch just as long.
_PROBE_TIMEOUT = 5.0
_verdicts: dict[str, tuple[float, bool]] = {}
_verdicts_lock = threading.Lock()


def _answers(url: str, deadline: float | None = None) -> bool | None:
    """Whether anything answers at ``url``. Any HTTP status counts.

    urllib rather than httpx, for the node's sake (see :func:`unreachable`).
    Only a connection that never completed counts as no answer; any other
    failure is taken as one, so a fault of ours cannot send a runner to a mirror.

    ``deadline`` is the time the preparation must not pass, and None when the
    answer is on nobody's clock. The connect is given no longer than is left
    of it, and a probe that ran out of time says nothing about the host: it
    was not reached, and it was not found to be unreachable either. None says
    that, where True says the host answered and False that it did not.
    """
    import urllib.error
    import urllib.request

    timeout = _PROBE_TIMEOUT
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        timeout = min(timeout, remaining)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception as exc:  # noqa: BLE001 - only the network may say no; see above
        if not unreachable(exc):
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return None
        logger.debug("{} could not be reached: {}", url, exc)
        return False


def _reachable(url: str, deadline: float | None = None) -> bool | None:
    """:func:`_answers`, remembered for :data:`_VERDICT_TTL`.

    A probe that ran out of time is not remembered: it says nothing about the
    host, and the next launch asks again.
    """
    with _verdicts_lock:
        known = _verdicts.get(url)
    if known is not None and time.monotonic() - known[0] < _VERDICT_TTL:
        return known[1]
    answered = _answers(url, deadline)
    if answered is None:
        return None
    with _verdicts_lock:
        _verdicts[url] = (time.monotonic(), answered)
    return answered


def _reachable_all(urls: Sequence[str], deadline: float | None = None) -> dict[str, bool | None]:
    """:func:`_reachable` for each of ``urls``, asked together, so hosts that
    drop every packet cost one wait between them rather than one each."""
    if len(urls) < 2:
        return {url: _reachable(url, deadline) for url in urls}
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        return dict(zip(urls, pool.map(lambda url: _reachable(url, deadline), urls), strict=True))


def _moved(mirror_of: Mapping[str, str], deadline: float | None = None) -> set[str]:
    """Which of the default hosts in ``mirror_of`` to leave for its mirror.

    One that cannot be reached, and only when its mirror can: a runner offline
    still starts a package it fetched before, out of a cache it keeps by the
    registry's address, and pointed at a mirror it cannot reach either the
    same launch would fail.

    ``deadline`` is the time the preparation must not pass. A host whose probe
    ran out of time was neither reached nor found unreachable, so nothing
    moves: a preparation past its budget leaves the launch as it was
    configured.
    """
    answers = _reachable_all(list(mirror_of), deadline)
    if any(answered is None for answered in answers.values()):
        return set()
    down = [url for url, answered in answers.items() if not answered]
    if not down:
        return set()
    up = _reachable_all([mirror_of[url] for url in down], deadline)
    if any(answered is None for answered in up.values()):
        return set()
    return {url for url in down if up[mirror_of[url]] is True}


def _host(url: str) -> str:
    return urlsplit(url).hostname or url


def _program(word: str) -> str:
    """``uvx`` for ``uvx``, ``/usr/bin/uvx`` and ``C:\\...\\uvx.exe`` alike."""
    name = re.split(r"[\\/]", word)[-1].lower()
    stem, dot, suffix = name.rpartition(".")
    return stem if dot and suffix in {"cmd", "exe", "bat", "ps1"} else name


def _home(env: Mapping[str, str]) -> Path:
    home = env.get("USERPROFILE") if os.name == "nt" else env.get("HOME")
    return Path(home) if home else Path.home()


def _set(env: Mapping[str, str], *names: str) -> bool:
    """Whether any of ``names`` holds a value in ``env``, case-insensitively.

    npm reads its variables in either case, and Windows does not tell them apart.
    """
    wanted = {name.lower() for name in names}
    return any(key.lower() in wanted and value.strip() for key, value in env.items())


def _user_npmrc(env: Mapping[str, str]) -> Path:
    return Path(env["NPM_CONFIG_USERCONFIG"]) if env.get("NPM_CONFIG_USERCONFIG") else _home(env) / ".npmrc"


def _npm_registry_chosen(env: Mapping[str, str]) -> bool:
    """Whether the environment or the user's npmrc names a default registry,
    which settles it without asking anything. A scoped ``@corp:registry`` is
    not a default."""
    if _set(env, "npm_config_registry"):
        return True
    try:
        text = _user_npmrc(env).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r"^\s*registry\s*=\s*\S", text, re.MULTILINE) is not None


#: How long npm gets to say which registry it reads; starting npm is most of it.
_NPM_TIMEOUT = 15.0


def _npm_registry(env: Mapping[str, str], cwd: str | None) -> str | None:
    """The registry npm would read, run in ``cwd`` with ``env``, or None when it cannot say.

    npm itself is asked because the variable this module sets outranks every
    file npm reads -- the project's .npmrc, the global one, npm's own -- so
    anything less would replace a registry the user named in one of them. A
    directory not made yet is asked about from the nearest one that is:
    nothing below it can name a registry before it exists.
    """
    return (_npm_config(env, cwd, ("registry",)) or {}).get("registry")


def _npm_config(
    env: Mapping[str, str], cwd: str | None, names: Sequence[str], deadline: float | None = None
) -> dict[str, str] | None:
    """What npm, run in ``cwd`` with ``env``, holds for each of ``names``, or
    None when it cannot say; see :func:`_npm_registry`.

    ``deadline`` is the time the preparation must not pass: npm gets no longer
    than is left of it, and starting npm is most of what asking it costs.
    """
    npm = shutil.which("npm", path=env.get("PATH") or os.environ.get("PATH", os.defpath))
    if npm is None:
        return None
    timeout = _NPM_TIMEOUT
    if deadline is not None:
        timeout = max(0.0, min(timeout, deadline - time.monotonic()))
    where = Path(cwd) if cwd else None
    while where is not None and not where.is_dir():
        where = where.parent if where.parent != where else None
    try:
        done = subprocess.run(
            [npm, "config", "get", *names],
            cwd=where,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    text = done.stdout.decode("utf-8", "replace").strip()
    if len(names) == 1:
        # Asked for one setting, npm prints its value alone; for several,
        # each as ``name=value``.
        return {names[0]: text} if text else None
    values = {}
    for line in text.splitlines():
        name, equals, value = line.partition("=")
        if equals and name.strip() in names and value.strip():
            values[name.strip()] = value.strip()
    return values


def npm_registry_env(env: Mapping[str, str], cwd: str | None = None) -> dict[str, str]:
    """The variable that points npm, run in ``cwd`` with ``env``, at its
    mirror; empty unless the registry npm would use is its default, which
    cannot be reached while the mirror can.

    For an npm raven runs in a directory of its own, whose lockfile is raven's,
    and which starts nothing that outlives it; :func:`package_launch` says why
    an npx launch is never given one. May block for a probe and for npm itself,
    and never raises: a fault while deciding leaves npm as it is.
    """
    if not enabled(env):
        return {}
    try:
        return _npm_registry_env(env, cwd)
    except Exception as exc:  # noqa: BLE001 - the mirror is a way around an outage, never a gate
        logger.opt(exception=exc).warning("could not decide on an npm mirror; npm starts as configured")
        return {}


def _npm_registry_env(env: Mapping[str, str], cwd: str | None) -> dict[str, str]:
    if _npm_registry_chosen(env) or not _moved({NPM_DEFAULT_REGISTRY: NPM_REGISTRY}):
        return {}
    configured = _npm_registry(env, cwd)
    if configured is None or configured.rstrip("/") != NPM_DEFAULT_REGISTRY.rstrip("/"):
        return {}
    logger.info(
        "{} could not be reached; npm fetches from {} instead ({}=1 turns this off)",
        _host(NPM_DEFAULT_REGISTRY),
        _host(NPM_REGISTRY),
        OPT_OUT_ENV,
    )
    return {"npm_config_registry": NPM_REGISTRY}


#: How long npm may take to install what an npx launch asks for. The launch
#: waits for it, as it would have waited for npx to fetch the same packages.
_NPX_INSTALL_TIMEOUT = 600.0

#: A spec npx looks up on the registry: a package name, scoped or not, and a
#: version, a range or a tag. Paths, URLs, git and aliases are not fetched from
#: it, or not from it alone, and keep the launch as configured.
_REGISTRY_SPEC = re.compile(
    r"((?:@[A-Za-z0-9][A-Za-z0-9._~-]*/)?[A-Za-z0-9][A-Za-z0-9._~-]*)(?:@([A-Za-z0-9.^~*<>=|+_-]+))?"
)
#: One version and nothing else; npm reads a leading ``v`` or ``=`` as part of it.
_EXACT_VERSION = re.compile(r"[v=]?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
#: What npx runs when told which packages to fetch: one of their bins, by name.
_BIN_NAME = re.compile(r"[A-Za-z0-9@._+-]+")
#: A script's first line as npm's Windows shims read it (cmd-shim), nothing
#: else on Windows reading it at all: the variables ``env`` would set, the
#: program, and the arguments it takes before the script.
_SHEBANG = re.compile(r"#!\s*(?:/usr/bin/env\s+(?:-S\s+)?((?:[^ \t=]+=[^ \t=]+\s+)*))?([^ \t]+)(.*)")


class _NpxRequest(NamedTuple):
    """What an npx command line asks for."""

    #: Each package spec, as written.
    specs: list[str]
    #: The same, as (name, version range or tag), the second empty for none.
    packages: list[tuple[str, str]]
    #: The bin to run; None for the one package's own.
    command: str | None
    args: list[str]


def _npx_request(words: Sequence[str]) -> _NpxRequest | None:
    """``npx [-y] [-p SPEC]... [--] FIRST ARGS...``, read the way npx reads it,
    or None for any other shape.

    Without ``-p``, FIRST is the package and its own bin is run; with one,
    FIRST is the bin to run from the packages named. Any other option is a
    choice of how npx works -- ``--registry``, ``--no``, ``-c``, a cache or a
    config of its own -- which a launch that does without npx would not keep,
    so the launch stays as configured.
    """
    specs: list[str] = []
    rest = list(words)
    while rest and rest[0].startswith("-"):
        word = rest.pop(0)
        if word == "--":
            break
        if word in ("-y", "--yes"):
            continue
        if word in ("-p", "--package") and rest:
            specs.append(rest.pop(0))
        elif word.startswith(("-p=", "--package=")):
            specs.append(word.partition("=")[2])
        else:
            return None
    if not rest:
        return None
    first, args = rest[0], rest[1:]
    command = first if specs else None
    if command is not None and not _BIN_NAME.fullmatch(command):
        return None
    specs = specs or [first]
    packages = []
    for spec in specs:
        found = _REGISTRY_SPEC.fullmatch(spec)
        if found is None:
            return None
        packages.append((found.group(1), found.group(2) or ""))
    if len({name for name, _ in packages}) != len(packages):
        return None
    return _NpxRequest(specs, packages, command, args)


def _read_json(path: Path) -> dict:
    """The object in the JSON file at ``path``; empty when there is none to read."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _bins(manifest: Mapping[str, object], name: str) -> dict[str, str]:
    """A package.json's bins by name; one given as a bare path is named after the package."""
    bins = manifest.get("bin")
    if isinstance(bins, str):
        return {name.rpartition("/")[2]: bins}
    if isinstance(bins, dict):
        return {key: value for key, value in bins.items() if isinstance(key, str) and isinstance(value, str)}
    return {}


def _default_bin(name: str, bins: Mapping[str, str]) -> str | None:
    """The bin npx runs from a package named without ``-p``: its only one, or the one named after it."""
    if len(set(bins.values())) == 1:
        return next(iter(bins))
    unscoped = name.rpartition("/")[2]
    return unscoped if unscoped in bins else None


def _project_roots(where: Path) -> Iterator[Path]:
    """``where``, then each folder above it that npm could take for the
    project's root, or a workspace's: one holding a package.json or a
    node_modules. npm takes the nearest, or a workspace root above it, and
    ``where`` itself when there is none; asking every one of them finds more
    than npm would, and leaves one more launch as configured, not one fewer."""
    yield where
    for folder in where.parents:
        if (folder / "package.json").is_file() or (folder / "node_modules").is_dir():
            yield folder


def _npx_finds_locally(request: _NpxRequest, where: Path) -> bool:
    """Whether npx would start this from what the project already has, without
    asking a registry: a bin by that name -- the project's own, or one in a
    node_modules/.bin above ``where`` -- or a package it names, installed in
    the project. Those stay npx's to start."""
    first = request.specs[0] if request.command is None else None
    for folder in _project_roots(where):
        modules = folder / "node_modules"
        if any((modules / name / "package.json").is_file() for name, _ in request.packages):
            return True
        if first is not None:
            if (modules / ".bin" / first).is_file():
                return True
            manifest = _read_json(folder / "package.json")
            if first in _bins(manifest, str(manifest.get("name", ""))):
                return True
    return False


def _project_npmrc_names_a_registry(where: Path, env: Mapping[str, str]) -> bool:
    """Whether the project's .npmrc names a registry, scoped or not. npx reads
    it; the install it is replaced by runs in a directory of Raven's, which
    reads only the user's npmrc and npm's own, so a registry the project names
    would be passed over there. The user's own npmrc is no project's, as npm
    takes it."""
    user = os.path.normcase(os.path.abspath(_user_npmrc(env)))
    for folder in _project_roots(where):
        npmrc = folder / ".npmrc"
        if os.path.normcase(os.path.abspath(npmrc)) == user:
            continue
        try:
            text = npmrc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"^\s*(?:@[^\s:=]+:)?registry\s*=", text, re.MULTILINE):
            return True
    return False


def _global_bin(prefix: str) -> Path:
    """Where npm links the bins of what it installs globally, under its ``prefix``."""
    return Path(prefix) if os.name == "nt" else Path(prefix) / "bin"


def _npx_launch(
    argv: list[str],
    env: Mapping[str, str],
    cwd: str | None,
    deadline: float | None,
    cache_root: Path | None,
) -> list[str]:
    request = _npx_request(argv[1:])
    if request is None or _npm_registry_chosen(env):
        return argv
    where = Path(cwd) if cwd else Path.cwd()
    if _npx_finds_locally(request, where):
        return argv
    if deadline is not None and time.monotonic() >= deadline:
        return argv
    if not _moved({NPM_DEFAULT_REGISTRY: NPM_REGISTRY}, deadline):
        return argv
    if deadline is not None and time.monotonic() >= deadline:
        return argv
    if _project_npmrc_names_a_registry(where, env):
        return argv
    config = _npm_config(env, str(where), ("registry", "prefix"), deadline) or {}
    if config.get("registry", "").rstrip("/") != NPM_DEFAULT_REGISTRY.rstrip("/"):
        return argv
    if request.command is None and "prefix" in config and (_global_bin(config["prefix"]) / request.specs[0]).is_file():
        return argv
    prefix = _npx_install(request, env, deadline, cache_root)
    started = _npx_started(prefix, request, env) if prefix is not None else None
    if started is None:
        logger.warning(
            "{} could not be reached, and {} could not be installed from {} and started; npx starts as configured",
            _host(NPM_DEFAULT_REGISTRY),
            " ".join(request.specs),
            _host(NPM_REGISTRY),
        )
        return argv
    logger.info(
        "{} could not be reached; {} is installed from {} into {} and starts from there ({}=1 turns this off)",
        _host(NPM_DEFAULT_REGISTRY),
        " ".join(request.specs),
        _host(NPM_REGISTRY),
        prefix,
        OPT_OUT_ENV,
    )
    return started


def _npx_install(
    request: _NpxRequest, env: Mapping[str, str], deadline: float | None, cache_root: Path | None
) -> Path | None:
    """Raven's own install of the packages ``request`` names, from the mirror:
    its directory, or None when there is none to start.

    One directory per set of specs, as npx keeps one, under the cache the
    caller named -- Raven's own cache, or the node's own directory -- so two
    installs of the same specs on two machines do not write over each other. An
    exact version is installed once. Anything else -- a range, a tag, a bare
    name -- is brought up to date at each launch (``npm update``), as npx asks
    the registry each time what it means now; when that fails, the install
    made before still starts. Launches of the same specs take turns.

    An install that was killed leaves a directory that looks installed and is
    not, so a marker file says whether the last run of npm finished: an
    install without one is done again. The lock is held by the process that
    installs, and a stale one -- left by a Raven that died -- is broken, so an
    npm this Raven started is never run beside another one.
    """
    key = hashlib.sha256("\n".join(sorted(request.specs)).encode("utf-8")).hexdigest()[:16]
    prefix = (cache_root if cache_root is not None else _default_cache_root()) / key
    manifest = {"private": True, "dependencies": {name: version or "latest" for name, version in request.packages}}
    # Another launch of the same specs may be installing here. The wait is
    # bounded by the caller's own budget: past it the launch starts as it was
    # configured rather than hold the answer back.
    lock_timeout = _NPX_INSTALL_TIMEOUT + 60
    if deadline is not None:
        lock_timeout = max(0.0, min(lock_timeout, deadline - time.monotonic()))
    with _exclusive(prefix / ".lock", lock_timeout):
        written = _read_json(prefix / "package.json")
        if written != manifest:
            (prefix / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        installed = (
            written == manifest
            and (prefix / ".installed").is_file()
            and all((prefix / "node_modules" / name / "package.json").is_file() for name, _ in request.packages)
        )
        if installed and all(_EXACT_VERSION.fullmatch(version) for _, version in request.packages):
            return prefix
        # An npm of a Raven that died is still installing here: this lock is
        # free, that npm is not. Wait for it rather than run a second one over
        # the same directory.
        _await_orphan(prefix, deadline)
        installed = (
            written == manifest
            and (prefix / ".installed").is_file()
            and all((prefix / "node_modules" / name / "package.json").is_file() for name, _ in request.packages)
        )
        if installed and all(_EXACT_VERSION.fullmatch(version) for _, version in request.packages):
            return prefix
        if deadline is not None and time.monotonic() >= deadline:
            # The budget is spent: the launch starts as it was configured
            # rather than hold the answer back, and the install made before
            # still starts.
            return prefix if installed else None
        if _npm_install(prefix, "update" if installed else "install", env, deadline):
            (prefix / ".installed").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            return prefix
        return prefix if installed else None


def _await_orphan(prefix: Path, deadline: float | None) -> None:
    """Wait for an npm this Raven started before it died, if it is still running.

    It writes the same directory this install would, so a second npm over it
    would write the same files at once. The pid is in the directory beside the
    lock, and a pid that is gone is removed.
    """
    from raven.utils.pid import pid_alive

    pid_file = prefix / "install.pid"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if not pid_alive(pid):
        with contextlib.suppress(OSError):
            pid_file.unlink()
        return
    logger.info("waiting for an npm still installing in {}", prefix)
    while pid_alive(pid):
        if deadline is not None and time.monotonic() >= deadline:
            return
        time.sleep(0.2)
    with contextlib.suppress(OSError):
        pid_file.unlink()


def _default_cache_root() -> Path:
    """Where a launch's own install goes when the caller names no directory."""
    from raven.home import raven_home

    return raven_home() / "cache" / "npx"


def _npm_install(prefix: Path, verb: str, env: Mapping[str, str], deadline: float | None) -> bool:
    """Run ``npm <verb>`` in ``prefix`` against the mirror; whether it finished.

    The registry goes on the command line of this npm alone, which ends with
    the install. Run in a process group of its own, so a deadline ends npm's
    children too: on Windows the npm found is a .cmd, and the node it starts
    would outlive cmd.exe.
    """
    npm = shutil.which("npm", path=env.get("PATH") or os.environ.get("PATH", os.defpath))
    if npm is None:
        return False
    timeout = _NPX_INSTALL_TIMEOUT
    if deadline is not None:
        timeout = min(timeout, max(0.0, deadline - time.monotonic()))
    try:
        proc = subprocess.Popen(
            [npm, verb, "--registry", NPM_REGISTRY, "--no-audit", "--no-fund"],
            cwd=prefix,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError as exc:
        logger.warning("npm could not be started in {}: {}", prefix, exc)
        return False
    pid_file = prefix / "install.pid"
    with contextlib.suppress(OSError):
        pid_file.write_text(str(proc.pid), encoding="utf-8")
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _end_group(proc)
        proc.communicate()
        logger.warning("npm {} in {} did not finish in {:.0f}s", verb, prefix, timeout)
        return False
    finally:
        with contextlib.suppress(OSError):
            pid_file.unlink()
    if proc.returncode != 0:
        said = out.decode("utf-8", "replace").strip()[-800:]
        logger.warning("npm {} in {} failed (exit {}): {}", verb, prefix, proc.returncode, said)
        return False
    return True


def _end_group(proc: subprocess.Popen) -> None:
    """End ``proc`` and everything it started."""
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    [taskkill, "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30, check=False
                )
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def _npx_started(prefix: Path, request: _NpxRequest, env: Mapping[str, str]) -> list[str] | None:
    """The command line that starts, from ``prefix``, the bin npx would have run."""
    for name, _version in request.packages:
        package = prefix / "node_modules" / name
        manifest = _read_json(package / "package.json")
        bins = _bins(manifest, name)
        command = request.command if request.command is not None else _default_bin(name, bins)
        if command is None or command not in bins:
            continue
        target = (package / bins[command]).resolve()
        if package.resolve() not in target.parents or not target.is_file():
            return None
        return _script_argv(target, request.args, env)
    return None


def _script_argv(target: Path, args: Sequence[str], env: Mapping[str, str]) -> list[str] | None:
    """How to start ``target``, a bin's file, the way the link npm makes to it starts it."""
    if os.name != "nt":
        # npm marks a bin's file executable, and its first line names what runs it.
        return [str(target), *args] if os.access(target, os.X_OK) else None
    try:
        with target.open("rb") as handle:
            first = handle.readline(1024).decode("utf-8", "replace").strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return [str(target), *args] if target.suffix.lower() == ".exe" else None
    options = _node_options(first)
    node = shutil.which("node", path=env.get("PATH") or os.environ.get("PATH", os.defpath))
    if options is None or node is None:
        return None
    return [node, *options, str(target), *args]


def _node_options(first_line: str) -> list[str] | None:
    """What node is started with, before the script, for a script whose first
    line is ``first_line``, read as npm's Windows shims read it; None for a
    script node does not run, or one that needs variables set."""
    found = _SHEBANG.fullmatch(first_line.strip())
    if found is None:
        return None
    variables, program, options = found.groups()
    return None if variables or _program(program) != "node" else options.split()


@contextlib.contextmanager
def _exclusive(path: Path, timeout: float) -> Iterator[None]:
    """Hold ``path`` locked for the block, against every process and thread
    doing the same; TimeoutError when another holds it past ``timeout``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{path} was held for longer than {timeout:.0f}s")
            time.sleep(0.2)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                _unlock(fd)
    finally:
        os.close(fd)


try:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

except ImportError:  # pragma: no cover - the other platform
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


#: Where uv looks for the machine's uv.toml after the XDG directories.
_ETC = "/etc"


def _uv_configs(env: Mapping[str, str]) -> list[Path]:
    """The uv.toml files uv reads for a user who named none: theirs, then the machine's."""
    if env.get("UV_CONFIG_FILE"):
        return [Path(env["UV_CONFIG_FILE"])]
    if os.name == "nt":
        roots = [env.get("APPDATA"), env.get("PROGRAMDATA")]
    else:
        system = (env.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(os.pathsep)
        roots = [env.get("XDG_CONFIG_HOME") or str(_home(env) / ".config"), *system, _ETC]
    return [Path(root) / "uv" / "uv.toml" for root in roots if root]


def _uv_settings(env: Mapping[str, str]) -> list[dict]:
    """Each of :func:`_uv_configs` that parses, on its own: one file's list of
    indexes does not hide another's default.

    Read as utf-8 with a byte-order mark if it has one: a file written by
    Windows' editor carries a mark uv itself reads past, and a reader that
    does not would call the file broken and take the user's index for none.
    """
    import tomllib

    settings: list[dict] = []
    for path in _uv_configs(env):
        try:
            settings.append(tomllib.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError):
            continue
    return settings


def _uv_index_chosen(env: Mapping[str, str]) -> bool:
    if _set(env, "UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_INDEX"):
        return True
    for config in _uv_settings(env):
        indexes = config.get("index") if isinstance(config.get("index"), list) else []
        if config.get("index-url") or any(isinstance(i, dict) and i.get("default") for i in indexes):
            return True
    return False


def _names(args: Sequence[str], flags: Sequence[str]) -> bool:
    """Whether any of ``flags`` is among ``args``, as a word of its own or with ``=value``."""
    return any(arg in flags or arg.startswith(tuple(f"{flag}=" for flag in flags)) for arg in args)


def _uv_index_named(args: Sequence[str]) -> bool:
    """Whether ``args`` choose uvx's index already; a second --default-index is one uv refuses."""
    return _names(args, ("--default-index", "--index-url", "--no-index")) or any(
        arg.startswith("-i") and not arg.startswith("--") for arg in args
    )


def package_launch(
    argv: Sequence[str],
    env: Mapping[str, str],
    cwd: str | None = None,
    *,
    local: bool = True,
    deadline: float | None = None,
    cache_root: Path | None = None,
) -> list[str]:
    """``argv``, or a command line that fetches the same package from a
    mirror, where a ``uvx`` or ``npx`` launch cannot reach its default
    registry and the mirror answers.

    uvx is given PyPI's mirror on its own command line: uv keeps a flag to
    itself, so the choice ends with the fetch, while a variable would stay in
    the tool's environment and outrank the index a project names for itself.

    npx cannot be given one at all: npm hands its registry to everything npx
    starts, however the registry was given -- a variable, a flag, an npmrc
    (measured with npm 11.12) -- and there it would replace the registry a
    project names in its own .npmrc. So npx does not run. The packages it would
    fetch are installed from the mirror into a directory of Raven's own
    (:func:`_npx_install`), and the bin it would run starts from there, with
    the launch's own environment: the mirror ends with the install. A package
    npx would find without asking a registry -- in the project, or among npm's
    global bins -- stays npx's to start. Only a ``local`` launch gets this,
    the install being made on this machine; one that runs in a sandbox gets
    only what its command line can carry.

    ``uv run`` and ``npm install`` resolve the project they run in, and pnpm and
    bun read registries from files of their own; those start as configured
    too. ``env`` is the environment the launch gets, where the user's own
    choice of index or registry would be found, and ``cwd`` the directory it
    starts in, this process's own when None. A choice already made -- in the
    environment, a configuration file, or on the command line -- stands, and
    ``RAVEN_NO_MIRROR`` in that environment leaves every launch alone. May block for a probe and
    for npm; see :func:`package_launch_async`. Never raises: a fault while
    deciding leaves the launch as it was configured.

    ``deadline`` is the time the preparation must not pass, as
    :func:`time.monotonic` counts it: past it the launch starts as it was
    configured rather than hold the answer back. :func:`package_launch_async`
    turns a budget in seconds into one, and takes it before the work is
    queued, so waiting for a worker of the pool counts against it.
    ``cache_root`` is where the install of an npx launch goes; a node keeps
    its own under its own directory rather than in the home of the user it
    runs as.
    """
    if not argv or not enabled(env):
        return list(argv)
    try:
        return _package_launch(list(argv), env, cwd, local, deadline, cache_root)
    except Exception as exc:  # noqa: BLE001 - the mirror is a way around an outage, never a gate
        logger.opt(exception=exc).warning("could not decide on a mirror for {!r}; it starts as configured", argv[0])
        return list(argv)


def _package_launch(
    argv: list[str],
    env: Mapping[str, str],
    cwd: str | None,
    local: bool,
    deadline: float | None,
    cache_root: Path | None,
) -> list[str]:
    program = _program(argv[0])
    if program == "uvx":
        return _uvx_launch(argv, env, deadline)
    if program == "npx" and local:
        return _npx_launch(argv, env, cwd, deadline, cache_root)
    return argv


def _uvx_launch(argv: list[str], env: Mapping[str, str], deadline: float | None) -> list[str]:
    # A uv.toml named on the command line may choose the index, and the flag
    # added here would outrank it.
    if _names(argv[1:], ("--config-file",)) or _uv_index_named(argv[1:]) or _uv_index_chosen(env):
        return argv
    if deadline is not None and time.monotonic() >= deadline:
        return argv
    if not _moved({PYPI_DEFAULT_INDEX: PYPI_INDEX}, deadline):
        return argv
    logger.info(
        "{} could not be reached; uvx fetches from {} instead ({}=1 turns this off)",
        _host(PYPI_DEFAULT_INDEX),
        _host(PYPI_INDEX),
        OPT_OUT_ENV,
    )
    return [argv[0], "--default-index", PYPI_INDEX, *argv[1:]]


async def package_launch_async(
    argv: Sequence[str],
    env: Mapping[str, str],
    cwd: str | None = None,
    *,
    local: bool = True,
    budget: float | None = None,
    cache_root: Path | None = None,
) -> list[str]:
    """``argv``, or a command line that fetches the same package from a mirror.

    The preparation runs off the event loop, since its probe and npm block.

    ``budget`` is how long the preparation may take, in seconds. The clock
    starts here, before the work is queued, so the wait for a worker of the
    pool counts against it: a daemon admits more launches at once than its
    pool has workers, and a launch that waited for one has less of its budget
    left. A preparation past the budget is abandoned -- the launch starts as
    it was configured, and the answer comes in time.
    """
    deadline = time.monotonic() + budget if budget is not None else None
    work = asyncio.to_thread(package_launch, argv, env, cwd, local=local, deadline=deadline, cache_root=cache_root)
    if deadline is None:
        return await work
    try:
        return await asyncio.wait_for(work, timeout=max(0.0, deadline - time.monotonic()))
    except TimeoutError:
        logger.info("the preparation took longer than its budget; the launch starts as it was configured")
        return list(argv)


__all__ = [
    "HF_ENDPOINT",
    "JSDELIVR_GITHUB",
    "NPM_DEFAULT_REGISTRY",
    "NPM_REGISTRY",
    "OPT_OUT_ENV",
    "PYPI_DEFAULT_INDEX",
    "PYPI_INDEX",
    "PYTHON_DEFAULT_DOWNLOADS",
    "PYTHON_DOWNLOADS",
    "enabled",
    "npm_registry_env",
    "opt_out",
    "package_launch",
    "package_launch_async",
    "unreachable",
]
