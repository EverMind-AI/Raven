#!/usr/bin/env python3
"""Regenerate ``raven/ppt/services/assets/data/tabler_outline.json`` from upstream.

A one-shot tool, not part of the wheel and not run by CI. It exists so the shipped
icon data is reproducible: third-party geometry that nobody can rebuild is data
whose provenance is a claim rather than a fact.

    python3 scripts/build_tabler_icons.py --upstream tabler-icons-<commit>.tar.gz

``--upstream`` is the release tarball named by the shipped pin, or the unpacked
``icons/outline`` directory. Give it the tarball: the pin in the data file records
the commit and that archive's SHA-256, and only the tarball lets a rerun hash what
it was handed and check it against what the file claims. A directory has no hash to
take, so a rerun from one carries the old pin forward without having proved it, and
says so. Either way the input is one ``<name>.svg`` per icon, each with a comment
header carrying ``tags:`` and ``category:``. Roughly five thousand of them ship; a
deck needs about a fifth, so most of this file is the policy for which fifth.

Re-pinning is deliberate. A commit or hash that differs from the shipped one stops
the run until ``--repin`` says the move to a new upstream is intended, and a run
that would leave the file with no commit or no hash at all is refused outright --
provenance that was once a fact must not quietly become a claim again.

Two things it will not do. It never rewrites an icon that is already in the data
file -- those bytes came from an earlier converter and re-rounding them would show
up as a diff with no change in it -- and it never drops one, because a name that
stops resolving breaks a deck that used it.

The conversion is the other half. Upstream paths use the whole SVG grammar
(``a`` alone appears in four thousand of them); the shipped format is absolute
``M``/``L``/``C``/``Z`` so that a consumer can draw it without a path parser.
Arcs become cubic segments of at most a quarter turn, which is accurate to about
3e-4 of the radius -- an order of magnitude under the flattening every consumer
already does.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import re
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = REPO_ROOT / "raven/ppt/services/assets/data/tabler_outline.json"
GRID = 24.0
PRECISION = 3
# Where the tarball for a commit is fetched from, so the pin names a URL a reader
# can `curl | sha256sum` rather than a bare hash they have to take on trust.
ARCHIVE_URL = "https://codeload.github.com/tabler/tabler-icons/tar.gz/{commit}"
ARCHIVE_MEMBERS = "icons/outline/"
UPSTREAM_PACKAGE = "tabler-icons"
UPSTREAM_VARIANT = "outline"

# ---------------------------------------------------------------------------
# SVG path data -> absolute M/L/C/Z
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"[+-]?(?:\d*\.\d+|\d+\.?)(?:[eE][+-]?\d+)?")
_COMMAND = re.compile(r"[MmZzLlHhVvCcSsQqTtAa]")
_ARGC = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


class PathError(ValueError):
    """A `d` attribute this converter cannot read."""


class ProvenanceError(ValueError):
    """The run would leave the data file claiming an upstream it has not proved."""


def tokenize(data: str) -> list[tuple[str, list[float]]]:
    """(op, numbers) once per command, with repeated argument groups expanded."""
    index, end = 0, len(data)
    groups: list[tuple[str, list[float]]] = []
    op: str | None = None
    while index < end:
        char = data[index]
        if char in ", \t\r\n":
            index += 1
            continue
        if _COMMAND.match(char):
            op = char
            index += 1
            if op in "Zz":
                groups.append(("Z", []))
                op = None
            continue
        if op is None:
            raise PathError(f"number before any command in {data!r}")
        args: list[float] = []
        while len(args) < _ARGC[op.upper()]:
            while index < end and data[index] in ", \t\r\n":
                index += 1
            # An arc's two flags are single digits and may run straight into the
            # next number ("a5 5 0 1 1 7.5-6.5"), so they are read one char wide.
            if op in "Aa" and len(args) in (3, 4):
                if index >= end or data[index] not in "01":
                    raise PathError(f"arc flag must be 0 or 1 in {data!r}")
                args.append(float(data[index]))
                index += 1
                continue
            match = _NUMBER.match(data, index)
            if not match:
                raise PathError(f"expected a number at offset {index} in {data!r}")
            args.append(float(match.group()))
            index = match.end()
        groups.append((op, args))
        # A second argument group after a move continues as a line, per the grammar.
        if op == "M":
            op = "L"
        elif op == "m":
            op = "l"
    return groups


def arc_to_cubics(x1, y1, rx, ry, phi_deg, large, sweep, x2, y2):
    """An endpoint-parameterised arc as cubic segments of at most a quarter turn."""
    if x1 == x2 and y1 == y2:
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        return [((x1, y1), (x2, y2), (x2, y2))]
    phi = math.radians(phi_deg % 360.0)
    cos_p, sin_p = math.cos(phi), math.sin(phi)

    half_dx, half_dy = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cos_p * half_dx + sin_p * half_dy
    y1p = -sin_p * half_dx + cos_p * half_dy

    # Radii too small to reach both endpoints are scaled up, per F.6.6.
    oversize = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if oversize > 1.0:
        rx *= math.sqrt(oversize)
        ry *= math.sqrt(oversize)

    numerator = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    denominator = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    factor = math.sqrt(max(numerator, 0.0) / denominator) if denominator else 0.0
    if large == sweep:
        factor = -factor
    cxp, cyp = factor * rx * y1p / ry, -factor * ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2.0
    cy = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2.0

    ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
    vx, vy = (-x1p - cxp) / rx, (-y1p - cyp) / ry
    theta = math.atan2(uy, ux)
    delta = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi

    # The 1e-6 slack keeps an arc that is a quarter turn plus float noise from
    # being cut in two: an exact 90 degree corner is the commonest arc in the set.
    steps = max(1, math.ceil(abs(delta) / (math.pi / 2) - 1e-6))
    step = delta / steps
    alpha = 4.0 / 3.0 * math.tan(step / 4.0)

    def on_ellipse(angle):
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        return (cx + rx * cos_a * cos_p - ry * sin_a * sin_p, cy + rx * cos_a * sin_p + ry * sin_a * cos_p)

    def tangent(angle):
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        return (-rx * sin_a * cos_p - ry * cos_a * sin_p, -rx * sin_a * sin_p + ry * cos_a * cos_p)

    segments, px, py = [], x1, y1
    for step_index in range(steps):
        next_theta = theta + step
        ex, ey = on_ellipse(next_theta) if step_index < steps - 1 else (x2, y2)
        t0x, t0y = tangent(theta)
        t1x, t1y = tangent(next_theta)
        segments.append(((px + alpha * t0x, py + alpha * t0y), (ex - alpha * t1x, ey - alpha * t1y), (ex, ey)))
        theta, px, py = next_theta, ex, ey
    return segments


def _quadratic(x0, y0, qx, qy, x, y):
    """A quadratic raised to a cubic exactly -- no approximation involved."""
    return (
        x0 + 2.0 / 3.0 * (qx - x0),
        y0 + 2.0 / 3.0 * (qy - y0),
        x + 2.0 / 3.0 * (qx - x),
        y + 2.0 / 3.0 * (qy - y),
        x,
        y,
    )


def to_commands(data: str) -> list[tuple[str, tuple[float, ...]]]:
    """One `d` attribute as absolute M/L/C/Z commands."""
    out: list[tuple[str, tuple[float, ...]]] = []
    cx = cy = sx = sy = 0.0
    started = False
    prev_cubic = prev_quad = None
    for op, args in tokenize(data):
        upper, relative = op.upper(), op.islower()
        if upper == "M":
            x, y = args
            if relative:
                x, y = cx + x, cy + y
            out.append(("M", (x, y)))
            cx, cy = sx, sy = x, y
            started, prev_cubic, prev_quad = True, None, None
            continue
        if not started:
            raise PathError(f"path does not open with a move: {data!r}")
        if upper == "Z":
            out.append(("Z", ()))
            cx, cy, prev_cubic, prev_quad = sx, sy, None, None
        elif upper == "L":
            x, y = args
            if relative:
                x, y = cx + x, cy + y
            out.append(("L", (x, y)))
            cx, cy, prev_cubic, prev_quad = x, y, None, None
        elif upper == "H":
            x = cx + args[0] if relative else args[0]
            out.append(("L", (x, cy)))
            cx, prev_cubic, prev_quad = x, None, None
        elif upper == "V":
            y = cy + args[0] if relative else args[0]
            out.append(("L", (cx, y)))
            cy, prev_cubic, prev_quad = y, None, None
        elif upper == "C":
            x1, y1, x2, y2, x, y = args
            if relative:
                x1, y1, x2, y2, x, y = cx + x1, cy + y1, cx + x2, cy + y2, cx + x, cy + y
            out.append(("C", (x1, y1, x2, y2, x, y)))
            cx, cy, prev_cubic, prev_quad = x, y, (x2, y2), None
        elif upper == "S":
            x2, y2, x, y = args
            if relative:
                x2, y2, x, y = cx + x2, cy + y2, cx + x, cy + y
            x1, y1 = (2 * cx - prev_cubic[0], 2 * cy - prev_cubic[1]) if prev_cubic else (cx, cy)
            out.append(("C", (x1, y1, x2, y2, x, y)))
            cx, cy, prev_cubic, prev_quad = x, y, (x2, y2), None
        elif upper == "Q":
            qx, qy, x, y = args
            if relative:
                qx, qy, x, y = cx + qx, cy + qy, cx + x, cy + y
            out.append(("C", _quadratic(cx, cy, qx, qy, x, y)))
            cx, cy, prev_quad, prev_cubic = x, y, (qx, qy), None
        elif upper == "T":
            x, y = args
            if relative:
                x, y = cx + x, cy + y
            qx, qy = (2 * cx - prev_quad[0], 2 * cy - prev_quad[1]) if prev_quad else (cx, cy)
            out.append(("C", _quadratic(cx, cy, qx, qy, x, y)))
            cx, cy, prev_quad, prev_cubic = x, y, (qx, qy), None
        elif upper == "A":
            rx, ry, rotation, large, sweep, x, y = args
            if relative:
                x, y = cx + x, cy + y
            for control_1, control_2, end in arc_to_cubics(cx, cy, rx, ry, rotation, int(large), int(sweep), x, y):
                if control_1 == control_2 == end:
                    out.append(("L", end))
                else:
                    out.append(("C", (*control_1, *control_2, *end)))
            cx, cy, prev_cubic, prev_quad = x, y, None, None
        else:
            raise PathError(f"unsupported command {op!r}")
    return out


# ---------------------------------------------------------------------------
# Which of the five thousand ship
# ---------------------------------------------------------------------------

# A glyph that is a letter, a digit or a spelled-out number is a typeface, not an icon.
LETTER_OR_DIGIT = re.compile(r"(^|-)(letter|number)-|^(alphabet|math-symbols)")

# Categories that belong to somebody's kitchen, wardrobe or weekend rather than to a
# deck about a business or a system.
OFF_TOPIC_CATEGORIES = {
    "Food",
    "Sport",
    "Mood",
    "Games",
    "Laundry",
    "Gender",
    "Animals",
    "Gestures",
    "Numbers",
    "Zodiac",
    "Letters",
}

# The same subjects, reached through the tag header, for icons filed elsewhere.
OFF_TOPIC_TAGS = {
    "christmas",
    "halloween",
    "santa",
    "snowman",
    "easter",
    "valentine",
    "party",
    "confetti",
    "carnival",
    "circus",
    "clown",
    "funfair",
    "amusement",
    "adrenaline",
    "rollercoaster",
    "religion",
    "prayer",
    "church",
    "catholic",
    "jezus",
    "buddhism",
    "judaism",
    "islam",
    "hinduism",
    "faith",
    "worship",
    "spiritual",
    "cosmetics",
    "perfume",
    "makeup",
    "barber",
    "grooming",
    "nappy",
    "diaper",
    "clothing",
    "clothes",
    "apparel",
    "footwear",
    "sandals",
    "knit",
    "lingerie",
    "furniture",
    "bedding",
    "cushion",
    "mattress",
    "upholstery",
    "toilet",
    "bathroom",
    "poop",
    "laundry",
    "marijuana",
    "cannabis",
    "cigarette",
    "smoking",
    "bong",
    "cocktail",
    "beer",
    "whisky",
    "coffin",
    "grave",
    "cementry",
    "horror",
    "scary",
    "funeral",
    "toy",
    "playground",
    "monkeybar",
    "witch",
    "extraterrestrial",
    "ufo",
}

# Enumerated series: one idea repeated with a different digit, signal level or file
# extension stamped on it. Tags cannot separate `percentage-10` from `percentage-90`,
# because they are identical, so the family is named rather than scored.
SERIES = re.compile(
    r"^(percentage|time-duration|clock-hour|hours|antenna-bars|cell-signal|wifi|square-f)-?\d+$"
    r"|^clock-(12|24)$"
    r"|^rewind-(forward|backward)-\d+$"
    r"|^rating-\d+-plus$"
    r"|^signal-(\dg|e|g|h|lte)$"
    r"|^(exposure|crop|multiplier|tallymark)-"
    r"|^badge-(\dk|3d|ad|ar|cc|hd|sd|tm|vo|vr|wc)$"
    r"|^file-type-"
    r"|^math-(sin|cos|tg|ctg|sec|pi-divide|1-divide|x-|y-|equal-)"
    r"|^(coin|receipt|transaction|tip-jar|tax|file)-(euro|pound|yen|yuan|rupee|taka|monero|dollar|bitcoin)$"
    r"|^currency-"
    r"|^http-"
    r"|^logic-"
    r"|^topology-"
    r"|^creative-commons"
    r"|^(no-copyright|no-creative-commons|no-derivatives|free-rights|premium-rights)$"
    r"|^(align-box|border|ease|inner-shadow|keyframe|transform-point|text-regex|float|baseline-density)-"
    r"|^(join|cap)-(bevel|round|straight|projecting|rounded)$"
    r"|^h-[1-6]$"
    r"|^sort-(0-9|9-0|a-z|z-a|(ascending|descending)-(letters|numbers|shapes|small-big))$"
    r"|^layout-.*-inactive$"
    r"|^viewport-(short|tall|narrow|wide)$"
    r"|^(skew|spacing)-"
    r"|^language-(hiragana|katakana)$"
    # Arrow micro-variants: a road layout or a tail shape, not a direction.
    r"|^arrow-(autofit|rotary|roundabout|wave|bear|elbow|sharp-turn|badge|capsule|bounce|zig-zag"
    r"|curve|iteration|guide|ramp|fork-triple|merge-alt|big-\w+-lines?|\w+-rhombus|\w+-tail"
    r"|\w+-bar$|bar-to-|\w+-from-arc|\w+-to-arc)"
)

# The head of a family the rules above would otherwise erase whole. A deck about money
# needs a euro sign even though the other sixty currencies are noise, and one about an
# API needs GET and POST even though the other nine verbs are never drawn.
FAMILY_HEADS = {
    "currency",
    "currency-euro",
    "currency-pound",
    "currency-yen",
    "currency-yuan",
    "currency-renminbi",
    "currency-rupee",
    "currency-won",
    "currency-real",
    "currency-lira",
    "currency-shekel",
    "currency-ruble",
    "currency-dirham",
    "currency-riyal",
    "currency-ethereum",
    "coin-euro",
    "coin-pound",
    "coin-yen",
    "coin-yuan",
    "coin-rupee",
    "receipt-euro",
    "transaction-dollar",
    "transaction-euro",
    "tax",
    "tip-jar",
    "file-type-pdf",
    "file-type-csv",
    "file-type-doc",
    "file-type-xls",
    "file-type-ppt",
    "file-type-zip",
    "file-type-sql",
    "file-type-svg",
    "file-type-png",
    "file-type-js",
    "file-excel",
    "file-word",
    "http-get",
    "http-post",
    "http-put",
    "http-delete",
    "logic-and",
    "logic-or",
    "logic-not",
    "logic-xor",
    "topology-star",
    "topology-ring",
    "topology-bus",
    "topology-full",
    "clock-12",
    "clock-24",
    "percentage-25",
    "percentage-50",
    "percentage-75",
    "percentage-100",
    "math-avg",
    "math-max",
    "math-min",
    "math-function",
    "math-greater",
    "math-lower",
    "math-integral",
    "math-pi",
    "badge",
    "badges",
    "crop",
    "exposure",
    "border-radius",
    "border-all",
    "keyframe",
    "keyframes",
    "sort-ascending-letters",
    "sort-descending-letters",
    "signal-5g",
    "cell-signal-4",
    "antenna-bars-5",
}

# Suffixes that weld a badge onto a base glyph. `file-plus` says nothing on a slide
# that `file` beside a `plus` does not, and there are twelve hundred of them.
DECORATION = {
    "plus",
    "minus",
    "x",
    "check",
    "cancel",
    "exclamation",
    "question",
    "search",
    "share",
    "star",
    "heart",
    "bolt",
    "code",
    "cog",
    "dollar",
    "pin",
    "pause",
    "spark",
    "ai",
    "discount",
    "bitcoin",
    "edit",
    "scan",
    "asterisk",
    "filled",
    "play",
    "stop",
    "record",
    "hexagon",
    "circle",
    "square",
    "dashed",
    "double",
    "rounded",
    "arc",
    "y",
    "up",
    "down",
    "left",
    "right",
    "top",
    "bottom",
    "vertical",
    "horizontal",
    "center",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
}
DIRECTIONS = {"up", "down", "left", "right", "top", "bottom", "vertical", "horizontal"}
# ...except on these bases, where the direction is the whole icon.
DIRECTIONAL_BASES = {
    "arrow",
    "arrows",
    "caret",
    "chevron",
    "chevrons",
    "corner",
    "trending",
    "sort",
    "transfer",
    "fold",
    "player",
    "switch",
    "exchange",
    "rotate",
    "stack",
    "layout",
}

# Two hand-set corrections on top of the rules, kept short on purpose. KEEP is the
# vocabulary a rule cut for a reason that does not apply to it -- a per-category cap
# it happened to fall outside of. DROP is what survived every rule and still has no
# business on a slide about a business.
KEEP = {
    "arrow-back",
    "arrow-forward",
    "arrow-narrow-down",
    "arrow-down-left",
    "arrow-down-right",
    "arrows-sort",
    "arrows-maximize",
    "arrows-move",
    "arrows-horizontal",
    "arrows-vertical",
    "caret-down",
    "caret-left",
    "caret-up",
    "chevron-down",
    "chevron-left",
    "chevrons-down",
    "chevrons-left",
    "circle-arrow-down",
    "circle-chevron-down",
    "square-arrow-down",
    "rotate",
    "rotate-clockwise",
    "rotate-360",
    "select",
    "selector",
    "refresh-alert",
    "switch-horizontal",
    "switch-vertical",
    "u-turn-right",
    "click",
    "ban",
    "forbid",
    "restore",
    "details",
    "filters",
    "eye-closed",
    "door-enter",
    "door-exit",
    "elevator",
    "lifebuoy",
    "wand",
    "weight",
    "point",
    "deselect",
    "clear-all",
    "outbound",
    "calendar-week",
    "calendar-month",
    "calendar-clock",
    "calendar-repeat",
    "calendar-user",
    "heading",
    "italic",
    "underline",
    "language",
    "markdown",
    "sort-ascending",
    "sort-descending",
    "blockquote",
    "separator",
    "strikethrough",
    "line-height",
    "eraser",
    "cursor-text",
    "text-recognition",
    "book",
    "notebook",
    "contract",
    "invoice",
    "license",
    "ticket",
    "paperclip",
    "folder-open",
    "folder-root",
    "folder-symlink",
    "file-zip",
    "bulb",
    "frame",
    "hammer",
    "color-picker",
    "background",
    "magnet",
    "sparkle",
    "placeholder",
    "grid-3x3",
    "traffic-lights",
    "escalator",
    "route-alt-left",
    "route-alt-right",
    "air-traffic-control",
    "gas-station",
    "helicopter",
    "charging-pile",
    "battery-automotive",
    "car-suv",
    "hospital",
    "accessible",
    "braille",
    "deaf",
    "eyeglass",
    "square-root",
    "decimal",
    "omega",
    "equal-not",
    "trademark",
    "option",
    "biohazard",
    "screenshot",
    "panorama-horizontal",
    "hdr",
    "mountain",
    "tornado",
    "flood",
    "temperature-celsius",
    "hexagon-3d",
    "rectangular-prism",
    "squares-selected",
    "devices",
    "device-tv",
    "device-screen",
    "browser-maximize",
    "credit-card-refund",
    "credit-card-hand",
    "car-garage",
}
DROP = {
    "guitar-pick",
    "pokeball",
    "xbox-a",
    "xbox-b",
    "xbox-x",
    "xbox-y",
    "playstation-square",
    "playstation-triangle",
    "playstation-x",
    "playstation-circle",
    "device-nintendo",
    "device-gamepad",
    "fridge",
    "blender",
    "vinyl",
    "clubs",
    "spade",
    "diamonds",
    "hearts",
    "rosette",
    "diabolo",
    "masks-theater",
    "theater",
    "volcano",
    "beach",
    "campfire",
    "tent",
    "jacket",
    "shirt",
    "sunglasses",
    "baby-bottle",
    "baby-carriage",
    "massage",
    "torii",
    "mars",
    "venus",
    "explicit",
    "jetpack",
    "steam",
    "trowel",
    "wood",
    "spy",
    "zeppelin",
    "parachute",
    "dental-broken",
    "physotherapist",
    "tallymarks",
    "bell-z",
    "haze-moon",
    "luggage",
    "umbrella",
    "umbrella-closed",
    "acorn",
    "butterfly",
    "cactus",
    "feather",
    "flower",
    "leaf-maple",
    "twig",
    "iceberg",
    "cherry",
    "skull",
    "poo",
    "xxx",
    "zzz",
    "moped",
    "motorbike",
    "scooter",
    "scooter-electric",
    "caravan",
    "sailboat",
    "speedboat",
    "submarine",
    "jetski",
    "wiper",
    "wiper-wash",
    "garden-cart",
    "bulldozer",
    "tir",
    "track",
    "rv-truck",
    "car-door",
    "car-crash",
    "car-turbine",
    "car-crane",
    "automatic-gearbox",
    "steering-wheel",
    "trolley",
    "backpack",
    "gift-card",
    "e-passport",
    "cookie-man",
    "mickey",
    "moustache",
    "ladle",
    "chair-director",
    "barrel",
    "blade",
    "bath",
    "plunger",
    "razor",
    "razor-electric",
    "smoking",
    "smoking-no",
    "pray",
    "gymnastics",
    "snowboarding",
    "fidget-spinner",
    "lego",
    "picnic-table",
    "prison",
    "north-star",
    "crystal-ball",
    "alien",
    "balloon",
    "bomb",
    "boom",
    "matchstick",
    "lighter",
    "medicine-syrup",
    "footsteps",
    "hand-sanitizer",
    "face-mask",
    "empathize",
    "heart-broken",
    "eye-table",
    "eye-dotted",
    "body-scan",
    "diaper",
    "flip-flops",
    "sock",
    "tie",
    "hanger",
    "clothes-rack",
    "shoe",
    "shirt-sport",
    "armchair",
    "rocking-chair",
    "pillow",
    "toilet-paper",
    "sofa",
    "bed",
    "bed-flat",
    "man",
    "woman",
    "friends",
    "old",
    "vip",
    "sos",
    "metronome",
}

# How many icons of each category the set holds in total, shipped ones included.
# Set from the shape of what survives the rules: `System` is the general interface
# vocabulary and carries the most; the long tails of arrows and drawing-tool chrome
# carry the least. Counting the shipped ones is what makes a rerun a no-op -- with
# a cap over the candidates alone, everything a previous run refused would fit.
CAPS = {
    "Arrows": 85,
    "Badges": 3,
    "Buildings": 45,
    "Charts": 28,
    "Communication": 24,
    "Computers": 15,
    "Currencies": 18,
    "Database": 23,
    "Design": 100,
    "Development": 49,
    "Devices": 70,
    "Document": 88,
    "E-commerce": 44,
    "Electrical": 12,
    "Extensions": 13,
    "Health": 36,
    "Logic": 4,
    "Map": 60,
    "Math": 34,
    "Media": 37,
    "Nature": 13,
    "Photography": 15,
    "Shapes": 47,
    "Symbols": 14,
    "System": 212,
    "Text": 50,
    "Vehicles": 29,
    "Version control": 11,
    "Weather": 24,
}

# The six subjects a deck of this kind argues about, in the vocabulary the tag headers
# use. Weighted alongside what the already-curated icons are tagged with, so the
# ranking is learned from the set somebody already chose for this job.
SUBJECTS = {
    "business",
    "finance",
    "money",
    "commerce",
    "payment",
    "invoice",
    "budget",
    "market",
    "technology",
    "software",
    "hardware",
    "programming",
    "code",
    "network",
    "server",
    "cloud",
    "data",
    "database",
    "analytics",
    "statistics",
    "chart",
    "graph",
    "report",
    "metric",
    "measure",
    "process",
    "workflow",
    "flow",
    "step",
    "stage",
    "pipeline",
    "automation",
    "operation",
    "organization",
    "team",
    "people",
    "user",
    "management",
    "structure",
    "hierarchy",
    "role",
    "risk",
    "security",
    "alert",
    "warning",
    "protect",
    "secure",
    "compliance",
    "audit",
    "error",
    "time",
    "schedule",
    "plan",
    "goal",
    "target",
    "growth",
    "increase",
    "decrease",
    "compare",
    "document",
    "communication",
    "search",
    "share",
    "connection",
    "storage",
    "transport",
    "logistics",
}


# Sixteen names in the data file are ours, not upstream's -- a friendlier spelling for
# a glyph an author reaches for by meaning. They have no tag header to read, and they
# are the most-used names in the set, so their keywords are written here by hand.
ALIAS_CATEGORIES = {
    "boxes": "Development",
    "chart": "Charts",
    "check_circle": "System",
    "document": "Document",
    "factory": "Buildings",
    "image": "Media",
    "info": "System",
    "layers": "Design",
    "lightbulb": "Design",
    "monitor": "Devices",
    "play": "Media",
    "teacher": "Buildings",
    "trend_up": "Arrows",
    "warning": "System",
    "workflow": "System",
    "x_circle": "System",
}
ALIAS_KEYWORDS = {
    "boxes": "development packages inventory bundle modules crates units",
    "chart": "charts analytics data statistics graph visualization report metric measure",
    "check_circle": "system done complete confirm approve success tick accept pass ok",
    "document": "documents file paper page text report record",
    "factory": "buildings manufacturing plant industry production industrial works",
    "image": "media picture photo graphic illustration visual",
    "info": "system information detail note help about",
    "layers": "design stack levels tiers layer overlay depth",
    "lightbulb": "idea insight innovation invention bulb light think creative",
    "monitor": "devices screen display desktop computer",
    "play": "media player start run begin playback",
    "teacher": "people education training instructor lecture classroom school teaching",
    "trend_up": "arrows growth increase rise improvement upward trending trend",
    "warning": "system alert caution danger risk attention triangle exclamation notice",
    "workflow": "process flow pipeline steps sequence orchestration automation",
    "x_circle": "system close cancel remove reject error cross deny fail",
}

_HEADER = re.compile(r"<!--(.*?)-->", re.S)
_TAGS = re.compile(r"tags:\s*\[(.*?)\]", re.S)
_CATEGORY = re.compile(r"category:\s*(.+)")
_PATH_D = re.compile(r'<path\s[^>]*?\bd="([^"]+)"')


def read_icon(text: str) -> dict:
    """One `.svg` file's tag header and path data, which is all of it this reads."""
    header = _HEADER.search(text)
    tags: list[str] = []
    category = ""
    if header:
        found = _TAGS.search(header.group(1))
        if found:
            tags = [part.strip().strip("\"'") for part in found.group(1).split(",") if part.strip()]
        named = _CATEGORY.search(header.group(1))
        if named:
            category = named.group(1).strip().strip('"')
    return {"tags": tags, "category": category, "paths": _PATH_D.findall(text)}


