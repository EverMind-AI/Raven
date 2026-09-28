"""Raven CLI entry-point.

This module wires together every top-level command and every subcommand
group. The actual implementations live in per-feature modules:

- Top-level commands (each exposes a ``register(app)`` function):
    - ``agent``    → ``raven/cli/agent_commands.py``
    - ``doctor``   → ``raven/cli/doctor_commands.py``
    - ``gateway``  → ``raven/cli/gateway_commands.py``
    - ``onboard``  → ``raven/cli/onboard_commands.py``
    - ``status``   → ``raven/cli/status_commands.py``
    - ``upgrade``  → ``raven/cli/upgrade_commands.py``

- Subcommand groups (each exposes a typer ``*_app`` instance):
    - ``agents``   → ``raven/cli/agents_commands.py`` (mounted via its ``register(app)``)
    - ``channels`` → ``raven/cli/channel_commands.py``
    - ``cron``     → ``raven/cli/cron_commands.py``
    - ``provider`` → ``raven/cli/provider_commands.py``
    - ``sandbox``  → ``raven/cli/sandbox_commands.py``
    - ``sentinel`` → ``raven/cli/sentinel_commands.py``
    - ``sessions`` → ``raven/cli/session_commands.py``
    - ``import``   → ``raven/cli/import_commands.py``
    - ``skill``    → ``raven/cli/skill_commands.py``
    - ``trajectory`` → ``raven/cli/trajectory_commands.py``

Shared helpers used across multiple command modules live in
``raven/cli/_helpers.py`` (rendering) and ``raven/core/config_stack.py`` /
``raven/core/provider_stack.py`` (assembly).
"""

import os
import sys

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import typer
from rich.console import Console

from raven import __logo__, __version__

app = typer.Typer(
    name="raven",
    help=f"{__logo__} Raven - Agent Framework",
    no_args_is_help=False,
    invoke_without_command=True,
)
console = Console()


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} Raven v{__version__}")
        raise typer.Exit()


def _saved_language() -> str:
    """The ``language`` a config on disk carries, ``en`` when there is none yet or it cannot be read."""
    try:
        import json

        from raven.config.paths import get_config_path

        return str(json.loads(get_config_path().read_text(encoding="utf-8")).get("language") or "en")
    except Exception:
        return "en"


def _can_open_a_browser() -> bool:
    """Whether this machine has a browser to hand the page to.

    ``webbrowser.get()`` raises when nothing is registered, which is exactly the
    question worth asking before anything is started: on a headless box the page
    would come up on a URL the reader has no way to open, while the TUI is the
    surface that still works there.
    """
    import webbrowser

    try:
        webbrowser.get()
    except webbrowser.Error:
        return False
    return True


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(None, "--version", "-v", callback=version_callback, is_eager=True),
):
    """Raven - Agent Framework.

    Bare ``raven`` (no subcommand) opens the page, the same as ``raven web``:
    that is where the installer ends, so the command a reader types the next day
    should not land them somewhere else.

    Where no browser can be opened the page would be a URL nobody can reach, so
    the bare command runs the native TUI instead -- through the ``tui`` callback,
    so the startup gate (onboard when provider+model are missing, else launch the
    session) is identical either way. ``raven web`` and ``raven tui`` still name
    a surface outright and are unaffected.
    """
    i18n.set_language(_saved_language())
    if ctx.invoked_subcommand is not None:
        return
    if _can_open_a_browser():
        from raven.cli.serve_commands import _web
        from raven.rpc.transports.ws import DEFAULT_PORT

        _web(DEFAULT_PORT)
        return
    console.print("No browser to open the page with; starting the terminal UI. `raven web` prints the URL instead.")
    from raven.cli.tui_commands import tui as _tui_entry

    # Delegate to the exact `raven tui` callback so the onboarding gate and
    # launch behavior are identical for both entry points. Every option of
    # `tui` must be passed an explicit plain default: its typer.Option
    # defaults are OptionInfo sentinels that only typer resolves, and an
    # omitted one arrives here as a sentinel that reads as "flag was set".
    _tui_entry(
        ctx,
        check=False,
        dev=False,
        color=None,
        print_colors=False,
        preview_colors=False,
        workspace=None,
        home=None,
        standalone=False,
    )


