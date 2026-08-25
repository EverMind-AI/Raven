"""Defects only the render shows: words on words, rules through words, words
escaping their card.

All three are mixed ground truth, and the mix is the design. The shape
positions come from the .pptx, where a filled panel or a hairline is exact and
will never be moved; the word positions come from the rendered PDF, where they
are true. The two share a point coordinate system with its origin at the top
left, so they can be compared without a transform.

The thresholds here are calibrated against one hand-built reference deck --
reviewed, accepted, and known good -- on which every check below reports
nothing. That is what they are for: a measurement that fires on a page a reader
would call fine costs the author a rebuild and teaches it to distrust the
gates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from raven.ppt.contracts.findings import Audience, Finding, Severity
from raven.ppt.services.measure.geometry import (
    EMU_PER_POINT,
    Rect,
    has_text,
    is_panel,
    iter_shapes,
    open_deck,
    pages,
    shape_rect_pt,
    shows_picture,
)
from raven.ppt.services.measure.words import WordBox, by_page, rect

# Two words share a place when the smaller of them is this much covered. Below
# it, a generously-sized box's word merely grazes its neighbour and no reader
# ever sees them collide.
COLLISION_SHARE = 0.4

# A horizontal hairline: under ~0.06in tall and long enough to be a divider
# rather than a tick or a bullet.
RULE_MAX_HEIGHT_PT = 4.5
RULE_MIN_WIDTH_PT = 36.0
# Through the glyphs, not grazing an edge. The top quarter is spared -- a rule
# over a word's ascenders reads as a border -- and the bottom gets less, because
# a word's bbox ends below its descenders and a line at three-quarter height
# already cuts through p and g; only a true underline sits lower.
RULE_TOP_SPARE = 0.25
RULE_BOTTOM_SPARE = 0.15

# A card, not a rule or a mark.
CARD_MIN_WIDTH_PT = 72.0
CARD_MIN_HEIGHT_PT = 36.0
# A word belongs to a card when the card holds this much of it. Deliberately not
# "the card contains the word's centre": by centre alone a word that has escaped
# far enough -- centre and all -- stops being anyone's problem, and that is
# exactly the word most worth reporting.
WORD_IN_CARD_SHARE = 0.3
# What a border stroke and antialiasing account for.
CARD_SLOP_PT = 3.0
# And the other side of the same edge: copy inside its card but flush against the
# card's rim. Measured on the polished deck a reviewer went through page by page --
# two of its cards end exactly on the last line's descender, which reads as cramped
# next to the same deck's other cards at 0.20in of padding. 4pt is under a
# sixteenth of an inch: below it the type is touching the edge, not sitting in it.
CARD_PADDING_PT = 4.0

# One broken card produces a dozen pair-wise hits and the fix is the card, not
# the dozen, so each check reports a few per page and stops.
COLLISIONS_PER_PAGE = 4
RULES_PER_PAGE = 3
OVERFLOWS_PER_PAGE = 4

BODY_TOP_PT = 1.25 * 72
BODY_BOTTOM_PT = 6.90 * 72
HEADER_BOTTOM_PT = 1.70 * 72
EMPTY_GAP_PT = 0.95 * 72
EMPTY_TRAILING_PT = 1.20 * 72
EMPTY_PANEL_BOTTOM_PT = 0.75 * 72
EMPTY_PANEL_USED_SHARE = 0.58
LOOSE_HEADER_GAP_PT = 0.30 * 72


def word_collisions(words: Sequence[WordBox], per_page: int = COLLISIONS_PER_PAGE) -> list[Finding]:
    """Words painted over other words, measured on the render.

    The one measured layout problem that refuses a deck. D2 has every measurement
    feed the design loop instead, on the argument that hard-refusing invites the
    shrink-and-retry oscillation text fitting already threatens -- but three live
    runs published decks with copy painted over copy, and shrinking is not the way
    out of this one: the type floor is measured too, so it trades one finding for
    another, and the move a collision wants is a wider box, which costs nothing.
    """
    findings: list[Finding] = []
    for page, painted in sorted(by_page(words).items()):
        reported = 0
        for index, first in enumerate(painted):
            if reported >= per_page:
                break
            for second in painted[index + 1 :]:
                smaller = min(rect(first).area, rect(second).area)
                if smaller <= 0:
                    continue
                share = rect(first).overlap(rect(second)) / smaller
                # The same word over the same word is the renderer double-
                # painting bold -- sometimes offset by a pixel or two, so any
                # same-text pair is discounted rather than only exact stacks.
                if share < COLLISION_SHARE or first.text == second.text:
                    continue
                findings.append(
                    Finding(
                        kind="word_collision",
                        severity=Severity.BLOCKING,
                        page=page,
                        audience=Audience.DESIGNER,
                        message=(
                            f"in the render, {first.text[:24]!r} is painted over {second.text[:24]!r} -- "
                            "two pieces of text are occupying the same place"
                        ),
                        detail={"over": first.text[:24], "under": second.text[:24], "share": round(share, 3)},
                    )
                )
                reported += 1
                break
    return findings


def rule_strikes(
    rules: Mapping[int, Sequence[Rect]],
    words: Sequence[WordBox],
    per_page: int = RULES_PER_PAGE,
) -> list[Finding]:
    """Hairline rules that cross rendered text.

    The declared geometry looks innocent: a divider drawn at a table's declared
    row boundary sits exactly between two rows on paper. But a row's declared
    height is only a minimum -- the renderer grows rows to fit their text, the
    boundaries below drift down, and the rule, which does not move, ends up
    striking through a row's copy.
    """
    painted = by_page(words)
    findings: list[Finding] = []
    for page, drawn in sorted(rules.items()):
        reported = 0
        for rule in drawn:
            if reported >= per_page:
                break
            middle = (rule.y0 + rule.y1) / 2
            for word in painted.get(page, ()):
                tall = word.y1 - word.y0
                if tall <= 0 or not (word.y0 + RULE_TOP_SPARE * tall < middle < word.y1 - RULE_BOTTOM_SPARE * tall):
                    continue
                if min(rule.x1, word.x1) - max(rule.x0, word.x0) <= 0:
                    continue
                findings.append(
                    Finding(
                        kind="rule_strike",
                        severity=Severity.WARNING,
                        page=page,
                        audience=Audience.DESIGNER,
                        message=(
                            f"a divider line strikes through the text {word.text[:28]!r} -- in the render "
                            "the rows have grown taller than drawn, and the line no longer sits between them"
                        ),
                        detail={"text": word.text[:28], "rule_y_pt": round(middle, 2)},
                    )
                )
                reported += 1
                break
    return findings


def card_overflows(
    cards_by_page: Mapping[int, Sequence[Rect]],
    words: Sequence[WordBox],
    per_page: int = OVERFLOWS_PER_PAGE,
) -> list[Finding]:
    """Rendered words that escape the card they belong to.

    `word_collisions` only sees text landing on text; copy that runs off the
    bottom of its card onto blank page, or onto the card's border, escapes it.

    One card, one finding, and the finding carries the number to act on. Reporting
    each escaped word separately put nine findings on one deck for three cards, and
    saying only that copy "has outgrown the card" left the author guessing between
    two moves: a live run spent fourteen requests alternating between this and
    `word_collision`, shortening the copy until the card fit and lengthening the card
    until it hit its neighbour. The height the copy actually needs is already
    measured here -- withholding it is what made the two findings look like a
    contradiction.
    """
    painted = by_page(words)
    findings: list[Finding] = []
    for page, panels in sorted(cards_by_page.items()):
        escaped: dict[int, list[tuple[WordBox, Rect]]] = {}
        for word in painted.get(page, ()):
            box = rect(word)
            if box.area <= 0:
                continue
            # The word belongs to the smallest card holding a real share of it,
            # because cards nest: a stat sits in its own tile inside a band.
            home_at = None
            for index, card in enumerate(panels):
                if card.overlap(box) / box.area < WORD_IN_CARD_SHARE:
                    continue
                if home_at is None or card.area < panels[home_at].area:
                    home_at = index
            if home_at is None:
                continue  # page furniture: titles and footers live on the page, not in a card
            home = panels[home_at]
            escape = max(home.x0 - box.x0, home.y0 - box.y0, box.x1 - home.x1, box.y1 - home.y1)
            if escape <= CARD_SLOP_PT:
                continue
            escaped.setdefault(home_at, []).append((word, box))
        shown = sorted(escaped.items())[:per_page]
        for home_at, items in shown:
            findings.append(_card_overflow(page, panels[home_at], items))
        if len(escaped) > len(shown):
            # A page where every card overflows is one layout decision, not N, and
            # the list would bury the rest of the report. Say what was left out
            # rather than letting the count read as the whole of it.
            findings.append(
                Finding(
                    kind="card_overflow",
                    severity=Severity.WARNING,
                    page=page,
                    audience=Audience.DESIGNER,
                    message=(
                        f"{len(escaped) - len(shown)} more card(s) on this page overflow the same way. "
                        f"They are not listed one by one because the row itself is what is too small"
                    ),
                    detail={"unlisted_cards": len(escaped) - len(shown)},
                )
            )
    return findings


def _card_overflow(page: int, home: Rect, items: Sequence[tuple[WordBox, Rect]]) -> Finding:
    """One card's overflow, with the move that fixes it."""
    below = max(box.y1 for _, box in items) - home.y1
    beside = max(max(home.x0 - box.x0, box.x1 - home.x1) for _, box in items)
    sample = ", ".join(repr(word.text[:16]) for word, _ in items[:3])
    detail: dict[str, object] = {
        "words": [word.text[:28] for word, _ in items[:6]],
        "card_in": [round(home.width / 72, 2), round(home.height / 72, 2)],
    }
    if below >= beside:
        needed = (home.height + below + CARD_SLOP_PT) / 72
        detail["needs_height_in"] = round(needed, 2)
        advice = (
            f"the card is {home.height / 72:.2f}in tall and this copy needs {needed:.2f}in. "
            f"Give it that height, take the room from a neighbour, or say it in fewer words "
            f"-- do not shrink the type to fit"
        )
    else:
        needed = (home.width + beside + CARD_SLOP_PT) / 72
        detail["needs_width_in"] = round(needed, 2)
        advice = (
            f"the card is {home.width / 72:.2f}in wide and this copy needs {needed:.2f}in across. "
            f"Widen the card or break the line"
        )
    return Finding(
        kind="card_overflow",
        severity=Severity.WARNING,
        page=page,
        audience=Audience.DESIGNER,
        message=f"{len(items)} word(s) of this card's copy escape it in the render ({sample}): {advice}",
        detail=detail,
    )


