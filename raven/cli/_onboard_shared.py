"""Shared onboarding wizard kit: console, language, prompt chrome, primitives.

Extracted from onboard_commands so the four onboard modules import downward
instead of forming a sibling import cycle (channels/everos/web reached back
into onboard_commands for these at module level, an SCC held together only
by the partial-import fallback). Language state lives here; the wizard sets
it through :func:`set_lang`.
"""

from __future__ import annotations

from typing import Any, Optional

import typer
from rich.console import Console
from rich.panel import Panel

from raven.cli._theme import POINTER, QMARK


def set_lang(lang: str) -> None:
    """Set the wizard UI language ('en' / 'zh') for every later prompt."""
    global _LANG
    _LANG = lang


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

_TOTAL_STEPS = 7

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


def _load_raw_config() -> dict[str, Any]:
    """Return the parsed on-disk config, or ``{}`` if absent/empty.

    A present-but-unparseable config raises ConfigReadError (surfaced cleanly by
    the CLI entrypoint) instead of being silently treated as empty -- which
    would let onboard misread state and write over a config whose only fault is
    a syntax typo.
    """
    from raven.config.loader import get_config_path, read_raw_or_raise

    return read_raw_or_raise(get_config_path()) or {}


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