def read_upstream(directory: Path) -> dict[str, dict]:
    """name -> {tags, category, paths} for every `.svg` in the upstream directory."""
    return {svg.stem: read_icon(svg.read_text(encoding="utf-8")) for svg in sorted(directory.glob("*.svg"))}


def read_archive(archive: Path) -> tuple[dict[str, dict], str]:
    """The same reading out of the release tarball, and the tarball's own SHA-256.

    The hash is why this path exists. A directory on disk is whatever somebody left
    there; the tarball is the artifact the pin names, so hashing the bytes as they
    are read is the one moment the script can tell whether the tree it is converting
    is the tree the data file claims to have come from.

    Members are matched on `icons/outline/` appearing anywhere in the path, because
    the archive GitHub builds wraps the tree in a `<repo>-<commit>/` directory, and
    matching the suffix rather than the whole path also keeps `icons/filled/` out.
    """
    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    icons: dict[str, dict] = {}
    with tarfile.open(archive, "r:*") as bundle:
        for member in bundle:
            if not member.isfile() or ARCHIVE_MEMBERS not in member.name or not member.name.endswith(".svg"):
                continue
            stream = bundle.extractfile(member)
            if stream is not None:
                icons[Path(member.name).stem] = read_icon(stream.read().decode("utf-8"))
    return dict(sorted(icons.items())), digest.hexdigest()


