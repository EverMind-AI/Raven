"""Five-step onboarding wizard: LLM provider → sandbox → channel → memory → deep research.

Goal: get a new user from ``pip install`` to a working agent in a few
minutes, without ever opening ``~/.raven/config.json`` or
``~/.everos/raven/everos.toml``.

Steps (mirrors ``my_docs/temp/onboard-flow.mermaid``):
  0. Welcome
  1. LLM provider (required; multi-provider, in-step connectivity + test probe)
  2. Sandbox / run location (optional, single-select)
  3. Chat channel (optional, stackable)
  4. EverOS long-term memory (optional; llm/embedding required once enabled,
     rerank/multimodal optional)
  5. deep_research tool (optional; MiroThinker key + model)
  6. Done

All writes go through the ``update_providers`` / ``update_channels`` /
``update`` / ``update_everos`` ops libraries — this module owns the UX layer,
not config-schema knowledge.

Navigation: questionary 2.1.1 has no first-class cross-screen "back", so the
wizard is a screen state machine and back is expressed as a ``0) back``
sentinel choice on the screens that support it (Step 1 <-> language pick,
Step 2 -> Step 1); Steps 3, 4 and 5 are optional and forward-only (re-run
``onboard`` to change them). Ctrl+C exits at any point, keeping whatever was
already written.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Optional

import typer
from rich.console import Console
from rich.panel import Panel

from raven.cli._helpers import (
    DEFAULT_PROBE_MESSAGE,
    print_probe_troubleshooting,
    send_probe,
)

console = Console()

_TOTAL_STEPS = 5

# Sentinel returned by a screen function to ask the runner to go back one
# screen; ``None`` from a picker means Ctrl+C (exit).
_BACK = object()

# Sentinel a required EverOS role returns when the user chooses to give up EverOS
# rather than configure it; ``_step4_memory`` then falls back to Markdown memory.
_ABORT_EVEROS = object()

# Unified prompt chrome (display-only): no leading question glyph (drops
# questionary's default "?"). A single-space qmark is rendered as one blank,
# which — with questionary's own leading space — puts every prompt line on the
# same 2-space column as our printed help/status lines, so the left edge stays
# flush instead of jittering between 1- and 2-space indents. Pointer is a
# calmer "❯" than questionary's default "»".
_QMARK = " "
_POINTER = "❯"

# UI language, chosen on the wizard's first screen. ``_t`` returns the English
# or Chinese variant so every later prompt / message stays bilingual.
_LANG = "en"


def _t(en: str, zh: str) -> str:
    """Return ``zh`` when the user picked Chinese, else ``en``."""
    return zh if _LANG == "zh" else en


# ---------------------------------------------------------------------------
# Curated provider catalogue surfaced in Step 1's picker.
# ---------------------------------------------------------------------------


_CURATED_PROVIDERS: list[dict[str, Any]] = [
    {
        "name": "openrouter",
        "label": "OpenRouter (recommended — one key, many models)",
        "label_zh": "OpenRouter(推荐 · 一个 Key 调用多家模型)",
        "is_oauth": False,
    },
    {"name": "openai", "label": "OpenAI", "label_zh": "OpenAI", "is_oauth": False},
    {"name": "anthropic", "label": "Anthropic", "label_zh": "Anthropic", "is_oauth": False},
    {"name": "gemini", "label": "Gemini", "label_zh": "Gemini", "is_oauth": False},
    {"name": "deepseek", "label": "DeepSeek", "label_zh": "DeepSeek", "is_oauth": False},
    {
        "name": "github_copilot",
        "label": "GitHub Copilot (OAuth)",
        "label_zh": "GitHub Copilot(OAuth 登录)",
        "is_oauth": True,
    },
    {
        "name": "openai_codex",
        "label": "Codex (OAuth)",
        "label_zh": "Codex(OAuth 登录)",
        "is_oauth": True,
    },
    {
        "name": "custom",
        "label": "Other (OpenAI-compatible endpoint)",
        "label_zh": "其他(OpenAI 兼容端点)",
        "is_oauth": False,
    },
]

_QUESTIONARY_INSTALL_HINT = (
    "[red]Missing dependency:[/red] [#fbe23f]questionary[/#fbe23f] is required for "
    "interactive onboarding.\n"
    "Install it with: [#fbe23f]uv add 'questionary>=2.0,<3.0'[/#fbe23f]\n"
    "Or re-run with [#fbe23f]--non-interactive[/#fbe23f] plus the relevant flags."
)


_PROMPT_THEMED = False


def _theme_questionary(questionary: Any) -> None:
    """Give every ``select`` a consistent pointer and drop questionary's own
    "(Use arrow keys)" hint — the step header already prints the controls.

    Display-only and applied once: we wrap ``questionary.select`` so callers
    that don't pass ``pointer`` / ``instruction`` inherit the unified look,
    while any explicit value still wins (``setdefault``).
    """
    global _PROMPT_THEMED
    if _PROMPT_THEMED:
        return
    import functools

    _orig_select = questionary.select

    @functools.wraps(_orig_select)
    def _themed_select(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("pointer", _POINTER)
        # questionary shows "(Use arrow keys)" when instruction is falsy; a
        # single space is truthy yet visually blank, so it hides that hint
        # (the step header already prints the controls).
        kwargs.setdefault("instruction", " ")
        return _orig_select(*args, **kwargs)

    questionary.select = _themed_select
    _PROMPT_THEMED = True


def _require_questionary() -> Any:
    """Lazy-import :mod:`questionary` so missing-package errors stay scoped here."""
    try:
        import questionary
    except ModuleNotFoundError:
        console.print(_QUESTIONARY_INSTALL_HINT)
        raise typer.Exit(1)
    _theme_questionary(questionary)
    return questionary


def _config_language() -> str:
    """Read the saved UI language from the on-disk config ('en' / 'zh').

    A missing / empty config (fresh install) defaults to 'en'; a malformed one
    raises ConfigReadError (surfaced by the CLI entrypoint) rather than being
    silently read as empty.
    """
    data = _load_raw_config()
    lang = data.get("language")
    return lang if lang in ("en", "zh") else "en"


def _pick_language() -> None:
    """First screen: choose the wizard's language. Updates module-level ``_LANG``.

    Persistence happens later (after bootstrap created the config file), via
    ``set_language`` in :func:`_run_wizard_body`.
    """
    global _LANG
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    # Framed like the other screens (bilingual, since no language is chosen yet)
    # so it reads as the wizard's first step, not a bare floating list.
    console.print()
    console.print(
        Panel(
            "[bold white]Let's set up Raven — first, choose your language.[/bold white]\n"
            "[dim]开始配置 Raven — 请先选择语言。[/dim]",
            title="[bold #fbe23f]Raven setup[/bold #fbe23f]",
            title_align="left",
            border_style="#c8a900",
            padding=(1, 2),
        )
    )
    console.print("  [dim]↑↓ select · Enter confirm · Ctrl+C quit[/dim]")
    console.print()

    picked = questionary.select(
        "Language / 语言",
        choices=[
            questionary.Choice("English", value="en"),
            questionary.Choice("中文(简体)", value="zh"),
        ],
        default=_LANG,  # preselect the saved language on a re-run
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()
    if picked is None:
        raise typer.Exit(1)
    _LANG = picked


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _step_header(n: int, title: str) -> None:
    # Progress dots: filled for done/current steps, hollow for upcoming ones.
    dots = " ".join("[#fbe23f]●[/#fbe23f]" if i <= n else "[grey37]○[/grey37]" for i in range(1, _TOTAL_STEPS + 1))
    console.print()
    console.print(
        Panel(
            f"[bold white]{title}[/bold white]",
            title=f"[bold #fbe23f]{_t('Step', '步骤')} {n}/{_TOTAL_STEPS}[/bold #fbe23f]",
            title_align="left",
            subtitle=dots,
            subtitle_align="right",
            border_style="#c8a900",
            padding=(0, 2),
        )
    )
    console.print()  # breathing room between the header and the step's prompts


def _check_tty_or_die(non_interactive: bool) -> None:
    """Bail when stdout isn't a TTY and the user didn't opt into headless mode."""
    if non_interactive:
        return
    if not sys.stdout.isatty():
        console.print(
            "[red]Non-interactive terminal detected.[/red]\n"
            "Re-run with: "
            "[#fbe23f]raven onboard --non-interactive --provider <name> --api-key <key>[/#fbe23f]"
        )
        raise typer.Exit(2)


def _load_raw_config() -> dict[str, Any]:
    """Return the parsed on-disk config, or ``{}`` if absent/empty.

    A present-but-unparseable config raises ConfigReadError (surfaced cleanly by
    the CLI entrypoint) instead of being silently treated as empty -- which
    would let onboard misread state and write over a config whose only fault is
    a syntax typo.
    """
    from raven.config.loader import get_config_path, read_raw_or_raise

    return read_raw_or_raise(get_config_path()) or {}


def _configured_providers() -> list[str]:
    """Names of providers that currently have an api_key set on disk."""
    data = _load_raw_config()
    providers = data.get("providers") or {}
    return [name for name, p in providers.items() if isinstance(p, dict) and p.get("apiKey")]


def _is_config_populated() -> bool:
    """True iff at least one provider has a key AND a default model is set.

    "Populated" for the startup gate means the required step (Step 1) is
    satisfied: a provider key plus ``agents.defaults.model``. Either alone is
    not enough to talk to a model.
    """
    data = _load_raw_config()
    providers = data.get("providers") or {}
    has_provider = any(isinstance(p, dict) and p.get("apiKey") for p in providers.values())
    model = (data.get("agents", {}) or {}).get("defaults", {}).get("model")
    return bool(has_provider and model)


def _handle_existing_config(*, reset: bool, yes: bool, non_interactive: bool) -> None:
    """Guard against silently overwriting an existing config in non-interactive
    runs.

    Interactive runs always fall through into the structured wizard: every step
    defaults to "Keep current" for already-set values, so pressing Enter all the
    way through is equivalent to skipping, and changing any value reconfigures
    just that one. No separate skip/redo/quit screen — it would drop the wizard's
    welcome banner and step framing.
    """
    if reset:
        return
    if not _is_config_populated():
        return

    if non_interactive:
        if yes:
            console.print("[dim]Existing config detected; --yes set, proceeding with overwrite.[/dim]")
            return
        console.print(
            "[red]Existing config detected.[/red] Pass [#fbe23f]--reset[/#fbe23f] (or "
            "[#fbe23f]--yes[/#fbe23f]) to overwrite, or edit in place with "
            "[#fbe23f]raven provider set[/#fbe23f] / [#fbe23f]raven channels enable[/#fbe23f]."
        )
        raise typer.Exit(2)
    # Interactive: fall through to the wizard (per-step "Keep current" handles
    # the existing config gracefully).


def _bootstrap_empty_config() -> None:
    """Make sure ``~/.raven/config.json`` + workspace dir exist before we patch.

    We seed the user-facing extension defaults (memory / plugins / skillForge),
    including ``memory.backend = "everos"`` (the schema default). EverOS
    degrades gracefully when its models aren't configured yet (empty recall + a
    warning, never a crash), so an enabled-but-modelless install is safe. The
    wizard's Step 4 — and its skip / non-interactive guard — resolve the backend
    back to ``None`` when the user opts out or never configures the required
    models (``_memory_enabled`` gates on both required models (llm + embedding)
    being present, not just the backend name).

    Seeding runs on EVERY onboard, not just a brand-new config: the writer is
    ``setdefault``-based (non-clobbering), so it backfills these blocks into a
    pre-existing config that predates them without touching any value the user
    already set. The base ``Config()`` is only written when the file is absent —
    overwriting an existing file there would clobber it.
    """
    from raven.config.loader import get_config_path, load_config, save_config
    from raven.config.paths import get_workspace_path
    from raven.utils.helpers import sync_workspace_templates

    path = get_config_path()
    if not path.exists():
        save_config(load_config())  # writes default Config() to disk
    _init_extension_block_defaults()
    workspace = get_workspace_path()
    workspace.mkdir(parents=True, exist_ok=True)
    sync_workspace_templates(workspace)


# ---------------------------------------------------------------------------
# Step 1 — provider primitives (reused verbatim from the 3-step wizard)
# ---------------------------------------------------------------------------


def _provider_label(name: str) -> str:
    """Display label for a provider, falling back to the registry's display_name."""
    for entry in _CURATED_PROVIDERS:
        if entry["name"] == name:
            return _t(entry["label"], entry.get("label_zh", entry["label"]))
    try:
        from raven.providers.registry import find_by_name

        spec = find_by_name(name)
        return spec.label if spec else name
    except Exception:
        return name


def _validate_provider_name(name: str) -> str:
    """Resolve a user-supplied provider name (kebab or snake) to a registry key."""
    from raven.config.update_providers import provider_field_specs

    candidate = name.replace("-", "_")
    try:
        provider_field_specs(candidate)
    except KeyError as exc:
        raise typer.BadParameter(str(exc))
    return candidate


def _back_placeholder(allow_back: bool) -> Any:
    """A faint in-field placeholder telling the user an empty submit rewinds.

    Rendered greyed inside the input (via prompt_toolkit's ``placeholder``),
    it disappears the moment they type and leaves nothing behind once the
    prompt is answered. Returns ``None`` when back isn't offered.
    """
    if not allow_back:
        return None
    return [("fg:#6c6c6c italic", _t("empty ↵ to go back", "留空回车返回上一步"))]


def _field_placeholder(allow_back: bool, required: bool) -> Any:
    """In-field hint for a channel credential prompt.

    First field: empty submit rewinds to the channel picker (back). Later
    optional fields: empty submit skips them. Required later fields get no
    hint — an empty submit there silently drops a value the channel needs.
    """
    if allow_back:
        return _back_placeholder(True)
    if not required:
        return [("fg:#6c6c6c italic", _t("empty ↵ to skip", "留空回车跳过"))]
    return None


def _collect_fields(prompts: list[Callable[[], Any]]) -> Optional[list[Any]]:
    """Run text-prompt callables in order with empty-submit = back.

    Each callable prompts one field and returns its value, or ``_BACK`` (an
    empty submit) to rewind one field. Backing out of the first field returns
    ``None`` so the caller can rewind to the preceding screen. Returns the list
    of collected values on success.
    """
    values: list[Any] = []
    i = 0
    while i < len(prompts):
        value = prompts[i]()
        if value is _BACK:
            if i == 0:
                return None
            values.pop()
            i -= 1
            continue
        if i < len(values):
            values[i] = value
        else:
            values.append(value)
        i += 1
    return values


