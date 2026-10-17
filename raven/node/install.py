"""Put this Raven's node on a registered machine.

What lands there, all under ``<node_dir>/<install name>/`` (``node_dir`` from the
machine's registry row, ``~/.raven-node`` when it names none): a Python 3.12
virtualenv, the packages in :data:`raven.node.protocol.REQUIREMENTS` pinned to
this Raven's own versions, this Raven's own code (:mod:`raven.node.bundle`), and
beside it a record of the version the directory is named by: the code runs from
``lib/``, not as an installed distribution, so without it the node would read
no version of its own and name its socket and lock ``0.0.0+unknown-<digest>``.
Nothing else on the machine changes: no system package, no shell rc file, no
environment it already had, no file outside ``node_dir``, and a ``node_dir``
that was there before keeps its mode (only one this makes is made owner-only).

Everything Python goes through ``uv``, as it does in this repository: the
virtualenv is ``uv venv`` of the machine's own Python 3.12 when it has one,
else of a Python uv keeps inside ``node_dir``; the packages are ``uv pip
install``, from the index pip is configured with on the machine when it is
(``PIP_INDEX_URL``, or ``index-url`` in pip's config files, which uv does
not read on its own), else from PyPI. That index reaches uv on the step's
stdin, as ``UV_DEFAULT_INDEX``, and never on a command line: pip takes a
sign-in inside the URL, and a command line is readable by every account on
the machine and, as this computer's ssh argv, by every account here. A
machine without uv gets one under
``node_dir/.uv`` first: uv's installer for one pinned version
(:data:`UV_VERSION`), fetched with the machine's curl or wget and run as an
unmanaged install, which writes no receipt under the home directory and
changes no PATH; with neither uv nor a way to fetch it, the install stops
and says so. So does a machine whose uv is older than
:data:`UV_INDEX_SINCE` when pip there is configured with an index: such a
uv ignores ``UV_DEFAULT_INDEX`` and would install from PyPI behind a plan
that names the mirror. A uv an earlier install fetched under ``node_dir/.uv``
is used in its place when it is there, so the next install fetches nothing.

Each step is one ssh call through :func:`raven.acp_client.remote.run`; a step
that fails removes the half-made node directory, so the next attempt starts
clean and :func:`plan` never mistakes a broken node for an installed one.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from raven.acp_client import remote
from raven.node import bundle, protocol
from raven.node.client import node_dir as registered_node_dir

#: Roughly what a node takes, for the free-space check: Raven's code with
#: pillow, loguru and ripgrep-bin (measured 2026-10-10), a Python 3.12 when
#: the machine has none of its own, and uv when the machine has none.
_NODE_KB = 60 * 1024
_PYTHON_KB = 80 * 1024
_UV_KB = 25 * 1024

#: uv's own installer, as a person is told to run it when the machine has no uv and no way to fetch one.
UV_INSTALLER = "https://astral.sh/uv/install.sh"
#: The uv an install fetches when the machine has none: one version, as the
#: packages are one version each, so two installs a month apart put the same
#: uv on two machines. The installer for a version is served under its number.
UV_VERSION = "0.13.0"
UV_PINNED_INSTALLER = f"https://astral.sh/uv/{UV_VERSION}/install.sh"
#: The first uv that reads the package index from ``UV_DEFAULT_INDEX`` (with
#: ``--default-index``, both added in 0.4.23 per uv's changelog). An older one
#: ignores the variable and installs from PyPI, so with an index configured a
#: machine's uv older than this, or one that does not say its version, is
#: passed over for :data:`UV_VERSION`.
UV_INDEX_SINCE = (0, 4, 23)

_PROBE_TIMEOUT_S = 60.0
_STEP_TIMEOUT_S = 900.0


class InstallError(RuntimeError):
    """An install that could not go ahead or did not finish. Safe to show: no address."""


@dataclass(frozen=True)
class Plan:
    """What an install would do on one machine, found by one look at it."""

    target: remote.Machine
    node_dir: str
    install_name: str
    present: bool
    python: str
    uv: str
    free_kb: int | None
    fetch: str = ""
    """The machine's curl or wget, with which uv can be fetched when the machine has none."""
    index: str = ""
    """The package index pip is configured with on the machine, or ``""`` for PyPI: handed to uv on a step's stdin,
    never on a command line, and shown only without its sign-in (:func:`_shown_index`)."""
    uv_version: str = ""
    """What the machine's uv says it is (``uv --version``), or ``""`` when it says nothing readable."""

    @property
    def uv_too_old(self) -> bool:
        """Whether the machine's uv would ignore the configured index: older than :data:`UV_INDEX_SINCE`, or silent.

        Only with an index configured: without one, uv's default is what the
        plan says, and an old uv installs from it as well as a new one.
        """
        return bool(self.uv and self.index) and _version_of_uv(self.uv_version) < UV_INDEX_SINCE

    @property
    def uv_from(self) -> str:
        """``"machine"`` (its own uv), ``"fetched"`` (one put under node_dir first), or ``""`` (no way to one)."""
        if self.uv and not self.uv_too_old:
            return "machine"
        return "fetched" if self.fetch else ""

    @property
    def how(self) -> str:
        """``"python"`` (uv over the machine's own 3.12), ``"uv"`` (over one uv keeps), or ``""`` (no uv to be had)."""
        if not self.uv_from:
            return ""
        return "python" if self.python else "uv"

    @property
    def needed_kb(self) -> int:
        return _NODE_KB + (_PYTHON_KB if self.how == "uv" else 0) + (_UV_KB if self.uv_from == "fetched" else 0)

    def describe(self) -> str:
        """The install as a person is asked to approve it."""
        where = f"{self.node_dir}/{self.install_name}"
        python = (
            f"a virtualenv of the machine's own {self.python}"
            if self.how == "python"
            else "a Python 3.12 made by uv, kept beside it"
        )
        why = (
            f"the machine has {_uv_said(self)}, and only uv {_SINCE} or later installs from the index pip is "
            "configured with"
            if self.uv_too_old
            else "the machine has none"
        )
        uv = (
            f" uv {UV_VERSION} itself first, under {self.node_dir}/.uv ({why}), fetched with its own {self.fetch};"
            if self.uv_from == "fetched"
            else ""
        )
        if self.index:
            # The host and the path only: pip takes a sign-in inside the URL
            # (``https://user:password@host/simple``), and this line is
            # printed before the confirmation and under ``--yes``.
            shown = _shown_index(self.index)
            how = "the index pip is configured with there" + (
                ", shown without its sign-in" if shown != self.index else ""
            )
            index = f"{shown} ({how})"
        else:
            index = "PyPI (pip is configured with no other index there)"
        space = f"; {self.free_kb // (1024 * 1024)} GB free there" if self.free_kb is not None else ""
        return (
            f"Install the Raven node {self.install_name} on machine {self.target.label} into {where}:{uv} {python}, "
            f"{' '.join(bundle.pinned((*protocol.REQUIREMENTS, protocol.SEARCH_REQUIREMENT)))} from {index}, "
            f"and this Raven's code, all through uv. About {self.needed_kb // 1024} MB{space}. "
            "Nothing else on the machine changes."
        )


