"""Multi-source plugin discovery.

A discovery pass scans every source the host knows about (bundled
sub-tree, user-level dir, project-level dir, pip entry points),
deduplicates by plugin id, and returns a stable list of
:class:`DiscoveredPlugin` records. Discovery only reads manifests — no
plugin Python code is imported here. The :class:`Source` enum doubles
as the conflict-resolution priority ordering (higher wins).

Priority order (`bundled > user > project > entry_points`) follows the
design's "builtin shadow rule": a bundled plugin can never be shadowed
by a same-named local or pip-installed one. Among the non-bundled
sources, user-level wins so a developer can substitute a locally edited
copy for a pip-installed version while iterating.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import IntEnum
from importlib import metadata
from importlib.resources import as_file, files
from pathlib import Path

from raven.plugin.manifest import PluginManifest

logger = logging.getLogger(__name__)


_MANIFEST_FILENAME = "raven-plugin.toml"


class Source(IntEnum):
    """Where a manifest came from. Numeric value is conflict priority —
    higher wins. Lower values lose silently and are logged."""

    ENTRY_POINTS = 1
    PROJECT = 2
    USER = 3
    BUNDLED = 4


@dataclass(frozen=True)
class DiscoveredPlugin:
    """A manifest read from a specific source, awaiting activation."""

    manifest: PluginManifest
    source: Source
    location: Path | None
    """Path to the manifest file. ``None`` for entry-points-discovered
    plugins where the manifest lives inside a wheel's package data."""


class PluginDiscovery:
    """Scans every configured source and returns deduplicated plugins.

    Constructor params default to "off"; callers pass concrete paths
    or the entry-point group name to opt each source in. This keeps
    tests hermetic — they construct a discovery instance pointing at
    tmp dirs and don't accidentally pick up real plugins on the
    developer's machine.
    """

    def __init__(
        self,
        *,
        bundled_dir: Path | None = None,
        user_dir: Path | None = None,
        project_dir: Path | None = None,
        entry_points_group: str | None = None,
    ) -> None:
        self._bundled_dir = bundled_dir
        self._user_dir = user_dir
        self._project_dir = project_dir
        self._entry_points_group = entry_points_group

    def discover(self) -> list[DiscoveredPlugin]:
        """Run all enabled sources and resolve conflicts.

        The returned list is stable-ordered by plugin id so callers can
        log / display it deterministically.
        """
        all_found: list[DiscoveredPlugin] = []
        if self._bundled_dir is not None:
            all_found.extend(
                self._scan_dir(self._bundled_dir, Source.BUNDLED),
            )
        if self._user_dir is not None:
            all_found.extend(self._scan_dir(self._user_dir, Source.USER))
        if self._project_dir is not None:
            all_found.extend(
                self._scan_dir(self._project_dir, Source.PROJECT),
            )
        if self._entry_points_group is not None:
            all_found.extend(self._scan_entry_points(self._entry_points_group))

        return self._resolve_conflicts(all_found)

    # ── File-based sources ─────────────────────────────────────────

    def _scan_dir(
        self, root: Path, source: Source,
    ) -> list[DiscoveredPlugin]:
        """Look for ``<root>/<plugin_id>/raven-plugin.toml``.

        Subdir name is informational only — the canonical plugin id is
        the one inside the manifest. A mismatch is logged but the
        manifest still loads.
        """
        out: list[DiscoveredPlugin] = []
        if not root.is_dir():
            return out
        for sub in sorted(root.iterdir()):
            if not sub.is_dir():
                continue
            manifest_path = sub / _MANIFEST_FILENAME
            if not manifest_path.is_file():
                continue
            try:
                mf = PluginManifest.from_toml_path(manifest_path)
            except Exception as e:
                logger.warning(
                    "plugin manifest %s failed to parse (%s); skipping",
                    manifest_path, e,
                )
                continue
            if mf.id != sub.name:
                logger.info(
                    "plugin %s lives in directory %s — id and dir name differ",
                    mf.id, sub.name,
                )
            out.append(
                DiscoveredPlugin(
                    manifest=mf, source=source, location=manifest_path,
                ),
            )
        return out

    # ── Entry-points source ────────────────────────────────────────

    def _scan_entry_points(self, group: str) -> list[DiscoveredPlugin]:
        """Resolve every entry point in ``group`` and read the manifest
        shipped inside that entry point's package.

        Entry-point value is the bare package name (e.g. a third-party
        ``raven_mem0``). We use ``importlib.resources`` to locate
        ``raven-plugin.toml`` inside that package. The package's
        ``__init__.py`` is imported as part of resource resolution —
        side-effects there would betray the "manifest-only" promise, so
        plugin packages are expected to keep ``__init__`` empty / cheap.
        """
        out: list[DiscoveredPlugin] = []
        try:
            eps = metadata.entry_points(group=group)
        except Exception as e:
            logger.warning("entry_points discovery failed (%s); skipping", e)
            return out

        for ep in eps:
            package_name = ep.value.split(":", 1)[0]
            try:
                resource_root = files(package_name)
                manifest_resource = resource_root.joinpath(_MANIFEST_FILENAME)
                with as_file(manifest_resource) as manifest_path:
                    if not manifest_path.is_file():
                        logger.warning(
                            "entry-point %s points at package %s but "
                            "no %s found; skipping",
                            ep.name, package_name, _MANIFEST_FILENAME,
                        )
                        continue
                    mf = PluginManifest.from_toml_path(manifest_path)
                    out.append(
                        DiscoveredPlugin(
                            manifest=mf,
                            source=Source.ENTRY_POINTS,
                            location=None,
                        ),
                    )
            except Exception as e:
                logger.warning(
                    "failed to load manifest for entry-point %s (%s); skipping",
                    ep.name, e,
                )
        return out

    # ── Conflict resolution ────────────────────────────────────────

    @staticmethod
    def _resolve_conflicts(
        found: list[DiscoveredPlugin],
    ) -> list[DiscoveredPlugin]:
        """Group by plugin id, keep the highest-priority source.

        Lower-priority duplicates are logged once each so a misconfigured
        setup is debuggable without silently dropping plugins.
        """
        by_id: dict[str, DiscoveredPlugin] = {}
        for d in found:
            current = by_id.get(d.manifest.id)
            if current is None or d.source > current.source:
                if current is not None:
                    logger.info(
                        "plugin %s: %s shadows %s",
                        d.manifest.id, d.source.name, current.source.name,
                    )
                by_id[d.manifest.id] = d
            elif d.source < current.source:
                logger.info(
                    "plugin %s: %s shadowed by %s",
                    d.manifest.id, d.source.name, current.source.name,
                )
        # Stable sort by id so caller-side display order is deterministic.
        return sorted(by_id.values(), key=lambda p: p.manifest.id)


__all__ = ["DiscoveredPlugin", "PluginDiscovery", "Source"]
