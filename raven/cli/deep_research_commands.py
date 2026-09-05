"""``raven deep-research`` — configure the MiroThinker deep_research tool.

Subcommands: ``enable`` / ``get`` / ``reset``. ``enable`` with flags writes
directly (non-interactive); with no flags it runs the interactive wizard flow,
which is shared verbatim with onboard's deep_research step.

Config writes go through ``raven.config.update_tools`` only. Key validation is a
free ``GET /v1/models`` (NOT a chat completion — MiroThinker's chat endpoint
always runs a real, minute-scale, billed research, so a chat "ping" would both
time out and cost money).
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from raven.agent.tools.deep_research import DEFAULT_MODEL
from raven.cli._tty_guard import die_if_not_tty
from raven.config.update_tools import ConfigReadError, get_deep_research, reset_deep_research, set_deep_research
from raven.i18n import t

SIGNUP_URL = "https://platform.miromind.ai/console/api-keys"

_FLAGSHIP_MODEL = "mirothinker-1-7-deepresearch"

deep_research_app = typer.Typer(help="Configure the deep_research tool (MiroThinker).", no_args_is_help=True)


def _validate_key(api_key: str, api_base: str, *, transport: Any = None) -> dict[str, Any]:
    """Free ``GET /v1/models`` key check against MiroThinker's base."""
    from raven.agent.tools.deep_research import DEFAULT_BASE_URL
    from raven.cli._key_probe import probe_models

    return probe_models(api_key, api_base or DEFAULT_BASE_URL, transport=transport)


def _pick_model(questionary: Any, style: Any, qmark: str) -> str:
    # A short description aligned after each id. The shared 256k/16k window is
    # left out -- it is identical for both, so it is noise here and only bloats
    # the label (which questionary truncates rather than wraps).
    width = max(len(DEFAULT_MODEL), len(_FLAGSHIP_MODEL))
    choices = [
        questionary.Choice(
            f"{DEFAULT_MODEL.ljust(width)}   {t('faster, lower cost')} ({t('recommended')})",
            value=DEFAULT_MODEL,
        ),
        questionary.Choice(
            f"{_FLAGSHIP_MODEL.ljust(width)}   {t('deeper reasoning, broader tools')}",
            value=_FLAGSHIP_MODEL,
        ),
    ]
    picked = questionary.select(t("Select a model:"), choices=choices, style=style, qmark=qmark).ask()
    if picked is None:
        raise typer.Exit(1)  # Ctrl+C
    return picked


def configure_deep_research(*, non_interactive: bool = False, warnings: Optional[list[str]] = None) -> bool:
    """Interactive configure flow, shared by ``enable`` (no flags) and onboard.

    Returns True when a key was configured this run (or kept), False when
    skipped/cancelled. Non-interactive with no key to work from just records a
    warning and skips (the caller handles flag-driven writes directly).
    """
    warnings = warnings if warnings is not None else []
    from raven.cli._styles import RAVEN_STYLE
    from raven.cli.onboard_commands import _BACK, _QMARK, _prompt_api_key, _require_questionary, console

    if non_interactive:
        warnings.append("deep_research: skipped (non-interactive; pass --key to configure)")
        return False

    try:
        current = get_deep_research(redact=False)
    except ConfigReadError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1) from exc
    q = _require_questionary()

    if current["api_key"]:
        choice = q.select(
            t("deep_research is already configured."),
            choices=[
                q.Choice(t("Keep current"), value="keep"),
                q.Choice(t("Reconfigure"), value="reconfigure"),
            ],
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if choice is None:
            raise typer.Exit(1)  # Ctrl+C
        if choice == "keep":
            return True
    else:
        choice = q.select(
            t("Enable deep_research (MiroThinker)?"),
            choices=[
                q.Choice(t("Yes, configure it"), value="configure"),
                q.Choice(t("Skip for now"), value="skip"),
            ],
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if choice is None:
            raise typer.Exit(1)  # Ctrl+C
        if choice == "skip":
            return False

    console.print(
        t("Create an API key at [link={SIGNUP_URL}]{SIGNUP_URL}[/link], then paste it below.", SIGNUP_URL=SIGNUP_URL)
    )

    while True:
        key = _prompt_api_key(
            "deep_research",
            allow_back=True,
            back_label=t("empty ↵ to cancel"),
        )
        if key is _BACK:
            return False  # empty submit cancels configuration
        res = _validate_key(key, current["api_base"])
        if res["ok"]:
            break
        console.print(f"[yellow]⚠[/yellow] {t('Key validation failed')}: {res['status']}")
        action = q.select(
            t("What now?"),
            choices=[
                q.Choice(t("Re-enter key"), value="retry"),
                q.Choice(t("Save anyway"), value="save"),
                q.Choice(t("Cancel"), value="cancel"),
            ],
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if action is None:
            raise typer.Exit(1)  # Ctrl+C
        if action == "retry":
            continue
        if action == "save":
            break
        return False  # explicit cancel

    model = _pick_model(q, RAVEN_STYLE, _QMARK)
    set_deep_research({"api_key": key, "model": model})
    console.print(f"[green]✓[/green] {t('deep_research configured')}")
    return True


@deep_research_app.command("enable")
def enable_cmd(
    key: Optional[str] = typer.Option(None, "--key", help="MiroThinker API key."),
    model: Optional[str] = typer.Option(None, "--model", help="Model id (defaults to the mini engine)."),
    api_base: Optional[str] = typer.Option(None, "--api-base", help="Override the API base URL."),
) -> None:
    """Configure deep_research. No flags -> interactive wizard."""
    from rich.console import Console

    console = Console()

    if key is None and model is None and api_base is None:
        die_if_not_tty("raven deep-research enable --key <key>")
        configure_deep_research(non_interactive=False)
        return

    fields: dict[str, Any] = {}
    if key is not None:
        fields["api_key"] = key
    if api_base is not None:
        fields["api_base"] = api_base
    if model is not None:
        fields["model"] = model
    elif key is not None:
        fields["model"] = DEFAULT_MODEL  # --key without --model: default to the mini engine

    try:
        set_deep_research(fields)
    except ConfigReadError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print("[green]✓[/green] deep_research configured")
    if fields.get("api_key"):
        res = _validate_key(fields["api_key"], fields.get("api_base") or "")
        if not res["ok"]:
            console.print(f"[yellow]⚠[/yellow] key validation: {res['status']} (config saved anyway)")


@deep_research_app.command("get")
def get_cmd() -> None:
    """Print the current deep_research config (key redacted)."""
    from rich.console import Console

    console = Console()
    try:
        cfg = get_deep_research(redact=True)
    except ConfigReadError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"api_key : {cfg['api_key']}")
    console.print(f"api_base: {cfg['api_base'] or '(default)'}")
    console.print(f"model   : {cfg['model'] or '(default: ' + DEFAULT_MODEL + ')'}")


@deep_research_app.command("reset")
def reset_cmd() -> None:
    """Clear the deep_research key (new sessions start with the setup offer)."""
    from rich.console import Console

    console = Console()
    try:
        reset_deep_research()
    except ConfigReadError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print("[green]✓[/green] deep_research reset (key cleared; new sessions start with the setup offer)")