@dataclass(frozen=True)
class Installed:
    """A node that answered with this Raven's digest after being put in place."""

    node_dir: str
    install_name: str
    size_kb: int | None
    rg: bool = True
    """Whether ``ripgrep-bin`` went in; without it the node searches with Raven's Python fallback."""


Runner = Callable[[remote.Machine, str], tuple[int, str]]
Pusher = Callable[[remote.Machine, str, bytes], tuple[int, str]]


def _run(target: remote.Machine, command: str) -> tuple[int, str]:
    return remote.run(target, command, timeout=_STEP_TIMEOUT_S)


def _push(target: remote.Machine, command: str, data: bytes) -> tuple[int, str]:
    return remote.push(target, command, data, timeout=_STEP_TIMEOUT_S)


def _vars(node_dir: str, install_name: str) -> str:
    """The shell assignments every step starts with; ``~`` is left for the machine to expand."""
    return f'dir={remote.shell_path(node_dir)}; id={shlex.quote(install_name)}; node="$dir/$id"; '


#: The steps that run ``uv pip install``: the ones that read the index, when there is one, off their stdin.
_INDEX_STEPS = frozenset({"packages", "search"})

#: The index pip would install from on the machine, read the way pip reads it
#: and said as ``INDEX=<url>``: ``PIP_INDEX_URL`` first (the shell checks it),
#: else ``index-url`` in pip's config files, the user's over the system's
#: and ``[install]`` over ``[global]``. One line of Python for the machine's
#: own interpreter, with no single quote in it, since the whole line travels
#: inside single quotes on the ssh command.
_PIP_INDEX = (
    "import configparser, os; c = configparser.ConfigParser(interpolation=None); "
    'h = os.path.expanduser("~"); x = os.environ.get("XDG_CONFIG_HOME") or os.path.join(h, ".config"); '
    'c.read(["/etc/pip.conf", "/etc/xdg/pip/pip.conf", os.path.join(h, ".pip", "pip.conf"), '
    'os.path.join(x, "pip", "pip.conf")]); '
    'v = next((c.get(s, "index-url") for s in ("install", "global") if c.has_option(s, "index-url")), ""); '
    'print("INDEX=" + v.strip().splitlines()[0]) if v.strip() else None'
)


