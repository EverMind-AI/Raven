"""Custom Hatchling build hook: conditionally package the prebuilt TUI bundle.

The TUI ships as a single self-contained esbuild bundle at ``ui-tui/dist/entry.js``.
We want a wheel to carry it so `pip`/`uv tool install` yields a working
`raven tui` with no source checkout. But ``dist/`` is a build artifact and is
NOT committed (see .gitignore), so a clean checkout legitimately lacks it.

A static ``[tool.hatch.build.targets.wheel.force-include]`` entry would make
hatchling hard-fail with "Forced include not found" whenever ``dist/`` is
absent — which would break the ordinary developer flow (`git clone && uv sync`
with no prior `npm run build`). So instead we add the bundle to the wheel's
force-include map *only when it exists*, and emit a warning otherwise.

Release builds run ``npm ci && npm run build`` first (see
.github/workflows/release.yml), so the published wheel always carries the
bundle; dev builds without it simply fall back to the source tree at runtime
(see resolve_dist_entry() in raven/cli/tui_commands.py).
"""

from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        dist = Path(self.root) / "ui-tui" / "dist"
        if dist.is_dir() and (dist / "entry.js").is_file():
            # Map source -> path inside the wheel's `raven` package.
            build_data.setdefault("force_include", {})[str(dist)] = "raven/ui-tui/dist"
        else:
            self.app.display_warning(
                "ui-tui/dist/entry.js not found — building WITHOUT the bundled TUI. "
                "`raven tui` from this wheel will not work; run "
                "`npm --prefix ui-tui ci && npm --prefix ui-tui run build` before "
                "building a release wheel."
            )
