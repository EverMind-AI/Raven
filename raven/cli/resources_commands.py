"""Top-level ``resources`` command -- download the weights and dictionaries.

A seat on the CLI rather than only ``python -m raven.resources``, because this
is the command two runtime errors name: a reader that cannot find its
dictionary or its deepdoc model raises with the one line that fixes it, and
that line has to be typeable by someone who installed from a wheel and has no
checkout to run a script in.

The work is :mod:`raven.resources`; this only spells its flags as options.
"""

from __future__ import annotations

import typer


def register(app: typer.Typer) -> None:
    """Attach the ``resources`` command to ``app``."""

    @app.command()
    def resources(
        only: str = typer.Option(
            "",
            "--only",
            help="Fetch one set rather than both: 'dictionary' or 'deepdoc'.",
        ),
        all_layouts: bool = typer.Option(
            False,
            "--all-layouts",
            help="Also fetch the genre-specific layout models (3 x 76 MB), which nothing selects yet.",
        ),
        force: bool = typer.Option(False, "--force", help="Download again even when the file is already here."),
        optional: bool = typer.Option(
            False,
            "--optional",
            help="Report a failure and exit 0; for an install that must not stop because a host is unreachable.",
        ),
    ) -> None:
        """Download the model weights and dictionaries raven reads at runtime."""
        from raven.resources import main

        argv: list[str] = []
        if only:
            argv += ["--only", only]
        if all_layouts:
            argv.append("--all-layouts")
        if force:
            argv.append("--force")
        if optional:
            argv.append("--optional")
        raise typer.Exit(main(argv))