def probe_script(node_dir: str, install_name: str, digest: str) -> str:
    """One look at the machine: is this node there, which Python, uv, fetcher and package index it has, how much room.

    uv is looked for on the PATH, in the two places its installer puts it
    that a non-login PATH misses, and under ``node_dir/.uv``, where an
    earlier install of this Raven may have put it; that one is reported on
    its own as well (``NODE_UV``), for :func:`plan` to use when the one
    found first would ignore the configured index. The index is read the way
    pip reads it (:data:`_PIP_INDEX`), by whichever Python the loop found,
    since uv reads no pip configuration of its own accord.
    """
    return _vars(node_dir, install_name) + (
        f'if [ -x "$node/bin/python" ] && "$node/bin/python" -I -m raven.node --version 2>/dev/null '
        f"| grep -q {shlex.quote(f'"digest": "{digest}"')}; then echo PRESENT=1; fi; "
        'for p in python3.13 python3.12 python3; do q=$(command -v "$p" 2>/dev/null) || continue; '
        "if \"$q\" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' "
        '2>/dev/null; then echo "PYTHON=$q"; break; fi; done; '
        'for u in "$(command -v uv 2>/dev/null)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" "$dir/.uv/uv"; do '
        'if [ -n "$u" ] && [ -x "$u" ]; then echo "UV=$u"; '
        '"$u" --version 2>/dev/null | awk \'NR==1 && $2 {print "UV_VERSION=" $2}\'; break; fi; done; '
        'if [ -x "$dir/.uv/uv" ]; then echo "NODE_UV=$dir/.uv/uv"; '
        '"$dir/.uv/uv" --version 2>/dev/null | awk \'NR==1 && $2 {print "NODE_UV_VERSION=" $2}\'; fi; '
        'for f in curl wget; do c=$(command -v "$f" 2>/dev/null) || continue; echo "FETCH=$c"; break; done; '
        f'if [ -n "$PIP_INDEX_URL" ]; then echo "INDEX=$PIP_INDEX_URL"; elif [ -n "$q" ]; then "$q" -c {shlex.quote(_PIP_INDEX)} '
        "2>/dev/null; fi; "
        'd="$dir"; while [ ! -d "$d" ]; do d=$(dirname "$d"); done; '
        'echo "FREE_KB=$(df -Pk "$d" 2>/dev/null | awk \'NR==2 {print $4}\')"'
    )