def crowded_panels(
    cards_by_page: Mapping[int, Sequence[Rect]],
    words: Sequence[WordBox],
    per_page: int = OVERFLOWS_PER_PAGE,
) -> list[Finding]:
    """Copy inside its card and touching the card's edge.

    `card_overflow` reports the word that got out. This reports the word that did not
    and has nowhere left to go: on a page-by-page review of a polished deck, two note
    cards ended exactly on their last line's descender while every other card on the
    same deck carried 0.20in of padding. Nothing overflows, nothing collides, and the
    page reads as cramped in a way no measurement had a name for.
    """
    painted = by_page(words)
    # What the rest of this deck's panels carry, so the finding can name the number to
    # hit rather than asking for "padding": the pass gets a figure, and a deck whose
    # panels are all tight gets no invented target.
    house = _panel_padding(cards_by_page, painted)
    findings: list[Finding] = []
    for page, panels in sorted(cards_by_page.items()):
        reported = 0
        # One finding per panel, not per word: a line of eight words against the rim is
        # one cramped panel and one fix.
        seen: set[tuple[float, float, float, float]] = set()
        for word in painted.get(page, ()):
            if reported >= per_page:
                break
            box = rect(word)
            if box.area <= 0:
                continue
            candidates = [card for card in panels if card.overlap(box) / box.area >= WORD_IN_CARD_SHARE]
            if not candidates:
                continue
            home = min(candidates, key=lambda card: card.area)
            if (home.x0, home.y0, home.x1, home.y1) in seen:
                continue
            # Inside, or this is `card_overflow`'s finding rather than ours.
            escape = max(home.x0 - box.x0, home.y0 - box.y0, box.x1 - home.x1, box.y1 - home.y1)
            if escape > CARD_SLOP_PT:
                continue
            # The bottom rim only. A line tight against the left or right edge is
            # usually the design's own inset -- a rail, a full-width band, a table
            # column -- and reporting those buried the one that reads as cramped:
            # measured across three decks, 20 of the 32 hits were horizontal and every
            # one of them was a page a reviewer had called clean.
            gap = home.y1 - box.y1
            if gap >= CARD_PADDING_PT:
                continue
            side = "bottom"
            findings.append(
                Finding(
                    kind="crowded_panel",
                    severity=Severity.WARNING,
                    page=page,
                    audience=Audience.DESIGNER,
                    message=(
                        f"in the render, {word.text[:28]!r} sits {gap / 72:.3f}in from the {side} edge of its "
                        f"panel -- the copy is touching the rim rather than sitting inside it. "
                        + (
                            f"This deck's other panels carry {house / 72:.2f}in there; give this one the same, "
                            if house is not None
                            else "Give the panel the padding its copy needs, "
                        )
                        + "or the copy one line less"
                    ),
                    detail={"text": word.text[:28], "side": side, "gap_in": round(gap / 72, 3)},
                )
            )
            seen.add((home.x0, home.y0, home.x1, home.y1))
            reported += 1
    return findings


