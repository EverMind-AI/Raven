"""Shared Typer-reflection helpers for ``commands.catalog`` + ``cli.dispatch``.

Both ``methods/commands.py`` (catalog handler) and
``methods/cli_dispatch.py`` (dispatch-compat check) need to enumerate the
visible commands registered on a Typer app. Keeping the helpers here
avoids:

1. Drift between the two modules' name-resolution rules (the reviewer
   pre-merge flagged the previous duplication as L1).
2. A circular import — ``commands.py`` imports ``_DISPATCH_BLACKLIST``
   from ``cli_dispatch.py``, so the reverse direction is blocked at
   module load.

Reflection contract (per harness-command-catalog-dynamic design.md §D1):

- ``CommandInfo.name`` is preferred when set; otherwise resolve via
  ``callback.__name__`` with ``_`` → ``-`` to match Typer's CLI surface.
- ``CommandInfo.hidden`` is honored — ``hidden=True`` commands do not
  show up in ``--help`` output and must not show up in the slash
  catalog either.
"""

from __future__ import annotations

import typer


def resolve_name(ci: typer.models.CommandInfo) -> str | None:
    """Return the canonical (hyphenated) name for a Typer command, or None.

    Typer canonicalises ``@command("foo-bar")`` (explicit) directly. For
    ``@command()`` on a function ``foo_bar`` (implicit), ``ci.name`` is
    ``None`` and the user-facing surface is ``foo-bar`` — we mirror that
    here.
    """
    if ci.name:
        return ci.name
    if not ci.callback:
        return None
    return ci.callback.__name__.replace("_", "-")


def collect_command_names(typer_obj: typer.Typer) -> set[str]:
    """Return all visible (non-hidden) command names registered on a Typer app.

    Works uniformly for the root app and any subgroup — both expose
    ``registered_commands`` of the same shape.
    """
    names: set[str] = set()
    for ci in typer_obj.registered_commands:
        if getattr(ci, "hidden", False):
            continue
        name = resolve_name(ci)
        if name is not None:
            names.add(name)
    return names


__all__ = ["resolve_name", "collect_command_names"]
