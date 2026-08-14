"""Six-step onboarding wizard: LLM provider → sandbox → channel → memory → deep research → import.

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
  6. Cold-start import from other AI tools (optional)
  7. Done

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

from raven.cli import onboard_channels, onboard_everos
from raven.cli._helpers import (
    DEFAULT_PROBE_MESSAGE,
    print_probe_troubleshooting,
    send_probe,
)
from raven.cli._theme import POINTER, QMARK
from raven.providers.registry import (
    CRED_ENDPOINT,
    CRED_LOCAL,
    CRED_OAUTH,
    credential_kind,
)
from raven.providers.wire import stored_model_id


class _ThemedConsole(Console):
    """Console that applies the light/dark theme on its first render.

    Theming is deferred to the first ``print`` (never at import) so plain
    ``raven ...`` commands don't detect or probe the terminal. Because every
    onboard render goes through ``print`` on this instance, there is no
    "push the theme before rendering" ordering constraint and no dependence on
    which entry point (wizard, ``raven deep-research enable``, ...) ran first.
    """

    _themed: bool = False

    def print(self, *args: Any, **kwargs: Any) -> None:
        if not self._themed:
            from raven.cli._theme import build_rich_theme, detect_scheme

            self.push_theme(build_rich_theme(detect_scheme()))
            self._themed = True
        super().print(*args, **kwargs)


console = _ThemedConsole()

_TOTAL_STEPS = 6

# Sentinel returned by a screen function to ask the runner to go back one
# screen; ``None`` from a picker means Ctrl+C (exit).
_BACK = object()

# Sentinel a required EverOS role returns when the user chooses to give up EverOS
# rather than configure it; ``_step4_memory`` then leaves memory disabled.
_ABORT_EVEROS = object()

# Unified prompt chrome (display-only), shared with every other command's
# prompts: a single-space qmark renders as one blank, which -- with
# questionary's own leading space -- puts every prompt line on the same 2-space
# column as our printed help/status lines, so the left edge stays flush instead
# of jittering between 1- and 2-space indents.
_QMARK = QMARK
_POINTER = POINTER

# UI language, chosen on the wizard's first screen. ``_t`` returns the English
# or Chinese variant so every later prompt / message stays bilingual.
_LANG = "en"


def _t(en: str, zh: str) -> str:
    """Return ``zh`` when the user picked Chinese, else ``en``."""
    return zh if _LANG == "zh" else en


# ---------------------------------------------------------------------------
# Curated provider catalogue surfaced in Step 1's picker.
# ---------------------------------------------------------------------------


# Sentinel entries the picker renders as its own step rather than a provider.
_PICK_LITELLM_VENDOR = "__litellm_vendor__"

# Every provider we carry a spec for, grouped the way the picker shows them.
# Ordered by expected use inside each group; the groups themselves are what a
# user scans for, so a provider is never hidden the way eight of them were when
# this list was a hand-picked subset of the registry.
_CURATED_GROUPS: list[dict[str, Any]] = [
    {
        "kind": "api_key",
        "providers": [
            {
                "name": "openrouter",
                "label": "OpenRouter (recommended - one key, many models)",
                "label_zh": "OpenRouter(推荐 · 一个 Key 调用多家模型)",
            },
            {"name": "openai", "label": "OpenAI", "label_zh": "OpenAI"},
            {"name": "anthropic", "label": "Anthropic", "label_zh": "Anthropic"},
            {"name": "gemini", "label": "Gemini", "label_zh": "Gemini"},
            # Marked because EverMind and MiniMax collaborate in the open, not
            # because it ranks differently on capability -- "open-source" rather
            # than a bare "partner", which in a list of vendors reads as paid
            # placement. The OAuth MiniMax entries carry no marker: the same
            # vendor is already marked here, and stacking it on "(OAuth)" makes
            # the row twice as long for nothing.
            {
                "name": "minimax",
                "label": "MiniMax (open-source partner)",
                "label_zh": "MiniMax(开源合作伙伴)",
            },
            {"name": "deepseek", "label": "DeepSeek", "label_zh": "DeepSeek"},
            {"name": "zai", "label": "Z.ai (Zhipu)", "label_zh": "Z.ai(智谱)"},
            {"name": "dashscope", "label": "DashScope", "label_zh": "阿里云百炼"},
            {"name": "moonshot", "label": "Moonshot", "label_zh": "Moonshot(月之暗面)"},
            {"name": "volcengine", "label": "VolcEngine", "label_zh": "火山方舟"},
            {"name": "siliconflow", "label": "SiliconFlow", "label_zh": "硅基流动"},
            {"name": "groq", "label": "Groq", "label_zh": "Groq"},
            {"name": "aihubmix", "label": "AiHubMix", "label_zh": "AiHubMix"},
            {"name": "azure_openai", "label": "Azure OpenAI", "label_zh": "Azure OpenAI"},
        ],
    },
    {
        "kind": "oauth",
        "providers": [
            {
                "name": "github_copilot",
                "label": "GitHub Copilot (OAuth)",
                "label_zh": "GitHub Copilot(OAuth 登录)",
            },
            {"name": "openai_codex", "label": "OpenAI Codex (OAuth)", "label_zh": "OpenAI Codex(OAuth 登录)"},
            {
                "name": "minimax_global",
                "label": "MiniMax Global (OAuth)",
                "label_zh": "MiniMax Global(OAuth 登录)",
            },
            {"name": "minimax_cn", "label": "MiniMax CN (OAuth)", "label_zh": "MiniMax CN(OAuth 登录)"},
        ],
    },
    {
        "kind": "local",
        "providers": [
            {"name": "ollama_chat", "label": "Ollama (local)", "label_zh": "Ollama(本地)"},
            {"name": "hosted_vllm", "label": "vLLM / self-hosted", "label_zh": "vLLM / 自托管"},
        ],
    },
    {
        "kind": "fallback",
        "providers": [
            {
                "name": _PICK_LITELLM_VENDOR,
                "label": "Another supported vendor (type to search)",
                "label_zh": "其他支持的厂商(输入可搜索)",
            },
            {
                "name": "custom",
                "label": "Self-hosted OpenAI-compatible endpoint",
                "label_zh": "自建 OpenAI 兼容端点",
            },
        ],
    },
]

# Flat view for callers that only need "which providers does the wizard offer".
_CURATED_PROVIDERS: list[dict[str, Any]] = [
    entry for group in _CURATED_GROUPS for entry in group["providers"] if entry["name"] != _PICK_LITELLM_VENDOR
]

_QUESTIONARY_INSTALL_HINT = (
    "[red]Missing dependency:[/red] [accent]questionary[/accent] is required for "
    "interactive onboarding.\n"
    "Install it with: [accent]uv add 'questionary>=2.0,<3.0'[/accent]\n"
    "Or re-run with [accent]--non-interactive[/accent] plus the relevant flags."
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
            "[heading]Let's set up Raven — first, choose your language.[/heading]\n"
            "[dim]开始配置 Raven — 请先选择语言。[/dim]",
            title="[bold][accent]Raven setup[/accent][/bold]",
            title_align="left",
            border_style="border",
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
    dots = " ".join("[accent]●[/accent]" if i <= n else "[grey37]○[/grey37]" for i in range(1, _TOTAL_STEPS + 1))
    console.print()
    console.print(
        Panel(
            f"[heading]{title}[/heading]",
            title=f"[bold][accent]{_t('Step', '步骤')} {n}/{_TOTAL_STEPS}[/accent][/bold]",
            title_align="left",
            subtitle=dots,
            subtitle_align="right",
            border_style="border",
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
            "[accent]raven onboard --non-interactive --provider <name> --api-key <key>[/accent]"
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
    """Names of providers with a usable API key or OAuth token."""
    from raven.config.update_providers import list_providers

    return [row["name"] for row in list_providers() if row["configured"]]


def _is_config_populated() -> bool:
    """True iff the provider that serves the configured model has its credentials.

    "Populated" for the startup gate means the required step (Step 1) is
    satisfied: a default model plus credentials for whoever answers it. Either
    alone is not enough to talk to a model.

    Which provider answers is not re-derived here. The config already resolves
    it -- honoring an explicit ``agents.defaults.provider``, then prefix over
    keyword, and declining to fall back to an OAuth provider -- and a second
    derivation from the model-id prefix is how this gate came to disagree with
    ``raven status`` about a signed-in provider. What is left to ask is whether
    that provider's credentials are actually on disk, which is the one thing the
    resolver takes on trust for the OAuth families.
    """
    from raven.config.loader import load_config

    data = _load_raw_config()
    model = (data.get("agents", {}) or {}).get("defaults", {}).get("model")
    if not model:
        return False

    try:
        serving = load_config().get_provider_name(str(model))
    except Exception:
        # A config too damaged to resolve is not a configured one, and the wizard
        # is a better answer here than a traceback. The raw read above already
        # raised on a syntax error, so this is the semantic case.
        return False

    return bool(serving and serving in _configured_providers())


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
            "[red]Existing config detected.[/red] Pass [accent]--reset[/accent] (or "
            "[accent]--yes[/accent]) to overwrite, or edit in place with "
            "[accent]raven provider set[/accent] / [accent]raven channels enable[/accent]."
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
    back to ``None`` when the user opts out or never configures the one required
    model (``_memory_enabled`` gates on the llm role being present, not just the
    backend name; embedding and rerank only cost recall quality).

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
    onboard_everos._init_extension_block_defaults()
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
    """Resolve a user-supplied provider name (kebab or snake) to a registry key.

    A vendor LiteLLM routes to but Raven carries no spec for is configurable
    too: the wizard has no default model or OAuth flow to offer it, but it does
    not need one -- the credentials go in under the vendor's name and the model
    list comes from the vendor itself. Callers must therefore treat the spec as
    optional metadata, not as permission.
    """
    from raven.config.update_providers import provider_field_specs
    from raven.providers.registry import find_by_name, normalize_provider_name

    candidate = name.replace("-", "_")
    try:
        provider_field_specs(candidate)
    except KeyError as exc:
        raise typer.BadParameter(str(exc))
    spec = find_by_name(candidate)
    if spec is None:
        # No spec, but provider_field_specs above already confirmed LiteLLM
        # routes to it, which is all configuring it takes.
        return normalize_provider_name(candidate)
    # Return the current name: everything downstream compares and stores by it,
    # and a former name would have the wizard reading one section and writing
    # another.
    return spec.name


def _back_placeholder(allow_back: bool, label: Optional[str] = None) -> Any:
    """A faint in-field placeholder telling the user what an empty submit does.

    Rendered greyed inside the input (via prompt_toolkit's ``placeholder``),
    it disappears the moment they type and leaves nothing behind once the
    prompt is answered. Returns ``None`` when back isn't offered. ``label``
    overrides the default "go back" wording for prompts where an empty submit
    means something else (e.g. cancelling rather than rewinding a step).
    """
    if not allow_back:
        return None
    return [("fg:#6c6c6c italic", label or _t("empty ↵ to go back", "留空回车返回上一步"))]


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


def _select_provider_row() -> Optional[str]:
    """Render the grouped provider list once and return the raw choice.

    Separate from `_select_provider` so backing out of the vendor sub-list can
    show this list again rather than unwinding the whole step.
    """
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    choices: list[Any] = []
    for group in _CURATED_GROUPS:
        # A rule between groups, so the API-key providers, the OAuth ones, the
        # local deployments and the two fallbacks read as four decisions rather
        # than one list of twenty.
        if choices:
            choices.append(questionary.Separator())
        for entry in group["providers"]:
            choices.append(
                questionary.Choice(
                    _t(entry["label"], entry.get("label_zh", entry["label"])),
                    value=entry["name"],
                )
            )
    choices.append(questionary.Separator())
    choices.append(questionary.Choice(_t("Back", "返回"), value=_BACK))

    return questionary.select(
        _t("Provider:", "服务商:"),
        choices=choices,
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()  # None on Ctrl+C


def _select_provider() -> Optional[str]:
    """Interactive provider picker built from the curated catalogue.

    Returns the provider name, ``_BACK`` if the user chose the back sentinel,
    or ``None`` on Ctrl+C.
    """
    picked = _select_provider_row()
    while picked == _PICK_LITELLM_VENDOR:
        # Second step rather than a hundred more rows: LiteLLM routes to far more
        # vendors than anyone wants to scroll, and typing the name is how someone
        # who already knows which one they want gets there.
        #
        # Backing out of it returns to this list, not out of the step: the user
        # opened a sub-list, so empty-submit means "close the sub-list". Passing
        # its _BACK straight up sent them to the language screen instead.
        typed = _prompt_litellm_vendor()
        if typed is None:
            return None
        if typed is not _BACK:
            return typed
        picked = _select_provider_row()
    return picked  # _BACK on back, None on Ctrl+C


def _litellm_vendor_choices() -> list[str]:
    """Vendor names for the second step: the ones the picker does not already show.

    Read from the packaged snapshot rather than LiteLLM itself, so offering them
    costs no import on a path that only renders choices.
    """
    from raven.providers.litellm_provider_names import LITELLM_PROVIDER_NAMES
    from raven.providers.registry import find_by_name, normalize_provider_name

    # Every name a listed provider answers to, not just the one shown: LiteLLM
    # knows "ollama" and "vllm", which are the pre-rename spellings of two rows
    # already on the list, so matching on the displayed name alone offered them
    # a second time under a name that resolves to the same section.
    already_listed: set[str] = set()
    for entry in _CURATED_PROVIDERS:
        spec = find_by_name(entry["name"])
        already_listed |= set(spec.route_names) if spec else {normalize_provider_name(entry["name"])}
    return sorted(n for n in LITELLM_PROVIDER_NAMES if normalize_provider_name(n) not in already_listed)


def _prompt_litellm_vendor() -> Optional[str]:
    """Ask for a vendor by name, completing against the ones LiteLLM routes to.

    Returns the provider name, ``_BACK`` to rewind to the picker, or ``None`` on
    Ctrl+C. The names come from the packaged snapshot, so offering them costs no
    LiteLLM import.
    """
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.providers.registry import normalize_provider_name

    choices = _litellm_vendor_choices()

    typed = questionary.autocomplete(
        _t(
            f"Vendor name ({len(choices)} supported - type to search, Tab to complete, empty to go back):",
            f"厂商名(支持 {len(choices)} 家 — 输入可搜索,Tab 补全,留空返回):",
        ),
        choices=choices,
        style=RAVEN_STYLE,
        qmark=_QMARK,
        ignore_case=True,
        match_middle=True,
    ).ask()
    if typed is None:
        return None
    typed = typed.strip()
    if not typed:
        return _BACK
    # Validation happens where the name is used, not here: the caller runs it
    # through the same gate the --provider flag goes through, which is what turns
    # a typo into a message instead of a traceback.
    return normalize_provider_name(typed)


def _prompt_api_key(provider: str, *, allow_back: bool = False, back_label: Optional[str] = None) -> Any:
    """Ask for an API key (hidden input). Returns ``_BACK`` on empty submit
    when ``allow_back`` is set, else the key string. ``back_label`` overrides
    the empty-submit hint for callers where it cancels rather than rewinds."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    def _validate(v: str) -> Any:
        if allow_back and v == "":
            return True  # a truly-empty submit is the back/cancel signal
        return (
            True
            if len(v.strip()) >= 8
            else _t(
                "API key looks off (empty or too short) — please re-enter (≥ 8 chars).",
                "API Key 看起来不对(过短或为空),请重新输入(至少 8 位)。",
            )
        )

    key = questionary.password(
        _t("Paste your API key:", "粘贴你的 API Key:"),
        validate=_validate,
        placeholder=_back_placeholder(allow_back, back_label),
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


def _prompt_local_api_base(spec: Any, *, current: str = "", allow_back: bool = False) -> Any:
    """Ask a local deployment for its server URL. Returns ``_BACK`` on empty submit.

    A local deployment is reached by address, not by key -- there is nothing to
    authenticate against a server the user is running.

    The field is seeded with the address already configured, falling back to the
    registry default for a first-time setup. Seeding the default unconditionally
    meant reconfiguring a server at some other address offered localhost, and
    pressing Enter to move on replaced a working address with it.
    """
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    def _validate(v: str) -> Any:
        if allow_back and v.strip() == "":
            return True
        return (
            True
            if v.strip().startswith(("http://", "https://"))
            else _t("URL must start with http:// or https://", "地址需以 http:// 或 https:// 开头")
        )

    url = questionary.text(
        _t(f"{spec.label} server URL:", f"{spec.label} 服务地址:"),
        default=current or spec.default_api_base or "",
        validate=_validate,
        placeholder=_back_placeholder(allow_back),
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()
    if url is None:
        # Ctrl+C quits, like the sibling credential prompts. Returning None left
        # each caller to decide what it meant, and they did not agree.
        raise typer.Exit(1)
    url = url.strip()
    if allow_back and not url:
        return _BACK
    return url


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
    if credential_kind(provider) != CRED_OAUTH:
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
            f"  [accent]Starting OAuth login for {spec.label}…[/accent]\n",
            f"  [accent]正在为 {spec.label} 启动 OAuth 登录…[/accent]\n",
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

    # A local deployment has no key to verify -- what is being checked is that
    # the address answers, and saying "API key" there describes a field the user
    # was never asked for.
    if credential_kind(provider) == CRED_LOCAL:
        console.print(_t("  [dim]⏳ Reaching the server…[/dim]", "  [dim]⏳ 正在连接服务…[/dim]"))
    else:
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
    # hit for a GET /v1/models pre-check. That's NOT a real auth failure: skip
    # the pre-check (the test message sent later exercises real connectivity via
    # litellm) instead of dumping the user into the failure submenu.
    #
    # `no_probe_endpoint` is the probe saying exactly this. It used to say
    # `not_configured` with "api_base" in the text, which is why the old
    # condition read that way -- and a rename this caller does not follow puts
    # every one of those providers into the failure submenu on the first step
    # of onboarding.
    if status == "no_probe_endpoint" or (status == "not_configured" and "api_base" in (result.get("error") or "")):
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
    """True if ``model`` would auto-route to ``spec`` under ``provider='auto'``.

    Defers to the spec so this guard cannot disagree with the routing it guards.
    """
    return bool(model and spec and spec.claims(model))


# How a provider proves who it is. Every decision the wizard makes about a
# provider -- which field to prompt for, what a failure offers to change, what
# "remove" clears, whether a rollback applies -- follows from this one question,
# and it was being answered independently at thirteen sites off two spec flags.
# Each of the last two review rounds found a site that disagreed with the others:
# a rollback that wrote credentials to an OAuth provider and killed the wizard, a
# menu that offered a key prompt to one, a prompt that half-guarded a spec it had
# already dereferenced. Answer it once.


def _format_model_for_provider(provider: str, spec: Any, model_id: str) -> str:
    """Apply the provider's route prefix to a raw ``/v1/models`` id when needed.

    A vendor Raven carries no spec for still needs the prefix, and needs it most:
    the id it returns is bare, and a bare id is routed by keyword and fallback
    rather than to the section the user just configured. Handing one back
    unprefixed sent the request wherever those rules landed -- configuring
    Mistral alongside OpenAI produced "mistral-large-latest", which resolves to
    OpenAI and spends OpenAI's key.

    The rule itself is ``providers.wire.stored_model_id``; deciding it here as
    well is what made the wizard and the TUI write one model two ways.
    """
    return stored_model_id(provider, model_id)


def _pick_model(
    provider: str,
    spec: Any,
    *,
    current_model: Optional[str],
    model_ids: Optional[list[str]],
    probe_status: str,
    user_provided_model: Optional[str],
    non_interactive: bool,
) -> str:
    """Decide the model string to write into ``agents.defaults.model``."""
    # Every exit goes through the formatter, which is idempotent. Applying it
    # only where the candidate list is built covered only the branch that has
    # candidates -- and a vendor Raven carries no spec for reaches the others:
    # the probe cannot pre-check it, so there is no list, so the user types the
    # id. A typed id is bare, and a bare id is routed by keyword and fallback
    # rather than to the provider just configured, which is how
    # "mistral-large-latest" came to be served with OpenAI's key.
    if user_provided_model:
        return _format_model_for_provider(provider, spec, user_provided_model)

    if non_interactive:
        default_model = spec.default_model if spec else ""
        if not default_model:
            raise typer.BadParameter(f"--model is required for provider '{provider}' (no built-in default model).")
        return _format_model_for_provider(provider, spec, default_model)

    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    if current_model and spec and _model_routes_to_provider(current_model, spec):
        default_value = current_model
    else:
        default_value = (spec.default_model if spec else "") or ""

    # A live fetch is the best answer when there is one; when there is not, the
    # same chain the TUI picker offers beats an empty prompt. Eleven providers
    # carry no curated shortlist, so before this a failed fetch left the user
    # typing a model id from memory.
    if not model_ids:
        from raven.providers.common_models import common_models_for, litellm_models_for

        known = [*common_models_for(provider), *litellm_models_for(provider)]
        if known:
            # openai / anthropic / deepseek / gemini have no /models endpoint to
            # pre-check, so there is no list on a perfectly healthy run. Saying
            # "couldn't reach the provider" there contradicted the line printed
            # just above it and read as a failure to a user for whom nothing
            # had failed.
            console.print(
                _t(
                    "  [dim]This provider has no model list to fetch - offering the ones we know.[/dim]",
                    "  [dim]该服务商没有可拉取的模型列表,先列出已知的。[/dim]",
                )
                if probe_status == "skipped"
                else _t(
                    "  [dim]Couldn't reach the provider for its model list - offering the ones we know.[/dim]",
                    "  [dim]未能向服务商拉取模型列表,先列出已知的。[/dim]",
                )
            )
            model_ids = known

    if default_value and default_value == spec.default_model:
        console.print(
            _t(
                f"  [dim]Default: {default_value} — recommended balance of quality/cost for daily use.[/dim]",
                f"  [dim]默认:{default_value} — 质量/成本均衡,适合日常使用。[/dim]",
            )
        )

    if model_ids:
        choices = [_format_model_for_provider(provider, spec, mid) for mid in model_ids]
        # Dedupe: the chain above already prefixes its ids, and _format_ leaves a
        # correctly-prefixed id alone, so two sources can agree on one model.
        choices = list(dict.fromkeys(choices))
        if default_value and default_value not in choices:
            choices.insert(0, default_value)
        # A provider with no static default (codex: every id we shipped was
        # refused, so only the account knows) still needs one here -- the empty
        # submit below falls back to it, and without one Enter tore the wizard
        # down. The list is newest-first, so its head is the better default a
        # hard-coded id could not be.
        if not default_value:
            default_value = choices[0]
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
                _t(f"Default model id for {provider}:", f"{provider} 的默认模型 id:"),
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
            # Said out loud: the prompt has already echoed an empty answer, and the
            # next thing on screen is a test message being sent. Without this the
            # model it was sent with appears nowhere.
            console.print(
                _t(
                    f"  [dim]No model entered - using {default_value}.[/dim]",
                    f"  [dim]未输入模型,使用 {default_value}。[/dim]",
                )
            )
            return _format_model_for_provider(provider, spec, default_value)
        raise typer.Exit(1)
    return _format_model_for_provider(provider, spec, chosen)


def _roll_back_provider_fields(provider: str, spec: Any, *, old_key: Optional[str], old_base: Optional[str]) -> None:
    """Undo what this pass wrote, restoring the state read before it started.

    A named function so the behaviour can be driven by a test: the two shapes
    this replaced were both wrong in ways only a test that calls it can hold
    down. Keying off the previous api_key skipped a local deployment entirely,
    leaving a mistyped address where a working one had been; and asking "was it
    configured" cleared both fields for a provider that had held only an
    api_base, erasing an endpoint this pass never touched.

    OAuth providers are skipped: their credentials live in a token file, the ops
    layer refuses to write credential fields for them, and doing it anyway turned
    a failed verification into a dead wizard.
    """
    if credential_kind(provider) == CRED_OAUTH:
        return
    _write_provider_fields(provider, {"api_key": old_key or "", "api_base": old_base})


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


def _persist_default_model(model: Optional[str], provider: str) -> None:
    """Patch ``agents.defaults.model`` and the pin that overrides it.

    Both, always. ``agents.defaults.provider`` wins over whatever a model id
    names, so writing the model alone leaves the wizard's own choice routed to
    whichever provider was pinned before -- with that provider's key. The rule
    for what to pin is ``providers.pin``, the same one the picker and
    ``raven provider use`` ask.
    """
    if not model:
        return
    from raven.config.loader import load_config
    from raven.config.update import set_default_model
    from raven.providers import pin

    try:
        pinned = load_config().agents.defaults.provider or ""
    except Exception:
        pinned = ""
    set_default_model(model, provider=pin.resolve(model, provider=provider, pinned=pinned))


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


def _run_test_probe(
    provider: str,
    *,
    non_interactive: bool,
    warnings: list[str],
    allow_repick: bool = True,
    is_oauth: bool = False,
) -> str:
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
        options.append(
            (_t("Sign in again", "重新登录"), "reauth") if is_oauth else (_t("Re-enter key", "重新填 Key"), "rekey")
        )
        options += [
            (_t("Switch provider", "更换服务商"), "switch"),
            (_t("Continue anyway", "仍然继续"), "continue"),
        ]
        choice = _failure_choice(options, non_interactive=non_interactive)
        if choice == "retry":
            return _run_test_probe(
                provider,
                non_interactive=non_interactive,
                warnings=warnings,
                allow_repick=allow_repick,
                is_oauth=is_oauth,
            )
        if choice in ("repick", "rekey", "reauth", "switch"):
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

    def _rewind() -> None:
        """Discard the flag values before the next pass through the picker.

        All of them, not just the provider: they were typed for the provider
        that just failed. A stale --api-key was written to the newly picked
        provider without a prompt, a stale --base-url pointed it at the previous
        provider's machine, and picking a local deployment -- which rejects
        --api-key by design -- ended the whole wizard on a usage error, losing
        the later steps this loop exists to keep.
        """
        nonlocal flag_provider, api_key, base_url, model
        flag_provider = api_key = base_url = model = None

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
            # Same gate as the flag path: the vendor step lets the user type a
            # name, and a typo there used to reach the config layer as an
            # uncaught KeyError that tore down the wizard mid-setup.
            try:
                provider = _validate_provider_name(picked)
            except typer.BadParameter as exc:
                console.print(f"  [red]x[/red] {exc}")
                _rewind()
                continue

        spec = find_by_name(provider)
        kind = credential_kind(provider)
        is_oauth = kind == CRED_OAUTH
        is_custom = kind == CRED_ENDPOINT
        # The interactive picker already echoes the chosen provider; only print
        # an explicit confirmation when it came from --provider (no echo then).
        if flag_provider:
            console.print(
                _t(
                    f"  [dim]Provider:[/dim] [accent]{_provider_label(provider)}[/accent]",
                    f"  [dim]服务商:[/dim] [accent]{_provider_label(provider)}[/accent]",
                )
            )

        # Snapshot the stored key before _collect_credentials overwrites it, so a
        # failed re-configuration of an existing provider can be rolled back to
        # its prior working key (rather than left holding the just-typed bad one).
        # Read through the ops library: it folds in a section still stored under
        # the provider's pre-rename name, which a raw lookup by the typed name
        # misses -- and the write below consolidates onto the current name, so a
        # rollback would otherwise restore nothing over a real key.
        from raven.config.update_providers import get_provider_config

        _prev = get_provider_config(provider, redact_secrets=False)
        old_key = _prev.get("api_key")
        old_base = _prev.get("api_base")

        custom_model = _collect_credentials(
            provider,
            is_oauth=is_oauth,
            is_custom=is_custom,
            is_local=kind == CRED_LOCAL,
            api_key=api_key,
            base_url=base_url,
            model=model,
            non_interactive=non_interactive,
        )
        if custom_model is _BACK:
            # User backed out of the first credential field — rewind to the
            # provider picker (drop the flags so the picker actually shows).
            _rewind()
            continue

        chosen_model = _resolve_model_with_test(
            provider,
            spec,
            is_custom=is_custom,
            custom_model=custom_model,
            user_model_flag=model,
            non_interactive=non_interactive,
            warnings=warnings,
            skip_test=skip_test,
        )
        if chosen_model is None:
            # "Switch provider" — re-run the picker (drop the flags so the second
            # pass prompts rather than reusing the failed values), undoing what
            # this pass wrote.
            #
            # Put back exactly what was there, read before this pass wrote
            # anything. One branch, because "was it configured" is the wrong
            # question twice over: a local deployment is configured by address
            # and has no key, so a rollback keyed off the old key skipped it and
            # a mistyped address replaced a working one for good; and a provider
            # that held only an api_base counts as unconfigured, so clearing
            # both fields for a "new" provider erased an endpoint this pass had
            # never touched.
            #
            # OAuth providers are left alone: their credentials live in a token
            # file, `set_provider_fields` refuses to write credential fields for
            # them at all, and doing so turned a failed verification into a
            # RuntimeError that took the whole wizard down.
            _roll_back_provider_fields(provider, spec, old_key=old_key, old_base=old_base)
            _rewind()
            continue
        _persist_default_model(chosen_model, provider)
        return {"provider": provider, "model": chosen_model}


def _collect_credentials(
    provider: str,
    *,
    is_oauth: bool,
    is_custom: bool,
    is_local: bool = False,
    api_key: Optional[str],
    base_url: Optional[str],
    model: Optional[str],
    non_interactive: bool,
) -> Any:
    """Auth setup: OAuth browser flow or api_key write. Returns the custom
    model id when the provider is ``custom`` (locked in here), ``None`` for a
    non-custom provider, or ``_BACK`` if the user backed out of the first
    interactive credential field, or if the vendor cannot be configured by a
    bare key at all (caller should rewind to the picker either way)."""
    from raven.providers.auth import key_refusal

    refusal = key_refusal(provider)
    if refusal is not None:
        console.print(f"  [red]x[/red] {refusal}")
        if non_interactive:
            raise typer.Exit(2)
        return _BACK

    if is_oauth:
        if non_interactive:
            console.print(
                "[red]OAuth providers require an interactive browser flow.[/red]\n"
                "Run [accent]raven provider login "
                f"{provider.replace('_', '-')}[/accent] separately, then re-run "
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

    if is_local:
        # A local deployment authenticates on nothing: it is reached by address.
        # Routing it through the api_key prompt would stop the user at a
        # minimum-length check for a credential that does not exist.
        from raven.providers.registry import find_by_name

        spec = find_by_name(provider)
        if api_key:
            # Said out loud rather than dropped: a local deployment writes no
            # api_key, so silently ignoring the flag looks like it was accepted.
            raise typer.BadParameter(
                f"{provider} is a local deployment and takes no --api-key; pass --base-url instead"
            )
        if base_url and not base_url.strip().startswith(("http://", "https://")):
            # The interactive prompt validates this; the flag path did not, so a
            # scheme-less address went into the config and failed at first use.
            raise typer.BadParameter(f"--base-url must start with http:// or https:// (got {base_url!r})")
        if not base_url:
            if non_interactive:
                raise typer.BadParameter(f"--base-url is required for {provider} in non-interactive mode")
            from raven.config.update_providers import get_provider_config

            try:
                stored = get_provider_config(provider, redact_secrets=False).get("api_base") or ""
            except KeyError:
                stored = ""
            base_url = _prompt_local_api_base(spec, current=stored, allow_back=True)
            if base_url is _BACK:
                return _BACK
        _write_provider_fields(provider, {"api_base": base_url})
        return None

    if not api_key:
        from raven.providers.registry import normalize_provider_name

        # GigaChat's key is not a typical API key -- it is base64(client_id:
        # client_secret) -- and the generic prompt below gives no room to say
        # so, so the wizard would otherwise send someone looking for a plain
        # key straight into a 401.
        if normalize_provider_name(provider) == "gigachat":
            console.print(
                "  [dim]GigaChat's key is base64(client_id:client_secret) from the "
                "GigaChat API console, not a typical API key.[/dim]"
            )

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
    provider: str,
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
        ok, status, model_ids = _verify_provider(provider, skip_test=skip_test)
        if not ok:
            options = (
                [
                    (_t("Retry", "重试"), "retry"),
                    # A local deployment that cannot be reached is usually a
                    # wrong address, and this is the branch it lands in -- so
                    # retry alone left the one thing worth changing unreachable.
                    *(
                        [(_t("Re-enter server URL", "重新填服务地址"), "rebase")]
                        if credential_kind(provider) == CRED_LOCAL
                        else []
                    ),
                    (_t("Continue anyway", "仍然继续"), "continue"),
                ]
                if status == "network_error"
                else [
                    # What to offer depends on what the provider is reached by.
                    # A local deployment has no key to re-enter, so offering that
                    # left a mistyped address with no way back to the field.
                    (
                        (_t("Sign in again", "重新登录"), "reauth")
                        if credential_kind(provider) == CRED_OAUTH
                        else (_t("Re-enter server URL", "重新填服务地址"), "rebase")
                        if credential_kind(provider) == CRED_LOCAL
                        else (_t("Re-enter key", "重新填 Key"), "rekey")
                    ),
                    # Also retry, because this branch takes the failures that
                    # cannot be sorted: a credential the account refused and a
                    # refresh that could not reach the network arrive as the same
                    # thing, and only one of them is fixed by signing in again.
                    (_t("Retry", "重试"), "retry"),
                    (_t("Switch provider", "更换服务商"), "switch"),
                    (_t("Continue anyway", "仍然继续"), "continue"),
                ]
            )
            choice = _failure_choice(options, non_interactive=non_interactive)
            if choice == "retry":
                continue
            if choice == "rekey" and not non_interactive:
                _write_provider_fields(provider, {"api_key": _prompt_api_key(provider)})
                continue
            if choice == "rebase" and not non_interactive:
                from raven.config.update_providers import get_provider_config

                try:
                    stored = get_provider_config(provider, redact_secrets=False).get("api_base") or ""
                except KeyError:
                    stored = ""
                retyped = _prompt_local_api_base(spec, current=stored)
                _write_provider_fields(provider, {"api_base": retyped})
                continue
            if choice == "reauth" and not non_interactive:
                if _run_oauth_login(provider):
                    continue
                return None
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
        _persist_default_model(custom_model, provider)
        if skip_test:
            return custom_model
        while True:
            result = _run_test_probe(provider, non_interactive=non_interactive, warnings=warnings, allow_repick=False)
            if result == "switch":
                return None
            if result == "rekey":
                _write_provider_fields(provider, {"api_key": _prompt_api_key(provider)})
                continue
            return custom_model  # ok / continue

    current = _load_current_default_model()
    while True:
        chosen = _pick_model(
            provider,
            spec,
            current_model=current,
            model_ids=model_ids,
            probe_status=status,
            user_provided_model=user_model_flag,
            non_interactive=non_interactive,
        )
        _persist_default_model(chosen, provider)
        if skip_test:
            return chosen
        result = _run_test_probe(
            provider,
            non_interactive=non_interactive,
            warnings=warnings,
            is_oauth=credential_kind(provider) == CRED_OAUTH,
        )
        if result == "switch":
            return None
        if result == "rekey":
            _write_provider_fields(provider, {"api_key": _prompt_api_key(provider)})
            # Re-test the same model with the new key (picker defaults to it).
            current = chosen
            user_model_flag = None
            continue
        if result == "reauth":
            if not _run_oauth_login(provider):
                return None
            current = chosen
            user_model_flag = None
            continue
        if result == "repick":
            current = chosen
            user_model_flag = None
            continue
        return chosen  # ok / continue


def _configure_existing_provider_model(*, non_interactive: bool) -> bool:
    """Choose a model for an already-authenticated provider without re-login."""
    if non_interactive:
        return False
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.providers.registry import find_by_name

    choices = [questionary.Choice(_provider_label(name), value=name) for name in _configured_providers()]
    if not choices:
        return False
    provider = questionary.select(
        _t("Choose the provider for the default model:", "选择默认模型对应的服务商:"),
        choices=choices,
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()
    if not provider:
        raise typer.Exit(1)
    spec = find_by_name(provider)
    ok, status, model_ids = _verify_provider(provider)
    if not ok:
        return False
    chosen = _pick_model(
        provider,
        spec,
        current_model=None,
        model_ids=model_ids,
        probe_status=status,
        user_provided_model=None,
        non_interactive=False,
    )
    _persist_default_model(chosen, provider)
    result = _run_test_probe(
        provider,
        non_interactive=False,
        warnings=[],
        is_oauth=credential_kind(provider) == CRED_OAUTH,
    )
    if result == "reauth":
        return _run_oauth_login(provider)
    return result in {"ok", "continue"}


# ---------------------------------------------------------------------------
# Step 1 — multi-provider entry (existing-config branch: done / add / edit)
# ---------------------------------------------------------------------------


def _manage_existing_providers(*, non_interactive: bool) -> None:
    """Edit/remove submenu for already-configured providers (interactive only)."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.providers.registry import find_by_name

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
            target_spec = find_by_name(target)
            if credential_kind(target) == CRED_OAUTH:
                # Nothing here to update: the credential is a token file, and the
                # ops layer refuses credential writes for these -- so offering the
                # key prompt ended the wizard instead of editing anything.
                console.print(
                    _t(
                        f"  [dim]{_provider_label(target)} signs in through OAuth. "
                        f"Run: raven provider login {target.replace('_', '-')}[/dim]",
                        f"  [dim]{_provider_label(target)} 通过 OAuth 登录。"
                        f"请运行: raven provider login {target.replace('_', '-')}[/dim]",
                    )
                )
                continue
            if credential_kind(target) == CRED_LOCAL:
                # A local deployment holds no key; what there is to update is
                # where it lives. Offering the key prompt wrote a credential into
                # a provider that never reads one, and left the address alone.
                from raven.config.update_providers import get_provider_config

                try:
                    stored = get_provider_config(target, redact_secrets=False).get("api_base") or ""
                except KeyError:
                    stored = ""
                retyped = _prompt_local_api_base(target_spec, current=stored)
                _write_provider_fields(target, {"api_base": retyped})
            elif credential_kind(target) == CRED_ENDPOINT:
                # A self-hosted endpoint is a key *and* the address it is sent
                # to. Updating only the key left the one field that moves when
                # the user redeploys -- the URL -- unreachable from this menu.
                from raven.config.update_providers import get_provider_config

                try:
                    stored = get_provider_config(target, redact_secrets=False).get("api_base") or ""
                except KeyError:
                    stored = ""
                retyped_key = _prompt_api_key(target)
                retyped_url = _prompt_base_url(stored or "https://")
                _write_provider_fields(target, {"api_key": retyped_key, "api_base": retyped_url})
            else:
                _write_provider_fields(target, {"api_key": _prompt_api_key(target)})
            console.print(
                _t(
                    f"  [green]✓ Updated {_provider_label(target)}.[/green]",
                    f"  [green]✓ 已更新 {_provider_label(target)}。[/green]",
                )
            )
        elif action == "remove":
            current = _load_current_default_model()
            from raven.providers.registry import find_by_name, normalize_provider_name, split_model_id

            spec = find_by_name(target)
            if spec is not None:
                was_default_source = bool(current and _model_routes_to_provider(current, spec))
            else:
                # A vendor with no spec of ours is reached by its prefix alone, so
                # that is the whole test. Treating "no spec" as "not the source"
                # skipped the guard and left a default model pointing at a
                # provider whose key had just been removed.
                prefix, _ = split_model_id(current or "")
                was_default_source = bool(current and prefix == normalize_provider_name(target))
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
            # Clear both: a local deployment counts as configured by its
            # api_base, so clearing only the key reported it removed and left it
            # in the list, still reachable. An OAuth provider has neither field
            # to clear and refuses the write, so it is told where its credential
            # actually lives instead of ending the run.
            target_spec = find_by_name(target)
            if credential_kind(target) == CRED_OAUTH:
                console.print(
                    _t(
                        f"  [dim]{_provider_label(target)}'s credential is an OAuth token, not a config field, "
                        "so there is nothing here to remove.[/dim]",
                        f"  [dim]{_provider_label(target)} 的凭据是 OAuth token,不在配置字段里,"
                        "这里没有可移除的内容。[/dim]",
                    )
                )
                continue
            _write_provider_fields(target, {"api_key": "", "api_base": None})
            if was_default_source:
                # Clear the now-dangling default so step 1's guard forces a
                # re-pick instead of leaving a model whose provider has no key.
                from raven.config.update import set_default_model

                # The pin goes with it: left behind it would route the next model
                # the user picks to the provider whose key was just removed.
                set_default_model("", provider="auto")
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
                questionary.Choice(_t("Choose default model", "选择默认模型"), value="model"),
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
        if action == "model":
            if _configure_existing_provider_model(non_interactive=False):
                continue
            console.print(
                _t(
                    "  [yellow]Could not configure a default model. Choose a provider and try again.[/yellow]",
                    "  [yellow]无法配置默认模型,请重新选择服务商。[/yellow]",
                )
            )
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


def _warn_host_risk() -> None:
    console.print(
        _t(
            "  [yellow]⚠ Third-party messages (channels, imports) can inject instructions.[/yellow]\n"
            "  [yellow]⚠ On the host, injected commands execute with full host privileges.[/yellow]",
            "  [yellow]⚠ 第三方消息(渠道、导入内容)可能向智能体注入指令。[/yellow]\n"
            "  [yellow]⚠ 本机模式下,注入的命令将以宿主机全部权限执行。[/yellow]",
        )
    )


def _confirm_host_run(questionary: Any) -> bool:
    """Warn about host-mode risk and ask for explicit confirmation (default No)."""
    from raven.cli._styles import RAVEN_STYLE

    _warn_host_risk()
    confirmed = questionary.confirm(
        _t("Run directly on the host anyway?", "仍要在本机直接运行吗?"),
        default=False,
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()
    if confirmed is None:
        raise typer.Exit(1)
    return bool(confirmed)


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
        if _current_sandbox_backend() == "none":
            _warn_host_risk()
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

    while True:
        picked = questionary.select(
            _t("Run location:", "运行位置:"), choices=choices, style=RAVEN_STYLE, qmark=_QMARK
        ).ask()
        if picked is None:
            raise typer.Exit(1)
        if picked is _BACK:
            return _BACK
        if picked == "keep":
            return None
        if picked != "none":
            break
        if not _confirm_host_run(questionary):
            continue
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
            if not _confirm_host_run(questionary):
                continue
            _persist_sandbox_backend("none")
            console.print(
                _t(
                    "  [green]✓ Running directly on the host.[/green]",
                    "  [green]✓ 将在本机直接运行。[/green]",
                )
            )
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
                    "(re-run [/dim][accent]raven onboard[/accent][dim] to reconfigure).[/dim]",
                    "[dim]在依赖相关功能前请先修复(重新运行 [/dim][accent]raven onboard[/accent][dim] 重新配置)。[/dim]",
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
    chans = ", ".join(onboard_channels._enabled_channels()) or _t("none", "无")
    mem = (
        _t("EverOS", "EverOS")
        if onboard_everos._memory_enabled()
        else _t("[yellow]off[/yellow]", "[yellow]未启用[/yellow]")
    )
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
    table.add_column(style="accent", no_wrap=True)
    table.add_column(style="dim")
    table.add_row("raven", _t("launch the native TUI (default)", "启动原生 TUI(默认)"))
    table.add_row("raven gateway", _t("run the gateway (serve channels)", "运行网关(对接渠道)"))
    table.add_row('raven agent -m "hello, world"', _t("ask a one-shot question", "一次性提问"))
    table.add_row("raven channels list", _t("see connected chat channels", "查看已接入的渠道"))
    table.add_row("raven provider list", _t("check your provider config", "检查当前服务商配置"))
    table.add_row("raven import run", _t("import AI tool history into Raven", "将其他 AI 工具的记忆导入 Raven"))
    table.add_row("raven --help", _t("see all available commands", "查看所有可用命令"))
    console.print(
        Panel(
            table,
            title=f"[bold]{_t('Get started', '开始使用')}[/bold]",
            title_align="left",
            border_style="border",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Step 5 — cold-start import
# ---------------------------------------------------------------------------


def _cell_len(text: str) -> int:
    """Rendered width, counting a CJK glyph as two columns."""
    from rich.text import Text

    return Text(text).cell_len


def _tier_choice_label(name: str, width: int, contents: str, cost: str) -> str:
    """One menu row: what it is, what it brings, what it costs.

    Names are padded to a common width so the separators line up; questionary
    renders a plain string, so the padding has to be applied here rather than
    left to a table. Separators are kept to a single space either side and the
    cost wording terse: a row that passes 80 columns wraps mid-phrase, which
    costs far more legibility than the padding buys.
    """
    return f"{name}{' ' * (width - _cell_len(name))} · {contents} · {cost}"


def _step6_import(*, skip: bool, non_interactive: bool) -> object:
    """Step 5 — optionally import conversation history from other AI tools."""
    _step_header(6, _t("Import history from other AI tools", "从其他 AI 工具导入历史"))

    if skip:
        console.print(
            _t(
                "  [dim]Skipped via --skip-import.[/dim]",
                "  [dim]已通过 --skip-import 跳过。[/dim]",
            )
        )
        return None

    if non_interactive:
        console.print(
            _t(
                "  [dim]Skipped (non-interactive).[/dim]",
                "  [dim]已跳过（非交互）。[/dim]",
            )
        )
        return None

    if not onboard_everos._memory_enabled():
        console.print(
            _t(
                "  [dim]Skipped — EverOS long-term memory is required for history import.[/dim]",
                "  [dim]已跳过——历史导入需要先启用 EverOS 长期记忆。[/dim]",
            )
        )
        return None

    import sys

    from raven.cli._log_file import redirect_loguru_to_file
    from raven.cli._styles import RAVEN_STYLE

    questionary = _require_questionary()
    action = questionary.select(
        _t(
            "Would you like to import conversation history from other AI tools? (Claude Code, Codex, etc.)",
            "是否要从其他 AI 工具（Claude Code、Codex 等）导入对话历史？",
        ),
        choices=[
            questionary.Choice(_t("Yes", "是"), value="yes"),
            questionary.Choice(_t("No", "否"), value="no"),
        ],
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()
    if action is None:
        raise typer.Exit(1)
    if action == "no":
        console.print(_t("  [dim]Skipped.[/dim]", "  [dim]已跳过。[/dim]"))
        return None

    # Set up file logging for the entire import lifecycle.
    # Wrapped in try/finally so logging is restored on any exit path.
    from loguru import logger as _restore_logger

    log_path = redirect_loguru_to_file("import.log", terminal_level=None)
    _restore_logger.enable("raven")
    try:
        return _step5_import_body(
            questionary=questionary,
            log_path=log_path,
        )
    finally:
        _restore_logger.remove()
        _restore_logger.add(sys.stderr, level="WARNING")
        _restore_logger.disable("raven")


def _step5_import_body(
    *,
    questionary: Any,
    log_path: Any,
) -> object:
    """Inner body of step 5, runs with file logging active."""
    import asyncio

    from raven.cli._styles import RAVEN_STYLE
    from raven.cli.import_commands import (
        PLATFORM_DISPLAY_NAMES,
        ImportRunResult,
        _build_and_run,
        _default_state,
        _importable_skill_count,
        _install_skills_without_a_scan,
        _make_phase_reporter,
        _print_summary,
        _report_scan_error,
    )
    from raven.importer.orchestrator import ProgressEvent
    from raven.importer.scanners import build_scanners, scan_all
    from raven.importer.types import Platform, Scanner, ScanResult, SourceKind, Tier, filter_by_tier

    # on_error is not optional here: scan_all isolates a failing scanner rather
    # than propagating, and loguru is file-only during onboarding, so without it
    # a platform that failed to scan is indistinguishable from one with no data.
    all_results = asyncio.run(scan_all(on_error=_report_scan_error))
    if not all_results:
        # Skills are directories rather than message sources, so they never
        # arrive as ScanResults: an install whose only importable data is skills
        # lands here, and stopping at the message above would tell that user
        # there is nothing to import while a dozen skills sit on disk.
        # Not assume_yes: the wizard's own "Start?" gate sits further down, past
        # the prompts this return skips, so nothing else asks before the copy.
        if not asyncio.run(_install_skills_without_a_scan(None, assume_yes=False)):
            console.print(_t("  No importable data found.", "  未找到可导入的数据。"))
        return None

    # Discovery walks the whole Hermes skill tree, and every prompt below can be
    # returned to, so it is counted once here rather than inside the loop.
    hermes_skill_count = asyncio.run(_importable_skill_count(Platform.HERMES))

    # Platform selection (sync questionary)

    def _platform_label(items: list[ScanResult], name: str) -> str:
        m = sum(1 for r in items if r.kind == SourceKind.MEMORY_FILE)
        c = sum(1 for r in items if r.kind == SourceKind.CONVERSATION)
        if m and c:
            return _t(
                f"{name} ({m} memory files, {c} conversations)",
                f"{name}（{m} 个记忆文件，{c} 个对话）",
            )
        if m:
            return _t(f"{name} ({m} memory files)", f"{name}（{m} 个记忆文件）")
        return _t(f"{name} ({c} conversations)", f"{name}（{c} 个对话）")

    by_platform: dict[str, list[ScanResult]] = {}
    for r in all_results:
        by_platform.setdefault(r.platform.value, []).append(r)

    back_value = "back"
    skip_value = "skip"

    # Nested loops, one per prompt, so Back is `break` -- it lands on the prompt
    # immediately above and keeps every choice made before it. A single flat
    # loop would send Back from any depth to the platform prompt, silently
    # discarding selections the user never asked to change.
    while True:  # platform level
        # -- Platform selection --
        # Scannable platforms first, then everything that cannot be picked. In
        # enum order the two kinds interleave, which buried the one real choice
        # among placeholders.
        platform_choices = [
            questionary.Choice(
                _platform_label(by_platform[p.value], PLATFORM_DISPLAY_NAMES.get(p.value, p.value)),
                value=p.value,
            )
            for p in Platform
            if p.value in by_platform
        ]
        platform_choices.append(
            questionary.Choice(
                _platform_label(all_results, _t("All platforms", "全部平台")),
                value="all",
            )
        )
        # ``disabled`` both greys the row (RAVEN_STYLE's `disabled` class) and
        # makes the arrow keys skip it, so an unsupported platform can no longer
        # be picked only to be told it is unsupported. Passed as ``True`` rather
        # than a reason string because questionary appends a reason in its own
        # hardcoded " (...)" -- ASCII parens, and the label is already written
        # with the full-width pair the rest of the Chinese copy uses.
        platform_choices.extend(
            questionary.Choice(
                _t(
                    f"{PLATFORM_DISPLAY_NAMES.get(p.value, p.value)} (coming soon)",
                    f"{PLATFORM_DISPLAY_NAMES.get(p.value, p.value)}（即将支持）",
                ),
                value=f"coming:{p.value}",
                disabled=True,
            )
            for p in Platform
            if p.value not in by_platform
        )
        # The top level has no prompt above it, so its exit leaves the step
        # entirely. Without it the only way out is Esc, which aborts the whole
        # onboarding; answering "no" to the offer above skips cleanly, and
        # changing your mind one prompt later should too.
        platform_choices.append(
            questionary.Choice(
                _t("Skip import", "跳过导入"),
                value=skip_value,
            )
        )
        selected_platform = questionary.select(
            _t("Select platform:", "选择平台："),
            choices=platform_choices,
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if selected_platform is None:
            raise typer.Exit(1)
        if selected_platform == skip_value:
            console.print(_t("  [dim]Skipped.[/dim]", "  [dim]已跳过。[/dim]"))
            return None

        if selected_platform == "all":
            results = all_results
        else:
            results = [r for r in all_results if r.platform.value == selected_platform]

        mem = sum(1 for r in results if r.kind == SourceKind.MEMORY_FILE)
        conv = sum(1 for r in results if r.kind == SourceKind.CONVERSATION)
        # Skills never travel as ScanResults, so every count derived from
        # `results` omits them. Left out, the wizard offers "2 items" and then
        # installs a dozen skills the user was never told about.
        skills = hermes_skill_count if selected_platform in ("all", Platform.HERMES.value) else 0
        console.print(
            _t(
                f"  {len(results) + skills} items selected "
                f"({mem} memory files, {skills} skills, {conv} conversations).",
                f"  已选 {len(results) + skills} 项（{mem} 个记忆文件，{skills} 个技能，{conv} 个对话）。",
            )
            if skills
            else _t(
                f"  {len(results)} items selected ({mem} memory files, {conv} conversations).",
                f"  已选 {len(results)} 项（{mem} 个记忆文件，{conv} 个对话）。",
            )
        )

        while True:  # tier level -- Back here returns to the platform prompt
            # -- Tier selection --
            # One line naming the three kinds of data, then each option carrying
            # its own contents and cost. The previous shape put two prose
            # paragraphs above the menu, so the reader had to match each
            # sentence back to an option by name, and both paragraphs wrapped
            # mid-word at 80 columns.
            console.print()
            console.print(
                _t(
                    "  [dim]Memory files are preferences and project knowledge; skills are copied "
                    "into the local skill pool; conversations are full chat history.[/dim]",
                    "  [dim]记忆文件是偏好与项目知识，技能会复制进本地技能池，对话是完整聊天历史。[/dim]",
                ),
                highlight=False,
            )
            console.print()
            file_label = _t("Memory files only", "仅记忆文件")
            full_label = _t("Full import", "完整导入")
            label_width = max(_cell_len(file_label), _cell_len(full_label))
            file_contents = (
                _t(f"{mem} memory files + {skills} skills", f"{mem} 个记忆文件 + {skills} 个技能")
                if skills
                else _t(f"{mem} memory files", f"{mem} 个记忆文件")
            )
            tier_choices = []
            if mem:
                tier_choices.append(
                    questionary.Choice(
                        _tier_choice_label(
                            file_label,
                            label_width,
                            file_contents,
                            _t("minutes, low LLM cost", "分钟级，LLM 开销小"),
                        ),
                        value=Tier.MEMORY_FILES,
                    )
                )
            tier_choices.append(
                questionary.Choice(
                    _tier_choice_label(
                        full_label,
                        label_width,
                        _t(f"the above + {conv} conversations", f"以上全部 + {conv} 个对话"),
                        _t("hours, high LLM cost", "数小时，LLM 开销大"),
                    ),
                    value=Tier.FULL,
                )
            )
            tier_choices.append(
                questionary.Choice(
                    _t("Back", "返回"),
                    value=back_value,
                )
            )
            selected_tier = questionary.select(
                _t("Select import tier:", "选择导入档位："),
                choices=tier_choices,
                style=RAVEN_STYLE,
                qmark=_QMARK,
            ).ask()
            if selected_tier is None:
                raise typer.Exit(1)
            if selected_tier == back_value:
                _step_header(6, _t("Import history from other AI tools", "从其他 AI 工具导入历史"))
                break

            # -- Filter --
            filtered = filter_by_tier(results, selected_tier)
            if not filtered:
                # The tier filter has nothing of the skills' to keep, so a
                # skills-only install reaches this return with its skills still
                # uninstalled unless they are handled here.
                scope = None if selected_platform == "all" else Platform(selected_platform)
                if not asyncio.run(_install_skills_without_a_scan(scope, assume_yes=False)):
                    console.print(_t("  No items match the selected tier.", "  所选档位无匹配项。"))
                return None

            f_mem = sum(1 for r in filtered if r.kind == SourceKind.MEMORY_FILE)
            f_conv = sum(1 for r in filtered if r.kind == SourceKind.CONVERSATION)

            # -- Execution mode --
            exec_mode = questionary.select(
                _t("Select execution mode:", "选择执行方式："),
                choices=[
                    questionary.Choice(
                        _t("Run now (wait for completion, show progress)", "立即执行（等待完成，显示进度）"),
                        value="foreground",
                    ),
                    questionary.Choice(
                        _t(
                            "Run in background (use raven import status to check progress)",
                            "后台执行（用 raven import status 查看进度）",
                        ),
                        value="background",
                    ),
                    questionary.Choice(
                        _t("Back", "返回"),
                        value=back_value,
                    ),
                ],
                style=RAVEN_STYLE,
                qmark=_QMARK,
            ).ask()
            if exec_mode is None:
                raise typer.Exit(1)
            if exec_mode == back_value:
                continue

            break
        # The tier level exits either by Back, which means try the platform
        # prompt again, or by completing, which means the whole step is done.
        if selected_tier != back_value:
            break

    # Summary + confirm
    platform_display = (
        PLATFORM_DISPLAY_NAMES.get(selected_platform, selected_platform)
        if selected_platform != "all"
        else _t("All platforms", "全部平台")
    )
    tier_display = (
        _t("Memory files only", "仅记忆文件") if selected_tier == Tier.MEMORY_FILES else _t("Full import", "完整导入")
    )
    mode_display = _t("Run now", "立即执行") if exec_mode == "foreground" else _t("Background", "后台执行")
    console.print(
        _t(
            f"\n  About to import:\n"
            f"    Platform: {platform_display}\n"
            f"    Tier:     {tier_display}\n"
            f"    Items:    {len(filtered) + skills} ({f_mem} memory files, "
            f"{skills} skills, {f_conv} conversations)\n"
            f"    Mode:     {mode_display}",
            f"\n  即将导入:\n"
            f"    平台:     {platform_display}\n"
            f"    档位:     {tier_display}\n"
            f"    数量:     {len(filtered) + skills} 项"
            f"（{f_mem} 个记忆文件，{skills} 个技能，{f_conv} 个对话）\n"
            f"    执行方式: {mode_display}",
        )
    )
    if not typer.confirm(
        _t("  Start?", "  开始执行？"),
        default=True,
    ):
        return None

    # Build items
    scanners = build_scanners()
    scanner_map: dict[str, Scanner] = {s.platform: s for s in scanners}
    items = [(scanner_map[r.platform], r) for r in filtered if r.platform in scanner_map]

    state = _default_state()
    state.set_total(len(items))

    if exec_mode == "background":
        import shutil
        import subprocess as _sp

        raven_bin = shutil.which("raven")
        platform_flag = selected_platform if selected_platform != "all" else None
        if not raven_bin:
            console.print(
                _t(
                    "  [red]Cannot find 'raven' command. Falling back to foreground execution.[/red]",
                    "  [red]找不到 'raven' 命令。回退到前台执行。[/red]",
                )
            )
            exec_mode = "foreground"
        elif platform_flag is None and len(by_platform) > 1:
            # `import run` has no way to say "every platform": with no --platform
            # and more than one platform holding data, the child reaches the
            # platform picker. A detached process with DEVNULL on both streams has
            # no terminal to ask on, so it would hang unseen after this step had
            # already reported the import as started.
            console.print(
                _t(
                    "  [yellow]An all-platforms import cannot run in the background yet;\n"
                    "  running it in the foreground instead.[/yellow]",
                    "  [yellow]全平台导入暂不支持后台执行，改为前台运行。[/yellow]",
                )
            )
            exec_mode = "foreground"
        else:
            cmd = [raven_bin, "import", "run", "--tier", selected_tier.value, "--yes"]
            if platform_flag:
                cmd.extend(["--platform", platform_flag])
            _sp.Popen(
                cmd,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
                start_new_session=True,
            )
            console.print(
                _t(
                    f"\n  Import started in background.\n"
                    f"  Check progress: [accent]raven import status[/accent]\n"
                    f"  Log: {log_path}",
                    f"\n  导入已在后台启动。\n  查看进度: [accent]raven import status[/accent]\n  详细日志: {log_path}",
                )
            )
            return None

    # Foreground execution (async, with Rich progress)
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

    async def _do_import() -> ImportRunResult:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task(
                _t("Importing...", "导入中..."),
                total=len(items),
            )

            def on_progress(event: ProgressEvent) -> None:
                progress.update(
                    task_id,
                    advance=1,
                    description=f"[{event.current}/{event.total}] {event.platform}/{event.source_key}",
                )

            return await _build_and_run(
                items,
                state,
                on_progress=on_progress,
                on_phase=_make_phase_reporter(progress),
            )

    _print_summary(asyncio.run(_do_import()), log_path=log_path)
    return None


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
    skip_import: bool = False,
    non_interactive: bool = False,
    yes: bool = False,
    reset: bool = False,
    skip_test: bool = False,
) -> None:
    """Run the 6-step onboarding wizard end-to-end.

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
            skip_import=skip_import,
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
    skip_import: bool = False,
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
                "[bold][accent]✨ Welcome to the Raven setup wizard[/accent][/bold]\n\n"
                "[dim]We'll configure, in order:[/dim]\n"
                "  [accent]①[/accent] LLM      [accent]②[/accent] Run location      "
                "[accent]③[/accent] Chat channel      [accent]④[/accent] Long-term memory      "
                "[accent]⑤[/accent] Deep research      [accent]⑥[/accent] Import history\n\n"
                "[dim]↑↓ select · Enter confirm · Ctrl+C quit anytime — anything already written is kept.[/dim]",
                "[bold][accent]✨ 欢迎使用 Raven 配置向导[/accent][/bold]\n\n"
                "[dim]我们将依次配置:[/dim]\n"
                "  [accent]①[/accent] LLM      [accent]②[/accent] 运行位置      "
                "[accent]③[/accent] 聊天渠道      [accent]④[/accent] 长期记忆      "
                "[accent]⑤[/accent] 深度研究      [accent]⑥[/accent] 历史导入\n\n"
                "[dim]↑↓ 选择 · Enter 确认 · 随时 Ctrl+C 退出 — 已写入的配置会保留。[/dim]",
            ),
            border_style="border",
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
        lambda: onboard_channels._step3_channel(channel=channel, skip=skip_channel, non_interactive=non_interactive),
        lambda: onboard_everos._step4_memory(
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
        lambda: _step6_import(skip=skip_import, non_interactive=non_interactive),
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


def ensure_ready_to_start(*, non_interactive: bool = False) -> None:
    """Run the wizard for a config that cannot start, and only for that.

    Two different things fail the startup check. A config with no usable provider
    at all is a first run, and the wizard is the answer -- it configures five more
    subsystems besides this one. A config whose default model happens to name a
    provider that has gone unusable is not: the wizard restarts at the language
    screen to fix one line, over a session that has other providers ready. Say
    which line, and let the user fix it where models are chosen.

    The distinction lives here rather than at each entry point, because both
    entries were asking the same question and only one answer can be right.
    """
    if _is_config_populated():
        return

    if not _configured_providers():
        run_wizard(non_interactive=non_interactive)
        return

    model = (_load_raw_config().get("agents", {}) or {}).get("defaults", {}).get("model")
    # Says what was found, not why: the provider it resolves to may have no
    # credentials, or the id may resolve to a provider that never served it (a
    # deployment name carrying another vendor's keyword does that). Naming a cause
    # we have not established sends the user to fix the wrong thing.
    console.print(
        _t(
            f"  [yellow]No usable provider resolves the default model ({model}).[/yellow]",
            f"  [yellow]默认模型({model})解析不到可用的服务商。[/yellow]",
        )
    )
    console.print(
        _t(
            "  [dim]Choose one that works: `raven tui` then /model. Or `raven onboard` to set this up again.[/dim]",
            "  [dim]换一个能用的:`raven tui` 后按 /model。或用 `raven onboard` 重新配置。[/dim]",
        )
    )


# ---------------------------------------------------------------------------
# Typer entry point
# ---------------------------------------------------------------------------


def register(app: typer.Typer) -> None:
    """Attach the ``onboard`` command to ``app``."""

    @app.command()
    def onboard(
        provider: Optional[str] = typer.Option(None, "--provider", help="LLM provider name (skips Step 1's prompt)"),
        api_key: Optional[str] = typer.Option(None, "--api-key", help="API key for the chosen provider"),
        base_url: Optional[str] = typer.Option(
            None,
            "--base-url",
            help="Server URL: required for a local deployment (ollama_chat / hosted_vllm), or a custom OpenAI-compatible endpoint",
        ),
        model: Optional[str] = typer.Option(None, "--model", help="Default model id (e.g. 'openai/gpt-4o-mini')"),
        channel: Optional[str] = typer.Option(None, "--channel", help="Channel to enable in Step 3"),
        skip_sandbox: bool = typer.Option(False, "--skip-sandbox", help="Skip Step 2 (run location)"),
        skip_channel: bool = typer.Option(False, "--skip-channel", help="Skip Step 3 (channel setup)"),
        skip_memory: bool = typer.Option(False, "--skip-memory", help="Skip Step 4 (long-term memory)"),
        skip_deep_research: bool = typer.Option(False, "--skip-deep-research", help="Skip Step 5 (deep_research tool)"),
        skip_import: bool = typer.Option(False, "--skip-import", help="Skip Step 6 (history import)"),
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
        """Six-step setup wizard: LLM provider → sandbox → channel → memory → deep research → import."""
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
            skip_import=skip_import,
            non_interactive=non_interactive,
            yes=yes,
            reset=reset,
            skip_test=skip_test,
        )


__all__ = ["register", "run_wizard", "ensure_ready_to_start"]
