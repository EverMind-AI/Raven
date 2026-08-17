"""Tests for CLI light/dark theme detection and palette (issue #169).

Covers: luminance classification, OSC 11 reply parsing, env precedence and
fallback, non-TTY / Windows / SSH short-circuit, dark byte-for-byte parity,
light-scheme readability regression, reduced-color-depth readability, and the
rich-theme crash-protection path (every onboard markup token resolves).
"""

from __future__ import annotations

import io

import pytest

from raven.cli import _theme

# capture the real predicate before the autouse fixture stubs it out
_REAL_CAN_PROBE = _theme._can_probe


@pytest.fixture(autouse=True)
def _reset_theme_cache(monkeypatch):
    _theme._cache = None
    for var in ("RAVEN_THEME", "RAVEN_TERM_BACKGROUND", "COLORFGBG", "CI", "SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(var, raising=False)
    # keep detection deterministic: never actually probe the terminal in tests
    monkeypatch.setattr(_theme, "_can_probe", lambda: False)
    yield
    _theme._cache = None


def _wcag_contrast(hex_fg: str, hex_bg: str) -> float:
    def rel_lum(h: str) -> float:
        h = h.lstrip("#")
        chans = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in chans]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    l1, l2 = rel_lum(hex_fg), rel_lum(hex_bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# --- luminance classification -------------------------------------------------


@pytest.mark.parametrize(
    "hex_bg, expected_light",
    [("#ffffff", True), ("#000000", False), ("#fff", True), ("#1e1e1e", False)],
)
def test_luminance_classification(hex_bg, expected_light):
    assert _theme._is_light_rgb(_theme._parse_hex(hex_bg)) is expected_light


def test_luminance_threshold_boundary():
    # gray at the 0.6 luma edge: just-below stays dark, just-above flips light
    assert _theme._is_light_rgb((150, 150, 150)) is False
    assert _theme._is_light_rgb((160, 160, 160)) is True


def test_parse_hex_rejects_garbage():
    assert _theme._parse_hex("nothex") is None
    assert _theme._parse_hex("#12") is None


# --- OSC 11 reply parsing -----------------------------------------------------


def test_osc_reply_rgb_16bit():
    assert _theme._osc_reply_to_rgb("\033]11;rgb:ffff/f5f5/eaea\033\\") == (255, 245, 234)


def test_osc_reply_hex():
    assert _theme._osc_reply_to_rgb("\033]11;#fff5ea\a") == (255, 245, 234)


# --- env precedence + fallback ------------------------------------------------


@pytest.mark.parametrize("value, expected", [("light", "light"), ("dark", "dark"), ("LIGHT", "light")])
def test_raven_theme_env_wins(monkeypatch, value, expected):
    monkeypatch.setenv("RAVEN_THEME", value)
    monkeypatch.setenv("COLORFGBG", "0;0")  # would say dark; explicit env must win
    assert _theme.detect_scheme() == expected


def test_raven_term_background_hint(monkeypatch):
    monkeypatch.setenv("RAVEN_TERM_BACKGROUND", "#ffffff")
    assert _theme.detect_scheme() == "light"


@pytest.mark.parametrize("colorfgbg, expected", [("15;7", "light"), ("7", "light"), ("15;0", "dark"), ("0", "dark")])
def test_colorfgbg(monkeypatch, colorfgbg, expected):
    monkeypatch.setenv("COLORFGBG", colorfgbg)
    assert _theme.detect_scheme() == expected


def test_fallback_dark_when_no_signal():
    assert _theme.detect_scheme() == "dark"


def test_fallback_is_not_cached(monkeypatch):
    # a fallback (non-definitive) result must not poison a later definitive call
    assert _theme.detect_scheme() == "dark"
    monkeypatch.setenv("RAVEN_THEME", "light")
    assert _theme.detect_scheme() == "light"


# --- short-circuit (no probe) -------------------------------------------------


def test_no_probe_on_non_tty(monkeypatch):
    monkeypatch.setattr(_theme.os, "name", "posix")
    monkeypatch.setattr(_theme.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(_theme.sys.stdout, "isatty", lambda: False, raising=False)
    assert _REAL_CAN_PROBE() is False


def test_no_probe_on_windows(monkeypatch):
    monkeypatch.setattr(_theme.os, "name", "nt")
    assert _REAL_CAN_PROBE() is False


def test_no_probe_under_ssh(monkeypatch):
    monkeypatch.setattr(_theme.os, "name", "posix")
    monkeypatch.setattr(_theme.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(_theme.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    assert _REAL_CAN_PROBE() is False


def test_no_probe_under_ci(monkeypatch):
    monkeypatch.setattr(_theme.os, "name", "posix")
    monkeypatch.setattr(_theme.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(_theme.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("CI", "true")
    assert _REAL_CAN_PROBE() is False


def test_no_probe_under_screen(monkeypatch):
    # GNU screen echoes the OSC 11 query as visible garbage; default tmux is
    # TERM=screen-256color and can't answer a bare query. Skip the probe.
    monkeypatch.setattr(_theme.os, "name", "posix")
    monkeypatch.setattr(_theme.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(_theme.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("TERM", "screen-256color")
    assert _REAL_CAN_PROBE() is False


# --- dark byte-for-byte parity (D2: dark rendering unchanged) -----------------


def test_dark_questionary_style_byte_identical():
    expected = {
        "qmark": "fg:#fbe23f bold",
        "question": "bold",
        "answer": "fg:#fbe23f bold",
        "pointer": "fg:#fbe23f bold",
        "highlighted": "fg:#FFF5EA bold noreverse",
        "selected": "fg:#c8a900 noreverse",
        "separator": "fg:#444444",
        "instruction": "fg:#6c6c6c italic",
        "disabled": "fg:#585858 italic",
        "validation-toolbar": "fg:#ff5f5f bold",
        "text": "fg:#FFF5EA",
    }
    got = dict(_theme.build_questionary_style("dark").style_rules)
    assert got == expected


# --- light readability (regression for #169) ----------------------------------


def test_light_text_is_not_a_light_color():
    # #169: list rows / input used #FFF5EA (near-white) and vanished on white.
    assert _theme.PALETTE["light"]["text"] == "#24201a"
    rules = dict(_theme.build_questionary_style("light").style_rules)
    assert rules["text"] == "fg:#24201a"
    assert rules["highlighted"] == "fg:#24201a bold noreverse"


@pytest.mark.parametrize("token", ["accent", "text", "selected", "border", "muted", "error", "disabled"])
def test_light_body_tokens_pass_wcag_on_white(token):
    assert _wcag_contrast(_theme.PALETTE["light"][token], "#ffffff") >= 4.5


@pytest.mark.parametrize("token", ["accent", "text", "selected", "error"])
def test_dark_body_tokens_pass_wcag_on_black(token):
    assert _wcag_contrast(_theme.PALETTE["dark"][token], "#1e1e1e") >= 4.5


# --- reduced color depth readability (D1 sentinel) ----------------------------


@pytest.mark.parametrize("token", ["accent", "text", "error"])
def test_light_tokens_readable_after_256_downgrade(token):
    from rich.color import Color, ColorSystem

    hex_val = _theme.PALETTE["light"][token]
    approx = Color.parse(hex_val).downgrade(ColorSystem.EIGHT_BIT).get_truecolor().hex
    assert _wcag_contrast(approx, "#ffffff") >= 4.0


# --- crash protection: every onboard markup token resolves --------------------


def test_rich_theme_defines_all_onboard_tokens():
    """Every rich style word in onboard's string literals resolves in the theme.

    Scans only string-literal contents (via ast) so Python subscripts / type
    hints like ``list[str]`` are not mistaken for markup tags.
    """
    import ast
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "raven" / "cli" / "onboard_commands.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
    blob = "\n".join(literals)
    # negative lookbehind skips rich-escaped brackets like ``\[sandbox]``
    tags = set(re.findall(r"(?<!\\)\[/?([a-z]+(?: [a-z]+)?)\]", blob))
    theme_names = set(_theme.build_rich_theme("light").styles.keys())
    builtin = {"dim", "bold", "red", "green", "yellow", "italic", "i", "b", "u", "reverse"}
    words = {w for tag in tags for w in tag.split()} - builtin
    missing = {w for w in words if w not in theme_names}
    assert not missing, f"onboard markup uses undefined theme tokens: {missing}"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_onboard_panels_render_without_missing_style(scheme):
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    theme = _theme.build_rich_theme(scheme)
    console = Console(theme=theme, file=io.StringIO(), force_terminal=True)
    console.print("[accent]x[/accent] [bold][accent]y[/accent][/bold] [heading]z[/heading]")
    console.print(Panel("b", title="[bold][accent]t[/accent][/bold]", border_style="border"))
    console.print(Panel("recap", border_style="#8a6d00"))  # preserved literal
    table = Table()
    table.add_column("h", style="accent", no_wrap=True)
    table.add_column("d", style="dim")
    table.add_row("a", "b")
    console.print(table)  # would raise MissingStyle if 'accent' unresolved


_THEME_KEYS = {"accent", "text", "heading", "selected", "border", "muted", "separator", "disabled", "error"}


def test_no_compound_markup_mixes_theme_key():
    """A theme style name must be its own markup tag. rich cannot resolve a
    custom theme name combined with another word: ``[bold accent]`` silently
    renders as bare text (no bold, no color, no exception). Nested tags
    ``[bold][accent]...[/accent][/bold]`` are the correct form.
    """
    import ast
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "raven" / "cli" / "onboard_commands.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    literals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    blob = "\n".join(literals)
    bad = set()
    for tag in re.findall(r"(?<!\\)\[/?([a-z][a-z ]*)\]", blob):
        words = tag.split()
        if len(words) > 1 and _THEME_KEYS & set(words):
            bad.add(tag)
    assert not bad, f"compound markup mixes a theme key with another word (renders unstyled): {bad}"


def test_bold_accent_renders_styled_not_bare():
    """Byte-level guard: nested [bold][accent] emits bold + the accent color,
    not bare text (the silent-degrade failure mode of a bad compound tag)."""
    from rich.console import Console

    con = Console(
        theme=_theme.build_rich_theme("dark"), file=io.StringIO(), force_terminal=True, color_system="truecolor"
    )
    con.print("[bold][accent]X[/accent][/bold]", end="")
    out = con.file.getvalue()
    assert "1;38;2;251;226;63" in out, f"expected bold + accent(#fbe23f) ANSI, got {out!r}"


def test_themed_console_self_themes_on_first_print():
    """Regression: onboard's console themes itself on first print, so any render
    path (any wizard helper, deep-research, etc.) resolves theme tokens without a
    prior push — no order-coupling, no MissingStyle. A plain Console would raise.
    """
    from rich.panel import Panel

    from raven.cli.onboard_commands import _ThemedConsole

    con = _ThemedConsole(file=io.StringIO(), force_terminal=True)
    assert con._themed is False
    con.print(Panel("[accent]x[/accent] [heading]y[/heading]", border_style="border"))
    assert con._themed is True