# ============================================================================
# Top-level command registrations
# ============================================================================

from raven.cli import (
    agent_commands,
    agents_commands,
    doctor_commands,
    gateway_commands,
    onboard_commands,
    plugin_commands,
    serve_commands,
    status_commands,
    tracing_commands,
    upgrade_commands,
)

onboard_commands.register(app)
gateway_commands.register(app)
agent_commands.register(app)
agents_commands.register(app)
status_commands.register(app)
doctor_commands.register(app)
plugin_commands.register(app)
serve_commands.register(app)
tracing_commands.register(app)
upgrade_commands.register(app)


# ============================================================================
# Subcommand registrations
# ============================================================================

from raven.cli.a2a_commands import a2a_app
from raven.cli.acp_commands import acp_app
from raven.cli.channel_commands import channels_app
from raven.cli.cron_commands import cron_app
from raven.cli.mcp_commands import mcp_app
from raven.cli.ops_connection_commands import ops_app
from raven.cli.playbook_commands import playbook_app
from raven.cli.provider_commands import provider_app
from raven.cli.sandbox_commands import sandbox_app
from raven.cli.sentinel_commands import sentinel_app
from raven.cli.skill_commands import skill_app
from raven.cli.trajectory_commands import trajectory_app

app.add_typer(a2a_app, name="a2a")
app.add_typer(acp_app, name="acp")
app.add_typer(channels_app, name="channels")
app.add_typer(cron_app, name="cron")
app.add_typer(ops_app, name="ops")
app.add_typer(mcp_app, name="mcp")
app.add_typer(playbook_app, name="playbook")
app.add_typer(provider_app, name="provider")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(sentinel_app, name="sentinel")
app.add_typer(skill_app, name="skill")
app.add_typer(trajectory_app, name="trajectory")


from raven.cli.tui_commands import tui_app

app.add_typer(tui_app, name="tui")

from raven.cli.session_commands import session_app

app.add_typer(session_app, name="sessions")

# Singular `plugin` beside the existing plural `plugins` listing: the group holds
# per-server actions (`plugin auth <server>`), which is a different verb shape
# from "show me what is installed".
from raven.cli.plugin_commands import plugin_app

app.add_typer(plugin_app, name="plugin")

from raven import i18n
from raven.cli.import_commands import import_app

app.add_typer(import_app, name="import")


def run() -> None:
    """Console-script entry point."""
    from raven.config.loader import ConfigReadError
    from raven.providers.auth import MissingCredentialsError

    try:
        app()
    except MissingCredentialsError as exc:
        # The gate is decided in `providers.auth` because three entry points ask
        # it; printing and exiting is this one's idiom, so it happens here rather
        # than there. Rendered once for every command, like ConfigReadError.
        from raven.cli._helpers import console

        console.print(f"[red]Error: {exc.summary}.[/red]")
        if exc.remedy:
            console.print(exc.remedy)
        raise SystemExit(1) from exc
    except ConfigReadError as exc:
        # A config-write command (channels/provider/onboard) hit an
        # unparseable config. The write layer already refused (file untouched);
        # surface it cleanly here, once, for every command instead of a traceback.
        from rich.console import Console

        Console(stderr=True).print(f"[red]✗[/red] {exc}")
        raise SystemExit(1) from exc
    finally:
        # Every command loads the config, so any of them can be the one that
        # migrates it -- `status`, `provider list`, `cron list`. Only `agent` and
        # `gateway` say so up front, and an unsaid notice is lost rather than
        # deferred: the watermark leaves the next load with nothing to report.
        # So this is the catch-all for every other command.
        from raven.cli._helpers import print_config_migration_notices

        print_config_migration_notices()


if __name__ == "__main__":
    run()
