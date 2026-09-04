# Worked example: parameterized gear schematic

This example demonstrates how shared parameters and executable assertions keep
a schematic internally consistent. It intentionally does **not** claim to be a
mechanically valid gear design.

## Transferable mechanism

1. **Name the claim before drawing.** Both schematic gears share a module, their
   pitch circles are tangent, the facing tooth and gap are phase-aligned at the
   initial frame, and their animation periods use the inverse tooth-count ratio.
2. **Generate related geometry from the same values.** Tooth points, centers,
   spokes, pitch guides, transforms, and animation periods derive from `M`,
   `Z1`, and `Z2`.
3. **Validate the geometry that is actually present.** The generator checks the
   transformed outer paths plus stroke padding. The SVG contains no text,
   filters, markers, or geometry outside those checked radial bounds.
4. **Keep mathematical and perceptual evidence separate.** Assertions verify
   declared relationships; a rendered inspection still checks whether the
   schematic reads clearly at its target size.
5. **State the proof boundary.** Trapezoidal teeth make the relationship easy to
   see, but pitch-circle tangency and phase alignment do not prove involute
   contact, interference-free rotation, load capacity, or manufacturability.

## Deliberately not reusable

The tooth profile, tooth counts, flat colors, dimensions, and page composition
are teaching choices, not a mechanical or visual template. A real transmission
requires an appropriate gear model and engineering validation; another kind of
diagram needs assertions for its own promised relationships.

## Code (`gen_gear_schematic.py`)

