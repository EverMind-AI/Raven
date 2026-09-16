"""Dependency direction between the host and the bundled memory plugins.

The host may know a plugin only through the plugin contract
(``raven.contracts.memory`` and ``raven.plugins``); it must not import the
``raven_everos`` or ``raven_cloud_memory`` packages. The plugin may use the host's public helpers --
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
PLUGIN_DIRS = (
    REPO_ROOT / "plugins-dist" / "everos-memory" / "raven_everos",
    REPO_ROOT / "plugins-dist" / "cloud-memory" / "raven_cloud_memory",
)

_PLUGIN_IMPORT = re.compile(r"^\s*(from|import)\s+raven_(everos|cloud_memory)\b", re.M)
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
    return [p for d in PLUGIN_DIRS for p in d.rglob("*.py")]


def test_scan_roots_exist() -> None:
    assert _host_files(), f"no host sources under {HOST_DIR}: the scan would pass vacuously"
    for d in PLUGIN_DIRS:
        assert list(d.rglob("*.py")), f"no plugin sources under {d}: the scan would pass vacuously"


def test_host_does_not_import_plugin_internals() -> None:
    offenders = {_rel(p) for p in _host_files() if _PLUGIN_IMPORT.search(p.read_text(encoding="utf-8"))}
    assert offenders == set(HOST_WIRE_PROTOCOL_SURFACES), (
        f"new host->plugin imports: {sorted(offenders - HOST_WIRE_PROTOCOL_SURFACES)}; "
        f"stale entries: {sorted(HOST_WIRE_PROTOCOL_SURFACES - offenders)}"
    )


def test_plugin_does_not_import_host_private_modules() -> None:
    offenders = {_rel(p) for p in _plugin_files() if _HOST_PRIVATE.search(p.read_text(encoding="utf-8"))}
    assert offenders == set(), f"new plugin->host imports: {sorted(offenders)}"