def excessive_whitespace(
    pptx_path: Path,
    words: Sequence[WordBox],
    structural: Sequence[int] = (),
    per_page: int = 2,
) -> list[Finding]:
    """Large rendered gaps that leave a content page visibly unfinished."""
    presentation = open_deck(pptx_path)
    page_width = presentation.slide_width / EMU_PER_POINT
    page_height = presentation.slide_height / EMU_PER_POINT
    body_top = min(BODY_TOP_PT, page_height * 0.24)
    body_bottom = min(BODY_BOTTOM_PT, page_height * 0.94)
    painted = by_page(words)
    skipped = set(structural)
    panels = cards(pptx_path)
    findings: list[Finding] = []

    for number, slide in pages(pptx_path):
        if number in skipped:
            continue
        on_page = painted.get(number, ())
        reported = 0
        header = [
            (lines[0][0].y0, max(word.y1 for line in lines for word in line))
            for _, box, lines in _boxes_with_lines(slide, on_page)
            if lines
            and box.width >= page_width * 0.55
            and box.y0 < HEADER_BOTTOM_PT
            and lines[0][0].y0 < HEADER_BOTTOM_PT
        ]
        header.sort()
        header_gaps = [
            (first[1], second[0], second[0] - first[1])
            for first, second in zip(header, header[1:])
            if second[0] > first[1]
        ]
        if header_gaps:
            start, end, gap = max(header_gaps, key=lambda item: item[2])
            if gap >= LOOSE_HEADER_GAP_PT:
                findings.append(
                    _empty_finding(
                        number,
                        "loose_header",
                        gap,
                        (
                            f"two header text rows sit {gap / 72:.2f}in apart and read as separate regions. "
                            "Pull the kicker, title and explanatory line into one compact group, then leave "
                            "the larger gap below that group before the body"
                        ),
                        start,
                        end,
                    )
                )
                reported += 1
        intervals = [
            (max(body_top, word.y0), min(body_bottom, word.y1))
            for word in on_page
            if word.y1 > body_top and word.y0 < body_bottom
        ]
        for shape in iter_shapes(slide.shapes):
            if not (shows_picture(shape) or getattr(shape, "has_chart", False)):
                continue
            box = shape_rect_pt(shape)
            if box.y1 > body_top and box.y0 < body_bottom:
                intervals.append((max(body_top, box.y0), min(body_bottom, box.y1)))

        bands = _vertical_bands(intervals)
        if bands:
            gaps = [(left[1], right[0], right[0] - left[1]) for left, right in zip(bands, bands[1:])]
            if gaps:
                start, end, gap = max(gaps, key=lambda item: item[2])
                if gap >= EMPTY_GAP_PT:
                    findings.append(
                        _empty_finding(
                            number,
                            "between_groups",
                            gap,
                            (
                                f"the render leaves a {gap / 72:.2f}in vertical blank field between two body "
                                "groups. Grow the load-bearing table, figure or type, or redistribute the groups; "
                                "do not fill it with decoration"
                            ),
                            start,
                            end,
                        )
                    )
                    reported += 1
            trailing = body_bottom - bands[-1][1]
            if reported < per_page and trailing >= EMPTY_TRAILING_PT:
                findings.append(
                    _empty_finding(
                        number,
                        "trailing_body",
                        trailing,
                        (
                            f"the page's last substantive body content ends {trailing / 72:.2f}in above the body "
                            "boundary, leaving the lower field unfinished. Increase the main content's scale or "
                            "use the space for the page's conclusion"
                        ),
                        bands[-1][1],
                        body_bottom,
                    )
                )
                reported += 1

        for panel in panels.get(number, ()):
            if reported >= per_page:
                break
            inside = [
                rect(word)
                for word in painted.get(number, ())
                if panel.overlap(rect(word)) / max(rect(word).area, 1e-6) >= WORD_IN_CARD_SHARE
            ]
            if not inside:
                continue
            top = min(box.y0 for box in inside)
            bottom = max(box.y1 for box in inside)
            used_share = (bottom - top) / max(panel.height, 1e-6)
            bottom_gap = panel.y1 - bottom
            if bottom_gap < EMPTY_PANEL_BOTTOM_PT or used_share >= EMPTY_PANEL_USED_SHARE:
                continue
            findings.append(
                _empty_finding(
                    number,
                    "empty_panel",
                    bottom_gap,
                    (
                        f"a {panel.height / 72:.2f}in-high panel uses only {used_share:.0%} of its height for "
                        f"copy and leaves {bottom_gap / 72:.2f}in blank at the bottom. Shorten the panel or "
                        "increase and redistribute its content"
                    ),
                    bottom,
                    panel.y1,
                )
            )
            reported += 1
    return findings


