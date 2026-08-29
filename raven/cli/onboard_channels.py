"""Chat channel cluster of the onboard wizard (Step 3).

Split out of ``onboard_commands`` because that module had grown past 5000
lines; this file owns channel selection, credential prompting, scancode
login, and channel management end to end. Shared wizard UI state
(``console``, ``_BACK``, ``_QMARK``, questionary helpers, ...) still
lives in ``onboard_commands`` -- this module reaches it via the ``oc`` module
reference (not a value import) so that test monkeypatches on
``onboard_commands`` attributes keep working whichever module a caller
patches through.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from raven.cli import _onboard_shared as oc
from raven.i18n import t


def _enabled_channels() -> list[str]:
    """Names of channels currently enabled on disk."""
    data = oc._load_raw_config()
    channels = data.get("channels") or {}
    return [name for name, c in channels.items() if isinstance(c, dict) and c.get("enabled")]


# Curated channel order: China-domestic first, then overseas. Channels not
# listed (e.g. a newly added adapter) fall to the end in alphabetical order so
# the picker never silently hides one.
# Display order: US/global-common → China-common → US/global-uncommon →
# China-uncommon. (Email is a universal but less-common-as-IM channel, so it
# sits in the uncommon tail.)
_CHANNEL_ORDER = (
    # US / global, common
    "telegram",
    "discord",
    "slack",
    "whatsapp",
    # China, common
    "weixin",
    "wecom",
    "feishu",
    "dingtalk",
    "qq",
    # US / global, less common
    "matrix",
    "email",
    # China, niche
    "mochat",
)


# Where to obtain each channel's credentials — shown (dim) before the field
# prompts so the user knows where to fetch the token / keys.
_CHANNEL_CRED_HELP: dict[str, str] = {
    "telegram": "Create a bot with @BotFather in Telegram (send /newbot) — it replies with the token.",
    "discord": "Discord Developer Portal → your app → Bot → Reset Token to copy it.",
    "slack": "api.slack.com/apps → OAuth & Permissions gives bot_token (xoxb-…); "
    "Basic Information → App-Level Tokens gives app_token (xapp-…).",
    "feishu": "Feishu / Lark Open Platform → your app → Credentials for App ID & App Secret.",
    "wecom": "WeCom admin console → your bot / app for its ID and secret.",
    "dingtalk": "DingTalk Open Platform → your app for Client ID & Client Secret.",
    "qq": "QQ Open Platform → your bot for App ID & secret.",
    "email": "Use your mail provider's IMAP / SMTP settings; for Gmail / Outlook create an app password.",
    "matrix": "From your Matrix account: an access token and your full user id (@you:server).",
    "mochat": "Get the claw token and agent user id from your Mochat workspace.",
}


def _ordered_channel_names() -> list[str]:
    from raven.channels.registry import discover_channel_names

    rank = {name: i for i, name in enumerate(_CHANNEL_ORDER)}
    return sorted(discover_channel_names(), key=lambda n: (rank.get(n, len(rank)), n))


def _select_channel() -> Optional[str]:
    """List available channels via the registry and let the user pick one."""
    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    names = _ordered_channel_names()
    choices = [questionary.Choice(n, value=n) for n in names]
    choices.append(questionary.Choice(t("Back"), value=oc._BACK))
    picked = questionary.select(t("Channel:"), choices=choices, style=RAVEN_STYLE, qmark=oc._QMARK).ask()
    return picked


# Channel fields this step must not treat as credentials. They are empty by
# default like a credential is, but each has a working default resolved
# elsewhere, so asking for them here would put an unexplained prompt ahead of
# the tokens the user actually came to enter.
_NON_CREDENTIAL_CHANNEL_FIELDS = frozenset({"enabled", "workspace"})


def _prompt_channel_fields(channel: str) -> Any:
    """Reflect a channel's Pydantic schema and prompt for credential-like fields."""
    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.config.update_channels import channel_field_specs

    try:
        specs = channel_field_specs(channel)
    except KeyError as exc:
        oc.console.print(f"  [red]✗[/red] {exc}")
        raise typer.Exit(1)

    # Pre-scan which credential fields we'll ask for, so we can tell the user
    # up front what's being configured (and handle the zero-field case).
    promptable = [
        (path, spec)
        for path, spec in specs.items()
        if path not in _NON_CREDENTIAL_CHANNEL_FIELDS
        and spec.get("type", "") == "str"
        and spec.get("default") in ("", None)
    ]
    if promptable:
        names = ", ".join(path for path, _ in promptable)
        oc.console.print(t("  [dim]Configuring {channel} — fill in:[/dim] {names}", channel=channel, names=names))
        help_text = _CHANNEL_CRED_HELP.get(channel)
        if help_text:
            oc.console.print(t("  [dim]Where to get it: {help}[/dim]", help=t(help_text)))
    else:
        oc.console.print(t("  [dim]{channel} needs no credentials; enabling.[/dim]", channel=channel))

    fields: dict[str, Any] = {}
    for idx, (path, spec) in enumerate(promptable):
        required = bool(spec.get("required"))
        description = spec.get("description", "")
        opt_tag = "" if required else t(" (optional)")
        prompt_label = f"{path}{opt_tag}" + (f" — {description}" if description else "") + ":"
        # First field's empty submit rewinds to the channel picker; a later
        # optional field's empty submit skips it; a later required field re-prompts
        # (empty was previously accepted silently, enabling a half-configured
        # channel — the write layer treats "required" as a UX marker only).
        allow_back = idx == 0
        placeholder = oc._field_placeholder(allow_back, required)
        while True:
            if spec.get("is_secret"):
                value = questionary.password(
                    prompt_label, placeholder=placeholder, style=RAVEN_STYLE, qmark=oc._QMARK
                ).ask()
            else:
                value = questionary.text(
                    prompt_label, placeholder=placeholder, style=RAVEN_STYLE, qmark=oc._QMARK
                ).ask()
            if value is None:
                raise typer.Exit(1)
            value = value.strip()
            if value:
                fields[path] = value
                break
            if allow_back:
                return oc._BACK  # first field empty → back to the channel picker
            if required:
                oc.console.print(t("  [yellow]{path} is required.[/yellow]", path=path))
                continue  # re-prompt instead of enabling a channel missing a credential
            break  # optional field: empty submit skips it
    return fields


