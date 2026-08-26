# Tables: drawing one, and the shortcut for the ordinary one

Called from §6 of the skill.

## Two ways to put a grid on a page, and both are yours

**Draw the table.** Nothing about a grid is privileged: a table is columns of boxes
with copy in them and some painted rectangles between, built out of `Box`,
`text_size`, `lines_needed`, `write` and `plane` like everything else the page
carries. The whole recipe is below and it runs. It is not a fallback for when
something breaks — it is where a table starts, because what the grid should look like
is the page's own argument talking, and that argument is different on every page.

**`ppt_layout.table()`** is the shortcut for one of those shapes and one only: the
ordinary row-and-column comparison, where the reader scans down a column of like
values and reads across a row to a verdict. It takes the Office look off, and it is
genuinely good at the part that is arithmetic rather than design — it sizes each
column from what that column holds, measures each row from the lines its cells really
wrap onto, and spreads the rows into the box it was given. Where the page wants that
table, call it and spend the time on the argument instead. Where the page wants a
different table, the recipe below is the shorter path, not the harder one.

What is *not* yours is `add_table`. A bare `add_table` arrives with Office's own look
— a white hairline around every cell, banding on, and a header style that fights the
palette. Measured on a live deck: 25 cells, every one outlined, two columns filled in
colours the theme does not contain. That is what `native_table` reports. A screenshot
of a source table is no better: another typeface, and it cannot be reweighted around
the page's conclusion.

## When the page has its own grid, draw the page's

`table()` has one opinion about what a table looks like, and it draws that table
well. A grid is a design decision before it is a call, though: which column carries
the claim, whether the rows are rows at all, what the reader is meant to compare
first, how much air the comparison needs to stay legible. The dials further down vary
that one table — vertical rules, a column filled in a colour you pick, banding,
per-column alignment, a bigger header, heavier lines, taller rows — and a variation on
it is still it. Where the page's answer is a different table, draw the page's.

Reasons the page's answer is a different table:

- **this deck already has a table that looks like this one.** Repetition belongs ahead
  of every structural reason under it: two pages of the same helper's defaults read as
  one template's output whatever the figures on them say, which is the note decks come
  back with. A second table is a second design problem, not a second call — turn the
  rows into cards, the label column into a row of headings, the figures into marks,
  one wide comparison into two stacked halves. Look at what the deck has already put
  on a page before settling this page's grid;
- **merged cells** anywhere but a `group_rows` band;
- **a grouped header** — one label spanning three columns, with their own labels under it;
- **an icon, a swatch, a logo or a sparkline inside a cell**;
- **columns that are not one grid** — two sub-tables side by side sharing row labels;
- **a cell whose copy needs its own internal layout** — a figure over a caption, two
  lines set at different sizes, a value with its unit set quieter;
- **a row that is not a row** — a full-width note between two groups, a callout, a
  footnote rule;
- **anything where you want the rows to be a different shape than a rectangle** — cards
  in a grid read better than five rows for five options with three attributes each.

Cards, split regions, a reading set beside the grid: the forms to build one of those
out of are in [deck/build/references/layouts.md](deck/build/references/layouts.md),
and `table_size(rows, theme, box=band)` says where a grid would have ended before the
page is committed to one.

## Drawing one: the whole recipe

This runs. It measures its own rows, fills its box, right-aligns what should be
right-aligned, and does three things `table()`'s defaults refuse: a vertical rule,
zebra bands and a tinted column at once.

