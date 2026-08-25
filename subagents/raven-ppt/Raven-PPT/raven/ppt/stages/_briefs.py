"""What the design pass is told, with measured numbers formatted in."""

from __future__ import annotations

PAGE_BRIEF = """Make this slide a better designed page by rewriting the block of
code that draws it. You get the page as it renders, the deck's shared setup as
read-only context, its page-level outline, and that block. Judge it as a reader
will see it projected.

What the page says stays: no invented facts, numbers, names or citations; nothing
dropped that the page needs to make its point. The outline's claim, supporting
points, intended visual and figures are binding. How they are arranged, grouped,
weighted, sized and decorated is yours.

A reader should see the hierarchy before reading: what leads, what supports it,
and where components begin and end. Equal visual weight everywhere argues
nothing. Use surfaces only for real peer groups or asides; wrapping every block
in the same filled card turns a presentation into a dashboard.
Every content page needs at least two visible levels below the title: the main
argument or load-bearing visual, then its supporting evidence. A conclusion or
decision gets its own baseline, weight or restrained accent rather than blending
into the last support item.

Cards and filled planes are optional tools, not the page skeleton. A page may be
figure-led, typographic, drawn-table-led, comparison-matrix-led, a process, a timeline, a chart, a comparison
on a shared baseline, or a free composition. Choose from the argument rather than
from whichever helper is easiest to call.

When the page pairs a figure with text, keep the figure dominant and organize the
supporting copy into two to four labelled points on restrained theme-coloured
surfaces. The points create a second visual layer; they do not become equal dashboard
tiles. Keep the figure's source caption, or its inspected visual caption when no source
caption exists.

Use the render as final truth. If a self-composed page leaves a large accidental
empty field, repeats the same filled-card treatment as neighbouring pages, gives
every region equal weight, or makes the main figure, table or argument too small
for the canvas, change the composition. Grow the load-bearing content, introduce
a visual layer, or redistribute what is already there. Do not add empty decoration,
duplicate copy, or shrink type to disguise the problem.

Do not use a native PowerPoint table, `add_table`, or `ppt_layout.table`. Preserve
every row and cell, but draw the table from aligned text regions and hairlines so row
height, hierarchy and type size answer to this page rather than to Office geometry.
Set table headers at the same size as the body rows or one step larger, and bold;
a header smaller than its cells reverses the hierarchy.

Header geometry belongs to the shared setup, never to one page. Every content page
calls the shared heading/content-page helper for its kicker, title, explanatory line
and rule; replace page-local header coordinates with that call. The helper keeps those
rows as one compact group and leaves the larger break below it before the body.

Two constructions read as broken: a picture flush against a panel fights its
outline, so inset it; a divider drawn across a declared table row boundary may
strike text when rendering grows the row, so tie rules to geometry that will not
move.

Nothing goes under {min_pt}pt, and the size most of the page runs at stays at or
above {body_pt}pt. If it cannot fit, reorganize the existing content. Shrinking
type is off the table.

The shared setup is read-only. Call what it defines but change none of it. The
build also refuses a filled colour bar carrying nothing; a surface containing a
real group is not such a bar.

Reply as JSON and nothing else:

{{"slide": <int>,
 "verdict": "ok" | "edited",
 "notes": ["what changed and what visible problem it fixed"],
 "block": "<the complete replacement for this page's block>"}}

The block must be complete runnable Python at the original indentation. Use
`"verdict": "ok"` and omit `block` only when the rendered page already uses its
space deliberately and communicates its outline clearly."""


DECK_BRIEF = """Make this deck read as one designed thing by rewriting the shared
setup its pages use. You get every page in a contact sheet and that setup.

Look for cross-page defects: inconsistent type roles, weak headers, spacing that
changes page to page, and one construction repeated for everything. A deck of
card grids reads as a dashboard, not a talk. Large accidental empty fields are
also visible here: name them in `vocabulary` so the page pass redistributes its
existing content instead of preserving the gap.

Define shared boundaries in the setup: one larger gap between groups and a
smaller one inside a group, one restrained surface treatment, one rule weight.
The header is owned here: define one compact kicker/title/explanatory-line geometry
and one body start in the shared helper, so no page block invents its own title row.
Change the setup, not page blocks; a page pass follows. Put page-level decisions
in short imperative `vocabulary` lines.

Keep the palette and font family. Every existing setup name must remain callable.
Nothing goes under {min_pt}pt and body copy remains at or above {body_pt}pt.

Do not normalize every page into a card grid. Pages may be image-led, drawn-table-led, comparison-matrix-led,
typographic, timelines, processes, charts, shared-baseline comparisons, or free
compositions. Only genuine same-level peers need cards.

Reply as JSON and nothing else:

{{"verdict": "ok" | "edited",
 "notes": ["what changed and why"],
 "vocabulary": ["what every page should now do, one line each"],
 "prelude": "<the complete replacement for the shared setup>"}}

Use `"verdict": "ok"` with no `prelude` if the setup is already right."""


INTAKE_BRIEF = """Read a deck task and say what it takes to do it. You get the
task, a workspace listing and existing project state.

Separate stated facts from guesses. The language, audience and page budget bind
only when stated; missing values become user questions elsewhere. `errands` are
specific material to retrieve, not work to perform. `task_is_material` is true
when the request itself contains evidence or substantive notes.

Reply as JSON and nothing else:

{"topic": "<one line>",
 "stated": {"language": <string or null>, "audience": <string or null>,
            "pages_low": <int or null>, "pages_high": <int or null>},
 "materials_dir": "<relative path or empty>",
 "task_is_material": <bool>,
 "template": "<user .pptx path, bundled template filename, or empty>",
 "questions": [{"question": "...", "why": "...", "options": ["...", "..."]}],
 "errands": [{"what": "...", "why": "...", "how": "..."}],
 "notes": ["a binding instruction not carried elsewhere"]}

Keep questions few. Do not ask duplicate language, audience or page-count
questions. Notes carry user instructions, not observations."""


HOUSE_STYLE = """

This deck is built inside a template the user supplied. Preserve its palette and
typography. Structural cover, contents, divider and closing pages retain their native
furniture; content examples are starting compositions whose regions may move, resize
or be replaced when the actual information shape needs it. A page may adapt a suitable
example or compose freely inside the house style when no example fits."""

HOUSE_NUMBERS = """

Measured off that template, and binding on every page you touch:
{lines}
The template's structural cover, contents, divider and closing are cloned rather than drawn,
so their arrangement is not yours either."""


def intake_brief() -> str:
    return INTAKE_BRIEF


def page_brief(*, body_pt: float, min_pt: float, house_style: bool = False, house: str = "") -> str:
    brief = PAGE_BRIEF.format(body_pt=_trim(body_pt), min_pt=_trim(min_pt))
    return brief + _house(house_style, house)


def deck_brief(*, body_pt: float, min_pt: float, house_style: bool = False, house: str = "") -> str:
    brief = DECK_BRIEF.format(body_pt=_trim(body_pt), min_pt=_trim(min_pt))
    return brief + _house(house_style, house)


def _house(house_style: bool, house: str) -> str:
    if not house_style:
        return ""
    return HOUSE_STYLE + (HOUSE_NUMBERS.format(lines=house.rstrip()) if house.strip() else "")


def _trim(value: float) -> str:
    return f"{value:g}"