def _enable_channel(channel: str, fields: dict[str, Any]) -> None:
    """Thin wrapper for ``enable_channel`` that surfaces ops errors with hints."""
    from pydantic import ValidationError

    from raven.config.update_channels import enable_channel

    try:
        enable_channel(channel, fields)
    except KeyError as exc:
        oc.console.print(f"  [red]✗[/red] {exc}")
        raise typer.Exit(1)
    except ValidationError as exc:
        oc.console.print(t("  [red]✗ Validation failed:[/red]\n{exc}", exc=exc))
        raise typer.Exit(1)


def _channel_uses_interactive_login(channel: str) -> bool:
    """True for scancode/QR channels (WeChat / WhatsApp) that pair via a live
    login flow rather than reflected credential fields."""
    try:
        from raven.channels.registry import discover_specs

        spec = discover_specs().get(channel)
        return bool(spec and spec.capabilities.interactive_login)
    except Exception:
        return False


# Scancode channels whose QR login is served by a Node.js bridge — these need
# Node/npm present before login can even start. The whatsapp adapter's
# ``login`` checks ``shutil.which("npm")`` and merely logs+returns False when
# it's absent, so we detect the missing-runtime case up front to show a
# meaningful "install Node / skip" menu rather than a pointless "re-show QR".
_NODE_BRIDGE_CHANNELS = {"whatsapp"}


def _node_runtime_missing(channel: str) -> bool:
    """True iff ``channel`` needs a Node bridge and ``npm`` isn't on PATH."""
    if channel not in _NODE_BRIDGE_CHANNELS:
        return False
    import shutil

    return shutil.which("npm") is None


def _handle_missing_node(channel: str, *, non_interactive: bool) -> str:
    """Show the Node-missing submenu (install-then-retry / skip).

    Returns ``"retry"`` (re-check after install) or ``"skip"`` (leave the
    channel enabled-but-unauthenticated). A pointless "re-show QR" is
    intentionally absent — there's no bridge to render a QR without Node.
    """
    oc.console.print(
        t(
            "  [yellow]✗ Node.js / npm not found (the {channel} bridge needs it). Install Node.js, then retry.[/yellow]",
            channel=channel,
        )
    )
    choice = oc._failure_choice(
        [
            (t("Retry after install"), "retry"),
            (t("Skip"), "skip"),
        ],
        non_interactive=non_interactive,
    )
    return choice