```python
from ppt_layout import Box, plane, write, text_size

SIDE, AIR = 0.14, 0.14                  # air inside a cell: sides, and ends
HEAD_RULE, ROW_RULE = 0.030, 0.014      # the two line weights, in inches


def hand_table(slide, box, rows, theme, *, shares, aligns, head_pt=15, cell_pt=14,
               tint_columns=(), band_rows=(), column_rules=()):
    """`rows` drawn into `box` as boxes, copy and painted rectangles."""
    font, han = theme["font_family"], theme["cjk_font_family"]
    total = float(sum(shares))
    widths = [box.w * share / total for share in shares]
    lefts, cursor = [], box.x0
    for width in widths:
        lefts.append(cursor)
        cursor += width

    # A row is as tall as the tallest thing in it, measured before anything is drawn.
    def height_of(line, size, bold):
        tallest = 0.0
        for value, width in zip(line, widths):
            tallest = max(tallest, text_size(str(value), width - 2 * SIDE,
                                             size=size, font=font, bold=bold).h)
        return tallest + 2 * AIR

    heights = [height_of(rows[0], head_pt, True)]
    heights += [height_of(line, cell_pt, False) for line in rows[1:]]
    # Spread whatever the box has over, so the table does not stop two thirds down.
    slack = box.h - sum(heights)
    if slack > 0:
        heights = [one + slack / len(heights) for one in heights]
    tall = sum(heights)

    # Grounds first, in the order they should stack: bands, then the tinted column
    # over them, then the vertical rules, then every word on top of all of it.
    for index in band_rows:
        top = box.y0 + sum(heights[:index])
        plane(slide, Box.at(box.x0, top, w=box.w, h=heights[index]), theme, tint="surface")
    for column in tint_columns:
        plane(slide, Box.at(lefts[column], box.y0, w=widths[column], h=tall), theme, tint="accent_soft")
    for column in column_rules:
        plane(slide, Box.at(lefts[column] - ROW_RULE / 2, box.y0, w=ROW_RULE, h=tall), theme, tint="grid")

    top = box.y0
    for index, line in enumerate(rows):
        head = index == 0
        for column, value in enumerate(line):
            cell = Box.at(lefts[column] + SIDE, top + AIR,
                          w=widths[column] - 2 * SIDE, h=heights[index] - 2 * AIR)
            write(slide, cell, str(value), size=head_pt if head else cell_pt, bold=head,
                  colour=theme["foreground"], font=font, cjk_font=han,
                  align=aligns[column], anchor="middle")
        top += heights[index]
        if head:
            plane(slide, Box.at(box.x0, top - HEAD_RULE / 2, w=box.w, h=HEAD_RULE), theme, tint="accent")
        elif index < len(rows) - 1:
            plane(slide, Box.at(box.x0, top - ROW_RULE / 2, w=box.w, h=ROW_RULE), theme, tint="grid")
    plane(slide, Box.at(box.x0, top - ROW_RULE / 2, w=box.w, h=ROW_RULE), theme, tint="grid")
    return Box.at(box.x0, box.y0, w=box.w, h=tall)
```

Called:

```python
rows = [
    ["指标", "2023", "2024", "2025", "同比"],
    ["月活跃开发者", "12,400", "31,800", "58,200", "+83%"],
    ["付费席位", "1,120", "4,050", "9,640", "+138%"],
    ["净收入留存", "104%", "118%", "127%", "+9pp"],
    ["单席位年费", "$228", "$249", "$249", "0%"],
    ["合计 ARR", "$0.26M", "$1.01M", "$2.40M", "+138%"],
]
drawn = hand_table(slide, body, rows, T,
                   shares=(1.6, 1.0, 1.0, 1.0, 1.0),
                   aligns=("left", "right", "right", "right", "right"),
                   band_rows=(2, 4), column_rules=(1,), tint_columns=(3,))
assert drawn.h <= body.h, f"the table needs {drawn.h:.2f}in and the region has {body.h:.2f}"
```

Six things in there are the decisions, and all six are yours to change:

| | |
| --- | --- |
| `shares` | the column proportions. Measure them off the content with `text_size(...).w` if you would rather not pick, but picking is fine — you know which column carries the argument |
| `aligns` | per column. Figures right, labels left, a short status word centred |
| `head_pt` / `cell_pt` | the header at the body's size or one step over it, never under |
| `SIDE` / `AIR` | how much the table breathes. `AIR` is the single number that decides whether it reads dense or open |
| `HEAD_RULE` / `ROW_RULE` | in inches, so 0.014in is about 1pt. Under about 0.010in a line stops resolving in the render and the reader sees nothing there |
| the grounds | bands, tinted columns, vertical rules — draw the ones the page needs and none of the ones it does not |

