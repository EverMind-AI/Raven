"""The Office preset shapes, and where a preset's points actually land.

Two rectangles is what this route could draw before this module: `RECTANGLE` and
`ROUNDED_RECTANGLE`, with `adjustments` touched exactly once in the whole
codebase. So every process, timeline and flow page came out as a row of boxes
with the arrows implied by whitespace, and the traces show what that costs --
an author computing a chevron's notch by hand, rendering, moving it, rendering
again. A primitive that is not there is a primitive the model simulates badly.

Preset geometry is the answer, and it has to be *preset* geometry rather than a
freeform tracing the same outline. `a:prstGeom` keeps the shape editable after
export, keeps its adjustment handles, inherits the theme, and carries its own
text rectangle -- a chevron's is inset past both points, which is the only
reason copy in a chevron does not sit on the arrow. A `custGeom` throws all four
away, and it would cost the measurements too: the geometry checks read a shape's
rectangle as the page states it, and a traced outline states a bounding box.

python-pptx knows the 177 preset names it can write and the default `avLst`
values each carries. It does not carry `gdLst` or `pathLst`, so it cannot answer
the one question that matters when placing one -- where does the point land --
and an author left to answer that arithmetically is the author the traces
caught. The answer is here: the normative guide formulas, evaluated.

Nothing in this module is physical. Widths and heights come in as whatever unit
the caller works in and every distance comes back in that same unit, exactly as
`icons` scales a 24x24 grid into whatever box it is given.
"""

from __future__ import annotations

import difflib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType

_DATA_PACKAGE = "raven.ppt.services.assets"
_GEOMETRY_FILE = "data/preset_shapes.json"
_INTENT_FILE = "data/preset_shape_intents.json"

# ECMA-376 fixes the catalogue at this many presets, and both of the independent
# lists the data was derived from agree on it. A count that drifts means the data
# was regenerated from something else.
PRESET_COUNT = 187

# DrawingML states an adjustment as hundred-thousandths, so 50000 is a half.
# python-pptx's `shape.adjustments` is the same number divided by this, which is
# why the script helper takes 0.5 and this module takes 50000.
ADJUSTMENT_SCALE = 100000.0

# ... and an angle as sixty-thousandths of a degree.
DEGREE = 60000.0
FULL_CIRCLE = 360.0 * DEGREE

# Operator -> how many operands it takes. The whole of the DrawingML formula
# language; a preset that used anything else would not be a preset.
_ARITY = {
    "val": 1,
    "*/": 3,
    "+-": 3,
    "+/": 3,
    "?:": 3,
    "abs": 1,
    "at2": 2,
    "cat2": 3,
    "cos": 2,
    "max": 2,
    "min": 2,
    "mod": 3,
    "pin": 3,
    "sat2": 3,
    "sin": 2,
    "sqrt": 1,
    "tan": 2,
}

# How many parameters a path command carries once its points are flattened.
_COMMAND_ARITY = {"moveTo": 2, "lnTo": 2, "quadBezTo": 4, "cubicBezTo": 6, "arcTo": 4, "close": 0}

# The ten presets that are connectors rather than shapes. python-pptx reaches
# these through `add_connector` and `MSO_CONNECTOR`, not `add_shape`, so they are
# in the catalogue -- their geometry is as real as any other's -- and out of the
# list an author picks a shape from. The constant is asserted against python-pptx's
# own coverage rather than trusted.
CONNECTOR_PRESETS = frozenset(
    {
        "line",
        "straightConnector1",
        "bentConnector2",
        "bentConnector3",
        "bentConnector4",
        "bentConnector5",
        "curvedConnector2",
        "curvedConnector3",
        "curvedConnector4",
        "curvedConnector5",
    }
)

_FRACTION_GUIDE = re.compile(r"^(wd|hd|ssd)([1-9][0-9]*)$")
_ANGLE_GUIDE = re.compile(r"^(?:(\d+))?cd([1-9][0-9]*)$")


class ShapeDataError(RuntimeError):
    """The packaged preset data is missing, unparsable or malformed."""