def pin(shipped: dict, measured: str | None, args: argparse.Namespace) -> dict:
    """The `upstream` header this run is allowed to write, or a refusal.

    Three refusals, and each is a way the file could end up asserting something no
    one checked. A hash that does not match the tarball just read means the input is
    not the artifact named -- the loudest case, because everything downstream would
    still look right. A commit or hash that differs from the shipped pin without
    `--repin` means the upstream moved and nobody said so. And a run that would leave
    the header with no commit or no hash at all is refused even though it is what the
    file used to look like: provenance is allowed to be established and allowed to be
    moved, but not to be dropped.

    A rerun from a directory has nothing to measure, so it carries the shipped pin
    forward unchanged. That is not a check and the caller is told as much.
    """
    at = args.commit or shipped.get("commit")
    release = args.version or shipped.get("version")
    hashed = args.sha256 or measured or shipped.get("sha256")
    if measured and hashed != measured:
        raise ProvenanceError(f"--sha256 {hashed} is not the tarball just read ({measured})")
    if not at or not hashed or not release:
        raise ProvenanceError("the header would carry no version, commit or hash; pass --version --commit --sha256")
    moved = [
        f"{field}: shipped {shipped[field]}, this run {value}"
        for field, value in (("version", release), ("commit", at), ("sha256", hashed))
        if shipped.get(field) and shipped[field] != value
    ]
    if moved and not args.repin:
        raise ProvenanceError("the upstream moved; pass --repin to accept it\n  " + "\n  ".join(moved))
    return {
        "package": UPSTREAM_PACKAGE,
        "version": release,
        "variant": UPSTREAM_VARIANT,
        "commit": at,
        "files": f"{ARCHIVE_MEMBERS}*.svg",
        "archive": ARCHIVE_URL.format(commit=at),
        "sha256": hashed,
    }