def _vertical_bands(intervals: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + 4:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _empty_finding(
    page: int,
    region: str,
    gap: float,
    message: str,
    start: float,
    end: float,
) -> Finding:
    return Finding(
        kind="excessive_whitespace",
        severity=Severity.WARNING,
        page=page,
        audience=Audience.DESIGNER,
        message=message,
        detail={
            "region": region,
            "gap_in": round(gap / 72, 2),
            "from_y_in": round(start / 72, 2),
            "to_y_in": round(end / 72, 2),
        },
    )


# A block whose last line holds this much and no more broke one or two characters
# short of fitting. Measured on a delivered deck, where four of eight agenda labels
# read "为什么要统 / 一", "跨帧一 / 致性", "统一是否有 / 效", "不追求全任务 / 最优".
ORPHAN_CHARS = 2
# And only a two-line block, which is a label. Body copy ending on a short line is
# ordinary typesetting with nothing to fix.
ORPHAN_LINES = 2
# Words within this much of each other vertically are on the same line: the renderer
# puts a line's words on one baseline, and a superscript or a CJK glyph in a mixed
# line moves a box by a point or two.
SAME_LINE_PT = 4.0


def orphan_lines(pptx_path: Path, words: Sequence[WordBox], per_page: int = OVERFLOWS_PER_PAGE) -> list[Finding]:
    """Labels the render broke with one or two characters alone on the last line.

    Read off the render rather than predicted. The first version wrapped the copy with
    the font measurer and reported what it thought would break: on a page a reviewer had
    called clean it claimed the title broke as "…基准上更好 / 更好", and the render shows
    that title on one line. A one-character orphan is too fine a thing to predict, and
    the render already knows.
    """
    painted = by_page(words)
    findings: list[Finding] = []
    for number, slide in pages(pptx_path):
        reported = 0
        on_page = painted.get(number, [])
        for shape in iter_shapes(slide.shapes):
            if reported >= per_page or not has_text(shape) or not shape.width:
                continue
            box = shape_rect_pt(shape)
            if box.area <= 0:
                continue
            inside = [word for word in on_page if box.overlap(rect(word)) / max(rect(word).area, 1e-6) >= 0.5]
            if not inside:
                continue
            lines = _lines(inside)
            if len(lines) != ORPHAN_LINES:
                continue
            last = "".join(word.text for word in lines[-1]).strip()
            if not last or len(last) > ORPHAN_CHARS:
                continue
            head = " ".join(word.text for word in lines[0])
            findings.append(
                Finding(
                    kind="orphan_line",
                    severity=Severity.WARNING,
                    page=number,
                    audience=Audience.DESIGNER,
                    message=(
                        f"'{head[:30]}' breaks with {last!r} alone on the second line -- the box is "
                        f"{box.width / 72:.2f}in and the label needs a hair more. Widen it, or say it in fewer "
                        f"characters"
                    ),
                    detail={"label": head[:40], "orphan": last, "box_in": round(box.width / 72, 2)},
                )
            )
            reported += 1
    return findings


def _panel_padding(cards_by_page: Mapping[int, Sequence[Rect]], painted: Mapping[int, Sequence[WordBox]]):
    """The bottom padding this deck's panels mostly carry, in points, or None.

    Measured over the panels that are not tight, because the number is there to tell a
    cramped panel what the others do. A deck whose panels are all tight has no house
    padding to quote and the finding says so instead of inventing one.
    """
    gaps: list[float] = []
    for page, panels in cards_by_page.items():
        for panel in panels:
            inside = [
                rect(word)
                for word in painted.get(page, ())
                if panel.overlap(rect(word)) / max(rect(word).area, 1e-6) >= WORD_IN_CARD_SHARE
            ]
            if not inside:
                continue
            gap = panel.y1 - max(box.y1 for box in inside)
            if gap >= CARD_PADDING_PT:
                gaps.append(gap)
    if not gaps:
        return None
    gaps.sort()
    return gaps[len(gaps) // 2]


def _lines(words: Sequence[WordBox]) -> list[list[WordBox]]:
    """The words grouped into the lines the renderer set them on."""
    lines: list[list[WordBox]] = []
    for word in sorted(words, key=lambda word: (word.y0, word.x0)):
        if lines and abs(word.y0 - lines[-1][0].y0) <= SAME_LINE_PT:
            lines[-1].append(word)
        else:
            lines.append([word])
    return lines


def _pitch(lines: Sequence[Sequence[WordBox]]) -> float | None:
    """The distance between this block's own lines, which is its inner rhythm."""
    tops = [line[0].y0 for line in lines]
    gaps = sorted(second - first for first, second in zip(tops, tops[1:]) if second > first)
    return gaps[len(gaps) // 2] if gaps else None


# Two stacked blocks are one block to a reader when the air between them is no more
# than the air inside them. 1.25 rather than 1.0 because a boundary a reader can see
# has to be *visibly* wider, and because the renderer's leading varies by a point.
SEPARATION_RATIO = 1.25
# Blocks this far apart horizontally are in different columns and their vertical gap
# says nothing.
SAME_COLUMN_SHARE = 0.5


def unseparated_blocks(pptx_path: Path, words: Sequence[WordBox], per_page: int = 2) -> list[Finding]:
    """Text blocks stacked with less air between them than the text inside them uses.

    The design brief asks for this in prose -- "a gap that is plainly wider than the
    gaps inside each group" -- and nothing measured it. Found by reading a polished deck
    page by page: four task definitions in one column ran together into a single grey
    mass, a table's last row touched the notes under it, and two column headings floated
    with no visible tie to what they headed. Each of those is two shapes whose gap is
    smaller than their own line spacing.
    """
    painted = by_page(words)
    findings: list[Finding] = []
    for number, slide in pages(pptx_path):
        on_page = painted.get(number, [])
        if not on_page:
            continue
        blocks = [(box, lines) for _shape, box, lines in _boxes_with_lines(slide, on_page)]
        reported = 0
        # Inside a box first. The case that produced this check was four task definitions
        # in one text box, which the pass had consolidated: to a reader they are four
        # groups, to the file they are eight paragraphs, and to a check comparing shapes
        # they are one shape and invisible.
        for shape, box, lines in _boxes_with_lines(slide, on_page):
            if reported >= per_page:
                break
            finding = _paragraph_gap(shape, lines, number)
            if finding is not None:
                findings.append(finding)
                reported += 1
        for first, second in _stacked(blocks):
            if reported >= per_page:
                break
            above, below = first, second
            gap = below[1][0][0].y0 - above[1][-1][0].y0
            pitch = _pitch(above[1]) or _pitch(below[1])
            if pitch is None or gap <= 0 or gap > pitch * SEPARATION_RATIO:
                continue
            head = " ".join(word.text for word in below[1][0])[:26]
            findings.append(
                Finding(
                    kind="unseparated_blocks",
                    severity=Severity.WARNING,
                    page=number,
                    audience=Audience.DESIGNER,
                    message=(
                        f"'{head}' starts {gap / 72:.2f}in under the block above it, which sets its own lines "
                        f"{pitch / 72:.2f}in apart -- the gap between the two groups is no wider than the gaps "
                        f"inside them, so a reader sees one block of text. Widen the gap, or put a surface or a "
                        f"hairline between them"
                    ),
                    detail={"head": head, "gap_in": round(gap / 72, 3), "pitch_in": round(pitch / 72, 3)},
                )
            )
            reported += 1
    return findings


def _boxes_with_lines(slide, on_page: Sequence[WordBox]):
    """Every text shape on the page with the lines the renderer set inside it."""
    for shape in iter_shapes(slide.shapes):
        if not has_text(shape) or not shape.width:
            continue
        box = shape_rect_pt(shape)
        if box.area <= 0:
            continue
        inside = [word for word in on_page if box.overlap(rect(word)) / max(rect(word).area, 1e-6) >= 0.5]
        lines = _lines(inside)
        if lines:
            yield shape, box, lines


def _paragraph_gap(shape, lines: list[list[WordBox]], page: int) -> Finding | None:
    """A paragraph boundary inside one box that a reader cannot see.

    The boundary is exact -- paragraphs are in the file -- and the gap is the render's.
    Lines are assigned to paragraphs by consuming characters in order, which holds for
    CJK and for Latin alike because the renderer breaks lines but never reorders them.
    """
    kept = [para for para in shape.text_frame.paragraphs if "".join(run.text for run in para.runs).strip()]
    paragraphs = ["".join("".join(run.text for run in para.runs).split()) for para in kept]
    if len(paragraphs) < 2 or len(lines) < 3:
        return None
    # Bold or larger than the paragraph above it, which is what makes a paragraph the
    # start of a *group* rather than the body of the one before it. A heading sitting
    # tight against its own body is right; a heading sitting tight against the previous
    # group's body is the defect -- and reporting every paragraph boundary reported the
    # first kind three times as often as the second.
    heads = [False] + [_leads(kept[index], kept[index - 1]) for index in range(1, len(kept))]
    starts: list[int] = []
    index, carried = 0, ""
    for position, line in enumerate(lines):
        if index >= len(paragraphs):
            break
        if not carried:
            starts.append(position)
        carried += "".join("".join(word.text for word in line).split())
        if len(carried) >= len(paragraphs[index]):
            index += 1
            carried = ""
    if len(starts) < 2:
        return None
    inner = [
        lines[position + 1][0].y0 - lines[position][0].y0
        for position in range(len(lines) - 1)
        if position + 1 not in starts
    ]
    inner = sorted(gap for gap in inner if gap > 0)
    if not inner:
        return None
    pitch = inner[len(inner) // 2]
    for order, position in enumerate(starts[1:], start=1):
        if order >= len(heads) or not heads[order]:
            continue
        gap = lines[position][0].y0 - lines[position - 1][0].y0
        if gap <= 0 or gap > pitch * SEPARATION_RATIO:
            continue
        head = " ".join(word.text for word in lines[position])[:26]
        return Finding(
            kind="unseparated_blocks",
            severity=Severity.WARNING,
            page=page,
            audience=Audience.DESIGNER,
            message=(
                f"'{head}' is a new paragraph in the same box and starts {gap / 72:.2f}in under the line above "
                f"it, which is the {pitch / 72:.2f}in this box uses between its own lines -- the groups read as "
                f"one block. Give the paragraph space before it, or split the box"
            ),
            detail={"head": head, "gap_in": round(gap / 72, 3), "pitch_in": round(pitch / 72, 3), "inside": True},
        )
    return None


def _leads(paragraph, previous) -> bool:
    """Whether this paragraph starts a group rather than continuing the one above.

    Bold when the one above is not, or set larger than it. Both are read off the file
    rather than the render, because the file is where the author said which is which.
    """

    def weight(para) -> tuple[bool, float]:
        runs = [run for run in para.runs if run.text.strip()]
        bold = any(run.font.bold for run in runs)
        sizes = [run.font.size.pt for run in runs if run.font.size is not None]
        return bold, max(sizes) if sizes else 0.0

    bold, size = weight(paragraph)
    was_bold, was_size = weight(previous)
    return (bold and not was_bold) or (size > was_size > 0)


def _stacked(blocks: Sequence[tuple[Rect, list[list[WordBox]]]]):
    """Pairs of blocks in the same column, the upper one first, nothing between them."""
    ordered = sorted(blocks, key=lambda entry: entry[0].y0)
    for index, above in enumerate(ordered):
        for below in ordered[index + 1 :]:
            narrower = min(above[0].width, below[0].width)
            if narrower <= 0:
                continue
            shared = min(above[0].x1, below[0].x1) - max(above[0].x0, below[0].x0)
            if shared / narrower < SAME_COLUMN_SHARE:
                continue
            if below[0].y0 < above[0].y1 - 1:
                continue  # overlapping boxes are somebody else's finding
            yield above, below
            break


def hairline_rules(pptx_path: Path) -> dict[int, list[Rect]]:
    """Every horizontal hairline in a built deck, in points, keyed by page.

    Not restricted to filled shapes: a rule is often drawn as a line, a
    connector or a borderless box, and all of them read the same on paper.
    """
    found: dict[int, list[Rect]] = {}
    for number, slide in pages(pptx_path):
        drawn: list[Rect] = []
        for shape in iter_shapes(slide.shapes):
            if has_text(shape):
                continue
            box = shape_rect_pt(shape)
            if box.height <= RULE_MAX_HEIGHT_PT and box.width >= RULE_MIN_WIDTH_PT:
                drawn.append(box)
        found[number] = drawn
    return found


def cards(pptx_path: Path) -> dict[int, list[Rect]]:
    """Every filled panel big enough to hold copy, in points, keyed by page."""
    found: dict[int, list[Rect]] = {}
    for number, slide in pages(pptx_path):
        panels: list[Rect] = []
        for shape in iter_shapes(slide.shapes):
            if has_text(shape) or not is_panel(shape):
                continue
            box = shape_rect_pt(shape)
            if box.width >= CARD_MIN_WIDTH_PT and box.height >= CARD_MIN_HEIGHT_PT:
                panels.append(box)
        found[number] = panels
    return found


# A page is allowed to lose this much of its declared copy to the renderer before it
# counts as clipped. Not zero: a soft hyphen, a zero-width joiner and a run split
# across two spans all come back a character or two short, and one template's
# decorative glyph is a private-use codepoint that no installed face draws.
COPY_SHOWN = 0.9
# Under this a page holds a label or two and the share is noise -- one dropped
# character out of eight is 12%.
CLIP_MIN_CHARS = 40


def clipped_copy(pptx_path: Path, words: Sequence, page_texts: Mapping[int, str] | None = None) -> list[Finding]:
    """Copy the deck states and the render does not show.

    The gap nothing else covers. `overset_copy` measures whether copy fits the height
    of its box; when it does not fit the *width* of a shape that clips -- a circle, a
    frame with wrap off, a table cell -- the renderer simply stops painting, and every
    other check passes: the words that are there sit exactly where they belong, the
    boxes do not overlap, the type is the right size. A live page rendered "Backbone",
    "Transformer" and "VOS" as "Bac", "Tran" and "VOS", and the deck's own text still
    said all three in full.

    So the file's copy is compared against the render's, per page, and what is missing
    is named. Both sides are stripped of whitespace before comparing, because the
    renderer's line breaking inserts its own.
    """
    declared = page_texts if page_texts is not None else _declared_text(pptx_path)
    rendered: dict[int, list[str]] = {}
    for word in words:
        rendered.setdefault(word.page, []).append(word.text)
    findings: list[Finding] = []
    for page, text in sorted(declared.items()):
        flat = _squeezed(text)
        if len(flat) < CLIP_MIN_CHARS:
            continue
        shown = _squeezed("".join(rendered.get(page, ())))
        if not shown:
            continue  # a page whose text the reader could not extract at all is not this check
        share = _shown_share(flat, shown)
        if share >= COPY_SHOWN:
            continue
        missing = _missing(text, shown)
        findings.append(
            Finding(
                kind="clipped_copy",
                severity=Severity.WARNING,
                page=page,
                audience=Audience.DESIGNER,
                message=(
                    f"the render shows {share:.0%} of what this page says"
                    + (f", and stops mid-word on {missing}" if missing else "")
                    + ". A shape whose copy is wider than it is clips rather than wrapping -- give the box the "
                    "width the words need, or use a shorter word. Nothing else can see this: the words that "
                    "did render sit exactly where they belong"
                ),
                detail={"page": page, "shown": round(share, 2), "cut": missing[:4]},
            )
        )
    return findings


def _declared_text(pptx_path: Path) -> dict[int, str]:
    from raven.ppt.services.measure.geometry import iter_text_frames, open_deck

    found: dict[int, str] = {}
    for number, slide in enumerate(open_deck(pptx_path).slides, start=1):
        found[number] = " ".join(frame.text for frame in iter_text_frames(slide))
    return found


def _squeezed(text: str) -> str:
    return "".join(text.split())


def _shown_share(declared: str, shown: str) -> float:
    """How much of the declared copy the render accounts for, character by character.

    A multiset rather than a subsequence: the renderer reorders nothing but it does
    split runs, and a page's own text arrives in shape order while the render arrives
    in reading order.
    """
    from collections import Counter

    have, want = Counter(shown), Counter(declared)
    return sum(min(count, have[char]) for char, count in want.items()) / max(sum(want.values()), 1)


def _missing(text: str, shown: str) -> list[str]:
    """Whole words the file states and the render did not finish.

    Named rather than counted: "stops mid-word on Backbone, Transformer" is the
    sentence that finds the shape, and a percentage is not.
    """
    cut: list[str] = []
    for word in text.replace("\n", " ").split():
        stripped = word.strip("·•,.;:()（）、，。")
        if len(stripped) < 5 or not stripped.isascii():
            continue
        if _squeezed(stripped) in shown:
            continue
        # A prefix of it did render: that is a clip rather than a word this page never
        # showed, and the distinction is what keeps a missing figure caption out of here.
        if any(stripped[:length] in shown for length in range(3, len(stripped))):
            cut.append(stripped)
    return cut
