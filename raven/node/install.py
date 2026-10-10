"""Put this Raven's node on a registered machine.

What lands there, all under ``<node_dir>/<install name>/`` (``node_dir`` from the
machine's registry row, ``~/.raven-node`` when it names none): a Python 3.12
virtualenv, the packages in :data:`raven.node.protocol.REQUIREMENTS` pinned to
this Raven's own versions, and this Raven's own code (:mod:`raven.node.bundle`).
Nothing else on the machine changes: no system package, no shell rc file, no
environment it already had.

The Python is the machine's own when it has 3.12 or later with ``venv``;
otherwise the machine's ``uv`` makes one inside ``node_dir``. With neither, the
install stops and says so rather than fetching an installer and running it.

Each step is one ssh call through :func:`raven.acp_client.remote.run`; a step
that fails removes the half-made node directory, so the next attempt starts
clean and :func:`plan` never mistakes a broken node for an installed one.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from raven.acp_client import remote
from raven.node import bundle, protocol
from raven.node.client import node_dir as registered_node_dir

#: Roughly what a node takes, for the free-space check: Raven's code with
#: pillow, loguru and ripgrep-bin (measured 2026-10-10), and a Python 3.12 when
#: the machine has none of its own.
_NODE_KB = 60 * 1024
_PYTHON_KB = 80 * 1024

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

    @property
    def how(self) -> str:
        """``"python"`` (the machine's own 3.12), ``"uv"`` (one made by uv), or ``""`` (neither)."""
        if self.python:
            return "python"
        return "uv" if self.uv else ""

    @property
    def needed_kb(self) -> int:
        return _NODE_KB + (_PYTHON_KB if self.how == "uv" else 0)

    def describe(self) -> str:
        """The install as a person is asked to approve it."""
        where = f"{self.node_dir}/{self.install_name}"
        python = (
            f"a virtualenv of the machine's own {self.python}"
            if self.how == "python"
            else "a Python 3.12 made by the machine's uv, kept beside it"
        )
        space = f"; {self.free_kb // (1024 * 1024)} GB free there" if self.free_kb is not None else ""
        return (
            f"Install the Raven node {self.install_name} on machine {self.target.label} into {where}: {python}, "
            f"{' '.join(bundle.pinned((*protocol.REQUIREMENTS, protocol.SEARCH_REQUIREMENT)))} from the "
            "machine's package index, and this Raven's code. "
            f"About {self.needed_kb // 1024} MB{space}. Nothing else on the machine changes."
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


def probe_script(node_dir: str, install_name: str, digest: str) -> str:
    """One look at the machine: is this node there, which Python and uv does it have, how much room."""
    return _vars(node_dir, install_name) + (
        f'if [ -x "$node/bin/python" ] && "$node/bin/python" -m raven.node --version 2>/dev/null '
        f"| grep -q {shlex.quote(f'"digest": "{digest}"')}; then echo PRESENT=1; fi; "
        'for p in python3.13 python3.12 python3; do q=$(command -v "$p" 2>/dev/null) || continue; '
        "if \"$q\" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 12) else 1)' "
        '2>/dev/null; then echo "PYTHON=$q"; break; fi; done; '
        'for u in "$(command -v uv 2>/dev/null)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do '
        'if [ -n "$u" ] && [ -x "$u" ]; then echo "UV=$u"; break; fi; done; '
        'd="$dir"; while [ ! -d "$d" ]; do d=$(dirname "$d"); done; '
        'echo "FREE_KB=$(df -Pk "$d" 2>/dev/null | awk \'NR==2 {print $4}\')"'
    )


def plan(machine_id: str, *, node_dir: str | None = None, run: Runner = _run) -> Plan:
    """Look at ``machine_id`` once and say what installing there would do.

    ``node_dir`` is where to keep nodes when it should differ from the
    machine's registry row; the caller records it once the install succeeds.
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
        if sep and key in {"PRESENT", "PYTHON", "UV", "FREE_KB"}:
            seen[key] = value.strip()
    free = seen.get("FREE_KB", "")
    return Plan(
        target=target,
        node_dir=where,
        install_name=install_name,
        present=seen.get("PRESENT") == "1",
        python=seen.get("PYTHON", ""),
        uv=seen.get("UV", ""),
        free_kb=int(free) if free.isdigit() else None,
    )


def refusal(p: Plan) -> str | None:
    """Why ``p`` cannot go ahead, or ``None`` when it can."""
    if p.how == "":
        return (
            f"machine {p.target.label} has neither a Python 3.12 (with venv) nor uv; "
            "install one of them there, then run this again"
        )
    if p.free_kb is not None and p.free_kb < p.needed_kb:
        return (
            f"{p.node_dir} on machine {p.target.label} has {p.free_kb // 1024} MB free and a node needs about "
            f"{p.needed_kb // 1024} MB; free some space or choose another directory with --dir"
        )
    return None


def steps(p: Plan) -> list[tuple[str, str]]:
    """``(name, command)`` for each ssh step after the code is sent, in order."""
    head = _vars(p.node_dir, p.install_name)
    if p.how == "python":
        venv = f'mkdir -p "$dir" && {shlex.quote(p.python)} -m venv "$node"'
    else:
        # Python, cache and all inside node_dir: the machine's home may be on a
        # disk with no room (measured 2026-10-10: one registered machine's root
        # filesystem was full).
        venv = (
            'mkdir -p "$dir" && UV_CACHE_DIR="$dir/.uv-cache" UV_PYTHON_INSTALL_DIR="$dir/.uv-python" '
            f'{shlex.quote(p.uv)} venv --quiet --seed --python 3.12 "$node"'
        )
    pip = '"$node/bin/python" -m pip install --quiet --no-cache-dir --disable-pip-version-check --no-input'
    requirements = " ".join(shlex.quote(r) for r in bundle.pinned(protocol.REQUIREMENTS))
    deps = f"{pip} {requirements}"
    # Optional, and never fails the install: the machine's own index first (its
    # owner chose it, often a nearby mirror); then the public index, for a
    # mirror that does not carry it; else the node searches without rg.
    (rg,) = bundle.pinned((protocol.SEARCH_REQUIREMENT,))
    search = (
        f"{{ {pip} {shlex.quote(rg)} || {pip} --extra-index-url {protocol.PUBLIC_INDEX} {shlex.quote(rg)}; }} "
        ">/dev/null 2>&1 && echo RG=1 || echo RG=0"
    )
    link = (
        '"$node/bin/python" -c \'import site, sys; '
        'open(site.getsitepackages()[0] + "/raven-node.pth", "w").write(sys.argv[1] + "\\n")\' "$node/lib"'
    )
    return [
        ("virtualenv", head + venv),
        ("packages", head + deps),
        ("search", head + search),
        ("code", head + 'mkdir -p "$node/lib" && tar -xzf - -C "$node/lib"'),
        ("path", head + link),
        ("check", head + '"$node/bin/python" -m raven.node --version'),
    ]


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
            else:
                rc, out = run(p.target, command)
            if rc != 0:
                raise InstallError(_failed(p, name, rc, out))
            if name == "search":
                rg = "RG=1" in out
            if name == "check":
                _confirm_digest(p, out)
    except BaseException:
        # Only the node's own directory: node_dir may hold other nodes, and a
        # Python uv made there is reused by the next attempt.
        run(p.target, head + 'rm -rf "$node"')
        raise
    rc, out = run(p.target, head + 'du -sk "$node" 2>/dev/null | cut -f1')
    size = out.strip().splitlines()[-1] if rc == 0 and out.strip() else ""
    return Installed(p.node_dir, p.install_name, int(size) if size.isdigit() else None, rg=rg)


def _failed(p: Plan, step: str, rc: int, out: str) -> str:
    """One sentence for a failed step: ssh's failure explained, else the machine's own last line."""
    if (said := remote.explain(p.target, rc, out)) is not None and rc != remote.NOT_FOUND_RC:
        return f"installing the Raven node: {said}"
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    last = lines[-1][:300] if lines else f"exit {rc}"
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