def survivors(upstream: dict[str, dict], shipped: set[str]) -> tuple[dict[str, dict], collections.Counter]:
    """Everything that clears the mechanical rules, and a tally of what did not."""
    refused: collections.Counter = collections.Counter()
    pool: dict[str, dict] = {}
    for name, icon in upstream.items():
        if name.replace("-", "_") in shipped:
            # First, so a rerun against a grown data file chooses the same set:
            # nothing already in the file is a candidate, whatever else it is.
            refused["already shipped"] += 1
        elif name in DROP:
            refused["off-topic by name"] += 1
        elif name in KEEP or name in FAMILY_HEADS:
            pool[name] = icon
        elif name.startswith("brand-"):
            refused["brand mark"] += 1
        elif name.endswith("-off"):
            refused["crossed-out variant"] += 1
        elif LETTER_OR_DIGIT.search(name):
            refused["letter or digit glyph"] += 1
        elif icon["category"] in OFF_TOPIC_CATEGORIES:
            refused["off-topic category"] += 1
        elif any(tag in OFF_TOPIC_TAGS for tag in icon["tags"]):
            refused["off-topic subject"] += 1
        elif SERIES.search(name):
            refused["enumerated series"] += 1
        else:
            pool[name] = icon
    trimmed: dict[str, dict] = {}
    for name, icon in pool.items():
        tail = name.rsplit("-", 1)[-1] if "-" in name else ""
        base = name.rsplit("-", 1)[0] if "-" in name else name
        directional = tail in DIRECTIONS and base.split("-")[0] in DIRECTIONAL_BASES
        decorated = tail in DECORATION and not directional and name not in KEEP
        if decorated and (base in upstream or base.replace("-", "_") in shipped):
            refused["decorated variant"] += 1
            continue
        trimmed[name] = icon
    return trimmed, refused