def _scancode_login(channel: str, *, non_interactive: bool = False) -> None:
    """Run a scancode channel's real QR login (reuses ``channel.login``).

    Mirrors ``raven channels login``: enable the channel so its config section
    persists, build the adapter via its spec factory, then drive
    ``await channel.login()`` (which for WhatsApp builds the bridge, displays
    the QR, and waits). A failed / timed-out login drops into a numbered
    submenu (retry / skip). Node-bridge channels missing Node/npm get a
    dedicated install-then-retry menu instead.
    """
    import asyncio

    from raven.channels.registry import discover_specs
    from raven.config.update_channels import disable_channel

    # Enable first so the config section exists for the factory to read while we
    # attempt login. We REVERT this (disable) on any path that doesn't complete
    # login, so a cancelled / skipped scan never shows up as "connected".
    _enable_channel(channel, {})

    specs = discover_specs()
    spec = specs.get(channel)
    if spec is None:
        disable_channel(channel)
        oc.console.print(t("  [red]✗ Unknown channel: {channel}[/red]", channel=channel))
        return

    # Enabled above so the factory can read the config section during login. ANY
    # path that doesn't finish login must revert the enable — including Ctrl+C in
    # a submenu (raises typer.Exit) or mid-scan (KeyboardInterrupt), neither an
    # ``Exception`` subclass — so wrap the whole flow and disable in ``finally``
    # unless we actually logged in.
    logged_in = False
    try:
        while True:
            # Node-bridge channels: gate on the runtime up front so a missing
            # Node/npm shows a useful install menu, not a "re-show QR" no-op.
            if _node_runtime_missing(channel):
                if _handle_missing_node(channel, non_interactive=non_interactive) == "retry":
                    continue
                oc.console.print(
                    t(
                        "  [dim]Skipped {channel}; install Node.js then run raven channels login {channel}.[/dim]",
                        channel=channel,
                    )
                )
                return

            from raven.config.loader import load_config

            channel_cfg = getattr(load_config().channels, channel, None)
            if channel_cfg is None:
                oc.console.print(t("  [red]✗ No config section for channel: {channel}[/red]", channel=channel))
                return
            from raven.config.admission import dispense_channel_config

            # Through the same admission door the gateway uses: the raw
            # section carries only socket fields since the central cargo
            # classes retired, and the factory expects the dispensed view.
            adapter = spec.factory(dispense_channel_config(spec, channel_cfg, channel=channel))
            if channel == "whatsapp":
                oc.console.print(t("  [dim]Building the WhatsApp bridge — the first run can take 30–120s…[/dim]"))
            oc.console.print(t("  [dim]Starting {a0} QR login…[/dim]", a0=spec.display_name))
            oc.console.print(
                t(
                    "  [dim]A login link / QR code will appear below — scan it with {a0} (or open the link on a phone signed in to {a0}) to connect. This waits until you finish.[/dim]",
                    a0=spec.display_name,
                )
            )
            from loguru import logger as _wiz_logger

            # The wizard silences raven logs for a clean UI, but a scancode login
            # emits its QR / link / progress / failure reason through loguru. Re-
            # enable ONLY this channel's adapter subtree for the login attempt (not
            # all of raven, which would dump unrelated noise), then restore quiet.
            _login_log_scope = f"raven.channels.adapters.{channel}"
            try:
                _wiz_logger.enable(_login_log_scope)
                ok = asyncio.run(adapter.login(force=True))
            except Exception as exc:
                oc.console.print(t("  [yellow]✗ Login failed: {exc}[/yellow]", exc=exc))
                ok = False
            finally:
                _wiz_logger.disable(_login_log_scope)
            if ok:
                oc.console.print(t("  [green]✓ Logged in; {channel} connected.[/green]", channel=channel))
                logged_in = True
                return
            choice = oc._failure_choice(
                [
                    (t("Retry"), "retry"),
                    (t("Skip this channel"), "skip"),
                ],
                non_interactive=non_interactive,
            )
            if choice == "retry":
                continue
            oc.console.print(
                t(
                    "  [dim]{channel} not connected — finish later with raven channels login {channel}.[/dim]",
                    channel=channel,
                )
            )
            return
    finally:
        if not logged_in:
            # Any non-login exit (skip, no-config, submenu Ctrl+C, mid-scan
            # interrupt) reverts the enable so a cancelled scan never persists as
            # "connected". The config section is kept for `raven channels login`.
            disable_channel(channel)


