from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from pathlib import Path

EXAMPLES_DIR = Path(__file__).parents[1] / "raven" / "memory_engine" / "skills" / "visual-artifact-design" / "examples"
EXAMPLE_NAMES = (
    "chart-page.md",
    "computed-geometry.md",
    "design-brief.md",
    "svg-scene.md",
)
FORBIDDEN_STYLE_FINGERPRINTS = (
    "Georgia",
    "Times New Roman",
    "#faf6ef",
    "#8a7a66",
    'class="card"',
    "phase-chips",
    "quiet finish",
    "white card",
)
CSS_FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([0-9.]+)(px|rem)\b")
SVG_FONT_SIZE_RE = re.compile(r"font-size=[\"']([0-9.]+)(px|rem)?[\"']")
BUTTON_RE = re.compile(r"<button\b[^>]*>(.*?)</button>", re.DOTALL | re.IGNORECASE)


def _example_text(name: str) -> str:
    return (EXAMPLES_DIR / name).read_text(encoding="utf-8")


def _css_hex_variable(text: str, name: str) -> str:
    match = re.search(rf"--{re.escape(name)}\s*:\s*(#[0-9a-fA-F]{{6}})\b", text)
    assert match is not None, f"missing --{name}"
    return match.group(1)


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    light, dark = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _assert_readable_font_sizes(name: str, text: str) -> None:
    for value, unit in CSS_FONT_SIZE_RE.findall(text):
        minimum = 14 if unit == "px" else 0.875
        assert float(value) >= minimum, f"{name}: {value}{unit} is below the shared baseline"
    for value, unit in SVG_FONT_SIZE_RE.findall(text):
        minimum = 14 if unit in ("", "px") else 0.875
        assert float(value) >= minimum, f"{name}: SVG {value}{unit} is below the shared baseline"


def _assert_formal_button_labels(name: str, text: str) -> None:
    for markup in BUTTON_RE.findall(text):
        label = re.sub(r"<[^>]+>", "", markup)
        symbols = [character for character in label if unicodedata.category(character) == "So"]
        assert not symbols, f"{name}: formal button label contains symbol/emoji {symbols!r}"


def test_visual_examples_keep_shared_accessibility_and_responsive_baselines() -> None:
    assert tuple(sorted(path.name for path in EXAMPLES_DIR.glob("*.md"))) == tuple(sorted(EXAMPLE_NAMES))

    for name in EXAMPLE_NAMES:
        text = _example_text(name)
        assert 'name="viewport"' in text, name
        assert "@media (max-width" in text, name
        assert "@media (prefers-reduced-motion: reduce)" in text, name
        assert "font-family: system-ui" in text, name
        assert "Deliberately not reusable" in text, name

        for fingerprint in FORBIDDEN_STYLE_FINGERPRINTS:
            assert fingerprint.casefold() not in text.casefold(), (name, fingerprint)

        surface = _css_hex_variable(text, "surface")
        primary = _css_hex_variable(text, "text")
        secondary = _css_hex_variable(text, "text-subtle")
        assert _contrast_ratio(surface, primary) >= 4.5, name
        assert _contrast_ratio(surface, secondary) >= 4.5, name

        _assert_readable_font_sizes(name, text)
        _assert_formal_button_labels(name, text)


def test_computed_geometry_example_executes_and_limits_its_claims(tmp_path: Path) -> None:
    text = _example_text("computed-geometry.md")
    assert re.search(r"do(?:es)? not prove involute", text)
    assert "actually mesh" not in text
    assert "kinematically true" not in text

    match = re.search(r"```python\n(.*?)\n```", text, re.DOTALL)
    assert match is not None
    script = tmp_path / "gen_gear_schematic.py"
    script.write_text(match.group(1), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert "verified: pitch tangency, initial phase, period ratio, and painted bounds" in completed.stdout
    assert "not verified: involute contact, interference, loads, or manufacturability" in completed.stdout

    generated = (tmp_path / "gear-schematic.html").read_text(encoding="utf-8")
    assert 'name="viewport"' in generated
    assert "prefers-reduced-motion" in generated
    assert "not an involute" in generated