def relevance(upstream: dict[str, dict], shipped: set[str]):
    """Score one icon by how much its tags look like the ones already chosen."""
    frequency = collections.Counter(tag for icon in upstream.values() for tag in icon["tags"])
    total = len(upstream)
    profile: collections.Counter = collections.Counter()
    for name in shipped:
        icon = upstream.get(name.replace("_", "-"))
        if icon:
            profile.update(icon["tags"])

    def score(name: str) -> float:
        tags = upstream[name]["tags"]
        if not tags:
            return 0.0
        value = 0.0
        for tag in tags:
            rarity = max(math.log(total / (1 + frequency[tag])), 0.0)
            value += (profile[tag] + (6 if tag in SUBJECTS else 0)) * rarity
        # A base glyph beats a compound of it: `folder` says more on a slide than
        # `folder-symlink` and costs the same bytes.
        return value / math.sqrt(len(tags)) + max(0, 3 - name.count("-")) * 4.0

    return score


def shelf(upstream: dict[str, dict], slug: str) -> str:
    """The category an icon is filed under, upstream's or ours for the aliases."""
    icon = upstream.get(slug.replace("_", "-"))
    return icon["category"] if icon else ALIAS_CATEGORIES.get(slug, "")


def select(upstream: dict[str, dict], shipped: set[str]) -> tuple[list[str], collections.Counter]:
    pool, refused = survivors(upstream, shipped)
    score = relevance(upstream, shipped)
    taken: list[str] = []
    per_category: collections.Counter = collections.Counter(shelf(upstream, slug) for slug in shipped)
    for name in sorted(pool, key=lambda n: (-score(n), n)):
        category = pool[name]["category"]
        if name not in KEEP and per_category[category] >= CAPS.get(category, 0):
            refused["over the category cap"] += 1
            continue
        per_category[category] += 1
        taken.append(name)
    return sorted(taken), refused


