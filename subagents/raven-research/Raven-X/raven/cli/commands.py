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
from raven.cli.provider_commands import provider_app
from raven.cli.sandbox_commands import sandbox_app
from raven.cli.sentinel_commands import sentinel_app
from raven.cli.skill_commands import skill_app

app.add_typer(channels_app, name="channels")
app.add_typer(cron_app, name="cron")
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

    Runs the Typer app, then settles the native runtimes the loop started
    before the interpreter finalizes: the stoppable ones are shut down, and
    only an unstoppable one left live (lancedb's Rust/tokio thread) forces a
    hard exit past finalization. See :mod:`raven.cli._exit`.

    Every exit path converges on that one settle step, which is the fix for
    what the earlier shape got wrong: a ``raise SystemExit`` from inside one of
    the ``except`` blocks below is not caught by their sibling handler, so those
    paths -- and any unhandled exception -- finalized unguarded and segfaulted
    (exit 139), replacing the real exit code. Measured on both.

    CliRunner invokes ``app`` directly and never reaches this wrapper, so
    in-process test hosts keep normal exit semantics.
    """
    from raven.cli._exit import flush_and_hard_exit, settle_native_runtimes
    from raven.config.loader import ConfigReadError
    from raven.providers.auth import MissingCredentialsError

    code: int | None = None
    try:
        app()
    except MissingCredentialsError as exc:
        # The gate is decided in `providers.auth` because more than one entry
        # point asks it; printing and exiting is this one's idiom, so it happens
        # here rather than there. Rendered once for every command, like
        # ConfigReadError.
        from raven.cli._helpers import console

        console.print(f"[red]Error: {exc.summary}.[/red]")
        if exc.remedy:
            console.print(exc.remedy)
        code = 1
    except ConfigReadError as exc:
        # A config-write command (channels/provider/onboard) hit an
        # unparseable config. The write layer already refused (file untouched);
        # surface it cleanly here, once, for every command instead of a traceback.
        from rich.console import Console

        Console(stderr=True).print(f"[red]✗[/red] {exc}")
        code = 1
    except SystemExit as exc:
        raw = exc.code
        if raw is None or isinstance(raw, int):
            code = raw or 0
        else:
            # `sys.exit("message")`: the payload *is* the message, and the
            # interpreter would print it before exiting 1. Re-raising a plain
            # int below would drop it, so print it here instead.
            print(raw, file=sys.stderr)
            code = 1
    except BaseException:
        # Rendered here rather than left to the interpreter: reaching
        # finalization is exactly what this function may have to prevent, and a
        # hard exit would take the traceback with it. Through `sys.excepthook`
        # so typer's rich traceback survives (it renders from the config typer
        # attaches to the exception in `Typer.__call__`).
        exc_info = sys.exc_info()
        try:
            sys.excepthook(*exc_info)
        except Exception:
            import traceback

            traceback.print_exception(*exc_info)
        # An interrupt's conventional code, which the interpreter produces by
        # re-raising SIGINT; click usually converts it to Abort/1 first, so this
        # covers the interrupts that land outside click's own guard.
        code = 130 if isinstance(exc_info[1], KeyboardInterrupt) else 1

    if settle_native_runtimes():
        flush_and_hard_exit(code or 0)
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    run()