def plan(machine_id: str, *, node_dir: str | None = None, run: Runner = _run) -> Plan:
    """Look at ``machine_id`` once and say what installing there would do.

    ``node_dir`` is where to keep nodes when it should differ from the
    machine's registry row; the caller records it once the install succeeds.
    A uv an earlier install fetched under ``node_dir/.uv`` stands in for the
    machine's own when that one would ignore the index pip is configured
    with (older than :data:`UV_INDEX_SINCE`, or silent about its version).
    """
    try:
        target = remote.machine(machine_id)
    except remote.RemoteMachineError as exc:
        raise InstallError(str(exc)) from None
    where = (node_dir or "").strip().rstrip("/") or registered_node_dir(target)
    install_name = bundle.install_name()
    rc, out = run(target, probe_script(where, install_name, bundle.digest()))
    if rc != 0:
        raise InstallError(remote.explain(target, rc, out) or f"could not look at machine {target.label}")
    seen: dict[str, str] = {}
    for line in out.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in {
            "PRESENT",
            "PYTHON",
            "UV",
            "UV_VERSION",
            "NODE_UV",
            "NODE_UV_VERSION",
            "FETCH",
            "FREE_KB",
            "INDEX",
        }:
            seen[key] = value.strip()
    index = seen.get("INDEX", "")
    uv, uv_version = seen.get("UV", ""), seen.get("UV_VERSION", "")
    own, own_version = seen.get("NODE_UV", ""), seen.get("NODE_UV_VERSION", "")
    if index and uv and own != uv and _version_of_uv(uv_version) < UV_INDEX_SINCE <= _version_of_uv(own_version):
        # The machine's own uv comes first in the look and would ignore the
        # index; the one an earlier install fetched under node_dir reads it,
        # and is used rather than fetched again.
        uv, uv_version = own, own_version
    free = seen.get("FREE_KB", "")
    return Plan(
        target=target,
        node_dir=where,
        install_name=install_name,
        present=seen.get("PRESENT") == "1",
        python=seen.get("PYTHON", ""),
        uv=uv,
        free_kb=int(free) if free.isdigit() else None,
        fetch=seen.get("FETCH", ""),
        index=index,
        uv_version=uv_version,
    )


def refusal(p: Plan) -> str | None:
    """Why ``p`` cannot go ahead, or ``None`` when it can.

    The checks an install needs and a look does not: a machine that already
    has this node is found so, and pruned, whatever its pip configuration.
    """
    if p.index:
        try:
            urlsplit(p.index).port
        except ValueError:
            # pip's configuration is hand-written, and a port there that is
            # not one is the owner's to correct; uv would refuse the URL too.
            return (
                f"the package index pip is configured with on machine {p.target.label} has a port that is not a "
                f"number from 0 to 65535 ({_shown_index(p.index)}); correct index-url in pip's configuration there, "
                "or PIP_INDEX_URL"
            )
    if p.how == "" and p.uv_too_old:
        return (
            f"machine {p.target.label} has {_uv_said(p)}, and only uv {_SINCE} or later installs from the package "
            f"index pip is configured with there; with no curl or wget to fetch uv {UV_VERSION}, upgrade uv there "
            f"({UV_INSTALLER}), then run this again"
        )
    if p.how == "":
        return (
            f"machine {p.target.label} has no uv, and no curl or wget to fetch one with; "
            f"install uv there ({UV_INSTALLER}), then run this again"
        )
    if p.free_kb is not None and p.free_kb < p.needed_kb:
        return (
            f"{p.node_dir} on machine {p.target.label} has {p.free_kb // 1024} MB free and a node needs about "
            f"{p.needed_kb // 1024} MB; free some space or choose another directory with --dir"
        )
    return None