# ---------------------------------------------------------------------------
# Emitting the data file
# ---------------------------------------------------------------------------


def _round(value: float) -> float | int:
    rounded = round(value, PRECISION)
    return int(rounded) if rounded == int(rounded) else rounded


def _extent(commands: list) -> float:
    """The longer side of one path's bounding box, on the 24 grid."""
    xs = [value for _, coords in commands for value in coords[0::2]]
    ys = [value for _, coords in commands for value in coords[1::2]]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def geometry(name: str, paths: list[str]) -> list:
    """One icon's `<path>` elements as the shipped ['path', commands] entries.

    Refuses an icon that is nothing but round-cap dots. Upstream draws a dot as a
    hundredth-of-a-unit stub and lets `stroke-linecap="round"` make it a disc,
    which a consumer stroking with butt caps -- as python-pptx freeforms do --
    would draw as a smudge. `add_icon` no longer does: it recognises a stub and
    paints a pen-wide square there, so `circle-dotted` and `line-dotted` would
    now draw. Re-admitting them is a rerun of this script against upstream and a
    change to the shipped count, so the refusal stands until somebody wants them.
    """
    entries = []
    for data in paths:
        commands = [[op, [_round(value) for value in coords]] for op, coords in to_commands(data)]
        if not commands:
            raise PathError(f"{name}: empty path")
        if commands[0][0] != "M":
            raise PathError(f"{name}: path does not open with a move")
        for op, coords in commands:
            if any(not (0.0 <= value <= GRID) for value in coords):
                raise PathError(f"{name}: {op} leaves the grid at {coords}")
        entries.append(["path", commands])
    if not entries:
        raise PathError(f"{name}: no paths")
    if max(_extent(commands) for _, commands in entries) < 0.5:
        raise PathError(f"{name}: every path is a round-cap dot, so nothing would draw")
    return entries