def _add_one_channel(*, non_interactive: bool = False) -> None:
    """Pick + (scancode login | reflect-prompt) + enable one channel."""
    while True:
        channel = _select_channel()
        if channel is None or channel is oc._BACK:
            return
        if _channel_uses_interactive_login(channel):
            _scancode_login(channel, non_interactive=non_interactive)
            return
        fields = _prompt_channel_fields(channel)
        if fields is oc._BACK:
            continue  # backed out of the first field — re-pick a channel
        _enable_channel(channel, fields)
        oc.console.print(t("  [green]✓ {channel} enabled.[/green]", channel=channel))
        return


def _manage_existing_channels() -> None:
    """Edit/disable submenu for already-enabled channels."""
    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.config.update_channels import disable_channel, set_channel_fields

    while True:
        enabled = _enabled_channels()
        if not enabled:
            return
        choices = [questionary.Choice(n, value=n) for n in enabled]
        choices.append(questionary.Choice(t("Back"), value=oc._BACK))
        target = questionary.select(
            t("Pick a channel to manage:"),
            choices=choices,
            style=RAVEN_STYLE,
            qmark=oc._QMARK,
        ).ask()
        if target is None or target is oc._BACK:
            return
        action = questionary.select(
            t("What would you like to do with {target}?", target=target),
            choices=[
                questionary.Choice(t("Edit config (re-enter fields)"), value="edit"),
                questionary.Choice(t("Disable (keep credentials)"), value="disable"),
                questionary.Choice(t("Back"), value=oc._BACK),
            ],
            style=RAVEN_STYLE,
            qmark=oc._QMARK,
        ).ask()
        if action is None or action is oc._BACK:
            continue
        if action == "edit":
            fields = _prompt_channel_fields(target)
            if fields is oc._BACK:
                continue  # backed out — return to the manage menu
            if fields:
                set_channel_fields(target, fields)
            oc.console.print(t("  [green]✓ {target} config updated.[/green]", target=target))
        elif action == "disable":
            disable_channel(target)
            oc.console.print(
                t(
                    "  [green]✓ Disabled {target} (credentials kept; re-enable later with raven channels enable {target}).[/green]",
                    target=target,
                )
            )


def _step3_channel(*, channel: Optional[str], skip: bool, non_interactive: bool) -> object:
    """Step 3 — optionally enable chat channel(s)."""
    oc._step_header(
        3,
        t("(Optional) Connect a messaging app so you can chat with Raven there"),
    )

    if skip:
        oc.console.print(t("  [dim]Skipped via --skip-channel.[/dim]"))
        return None

    if non_interactive:
        if channel:
            oc.console.print(
                f"[red]--channel {channel} given but non-interactive mode can't "
                "prompt for credential fields.[/red]\n"
                f"Run [accent]raven channels enable {channel} --<field> <value> ...[/accent] "
                "after onboard finishes."
            )
            raise typer.Exit(2)
        oc.console.print(t("  [dim]Skipped (non-interactive, --channel not given).[/dim]"))
        return None

    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    if channel:
        if _channel_uses_interactive_login(channel):
            _scancode_login(channel, non_interactive=non_interactive)
        else:
            fields = _prompt_channel_fields(channel)
            if fields is oc._BACK:
                oc.console.print(t("  [dim]Skipped.[/dim]"))
                return None
            _enable_channel(channel, fields)
            oc.console.print(t("  [green]✓ {channel} enabled.[/green]", channel=channel))
        return None

    while True:
        enabled = _enabled_channels()
        if not enabled:
            action = questionary.select(
                t("Connect a chat channel?"),
                choices=[
                    questionary.Choice(t("Add a channel"), value="add"),
                    questionary.Choice(
                        t("Skip (add later with raven channels enable)"),
                        value="skip",
                    ),
                ],
                style=RAVEN_STYLE,
                qmark=oc._QMARK,
            ).ask()
            if action is None:
                raise typer.Exit(1)
            if action == "skip":
                oc.console.print(t("  [dim]Skipped.[/dim]"))
                return None
            _add_one_channel(non_interactive=non_interactive)
            continue

        action = questionary.select(
            t("Chat channel already connected: {a0}. What would you like to do?", a0=", ".join(enabled)),
            choices=[
                questionary.Choice(t("Done, next step"), value="done"),
                questionary.Choice(t("Add a channel"), value="add"),
                questionary.Choice(t("Edit / remove a channel"), value="edit"),
            ],
            style=RAVEN_STYLE,
            qmark=oc._QMARK,
        ).ask()
        if action is None:
            raise typer.Exit(1)
        if action == "done":
            return None
        if action == "add":
            _add_one_channel(non_interactive=non_interactive)
        elif action == "edit":
            _manage_existing_channels()