def steps(p: Plan) -> list[tuple[str, str]]:
    """``(name, command)`` for each ssh step after the code is sent, in order."""
    # uv's cache and the Python it keeps live inside node_dir: the machine's
    # home may be on a disk with no room (measured 2026-10-10: one registered
    # machine's root filesystem was full).
    uv = shlex.quote(p.uv) if p.uv_from == "machine" else '"$dir/.uv/uv"'
    head = _vars(p.node_dir, p.install_name) + f"uv={uv}; "
    env = 'UV_CACHE_DIR="$dir/.uv-cache" UV_PYTHON_INSTALL_DIR="$dir/.uv-python"'
    # node_dir is made owner-only only when this makes it: one that was there
    # already is the owner's, and may hold what they keep beside the nodes.
    made = '[ -d "$dir" ] || { mkdir -p "$dir" && chmod 700 "$dir"; }; '
    out: list[tuple[str, str]] = []
    if p.uv_from == "fetched":
        # uv's installer for one version, as an unmanaged install: the one
        # binary goes where UV_UNMANAGED_INSTALL says, with no receipt written
        # under the home directory and no shell rc file changed; a wget
        # machine reads the installer the same way.
        fetch = f"wget -qO- {UV_PINNED_INSTALLER}" if p.fetch.endswith("wget") else f"curl -LsSf {UV_PINNED_INSTALLER}"
        out.append(
            (
                "uv",
                head + made + f'mkdir -p "$dir/.uv" && {fetch} | UV_UNMANAGED_INSTALL="$dir/.uv" sh >/dev/null '
                '&& test -x "$dir/.uv/uv"',
            )
        )
    python = shlex.quote(p.python) if p.how == "python" else "3.12"
    venv = made + f'{env} "$uv" venv --quiet --python {python} "$node"'
    # The index pip is configured with on the machine, which uv would not
    # read for itself, comes on the step's stdin into UV_DEFAULT_INDEX, the
    # variable uv reads as --default-index: never a command line, which every
    # account on the machine can read. A step that finds no line there fails
    # rather than install from PyPI behind the owner's back. Without an
    # index, uv's default, PyPI, and nothing is read.
    take_index = "IFS= read -r UV_DEFAULT_INDEX && export UV_DEFAULT_INDEX && " if p.index else ""
    pip = f'{env} "$uv" pip install --quiet --python "$node/bin/python"'
    requirements = " ".join(shlex.quote(r) for r in bundle.pinned(protocol.REQUIREMENTS))
    deps = f"{take_index}{pip} {requirements}"
    # Optional, and never fails the install: the machine's index first (its
    # owner's choice, often a nearby mirror); then the public index too, for
    # a mirror that does not carry it; else the node searches without rg.
    (rg,) = bundle.pinned((protocol.SEARCH_REQUIREMENT,))
    search = (
        f"{take_index}{{ {pip} {shlex.quote(rg)} || {pip} --extra-index-url {protocol.PUBLIC_INDEX} "
        f"{shlex.quote(rg)}; }} >/dev/null 2>&1 && echo RG=1 || echo RG=0"
    )
    # The code goes on the virtualenv's path, and beside it a distribution
    # record naming the version the directory is named by, so the node reads
    # that version (``raven.__version__``) and names itself as the host named
    # it. Outside ``lib/raven``, so the digest of the code is the same.
    link = (
        '"$node/bin/python" -c \'import os, site, sys; lib, version = sys.argv[1:3]; '
        'open(site.getsitepackages()[0] + "/raven-node.pth", "w").write(lib + "\\n"); '
        'meta = os.path.join(lib, "raven-" + version + ".dist-info"); os.makedirs(meta, exist_ok=True); '
        'open(os.path.join(meta, "METADATA"), "w").write("Metadata-Version: 2.1\\nName: raven\\nVersion: " '
        '+ version + "\\n")\' '
        f'"$node/lib" {shlex.quote(_version_of(p.install_name))}'
    )
    out += [
        ("virtualenv", head + venv),
        ("packages", head + deps),
        ("search", head + search),
        ("code", head + 'mkdir -p "$node/lib" && tar -xzf - -C "$node/lib"'),
        ("path", head + link),
        ("check", head + '"$node/bin/python" -I -m raven.node --version'),
    ]
    return out


def prune_script(node_dir: str, install_name: str) -> str:
    """Remove every other node in ``node_dir``, printing ``PRUNED=<id>`` for each.

    A directory counts as a node only when it holds both a ``bin/python`` and
    a ``lib/raven``, so the uv cache and Python beside them, and anything else
    the owner keeps there, are left alone.
    """
    return _vars(node_dir, install_name) + (
        'for d in "$dir"/*; do [ "$d" = "$node" ] && continue; '
        '[ -x "$d/bin/python" ] && [ -d "$d/lib/raven" ] && rm -rf "$d" && echo "PRUNED=${d##*/}"; done; true'
    )


def prune(p: Plan, *, run: Runner = _run) -> tuple[str, ...]:
    """Remove the nodes of other code from ``p``'s ``node_dir``; the ids removed.

    Opt-in: two Ravens of different versions may share one machine account,
    and each needs its own node.
    """
    rc, out = run(p.target, prune_script(p.node_dir, p.install_name))
    if rc != 0:
        raise InstallError(_failed(p, "prune", rc, out))
    return tuple(line.strip()[len("PRUNED=") :] for line in out.splitlines() if line.strip().startswith("PRUNED="))