**Two things to get right when you draw by hand.**

*Order.* Everything painted goes down before any word does, or the ground covers the
copy. Within the grounds, bands first and column tints over them, or a band cuts the
tinted column into pieces.

*The measurement is an estimate.* `text_size` and `lines_needed` are a per-character
class average with the theme's face scaled in, not a font metric — right on about 92%
of the strings that wrap. Nothing here moves at render time, because every box is
placed absolutely, so a row cannot push the row under it down; what a bad estimate
costs you is a cell whose copy runs past the bottom of its own cell. `AIR` is the
reserve against that. Check the render.

## What `table()` does for the ordinary comparison

**Its case is the row-and-column comparison the reader scans** — like values down a
column, a row read across to a verdict, one measure reported for each of several
periods. For that table, everything above is done for you, plus the part that is
arithmetic rather than design: **it sizes each column from what that column holds**,
so a 22-character benchmark name gets the room it needs and a 4-character metric does
not. Doing that by hand is what one live run spent thirteen requests on — it shortened
"DAVIS J&F" to "DAVIS", tried `weights=(1.9, 1.25, 1.25, 1.3, 1.3)`, rebuilt,
shortened another header, rebuilt again. `weights` is still there for a column you
want wider than its content.

**Rows are measured, not assumed.** Each row is as tall as the lines its own cells
wrap onto at the widths the columns actually get. A declared row height is only a
floor: rows used to be sized from the type alone, the renderer grew every wrapped one,
and the dividers — free rectangles at the boundaries the arithmetic had named — stayed
where they were and came down through a row's copy. That was the `rule_strike`
reading. The rules are the cells' own borders now, so they sit on the boundary
wherever the boundary ends up.

**And the rows spread into the box.** A table whose content is narrower and shorter
than its region used to leave the bottom third of the page white; the slack now goes
onto the rows in equal parts, capped at one line's worth each so a three-row table
stays a three-row table instead of becoming three bands. `fill=False` turns that off.
It is still worth asking `table_size(rows, theme, box=band)` while the page is being
written: a four-column table of short values comes back narrow whatever you do with
the height, and the answer to that is more to say — a column of commentary, a `mark`
column, the rows you were going to summarise — not a wider table.

It also carries the five things a table has to be able to say: `emphasize_rows` and
`emphasize_columns` tint the row or column carrying the claim, `group_rows` turns a
row into a named band across the table, `indent_rows` steps a detail row in, and
`total_rows` sets a row bold under a rule. `marks` puts a rating, a state, a direction
or a share in a cell as a shape rather than a string (below).

Compose freely around it: a two-product comparison may be two parallel regions with
the same row labels rather than one table, and a final conclusion may sit on its own
baseline below.

Preserve units, scales, qualifiers, series meaning and source labels exactly.

## The dials, and when to turn them

The defaults are an argument, not a rule: no vertical rules, no banding, nothing
filled, one accent rule under the header, one hairline under the last row, header and
body at one size. Every one of them is a keyword away from being something else, and
a page that needs the other thing should have it. **The reason a default is what it
is is written beside it, so that overruling it is a decision rather than a guess.**

| | | |
| --- | --- | --- |
| `size` | body type, 14pt | the floor is 14; going under it is refused elsewhere in the pipeline |
| `header_size` | `size` | raise it a step or two when the header is doing work — column headings that are questions, or a header over three-line cells. Never set it under `size` |
| `align` | measured per column | `("left", "right", "right", "center")`, one per column. Turn to it for a centred column, or where the measurement reads a column differently from the page |
| `rule_pt` | 2.25 | the accent rule under the header. Heavier where the table is the page; lighter where it sits beside a chart |
| `grid_pt` | 1.0 | the row hairline. It was 0.9pt and did not resolve at all in the render — do not go under 1 |
| `padding` | 0.03in | the air above and below a row's copy. The single number that decides dense against open |
| `row_height` / `header_height` | measured | a floor you set, in inches. A row still grows past it for copy that needs the room — a declared height cannot shrink a line |
| `fill` | `True` | `False` to keep the table at the size its content asks for |
| `column_rules` | `False` | off because alignment already separates columns and a full grid is the Office look. **On** where the columns are unrelated scales rather than one comparison, or where a matrix has so many that the eye loses which one it is in |
| `fills` | none | `{(row, column): colour}`, either coordinate `None` for all of them: `{(None, 3): "accent_soft"}` fills column 3, `{(2, None): "surface"}` fills row 2, `{(2, 3): "#FFEECC"}` one cell. Any role the theme carries, including one the deck named for itself, or a literal |
| `banding` | `False` | off because row spacing and alignment already tell the rows apart, and a tint on every other row fights whatever the template's own palette is doing. **On** for a long lookup table nobody reads straight through — twenty rows of figures somebody scans down for one line |
| `style` | `"minimal"` | the four below |

