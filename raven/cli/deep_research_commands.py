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
from raven.config.update_tools import ConfigReadError, get_deep_research, reset_deep_research, set_deep_research

SIGNUP_URL = "https://platform.miromind.ai/console/api-keys"

_FLAGSHIP_MODEL = "mirothinker-1-7-deepresearch"

deep_research_app = typer.Typer(help="Configure the deep_research tool (MiroThinker).", no_args_is_help=True)


def _validate_key(api_key: str, api_base: str, *, transport: Any = None) -> dict[str, Any]:
    """Free ``GET /v1/models`` key check. 200 = key valid; returns model ids.

    Mirrors update_providers' models-test, including its ``/v1`` de-dup: the
    MiroThinker default base already ends in ``/v1``, so appending ``/v1/models``
    blindly would 404. ``transport`` is injectable for tests.
    """
    import httpx

    from raven.agent.tools.deep_research import DEFAULT_BASE_URL

    base = (api_base or DEFAULT_BASE_URL).rstrip("/")
    url = base + "/models"
    if "/v1" not in base:
        url = base + "/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    kwargs: dict[str, Any] = {"timeout": 10}
    if transport is not None:
        kwargs["transport"] = transport
    try:
        with httpx.Client(**kwargs) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "status": "network_error", "model_ids": None, "error": str(exc)}
    if resp.status_code != 200:
        return {"ok": False, "status": f"http_{resp.status_code}", "model_ids": None, "error": resp.text[:200]}
    ids: list[str] = []
    try:
        for item in resp.json().get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                ids.append(item["id"])
    except Exception:
        pass
    return {"ok": True, "status": "ok", "model_ids": ids, "error": None}


def _pick_model(questionary: Any, style: Any, qmark: str, t: Any) -> str:
    # A short description aligned after each id. The shared 256k/16k window is
    # left out -- it is identical for both, so it is noise here and only bloats
    # the label (which questionary truncates rather than wraps).
    width = max(len(DEFAULT_MODEL), len(_FLAGSHIP_MODEL))
    choices = [
        questionary.Choice(
            f"{DEFAULT_MODEL.ljust(width)}   {t('faster, lower cost', '更快、更省')} ({t('recommended', '推荐')})",
            value=DEFAULT_MODEL,
        ),
        questionary.Choice(
            f"{_FLAGSHIP_MODEL.ljust(width)}   {t('deeper reasoning, broader tools', '更强推理、更广工具')}",
            value=_FLAGSHIP_MODEL,
        ),
    ]
    picked = questionary.select(t("Select a model:", "选择 model:"), choices=choices, style=style, qmark=qmark).ask()
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
    from raven.cli.onboard_commands import _QMARK, _prompt_api_key, _require_questionary, _t, console

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
            _t("deep_research is already configured.", "deep_research 已配置。"),
            choices=[
                q.Choice(_t("Keep current", "保持现有"), value="keep"),
                q.Choice(_t("Reconfigure", "重新配置"), value="reconfigure"),
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
            _t("Enable deep_research (MiroThinker)?", "启用 deep_research(MiroThinker)?"),
            choices=[
                q.Choice(_t("Yes, configure it", "是,配置"), value="configure"),
                q.Choice(_t("Skip for now", "暂时跳过"), value="skip"),
            ],
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if choice is None:
            raise typer.Exit(1)  # Ctrl+C
        if choice == "skip":
            return False

    console.print(
        _t(
            f"Create an API key at [link={SIGNUP_URL}]{SIGNUP_URL}[/link], then paste it below.",
            f"到 [link={SIGNUP_URL}]{SIGNUP_URL}[/link] 创建 API key,然后粘贴到下面。",
        )
    )

    while True:
        key = _prompt_api_key("deep_research")
        res = _validate_key(key, current["api_base"])
        if res["ok"]:
            break
        console.print(f"[yellow]⚠[/yellow] {_t('Key validation failed', 'Key 验证失败')}: {res['status']}")
        action = q.select(
            _t("What now?", "怎么办?"),
            choices=[
                q.Choice(_t("Re-enter key", "重新输入 key"), value="retry"),
                q.Choice(_t("Save anyway", "仍然保存"), value="save"),
                q.Choice(_t("Cancel", "取消"), value="cancel"),
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

    model = _pick_model(q, RAVEN_STYLE, _QMARK, _t)
    set_deep_research({"api_key": key, "model": model})
    console.print(f"[green]✓[/green] {_t('deep_research configured', 'deep_research 已配置')}")
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