def install(p: Plan, *, run: Runner = _run, push: Pusher = _push) -> Installed:
    """Carry out ``p``. Raises :class:`InstallError`, after removing the half-made node."""
    if (why := refusal(p)) is not None:
        raise InstallError(why)
    head = _vars(p.node_dir, p.install_name)
    rg = False
    try:
        for name, command in steps(p):
            if name == "code":
                rc, out = push(p.target, command, bundle.tarball())
            elif name in _INDEX_STEPS and p.index:
                rc, out = push(p.target, command, p.index.encode("utf-8") + b"\n")
            else:
                rc, out = run(p.target, command)
            if rc != 0:
                raise InstallError(_failed(p, name, rc, out))
            if name == "search":
                rg = "RG=1" in out
            if name == "check":
                _confirm_digest(p, out)
    except BaseException:
        # Only the node's own directory: node_dir may hold other nodes, and the
        # uv and the Python put there are reused by the next attempt.
        run(p.target, head + 'rm -rf "$node"')
        raise
    rc, out = run(p.target, head + 'du -sk "$node" 2>/dev/null | cut -f1')
    size = out.strip().splitlines()[-1] if rc == 0 and out.strip() else ""
    return Installed(p.node_dir, p.install_name, int(size) if size.isdigit() else None, rg=rg)


def _shown_index(url: str) -> str:
    """``url`` as a sentence shows it: the host and the path, with any sign-in in it left out.

    Cut from the text after the last ``@`` of the authority, as ``urlsplit``
    finds the sign-in, and never through ``.port``: a port pip's configuration
    holds that is not a number would raise, and a sentence about it must
    still be one sentence.
    """
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


_SINCE = ".".join(str(part) for part in UV_INDEX_SINCE)


def _version_of_uv(said: str) -> tuple[int, ...]:
    """``uv --version``'s number as a tuple to compare, or ``()`` -- older than any -- when it is not one."""
    parts = said.strip().split(".")
    if len(parts) < 3 or not all(part.isdigit() for part in parts[:3]):
        return ()
    return tuple(int(part) for part in parts[:3])


def _uv_said(p: Plan) -> str:
    """The machine's uv as a sentence names it: its version when it said one, else that it says none."""
    if _version_of_uv(p.uv_version):
        return f"uv {p.uv_version}"
    return "a uv that does not say which version it is"


def _version_of(install_name: str) -> str:
    """The version an install name begins with: everything before its digest (``<version>-<digest>``)."""
    return install_name.rpartition("-")[0] or install_name


def _failed(p: Plan, step: str, rc: int, out: str) -> str:
    """One sentence for a failed step: ssh's failure explained, else the machine's own last line."""
    if (said := remote.explain(p.target, rc, out)) is not None and rc != remote.NOT_FOUND_RC:
        return f"installing the Raven node: {said}"
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    last = lines[-1][:300] if lines else f"exit {rc}"
    if p.index:
        # uv names the index in its own refusals; a sign-in in it stays out of this sentence too.
        last = last.replace(p.index, _shown_index(p.index))
    return f"installing the Raven node on machine {p.target.label} failed at the {step} step: {last}"


def _confirm_digest(p: Plan, out: str) -> None:
    lines = [line for line in out.splitlines() if line.strip().startswith("{")]
    try:
        reported = json.loads(lines[-1]) if lines else {}
    except ValueError:
        reported = {}
    if reported.get("digest") != bundle.digest() or reported.get("protocol") != protocol.PROTOCOL:
        raise InstallError(
            f"the Raven node installed on machine {p.target.label} does not report this Raven's code "
            f"({reported.get('digest') or 'nothing'} instead of {bundle.digest()})"
        )


__all__ = [
    "UV_INDEX_SINCE",
    "UV_INSTALLER",
    "UV_PINNED_INSTALLER",
    "UV_VERSION",
    "InstallError",
    "Installed",
    "Plan",
    "install",
    "plan",
    "probe_script",
    "prune",
    "prune_script",
    "refusal",
    "steps",
]
