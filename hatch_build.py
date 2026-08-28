"""Custom Hatchling build hook: conditionally package the prebuilt TUI bundle,
the served page, and the vendored sub-agent tree.

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
(see resolve_dist_entry() in raven/cli/tui_commands.py). That fallback is also
why an editable build carries neither artifact: the checkout it reads is the
one it was built from, so a packaged copy there is dead weight. All three trees
this hook maps are therefore skipped for ``version == "editable"``.

``subagents/`` is force-included file by file rather than as one directory,
because a directory entry would ship the working copy rather than the source.
Hatchling applies no ``include`` / ``exclude`` config to a force-included path:
``recurse_forced_files`` drops a hardcoded set of directories (``.venv``,
``.git``, the caches) and nothing else. What sits next to the source in a built
working copy is each fork's ``.env`` — scaffolded by ``subagents/install.sh``
and filled with a real provider key — plus rollback tarballs and benchmark data,
none of it committed and all of it swept up by a directory walk. Reading the
list from git instead is safe by construction, and is the same rule
``scripts/check_vendored_subagents.py`` already applies for the same reason.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path, PurePosixPath

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

VENDOR_ROOT = "subagents"


def _is_secret(relative_path: str) -> bool:
    """Whether this path is a filled-in secrets file rather than its template."""
    name = PurePosixPath(relative_path).name
    if not (name == ".env" or name.startswith(".env.")):
        return False
    return not name.endswith((".example", ".sample", ".template"))


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        self._include_prebuilt_assets(build_data, version)
        self._include_vendored_subagents(build_data, version)

    def _include_prebuilt_assets(self, build_data: dict, version: str) -> None:
        """Map the prebuilt TUI bundle and served page into ``raven/``.

        Skipped for an editable build, for the reason the sub-agent tree below
        records: hatchling honours this map for editable builds too, and there
        both resolvers read the checkout's own copy first
        (``resolve_dist_entry`` and ``resolve_ui_dist`` try the packaged path,
        which for an editable install points inside the checkout's ``raven/``
        and does not exist). So every ``uv sync`` was copying 4.6 MB uncompressed
        into a site-packages directory nothing ever opens -- measured by building
        the editable wheel both ways: 4,647,584 B of force-included files before,
        65,052 B after, the remainder being ``bridge`` from pyproject.toml.

        The warnings go with it rather than firing mode-blind: they tell the
        reader what to run *before building a release wheel*, which an editable
        build is not, and a checkout missing an artifact is already told so by
        the command that needs it -- ``raven tui`` names both candidate paths
        and the npm command, ``raven web`` names ``ui/build.py``.
        """
        if version == "editable":
            return

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

        # The same conditional treatment for the served page. `raven serve` looks
        # for the packaged copy first (resolve_ui_dist), so without this a wheel
        # answers `/` with the placeholder even though the source tree has a page.
        ui_dist = Path(self.root) / "ui" / "dist"
        if ui_dist.is_dir() and (ui_dist / "index.html").is_file():
            build_data.setdefault("force_include", {})[str(ui_dist)] = "raven/ui/dist"
        else:
            self.app.display_warning(
                "ui/dist/index.html not found — building WITHOUT the bundled page. "
                "`raven serve` from this wheel will answer / with the placeholder; "
                "run `python ui/build.py` before building a release wheel."
            )

    def _include_vendored_subagents(self, build_data: dict, version: str) -> None:
        """Map each committed file of ``subagents/`` into ``raven/subagents``.

        A wheel that carries the tree is what puts the four agents on a machine
        that installed raven from a release: ``subagents_root()`` finds the
        packaged copy and ``_install_packaged_tree()`` copies it out to the raven
        home, where an upgrade cannot take the built venvs with it. Without it,
        onboarding step 5 has nothing to offer and the agents exist only in source
        checkouts.

        Copies rather than moves, and both copies stay: the one under site-packages
        is what the installer's RECORD owns, and it is the fallback ``subagents_root``
        falls back to when the copy-out fails. So an install carrying this tree holds
        it twice, about 106 MiB in each place.

        Skipped for an editable build, which *is* a source checkout -- there
        ``subagents_root()`` already reads the tree beside the package. Hatchling
        honours this map for editable builds too (``get_forced_inclusion_map``
        folds ``build_data`` in through ``build_force_include``), so without the
        guard every ``uv sync`` copies 106 MiB into a site-packages directory that
        nothing ever reads.
        """
        if version == "editable":
            return

        root = Path(self.root)
        if not (root / VENDOR_ROOT).is_dir():
            # Loud rather than silent, for the reason the sdist note above the
            # build step in .github/workflows/release.yml already records: a
            # combined `uv build` builds the wheel from a freshly made sdist, and
            # the sdist carries neither this tree nor a .git to read it from.
            self.app.display_warning(
                f"{VENDOR_ROOT}/ not found — building WITHOUT the vendored sub-agents. "
                "Onboarding step 5 will report no sub-agent tree. Build the wheel "
                "directly from a git checkout, not from an sdist."
            )
            return

        tracked = self._committed_paths(VENDOR_ROOT)
        if tracked is None:
            self.app.display_warning(
                f"git cannot list {VENDOR_ROOT}/ — building WITHOUT the vendored sub-agents. "
                "The wheel's onboarding step 5 will report no sub-agent tree. Build "
                "from a git checkout to include them."
            )
            return
        if not tracked:
            # Kept apart from the case above: git answered, and what it said is
            # that this tree is not committed. Saying "git cannot list" there
            # would send the reader to look for a broken checkout.
            self.app.display_warning(
                f"git tracks no files under {VENDOR_ROOT}/ — building WITHOUT the "
                "vendored sub-agents. The wheel's onboarding step 5 will report no "
                "sub-agent tree."
            )
            return

        forced = build_data.setdefault("force_include", {})
        for relative_path in tracked:
            if _is_secret(relative_path):
                msg = (
                    f"refusing to package {relative_path}: a filled-in secrets file "
                    "must never reach a wheel. Untrack it and rotate the key it holds."
                )
                raise ValueError(msg)
            source = root / relative_path
            # An index entry whose file is gone (a deletion staged but not
            # committed) would make hatchling hard-fail on "Forced include not
            # found" and take the whole build down with it.
            if source.is_file():
                forced[str(source)] = f"raven/{relative_path}"

    def _committed_paths(self, relative_root: str) -> list[str] | None:
        """Paths git tracks under ``relative_root``, or None when git cannot answer."""
        git = shutil.which("git")
        if git is None:
            return None
        try:
            completed = subprocess.run(
                [git, "-C", self.root, "ls-files", "-z", relative_root],
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return [path for path in completed.stdout.split("\0") if path]
