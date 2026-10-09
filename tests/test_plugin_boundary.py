"""Dependency direction between the host and the bundled everos plugin.

The host may know the plugin only through the plugin contract
(``raven.contracts.memory`` and ``raven.plugins``); it must not import the
``raven_everos`` package. The plugin may use the host's public helpers --
``raven.config.update`` included, since the ``plugins.config`` slice it writes
there is its own data -- but must not reach into host modules that exist for
the plugin's sake. The list below holds the violations still standing; a task
that removes one deletes its entry here, and the final task asserts it is
empty.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOST_DIR = REPO_ROOT / "raven"
# Every distribution beside the host wheel, by its package directory; a second
# memory plugin landed beside the first, and a scan that knew one by name
# would have let the other import whatever it liked.
PLUGIN_DIRS = sorted(d for d in (REPO_ROOT / "plugins-dist").glob("*/raven_*") if d.is_dir())

_PLUGIN_IMPORT = re.compile(r"^\s*(from|import)\s+raven_everos(_cloud)?\b", re.M)
_HOST_PRIVATE = re.compile(
    r"^\s*(?:from\s+raven\.(cli|config\.loader|config\.raven)\b|import\s+raven\.(cli|config\.loader|config\.raven)\b)",
    re.M,
)

# The two GUI surfaces still shaped around EverOS's own store: the memory
# browser reads its four kinds over EverOS's HTTP API, and the settings page
# edits the model roles behind EverOS's extraction. Both need the wire contract
# generalised before a second backend could answer them, which is a frontend
# change; everything else the host does now goes through MemoryBackend.
HOST_WIRE_PROTOCOL_SURFACES: frozenset[str] = frozenset(
    {
        "raven/rpc/methods/console.py",
        "raven/rpc/methods/memory.py",
    }
)


def _rel(p: Path) -> str:
    return str(p.relative_to(REPO_ROOT))


def _host_files() -> list[Path]:
    return list(HOST_DIR.rglob("*.py"))


def _plugin_files() -> list[Path]:
    return [f for d in PLUGIN_DIRS for f in d.rglob("*.py")]


def test_scan_roots_exist() -> None:
    assert _host_files(), f"no host sources under {HOST_DIR}: the scan would pass vacuously"
    assert {"raven_everos", "raven_everos_cloud"} <= {d.name for d in PLUGIN_DIRS}, PLUGIN_DIRS
    for d in PLUGIN_DIRS:
        assert any(d.rglob("*.py")), f"no plugin sources under {d}: the scan would pass vacuously"


def test_host_does_not_import_plugin_internals() -> None:
    offenders = {_rel(p) for p in _host_files() if _PLUGIN_IMPORT.search(p.read_text(encoding="utf-8"))}
    assert offenders == set(HOST_WIRE_PROTOCOL_SURFACES), (
        f"new host->plugin imports: {sorted(offenders - HOST_WIRE_PROTOCOL_SURFACES)}; "
        f"stale entries: {sorted(HOST_WIRE_PROTOCOL_SURFACES - offenders)}"
    )


def test_plugin_does_not_import_host_private_modules() -> None:
    offenders = {_rel(p) for p in _plugin_files() if _HOST_PRIVATE.search(p.read_text(encoding="utf-8"))}
    assert offenders == set(), f"new plugin->host imports: {sorted(offenders)}"


def test_nothing_under_raven_decides_ownership() -> None:
    """Who may write an EverOS root is the plugin's judgement, never the host's.

    A20's only acceptance evidence. The host asking `everos_owned` for itself is
    how the guard came to live in two places with one of them out of date -- the
    write primitives are where it belongs, so a new caller cannot opt out of it.
    A grep, because the property is about absence: nothing under `raven/` may
    name it, and a test that only checked the callers it knows about would pass
    the moment somebody adds one it does not.
    """
    offenders = {_rel(p) for p in _host_files() if re.search(r"\beveros_owned\b", p.read_text(encoding="utf-8"))}

    assert offenders == set(), f"the host is deciding EverOS ownership in: {sorted(offenders)}"
