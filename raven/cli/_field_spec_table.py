"""The field-spec table the channel and provider config commands print, and the
``--help`` interception both use for their free-form ``ctx.args`` verbs."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table


def help_requested(extra_args: list[str]) -> bool:
    """Detect ``--help`` / ``-h`` inside a free-form ``ctx.args`` list."""
    return any(t in ("--help", "-h") or t.startswith("--help=") for t in extra_args)


def render_field_spec_table(
    console: Console,
    *,
    title: str,
    specs: dict[str, dict[str, Any]],
    required_column: bool,
) -> None:
    """Print one row per declared field: flag, type, default, secrecy, description.

    ``required_column`` is on for channels, whose declarations carry
    requiredness; provider fields do not declare it.
    """
    table = Table(title=title)
    table.add_column("Flag", style="cyan", no_wrap=True)
    table.add_column("Type", overflow="fold")
    table.add_column("Default", no_wrap=True)
    if required_column:
        table.add_column("Required?", no_wrap=True, justify="center")
    table.add_column("Secret?", no_wrap=True, justify="center")
    table.add_column("Description", overflow="fold")
    for path, spec in specs.items():
        flag = "--" + path.replace("_", "-")
        default = spec["default"]
        default_str = "" if default in (None, "", [], {}) else str(default)
        row = [flag, spec["type"], default_str]
        if required_column:
            row.append("\u2713" if spec.get("required") else "")
        row.append("\u2713" if spec["is_secret"] else "")
        row.append(spec.get("description", "") or "")
        table.add_row(*row)
    console.print(table)
