"""Chat channel cluster of the onboard wizard (Step 3).

Split out of ``onboard_commands`` because that module had grown past 5000
lines; this file owns channel selection, credential prompting, scancode
login, and channel management end to end. Shared wizard UI state
(``console``, ``_t``, ``_BACK``, ``_QMARK``, questionary helpers, ...) still
lives in ``onboard_commands`` -- this module reaches it via the ``oc`` module
reference (not a value import) so that test monkeypatches on
``onboard_commands`` attributes keep working whichever module a caller
patches through.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from raven.cli import onboard_commands as oc


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
_CHANNEL_CRED_HELP: dict[str, tuple[str, str]] = {
    "telegram": (
        "Create a bot with @BotFather in Telegram (send /newbot) — it replies with the token.",
        "在 Telegram 里找 @BotFather 发 /newbot 创建机器人,它会回复 token。",
    ),
    "discord": (
        "Discord Developer Portal → your app → Bot → Reset Token to copy it.",
        "Discord 开发者门户 → 你的应用 → Bot → Reset Token 复制。",
    ),
    "slack": (
        "api.slack.com/apps → OAuth & Permissions gives bot_token (xoxb-…); "
        "Basic Information → App-Level Tokens gives app_token (xapp-…).",
        "api.slack.com/apps → OAuth & Permissions 拿 bot_token(xoxb-…);"
        "Basic Information → App-Level Tokens 拿 app_token(xapp-…)。",
    ),
    "feishu": (
        "Feishu / Lark Open Platform → your app → Credentials for App ID & App Secret.",
        "飞书开放平台 → 你的应用 → 凭证与基础信息 拿 App ID / App Secret。",
    ),
    "wecom": (
        "WeCom admin console → your bot / app for its ID and secret.",
        "企业微信管理后台 → 机器人 / 应用 拿 ID 和 secret。",
    ),
    "dingtalk": (
        "DingTalk Open Platform → your app for Client ID & Client Secret.",
        "钉钉开放平台 → 你的应用 拿 Client ID / Client Secret。",
    ),
    "qq": (
        "QQ Open Platform → your bot for App ID & secret.",
        "QQ 开放平台 → 你的机器人 拿 App ID 和 secret。",
    ),
    "email": (
        "Use your mail provider's IMAP / SMTP settings; for Gmail / Outlook create an app password.",
        "用你邮箱服务商的 IMAP / SMTP 设置;Gmail / Outlook 需创建应用专用密码。",
    ),
    "matrix": (
        "From your Matrix account: an access token and your full user id (@you:server).",
        "从你的 Matrix 账号获取 access token 和完整用户 id(@you:server)。",
    ),
    "mochat": (
        "Get the claw token and agent user id from your Mochat workspace.",
        "从你的 Mochat 工作区获取 claw token 和 agent user id。",
    ),
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
    choices.append(questionary.Choice(oc._t("Back", "返回"), value=oc._BACK))
    picked = questionary.select(oc._t("Channel:", "渠道:"), choices=choices, style=RAVEN_STYLE, qmark=oc._QMARK).ask()
    return picked


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
        if path != "enabled" and spec.get("type", "") == "str" and spec.get("default") in ("", None)
    ]
    if promptable:
        names = ", ".join(path for path, _ in promptable)
        oc.console.print(
            oc._t(
                f"  [dim]Configuring {channel} — fill in:[/dim] {names}",
                f"  [dim]正在配置 {channel} — 请填写:[/dim] {names}",
            )
        )
        help_text = _CHANNEL_CRED_HELP.get(channel)
        if help_text:
            oc.console.print(
                oc._t(
                    f"  [dim]Where to get it: {help_text[0]}[/dim]",
                    f"  [dim]去哪拿:{help_text[1]}[/dim]",
                )
            )
    else:
        oc.console.print(
            oc._t(
                f"  [dim]{channel} needs no credentials; enabling.[/dim]",
                f"  [dim]{channel} 无需填写凭证,正在启用。[/dim]",
            )
        )

    fields: dict[str, Any] = {}
    for idx, (path, spec) in enumerate(promptable):
        required = bool(spec.get("required"))
        description = spec.get("description", "")
        opt_tag = "" if required else oc._t(" (optional)", " (可选)")
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
                oc.console.print(
                    oc._t(f"  [yellow]{path} is required.[/yellow]", f"  [yellow]{path} 为必填项。[/yellow]")
                )
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
        oc.console.print(oc._t(f"  [red]✗ Validation failed:[/red]\n{exc}", f"  [red]✗ 校验失败:[/red]\n{exc}"))
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
        oc._t(
            f"  [yellow]✗ Node.js / npm not found (the {channel} bridge needs it). "
            "Install Node.js, then retry.[/yellow]",
            f"  [yellow]✗ 未找到 Node.js / npm({channel} 的桥接需要它)。请先安装 Node.js,再重试。[/yellow]",
        )
    )
    choice = oc._failure_choice(
        [
            (oc._t("Retry after install", "安装后重试"), "retry"),
            (oc._t("Skip", "跳过"), "skip"),
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
        oc.console.print(oc._t(f"  [red]✗ Unknown channel: {channel}[/red]", f"  [red]✗ 未知渠道:{channel}[/red]"))
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
                    oc._t(
                        f"  [dim]Skipped {channel}; install Node.js then run raven channels login {channel}.[/dim]",
                        f"  [dim]已跳过 {channel};装好 Node.js 后运行 raven channels login {channel}。[/dim]",
                    )
                )
                return

            from raven.config.loader import load_config

            channel_cfg = getattr(load_config().channels, channel, None)
            if channel_cfg is None:
                oc.console.print(
                    oc._t(
                        f"  [red]✗ No config section for channel: {channel}[/red]",
                        f"  [red]✗ 渠道 {channel} 没有配置段。[/red]",
                    )
                )
                return
            adapter = spec.factory(channel_cfg)
            if channel == "whatsapp":
                oc.console.print(
                    oc._t(
                        "  [dim]Building the WhatsApp bridge — the first run can take 30–120s…[/dim]",
                        "  [dim]正在构建 WhatsApp 桥接,首次约需 30–120 秒…[/dim]",
                    )
                )
            oc.console.print(
                oc._t(
                    f"  [dim]Starting {spec.display_name} QR login…[/dim]",
                    f"  [dim]正在启动 {spec.display_name} 扫码登录…[/dim]",
                )
            )
            oc.console.print(
                oc._t(
                    f"  [dim]A login link / QR code will appear below — scan it with "
                    f"{spec.display_name} (or open the link on a phone signed in to "
                    f"{spec.display_name}) to connect. This waits until you finish.[/dim]",
                    f"  [dim]下方会出现登录链接 / 二维码 — 用 {spec.display_name} 扫码"
                    f"(或在已登录 {spec.display_name} 的手机上打开该链接)即可接入;"
                    f"这里会一直等到你完成。[/dim]",
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
                oc.console.print(
                    oc._t(
                        f"  [yellow]✗ Login failed: {exc}[/yellow]",
                        f"  [yellow]✗ 登录失败:{exc}[/yellow]",
                    )
                )
                ok = False
            finally:
                _wiz_logger.disable(_login_log_scope)
            if ok:
                oc.console.print(
                    oc._t(
                        f"  [green]✓ Logged in; {channel} connected.[/green]",
                        f"  [green]✓ 已登录;{channel} 已接入。[/green]",
                    )
                )
                logged_in = True
                return
            choice = oc._failure_choice(
                [
                    (oc._t("Retry", "重试"), "retry"),
                    (oc._t("Skip this channel", "跳过此渠道"), "skip"),
                ],
                non_interactive=non_interactive,
            )
            if choice == "retry":
                continue
            oc.console.print(
                oc._t(
                    f"  [dim]{channel} not connected — finish later with raven channels login {channel}.[/dim]",
                    f"  [dim]{channel} 未接入 — 之后用 raven channels login {channel} 完成。[/dim]",
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
        oc.console.print(oc._t(f"  [green]✓ {channel} enabled.[/green]", f"  [green]✓ {channel} 已启用。[/green]"))
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
        choices.append(questionary.Choice(oc._t("Back", "返回"), value=oc._BACK))
        target = questionary.select(
            oc._t("Pick a channel to manage:", "选择要管理的渠道:"),
            choices=choices,
            style=RAVEN_STYLE,
            qmark=oc._QMARK,
        ).ask()
        if target is None or target is oc._BACK:
            return
        action = questionary.select(
            oc._t(f"What would you like to do with {target}?", f"对 {target} 想做什么?"),
            choices=[
                questionary.Choice(oc._t("Edit config (re-enter fields)", "编辑配置(重填字段)"), value="edit"),
                questionary.Choice(oc._t("Disable (keep credentials)", "停用(保留凭证)"), value="disable"),
                questionary.Choice(oc._t("Back", "返回"), value=oc._BACK),
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
            oc.console.print(
                oc._t(
                    f"  [green]✓ {target} config updated.[/green]",
                    f"  [green]✓ {target} 配置已更新。[/green]",
                )
            )
        elif action == "disable":
            disable_channel(target)
            oc.console.print(
                oc._t(
                    f"  [green]✓ Disabled {target} (credentials kept; re-enable later "
                    f"with raven channels enable {target}).[/green]",
                    f"  [green]✓ 已停用 {target}(凭证保留;之后用 raven channels enable {target} 重新启用)。[/green]",
                )
            )


def _step3_channel(*, channel: Optional[str], skip: bool, non_interactive: bool) -> object:
    """Step 3 — optionally enable chat channel(s)."""
    oc._step_header(
        3,
        oc._t(
            "(Optional) Connect a messaging app so you can chat with Raven there",
            "(可选)接入即时通讯软件,直接在里面和 Raven 聊天",
        ),
    )

    if skip:
        oc.console.print(
            oc._t(
                "  [dim]Skipped via --skip-channel.[/dim]",
                "  [dim]已通过 --skip-channel 跳过。[/dim]",
            )
        )
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
        oc.console.print(
            oc._t(
                "  [dim]Skipped (non-interactive, --channel not given).[/dim]",
                "  [dim]已跳过(非交互且未提供 --channel)。[/dim]",
            )
        )
        return None

    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    if channel:
        if _channel_uses_interactive_login(channel):
            _scancode_login(channel, non_interactive=non_interactive)
        else:
            fields = _prompt_channel_fields(channel)
            if fields is oc._BACK:
                oc.console.print(oc._t("  [dim]Skipped.[/dim]", "  [dim]已跳过。[/dim]"))
                return None
            _enable_channel(channel, fields)
            oc.console.print(
                oc._t(
                    f"  [green]✓ {channel} enabled.[/green]",
                    f"  [green]✓ {channel} 已启用。[/green]",
                )
            )
        return None

    while True:
        enabled = _enabled_channels()
        if not enabled:
            action = questionary.select(
                oc._t("Connect a chat channel?", "接入一个聊天渠道吗?"),
                choices=[
                    questionary.Choice(oc._t("Add a channel", "新增一个渠道"), value="add"),
                    questionary.Choice(
                        oc._t(
                            "Skip (add later with raven channels enable)",
                            "跳过(之后用 raven channels enable 添加)",
                        ),
                        value="skip",
                    ),
                ],
                style=RAVEN_STYLE,
                qmark=oc._QMARK,
            ).ask()
            if action is None:
                raise typer.Exit(1)
            if action == "skip":
                oc.console.print(oc._t("  [dim]Skipped.[/dim]", "  [dim]已跳过。[/dim]"))
                return None
            _add_one_channel(non_interactive=non_interactive)
            continue

        action = questionary.select(
            oc._t(
                f"Chat channel already connected: {', '.join(enabled)}. What would you like to do?",
                f"聊天渠道已接入:{', '.join(enabled)}。想做什么?",
            ),
            choices=[
                questionary.Choice(oc._t("Done, next step", "完成,下一步"), value="done"),
                questionary.Choice(oc._t("Add a channel", "新增一个渠道"), value="add"),
                questionary.Choice(oc._t("Edit / remove a channel", "编辑 / 移除渠道"), value="edit"),
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