class ShapeFormulaError(ValueError):
    """A guide formula could not be evaluated against the frame it was given."""


class UnknownPresetError(LookupError):
    """A name is not a preset, with the nearest candidates attached.

    `LookupError` rather than `KeyError` for the same reason `icons` does it:
    `KeyError`'s `str()` wraps the message in quotes, and this message is a
    sentence the author is meant to read and act on.
    """

    def __init__(self, name: str, candidates: list[str], total: int):
        self.name = name
        self.candidates = candidates
        hint = (
            f"; closest: {', '.join(candidates)}"
            if candidates
            else f"; nothing close -- all {total} names are in PRESET_NAMES"
        )
        super().__init__(f"unknown preset shape {name!r}{hint}")


@dataclass(frozen=True)
class PresetGeometry:
    """One preset resolved against one frame, in the caller's own unit.

    `guides` is the interesting half. Every preset states its construction as
    named guides -- a chevron's `x1` is the depth of its notch, a `rightArrow`'s
    `dx1` is the length of its head -- so a caller that needs to place something
    against a preset reads the guide rather than re-deriving it. That is the
    difference between a helper that lays five chevrons out exactly and an author
    nudging them between renders.
    """

    name: str
    width: float
    height: float
    adjustments: Mapping[str, float]
    guides: Mapping[str, float]
    text_rect: tuple[float, float, float, float] | None
    paths: tuple[tuple[tuple[str, tuple[float, ...]], ...], ...]
    connections: tuple[tuple[float, float, float], ...]

    def points(self) -> tuple[tuple[float, float], ...]:
        """Every anchor a straight or Bezier command lands on, in path order.

        An `arcTo` is not an anchor: it states two radii and a sweep, and where
        it leaves you is the sweep's business. Every shape built from lines --
        which is every chevron, block arrow and flowchart symbol -- is fully
        described by what comes back here, and a round one is not.
        """
        found: list[tuple[float, float]] = []
        for path in self.paths:
            for name, parameters in path:
                if name in {"moveTo", "lnTo", "quadBezTo", "cubicBezTo"}:
                    found.append((parameters[-2], parameters[-1]))
        return tuple(found)


