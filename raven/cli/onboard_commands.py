"""Seven-step onboarding wizard: provider → sandbox → channel → memory → web → sub-agents → import.

Goal: get a new user from ``pip install`` to a working agent in a few
minutes, without ever opening ``~/.raven/config.json`` or
``~/.everos/raven/everos.toml``.

Steps:
  0. Welcome
  1. LLM provider (required; multi-provider, in-step connectivity + test probe)
  2. Sandbox / run location (optional, single-select)
  3. Chat channel (optional, stackable)
  4. EverOS long-term memory (optional; llm/embedding required once enabled,
     rerank/multimodal optional)
  5. Web access (optional; pick a search vendor and a page reader and give
     each its key; keys are mirrored to ~/.raven/env so sub-agents inherit them)
  6. Sub-agents shipped in this checkout (optional; per folder, own key or
     this raven's LLM)
  7. Cold-start import from other AI tools (optional)
  8. Done

All writes go through the ``update_providers`` / ``update_channels`` /
``update`` / ``update_everos`` ops libraries — this module owns the UX layer,
not config-schema knowledge.

Navigation: questionary 2.1.1 has no first-class cross-screen "back", so the
wizard is a screen state machine and back is expressed as a ``0) back``
sentinel choice on the screens that support it (Step 1 <-> language pick,
Step 2 -> Step 1); Steps 3 to 7 are optional and forward-only (re-run
``onboard`` to change them). Ctrl+C exits at any point, keeping whatever was
already written.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Optional

import typer
from rich.panel import Panel

from raven import i18n
from raven.cli import onboard_channels, onboard_everos, onboard_web
from raven.cli._helpers import print_probe_troubleshooting
from raven.cli._onboard_shared import (  # noqa: F401  (re-exports: tests and
    # sibling wizards address these through this module's namespace)
    _ABORT_EVEROS,
    _BACK,
    _POINTER,
    _QMARK,
    _TOTAL_STEPS,
    _back_placeholder,
    _failure_choice,
    _field_placeholder,
    _load_raw_config,
    _prompt_api_key,
    _require_questionary,
    _step_header,
    console,
)
from raven.core.provider_stack import DEFAULT_PROBE_MESSAGE, send_probe
from raven.i18n import t
from raven.providers.registry import (
    SHAPE_ENDPOINT,
    SHAPE_LOCAL,
    SHAPE_OAUTH,
    auth_shape,
)
from raven.providers.wire import stored_model_id

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
            },
            {"name": "openai", "label": "OpenAI"},
            {"name": "anthropic", "label": "Anthropic"},
            {"name": "gemini", "label": "Gemini"},
            # Marked because EverMind and MiniMax collaborate in the open, not
            # because it ranks differently on capability -- "open-source" rather
            # than a bare "partner", which in a list of vendors reads as paid
            # placement. The OAuth MiniMax entries carry no marker: the same
            # vendor is already marked here, and stacking it on "(OAuth)" makes
            # the row twice as long for nothing.
            {
                "name": "minimax",
                "label": "MiniMax (Global, open-source partner)",
            },
            {"name": "minimax_cn_api", "label": "MiniMax (CN)"},
            {"name": "deepseek", "label": "DeepSeek"},
            {"name": "zai", "label": "Z.ai (Zhipu)"},
            {"name": "dashscope", "label": "Alibaba Cloud"},
            {"name": "moonshot", "label": "Moonshot"},
            {"name": "nvidia_nim", "label": "NVIDIA"},
            {"name": "volcengine", "label": "VolcEngine"},
            {"name": "siliconflow", "label": "SiliconFlow"},
            {"name": "groq", "label": "Groq"},
            {"name": "aihubmix", "label": "AiHubMix"},
            {"name": "azure_openai", "label": "Azure OpenAI"},
        ],
    },
    {
        "kind": "oauth",
        "providers": [
            {
                "name": "github_copilot",
                "label": "GitHub Copilot (OAuth)",
            },
            {"name": "openai_codex", "label": "OpenAI Codex (OAuth)"},
            {
                "name": "minimax_global",
                "label": "MiniMax Global (OAuth)",
            },
            {"name": "minimax_cn", "label": "MiniMax CN (OAuth)"},
        ],
    },
    {
        "kind": "local",
        "providers": [
            {"name": "lm_studio", "label": "LM Studio (local)"},
            {"name": "ollama_chat", "label": "Ollama (local)"},
            {"name": "hosted_vllm", "label": "vLLM / self-hosted"},
        ],
    },
    {
        "kind": "fallback",
        "providers": [
            {
                "name": _PICK_LITELLM_VENDOR,
                "label": "Another supported vendor (type to search)",
            },
            {
                "name": "custom",
                "label": "Self-hosted OpenAI-compatible endpoint",
            },
        ],
    },
]

# Flat view for callers that only need "which providers does the wizard offer".
_CURATED_PROVIDERS: list[dict[str, Any]] = [
    entry for group in _CURATED_GROUPS for entry in group["providers"] if entry["name"] != _PICK_LITELLM_VENDOR
]


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
    """First screen: choose the wizard's language. Updates the UI language.

    Persistence happens later (after bootstrap created the config file), via
    ``set_language`` in :func:`_run_wizard_body`.
    """
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
        default=i18n.current_language(),  # preselect the saved language on a re-run
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()
    if picked is None:
        raise typer.Exit(1)
    i18n.set_language(picked)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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
    from raven.utils.workspace import sync_workspace_templates

    path = get_config_path()
    if not path.exists():
        # Creates the file; save_config dumps with exclude_defaults, so a
        # fresh config is sparse -- it records what the user set, nothing else.
        save_config(load_config())
    workspace = get_workspace_path()
    workspace.mkdir(parents=True, exist_ok=True)
    sync_workspace_templates(workspace, notify=lambda m: console.print(f"  [dim]{m}[/dim]"))


# ---------------------------------------------------------------------------
# Step 1 — provider primitives (reused verbatim from the 3-step wizard)
# ---------------------------------------------------------------------------


def _provider_label(name: str) -> str:
    """Display label for a provider, falling back to the registry's display_name."""
    for entry in _CURATED_PROVIDERS:
        if entry["name"] == name:
            return t(entry["label"])
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
                    t(entry["label"]),
                    value=entry["name"],
                )
            )
    choices.append(questionary.Separator())
    choices.append(questionary.Choice(t("Back"), value=_BACK))

    return questionary.select(
        t("Provider:"),
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
        t("Vendor name ({a0} supported - type to search, Tab to complete, empty to go back):", a0=len(choices)),
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
        return True if v.strip().startswith(("http://", "https://")) else t("URL must start with http:// or https://")

    url = questionary.text(
        t("{a0} server URL:", a0=spec.label),
        default=current or spec.display_api_base or "",
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
        return True if v.startswith(("http://", "https://")) else t("URL must start with http:// or https://")

    url = questionary.text(
        t("Base URL (must include /v1):"),
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
        return True if v.strip() else t("Model id is required for custom endpoints.")

    model = questionary.text(
        t("Default model id (e.g. 'gpt-3.5-turbo' or 'qwen-max'):"),
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
    if auth_shape(provider) != SHAPE_OAUTH:
        console.print(t("  [red]✗ {provider} is not an OAuth provider.[/red]", provider=provider))
        raise typer.Exit(1)
    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(t("  [red]✗ No login handler registered for {provider}.[/red]", provider=provider))
        raise typer.Exit(1)
    console.print(t("  [accent]Starting OAuth login for {a0}…[/accent]\n", a0=spec.label))
    console.print(
        t(
            "  [dim]A browser window / link will open — finish the sign-in there, "
            "then come back here. This waits until you're done.[/dim]\n"
        )
    )
    try:
        handler()
    except typer.Exit as exc:
        # Handlers signal a failed login with Exit(1); Exit(0) (if any) is success.
        if exc.exit_code:
            return False
    except Exception as exc:  # network / browser / token errors — recoverable
        console.print(t("  [yellow]✗ Login didn't complete: {exc}[/yellow]", exc=exc))
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
    if auth_shape(provider) == SHAPE_LOCAL:
        console.print(t("  [dim]⏳ Reaching the server…[/dim]"))
    else:
        console.print(t("  [dim]⏳ Verifying your API key…[/dim]"))
    result = probe(provider)
    if result["ok"]:
        models = result.get("models_count")
        suffix = t(" ({models} models available)", models=models) if models else ""
        console.print(t("  [green]✓ Connected!{suffix}[/green]", suffix=suffix))
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
                t(
                    "  [dim]Skipping the model-list pre-check (this provider has no public /models endpoint); connectivity is not tested (--skip-test).[/dim]"
                )
            )
        else:
            console.print(
                t(
                    "  [dim]Skipping the model-list pre-check (this provider has no public /models endpoint); the test message below will confirm connectivity.[/dim]"
                )
            )
        return True, "skipped", None
    hint_map = {
        "invalid_key": t("Auth failed: the API key is invalid — check for typos / stray spaces."),
        "no_credits": t("Account out of credits or not provisioned — top up and retry."),
        "rate_limited": t("Rate limited — wait a bit and retry, or switch provider."),
        "network_error": t("Network error reaching the provider — check network / proxy / VPN."),
        "oauth_token_missing": t("Run: raven provider login {a0}", a0=provider.replace("_", "-")),
    }
    msg = hint_map.get(status, t("Verification failed: {status}", status=status))
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
# Sites did disagree in practice:
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


def _uses_manual_endpoint_flow(provider: str, spec: Any) -> bool:
    """Return whether setup must collect an endpoint instead of using a shipped one."""
    return provider == "custom" or (
        auth_shape(provider) == SHAPE_ENDPOINT and not (spec and spec.usable_default_api_base)
    )


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
                t("  [dim]This provider has no model list to fetch - offering the ones we know.[/dim]")
                if probe_status == "skipped"
                else t("  [dim]Couldn't reach the provider for its model list - offering the ones we know.[/dim]")
            )
            model_ids = known

    if default_value and default_value == spec.default_model:
        console.print(
            t(
                "  [dim]Default: {default_value} — recommended balance of quality/cost for daily use.[/dim]",
                default_value=default_value,
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
        prompt_label = t("Default model ({a0} available — type to filter, Tab to complete):", a0=len(choices))
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
        console.print(t("  [dim]Couldn't fetch the model list — enter the model id by hand.[/dim]"))
        if default_value:
            chosen = questionary.text(
                t("Default model (press Enter for [{default_value}]):", default_value=default_value),
                default=default_value,
                style=RAVEN_STYLE,
                qmark=_QMARK,
            ).ask()
        else:
            chosen = questionary.text(
                t("Default model id for {provider}:", provider=provider),
                validate=lambda v: True if v.strip() else t("Model id is required."),
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
            console.print(t("  [dim]No model entered - using {default_value}.[/dim]", default_value=default_value))
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
    if auth_shape(provider) == SHAPE_OAUTH:
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
        console.print(t("  [red]✗ Validation failed:[/red]\n{exc}", exc=exc))
        raise typer.Exit(1)


def _persist_default_model(model: Optional[str], provider: str) -> None:
    """Patch ``agents.defaults.model`` and the provider that serves it.

    Both, always. ``agents.defaults.provider`` decides whose credential a model
    id is sent on, so writing the model alone leaves the wizard's own choice
    routed to whoever was named before -- with that vendor's key. The wizard
    asks for the provider before the model, so it always has one; nothing is
    derived here, which is the same rule ``/model`` and ``raven provider use``
    enforce.
    """
    if not model:
        return
    from raven.config.update import set_default_model

    set_default_model(model, provider=provider)


# ---------------------------------------------------------------------------
# Step 1 — connectivity-failure submenu + test probe
# ---------------------------------------------------------------------------


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
        t('  [dim]Sending test message: "{DEFAULT_PROBE_MESSAGE}"[/dim]', DEFAULT_PROBE_MESSAGE=DEFAULT_PROBE_MESSAGE)
    )
    try:
        text, tokens, elapsed = send_probe()
    except Exception as exc:
        console.print(t("  [red]✗ Test failed:[/red] {exc}", exc=exc))
        console.print(
            t("  [dim]Run 'raven provider test' to re-check, or confirm the model is served by this provider.[/dim]")
        )
        print_probe_troubleshooting(provider)
        options = [(t("Retry"), "retry")]
        if allow_repick:
            options.append((t("Re-pick model"), "repick"))
        options.append((t("Sign in again"), "reauth") if is_oauth else (t("Re-enter key"), "rekey"))
        options += [
            (t("Switch provider"), "switch"),
            (t("Continue anyway"), "continue"),
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
        kind = auth_shape(provider)
        is_oauth = kind == SHAPE_OAUTH
        is_custom = _uses_manual_endpoint_flow(provider, spec)
        # The interactive picker already echoes the chosen provider; only print
        # an explicit confirmation when it came from --provider (no echo then).
        if flag_provider:
            console.print(t("  [dim]Provider:[/dim] [accent]{a0}[/accent]", a0=_provider_label(provider)))

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
            is_local=kind == SHAPE_LOCAL,
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
                    (t("Retry"), "retry"),
                    (t("Back (pick another provider)"), "back"),
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
                    (t("Retry"), "retry"),
                    # A local deployment that cannot be reached is usually a
                    # wrong address, and this is the branch it lands in -- so
                    # retry alone left the one thing worth changing unreachable.
                    *([(t("Re-enter server URL"), "rebase")] if auth_shape(provider) == SHAPE_LOCAL else []),
                    (t("Continue anyway"), "continue"),
                ]
                if status == "network_error"
                else [
                    # What to offer depends on what the provider is reached by.
                    # A local deployment has no key to re-enter, so offering that
                    # left a mistyped address with no way back to the field.
                    (
                        (t("Sign in again"), "reauth")
                        if auth_shape(provider) == SHAPE_OAUTH
                        else (t("Re-enter server URL"), "rebase")
                        if auth_shape(provider) == SHAPE_LOCAL
                        else (t("Re-enter key"), "rekey")
                    ),
                    # Also retry, because this branch takes the failures that
                    # cannot be sorted: a credential the account refused and a
                    # refresh that could not reach the network arrive as the same
                    # thing, and only one of them is fixed by signing in again.
                    (t("Retry"), "retry"),
                    (t("Switch provider"), "switch"),
                    (t("Continue anyway"), "continue"),
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
            is_oauth=auth_shape(provider) == SHAPE_OAUTH,
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
        t("Choose the provider for the default model:"),
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
        is_oauth=auth_shape(provider) == SHAPE_OAUTH,
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
        choices.append(questionary.Choice(t("Back"), value=_BACK))
        target = questionary.select(
            t("Pick a provider to manage:"),
            choices=choices,
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if target is None or target is _BACK:
            return

        action = questionary.select(
            t("What would you like to do with {a0}?", a0=_provider_label(target)),
            choices=[
                questionary.Choice(t("Update API key"), value="update"),
                questionary.Choice(
                    t("Remove (clear this provider's key)"),
                    value="remove",
                ),
                questionary.Choice(t("Back"), value=_BACK),
            ],
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if action is None or action is _BACK:
            continue
        if action == "update":
            target_spec = find_by_name(target)
            if auth_shape(target) == SHAPE_OAUTH:
                # Nothing here to update: the credential is a token file, and the
                # ops layer refuses credential writes for these -- so offering the
                # key prompt ended the wizard instead of editing anything.
                console.print(
                    t(
                        "  [dim]{a0} signs in through OAuth. Run: raven provider login {a1}[/dim]",
                        a0=_provider_label(target),
                        a1=target.replace("_", "-"),
                    )
                )
                continue
            if auth_shape(target) == SHAPE_LOCAL:
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
            elif _uses_manual_endpoint_flow(target, target_spec):
                # An endpoint without a shipped address needs both its key and
                # URL updated here; the URL may move when the service is redeployed.
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
            console.print(t("  [green]✓ Updated {a0}.[/green]", a0=_provider_label(target)))
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
                    t(
                        "The current default model comes from {a0}; removing it means you'll need to pick a new default. Remove anyway?",
                        a0=_provider_label(target),
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
            if auth_shape(target) == SHAPE_OAUTH:
                console.print(
                    t(
                        "  [dim]{a0}'s credential is an OAuth token, not a config field, so there is nothing here to remove.[/dim]",
                        a0=_provider_label(target),
                    )
                )
                continue
            _write_provider_fields(target, {"api_key": "", "api_base": None})
            if was_default_source:
                # Clear the now-dangling default so step 1's guard forces a
                # re-pick instead of leaving a model whose provider has no key.
                from raven.config.update import set_default_model

                # The provider goes with it: left behind it would route the next
                # model the user picks to the vendor whose key was just removed.
                # Cleared to "", not to "auto" -- that sentinel is retired, and
                # the migration that rewrites it will not come back for a config
                # already stamped at the current generation.
                set_default_model("", provider="")
            console.print(t("  [green]✓ Removed {a0}'s configuration.[/green]", a0=_provider_label(target)))


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
    _step_header(1, t("Choose your LLM provider"))
    console.print(t("  [dim]Raven's chat and reasoning are all driven by it.[/dim]"))

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
            t("LLM provider already configured: {names}. What would you like to do?", names=names),
            choices=[
                questionary.Choice(t("Done, continue"), value="done"),
                questionary.Choice(t("Choose default model"), value="model"),
                questionary.Choice(t("Add another provider"), value="add"),
                questionary.Choice(t("Edit / remove a provider"), value="edit"),
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
                    t("  [yellow]At least one provider with a default model is required — add or re-pick one.[/yellow]")
                )
                continue
            return None
        if action == "model":
            if _configure_existing_provider_model(non_interactive=False):
                continue
            console.print(t("  [yellow]Could not configure a default model. Choose a provider and try again.[/yellow]"))
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
    console.print(t("  [dim]⏳ Checking sandbox availability…[/dim]"))
    try:
        import boxlite  # noqa: F401
    except ImportError:
        return False, "missing"
    except Exception:
        return False, "error"
    return True, "ok"


def _warn_host_risk() -> None:
    console.print(
        t(
            "  [yellow]⚠ Third-party messages (channels, imports) can inject instructions.[/yellow]\n"
            "  [yellow]⚠ On the host, injected commands execute with full host privileges.[/yellow]"
        )
    )


def _confirm_host_run(questionary: Any) -> bool:
    """Warn about host-mode risk and ask for explicit confirmation (default No)."""
    from raven.cli._styles import RAVEN_STYLE

    _warn_host_risk()
    confirmed = questionary.confirm(
        t("Run directly on the host anyway?"),
        default=False,
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()
    if confirmed is None:
        raise typer.Exit(1)
    return bool(confirmed)


def _step2_sandbox(*, skip: bool, non_interactive: bool) -> object:
    """Step 2 — choose run location (host / boxlite sandbox)."""
    _step_header(2, t("Choose where Raven runs code / commands"))

    if skip or non_interactive:
        run_loc = t("Host (direct)") if _current_sandbox_backend() == "none" else t("Sandbox (boxlite)")
        console.print(t("  [dim]Keeping run location: {location}.[/dim]", location=run_loc))
        if _current_sandbox_backend() == "none":
            _warn_host_risk()
        return None

    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    current = _current_sandbox_backend()
    choices: list[Any] = []
    if current != "none":
        choices.append(questionary.Choice(t("Keep current: sandbox (boxlite)"), value="keep"))
    choices.extend(
        [
            questionary.Choice(
                t("Host (direct) — simplest, runs right on your machine"),
                value="none",
            ),
            questionary.Choice(
                t("Sandbox isolation (boxlite) — isolated in a lightweight VM, safer (needs platform support)"),
                value="boxlite",
            ),
            questionary.Choice(t("Back"), value=_BACK),
        ]
    )

    while True:
        picked = questionary.select(t("Run location:"), choices=choices, style=RAVEN_STYLE, qmark=_QMARK).ask()
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
        console.print(t("  [green]✓ Running directly on the host.[/green]"))
        return None

    # boxlite — probe before committing.
    while True:
        ok, reason = _probe_boxlite()
        if ok:
            _persist_sandbox_backend("boxlite")
            console.print(
                t(
                    "  [green]✓ Sandbox available. Using default resources "
                    "(2 CPU / 2 GB / network); tune in the config file if needed.[/green]"
                )
            )
            return None
        if reason == "missing":
            console.print(
                t(
                    "  [yellow]✗ Sandbox runtime (boxlite) isn't installed.[/yellow]\n"
                    "  [dim]Install it, then choose “Retry after install”:  "
                    "pip install 'raven\\[sandbox]'[/dim]"
                )
            )
        else:  # reason == "error": importable but failed to initialize
            console.print(
                t(
                    "  [yellow]✗ Sandbox runtime (boxlite) is installed but failed to "
                    "start.[/yellow]\n"
                    "  [dim]Your machine may lack the required virtualization support. "
                    "Fall back to host, or check the boxlite setup docs.[/dim]"
                )
            )
        while True:
            choice = _failure_choice(
                [
                    (t("Fall back to host"), "host"),
                    (t("Retry after install"), "retry"),
                    (t("Skip"), "skip"),
                ],
                non_interactive=non_interactive,
            )
            if choice == "retry":
                break
            if choice == "host":
                # A declined confirm re-asks this submenu; only "retry" may
                # re-probe (and reprint the failure banner).
                if not _confirm_host_run(questionary):
                    continue
                _persist_sandbox_backend("none")
                console.print(t("  [green]✓ Running directly on the host.[/green]"))
            return None


# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------


def _print_next_steps(*, warnings: list[str], show_next_steps: bool = True) -> None:
    from rich.table import Table

    console.print()
    if warnings:
        console.print(
            Panel(
                t("[bold yellow]⚠ Setup finished with warnings[/bold yellow]")
                + "\n\n"
                + t("[dim]These items didn't pass a connectivity test:[/dim] ")
                + f"{', '.join(warnings)}\n"
                + t(
                    "[dim]Fix them before relying on the related features "
                    "(re-run [/dim][accent]raven onboard[/accent][dim] to reconfigure).[/dim]"
                ),
                border_style="yellow",
                padding=(1, 2),
            )
        )
    else:
        console.print(
            Panel(
                t("[bold green]🎉 Setup complete![/bold green]"),
                border_style="green",
                padding=(0, 2),
            )
        )

    # Recap what was configured (read from disk) so the user has closure.
    provs = ", ".join(_provider_label(n).split(" (")[0] for n in _configured_providers()) or "—"
    run_loc = t("Host (direct)") if _current_sandbox_backend() == "none" else t("Sandbox (boxlite)")
    chans = ", ".join(onboard_channels._enabled_channels()) or t("none")
    mem = t("EverOS") if onboard_everos._memory_enabled() else t("[yellow]off[/yellow]")
    recap = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    recap.add_column(style="dim", no_wrap=True)
    recap.add_column()
    recap.add_row(t("Provider"), provs)
    recap.add_row(t("Default model"), _load_current_default_model() or "—")
    recap.add_row(t("Run location"), run_loc)
    recap.add_row(t("Channels"), chans)
    recap.add_row(t("Memory"), mem)
    console.print(
        Panel(
            recap,
            title=f"[bold]{t('Your setup')}[/bold]",
            title_align="left",
            border_style="#8a6d00",
            padding=(1, 2),
        )
    )

    if not show_next_steps:
        # The startup gate runs the wizard with the TUI already on its way in,
        # so a list of commands to try next is answered before it is read.
        console.print(t("  [dim]Setup complete - starting the TUI...[/dim]"))
        return

    table = Table(show_header=False, box=None, padding=(0, 3, 0, 0))
    table.add_column(style="accent", no_wrap=True)
    table.add_column(style="dim")
    table.add_row("raven", t("launch the native TUI (default)"))
    table.add_row("raven gateway", t("run the gateway (serve channels)"))
    table.add_row('raven agent -m "hello, world"', t("ask a one-shot question"))
    table.add_row("raven channels list", t("see connected chat channels"))
    table.add_row("raven provider list", t("check your provider config"))
    table.add_row("raven import run", t("import AI tool history into Raven"))
    table.add_row("raven --help", t("see all available commands"))
    console.print(
        Panel(
            table,
            title=f"[bold]{t('Get started')}[/bold]",
            title_align="left",
            border_style="border",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Step 7 — cold-start import
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


def _step7_import(*, skip: bool, non_interactive: bool) -> object:
    """Step 7 — optionally import conversation history from other AI tools."""
    _step_header(7, t("Import history from other AI tools"))

    if skip:
        console.print(t("  [dim]Skipped via --skip-import.[/dim]"))
        return None

    if non_interactive:
        console.print(t("  [dim]Skipped (non-interactive).[/dim]"))
        return None

    if not onboard_everos._memory_enabled():
        console.print(t("  [dim]Skipped — EverOS long-term memory is required for history import.[/dim]"))
        return None

    import sys

    from raven.cli._log_file import redirect_loguru_to_file
    from raven.cli._styles import RAVEN_STYLE

    questionary = _require_questionary()
    action = questionary.select(
        t("Would you like to import conversation history from other AI tools? (Claude Code, Codex, etc.)"),
        choices=[
            questionary.Choice(t("Yes"), value="yes"),
            questionary.Choice(t("No"), value="no"),
        ],
        style=RAVEN_STYLE,
        qmark=_QMARK,
    ).ask()
    if action is None:
        raise typer.Exit(1)
    if action == "no":
        console.print(t("  [dim]Skipped.[/dim]"))
        return None

    # Set up file logging for the entire import lifecycle.
    # Wrapped in try/finally so logging is restored on any exit path.
    from loguru import logger as _restore_logger

    log_path = redirect_loguru_to_file("import.log", terminal_level=None)
    _restore_logger.enable("raven")
    try:
        return _step7_import_body(
            questionary=questionary,
            log_path=log_path,
        )
    finally:
        _restore_logger.remove()
        _restore_logger.add(sys.stderr, level="WARNING")
        _restore_logger.disable("raven")


def _step7_import_body(
    *,
    questionary: Any,
    log_path: Any,
) -> object:
    """Inner body of step 7, runs with file logging active."""
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
            console.print(t("  No importable data found."))
        return None

    # Discovery walks the whole Hermes skill tree, and every prompt below can be
    # returned to, so it is counted once here rather than inside the loop.
    hermes_skill_count = asyncio.run(_importable_skill_count(Platform.HERMES))

    # Platform selection (sync questionary)

    def _platform_label(items: list[ScanResult], name: str) -> str:
        m = sum(1 for r in items if r.kind == SourceKind.MEMORY_FILE)
        c = sum(1 for r in items if r.kind == SourceKind.CONVERSATION)
        if m and c:
            return t("{name} ({m} memory files, {c} conversations)", name=name, m=m, c=c)
        if m:
            return t("{name} ({m} memory files)", name=name, m=m)
        return t("{name} ({c} conversations)", name=name, c=c)

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
                _platform_label(all_results, t("All platforms")),
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
                t("{a0} (coming soon)", a0=PLATFORM_DISPLAY_NAMES.get(p.value, p.value)),
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
                t("Skip import"),
                value=skip_value,
            )
        )
        selected_platform = questionary.select(
            t("Select platform:"),
            choices=platform_choices,
            style=RAVEN_STYLE,
            qmark=_QMARK,
        ).ask()
        if selected_platform is None:
            raise typer.Exit(1)
        if selected_platform == skip_value:
            console.print(t("  [dim]Skipped.[/dim]"))
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
            t(
                "  {a0} items selected ({mem} memory files, {skills} skills, {conv} conversations).",
                a0=len(results) + skills,
                mem=mem,
                skills=skills,
                conv=conv,
            )
            if skills
            else t(
                "  {a0} items selected ({mem} memory files, {conv} conversations).", a0=len(results), mem=mem, conv=conv
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
                t(
                    "  [dim]Memory files are preferences and project knowledge; skills are copied "
                    "into the local skill pool; conversations are full chat history.[/dim]"
                ),
                highlight=False,
            )
            console.print()
            file_label = t("Memory files only")
            full_label = t("Full import")
            label_width = max(_cell_len(file_label), _cell_len(full_label))
            file_contents = (
                t("{mem} memory files + {skills} skills", mem=mem, skills=skills)
                if skills
                else t("{mem} memory files", mem=mem)
            )
            tier_choices = []
            if mem:
                tier_choices.append(
                    questionary.Choice(
                        _tier_choice_label(
                            file_label,
                            label_width,
                            file_contents,
                            t("minutes, low LLM cost"),
                        ),
                        value=Tier.MEMORY_FILES,
                    )
                )
            tier_choices.append(
                questionary.Choice(
                    _tier_choice_label(
                        full_label,
                        label_width,
                        t("the above + {conv} conversations", conv=conv),
                        t("hours, high LLM cost"),
                    ),
                    value=Tier.FULL,
                )
            )
            tier_choices.append(
                questionary.Choice(
                    t("Back"),
                    value=back_value,
                )
            )
            selected_tier = questionary.select(
                t("Select import tier:"),
                choices=tier_choices,
                style=RAVEN_STYLE,
                qmark=_QMARK,
            ).ask()
            if selected_tier is None:
                raise typer.Exit(1)
            if selected_tier == back_value:
                _step_header(7, t("Import history from other AI tools"))
                break

            # -- Filter --
            filtered = filter_by_tier(results, selected_tier)
            if not filtered:
                # The tier filter has nothing of the skills' to keep, so a
                # skills-only install reaches this return with its skills still
                # uninstalled unless they are handled here.
                scope = None if selected_platform == "all" else Platform(selected_platform)
                if not asyncio.run(_install_skills_without_a_scan(scope, assume_yes=False)):
                    console.print(t("  No items match the selected tier."))
                return None

            f_mem = sum(1 for r in filtered if r.kind == SourceKind.MEMORY_FILE)
            f_conv = sum(1 for r in filtered if r.kind == SourceKind.CONVERSATION)

            # -- Execution mode --
            exec_mode = questionary.select(
                t("Select execution mode:"),
                choices=[
                    questionary.Choice(
                        t("Run now (wait for completion, show progress)"),
                        value="foreground",
                    ),
                    questionary.Choice(
                        t("Run in background (use raven import status to check progress)"),
                        value="background",
                    ),
                    questionary.Choice(
                        t("Back"),
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
        else t("All platforms")
    )
    tier_display = t("Memory files only") if selected_tier == Tier.MEMORY_FILES else t("Full import")
    mode_display = t("Run now") if exec_mode == "foreground" else t("Background")
    console.print(
        t(
            "\n  About to import:\n    Platform: {platform_display}\n    Tier:     {tier_display}\n    Items:    {a2} ({f_mem} memory files, {skills} skills, {f_conv} conversations)\n    Mode:     {mode_display}",
            platform_display=platform_display,
            tier_display=tier_display,
            a2=len(filtered) + skills,
            f_mem=f_mem,
            skills=skills,
            f_conv=f_conv,
            mode_display=mode_display,
        )
    )
    if not typer.confirm(
        t("  Start?"),
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
            console.print(t("  [red]Cannot find 'raven' command. Falling back to foreground execution.[/red]"))
            exec_mode = "foreground"
        elif platform_flag is None and len(by_platform) > 1:
            # `import run` has no way to say "every platform": with no --platform
            # and more than one platform holding data, the child reaches the
            # platform picker. A detached process with DEVNULL on both streams has
            # no terminal to ask on, so it would hang unseen after this step had
            # already reported the import as started.
            console.print(
                t(
                    "  [yellow]An all-platforms import cannot run in the background yet;\n"
                    "  running it in the foreground instead.[/yellow]"
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
                t(
                    "\n  Import started in background.\n  Check progress: [accent]raven import status[/accent]\n  Log: {log_path}",
                    log_path=log_path,
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
                t("Importing..."),
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
    skip_web: bool = False,
    skip_subagents: bool = False,
    skip_import: bool = False,
    serper_api_key: Optional[str] = None,
    jina_api_key: Optional[str] = None,
    search_provider: Optional[str] = None,
    fetch_provider: Optional[str] = None,
    search_api_key: Optional[str] = None,
    fetch_api_key: Optional[str] = None,
    non_interactive: bool = False,
    yes: bool = False,
    reset: bool = False,
    skip_test: bool = False,
    show_next_steps: bool = True,
) -> None:
    """Run the seven-step onboarding wizard end-to-end.

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
            skip_web=skip_web,
            skip_subagents=skip_subagents,
            skip_import=skip_import,
            serper_api_key=serper_api_key,
            jina_api_key=jina_api_key,
            search_provider=search_provider,
            fetch_provider=fetch_provider,
            search_api_key=search_api_key,
            fetch_api_key=fetch_api_key,
            non_interactive=non_interactive,
            yes=yes,
            reset=reset,
            skip_test=skip_test,
            show_next_steps=show_next_steps,
        )
    finally:
        _logger.enable("raven")


def _step6_subagents(*, skip: bool, non_interactive: bool, warnings: list[str]) -> object:
    """Step 6 — the sub-agents in this checkout, optional, forward-only.

    This is a wizard step rather than an installer step because it needs a
    configured host raven, and the retired vendored installer ran before one existed.
    Skipped on --skip-subagents or non-interactive; roster membership comes from
    discovery either way, so leaving it undone just means the vendored agents
    stay listed-and-disabled until their venvs are built, and re-running
    ``onboard`` builds them.
    """
    _step_header(6, t("Sub-agents"))
    if skip or non_interactive:
        console.print(t("  [dim]Skipping the sub-agents (set them up later: raven onboard).[/dim]"))
        return None
    from raven.cli.subagent_setup import configure_subagents

    configure_subagents(non_interactive=non_interactive, warnings=warnings)
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
    skip_web: bool = False,
    skip_subagents: bool = False,
    skip_import: bool = False,
    serper_api_key: Optional[str] = None,
    jina_api_key: Optional[str] = None,
    search_provider: Optional[str] = None,
    fetch_provider: Optional[str] = None,
    search_api_key: Optional[str] = None,
    fetch_api_key: Optional[str] = None,
    non_interactive: bool = False,
    yes: bool = False,
    reset: bool = False,
    skip_test: bool = False,
    show_next_steps: bool = True,
) -> None:
    _check_tty_or_die(non_interactive)
    i18n.set_language(_config_language())  # start from the saved language (default "en")
    if not non_interactive:
        _pick_language()  # may change the UI language (persisted after bootstrap below)
    _handle_existing_config(reset=reset, yes=yes, non_interactive=non_interactive)
    _bootstrap_empty_config()
    if not non_interactive:
        from raven.config.update import set_language

        set_language(i18n.current_language())  # persist now that config.json exists

    console.print()
    console.print(
        Panel(
            t(
                "[bold][accent]✨ Welcome to the Raven setup wizard[/accent][/bold]\n\n"
                "[dim]We'll configure, in order:[/dim]\n"
                "  [accent]①[/accent] LLM      [accent]②[/accent] Run location      "
                "[accent]③[/accent] Chat channel      [accent]④[/accent] Long-term memory\n"
                "  [accent]⑤[/accent] Web access      [accent]⑥[/accent] Sub-agents         "
                "[accent]⑦[/accent] Import history\n\n"
                "[dim]↑↓ select · Enter confirm · Ctrl+C quit anytime — anything already written is kept.[/dim]"
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
        # Ahead of the sub-agents screen on purpose: the folders it sets up fall
        # back to the host's config for these vendors and keys, so writing them
        # first is what lets a folder inherit rather than be asked again.
        lambda: onboard_web._step5_web(
            skip=skip_web,
            non_interactive=non_interactive,
            yes=yes,
            serper_api_key=serper_api_key,
            jina_api_key=jina_api_key,
            search_provider=search_provider,
            fetch_provider=fetch_provider,
            search_api_key=search_api_key,
            fetch_api_key=fetch_api_key,
        ),
        lambda: _step6_subagents(
            skip=skip_subagents,
            non_interactive=non_interactive,
            warnings=warnings,
        ),
        lambda: _step7_import(skip=skip_import, non_interactive=non_interactive),
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

                set_language(i18n.current_language())
            else:
                index -= 1
        else:
            index += 1

    _print_next_steps(warnings=warnings, show_next_steps=show_next_steps)


# ---------------------------------------------------------------------------
# Startup gate — invoked by bare `raven` and `raven tui` (the same callback).
# One-shot `raven agent` deliberately stays out: launching a seven-step wizard
# from a scriptable command would be a surprise, so it reports the missing
# credentials through `check_provider_credentials` instead.
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
        run_wizard(non_interactive=non_interactive, show_next_steps=False)
        return

    model = (_load_raw_config().get("agents", {}) or {}).get("defaults", {}).get("model")
    # Says what was found, not why: the provider it resolves to may have no
    # credentials, or the id may resolve to a provider that never served it (a
    # deployment name carrying another vendor's keyword does that). Naming a cause
    # we have not established sends the user to fix the wrong thing.
    console.print(t("  [yellow]No usable provider resolves the default model ({model}).[/yellow]", model=model))
    console.print(
        t("  [dim]Choose one that works: `raven tui` then /model. Or `raven onboard` to set this up again.[/dim]")
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
            help="Server URL: required for a local deployment (LM Studio / Ollama / vLLM), or a custom OpenAI-compatible endpoint",
        ),
        model: Optional[str] = typer.Option(None, "--model", help="Default model id (e.g. 'openai/gpt-4o-mini')"),
        channel: Optional[str] = typer.Option(None, "--channel", help="Channel to enable in Step 3"),
        serper_api_key: Optional[str] = typer.Option(
            None,
            "--serper-api-key",
            help="Serper key for web_search (honoured even under --non-interactive, which skips Step 5)",
        ),
        jina_api_key: Optional[str] = typer.Option(
            None,
            "--jina-api-key",
            help="Jina key for web_fetch (honoured even under --non-interactive, which skips Step 5)",
        ),
        search_provider: Optional[str] = typer.Option(
            None,
            "--search-provider",
            help="web_search vendor: serper, anysearch, serpapi, tavily, exa, brave or firecrawl",
        ),
        fetch_provider: Optional[str] = typer.Option(
            None,
            "--fetch-provider",
            help="web_fetch vendor: jina, anysearch, tavily, exa or firecrawl",
        ),
        search_api_key: Optional[str] = typer.Option(
            None,
            "--search-api-key",
            help="Key for the web_search vendor (the one --search-provider names, or the configured one)",
        ),
        fetch_api_key: Optional[str] = typer.Option(
            None,
            "--fetch-api-key",
            help="Key for the web_fetch vendor (the one --fetch-provider names, or the configured one)",
        ),
        skip_sandbox: bool = typer.Option(False, "--skip-sandbox", help="Skip Step 2 (run location)"),
        skip_channel: bool = typer.Option(False, "--skip-channel", help="Skip Step 3 (channel setup)"),
        skip_memory: bool = typer.Option(False, "--skip-memory", help="Skip Step 4 (long-term memory)"),
        skip_web: bool = typer.Option(False, "--skip-web", help="Skip Step 5 (web tool keys)"),
        skip_subagents: bool = typer.Option(False, "--skip-subagents", help="Skip Step 6 (sub-agent setup)"),
        skip_import: bool = typer.Option(False, "--skip-import", help="Skip Step 7 (history import)"),
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
        """Seven-step setup wizard: provider → sandbox → channel → memory → web → sub-agents → import."""
        run_wizard(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            channel=channel,
            skip_sandbox=skip_sandbox,
            skip_channel=skip_channel,
            skip_memory=skip_memory,
            skip_web=skip_web,
            skip_subagents=skip_subagents,
            skip_import=skip_import,
            serper_api_key=serper_api_key,
            jina_api_key=jina_api_key,
            search_provider=search_provider,
            fetch_provider=fetch_provider,
            search_api_key=search_api_key,
            fetch_api_key=fetch_api_key,
            non_interactive=non_interactive,
            yes=yes,
            reset=reset,
            skip_test=skip_test,
        )


__all__ = ["register", "run_wizard", "ensure_ready_to_start"]