def keywords(icon: dict, slug: str) -> str:
    """The tag header as one searchable string, minus what the name already says."""
    words = [icon["category"].lower().replace(" ", "-")] + [tag.lower() for tag in icon["tags"]]
    spelled = set(slug.split("_"))
    out: list[str] = []
    for word in words:
        cleaned = re.sub(r"[^a-z0-9-]+", " ", word).strip()
        for part in cleaned.split():
            if part and part not in spelled and part not in out:
                out.append(part)
    return " ".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--upstream", required=True, type=Path, help="the release tarball, or an `icons/outline` directory"
    )
    parser.add_argument("--version", help="the upstream release this tree is, when re-pinning")
    parser.add_argument("--commit", help="the upstream commit this tree is at, when re-pinning")
    parser.add_argument("--sha256", help="the tarball's SHA-256, when the input is a directory and the pin moves")
    parser.add_argument(
        "--repin", action="store_true", help="accept a version, commit or hash the shipped pin does not have"
    )
    parser.add_argument("--out", type=Path, default=DATA_FILE)
    parser.add_argument("--dry-run", action="store_true", help="report the selection without writing")
    args = parser.parse_args(argv)

    payload = json.loads(args.out.read_text(encoding="utf-8"))
    shipped: dict[str, list] = payload["icons"]
    if args.upstream.is_dir():
        upstream, measured = read_upstream(args.upstream), None
    else:
        upstream, measured = read_archive(args.upstream)
    if not upstream:
        print(f"no .svg files in {args.upstream}", file=sys.stderr)
        return 2
    try:
        provenance = pin(payload.get("upstream") or {}, measured, args)
    except ProvenanceError as exc:
        print(exc, file=sys.stderr)
        return 2

    chosen, refused = select(upstream, set(shipped))

    icons = dict(shipped)
    index: dict[str, str] = {}
    shelves: dict[str, str] = {}
    undrawable: list[str] = []
    for name in chosen:
        slug = name.replace("-", "_")
        if slug in icons:
            raise SystemExit(f"{name} collides with shipped icon {slug}")
        try:
            icons[slug] = geometry(name, upstream[name]["paths"])
        except PathError as exc:
            undrawable.append(str(exc))
            refused["nothing would draw"] += 1
    # Every icon carries keywords, including the ones already shipped -- a search that
    # only reached the new ones would rank the staples below their own variants.
    for slug in icons:
        icon = upstream.get(slug.replace("_", "-"))
        if icon:
            index[slug] = keywords(icon, slug)
        elif slug in ALIAS_KEYWORDS:
            index[slug] = ALIAS_KEYWORDS[slug]
        else:
            raise SystemExit(f"{slug} has no upstream tags and no hand-written keywords")
        shelves[slug] = shelf(upstream, slug)

    payload["upstream"] = provenance
    payload["icons"] = dict(sorted(icons.items()))
    payload["categories"] = dict(sorted(shelves.items()))
    payload["keywords"] = dict(sorted(index.items()))
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"

    proved = "hash checked" if measured else "carried forward unverified -- rerun from the tarball to check it"
    print(f"pin {provenance['version']} @ {provenance['commit'][:8]} sha256 {provenance['sha256'][:12]} ({proved})")
    print(f"upstream {len(upstream)} -> chose {len(chosen) - len(undrawable)}; shipping {len(payload['icons'])} icons")
    for line in undrawable:
        print(f"  dropped   {line}")
    for reason, count in refused.most_common():
        print(f"  refused {count:5d}  {reason}")
    print(f"  keywords on {len(index)} of {len(payload['icons'])}")
    print(f"  {len(text.encode('utf-8')):,} bytes ({len(text.encode('utf-8')) / len(payload['icons']):.0f} per icon)")
    if args.dry_run:
        return 0
    args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
