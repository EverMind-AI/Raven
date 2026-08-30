"""One-call plugin bootstrap helper.

Glues :class:`PluginDiscovery` and :class:`PluginRegistry` so callers
(CLI / AgentLoop construction) don't repeat the two-step dance.

Kept as a free function rather than a class so the dataflow stays
obviously linear: discover → activate → return. Callers wanting more
control instantiate the two pieces directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from raven.plugins.discover import PluginDiscovery
from raven.plugins.registry import PluginRegistry


def assemble_plugin_registry(
    *,
    bundled_dir: Path | None = None,
    user_dir: Path | None = None,
    project_dir: Path | None = None,
    entry_points_group: str | None = "raven.plugins",
    disabled: frozenset[str] = frozenset(),
    extra_dirs: Sequence[Path] = (),
) -> PluginRegistry:
    """Discover all manifests, admit the enabled ones, return the registry.

    ``entry_points_group`` defaults to ``"raven.plugins"`` because
    that is the public group third-party plugins target in their
    ``pyproject.toml``. Pass ``None`` to suppress entry-point discovery
    entirely (tests do this to stay hermetic).
    """
    discovery = PluginDiscovery(
        bundled_dir=bundled_dir,
        user_dir=user_dir,
        project_dir=project_dir,
        entry_points_group=entry_points_group,
        extra_dirs=extra_dirs,
    )
    registry = PluginRegistry()
    registry.activate(discovery.discover(), disabled=disabled)
    return registry


__all__ = ["assemble_plugin_registry"]