def _select_provider() -> Optional[str]:
    """Interactive provider picker built from the curated catalogue.

    Returns the provider name, ``_BACK`` if the user chose the back sentinel,
    or ``None`` on Ctrl+C.
    """
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    choices: list[Any] = [
        questionary.Choice(
            _t(entry["label"], entry.get("label_zh", entry["label"])),
            value=entry["name"],
        )
        for entry in _CURATED_PROVIDERS
    ]
    choices.append(questionary.Separator())
    choices.append(questionary.Choice(_t("Back", "返回"), value=_BACK))

    picked = questionary.select(
        _t("Provider:", "服务商:"),
        choices=choices,
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()
    return picked  # None on Ctrl+C


def _prompt_api_key(provider: str, *, allow_back: bool = False) -> Any:
    """Ask for an API key (hidden input). Returns ``_BACK`` on empty submit
    when ``allow_back`` is set, else the key string."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    def _validate(v: str) -> Any:
        if allow_back and v == "":
            return True  # empty is the back signal, not an error
        return (
            True
            if len(v) >= 8
            else _t(
                "API key looks off (empty or too short) — please re-enter (≥ 8 chars).",
                "API Key 看起来不对(过短或为空),请重新输入(至少 8 位)。",
            )
        )

    key = questionary.password(
        _t("Paste your API key:", "粘贴你的 API Key:"),
        validate=_validate,
        placeholder=_back_placeholder(allow_back),
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()
    if key is None:
        raise typer.Exit(1)
    key = key.strip()
    if allow_back and key == "":
        return _BACK
    if not key:
        raise typer.Exit(1)
    return key


def _prompt_base_url(default: str = "https://", *, allow_back: bool = False) -> Any:
    """Ask for an OpenAI-compatible base URL (used by the 'custom' provider).
    Returns ``_BACK`` on empty submit when ``allow_back`` is set."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    # With back enabled, don't seed a default — an empty field must be reachable
    # so the user can submit nothing to rewind.
    seed = "" if allow_back else default

    def _validate(v: str) -> Any:
        if allow_back and v == "":
            return True
        return (
            True
            if v.startswith(("http://", "https://"))
            else _t("URL must start with http:// or https://", "地址需以 http:// 或 https:// 开头")
        )

    url = questionary.text(
        _t("Base URL (must include /v1):", "Base URL(需包含 /v1):"),
        default=seed,
        validate=_validate,
        placeholder=_back_placeholder(allow_back),
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()
    if url is None:
        raise typer.Exit(1)
    url = url.strip()
    if allow_back and url == "":
        return _BACK
    if not url:
        raise typer.Exit(1)
    return url


def _prompt_custom_model(*, allow_back: bool = False) -> Any:
    """Ask for the model name when using a custom OpenAI-compatible endpoint.
    Returns ``_BACK`` on empty submit when ``allow_back`` is set."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    def _validate(v: str) -> Any:
        if allow_back and v.strip() == "":
            return True
        return True if v.strip() else _t("Model id is required for custom endpoints.", "自定义端点必须指定模型 id。")

    model = questionary.text(
        _t(
            "Default model id (e.g. 'gpt-3.5-turbo' or 'qwen-max'):",
            "默认模型 id(如 'gpt-3.5-turbo' 或 'qwen-max'):",
        ),
        validate=_validate,
        placeholder=_back_placeholder(allow_back),
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()
    if model is None:
        raise typer.Exit(1)
    if allow_back and model.strip() == "":
        return _BACK
    if not model:
        raise typer.Exit(1)
    return model.strip()


def _run_oauth_login(provider: str) -> bool:
    """Dispatch the OAuth login handler registered by ``provider_commands``.

    Returns ``True`` on success. A login that fails (the handler raises
    ``typer.Exit`` or any error) returns ``False`` so the caller can offer a
    retry / back menu instead of tearing the whole wizard down. A genuine
    Ctrl+C (``KeyboardInterrupt``) is left to propagate as a quit.
    """
    from raven.cli.provider_commands import _LOGIN_HANDLERS
    from raven.providers.registry import find_by_name

    spec = find_by_name(provider)
    if not spec or not spec.is_oauth:
        console.print(
            _t(
                f"  [red]✗ {provider} is not an OAuth provider.[/red]",
                f"  [red]✗ {provider} 不是 OAuth 服务商。[/red]",
            )
        )
        raise typer.Exit(1)
    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(
            _t(
                f"  [red]✗ No login handler registered for {provider}.[/red]",
                f"  [red]✗ 未为 {provider} 注册登录处理器。[/red]",
            )
        )
        raise typer.Exit(1)
    console.print(
        _t(
            f"  [#fbe23f]Starting OAuth login for {spec.label}…[/#fbe23f]\n",
            f"  [#fbe23f]正在为 {spec.label} 启动 OAuth 登录…[/#fbe23f]\n",
        )
    )
    console.print(
        _t(
            "  [dim]A browser window / link will open — finish the sign-in there, "
            "then come back here. This waits until you're done.[/dim]\n",
            "  [dim]会打开浏览器窗口 / 链接 — 在那里完成登录后回到这里;这里会一直等到你完成。[/dim]\n",
        )
    )
    try:
        handler()
    except typer.Exit as exc:
        # Handlers signal a failed login with Exit(1); Exit(0) (if any) is success.
        if exc.exit_code:
            return False
    except Exception as exc:  # network / browser / token errors — recoverable
        console.print(
            _t(
                f"  [yellow]✗ Login didn't complete: {exc}[/yellow]",
                f"  [yellow]✗ 登录未完成:{exc}[/yellow]",
            )
        )
        return False
    return True


def _verify_provider(provider: str, *, skip_test: bool = False) -> tuple[bool, str, Optional[list[str]]]:
    """Hit ``GET /v1/models`` to verify the credentials we just stored.

    Returns ``(ok, status, model_ids)``. ``status`` is one of the ops-library
    failure codes (``invalid_key`` / ``no_credits`` / ``rate_limited`` /
    ``network_error`` / …) and drives the failure submenu's wording.
    """
    from raven.config.update_providers import test_provider as probe

    console.print(_t("  [dim]⏳ Verifying your API key…[/dim]", "  [dim]⏳ 正在验证 API Key…[/dim]"))
    result = probe(provider)
    if result["ok"]:
        models = result.get("models_count")
        suffix = _t(f" ({models} models available)", f"(共 {models} 个可用模型)") if models else ""
        console.print(_t(f"  [green]✓ Connected!{suffix}[/green]", f"  [green]✓ 连接成功!{suffix}[/green]"))
        return True, "valid", result.get("model_ids")

    status = result.get("status", "unknown")
    # Some direct providers (openai / anthropic / deepseek / gemini) ship no
    # base URL and rely on the SDK's built-in endpoint, so there's nothing to
    # hit for a GET /v1/models pre-check — the probe reports "not_configured"
    # because api_base is empty. That's NOT a real auth failure: skip the pre-
    # check (the test message sent later exercises real connectivity via
    # litellm) instead of dumping the user into the failure submenu.
    if status == "not_configured" and "api_base" in (result.get("error") or ""):
        if skip_test:
            console.print(
                _t(
                    "  [dim]Skipping the model-list pre-check (this provider has no public /models endpoint); connectivity is not tested (--skip-test).[/dim]",
                    "  [dim]跳过模型列表预检(该服务商无公开 /models 端点);未做连通测试(--skip-test)。[/dim]",
                )
            )
        else:
            console.print(
                _t(
                    "  [dim]Skipping the model-list pre-check (this provider has no public /models endpoint); the test message below will confirm connectivity.[/dim]",
                    "  [dim]跳过模型列表预检(该服务商无公开 /models 端点);稍后的测试消息会验证连通。[/dim]",
                )
            )
        return True, "skipped", None
    hint_map = {
        "invalid_key": _t(
            "Auth failed: the API key is invalid — check for typos / stray spaces.",
            "鉴权失败:API Key 无效 — 检查有无拼写错误或多余空格。",
        ),
        "no_credits": _t(
            "Account out of credits or not provisioned — top up and retry.",
            "账户余额不足或未开通 — 充值后重试。",
        ),
        "rate_limited": _t(
            "Rate limited — wait a bit and retry, or switch provider.",
            "触发限流 — 稍等后重试,或更换服务商。",
        ),
        "network_error": _t(
            "Network error reaching the provider — check network / proxy / VPN.",
            "连接服务商时网络出错 — 检查网络 / 代理 / VPN。",
        ),
        "oauth_token_missing": _t(
            f"Run: raven provider login {provider.replace('_', '-')}",
            f"请运行:raven provider login {provider.replace('_', '-')}",
        ),
    }
    msg = hint_map.get(status, _t(f"Verification failed: {status}", f"验证失败:{status}"))
    console.print(f"  [yellow]✗ {msg}[/yellow]" + (f"  [dim]{result['error']}[/dim]" if result.get("error") else ""))
    return False, status, None


def _load_current_default_model() -> Optional[str]:
    """Read ``agents.defaults.model`` from the on-disk config, if it exists."""
    data = _load_raw_config()
    return (data or {}).get("agents", {}).get("defaults", {}).get("model") or None


def _model_routes_to_provider(model: str, spec: Any) -> bool:
    """True if ``model`` would auto-route to ``spec`` under ``provider='auto'``."""
    if not model or not spec:
        return False
    model_lower = model.lower()
    model_normalized = model_lower.replace("-", "_")
    if "/" in model_lower:
        prefix = model_lower.split("/", 1)[0].replace("-", "_")
        return prefix == spec.name
    return any(
        kw.lower() in model_lower or kw.lower().replace("-", "_") in model_normalized
        for kw in (getattr(spec, "keywords", None) or ())
    )


def _format_model_for_provider(spec: Any, model_id: str) -> str:
    """Apply ``spec.litellm_prefix`` to a raw ``/v1/models`` id when needed."""
    if not model_id:
        return model_id
    prefix = getattr(spec, "litellm_prefix", "") or ""
    if not prefix:
        return model_id
    if model_id.startswith(f"{prefix}/"):
        return model_id
    for skip in getattr(spec, "skip_prefixes", ()) or ():
        if model_id.startswith(skip):
            return model_id
    return f"{prefix}/{model_id}"


def _pick_model(
    spec: Any,
    *,
    current_model: Optional[str],
    model_ids: Optional[list[str]],
    user_provided_model: Optional[str],
    non_interactive: bool,
) -> str:
    """Decide the model string to write into ``agents.defaults.model``."""
    if user_provided_model:
        return user_provided_model

    if non_interactive:
        if not spec.default_model:
            raise typer.BadParameter(
                f"--model is required for provider '{spec.name}' (no built-in default model in registry)."
            )
        return spec.default_model

    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    if current_model and _model_routes_to_provider(current_model, spec):
        default_value = current_model
    else:
        default_value = spec.default_model or ""

    if model_ids:
        choices = [_format_model_for_provider(spec, mid) for mid in model_ids]
        if default_value and default_value not in choices:
            choices.insert(0, default_value)
        prompt_label = _t(
            f"Default model ({len(choices)} available — type to filter, Tab to complete):",
            f"默认模型(共 {len(choices)} 个 — 输入可筛选,Tab 补全):",
        )
        chosen = questionary.autocomplete(
            prompt_label,
            choices=choices,
            default=default_value,
            style=RAVEN_STYLE,
            qmark=_QMARK,
            ignore_case=True,
            match_middle=True,
        ).ask()
    else:
        console.print(
            _t(
                "  [dim]Couldn't fetch the model list — enter the model id by hand.[/dim]",
                "  [dim]未能拉取模型列表,请手动输入模型 id。[/dim]",
            )
        )
        if default_value:
            chosen = questionary.text(
                _t(
                    f"Default model (press Enter for [{default_value}]):",
                    f"默认模型(回车使用 [{default_value}]):",
                ),
                default=default_value,
                style=RAVEN_STYLE,
                qmark=_QMARK,
            ).ask()
        else:
            chosen = questionary.text(
                _t(f"Default model id for {spec.name}:", f"{spec.name} 的默认模型 id:"),
                validate=lambda v: True if v.strip() else _t("Model id is required.", "必须指定模型 id。"),
                style=RAVEN_STYLE,
                qmark=_QMARK,
            ).ask()

    if chosen is None:
        raise typer.Exit(1)  # Ctrl+C
    chosen = chosen.strip()
    if not chosen:
        # Empty submit (e.g. the prefilled default was cleared) falls back to the
        # default rather than tearing down the wizard. The no-default branch
        # validates non-empty, so an empty value only reaches here with a default.
        if default_value:
            return default_value
        raise typer.Exit(1)
    return chosen


def _write_provider_fields(provider: str, fields: dict[str, Any]) -> None:
    """Thin wrapper that surfaces ops-library errors with friendly hints."""
    from pydantic import ValidationError

    from raven.config.update_providers import set_provider_fields

    try:
        set_provider_fields(provider, fields)
    except KeyError as exc:
        console.print(f"  [red]✗[/red] {exc}")
        raise typer.Exit(1)
    except RuntimeError as exc:
        console.print(f"  [red]✗[/red] {exc}")
        raise typer.Exit(1)
    except ValidationError as exc:
        console.print(_t(f"  [red]✗ Validation failed:[/red]\n{exc}", f"  [red]✗ 校验失败:[/red]\n{exc}"))
        raise typer.Exit(1)


def _persist_default_model(model: Optional[str]) -> None:
    """Patch ``agents.defaults.model`` if we picked one."""
    if not model:
        return
    from raven.config.update import set_default_model

    set_default_model(model)


# ---------------------------------------------------------------------------
# Step 1 — connectivity-failure submenu + test probe
# ---------------------------------------------------------------------------


def _failure_choice(options: list[tuple[str, str]], *, non_interactive: bool) -> str:
    """Render a numbered failure submenu, return the chosen value.

    ``options`` is a list of ``(label, value)``. In non-interactive mode the
    last option (always "continue anyway") is auto-chosen so headless runs
    never block.
    """
    if non_interactive:
        return options[-1][1]
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    chosen = questionary.select(
        _t("What would you like to do?", "想做什么?"),
        choices=[questionary.Choice(label, value=value) for label, value in options],
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()
    if chosen is None:
        raise typer.Exit(1)
    return chosen


def _run_test_probe(provider: str, *, non_interactive: bool, warnings: list[str], allow_repick: bool = True) -> str:
    """Send a one-shot test message; on failure offer recovery options.

    Returns one of ``"ok"`` / ``"continue"`` / ``"repick"`` / ``"rekey"`` /
    ``"switch"``. A test-message failure can be a wrong model, a bad key, or an
    account/balance issue, so the menu offers all the matching exits (aligning
    with the connectivity-failure menu in ``_resolve_model_with_test``);
    ``allow_repick=False`` drops the model option for custom providers whose
    model was fixed with the base_url upfront (Switch re-enters both).
    """
    console.print(
        _t(
            f'  [dim]Sending test message: "{DEFAULT_PROBE_MESSAGE}"[/dim]',
            f'  [dim]正在发送测试消息:"{DEFAULT_PROBE_MESSAGE}"[/dim]',
        )
    )
    try:
        text, tokens, elapsed = send_probe()
    except Exception as exc:
        console.print(_t(f"  [red]✗ Test failed:[/red] {exc}", f"  [red]✗ 测试失败:[/red] {exc}"))
        console.print(
            _t(
                "  [dim]Run 'raven provider test' to re-check, or confirm the model is served by this provider.[/dim]",
                "  [dim]可运行 'raven provider test' 复查,或确认该模型确由此服务商提供。[/dim]",
            )
        )
        print_probe_troubleshooting(provider)
        options = [(_t("Retry", "重试"), "retry")]
        if allow_repick:
            options.append((_t("Re-pick model", "重新选模型"), "repick"))
        options += [
            (_t("Re-enter key", "重新填 Key"), "rekey"),
            (_t("Switch provider", "更换服务商"), "switch"),
            (_t("Continue anyway", "仍然继续"), "continue"),
        ]
        choice = _failure_choice(options, non_interactive=non_interactive)
        if choice == "retry":
            return _run_test_probe(
                provider, non_interactive=non_interactive, warnings=warnings, allow_repick=allow_repick
            )
        if choice in ("repick", "rekey", "switch"):
            return choice
        warnings.append("provider test message")
        return "continue"

    console.print(f"  [bold]▶ Agent:[/bold] {text}")
    extras: list[str] = []
    if tokens:
        extras.append(f"{tokens} tokens")
    extras.append(f"{elapsed:.1f}s")
    console.print(f"  [green]✓ {', '.join(extras)}[/green]")
    return "ok"


# ---------------------------------------------------------------------------
# Step 1 — add one provider (used by both first-run and the "add" entry)
# ---------------------------------------------------------------------------


def _configure_one_provider(
    *,
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    model: Optional[str],
    non_interactive: bool,
    warnings: list[str],
    skip_test: bool = False,
) -> Optional[dict[str, Any]]:
    """Drive one provider through pick → credentials → verify → model → test.

    Returns ``{"provider", "model"}`` on success, or ``None`` if the user
    chose to go back from the interactive provider picker.
    """
    from raven.providers.registry import find_by_name

    # Loop so "Switch provider" on a connectivity failure rewinds to the
    # picker instead of tearing the whole wizard down (keeps steps 2/3/4).
    # A provider passed by flag is used once; switching then requires the
    # interactive picker (or, in non-interactive mode, is impossible).
    flag_provider = provider
    configured_before = set(_configured_providers())
    while True:
        if flag_provider:
            provider = _validate_provider_name(flag_provider)
        else:
            if non_interactive:
                raise typer.BadParameter("--provider is required in non-interactive mode")
            picked = _select_provider()
            if picked is None:
                raise typer.Exit(1)
            if picked is _BACK:
                return None
            provider = picked

        spec = find_by_name(provider)
        is_oauth = bool(spec and spec.is_oauth)
        is_custom = provider == "custom"
        # The interactive picker already echoes the chosen provider; only print
        # an explicit confirmation when it came from --provider (no echo then).
        if flag_provider:
            console.print(
                _t(
                    f"  [dim]Provider:[/dim] [#fbe23f]{_provider_label(provider)}[/#fbe23f]",
                    f"  [dim]服务商:[/dim] [#fbe23f]{_provider_label(provider)}[/#fbe23f]",
                )
            )

        # Snapshot the stored key before _collect_credentials overwrites it, so a
        # failed re-configuration of an existing provider can be rolled back to
        # its prior working key (rather than left holding the just-typed bad one).
        _prev = (_load_raw_config().get("providers") or {}).get(provider) or {}
        old_key = _prev.get("apiKey")
        old_base = _prev.get("apiBase")

        custom_model = _collect_credentials(
            provider,
            is_oauth=is_oauth,
            is_custom=is_custom,
            api_key=api_key,
            base_url=base_url,
            model=model,
            non_interactive=non_interactive,
        )
        if custom_model is _BACK:
            # User backed out of the first credential field — rewind to the
            # provider picker (drop any flag so the picker actually shows).
            flag_provider = None
            continue

        chosen_model = _resolve_model_with_test(
            spec,
            is_custom=is_custom,
            custom_model=custom_model,
            user_model_flag=model,
            non_interactive=non_interactive,
            warnings=warnings,
            skip_test=skip_test,
        )
        if chosen_model is None:
            # "Switch provider" — re-run the picker (drop the flag so the second
            # pass prompts rather than reusing the failed flag value). Roll back
            # the just-written key: clear it if this provider was newly added this
            # pass, or restore the prior key if we were reconfiguring an existing
            # one (so a failed edit doesn't clobber a working provider).
            if provider not in configured_before:
                _write_provider_fields(provider, {"api_key": ""})
            elif old_key:
                _write_provider_fields(provider, {"api_key": old_key, "api_base": old_base})
            flag_provider = None
            continue
        _persist_default_model(chosen_model)
        return {"provider": provider, "model": chosen_model}


def _collect_credentials(
    provider: str,
    *,
    is_oauth: bool,
    is_custom: bool,
    api_key: Optional[str],
    base_url: Optional[str],
    model: Optional[str],
    non_interactive: bool,
) -> Any:
    """Auth setup: OAuth browser flow or api_key write. Returns the custom
    model id when the provider is ``custom`` (locked in here), ``None`` for a
    non-custom provider, or ``_BACK`` if the user backed out of the first
    interactive credential field (caller should rewind to the picker)."""
    if is_oauth:
        if non_interactive:
            console.print(
                "[red]OAuth providers require an interactive browser flow.[/red]\n"
                "Run [#fbe23f]raven provider login "
                f"{provider.replace('_', '-')}[/#fbe23f] separately, then re-run "
                "onboard."
            )
            raise typer.Exit(2)
        # Loop so a failed login offers retry / back instead of crashing out.
        while True:
            if _run_oauth_login(provider):
                return None
            choice = _failure_choice(
                [
                    (_t("Retry", "重试"), "retry"),
                    (_t("Back (pick another provider)", "返回(改选服务商)"), "back"),
                ],
                non_interactive=non_interactive,
            )
            if choice == "retry":
                continue
            return _BACK

    # Pure interactive path (no creds came from flags): prompt field-by-field
    # with empty-submit = back; backing out of the first field rewinds to the
    # provider picker.
    pure_interactive = not non_interactive and not api_key and (not is_custom or (not base_url and not model))
    if pure_interactive:
        prompts: list[Callable[[], Any]] = [lambda: _prompt_api_key(provider, allow_back=True)]
        if is_custom:
            prompts.append(lambda: _prompt_base_url(allow_back=True))
            prompts.append(lambda: _prompt_custom_model(allow_back=True))
        collected = _collect_fields(prompts)
        if collected is None:
            return _BACK
        api_key = collected[0]
        if is_custom:
            base_url = collected[1]
            model = collected[2]
    else:
        if not api_key:
            if non_interactive:
                raise typer.BadParameter("--api-key is required in non-interactive mode")
            api_key = _prompt_api_key(provider)
        if is_custom:
            if not base_url:
                if non_interactive:
                    raise typer.BadParameter("--base-url is required when --provider=custom in non-interactive mode")
                base_url = _prompt_base_url()
            if not model:
                if non_interactive:
                    raise typer.BadParameter("--model is required when --provider=custom in non-interactive mode")
                model = _prompt_custom_model()

    fields: dict[str, Any] = {"api_key": api_key}
    custom_model: Optional[str] = None
    if is_custom:
        fields["api_base"] = base_url
        custom_model = model
    elif base_url:
        fields["api_base"] = base_url

    _write_provider_fields(provider, fields)
    return custom_model


def _resolve_model_with_test(
    spec: Any,
    *,
    is_custom: bool,
    custom_model: Optional[str],
    user_model_flag: Optional[str],
    non_interactive: bool,
    warnings: list[str],
    skip_test: bool = False,
) -> Optional[str]:
    """Verify connectivity → pick the default model → send a test probe.

    On a verify or test-message failure, offers a recovery submenu (retry /
    re-pick model / re-enter key / switch / continue). Custom providers are
    probed too (model was fixed upfront). Only failures stop; success
    auto-advances. Returns the chosen model, or ``None`` to signal "switch
    provider" (the caller rewinds to the picker).
    """
    while True:
        ok, status, model_ids = _verify_provider(spec.name, skip_test=skip_test)
        if not ok:
            options = (
                [(_t("Retry", "重试"), "retry"), (_t("Continue anyway", "仍然继续"), "continue")]
                if status == "network_error"
                else [
                    (_t("Re-enter key", "重新填 Key"), "rekey"),
                    (_t("Switch provider", "更换服务商"), "switch"),
                    (_t("Continue anyway", "仍然继续"), "continue"),
                ]
            )
            choice = _failure_choice(options, non_interactive=non_interactive)
            if choice == "retry":
                continue
            if choice == "rekey" and not non_interactive:
                _write_provider_fields(spec.name, {"api_key": _prompt_api_key(spec.name)})
                continue
            if choice == "switch":
                return None
            warnings.append("provider connectivity")
            model_ids = None
        break

    if is_custom:
        assert custom_model is not None, "custom provider must have model set earlier"
        # Custom endpoints were previously trusted without a test message — the
        # highest-typo-risk case. Send the real probe (it builds from the stored
        # config, so a wrong base_url / model id fails here, not at first chat).
        _persist_default_model(custom_model)
        if skip_test:
            return custom_model
        while True:
            result = _run_test_probe(spec.name, non_interactive=non_interactive, warnings=warnings, allow_repick=False)
            if result == "switch":
                return None
            if result == "rekey":
                _write_provider_fields(spec.name, {"api_key": _prompt_api_key(spec.name)})
                continue
            return custom_model  # ok / continue

    current = _load_current_default_model()
    while True:
        chosen = _pick_model(
            spec,
            current_model=current,
            model_ids=model_ids,
            user_provided_model=user_model_flag,
            non_interactive=non_interactive,
        )
        _persist_default_model(chosen)
        if skip_test:
            return chosen
        result = _run_test_probe(spec.name, non_interactive=non_interactive, warnings=warnings)
        if result == "switch":
            return None
        if result == "rekey":
            _write_provider_fields(spec.name, {"api_key": _prompt_api_key(spec.name)})
            # Re-test the same model with the new key (picker defaults to it).
            current = chosen
            user_model_flag = None
            continue
        if result == "repick":
            current = chosen
            user_model_flag = None
            continue
        return chosen  # ok / continue


# ---------------------------------------------------------------------------
# Step 1 — multi-provider entry (existing-config branch: done / add / edit)
# ---------------------------------------------------------------------------


def _manage_existing_providers(*, non_interactive: bool) -> None:
    """Edit/remove submenu for already-configured providers (interactive only)."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    while True:
        configured = _configured_providers()
        if not configured:
            return
        choices = [questionary.Choice(_provider_label(n), value=n) for n in configured]
        choices.append(questionary.Choice(_t("Back", "返回"), value=_BACK))
        target = questionary.select(
            _t("Pick a provider to manage:", "选择要管理的服务商:"),
            choices=choices,
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if target is None or target is _BACK:
            return

        action = questionary.select(
            _t(
                f"What would you like to do with {_provider_label(target)}?",
                f"对 {_provider_label(target)} 想做什么?",
            ),
            choices=[
                questionary.Choice(_t("Update API key", "更新 API Key"), value="update"),
                questionary.Choice(
                    _t("Remove (clear this provider's key)", "移除(清除该服务商的 Key)"),
                    value="remove",
                ),
                questionary.Choice(_t("Back", "返回"), value=_BACK),
            ],
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if action is None or action is _BACK:
            continue
        if action == "update":
            _write_provider_fields(target, {"api_key": _prompt_api_key(target)})
            console.print(
                _t(
                    f"  [green]✓ Updated {_provider_label(target)}.[/green]",
                    f"  [green]✓ 已更新 {_provider_label(target)}。[/green]",
                )
            )
        elif action == "remove":
            current = _load_current_default_model()
            from raven.providers.registry import find_by_name

            spec = find_by_name(target)
            was_default_source = bool(current and spec and _model_routes_to_provider(current, spec))
            if was_default_source:
                confirm = questionary.confirm(
                    _t(
                        f"The current default model comes from {_provider_label(target)}; "
                        "removing it means you'll need to pick a new default. Remove anyway?",
                        f"当前默认模型来自 {_provider_label(target)};移除后需要重新选择默认模型。仍要移除吗?",
                    ),
                    default=False,
                    style=RAVEN_STYLE,
                    qmark=_QMARK,
                ).ask()
                if not confirm:
                    continue
            _write_provider_fields(target, {"api_key": ""})
            if was_default_source:
                # Clear the now-dangling default so step 1's guard forces a
                # re-pick instead of leaving a model whose provider has no key.
                from raven.config.update import set_default_model

                set_default_model("")
            console.print(
                _t(
                    f"  [green]✓ Removed {_provider_label(target)}'s configuration.[/green]",
                    f"  [green]✓ 已移除 {_provider_label(target)} 的配置。[/green]",
                )
            )


def _step1_provider(
    *,
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    model: Optional[str],
    non_interactive: bool,
    warnings: list[str],
    skip_test: bool = False,
) -> object:
    """Step 1 screen. Returns ``_BACK`` only when the user backs out of the
    first-run picker on the welcome screen (handled by the runner)."""
    _step_header(1, _t("Choose your LLM provider", "选择 LLM 服务商"))
    console.print(
        _t(
            "  [dim]Raven's chat and reasoning are all driven by it.[/dim]",
            "  [dim]Raven 的对话与思考都由它驱动。[/dim]",
        )
    )

    configured = _configured_providers()
    if non_interactive or not configured:
        result = _configure_one_provider(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            non_interactive=non_interactive,
            warnings=warnings,
            skip_test=skip_test,
        )
        if result is None:
            return _BACK
        return None

    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    while True:
        names = ", ".join(_provider_label(n).split(" (")[0] for n in _configured_providers())
        action = questionary.select(
            _t(
                f"LLM provider already configured: {names}. What would you like to do?",
                f"LLM 服务商已配置:{names}。想做什么?",
            ),
            choices=[
                questionary.Choice(_t("Done, continue", "完成,继续"), value="done"),
                questionary.Choice(_t("Add another provider", "新增一个服务商"), value="add"),
                questionary.Choice(_t("Edit / remove a provider", "编辑 / 移除服务商"), value="edit"),
            ],
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if action is None:
            raise typer.Exit(1)  # Ctrl+C exits; never treat it as "done"
        if action == "done":
            # Step 1 is required: never advance without at least one provider AND
            # a default model, so deleting every provider can't slip through.
            if not (_configured_providers() and _load_current_default_model()):
                console.print(
                    _t(
                        "  [yellow]At least one provider with a default model is required — add or re-pick one.[/yellow]",
                        "  [yellow]至少需要一个带默认模型的服务商 — 请新增或重新选择一个。[/yellow]",
                    )
                )
                continue
            return None
        if action == "add":
            _configure_one_provider(
                provider=None,
                api_key=None,
                base_url=None,
                model=None,
                non_interactive=False,
                warnings=warnings,
                skip_test=skip_test,
            )
        elif action == "edit":
            _manage_existing_providers(non_interactive=non_interactive)


# ---------------------------------------------------------------------------
# Step 2 — sandbox / run location
# ---------------------------------------------------------------------------


def _current_sandbox_backend() -> str:
    """Read ``tools.sandbox.backend`` from disk; defaults to ``none``."""
    data = _load_raw_config()
    return ((data.get("tools") or {}).get("sandbox") or {}).get("backend") or "none"


def _persist_sandbox_backend(backend: str) -> None:
    """Patch ``sandbox.backend`` on the on-disk config via the ops layer."""
    from raven.config.update import set_sandbox_backend

    set_sandbox_backend(backend)


def _probe_boxlite() -> tuple[bool, str]:
    """Probe boxlite availability. Returns ``(ok, reason)``.

    ``reason`` ∈ ``"ok"`` / ``"missing"`` / ``"error"``. The runtime import is
    the same availability gate ``build_executor`` uses for the boxlite backend.
    """
    console.print(_t("  [dim]⏳ Checking sandbox availability…[/dim]", "  [dim]⏳ 正在检测沙箱可用性…[/dim]"))
    try:
        import boxlite  # noqa: F401
    except ImportError:
        return False, "missing"
    except Exception:
        return False, "error"
    return True, "ok"


def _step2_sandbox(*, skip: bool, non_interactive: bool) -> object:
    """Step 2 — choose run location (host / boxlite sandbox)."""
    _step_header(2, _t("Choose where Raven runs code / commands", "选择 Raven 运行代码 / 命令的位置"))

    if skip or non_interactive:
        console.print(
            _t(
                "  [dim]Keeping run location: host (direct).[/dim]",
                "  [dim]保持运行位置:本机直接运行。[/dim]",
            )
        )
        return None

    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    current = _current_sandbox_backend()
    choices: list[Any] = []
    if current != "none":
        choices.append(
            questionary.Choice(_t("Keep current: sandbox (boxlite)", "沿用当前:沙箱(boxlite)"), value="keep")
        )
    choices.extend(
        [
            questionary.Choice(
                _t(
                    "Host (direct) — simplest, runs right on your machine",
                    "本机直接运行 — 最简单,直接在你的电脑上执行",
                ),
                value="none",
            ),
            questionary.Choice(
                _t(
                    "Sandbox isolation (boxlite) — isolated in a lightweight VM, safer (needs platform support)",
                    "沙箱隔离(boxlite)— 用轻量虚拟机隔离,更安全,需环境支持",
                ),
                value="boxlite",
            ),
            questionary.Choice(_t("Back", "返回"), value=_BACK),
        ]
    )

    picked = questionary.select(
        _t("Run location:", "运行位置:"), choices=choices, style=RAVEN_STYLE, qmark=_QMARK
    ).ask()
    if picked is None:
        raise typer.Exit(1)
    if picked is _BACK:
        return _BACK
    if picked == "keep":
        return None
    if picked == "none":
        _persist_sandbox_backend("none")
        console.print(
            _t(
                "  [green]✓ Running directly on the host.[/green]",
                "  [green]✓ 将在本机直接运行。[/green]",
            )
        )
        return None

    # boxlite — probe before committing.
    while True:
        ok, reason = _probe_boxlite()
        if ok:
            _persist_sandbox_backend("boxlite")
            console.print(
                _t(
                    "  [green]✓ Sandbox available. Using default resources "
                    "(2 CPU / 2 GB / network); tune in the config file if needed.[/green]",
                    "  [green]✓ 沙箱可用。将使用默认资源(2 CPU / 2 GB / 联网);如需调整可改配置文件。[/green]",
                )
            )
            return None
        if reason == "missing":
            console.print(
                _t(
                    "  [yellow]✗ Sandbox runtime (boxlite) isn't installed.[/yellow]\n"
                    "  [dim]Install it, then choose “Retry after install”:  "
                    "pip install 'raven\\[sandbox]'[/dim]",
                    "  [yellow]✗ 未安装沙箱运行时(boxlite)。[/yellow]\n"
                    "  [dim]先安装,再选「安装后重试」:  "
                    "pip install 'raven\\[sandbox]'[/dim]",
                )
            )
        else:  # reason == "error": importable but failed to initialize
            console.print(
                _t(
                    "  [yellow]✗ Sandbox runtime (boxlite) is installed but failed to "
                    "start.[/yellow]\n"
                    "  [dim]Your machine may lack the required virtualization support. "
                    "Fall back to host, or check the boxlite setup docs.[/dim]",
                    "  [yellow]✗ 沙箱运行时(boxlite)已安装,但启动失败。[/yellow]\n"
                    "  [dim]可能本机缺少所需的虚拟化支持。可退回本机运行,或查阅 boxlite 安装文档。[/dim]",
                )
            )
        choice = _failure_choice(
            [
                (_t("Fall back to host", "退回本机运行"), "host"),
                (_t("Retry after install", "安装后重试"), "retry"),
                (_t("Skip", "跳过"), "skip"),
            ],
            non_interactive=non_interactive,
        )
        if choice == "retry":
            continue
        if choice == "host":
            _persist_sandbox_backend("none")
            console.print(
                _t(
                    "  [green]✓ Running directly on the host.[/green]",
                    "  [green]✓ 将在本机直接运行。[/green]",
                )
            )
        return None


# ---------------------------------------------------------------------------
# Step 3 — chat channel (stackable)
# ---------------------------------------------------------------------------


def _enabled_channels() -> list[str]:
    """Names of channels currently enabled on disk."""
    data = _load_raw_config()
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
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    names = _ordered_channel_names()
    choices = [questionary.Choice(n, value=n) for n in names]
    choices.append(questionary.Choice(_t("Back", "返回"), value=_BACK))
    picked = questionary.select(_t("Channel:", "渠道:"), choices=choices, style=RAVEN_STYLE, qmark=_QMARK).ask()
    return picked


def _prompt_channel_fields(channel: str) -> Any:
    """Reflect a channel's Pydantic schema and prompt for credential-like fields."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.config.update_channels import channel_field_specs

    try:
        specs = channel_field_specs(channel)
    except KeyError as exc:
        console.print(f"  [red]✗[/red] {exc}")
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
        console.print(
            _t(
                f"  [dim]Configuring {channel} — fill in:[/dim] {names}",
                f"  [dim]正在配置 {channel} — 请填写:[/dim] {names}",
            )
        )
        help_text = _CHANNEL_CRED_HELP.get(channel)
        if help_text:
            console.print(
                _t(
                    f"  [dim]Where to get it: {help_text[0]}[/dim]",
                    f"  [dim]去哪拿:{help_text[1]}[/dim]",
                )
            )
    else:
        console.print(
            _t(
                f"  [dim]{channel} needs no credentials; enabling.[/dim]",
                f"  [dim]{channel} 无需填写凭证,正在启用。[/dim]",
            )
        )

    fields: dict[str, Any] = {}
    for idx, (path, spec) in enumerate(promptable):
        required = bool(spec.get("required"))
        description = spec.get("description", "")
        opt_tag = "" if required else _t(" (optional)", " (可选)")
        prompt_label = f"{path}{opt_tag}" + (f" — {description}" if description else "") + ":"
        # First field's empty submit rewinds to the channel picker; a later
        # optional field's empty submit skips it; a later required field re-prompts
        # (empty was previously accepted silently, enabling a half-configured
        # channel — the write layer treats "required" as a UX marker only).
        allow_back = idx == 0
        placeholder = _field_placeholder(allow_back, required)
        while True:
            if spec.get("is_secret"):
                value = questionary.password(
                    prompt_label, placeholder=placeholder, style=RAVEN_STYLE, qmark=_QMARK
                ).ask()
            else:
                value = questionary.text(prompt_label, placeholder=placeholder, style=RAVEN_STYLE, qmark=_QMARK).ask()
            if value is None:
                raise typer.Exit(1)
            value = value.strip()
            if value:
                fields[path] = value
                break
            if allow_back:
                return _BACK  # first field empty → back to the channel picker
            if required:
                console.print(_t(f"  [yellow]{path} is required.[/yellow]", f"  [yellow]{path} 为必填项。[/yellow]"))
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
        console.print(f"  [red]✗[/red] {exc}")
        raise typer.Exit(1)
    except ValidationError as exc:
        console.print(_t(f"  [red]✗ Validation failed:[/red]\n{exc}", f"  [red]✗ 校验失败:[/red]\n{exc}"))
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
    console.print(
        _t(
            f"  [yellow]✗ Node.js / npm not found (the {channel} bridge needs it). "
            "Install Node.js, then retry.[/yellow]",
            f"  [yellow]✗ 未找到 Node.js / npm({channel} 的桥接需要它)。请先安装 Node.js,再重试。[/yellow]",
        )
    )
    choice = _failure_choice(
        [
            (_t("Retry after install", "安装后重试"), "retry"),
            (_t("Skip", "跳过"), "skip"),
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
        console.print(_t(f"  [red]✗ Unknown channel: {channel}[/red]", f"  [red]✗ 未知渠道:{channel}[/red]"))
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
                console.print(
                    _t(
                        f"  [dim]Skipped {channel}; install Node.js then run raven channels login {channel}.[/dim]",
                        f"  [dim]已跳过 {channel};装好 Node.js 后运行 raven channels login {channel}。[/dim]",
                    )
                )
                return

            from raven.config.loader import load_config

            channel_cfg = getattr(load_config().channels, channel, None)
            if channel_cfg is None:
                console.print(
                    _t(
                        f"  [red]✗ No config section for channel: {channel}[/red]",
                        f"  [red]✗ 渠道 {channel} 没有配置段。[/red]",
                    )
                )
                return
            adapter = spec.factory(channel_cfg)
            if channel == "whatsapp":
                console.print(
                    _t(
                        "  [dim]Building the WhatsApp bridge — the first run can take 30–120s…[/dim]",
                        "  [dim]正在构建 WhatsApp 桥接,首次约需 30–120 秒…[/dim]",
                    )
                )
            console.print(
                _t(
                    f"  [dim]Starting {spec.display_name} QR login…[/dim]",
                    f"  [dim]正在启动 {spec.display_name} 扫码登录…[/dim]",
                )
            )
            console.print(
                _t(
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
                console.print(
                    _t(
                        f"  [yellow]✗ Login failed: {exc}[/yellow]",
                        f"  [yellow]✗ 登录失败:{exc}[/yellow]",
                    )
                )
                ok = False
            finally:
                _wiz_logger.disable(_login_log_scope)
            if ok:
                console.print(
                    _t(
                        f"  [green]✓ Logged in; {channel} connected.[/green]",
                        f"  [green]✓ 已登录;{channel} 已接入。[/green]",
                    )
                )
                logged_in = True
                return
            choice = _failure_choice(
                [
                    (_t("Retry", "重试"), "retry"),
                    (_t("Skip this channel", "跳过此渠道"), "skip"),
                ],
                non_interactive=non_interactive,
            )
            if choice == "retry":
                continue
            console.print(
                _t(
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
        if channel is None or channel is _BACK:
            return
        if _channel_uses_interactive_login(channel):
            _scancode_login(channel, non_interactive=non_interactive)
            return
        fields = _prompt_channel_fields(channel)
        if fields is _BACK:
            continue  # backed out of the first field — re-pick a channel
        _enable_channel(channel, fields)
        console.print(_t(f"  [green]✓ {channel} enabled.[/green]", f"  [green]✓ {channel} 已启用。[/green]"))
        return


def _manage_existing_channels() -> None:
    """Edit/disable submenu for already-enabled channels."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.config.update_channels import disable_channel, set_channel_fields

    while True:
        enabled = _enabled_channels()
        if not enabled:
            return
        choices = [questionary.Choice(n, value=n) for n in enabled]
        choices.append(questionary.Choice(_t("Back", "返回"), value=_BACK))
        target = questionary.select(
            _t("Pick a channel to manage:", "选择要管理的渠道:"),
            choices=choices,
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if target is None or target is _BACK:
            return
        action = questionary.select(
            _t(f"What would you like to do with {target}?", f"对 {target} 想做什么?"),
            choices=[
                questionary.Choice(_t("Edit config (re-enter fields)", "编辑配置(重填字段)"), value="edit"),
                questionary.Choice(_t("Disable (keep credentials)", "停用(保留凭证)"), value="disable"),
                questionary.Choice(_t("Back", "返回"), value=_BACK),
            ],
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if action is None or action is _BACK:
            continue
        if action == "edit":
            fields = _prompt_channel_fields(target)
            if fields is _BACK:
                continue  # backed out — return to the manage menu
            if fields:
                set_channel_fields(target, fields)
            console.print(
                _t(
                    f"  [green]✓ {target} config updated.[/green]",
                    f"  [green]✓ {target} 配置已更新。[/green]",
                )
            )
        elif action == "disable":
            disable_channel(target)
            console.print(
                _t(
                    f"  [green]✓ Disabled {target} (credentials kept; re-enable later "
                    f"with raven channels enable {target}).[/green]",
                    f"  [green]✓ 已停用 {target}(凭证保留;之后用 raven channels enable {target} 重新启用)。[/green]",
                )
            )


def _step3_channel(*, channel: Optional[str], skip: bool, non_interactive: bool) -> object:
    """Step 3 — optionally enable chat channel(s)."""
    _step_header(
        3,
        _t(
            "(Optional) Connect a messaging app so you can chat with Raven there",
            "(可选)接入即时通讯软件,直接在里面和 Raven 聊天",
        ),
    )

    if skip:
        console.print(
            _t(
                "  [dim]Skipped via --skip-channel.[/dim]",
                "  [dim]已通过 --skip-channel 跳过。[/dim]",
            )
        )
        return None

    if non_interactive:
        if channel:
            console.print(
                f"[red]--channel {channel} given but non-interactive mode can't "
                "prompt for credential fields.[/red]\n"
                f"Run [#fbe23f]raven channels enable {channel} --<field> <value> ...[/#fbe23f] "
                "after onboard finishes."
            )
            raise typer.Exit(2)
        console.print(
            _t(
                "  [dim]Skipped (non-interactive, --channel not given).[/dim]",
                "  [dim]已跳过(非交互且未提供 --channel)。[/dim]",
            )
        )
        return None

    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    if channel:
        if _channel_uses_interactive_login(channel):
            _scancode_login(channel, non_interactive=non_interactive)
        else:
            fields = _prompt_channel_fields(channel)
            if fields is _BACK:
                console.print(_t("  [dim]Skipped.[/dim]", "  [dim]已跳过。[/dim]"))
                return None
            _enable_channel(channel, fields)
            console.print(
                _t(
                    f"  [green]✓ {channel} enabled.[/green]",
                    f"  [green]✓ {channel} 已启用。[/green]",
                )
            )
        return None

    while True:
        enabled = _enabled_channels()
        if not enabled:
            action = questionary.select(
                _t("Connect a chat channel?", "接入一个聊天渠道吗?"),
                choices=[
                    questionary.Choice(_t("Add a channel", "新增一个渠道"), value="add"),
                    questionary.Choice(
                        _t(
                            "Skip (add later with raven channels enable)",
                            "跳过(之后用 raven channels enable 添加)",
                        ),
                        value="skip",
                    ),
                ],
                style=RAVEN_STYLE,
                qmark=_QMARK,
            ).ask()
            if action is None:
                raise typer.Exit(1)
            if action == "skip":
                console.print(_t("  [dim]Skipped.[/dim]", "  [dim]已跳过。[/dim]"))
                return None
            _add_one_channel(non_interactive=non_interactive)
            continue

        action = questionary.select(
            _t(
                f"Chat channel already connected: {', '.join(enabled)}. What would you like to do?",
                f"聊天渠道已接入:{', '.join(enabled)}。想做什么?",
            ),
            choices=[
                questionary.Choice(_t("Done, next step", "完成,下一步"), value="done"),
                questionary.Choice(_t("Add a channel", "新增一个渠道"), value="add"),
                questionary.Choice(_t("Edit / remove a channel", "编辑 / 移除渠道"), value="edit"),
            ],
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if action is None:
            raise typer.Exit(1)
        if action == "done":
            return None
        if action == "add":
            _add_one_channel(non_interactive=non_interactive)
        elif action == "edit":
            _manage_existing_channels()


# ---------------------------------------------------------------------------
# Step 4 — EverOS long-term memory
# ---------------------------------------------------------------------------


def _set_memory_backend(backend: Optional[str]) -> None:
    """Set ``memory.backend`` (``"everos"`` / ``None``) via the ops layer."""
    from raven.config.update import set_memory_backend

    set_memory_backend(backend)


def _init_extension_block_defaults() -> None:
    """Seed the memory / plugins / skillForge extension defaults via the ops layer."""
    from raven.config.update import init_extension_block_defaults

    init_extension_block_defaults()


def _everos_section(section: str) -> dict[str, Any]:
    """Current values of an EverOS section, or ``{}``."""
    from raven.config.update_everos import load_everos_config

    return load_everos_config().get(section, {}) or {}


def _memory_enabled() -> bool:
    """True iff EverOS memory is both selected AND usable on disk.

    "Usable" requires both required models (llm and embedding) in the EverOS
    toml: the seed/schema default sets ``memory.backend="everos"`` before any
    models exist, so a bare backend check would mis-report a fresh, modelless
    install as "enabled". Both roles are ``optional: False``; gating on either
    alone would treat a half-configured setup (e.g. llm written but embedding
    skipped) as enabled and offer "keep current" over a non-functional memory.
    """
    data = _load_raw_config()
    if (data.get("memory") or {}).get("backend") != "everos":
        return False
    return bool(_everos_section("llm").get("model") and _everos_section("embedding").get("model"))


# Providers whose main model can be reused as the EverOS memory LLM: they
# speak the OpenAI chat-completions protocol that EverOS's bare OpenAI client
# requires. OAuth providers (github_copilot / openai_codex) and non-OpenAI
# wire protocols (anthropic / gemini) are excluded.
_OPENAI_COMPATIBLE_PROVIDERS = {"openrouter", "openai", "deepseek", "custom"}

# Fallback OpenAI-compatible base URLs for providers whose registry
# ``default_api_base`` is empty (they rely on the SDK's built-in default,
# which EverOS's bare client doesn't know). EverOS needs an explicit base_url.
_PROVIDER_BASE_URL_FALLBACK = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


def _resolve_model_provider(model: str) -> Optional[str]:
    """Best-effort: which configured provider does ``model`` belong to?

    Prefixed models (``openrouter/...`` / ``openai/gpt-4o``) read off the head.
    A custom endpoint stores its model as a BARE id (e.g. ``qwen-max``) with no
    prefix, so an unrecognized head falls back to ``"custom"`` when a custom
    provider is actually configured with a key. Returns ``None`` when no match.
    """
    if not model:
        return None
    head = model.split("/", 1)[0].replace("-", "_")
    if "/" in model:
        from raven.config.update_providers import provider_field_specs

        try:
            provider_field_specs(head)
            return head
        except KeyError:
            pass
    # No usable prefix → could be a bare custom-endpoint model.
    custom = (_load_raw_config().get("providers") or {}).get("custom") or {}
    if custom.get("apiKey"):
        return "custom"
    # A bare id that still matches a known provider head (rare; e.g. a direct
    # provider's bare default before prefixing) — accept the head if known.
    return head if head in _OPENAI_COMPATIBLE_PROVIDERS else None


def _model_is_openai_compatible(model: Optional[str]) -> bool:
    """Heuristic: can the main chat model's provider be reused for memory LLM?

    EverOS's memory LLM uses a bare OpenAI client, so the main model is
    reusable only when its provider speaks the OpenAI chat protocol. Custom
    endpoints are OpenAI-compatible by definition (the wizard only offers
    ``custom`` for OpenAI-compatible endpoints).
    """
    if not model:
        return False
    return _resolve_model_provider(model) in _OPENAI_COMPATIBLE_PROVIDERS


def _resolve_reuse_llm_creds(main_model: str) -> dict[str, Optional[str]]:
    """Map a litellm-style main model to bare EverOS LLM settings.

    EverOS sends ``EVEROS_LLM__MODEL`` to ``base_url`` via a bare OpenAI
    client, so:
      - strip the provider's litellm prefix to the bare model id the upstream
        endpoint expects (``openrouter/anthropic/claude-x`` → ``anthropic/claude-x``;
        a custom endpoint's bare id is used as-is);
      - resolve the provider's real ``base_url`` (configured ``apiBase`` →
        registry ``default_api_base`` → a known fallback);
      - carry the provider's stored api_key.
    """
    from raven.providers.registry import find_by_name

    provider = _resolve_model_provider(main_model) or main_model.split("/", 1)[0].replace("-", "_")
    spec = find_by_name(provider)
    prov_cfg = (_load_raw_config().get("providers") or {}).get(provider, {})

    # Strip the litellm routing prefix to the bare model id the upstream
    # endpoint expects. Direct providers (openai / deepseek / gemini) carry a
    # ``{provider}/`` route prefix that litellm consumes but the raw OpenAI
    # client must not see; gateways (openrouter) carry their litellm_prefix.
    # Custom endpoints store a bare id already, so no prefix matches → unchanged.
    bare_model = main_model
    litellm_prefix = getattr(spec, "litellm_prefix", "") if spec else ""
    for prefix in (litellm_prefix, provider):
        if prefix and bare_model.startswith(f"{prefix}/"):
            bare_model = bare_model.split("/", 1)[1]
            break

    base_url = (
        prov_cfg.get("apiBase")
        or (getattr(spec, "default_api_base", "") if spec else "")
        or _PROVIDER_BASE_URL_FALLBACK.get(provider)
    )
    return {
        "model": bare_model,
        "api_key": prov_cfg.get("apiKey"),
        "base_url": base_url,
    }


def _prompt_text(label: str, *, secret: bool = False, default: str = "", allow_back: bool = False) -> Any:
    """Prompt for free text. With ``allow_back``, an empty submit returns
    ``_BACK`` (and a hint is shown); otherwise returns the stripped string."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    placeholder = _back_placeholder(allow_back)
    if secret:
        value = questionary.password(label, placeholder=placeholder, style=RAVEN_STYLE, qmark=_QMARK).ask()
    else:
        value = questionary.text(label, default=default, placeholder=placeholder, style=RAVEN_STYLE, qmark=_QMARK).ask()
    if value is None:
        raise typer.Exit(1)
    value = value.strip()
    if allow_back and value == "":
        return _BACK
    return value


def _probe_everos_chat(model: Optional[str], *, api_key: Optional[str], base_url: Optional[str]) -> tuple[bool, str]:
    """Real capability probe for a memory-LLM endpoint: ``POST
    {base_url}/chat/completions`` once and confirm a choice comes back. Unlike a
    bare ``GET /models`` connectivity check, this exercises the picked model, so
    an endpoint that lists models but doesn't serve the chosen id fails here
    instead of reporting a false green. Provider-agnostic; never raises."""
    import httpx

    if not base_url:
        return False, "no base_url configured"
    url = base_url.rstrip("/") + ("/chat/completions" if "/v1" in base_url else "/v1/chat/completions")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        return False, f"probe failed: {exc}"
    choices = data.get("choices") if isinstance(data, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return True, "ok"
    return False, "endpoint returned no completion"


def _verify_everos_llm(
    label: str,
    *,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    non_interactive: bool,
    warnings: list[str],
    continue_hint: Optional[tuple[str, str]] = None,
) -> bool:
    """Probe the memory LLM with a real chat completion, offering retry/continue on failure."""
    console.print(_t(f"  [dim]⏳ Verifying {label}…[/dim]", f"  [dim]⏳ 正在验证 {label}…[/dim]"))
    ok, detail = _probe_everos_chat(model, api_key=api_key, base_url=base_url)
    if ok:
        console.print(_t(f"  [green]✓ {label} connected.[/green]", f"  [green]✓ {label} 连接成功。[/green]"))
        return True
    console.print(
        _t(
            f"  [yellow]✗ Couldn't verify {label}: {detail}[/yellow]",
            f"  [yellow]✗ 验证失败 {label}:{detail}[/yellow]",
        )
    )
    if continue_hint:
        cont_label = _t(f"Continue anyway ({continue_hint[0]})", f"仍然继续({continue_hint[1]})")
    else:
        cont_label = _t("Continue anyway", "仍然继续")
    choice = _failure_choice(
        [
            (_t("Re-enter", "重新填写"), "rekey"),
            (cont_label, "continue"),
        ],
        non_interactive=non_interactive,
    )
    if choice == "rekey":
        return False
    warnings.append(label)
    return True


def _verify_rerank(
    label: str,
    *,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    rerank_provider: Optional[str],
    non_interactive: bool,
    warnings: list[str],
    continue_hint: Optional[tuple[str, str]] = None,
) -> bool:
    """Probe a rerank endpoint with a provider-specific request, offering retry/continue on failure."""
    console.print(_t(f"  [dim]⏳ Verifying {label}…[/dim]", f"  [dim]⏳ 正在验证 {label}…[/dim]"))
    ok, detail = _probe_rerank(model, api_key=api_key, base_url=base_url, rerank_provider=rerank_provider)
    if ok:
        console.print(_t(f"  [green]✓ {label} connected.[/green]", f"  [green]✓ {label} 连接成功。[/green]"))
        return True
    console.print(
        _t(
            f"  [yellow]✗ Couldn't verify {label}: {detail}[/yellow]",
            f"  [yellow]✗ 验证失败 {label}:{detail}[/yellow]",
        )
    )
    if continue_hint:
        cont_label = _t(f"Continue anyway ({continue_hint[0]})", f"仍然继续({continue_hint[1]})")
    else:
        cont_label = _t("Continue anyway", "仍然继续")
    choice = _failure_choice(
        [
            (_t("Re-enter", "重新填写"), "rekey"),
            (cont_label, "continue"),
        ],
        non_interactive=non_interactive,
    )
    if choice == "rekey":
        return False
    warnings.append(label)
    return True


def _probe_rerank(
    model: Optional[str],
    *,
    api_key: Optional[str],
    base_url: Optional[str],
    rerank_provider: Optional[str],
) -> tuple[bool, str]:
    """Real capability probe for a rerank endpoint. Dispatches by provider
    protocol (vllm / deepinfra / dashscope). Never raises."""
    import httpx

    if not base_url or not model:
        return False, "no base_url or model configured"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    headers["Content-Type"] = "application/json"

    try:
        if rerank_provider == "deepinfra":
            url = f"{base_url.rstrip('/')}/{model}"
            body: dict = {"queries": ["ping"], "documents": ["pong"]}
        elif rerank_provider == "dashscope":
            url = f"{base_url.rstrip('/')}/api/v1/services/rerank/text-rerank/text-rerank"
            body = {
                "model": model,
                "input": {"query": "ping", "documents": ["pong"]},
                "parameters": {"return_documents": False, "top_n": 1},
            }
        else:  # vllm / OpenAI-compat
            url = f"{base_url.rstrip('/')}/rerank"
            body = {"model": model, "query": "ping", "documents": ["pong"]}

        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json=body, headers=headers)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        return False, f"probe failed: {exc}"

    if rerank_provider == "deepinfra":
        scores = data.get("scores")
        if isinstance(scores, list) and scores:
            return True, "ok"
        return False, "endpoint returned no scores"
    if rerank_provider == "dashscope":
        output = data.get("output")
        results = output.get("results") if isinstance(output, dict) else None
        if isinstance(results, list) and results:
            return True, "ok"
        return False, "endpoint returned no results"
    # vllm
    results = data.get("results")
    if isinstance(results, list) and results:
        return True, "ok"
    return False, "endpoint returned no results"


_REQUIRED_EMBEDDING_DIM = 1024


def _probe_embedding_dim(url: str, headers: dict, model: str) -> int | str:
    """Try embedding with ``dimensions=1024``; fall back to native dim.

    Returns the effective dimension (int) on success, or an error
    description (str) on failure.
    """
    import httpx

    def _try_embed(client: httpx.Client, body: dict) -> int | str:
        try:
            resp = client.post(url, json=body, headers=headers)
            if resp.status_code != 200:
                return f"HTTP {resp.status_code}"
            items = resp.json().get("data", [])
            if not items:
                return "empty response"
            first = items[0]
            if not isinstance(first, dict):
                return "unexpected response format"
            return len(first.get("embedding", []))
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            return str(exc)

    with httpx.Client(timeout=15) as client:
        result = _try_embed(
            client, {"model": model, "input": ["dimension check"], "dimensions": _REQUIRED_EMBEDDING_DIM}
        )
        if result == _REQUIRED_EMBEDDING_DIM:
            return result
        return _try_embed(client, {"model": model, "input": ["dimension check"]})


def _verify_embedding_dim(
    *,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    non_interactive: bool,
) -> bool:
    """Send a test embedding request and verify the vector dimension is 1024.

    Returns True to proceed, False to re-prompt.
    """
    if not base_url or not model:
        return True

    url = base_url.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    while True:
        console.print(
            _t(
                "  [dim]⏳ Checking embedding dimension…[/dim]",
                "  [dim]⏳ 正在检测 embedding 维度…[/dim]",
            )
        )
        result = _probe_embedding_dim(url, headers, model)

        if result == _REQUIRED_EMBEDDING_DIM:
            console.print(
                _t(
                    f"  [green]✓ Supports {result}-dim.[/green]",
                    f"  [green]✓ 支持 {result} 维。[/green]",
                )
            )
            return True

        if isinstance(result, int) and result < _REQUIRED_EMBEDDING_DIM:
            console.print(
                _t(
                    f"  [red]✗ Dimension too small: model outputs {result}-dim, "
                    f"EverOS requires >= {_REQUIRED_EMBEDDING_DIM}. Please pick another model.[/red]",
                    f"  [red]✗ 维度不足：模型输出 {result} 维，"
                    f"EverOS 要求 >= {_REQUIRED_EMBEDDING_DIM} 维，请重新选择。[/red]",
                )
            )
            return False

        if isinstance(result, int) and result > _REQUIRED_EMBEDDING_DIM:
            console.print(
                _t(
                    f"  [red]✗ Model outputs {result}-dim and does not support the "
                    f"dimensions parameter to truncate to {_REQUIRED_EMBEDDING_DIM}. "
                    "Please pick another model.[/red]",
                    f"  [red]✗ 模型输出 {result} 维，且不支持 dimensions 参数"
                    f"截断到 {_REQUIRED_EMBEDDING_DIM} 维，请重新选择。[/red]",
                )
            )
            return False

        console.print(
            _t(
                f"  [yellow]✗ Couldn't verify dimension: {result}[/yellow]",
                f"  [yellow]✗ 无法验证维度：{result}[/yellow]",
            )
        )
        if non_interactive:
            return False
        choice = _failure_choice(
            [
                (_t("Retry", "重试"), "retry"),
                (_t("Re-enter", "重新选择"), "rekey"),
            ],
            non_interactive=False,
        )
        if choice == "rekey":
            return False


# Curated OpenAI-compatible endpoints for EverOS memory models. Picking one
# pre-fills its base_url (mirrors the main provider step); everything else is
# reachable via "reuse an existing endpoint" or "custom" (type a base_url).
# These are the providers' documented OpenAI-compatible /v1 endpoints.
_EVEROS_PROVIDERS: list[dict[str, Any]] = [
    {
        "name": "openai",
        "label": "OpenAI",
        "label_zh": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "supports": {"llm", "embedding", "multimodal"},
    },
    {
        "name": "openrouter",
        "label": "OpenRouter",
        "label_zh": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "supports": {"llm", "embedding", "rerank", "multimodal"},
        "rerank_provider": "vllm",
    },
    {
        "name": "deepseek",
        "label": "DeepSeek",
        "label_zh": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "supports": {"llm"},
    },
    {
        "name": "deepinfra",
        "label": "DeepInfra",
        "label_zh": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "supports": {"llm", "embedding", "rerank"},
        "rerank_provider": "deepinfra",
        "rerank_base_url": "https://api.deepinfra.com/v1/inference",
    },
    {
        "name": "siliconflow",
        "label": "SiliconFlow",
        "label_zh": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "supports": {"llm", "embedding", "rerank"},
        "rerank_provider": "vllm",
    },
    {
        "name": "dashscope",
        "label": "DashScope (Alibaba)",
        "label_zh": "阿里百炼 DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "supports": {"llm", "embedding", "rerank"},
        "rerank_provider": "dashscope",
        "rerank_base_url": "https://dashscope.aliyuncs.com",
    },
]


def _match_provider_by_url(base_url: Optional[str]) -> Optional[str]:
    """Reverse-lookup a curated provider name from its base_url."""
    if not base_url:
        return None
    normalized = base_url.rstrip("/")
    for prov in _EVEROS_PROVIDERS:
        if prov["base_url"].rstrip("/") == normalized:
            return prov["name"]
    return None


# Per-role config: menu/verify label, model-id example, whether optional, and
# whether to run a connectivity probe after configuring (rerank/multimodal use
# non-chat endpoints whose /models probe isn't a reliable health check).
_EVEROS_ROLES: dict[str, dict[str, Any]] = {
    "llm": {
        "label": ("Memory LLM", "记忆 LLM"),
        "example": "gpt-4.1-mini",
        "optional": False,
        "verify": True,
        "purpose": (
            "reads each conversation to judge what matters and extract the key points",
            "从对话中判断信息边界、抽取要点",
        ),
        "recommendation": (
            "Recommended: [bold]gpt-4.1-mini[/bold] or stronger; weaker models may degrade quality",
            "推荐 [bold]gpt-4.1-mini[/bold] 或更强的模型，更弱的模型可能导致提取质量下降",
        ),
        "continue_hint": ("memory extraction may fail", "记忆抽取可能失败"),
    },
    "embedding": {
        "label": ("Memory embedding", "记忆 embedding"),
        "example": "Qwen/Qwen3-Embedding-4B",
        "optional": False,
        "verify": True,
        "purpose": (
            "turns text into vectors so memories are stored and retrieved by meaning, not just keywords",
            "把文字转成向量,存入记忆库并在检索时按「意思」匹配,而不只是关键词",
        ),
        "recommendation": (
            "Recommended: [bold]Qwen/Qwen3-Embedding-4B[/bold]; must be [bold yellow]1024-dim[/bold yellow] and support Chinese + English",
            "推荐 [bold]Qwen/Qwen3-Embedding-4B[/bold]，需 [bold yellow]1024 维[/bold yellow]且支持中英文的模型",
        ),
        "continue_hint": ("semantic recall will be unavailable", "语义召回将不可用"),
    },
    "rerank": {
        "label": ("Memory rerank", "记忆 rerank"),
        "example": "Qwen/Qwen3-Reranker-4B",
        "optional": True,
        "verify": True,
        "purpose": (
            "re-ranks the candidates from semantic search so the best match comes first (slightly slower); "
            "memory works fine without it, just with slightly weaker ordering",
            "在语义召回一批候选后再精排一遍,让结果更准,会略增延迟;不配也能正常用记忆,只是排序略逊",
        ),
        "recommendation": (
            "Recommended: [bold]Qwen/Qwen3-Reranker-4B[/bold]",
            "推荐 [bold]Qwen/Qwen3-Reranker-4B[/bold]",
        ),
        "continue_hint": ("rerank quality may degrade", "rerank 精度可能下降"),
        "skip_note": (
            "Skipped reranking; memory retrieval still works.",
            "已跳过 rerank,记忆检索仍可用。",
        ),
    },
    "multimodal": {
        "label": ("Memory multimodal", "记忆多模态"),
        "example": "google/gemini-3-flash-preview",
        "optional": True,
        "verify": True,
        "purpose": (
            "lets Raven store and recall images / PDFs / audio as memory\n"
            "  only needed if you want multimodal content remembered, not merely because such files exist",
            "让 Raven 把图片 / PDF / 音频也作为记忆来理解和检索\n"
            "  仅当你确有把多模态内容纳入记忆的需求时才配,有这类文件并不等于需要",
        ),
        "recommendation": (
            "Recommended: [bold]google/gemini-3-flash-preview[/bold]",
            "推荐 [bold]google/gemini-3-flash-preview[/bold]",
        ),
        "skip_note": (
            "Skipped; everything else is unaffected — configure it later if you come to need multimodal memory.",
            "已跳过;其余功能不受影响,日后确有把多模态内容纳入记忆的需求时再配即可。",
        ),
    },
}


_EMBEDDING_MODEL_PATTERNS = ("embed", "bge", "e5-", "gte-")
_MULTIMODAL_MODEL_PATTERNS = ("vision", "4o", "gemini", "pixtral", "qwen-vl", "qwen2-vl", "qwen2.5-vl")


def _fetch_everos_models(
    base_url: Optional[str],
    api_key: Optional[str],
    *,
    section: str = "llm",
    provider_name: Optional[str] = None,
) -> Optional[list[str]]:
    """Fetch available model ids from a provider endpoint. Never raises.

    For ``section="embedding"``, delegates to per-provider logic because
    each provider exposes embedding models differently.
    """
    if not base_url:
        return None
    if section == "embedding":
        return _fetch_embedding_models(base_url, api_key, provider_name)
    if section == "rerank":
        return _fetch_rerank_models(base_url, api_key, provider_name)
    if section == "multimodal":
        return _fetch_multimodal_models(base_url, api_key, provider_name)
    return _fetch_openai_models(base_url, api_key)


def _fetch_openai_models(
    base_url: str,
    api_key: Optional[str],
    *,
    params: Optional[dict[str, str]] = None,
) -> Optional[list[str]]:
    """``GET {base_url}/models`` with OpenAI-style response parsing."""
    import httpx

    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        return None
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    ids = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
    return sorted(ids) or None


def _fetch_deepinfra_models(
    api_key: Optional[str],
    reported_type: str,
    *,
    name_contains: Optional[str] = None,
) -> Optional[list[str]]:
    """Fetch DeepInfra models filtered by ``reported_type`` and optional name substring."""
    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get("https://api.deepinfra.com/models/list", headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    items = data if isinstance(data, list) else []
    ids = [
        m.get("model_name")
        for m in items
        if isinstance(m, dict)
        and m.get("reported_type") == reported_type
        and m.get("model_name")
        and (name_contains is None or name_contains in m.get("model_name", ""))
    ]
    return sorted(ids) or None


def _fetch_embedding_models(
    base_url: str,
    api_key: Optional[str],
    provider_name: Optional[str],
) -> Optional[list[str]]:
    """Provider-specific embedding model listing."""
    if provider_name == "openrouter":
        return _fetch_openai_models(base_url.rstrip("/") + "/embeddings", api_key)

    if provider_name == "siliconflow":
        return _fetch_openai_models(base_url, api_key, params={"type": "text", "sub_type": "embedding"})

    if provider_name == "deepinfra":
        return _fetch_deepinfra_models(api_key, "embeddings")

    # OpenAI, DashScope, custom — GET /models + name-based filter.
    ids = _fetch_openai_models(base_url, api_key)
    if ids is None:
        return None
    filtered = [i for i in ids if any(p in i.lower() for p in _EMBEDDING_MODEL_PATTERNS)]
    return filtered or None


def _fetch_rerank_models(
    base_url: str,
    api_key: Optional[str],
    provider_name: Optional[str],
) -> Optional[list[str]]:
    """Provider-specific rerank model listing."""
    if provider_name == "deepinfra":
        # The deepinfra provider hardcodes a Qwen3-Reranker chat template,
        # so only Qwen3-Reranker models are compatible.
        return _fetch_deepinfra_models(api_key, "reranker", name_contains="Qwen3-Reranker")

    if provider_name == "siliconflow":
        return _fetch_openai_models(base_url, api_key, params={"sub_type": "reranker"})

    if provider_name == "dashscope":
        return ["gte-rerank-v2"]

    if provider_name == "openrouter":
        return _fetch_openai_models(base_url, api_key, params={"output_modalities": "rerank"})

    # vllm / custom — no standard rerank listing.
    return None


def _fetch_multimodal_models(
    base_url: str,
    api_key: Optional[str],
    provider_name: Optional[str],
) -> Optional[list[str]]:
    """Provider-specific multimodal (vision) model listing."""
    if provider_name == "openrouter":
        return _fetch_openai_models(base_url, api_key, params={"input_modalities": "image"})

    # OpenAI, custom — GET /models + name-based filter.
    ids = _fetch_openai_models(base_url, api_key)
    if ids is None:
        return None
    filtered = [i for i in ids if any(p in i.lower() for p in _MULTIMODAL_MODEL_PATTERNS)]
    return filtered or None


def _everos_pick_model(
    *,
    base_url: Optional[str],
    api_key: Optional[str],
    example: str,
    allow_back: bool,
    section: str = "llm",
    provider_name: Optional[str] = None,
    recommendation: Optional[tuple[str, str]] = None,
) -> Any:
    """Pick a model id for an EverOS endpoint: fetch ``/models`` for a
    fuzzy-searchable list, else fall back to free text. Empty submit = back."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    console.print(_t("  [dim]⏳ Loading models…[/dim]", "  [dim]⏳ 正在拉取模型列表…[/dim]"))
    models = _fetch_everos_models(base_url, api_key, section=section, provider_name=provider_name)
    if recommendation:
        console.print(f"  [dim]{_t(*recommendation)}[/dim]")
    if models:
        question = questionary.autocomplete(
            _t(
                f"Model ({len(models)} available — type to filter):",
                f"模型(共 {len(models)} 个 — 输入可筛选):",
            ),
            choices=models,
            ignore_case=True,
            match_middle=True,
            placeholder=_back_placeholder(allow_back),
            style=RAVEN_STYLE,
            qmark=_QMARK,
        )
        # Trigger the completion popup immediately so the user sees
        # all available models without typing first.
        app = question.application

        def _show_completions() -> None:
            buf = app.current_buffer
            buf.start_completion()

        app.pre_run_callables.append(_show_completions)
        chosen = question.ask()
    else:
        console.print(
            _t(
                "  [dim]Couldn't list models from this endpoint — type the id manually.[/dim]",
                "  [dim]该端点拉不到模型列表 — 请手动输入模型 id。[/dim]",
            )
        )
        chosen = questionary.text(
            _t(f"Model id (e.g. {example}):", f"模型 id(如 {example}):"),
            placeholder=_back_placeholder(allow_back),
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
    if chosen is None:
        raise typer.Exit(1)
    chosen = chosen.strip()
    if allow_back and chosen == "":
        return _BACK
    if not chosen:
        raise typer.Exit(1)
    return chosen


def _everos_pick_creds_and_model(
    *,
    section: str,
    example: str,
    main_model: Optional[str],
    non_interactive: bool,
    recommendation: Optional[tuple[str, str]] = None,
) -> Any:
    """Mirror the main provider step for one EverOS model: pick a source
    (curated provider / custom) → API key → model. Returns a dict with
    ``model`` / ``api_key`` / ``base_url`` (plus ``provider`` for rerank), or
    ``_BACK`` when the user backs out of the source picker. Empty submit on any
    field rewinds one step."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    llm_section = _everos_section("llm")

    # For the LLM role, default to the main chat model's provider.
    # For other roles (embedding/rerank/multimodal), default to whichever
    # provider the LLM step just configured — the user likely has the
    # same API key and only needs to pick a different model.
    if section == "llm":
        default_provider = _resolve_model_provider(main_model or "")
        reuse_source = "main"
    else:
        default_provider = _match_provider_by_url(llm_section.get("base_url"))
        reuse_source = "llm"

    while True:  # source picker — a field-level back rewinds here
        choices: list[Any] = []
        default_choice = None
        for prov in _EVEROS_PROVIDERS:
            if section not in prov.get("supports", set()):
                continue
            is_default = default_provider is not None and prov["name"] == default_provider
            if is_default:
                if reuse_source == "main":
                    label = _t(
                        f"{prov['label']} (main model provider, reuse Key)",
                        f"{prov['label_zh']}（主模型服务商，复用 Key）",
                    )
                else:
                    label = _t(
                        f"{prov['label']} (memory LLM provider, reuse Key)",
                        f"{prov['label_zh']}（记忆 LLM 服务商，复用 Key）",
                    )
            else:
                label = _t(prov["label"], prov["label_zh"])
            choice = questionary.Choice(label, value=("provider", prov))
            choices.append(choice)
            if is_default:
                default_choice = choice.value
        choices.append(
            questionary.Choice(
                _t("Other (custom OpenAI-compatible endpoint)", "其他(自定义 OpenAI 兼容端点)"),
                value=("custom",),
            )
        )
        choices.append(questionary.Separator())
        choices.append(questionary.Choice(_t("Back", "返回"), value=_BACK))

        src = questionary.select(
            _t("Pick a provider (or reuse / custom):", "选择服务商(或复用 / 自定义):"),
            choices=choices,
            default=default_choice,
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if src is None:
            raise typer.Exit(1)
        if src is _BACK:
            return _BACK
        kind = src[0]

        # Resolve (api_key, base_url) from the chosen source.
        chosen_provider: Optional[str] = None
        if kind == "provider":
            chosen_provider = src[1]["name"]
            base_url = src[1]["base_url"]
            prefilled_key: Optional[str] = None
            if default_provider == src[1]["name"]:
                if reuse_source == "main":
                    prefilled_key = _resolve_reuse_llm_creds(main_model or "").get("api_key")
                else:
                    prefilled_key = llm_section.get("api_key")
            if prefilled_key:
                if reuse_source == "main":
                    console.print(
                        _t(
                            "  [dim]API key reused from main chat model.[/dim]",
                            "  [dim]已复用主对话模型的 API Key。[/dim]",
                        )
                    )
                else:
                    console.print(
                        _t(
                            "  [dim]API key reused from memory LLM.[/dim]",
                            "  [dim]已复用记忆 LLM 的 API Key。[/dim]",
                        )
                    )
                api_key = prefilled_key
            else:
                api_key = _prompt_api_key(src[1]["name"], allow_back=True)
                if api_key is _BACK:
                    continue
        else:  # custom
            base_url = _prompt_text(_t("Base URL (must include /v1):", "Base URL(需包含 /v1):"), allow_back=True)
            if base_url is _BACK:
                continue
            api_key = _prompt_text(_t("API key (hidden):", "API Key(隐藏输入):"), secret=True, allow_back=True)
            if api_key is _BACK:
                continue

        # Guard against a source that resolved to an empty key / endpoint —
        # set_everos_section drops None values, which would otherwise persist a
        # section with a model but no usable endpoint.
        if not (api_key and base_url):
            console.print(
                _t(
                    "  [yellow]✗ Missing API key or Base URL for this source — pick another.[/yellow]",
                    "  [yellow]✗ 该来源缺少 API Key 或 Base URL — 请换一个。[/yellow]",
                )
            )
            continue

        # rerank: resolve service type + override base_url when needed.
        rerank_provider: Optional[str] = None
        if section == "rerank":
            chosen_prov_dict = src[1] if kind == "provider" else None
            if chosen_prov_dict and chosen_prov_dict.get("rerank_provider"):
                rerank_provider = chosen_prov_dict["rerank_provider"]
                if chosen_prov_dict.get("rerank_base_url"):
                    base_url = chosen_prov_dict["rerank_base_url"]
            else:
                rerank_provider = questionary.select(
                    _t("Rerank service type:", "rerank 服务类型:"),
                    choices=[
                        questionary.Choice("deepinfra", value="deepinfra"),
                        questionary.Choice("vllm", value="vllm"),
                        questionary.Choice("dashscope", value="dashscope"),
                        questionary.Choice(_t("Back", "返回"), value=_BACK),
                    ],
                    style=RAVEN_STYLE,
                    qmark=_QMARK,
                ).ask()
                if rerank_provider is None:
                    raise typer.Exit(1)
                if rerank_provider is _BACK:
                    continue

        model = _everos_pick_model(
            base_url=base_url,
            api_key=api_key,
            example=example,
            allow_back=True,
            section=section,
            provider_name=chosen_provider,
            recommendation=recommendation,
        )
        if model is _BACK:
            continue

        result: dict[str, Any] = {"model": model, "api_key": api_key, "base_url": base_url}
        if rerank_provider:
            result["provider"] = rerank_provider
        return result


def _config_everos_role(
    *, section: str, main_model: Optional[str], non_interactive: bool, warnings: list[str], skip_test: bool = False
) -> Any:
    """Configure one EverOS memory role (llm / embedding / rerank / multimodal)
    with the unified provider→key→model flow, reuse shortcuts, and a back loop.

    Returns ``None`` normally; returns ``_ABORT_EVEROS`` when the user gives up a
    required role (the caller then disables EverOS and keeps Markdown memory)."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.config.update_everos import clear_everos_section, set_everos_section

    role = _EVEROS_ROLES[section]
    label_en, label_zh = role["label"]
    purpose_en, purpose_zh = role["purpose"]
    optional = role["optional"]
    verify_label = _t(label_en, label_zh)

    # Tell the user what this model is for before asking them to configure it.
    # Header sits on the 2-space info column (bold accent); the purpose nests
    # one line under it (dim), matching the layout system used everywhere else.
    tag = _t("optional", "可选")
    console.print()
    # highlight=False so Rich's default highlighter doesn't tint the dim prose
    # (parens/numbers/words) and make an informational hint read like an error.
    console.print(
        _t(
            f"  [bold #fbe23f]{label_en}[/bold #fbe23f]"
            + (f" [dim]({tag})[/dim]" if optional else "")
            + f"\n  [dim]{purpose_en}[/dim]",
            f"  [bold #fbe23f]{label_zh}[/bold #fbe23f]"
            + (f" [dim]({tag})[/dim]" if optional else "")
            + f"\n  [dim]{purpose_zh}[/dim]",
        ),
        highlight=False,
    )

    while True:  # role-menu loop — a back-out of the source picker returns here
        current = _everos_section(section).get("model")
        if current:
            choices = [
                questionary.Choice(_t(f"Keep current: {current}", f"沿用当前:{current}"), value="keep"),
                questionary.Choice(_t("Reconfigure", "重新配置"), value="redo"),
            ]
            if optional:
                choices.append(questionary.Choice(_t("Skip", "跳过"), value="off"))
            action = questionary.select(
                _t("Already configured — what now?", "已配置,怎么处理?"),
                choices=choices,
                style=RAVEN_STYLE,
                qmark=_QMARK,
            ).ask()
            if action is None:
                raise typer.Exit(1)
            if action == "keep":
                return
            if action == "off":
                clear_everos_section(section)
                console.print(_t(f"  [dim]{label_en} skipped.[/dim]", f"  [dim]已跳过 {label_zh}。[/dim]"))
                return
        elif optional:
            action = questionary.select(
                _t("Configure it?", "要配置吗?"),
                choices=[
                    questionary.Choice(_t("Configure", "配置"), value="redo"),
                    questionary.Choice(_t("Skip", "跳过"), value="skip"),
                ],
                style=RAVEN_STYLE,
                qmark=_QMARK,
            ).ask()
            if action is None:
                raise typer.Exit(1)
            if action == "skip":
                note_en, note_zh = role.get("skip_note", (f"Skipped {label_en}.", f"已跳过 {label_zh}。"))
                console.print(_t(f"  [dim]{note_en}[/dim]", f"  [dim]{note_zh}[/dim]"))
                return
        # A required role with nothing configured falls straight into the picker.

        result = _everos_pick_creds_and_model(
            section=section,
            example=role["example"],
            main_model=main_model,
            non_interactive=non_interactive,
            recommendation=role.get("recommendation"),
        )
        if result is _BACK:
            if optional or _everos_section(section).get("model"):
                # Optional roles offer Skip; a required role that already has a
                # value on disk falls back to its keep/reconfigure menu. Either
                # way, re-show the role menu rather than forcing the give-up exit.
                continue
            # A required role with nothing configured has no Skip, so backing out
            # of the picker would loop forever. Offer a bounded exit: keep trying,
            # or give up EverOS and fall back to Markdown memory.
            action = questionary.select(
                _t(
                    f"{label_en} is required for EverOS memory. What would you like to do?",
                    f"{label_zh} 是 EverOS 记忆必需的。想做什么?",
                ),
                choices=[
                    questionary.Choice(_t("Pick a provider / model", "选择服务商 / 模型"), value="retry"),
                    questionary.Choice(
                        _t("Give up EverOS (use native Markdown memory)", "放弃 EverOS(改用原生 Markdown 记忆)"),
                        value="abort",
                    ),
                ],
                style=RAVEN_STYLE,
                qmark=_QMARK,
            ).ask()
            if action is None:
                raise typer.Exit(1)
            if action == "retry":
                continue
            return _ABORT_EVEROS

        if role["verify"] and skip_test:
            console.print(
                _t(
                    f"  [dim]Skipping the {verify_label} test call (--skip-test).[/dim]",
                    f"  [dim]已跳过 {verify_label} 的测试调用(--skip-test)。[/dim]",
                )
            )
            ok = True
        elif section == "llm":
            ok = _verify_everos_llm(
                verify_label,
                model=result["model"],
                api_key=result["api_key"],
                base_url=result["base_url"],
                non_interactive=non_interactive,
                warnings=warnings,
                continue_hint=role.get("continue_hint"),
            )
        elif section == "embedding":
            ok = _verify_embedding_dim(
                model=result["model"],
                api_key=result["api_key"],
                base_url=result["base_url"],
                non_interactive=non_interactive,
            )
        elif section == "rerank":
            ok = _verify_rerank(
                verify_label,
                model=result["model"],
                api_key=result["api_key"],
                base_url=result["base_url"],
                rerank_provider=result.get("provider"),
                non_interactive=non_interactive,
                warnings=warnings,
                continue_hint=role.get("continue_hint"),
            )
        elif section == "multimodal":
            ok = _verify_everos_llm(
                verify_label,
                model=result["model"],
                api_key=result["api_key"],
                base_url=result["base_url"],
                non_interactive=non_interactive,
                warnings=warnings,
                continue_hint=role.get("continue_hint"),
            )
        else:
            ok = True
        if not ok:
            continue

        set_everos_section(section, result)
        console.print(
            _t(
                f"  [green]✓ {label_en} configured.[/green]",
                f"  [green]✓ 已配置 {label_zh}。[/green]",
            )
        )
        return


def _step4_memory(
    *, skip: bool, non_interactive: bool, main_model: Optional[str], warnings: list[str], skip_test: bool = False
) -> object:
    """Step 4 — EverOS long-term memory (enable + model sub-screens).

    The bootstrap seeds ``memory.backend="everos"`` (schema default), so this
    step's job is to either confirm it by configuring the required models or
    resolve it back to ``None`` (native Markdown) on skip / non-interactive /
    decline. ``_memory_enabled`` gates on both required models (llm + embedding)
    being present, so a fresh modelless seed is treated as "not yet enabled" (the user still sees
    the enable prompt) and the "keep current" path only triggers once models
    are actually on disk.
    """
    _step_header(4, _t("EverOS long-term memory", "EverOS 长期记忆"))

    if skip or non_interactive:
        # Never configured the required models here → disable backend-driven
        # memory so runtime doesn't activate EverOS without an llm/embedding.
        # (``_memory_enabled`` already gates on both required models, so an
        # already-enabled+configured setup is preserved.)
        if not _memory_enabled():
            _set_memory_backend(None)
        console.print(
            _t(
                "  [dim]Keeping native Markdown memory.[/dim]",
                "  [dim]保持原生 Markdown 记忆。[/dim]",
            )
        )
        return None

    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    if _memory_enabled():
        action = questionary.select(
            _t(
                "EverOS long-term memory is already enabled. What would you like to do?",
                "EverOS 长期记忆已启用。想做什么?",
            ),
            choices=[
                questionary.Choice(_t("Keep it enabled", "保持启用"), value="keep"),
                questionary.Choice(_t("Reconfigure", "重新配置"), value="redo"),
            ],
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if action is None:
            raise typer.Exit(1)
        if action == "keep":
            return None  # backend already "everos" + models on disk; leave as-is
    else:
        console.print(
            _t(
                "  [dim]Enable to give Raven EverOS's stronger long-term memory — it needs a memory LLM and an "
                "embedding model. Or skip and keep Raven's built-in Markdown memory (no extra setup).[/dim]",
                "  [dim]启用后,Raven 获得 EverOS 提供的更强长期记忆能力,需额外配置记忆用的 LLM 和 embedding 模型;"
                "不启用则使用 Raven 原生 Markdown 记忆,无需额外配置。[/dim]",
            )
        )
        action = questionary.select(
            _t("Enable EverOS long-term memory?", "启用 EverOS 长期记忆?"),
            choices=[
                questionary.Choice(_t("Enable (configure the memory models)", "启用(继续配置记忆模型)"), value="on"),
                questionary.Choice(
                    _t(
                        "Don't enable (use Raven's native Markdown memory)",
                        "不启用(使用 Raven 原生 Markdown 记忆)",
                    ),
                    value="off",
                ),
            ],
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if action is None:
            raise typer.Exit(1)
        if action == "off":
            _set_memory_backend(None)
            console.print(
                _t(
                    "  [dim]Using native Markdown memory.[/dim]",
                    "  [dim]使用原生 Markdown 记忆。[/dim]",
                )
            )
            return None

    # Ensure the EverOS home directory has its config templates (everos.toml
    # + ome.toml) BEFORE writing model sections — set_everos_section merges
    # into the template so default sections (memory/sqlite/lancedb/api) are
    # preserved. Also creates ome.toml which the runtime requires.
    from raven.config.update_everos import configure_everos_env, ensure_everos_home

    configure_everos_env()
    ensure_everos_home()

    # Configure required models FIRST, then flip the backend on — so a Ctrl+C
    # mid-configuration leaves backend at its prior (disabled) value rather
    # than an enabled-but-modelless state.
    for _role in ("llm", "embedding", "rerank", "multimodal"):
        # Each role prints one leading blank before its own header, so no extra
        # separator here — avoids the double blank line between roles.
        outcome = _config_everos_role(
            section=_role,
            main_model=main_model,
            non_interactive=non_interactive,
            warnings=warnings,
            skip_test=skip_test,
        )
        if outcome is _ABORT_EVEROS:
            _set_memory_backend(None)
            console.print(
                _t(
                    "  [dim]Gave up EverOS; keeping native Markdown memory.[/dim]",
                    "  [dim]已放弃 EverOS,改用原生 Markdown 记忆。[/dim]",
                )
            )
            return None
    _set_memory_backend("everos")
    return None


# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------


def _print_next_steps(*, warnings: list[str]) -> None:
    from rich.table import Table

    console.print()
    if warnings:
        console.print(
            Panel(
                _t(
                    "[bold yellow]⚠ Setup finished with warnings[/bold yellow]",
                    "[bold yellow]⚠ 配置完成,但有警告[/bold yellow]",
                )
                + "\n\n"
                + _t(
                    "[dim]These items didn't pass a connectivity test:[/dim] ",
                    "[dim]以下项目未通过连通测试:[/dim] ",
                )
                + f"{', '.join(warnings)}\n"
                + _t(
                    "[dim]Fix them before relying on the related features "
                    "(re-run [/dim][#fbe23f]raven onboard[/#fbe23f][dim] to reconfigure).[/dim]",
                    "[dim]在依赖相关功能前请先修复(重新运行 [/dim][#fbe23f]raven onboard[/#fbe23f][dim] 重新配置)。[/dim]",
                ),
                border_style="yellow",
                padding=(1, 2),
            )
        )
    else:
        console.print(
            Panel(
                _t(
                    "[bold green]🎉 Setup complete![/bold green]",
                    "[bold green]🎉 配置完成![/bold green]",
                ),
                border_style="green",
                padding=(0, 2),
            )
        )

    # Recap what was configured (read from disk) so the user has closure.
    provs = ", ".join(_provider_label(n).split(" (")[0] for n in _configured_providers()) or "—"
    run_loc = (
        _t("Host (direct)", "本机直接运行")
        if _current_sandbox_backend() == "none"
        else _t("Sandbox (boxlite)", "沙箱(boxlite)")
    )
    chans = ", ".join(_enabled_channels()) or _t("none", "无")
    mem = _t("EverOS", "EverOS") if _memory_enabled() else _t("native Markdown", "原生 Markdown")
    recap = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    recap.add_column(style="dim", no_wrap=True)
    recap.add_column()
    recap.add_row(_t("Provider", "服务商"), provs)
    recap.add_row(_t("Default model", "默认模型"), _load_current_default_model() or "—")
    recap.add_row(_t("Run location", "运行位置"), run_loc)
    recap.add_row(_t("Channels", "聊天渠道"), chans)
    recap.add_row(_t("Memory", "长期记忆"), mem)
    console.print(
        Panel(
            recap,
            title=f"[bold]{_t('Your setup', '你的配置')}[/bold]",
            title_align="left",
            border_style="#8a6d00",
            padding=(1, 2),
        )
    )

    table = Table(show_header=False, box=None, padding=(0, 3, 0, 0))
    table.add_column(style="#fbe23f", no_wrap=True)
    table.add_column(style="dim")
    table.add_row("raven", _t("launch the native TUI (default)", "启动原生 TUI(默认)"))
    table.add_row("raven gateway", _t("run the gateway (serve channels)", "运行网关(对接渠道)"))
    table.add_row('raven agent -m "hello, world"', _t("ask a one-shot question", "一次性提问"))
    table.add_row("raven channels list", _t("see connected chat channels", "查看已接入的渠道"))
    table.add_row("raven provider list", _t("check your provider config", "检查当前服务商配置"))
    console.print(
        Panel(
            table,
            title=f"[bold]{_t('Get started', '开始使用')}[/bold]",
            title_align="left",
            border_style="#c8a900",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Wizard runner (screen state machine) + reusable entry point
# ---------------------------------------------------------------------------


def run_wizard(
    *,
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    channel: Optional[str] = None,
    skip_sandbox: bool = False,
    skip_channel: bool = False,
    skip_memory: bool = False,
    skip_deep_research: bool = False,
    non_interactive: bool = False,
    yes: bool = False,
    reset: bool = False,
    skip_test: bool = False,
) -> None:
    """Run the 5-step onboarding wizard end-to-end.

    The reusable entry point: the ``onboard`` CLI command and the startup gate
    both call this. Screens form a state machine so a ``0) Back`` choice can
    rewind one step; Ctrl+C exits keeping whatever was already written.

    Internal INFO logs (config writes, etc.) are hushed for the wizard's
    duration so they don't clutter the UI, then restored in ``finally`` —
    display-only; logging elsewhere is unaffected.
    """
    from loguru import logger as _logger

    _logger.disable("raven")
    try:
        _run_wizard_body(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            channel=channel,
            skip_sandbox=skip_sandbox,
            skip_channel=skip_channel,
            skip_memory=skip_memory,
            skip_deep_research=skip_deep_research,
            non_interactive=non_interactive,
            yes=yes,
            reset=reset,
            skip_test=skip_test,
        )
    finally:
        _logger.enable("raven")


def _step5_deep_research(*, skip: bool, non_interactive: bool, warnings: list[str]) -> object:
    """Step 5 — deep_research (MiroThinker) tool, optional, forward-only.

    Delegates to the shared configure flow (also reachable via
    ``raven deep-research enable``). Skipped on --skip-deep-research or
    non-interactive; leaving it unconfigured just means the opt-in tool stays
    unregistered.
    """
    _step_header(5, _t("Deep research tool", "深度研究工具"))
    if skip or non_interactive:
        console.print(
            _t(
                "  [dim]Skipping deep_research (configure later: raven deep-research enable).[/dim]",
                "  [dim]跳过 deep_research(以后可用 raven deep-research enable 配置)。[/dim]",
            )
        )
        return None
    from raven.cli.deep_research_commands import configure_deep_research

    configure_deep_research(non_interactive=non_interactive, warnings=warnings)
    return None


def _run_wizard_body(
    *,
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    channel: Optional[str] = None,
    skip_sandbox: bool = False,
    skip_channel: bool = False,
    skip_memory: bool = False,
    skip_deep_research: bool = False,
    non_interactive: bool = False,
    yes: bool = False,
    reset: bool = False,
    skip_test: bool = False,
) -> None:
    global _LANG
    _check_tty_or_die(non_interactive)
    _LANG = _config_language()  # start from the saved language (default "en")
    if not non_interactive:
        _pick_language()  # may change _LANG (persisted after bootstrap below)
    _handle_existing_config(reset=reset, yes=yes, non_interactive=non_interactive)
    _bootstrap_empty_config()
    if not non_interactive:
        from raven.config.update import set_language

        set_language(_LANG)  # persist now that config.json exists

    console.print()
    console.print(
        Panel(
            _t(
                "[bold #fbe23f]✨ Welcome to the Raven setup wizard[/bold #fbe23f]\n\n"
                "[dim]We'll configure, in order:[/dim]\n"
                "  [#fbe23f]①[/#fbe23f] LLM      [#fbe23f]②[/#fbe23f] Run location      "
                "[#fbe23f]③[/#fbe23f] Chat channel      [#fbe23f]④[/#fbe23f] Long-term memory      "
                "[#fbe23f]⑤[/#fbe23f] Deep research\n\n"
                "[dim]↑↓ select · Enter confirm · Ctrl+C quit anytime — anything already written is kept.[/dim]",
                "[bold #fbe23f]✨ 欢迎使用 Raven 配置向导[/bold #fbe23f]\n\n"
                "[dim]我们将依次配置:[/dim]\n"
                "  [#fbe23f]①[/#fbe23f] LLM      [#fbe23f]②[/#fbe23f] 运行位置      "
                "[#fbe23f]③[/#fbe23f] 聊天渠道      [#fbe23f]④[/#fbe23f] 长期记忆      "
                "[#fbe23f]⑤[/#fbe23f] 深度研究\n\n"
                "[dim]↑↓ 选择 · Enter 确认 · 随时 Ctrl+C 退出 — 已写入的配置会保留。[/dim]",
            ),
            border_style="#c8a900",
            padding=(1, 2),
        )
    )

    warnings: list[str] = []

    # Screen state machine. Each screen returns ``_BACK`` to rewind or anything
    # else to advance. Step 1 is required; backing out of it from the first
    # screen is a no-op (there's no earlier screen).
    screens: list[Callable[[], object]] = [
        lambda: _step1_provider(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            non_interactive=non_interactive,
            warnings=warnings,
            skip_test=skip_test,
        ),
        lambda: _step2_sandbox(skip=skip_sandbox, non_interactive=non_interactive),
        lambda: _step3_channel(channel=channel, skip=skip_channel, non_interactive=non_interactive),
        lambda: _step4_memory(
            skip=skip_memory,
            non_interactive=non_interactive,
            main_model=_load_current_default_model(),
            warnings=warnings,
            skip_test=skip_test,
        ),
        lambda: _step5_deep_research(
            skip=skip_deep_research,
            non_interactive=non_interactive,
            warnings=warnings,
        ),
    ]

    index = 0
    while index < len(screens):
        result = screens[index]()
        if result is _BACK:
            if index == 0:
                # The language picker ran before the state machine, so Step 1
                # is the first *numbered* screen but not the first screen the
                # user saw. Backing out of it returns to the language picker:
                # re-pick (persisting the choice) and then re-display Step 1 in
                # the chosen language. Step 1 stays required -- we never skip
                # past it, which would leave provider/model unwritten and
                # re-trip the startup gate into an infinite loop.
                _pick_language()
                from raven.config.update import set_language

                set_language(_LANG)
            else:
                index -= 1
        else:
            index += 1

    _print_next_steps(warnings=warnings)


# ---------------------------------------------------------------------------
# Startup gate — invoked by bare `raven` / `raven agent` / TUI entry points
# ---------------------------------------------------------------------------


def ensure_configured_or_onboard(*, non_interactive: bool = False) -> bool:
    """Run the wizard when the required config (provider + model) is missing.

    Returns ``True`` if config was already complete (caller proceeds straight
    to the session), ``False`` if the wizard ran (config is now populated). In
    a non-interactive context with missing config, the wizard's TTY check
    will raise — callers on non-TTY paths must guard before invoking.
    """
    if _is_config_populated():
        return True
    run_wizard(non_interactive=non_interactive)
    return False


# ---------------------------------------------------------------------------
# Typer entry point
# ---------------------------------------------------------------------------


def register(app: typer.Typer) -> None:
    """Attach the ``onboard`` command to ``app``."""

    @app.command()
    def onboard(
        provider: Optional[str] = typer.Option(None, "--provider", help="LLM provider name (skips Step 1's prompt)"),
        api_key: Optional[str] = typer.Option(None, "--api-key", help="API key for the chosen provider"),
        base_url: Optional[str] = typer.Option(None, "--base-url", help="Custom OpenAI-compatible base URL"),
        model: Optional[str] = typer.Option(None, "--model", help="Default model id (e.g. 'openai/gpt-4o-mini')"),
        channel: Optional[str] = typer.Option(None, "--channel", help="Channel to enable in Step 3"),
        skip_sandbox: bool = typer.Option(False, "--skip-sandbox", help="Skip Step 2 (run location)"),
        skip_channel: bool = typer.Option(False, "--skip-channel", help="Skip Step 3 (channel setup)"),
        skip_memory: bool = typer.Option(False, "--skip-memory", help="Skip Step 4 (long-term memory)"),
        skip_deep_research: bool = typer.Option(False, "--skip-deep-research", help="Skip Step 5 (deep_research tool)"),
        non_interactive: bool = typer.Option(
            False,
            "--non-interactive",
            help="Run without prompts (requires flags for any missing field)",
        ),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip all confirm prompts"),
        reset: bool = typer.Option(
            False,
            "--reset",
            help="Re-run the wizard over an existing config (does not erase it; each step keeps current values as defaults)",
        ),
        skip_test: bool = typer.Option(
            False,
            "--skip-test",
            help="Skip the one-shot test message (avoids a billed call; connectivity is still checked)",
        ),
    ) -> None:
        """Five-step setup wizard: LLM provider → sandbox → channel → memory → deep research."""
        run_wizard(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            channel=channel,
            skip_sandbox=skip_sandbox,
            skip_channel=skip_channel,
            skip_memory=skip_memory,
            skip_deep_research=skip_deep_research,
            non_interactive=non_interactive,
            yes=yes,
            reset=reset,
            skip_test=skip_test,
        )


__all__ = ["register", "run_wizard", "ensure_configured_or_onboard"]