`fills` and `emphasize_columns` are two different sentences. `emphasize_columns=(3,)`
says *column 3 is the one that matters* and picks the accent for you; `fills` says
*column 3 is this colour*. Reach for the emphasis when the page has one claim, and
for `fills` when the deck has a palette of its own to spend.

`table_size` takes the ones that change the geometry — `size`, `header_size`,
`padding`, `row_height`, `header_height`, `indent_rows`, `fill`, `style`, `weights`,
`group_rows`, `marks` — so the height you ask for is the height of the table you then
draw. The ink-only ones (`align`, `rule_pt`, `grid_pt`, `fills`, `banding`,
`column_rules`, `emphasize_*`, `total_rows`) are not there, because they cannot
change it.

## Which row, which column

`emphasize_rows`, `indent_rows`, `total_rows` and the keys of `group_rows` are indices
into `rows` itself, so **row 0 is the header and none of them may name it**: the body
runs 1 to `len(rows) - 1`. `emphasize_rows=(0,)` does not tint the first line of data,
it raises *emphasize_rows names row 0; the body rows are 1 to 3 (row 0 is the header)*
-- the message names whichever argument you passed, so an index off by one comes back
as a sentence rather than as a page that looks nearly right. `emphasize_columns`
counts the other way, from 0 and over the header's own cells, so column 0 is the
label column and `len(header)` is one past the end.

**Alignment is read off the cells and you do not normally pass anything.** A column
every one of whose entries is a figure is right-aligned, because a column of numbers
that is not right-aligned cannot be compared down its length; a column of prose is
left-aligned. `numeric_from=1` used to be the default -- everything but the first
column right-aligned, on the assumption that a table is labels and figures -- and a
four-column comparison of sentences came out with three columns ragged down their
left edge, which was the single thing that made those tables look wrong.
`numeric_from` still takes a column index for the table the measurement reads
differently: the first column to right-align, `0` for the labels as well,
`len(header)` for none of them. `align` overrides both and names every column; a name
that is not `left`, `center` or `right` raises rather than being ignored. The header
takes its column's alignment rather than its own.

`group_rows` is `{row: "name"}`, and the row it names has to be a row you wrote into
`rows`: it is merged across the table and its own cells are discarded, so write it as
`[""] * len(header)` or as the name alone -- the table comes out the same width
either way, because a band never sets a column's width.

## The four styles

`style=` is one of four, and `table_size` takes the same argument. A name that is not
one of them raises *unknown table style 'banded'* before a cell is drawn.

| | |
| --- | --- |
| `minimal` | the default: an accent rule under the header, one hairline under the last row, nothing else |
| `header_tint` | the header row filled in `accent_soft` |
| `row_rules` | a hairline at every row boundary rather than only the last |
| `compact` | a lower floor under the rows -- 0.293in against 0.333in at 14pt |

`compact` buys less than its name suggests, and the floor is why: a row is never
shorter than the line box in it plus the cell's own margins, and it is never shorter
than the lines its copy actually takes either. So it saves 0.04in on a one-line row at
14pt and nothing at all on a row that wraps. It is worth having where the rows are
many, short and the type is small; a table that misses its region by more than that
wants a column dropped, `padding` lowered, or `table_size` asked earlier.

## What a cell cannot say in a string

