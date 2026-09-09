"""``raven upgrade``: the typer shell over the upgrade engine.

The engine (release lookup, version keys, the plan, the detached handoff)
lives in ``raven.updates.upgrade``; what stays on the surface is exactly the
part that renders -- the command body and its console messages. Engine
symbols are reached through the module, never from-imported: the engine's
own docstrings promise module-level monkeypatchability, and an early-bound
copy here would put this shell outside that seam.
"""

from __future__ import annotations

from importlib import metadata

import httpx
import typer
from rich.console import Console

from raven.updates import upgrade as _upgrade

console = Console()


def register(app: typer.Typer) -> None:
    @app.command()
    def upgrade(
        check: bool = typer.Option(
            False,
            "--check",
            help="Check for a newer Raven build without installing it.",
        ),
    ) -> None:
        """Check for and install the newest Raven build this install is entitled to.

        Stable by default; an install that joined the beta channel gets its
        newest beta instead. The failure path of the served page's one-click
        update tells the reader to run this, so it has to follow the same
        channel that offered them the update.
        """
        try:
            current_version = _upgrade._current_version()
            release, version_key = _upgrade.fetch_latest_for_channel()
            current_key = version_key(current_version)
            latest_key = version_key(release.version)

            if current_key == latest_key:
                console.print(f"Raven {current_version} is up to date.")
                return
            if current_key > latest_key:
                console.print(
                    f"Raven {current_version} is newer than the latest release "
                    f"{release.version}; no downgrade was performed."
                )
                return
            if check:
                console.print(f"Raven upgrade available: {current_version} -> {release.version}")
                console.print("Run [cyan]raven upgrade[/cyan] to install it.")
                return
            if _upgrade._is_editable_install():
                raise _upgrade.UpgradeError(
                    "Editable Raven installations cannot be upgraded automatically. "
                    "Pull the source checkout and rebuild Raven."
                )
            target = _upgrade._uv_tool_target()
            if target is None:
                raise _upgrade.UpgradeError(
                    "This Raven installation is not managed by uv. "
                    "Reinstall Raven with the official installer, then run raven upgrade."
                )

            _upgrade._handoff_upgrade(release, current_version, target)
        except _upgrade.ReleaseLookupError as exc:
            console.print(f"[red]Unable to upgrade Raven:[/red] {_upgrade._sentence(exc)}")
            raise typer.Exit(1) from exc
        except (
            _upgrade.UpgradeError,
            httpx.HTTPError,
            ValueError,
            metadata.PackageNotFoundError,
        ) as exc:
            console.print(
                f"[red]Unable to upgrade Raven:[/red] {_upgrade._sentence(exc)} "
                "If the problem persists, rerun the official installer."
            )
            raise typer.Exit(1) from exc
