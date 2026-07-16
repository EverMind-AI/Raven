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
    - ``channels`` → ``raven/cli/channel_commands.py``
    - ``cron``     → ``raven/cli/cron_commands.py``
    - ``provider`` → ``raven/cli/provider_commands.py``
    - ``sandbox``  → ``raven/cli/sandbox_commands.py``
    - ``sentinel`` → ``raven/cli/sentinel_commands.py``
    - ``sessions`` → ``raven/cli/session_commands.py``
    - ``skill``    → ``raven/cli/skill_commands.py``

Shared helpers used across multiple command modules live in
``raven/cli/_helpers.py``.
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

# LiteLLM prints a red-bold "Provider List: ..." banner to stdout when it
# can't match a model prefix. For our custom-provider setup this fires on
# every call, clashes with prompt_toolkit's rendered prompt, and shows up
# as ?[1;31m... garbage when patch_stdout is active. Silence it.
try:
    import litellm

    litellm.suppress_debug_info = True
except Exception:
    pass

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


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(None, "--version", "-v", callback=version_callback, is_eager=True),
):
    """Raven - Agent Framework.

    Bare ``raven`` (no subcommand) is equivalent to ``raven tui``: it runs the
    same startup gate (onboard when provider+model are missing, else launch the
    session) and then enters the native TUI. Both paths share the identical
    pre-launch check by routing through the ``tui`` callback.
    """
    if ctx.invoked_subcommand is not None:
        return
    from raven.cli.tui_commands import tui as _tui_entry

    # Delegate to the exact `raven tui` callback so the onboarding gate and
    # launch behavior are identical for both entry points. Pass explicit
    # plain defaults (the function's typer.Option defaults are OptionInfo
    # sentinels, only resolved when typer drives the command).
    _tui_entry(
        ctx,
        check=False,
        dev=False,
        color=None,
        print_colors=False,
        preview_colors=False,
    )


# ============================================================================
# Top-level command registrations
# ============================================================================

from raven.cli import (
    agent_commands,
    doctor_commands,
    gateway_commands,
    onboard_commands,
    plugin_commands,
    status_commands,
    tracing_commands,
    upgrade_commands,
)

onboard_commands.register(app)
gateway_commands.register(app)
agent_commands.register(app)
status_commands.register(app)
doctor_commands.register(app)
plugin_commands.register(app)
tracing_commands.register(app)
upgrade_commands.register(app)


# ============================================================================
# Subcommand registrations
# ============================================================================

from raven.cli.channel_commands import channels_app
from raven.cli.cron_commands import cron_app
from raven.cli.deep_research_commands import deep_research_app
from raven.cli.provider_commands import provider_app
from raven.cli.sandbox_commands import sandbox_app
from raven.cli.sentinel_commands import sentinel_app
from raven.cli.skill_commands import skill_app

app.add_typer(channels_app, name="channels")
app.add_typer(cron_app, name="cron")
app.add_typer(deep_research_app, name="deep-research")
app.add_typer(provider_app, name="provider")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(sentinel_app, name="sentinel")
app.add_typer(skill_app, name="skill")


from raven.cli.tui_commands import tui_app

app.add_typer(tui_app, name="tui")

from raven.cli.session_commands import session_app

app.add_typer(session_app, name="sessions")


def run() -> None:
    """Console-script entry point.

    Runs the Typer app, then hard-exits past CPython interpreter finalization
    when a native runtime that segfaults at finalization is live (lancedb's
    Rust/tokio background thread — see :mod:`raven.cli._exit`). Any command that
    builds the agent loop starts that thread, so guarding here covers them all
    at once. CliRunner invokes ``app`` directly and never reaches this wrapper,
    so in-process test hosts keep normal exit semantics.
    """
    from raven.cli._exit import flush_and_hard_exit, lancedb_finalization_hazard
    from raven.config.loader import ConfigReadError

    try:
        app()
    except ConfigReadError as exc:
        # A config-write command (channels/provider/deep-research/onboard) hit an
        # unparseable config. The write layer already refused (file untouched);
        # surface it cleanly here, once, for every command instead of a traceback.
        from rich.console import Console

        Console(stderr=True).print(f"[red]✗[/red] {exc}")
        raise SystemExit(1) from exc
    except SystemExit as exc:
        code = exc.code
        if not isinstance(code, int):
            code = 0 if code is None else 1
        if lancedb_finalization_hazard():
            flush_and_hard_exit(code)
        raise


if __name__ == "__main__":
    run()