def _finite(value: object, label: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ShapeFormulaError(f"{label} must be a number") from exc
    if not math.isfinite(number):
        raise ShapeFormulaError(f"{label} must be finite")
    return number


class _Frame:
    """The built-in guides of one shape-local frame, plus what gets bound to it."""

    def __init__(self, width: float, height: float):
        width = _finite(width, "width")
        height = _finite(height, "height")
        if width < 0 or height < 0:
            raise ShapeFormulaError("a preset frame has no negative side")
        short, long = min(width, height), max(width, height)
        self._builtin: dict[str, float] = {
            "l": 0.0,
            "t": 0.0,
            "r": width,
            "b": height,
            "w": width,
            "h": height,
            "hc": width / 2.0,
            "vc": height / 2.0,
            "ss": short,
            "ls": long,
        }
        self._bound: dict[str, float] = {}

    def bind(self, name: str, value: float) -> float:
        self._bound[name] = value
        return value

    def resolve(self, token: str) -> float:
        token = token.strip()
        try:
            literal = float(token)
        except ValueError:
            pass
        else:
            return _finite(literal, f"literal {token!r}")
        if token in self._bound:
            return self._bound[token]
        if token in self._builtin:
            return self._builtin[token]
        derived = self._derive(token)
        if derived is None:
            raise ShapeFormulaError(f"no such guide: {token!r}")
        self._builtin[token] = derived
        return derived

    def _derive(self, token: str) -> float | None:
        """`wd8`, `ssd6`, `3cd8` -- the guides the standard spells rather than lists."""
        fraction = _FRACTION_GUIDE.fullmatch(token)
        if fraction:
            family, divisor = fraction.groups()
            return self._builtin[{"wd": "w", "hd": "h", "ssd": "ss"}[family]] / int(divisor)
        angle = _ANGLE_GUIDE.fullmatch(token)
        if angle:
            numerator, divisor = angle.groups()
            return FULL_CIRCLE * int(numerator or "1") / int(divisor)
        return None

    def value(self, source: str | float) -> float:
        """A formula, a guide name, or a number already."""
        if not isinstance(source, str):
            return _finite(source, "adjustment")
        parts = source.split()
        if parts and parts[0] in _ARITY:
            return self.evaluate(source)
        if len(parts) == 1:
            return self.resolve(parts[0])
        raise ShapeFormulaError(f"not a guide value: {source!r}")

    def evaluate(self, formula: str) -> float:
        parts = formula.split()
        operator = parts[0]
        arity = _ARITY.get(operator)
        if arity is None:
            raise ShapeFormulaError(f"unsupported guide operator {operator!r} in {formula!r}")
        surplus = parts[arity + 1 :]
        # Three circular-arrow presets carry `+- xH 0 dxB 0`, an inert trailing
        # zero that Apache POI ignores. Accept that one form and nothing else, so
        # a real arity mistake in regenerated data still fails.
        if len(parts) < arity + 1 or (surplus and not (operator == "+-" and set(surplus) == {"0"})):
            raise ShapeFormulaError(f"{operator!r} takes {arity} operands: {formula!r}")
        operands = tuple(self.resolve(token) for token in parts[1 : arity + 1])
        try:
            result = _apply(operator, operands)
        except (ArithmeticError, ValueError) as exc:
            raise ShapeFormulaError(f"cannot evaluate {formula!r}: {exc}") from exc
        return _finite(result, f"result of {formula!r}")


def _apply(operator: str, v: tuple[float, ...]) -> float:
    if operator == "val":
        return v[0]
    if operator == "*/":
        return 0.0 if v[2] == 0 else v[0] * v[1] / v[2]
    if operator == "+-":
        return v[0] + v[1] - v[2]
    if operator == "+/":
        return 0.0 if v[2] == 0 else (v[0] + v[1]) / v[2]
    if operator == "?:":
        return v[1] if v[0] > 0 else v[2]
    if operator == "abs":
        return abs(v[0])
    if operator == "at2":
        return math.degrees(math.atan2(v[1], v[0])) * DEGREE
    if operator == "cat2":
        return v[0] * math.cos(math.atan2(v[2], v[1]))
    if operator == "cos":
        return v[0] * math.cos(math.radians(v[1] / DEGREE))
    if operator == "max":
        return max(v[0], v[1])
    if operator == "min":
        return min(v[0], v[1])
    if operator == "mod":
        return math.sqrt(sum(value * value for value in v))
    if operator == "pin":
        return max(v[0], min(v[1], v[2]))
    if operator == "sat2":
        return v[0] * math.sin(math.atan2(v[2], v[1]))
    if operator == "sin":
        return v[0] * math.sin(math.radians(v[1] / DEGREE))
    if operator == "sqrt":
        return math.sqrt(v[0])
    return v[0] * math.tan(math.radians(v[1] / DEGREE))  # tan, the last one


def _validate(name: str, raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ShapeDataError(f"preset {name!r} must be an object")
    for key in ("adj", "gd"):
        for entry in raw.get(key, ()):
            if not isinstance(entry, list) or len(entry) != 2 or not all(isinstance(part, str) for part in entry):
                raise ShapeDataError(f"preset {name!r} has a {key} entry that is not a [name, formula] pair")
    rect = raw.get("rect")
    if rect is not None and (not isinstance(rect, list) or len(rect) != 4):
        raise ShapeDataError(f"preset {name!r} has a text rectangle that is not four expressions")
    paths = raw.get("paths")
    if not isinstance(paths, list):
        raise ShapeDataError(f"preset {name!r} has no path list")
    for path in paths:
        for command in path.get("c", ()):
            arity = _COMMAND_ARITY.get(command[0])
            if arity is None:
                raise ShapeDataError(f"preset {name!r} uses unsupported path command {command[0]!r}")
            if len(command) - 1 != arity:
                raise ShapeDataError(f"preset {name!r} command {command[0]!r} needs {arity} parameters")
    return raw


def _read(filename: str) -> dict:
    try:
        return json.loads(files(_DATA_PACKAGE).joinpath(filename).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShapeDataError(f"cannot load packaged shape data {filename}: {exc}") from exc


@lru_cache(maxsize=1)
def _load() -> tuple[Mapping[str, dict], Mapping[str, object]]:
    payload = _read(_GEOMETRY_FILE)
    if payload.get("version") != 1:
        raise ShapeDataError("preset shape data must declare version 1")
    shapes = payload.get("shapes")
    order = payload.get("order")
    if not isinstance(shapes, dict) or not isinstance(order, list):
        raise ShapeDataError("preset shape data must carry both a shape table and the enum order")
    if len(shapes) != PRESET_COUNT or set(order) != set(shapes):
        raise ShapeDataError(f"preset shape data must cover the {PRESET_COUNT} names of the enum, found {len(shapes)}")
    parsed = {name: _validate(name, raw) for name, raw in sorted(shapes.items())}
    header = {key: value for key, value in payload.items() if key != "shapes"}
    return MappingProxyType(parsed), MappingProxyType(header)


@lru_cache(maxsize=1)
def _intents() -> tuple[tuple[dict, ...], Mapping[str, str], Mapping[str, object]]:
    payload = _read(_INTENT_FILE)
    groups = payload.get("groups")
    if payload.get("version") != 1 or not isinstance(groups, list):
        raise ShapeDataError("preset intent data must declare version 1 and carry groups")
    by_name = {name: intent for group in groups for name, intent in group["presets"].items()}
    if set(by_name) != set(_load()[0]):
        raise ShapeDataError("every preset needs an intent and every intent needs a preset")
    header = {key: value for key, value in payload.items() if key != "groups"}
    return tuple(groups), MappingProxyType(by_name), MappingProxyType(header)


def preset_names() -> tuple[str, ...]:
    """Every preset name, sorted. Load errors surface here rather than at import."""
    return tuple(_load()[0])


def drawable_presets() -> tuple[str, ...]:
    """The presets a shape can be *added* as, which is every one but the connectors."""
    return tuple(name for name in preset_names() if name not in CONNECTOR_PRESETS)


# A guide that clamps an adjustment to 21599999 is clamping it to just under a full
# turn, which is how DrawingML says "this one is an angle". It is the only mark in
# the data that separates the two units the standard uses in the same field, and it
# is a mark the data carries rather than a list somebody has to maintain.
_TURN = re.compile(r"\b21(?:599999|600000)\b")
# Above this, a proportion would be more than the shape it is a proportion of. Some
# legitimately are -- a callout's tail reaches outside its box, a pentagon's `vf`
# stretches it past square -- so a large default alone does not make an angle. What
# it does mean is that scaling it as a proportion would write a number the standard
# reads as some other quantity, which is worth refusing rather than drawing.
_PROPORTION_CEILING = 100000.0
# The two names the polygons use for their own stretch factors.
_STRETCH = frozenset({"hf", "vf"})


def angle_adjustments(name: str) -> frozenset[str]:
    """Which of one preset's adjustments are angles rather than proportions.

    `pie` answers `{"adj1", "adj2"}`: its two are a start and an end on the circle,
    written in sixty-thousandths of a degree, while `chevron`'s single `adj` is a
    hundred-thousandth of its own width. The field is the same field and the units
    are not, so a helper that scales every adjustment the same way writes 1.25
    degrees where the author asked for 270 and reports nothing.
    """
    shape = _load()[0][resolve_preset_name(name)]
    names = {key for key, _ in shape.get("adj") or ()}
    if not names:
        return frozenset()
    found: set[str] = set()
    for _, formula in shape.get("gd") or ():
        if _TURN.search(formula):
            found |= names & set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", formula))
    return frozenset(found)


def unscalable_adjustments(name: str) -> frozenset[str]:
    """Adjustments this module will not scale, because it cannot tell what they are.

    Four of them, over `circularArrow` and its two mirrors and `mathNotEqual`: their
    defaults are angles by size -- 19 degrees, 110 degrees -- and no guide clamps
    them to a turn, so the mark that names the rest of the angles does not reach
    them. Writing them as proportions is what the caller would get in silence, and a
    named refusal is worth more than a shape drawn to the wrong number.
    """
    shape = _load()[0][resolve_preset_name(name)]
    angles = angle_adjustments(name)
    unscalable = set()
    for key, formula in shape.get("adj") or ():
        if key in angles or key in _STRETCH or "callout" in name.lower():
            continue
        if abs(float(formula.split()[1])) > _PROPORTION_CEILING:
            unscalable.add(key)
    return frozenset(unscalable)


def catalog_json() -> str:
    """The drawable presets as JSON text, for injecting into a build directory.

    A name, the knobs it takes and the line saying what it is for -- what an author
    needs to pick one and set it. The geometry stays here: nothing in a build
    directory evaluates a guide, because the one helper that needed an answer was
    written knowing it, and shipping a formula evaluator into every build would put
    the whole preset table in front of the author for the sake of one number.

    Which knobs are angles travels with them, because the unit is not something a
    build directory can work out and getting it wrong is silent.
    """
    catalogue = {}
    for name in drawable_presets():
        entry: dict[str, object] = {"adj": list(preset_adjustments(name)), "for": preset_intent(name)}
        angles = sorted(angle_adjustments(name))
        if angles:
            entry["deg"] = angles
        refused = sorted(unscalable_adjustments(name))
        if refused:
            entry["opaque"] = refused
        catalogue[name] = entry
    return json.dumps(catalogue, separators=(",", ":"), ensure_ascii=False)


def preset_provenance() -> Mapping[str, object]:
    """Where the geometry came from, straight from the data file."""
    return _load()[1]


def intent_provenance() -> Mapping[str, object]:
    """Where the one-line intents came from, straight from the data file."""
    return _intents()[2]


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


@lru_cache(maxsize=1)
def _by_key() -> Mapping[str, str]:
    return MappingProxyType({_normalize(name): name for name in preset_names()})


def _words(text: str) -> set[str]:
    """A name's words, however it was spelled: camelCase, snake_case or shouted."""
    return set(re.findall(r"[a-z]+|[0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text)).lower()))


def preset_candidates(term: str, limit: int = 8) -> list[str]:
    """The nearest preset names to a miss, best guess first.

    Same three passes as the icon lookup, for the same reason: the ways a name
    misses are different. `chevrons` contains a real name, `flowchartdecison` is a
    typo only character similarity catches, and `arrowRight` shares its words with
    a real name that has them the other way round.

    That last one is why the word pass is ranked rather than alphabetical. Twenty-
    four names carry the word "arrow", so an unranked pass answers `arrowRight`
    with `bentArrow, bentUpArrow, circularArrow...` and never reaches `rightArrow`
    -- a list of eight wrong answers, which is worse than no list. Sharing both
    words beats sharing one, and sharing them with nothing left over beats sharing
    them with three more.
    """
    key = _normalize(term)
    if not key:
        return []
    names = preset_names()
    words = _words(term)
    ranked: list[str] = []

    def add(candidate: str) -> None:
        if candidate not in ranked:
            ranked.append(candidate)

    scored = []
    for candidate in names:
        parts = _words(candidate)
        shared = parts & words
        if shared:
            scored.append((-len(shared), len(parts ^ words), candidate))
    for _, _, candidate in sorted(scored):
        add(candidate)
    for candidate in names:
        folded = _normalize(candidate)
        if key in folded or folded in key:
            add(candidate)
    for candidate in difflib.get_close_matches(key, [_normalize(n) for n in names], n=limit, cutoff=0.62):
        add(_by_key()[candidate])
    return ranked[:limit]


def resolve_preset_name(term: str) -> str:
    """The catalogue's spelling of what an author wrote, or a miss naming the near ones.

    `rightArrow`, `right_arrow`, `RIGHT ARROW` and `rightarrow` are the same
    shape; refusing four of them punishes an author for a separator it had no way
    to know about, which is the mistake the icon lookup already paid for once.
    """
    found = _by_key().get(_normalize(term))
    if found is not None:
        return found
    raise UnknownPresetError(str(term), preset_candidates(term), len(_by_key()))


def preset_adjustments(name: str) -> Mapping[str, float]:
    """One preset's adjustment names and their default values, in DrawingML units.

    Hundred-thousandths for a proportion and sixty-thousandths of a degree for an
    angle -- `chevron` answers `{"adj": 50000.0}` for its half, and `arc` answers
    `{"adj1": 16200000.0}` for 270 degrees. Both are what the standard writes, and
    a single scale would be wrong for one of them. The script helper divides the
    proportion by `ADJUSTMENT_SCALE`, because that is the form python-pptx's
    `shape.adjustments` takes.
    """
    definition = _load()[0][resolve_preset_name(name)]
    frame = _Frame(1.0, 1.0)
    return MappingProxyType({guide: frame.value(formula) for guide, formula in definition.get("adj", ())})


def preset_geometry(
    name: str,
    width: float,
    height: float,
    adjustments: Mapping[str, float] | None = None,
) -> PresetGeometry:
    """Resolve one preset into the frame `width` x `height`, in the caller's unit.

    The frame's origin is its own top-left, so a caller adds the box's corner to
    whatever comes back. `adjustments` overrides defaults by name and refuses a
    name the preset does not have, because an adjustment that silently does
    nothing is worse than one that says so.
    """
    resolved = resolve_preset_name(name)
    definition = _load()[0][resolved]
    frame = _Frame(width, height)
    supplied = dict(adjustments or {})
    declared = [guide for guide, _ in definition.get("adj", ())]
    unknown = sorted(set(supplied) - set(declared))
    if unknown:
        known = ", ".join(declared) or "none"
        raise ShapeFormulaError(f"preset {resolved!r} has no adjustment {unknown}; it takes {known}")

    values: dict[str, float] = {}
    for guide, formula in definition.get("adj", ()):
        values[guide] = frame.bind(guide, frame.value(supplied.get(guide, formula)))
    adjusted = dict(values)
    for guide, formula in definition.get("gd", ()):
        # Order matters and rebinding is legal: a handful of presets reuse an
        # intermediate name later in the list, and the later value is the one the
        # paths mean.
        values[guide] = frame.bind(guide, frame.evaluate(formula))

    rect = definition.get("rect")
    paths: list[tuple[tuple[str, tuple[float, ...]], ...]] = []
    for path in definition["paths"]:
        # A path may declare its own coordinate space -- flowchart symbols are
        # drawn on 21600 -- and the points mean nothing until they are scaled back
        # into the frame the caller asked for.
        scale_x = width / frame.value(path["w"]) if path.get("w") else 1.0
        scale_y = height / frame.value(path["h"]) if path.get("h") else 1.0
        commands: list[tuple[str, tuple[float, ...]]] = []
        for command in path["c"]:
            kind, raw = command[0], command[1:]
            if kind == "arcTo":
                resolved_parameters = (
                    frame.value(raw[0]) * scale_x,
                    frame.value(raw[1]) * scale_y,
                    frame.value(raw[2]),
                    frame.value(raw[3]),
                )
            else:
                resolved_parameters = tuple(
                    frame.value(token) * (scale_x if index % 2 == 0 else scale_y) for index, token in enumerate(raw)
                )
            commands.append((kind, resolved_parameters))
        paths.append(tuple(commands))

    return PresetGeometry(
        name=resolved,
        width=frame.resolve("w"),
        height=frame.resolve("h"),
        adjustments=MappingProxyType(adjusted),
        guides=MappingProxyType(values),
        text_rect=tuple(frame.value(token) for token in rect) if rect else None,  # type: ignore[arg-type]
        paths=tuple(paths),
        connections=tuple(
            (frame.value(angle), frame.value(x), frame.value(y)) for angle, x, y in definition.get("cxn", ())
        ),
    )


def preset_intent(name: str) -> str:
    """The one line that says what a preset is for."""
    return _intents()[1][resolve_preset_name(name)]


def preset_groups() -> tuple[Mapping[str, object], ...]:
    """The catalogue as Office groups it, each with its label, use and members."""
    return tuple(MappingProxyType(group) for group in _intents()[0])