A rating, a state, a direction and a share compare badly as text and well as
shapes. "3.5 / 5" down a column of criteria is a number the reader compares by
reading each one; five dots with three and a half filled is a length, and lengths
compare at a glance.

`mark(slide, box, theme, kind, value=None, *, colour=None)` draws one over the box
you give it — the cell of the table you drew, so it belongs with `write` and `plane`
rather than with any table object, and it is as available to a table you drew by hand
as to one `table()` drew. Seven kinds:

| `kind` | `value` reads | draws |
|---|---|---|
| `harvey` | a **rating**: `"3.5"`, `"3.5/4"` for a four-step scale, or `"75%"` of it — not a share, so `0.75` is refused rather than read as 0.75 of 5 | dots, filled to the rating |
| `status_dot` | — | a disc in `colour` |
| `delta` | a signed number (`"+18%"`, `"-4"`, `"0"`) | a triangle its way; a short bar at zero |
| `progress` | a share (`"76%"`, `"0.76"`, `"76"`) | a track with the share filled |
| `check` / `cross` / `partial` | — | supported / not / partly |

Nothing is red and nothing is green: a theme carries neither, and up is not good in
every column — an arrow on "open exceptions" means the opposite of one on
"availability". A mark states the fact in the page's own ink, and the accent stays
yours to spend on the row that carries the claim.

Leave the column wide enough for the mark before you draw it: a marked cell often
holds no string, and a column sized from its strings collapses to nothing. A rating
sets its dots at one sixth of the cell's width, so five steps want about an inch and
read small below half of one; a progress track wants three times the row's height.

## The same marks, keyed to a cell

`table(..., marks=...)` draws those shapes into the table it is already building,
keyed `{(row, column): "kind[:value][:colour]"}` on the conventions above -- so the
header takes none, and `{(0, 1): "check"}` raises *a mark at (0, 1) is outside the
body of a 4-row, 4-column table (row 0 is the header)*. A key that is not a pair of
numbers raises *a mark is keyed by (row, column)*, and a mark on a cell a ragged row
never wrote names the row and its length rather than coming back as an IndexError.

The spec is the kind and then up to two fields in either order, told apart by what
they look like rather than by where they sit, so `progress:76%:accent` and
`progress:accent:76%` are the same mark. A third field is refused, and so is a value
on one of the three kinds that read none: `check:ACCENT` raises rather than drawing a
tick in the default ink, because a misspelt role is the only intent in that string.

**A numeric kind given no value reads the cell's own string**, which is the reason to
key a mark to a cell rather than draw one over it. `{(1, 3): "delta"}` on a cell
holding "+4.7" states the direction and the figure once and they cannot drift apart;
the string stays in the cell. What the cell holds then has to be something that kind
can read -- `harvey` over a cell holding "82.1" raises *a rating of 82.1 is outside a
5-step scale*, and over an empty cell, *a harvey mark needs a rating, not ''*.

Two things this form does that a bare `mark` cannot. The marked column is widened to
hold what is in it, so the collapse above is already solved here: a five-step rating
took one table's 0.98in column to 1.34in and a ten-step scale took it to 2.49in. And
the mark takes the part of the cell the string does not need, on whichever side the
column's alignment leaves free -- a bar in a right-aligned column starts at the cell's
left edge, and in a left-aligned one it starts past the string, which is never given
more than three fifths of the width.

A marked row's strings still want to fit one line. The marks are placed on the row
heights the table measured, and a row measured at one line is where a mark and its
string were designed to sit beside each other.

Both forms read one colour vocabulary: any key the theme carries whose value is a
`#RRGGBB` string. So a deck that stated `ours` once in its palette can write
`mark(colour="ours")` beside the table and `{(1, 3): "delta:ours"}` inside it, and
the two come out the same colour. A literal `#RRGGBB` works in either. A key that is
not a colour is refused rather than reaching the renderer as one -- `mark`'s
`colour="font_family"` raises, and inside a spec a field that is no colour is read as
the *value* instead, which is where `harvey:4:ours` would land if `ours` were not one.

A field is told from its neighbour by what it looks like rather than by where it
sits, so `progress:76%:ours` and `progress:ours:76%` are the same mark.