```python
#!/usr/bin/env python3
"""Generate and validate a phase-aligned gear schematic.

The checks prove only the claims printed by this script. The trapezoidal tooth
profile is illustrative and must not be used as a manufactured gear profile.
"""

from __future__ import annotations

import math
from pathlib import Path


VIEW_W, VIEW_H = 760.0, 500.0
M = 14.0
Z1, Z2 = 14, 20
TOOTH_TIP_FRACTION = 0.38
FLANK_FRACTION = 0.20
STROKE_WIDTH = 3.0
T1 = 8.0


def gear_points(z: int, module: float, phase_degrees: float) -> list[tuple[float, float]]:
    """Return a centered schematic outline; this is not an involute profile."""
    pitch_radius = module * z / 2
    tip_radius = pitch_radius + 0.9 * module
    root_radius = pitch_radius - 1.1 * module
    pitch_angle = 360.0 / z
    tip_half = TOOTH_TIP_FRACTION * pitch_angle / 2
    root_half = tip_half + FLANK_FRACTION * pitch_angle
    points: list[tuple[float, float]] = []
    for index in range(z):
        center = phase_degrees + index * pitch_angle
        gap_offset = (pitch_angle / 2 - root_half) / 2
        profile = (
            (center - root_half - gap_offset, root_radius),
            (center - root_half, root_radius),
            (center - tip_half, tip_radius),
            (center + tip_half, tip_radius),
            (center + root_half, root_radius),
            (center + root_half + gap_offset, root_radius),
        )
        for angle_degrees, radius in profile:
            angle = math.radians(angle_degrees)
            points.append((radius * math.cos(angle), radius * math.sin(angle)))
    return points


def translate(points: list[tuple[float, float]], cx: float, cy: float) -> list[tuple[float, float]]:
    return [(cx + x, cy + y) for x, y in points]


def path_data(points: list[tuple[float, float]]) -> str:
    return "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in points) + " Z"


def rotational_painted_bounds(
    points: list[tuple[float, float]], cx: float, cy: float, stroke_width: float
) -> tuple[float, float, float, float]:
    """Conservative bounds for every rotation plus a round centered stroke."""
    radius = max(math.hypot(x - cx, y - cy) for x, y in points)
    extent = radius + stroke_width / 2
    return cx - extent, cy - extent, cx + extent, cy + extent


def assert_inside(label: str, bounds: tuple[float, float, float, float]) -> None:
    left, top, right, bottom = bounds
    assert 0 <= left < right <= VIEW_W, f"{label} exceeds horizontal viewBox: {bounds}"
    assert 0 <= top < bottom <= VIEW_H, f"{label} exceeds vertical viewBox: {bounds}"


def spokes(cx: float, cy: float, inner: float, outer: float, count: int) -> str:
    lines = []
    for index in range(count):
        angle = math.radians(index * 360 / count)
        lines.append(
            f'<line x1="{cx + inner * math.cos(angle):.2f}" '
            f'y1="{cy + inner * math.sin(angle):.2f}" '
            f'x2="{cx + outer * math.cos(angle):.2f}" '
            f'y2="{cy + outer * math.sin(angle):.2f}" />'
        )
    return "".join(lines)


r1 = M * Z1 / 2
r2 = M * Z2 / 2
cx1, cy = 220.0, 250.0
cx2 = cx1 + r1 + r2
phase1 = 0.0
pitch2 = 360.0 / Z2
phase2 = 180.0 + pitch2 / 2
T2 = T1 * Z2 / Z1

points1 = translate(gear_points(Z1, M, phase1), cx1, cy)
points2 = translate(gear_points(Z2, M, phase2), cx2, cy)
bounds1 = rotational_painted_bounds(points1, cx1, cy, STROKE_WIDTH)
bounds2 = rotational_painted_bounds(points2, cx2, cy, STROKE_WIDTH)

# These are the complete mathematical claims made by this schematic.
assert math.isclose(cx2 - cx1, r1 + r2, abs_tol=1e-9)
assert math.isclose(phase2 - pitch2 / 2, 180.0, abs_tol=1e-9)
assert math.isclose(T2 / T1, Z2 / Z1, rel_tol=1e-9)
assert len(points1) == Z1 * 6 and len(points2) == Z2 * 6
assert_inside("gear one painted path", bounds1)
assert_inside("gear two painted path", bounds2)

svg_markup = f"""
<svg viewBox="0 0 {VIEW_W:.0f} {VIEW_H:.0f}" role="img"
     aria-labelledby="gear-title gear-description">
  <title id="gear-title">Two phase-aligned schematic gears</title>
  <desc id="gear-description">The pitch circles touch and the gears rotate at a tooth-count ratio. The tooth shapes are illustrative.</desc>
  <line class="center-line" x1="{cx1}" y1="{cy}" x2="{cx2}" y2="{cy}" />
  <circle class="pitch" cx="{cx1}" cy="{cy}" r="{r1}" />
  <circle class="pitch" cx="{cx2}" cy="{cy}" r="{r2}" />
  <g id="gear-one" style="transform-origin: {cx1}px {cy}px; --period: {T1}s">
    <path class="gear gear-one" d="{path_data(points1)}" />
    <g class="spokes">{spokes(cx1, cy, M * 1.5, r1 - M * 2.5, 5)}</g>
    <circle class="hub" cx="{cx1}" cy="{cy}" r="{M * 1.5}" />
  </g>
  <g id="gear-two" style="transform-origin: {cx2}px {cy}px; --period: {T2}s">
    <path class="gear gear-two" d="{path_data(points2)}" />
    <g class="spokes">{spokes(cx2, cy, M * 1.5, r2 - M * 2.5, 6)}</g>
    <circle class="hub" cx="{cx2}" cy="{cy}" r="{M * 1.5}" />
  </g>
</svg>
"""

facts_markup = f"""
<dl>
  <div><dt>Tooth counts</dt><dd>{Z1} and {Z2}</dd></div>
  <div><dt>Shared module</dt><dd>{M:g} schematic units</dd></div>
  <div><dt>Pitch-center distance</dt><dd>{r1 + r2:g} units</dd></div>
  <div><dt>Animation periods</dt><dd>{T1:g}s and {T2:.2f}s</dd></div>
</dl>
"""

html_template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Parameterized gear schematic</title>
<style>
  :root {
    --surface: #ffffff;
    --text: #111827;
    --text-subtle: #374151;
    --rule: #64748b;
    --gear-one: #cbd5e1;
    --gear-two: #94a3b8;
  }
  * { box-sizing: border-box; }
  html { background: var(--surface); color: var(--text); font-family: system-ui, sans-serif; }
  body { margin: 0; font-size: 1rem; line-height: 1.5; }
  main { width: min(100% - 2rem, 64rem); margin-inline: auto; padding-block: 3rem; }
  h1 { margin: 0; font-size: 2rem; line-height: 1.12; }
  .lede { max-width: 72ch; color: var(--text-subtle); }
  .drawing { border-block: 1px solid var(--rule); }
  svg { display: block; width: 100%; height: auto; }
  .gear, .hub, .spokes { stroke: var(--text); stroke-width: 3; stroke-linejoin: round; vector-effect: non-scaling-stroke; }
  .gear-one { fill: var(--gear-one); }
  .gear-two { fill: var(--gear-two); }
  .hub { fill: var(--surface); }
  .spokes { fill: none; stroke-width: 12; }
  .pitch { fill: none; stroke: var(--rule); stroke-width: 2; stroke-dasharray: 7 7; vector-effect: non-scaling-stroke; }
  .center-line { stroke: var(--rule); stroke-width: 2; vector-effect: non-scaling-stroke; }
  #gear-one { animation: spin var(--period) linear infinite; }
  #gear-two { animation: spin var(--period) linear infinite reverse; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .paused #gear-one, .paused #gear-two { animation-play-state: paused; }
  button { min-height: 2.75rem; margin-top: 1rem; padding: 0.625rem 1rem; border: 2px solid var(--text); border-radius: 0.25rem; background: var(--surface); color: var(--text); font: inherit; font-weight: 700; cursor: pointer; }
  button:focus-visible { outline: 3px solid var(--rule); outline-offset: 3px; }
  button:disabled { cursor: not-allowed; opacity: 0.72; }
  dl { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; margin-block: 2rem; background: var(--rule); }
  dl div { padding: 1rem; background: var(--surface); }
  dt { color: var(--text-subtle); }
  dd { margin: 0.25rem 0 0; font-weight: 700; }
  .boundary { max-width: 72ch; }
  @media (max-width: 44rem) {
    main { width: min(100% - 1.25rem, 64rem); padding-block: 1.5rem; }
    h1 { font-size: 1.625rem; }
    dl { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (prefers-reduced-motion: reduce) {
    #gear-one, #gear-two { animation: none; }
  }
</style>
</head>
<body>
<main>
  <h1>Shared parameters keep the schematic consistent</h1>
  <p class="lede">The diagram verifies pitch-circle tangency, initial phase alignment, animation ratio, and painted bounds. It does not certify a physical gear profile.</p>
  <div class="drawing">__SVG__</div>
  <button id="motion-toggle" type="button" aria-pressed="false">Pause motion</button>
  <p id="motion-status" aria-live="polite">Motion is running.</p>
  __FACTS__
  <p class="boundary"><strong>Proof boundary:</strong> this is a parameterized schematic, not an involute, interference, strength, or manufacturing analysis.</p>
</main>
<script>
  const button = document.getElementById('motion-toggle');
  const status = document.getElementById('motion-status');
  const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');

  function syncMotionPreference() {
    document.body.classList.remove('paused');
    button.setAttribute('aria-pressed', 'false');
    button.disabled = motionPreference.matches;
    button.textContent = motionPreference.matches ? 'Motion disabled by system preference' : 'Pause motion';
    status.textContent = motionPreference.matches ? 'The static frame is shown.' : 'Motion is running.';
  }

  button.addEventListener('click', () => {
    const paused = document.body.classList.toggle('paused');
    button.setAttribute('aria-pressed', String(paused));
    button.textContent = paused ? 'Resume motion' : 'Pause motion';
    status.textContent = paused ? 'Motion is paused.' : 'Motion is running.';
  });
  motionPreference.addEventListener('change', syncMotionPreference);
  syncMotionPreference();
</script>
</body>
</html>
"""

output = Path(__file__).with_name("gear-schematic.html")
output.write_text(
    html_template.replace("__SVG__", svg_markup).replace("__FACTS__", facts_markup),
    encoding="utf-8",
)
print(f"wrote {output}")
print(f"gear one painted bounds: {bounds1}")
print(f"gear two painted bounds: {bounds2}")
print("verified: pitch tangency, initial phase, period ratio, and painted bounds")
print("not verified: involute contact, interference, loads, or manufacturability")
```
