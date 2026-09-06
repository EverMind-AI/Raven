# Tables: drawing one, and the shortcut for the ordinary one

Called from §6 of the skill.

## First: does this want to be a table at all

**A table is for figures.** Its whole value is that a reader scans *down a column* and
compares -- 93.05% against 66.80%, 200ms against 3000ms. That comparison is what the grid
buys, and it is the only thing it buys.

**Rows of words are not that.** Three rows reading "前史 / 1959-1998 / 生物启发 / Hubel &
Wiesel" are three *things*, not three readings of one measurement, and a grid around them
costs the page a third of its height and gives back nothing: no icon, no ground, no
scannable column, and a reader who has to take each row as a unit anyway. Those are cards
-- `card()` per thing, `card_size()` to level the row, `plane()` under the set (§7) -- and
the page fills instead of ending two thirds of the way down.

The test, before you write `table(`:

| The cells are | Draw |
| --- | --- |
| figures a reader compares down a column | a table |
| a state per cell that reads at a glance -- a tick, a cross, a filled scale | a table, with `marks` (below) |
| words, phrases, or a sentence per cell | **cards**, one per row |
| one word plus a sentence explaining it | **cards**, and the word is the card's title |

A column of "支持 / 不支持 / 部分" is the second row of that table only when it is drawn as
marks. Written as those words it is the third, and it is three cards.

**Everything below is for the first two.** The parameters are worth knowing and the calls
are worth copying, but a beautiful table around content that wanted cards is still the
wrong page.

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

What is *not* yours is `add_table`. A bare `add_table` arrives with Office's own look —
a white hairline around every cell, banding on, and a header style that fights the
palette. That is what `native_table` reports. A screenshot of a source table is no
better: another typeface, and it cannot be reweighted around the page's conclusion.

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
  one template's output whatever the figures on them say. A second table is a second design problem, not a second call — turn the
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
right-aligned, and does two things `table()`'s defaults refuse: zebra bands and a
tinted column at once.

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
class average with the theme's face scaled in, not a font metric. Nothing here moves at
render time, because every box is placed absolutely, so a row cannot push the row under
it down; what a bad estimate costs you is a cell whose copy runs past the bottom of its
own cell. `AIR` is the reserve against that. Check the render.

## What `table()` does for the ordinary comparison

**Its case is the row-and-column comparison the reader scans** — like values down a
column, a row read across to a verdict, one measure reported for each of several
periods. For that table, everything above is done for you, plus the part that is
arithmetic rather than design: **it sizes each column from what that column holds**, so
a 22-character benchmark name gets the room it needs and a 4-character metric does not.
`weights` is still there for a column you want wider than its content.

**A ground behind a table is cut to the table, never to the band.** `plane` at the band
and `table` in the same band is the commonest way to make a table look broken, because the
table is as wide as its content and as tall as its rows while the surface is as big as the
region -- measured on one band: the surface overhung the table by 2.87in to the right and
0.74in below, and the colour makes the mismatch loud where a bare table only looks narrow.
`weights` closes the horizontal half and not the vertical one. `table_size` answers both,
and it answers for the table that will be drawn as long as it is passed what `table` is
passed:

```python
laid = table_size(rows, T, box=band, marks=marks)          # the drawn size, both axes
plane(slide, Box.at(band.x0, band.y0, w=laid.w, h=laid.h), T, T["surface"])
table(slide, band, rows, T, marks=marks)
```

**A table fills the box it is given, so the box is where you say how wide it is.** It used
to stop at its content width whatever room it was handed: five real tables measured 43% to
92% of their box, and a delivered page had a five-column table end at 56% of a band whose
three cards above it ran to 94%. Now the columns keep their measured proportions and
divide the box, so a table under a full-width row lines up with it by being given the same
box -- and a table that should not run the page's whole width is a table given a narrower
box, never the whole body with `weights` guessed to make up a difference.

`weights` stays for the one thing the measurement cannot know: a column that has to be
wider than its own content. Four verdict columns that must read as equal, for instance --
sized from their own headers, `冷启动` came out 2.09in against 2.53in for the other three
and the ticks in it sat 0.22in off their neighbours:

```python
ppt_layout.table(slide, band, rows, T, weights=(2.0, 1.4, 1.6, 1.6, 1.8))
```

The numbers are relative, so they read as "the first column gets a fifth more than the
second"; only their ratios matter. And they are not free: measured on one reference page,
`weights` cut a header's column to 1.64in and wrapped it onto a second line, taking 0.17in
off every row below it. Ask `table_size(rows, T, box=band)` both ways and keep the one
whose columns you can defend.

**Rows are measured, not assumed.** Each row is as tall as the lines its own cells wrap
onto at the widths the columns actually get, and a declared row height is only a floor.
The rules are the cells' own borders, so they sit on the boundary wherever the boundary
ends up rather than where the arithmetic first named it -- which is what `rule_strike`
reports when a divider comes down through a row's copy.

**And the rows spread into the box.** Whatever slack a region has over the table's own
content goes onto the rows in equal parts, capped at two and a half lines each: rendered
at every row count over a 4.67in body, five rows and up fill it exactly, four stop 0.53in
short at 1.04in a row and still read as rows, and past that the leftover white is the
honest sign that a three-row table does not want a whole page. `fill=False` turns it off.

**So do not ask `table_size` in order to decide how much of a region to give the table.**
From five rows up the answer *is* `box.h`, and asking it about the whole body and then
putting a band above the table is circular -- the height came back for a region the table
is not going to be drawn in, and `take` refuses it at the band where the room runs out. A
live build failed four times on exactly that: a 4.22in answer measured against a 4.67in
body, drawn under a 0.62in band. Hand `table` the band and let it fill it, which needs no
measurement at all, or pass `box=` the band the table will be drawn in. `fill=False` is
the one case where this answers a height you can spend.

It also carries the five things a table has to be able to say: `emphasize_rows` and
`emphasize_columns` tint the row or column carrying the claim, `group_rows` turns a
row into a named band across the table, `indent_rows` steps a detail row in, and
`total_rows` sets a row bold under a rule in the muted tone, darker than the hairline
every boundary already carries. `marks` puts a rating, a state, a direction
or a share in a cell as a shape rather than a string (below).

Compose freely around it: a two-product comparison may be two parallel regions with
the same row labels rather than one table, and a final conclusion may sit on its own
baseline below.

Preserve units, scales, qualifiers, series meaning and source labels exactly.

## The dials, and when to turn them

The defaults are an argument, not a rule: no banding, nothing filled, a frame around
the table in the `muted` tone, a hairline at every row boundary and between every
column in the quieter `grid` tone, one accent rule under the header, and header and
body at one size. Every one of them is a keyword away from
being something else, and a page that needs the other thing should have it. **The
reason a default is what it is is written beside it, so that overruling it is a
decision rather than a guess.**

The frame and the interior hairlines are the defaults that are not restraints. Without
the frame the table drew a rule
under the header, a hairline under the last row and no side of any kind — three lines
each ending in mid-air, which reads as unfinished rather than as held back. Without the
row hairlines a four-column comparison whose cells wrap onto two lines said nothing
about where one row ended and the next began, and the reader was left counting
baselines to find which cell went with which label. Without the column rules a
five-column table of figures read "2012" and "8" as one cell and a three-column table
of centred phrases read as one run-on line, both on a delivered deck. So the outer
edge is closed and the rows and the columns are told apart for you, and a bare
`table(slide, box, rows, T)` is something a deck can
ship. It is not the Office look coming back: that is a hairline around every one of 25
cells with banding under them and a blue header over them, and this is one weight with
nothing under it. Two tones and not one: the frame separates the table from the page
and the hairlines separate one cell from the next, so the frame takes the `muted` tone and
the interior lines the `grid` one -- which is picked to sit under the copy and, drawn
around the outside, reads on a projector as no edge at all. `column_rules=False` takes
the vertical half of that interior off.

| | | |
| --- | --- | --- |
| `size` | body type, 14pt | the floor is 14; going under it is refused elsewhere in the pipeline |
| `header_size` | `size` | raise it a step or two when the header is doing work — column headings that are questions, or a header over three-line cells. Never set it under `size` |
| `align` | measured per column | `("left", "right", "right", "center")`, one per column. Turn to it for a centred column, or where the measurement reads a column differently from the page |
| `rule_pt` | 3.5 | the accent rule under the header. Heavier where the table is the page; lighter where it sits beside a chart, but never under `grid_pt` -- it is the line the table is read from |
| `grid_pt` | 2.5 | the weight of every line but the header's at once — the frame, a row hairline, a total's rule, a column rule. One weight, three tones: `muted` for the frame, `grid` inside, `muted` again under a total. In the `grid` tone a line under 2pt comes back from a 110dpi render as no line at all, so do not go under 2 |
| `padding` | 0.03in | the air above and below a row's copy. The single number that decides dense against open |
| `row_height` / `header_height` | measured | a floor you set, in inches. A row still grows past it for copy that needs the room — a declared height cannot shrink a line |
| `fill` | `True` | `False` to keep the table at the size its content asks for |
| `column_rules` | `True` | on because a centred cell has no visible edge to be centred against and two right-aligned figure columns run their figures together across a boundary nobody drew. It was off while alignment was held to separate the columns on its own; centring every non-numeric column ended that. **Off** where the table is read across one row at a time rather than down a column |
| `fills` | none | `{(row, column): colour}`, either coordinate `None` for all of them: `{(None, 3): "accent_soft"}` fills column 3, `{(2, None): "surface"}` fills row 2, `{(2, 3): "#FFEECC"}` one cell. Any role the theme carries, including one the deck named for itself, or a literal |
| `banding` | `False` | off because row spacing and alignment already tell the rows apart, and a tint on every other row fights whatever the template's own palette is doing. **On** for a long lookup table nobody reads straight through — twenty rows of figures somebody scans down for one line |
| `style` | `"minimal"` | the four below |

`fills` and `emphasize_columns` are two different sentences. `emphasize_columns=(3,)`
says *column 3 is the one that matters* and picks the accent for you; `fills` says
*column 3 is this colour*. Reach for the emphasis when the page has one claim, and
for `fills` when the deck has a palette of its own to spend.

`table_size` takes the ones that change the geometry — `size`, `header_size`, `padding`,
`row_height`, `header_height`, `indent_rows`, `fill`, `style`, `weights`, `group_rows`,
`marks` — so the height you ask for is the height of the table you then draw. The
ink-only ones (`align`, `rule_pt`, `grid_pt`, `fills`, `banding`, `column_rules`,
`emphasize_*`, `total_rows`) are not there, because they cannot change it. The frame and
the row hairlines are ink on that reading too: a border is drawn on the boundary and not
beside it, so it moves no glyph and no other rule. Raising `grid_pt` does not make the
answer stale, and neither did giving every style the row hairlines: the same rows come
back at the same height they did when only `row_rules` drew them.

## Three tables worth copying

Everything above is a dial and none of it is a table. Three whole calls, because a
comparison drawn from the defaults alone is legible and says nothing: it puts five
columns on the page without saying which one the page is about.

**The comparison, where one column is the claim.** The tint says where to look and the
last row is the one the argument rests on, so the reader arrives at both without being
told. Every cell is a reading of the same measurement, which is what makes the columns
comparable: a "相对劣势 48%" or a "基准线" among the figures costs that column its right
edge and the reader the comparison, and it is the first sign the page wanted cards.

```python
rows = [["指标", "EverOS", "传统 RAG", "全量上下文", "其他记忆基础设施"],
        ["LoCoMo 准确率", "93.05%", "48.30%", "—", "66.80%"],
        ["LongMemEval", "83.00%", "—", "—", "—"],
        ["检索延迟", "180ms", "350ms", "0ms", "1900ms"],
        ["Token 用量", "1.0x", "9.4x", "10.0x", "4.1x"]]
laid = table_size(rows, T, size=BODY_PT, padding=0.08)
table(slide, down.take(laid.h), rows, T, size=BODY_PT, numeric_from=1,
      emphasize_columns=(1,),      # our column, tinted -- not bolded, not coloured type
      total_rows=(4,),             # the row the claim rests on, bold under a rule
      column_rules=True,           # the boundary a centred label is centred against
      padding=0.08)                # the one dial that decides dense against open
```

**`column_rules` on every one of these**, and it is the default for the reason these
needed it: a table read *down* a column wants its boundaries drawn, and a centred cell
with no visible column edge does not read as centred at all -- measured: the label column
below is centre-aligned and looks arbitrary until the rule is there for it to be centred
against. The calls below still say it, because what a page is spending is worth reading
off the call; `column_rules=False` is the one that is now a decision.

**The table with sections.** `group_rows` is `{row: "name"}` and that row becomes one
band carrying the name -- its own cells stay empty. Reach for it when the *figures* fall
into two or three kinds and the kinds are part of the point; nine rows of numbers with no
bands are read as nine unrelated lines. The cells are still figures: a band does not turn
a column of sentences into a table.

```python
rows = [["模型", "参数量", "top-5 错误率"],
        ["2012-2014", "", ""],                 # the band's own row: empty cells
        ["AlexNet", "6,000 万", "15.3%"],
        ["VGG16", "1.38 亿", "7.3%"],
        ["2015-2017", "", ""],
        ["ResNet-152", "6,000 万", "3.57%"],
        ["SENet", "1.46 亿", "2.251%"]]
table(slide, box, rows, T, size=BODY_PT, numeric_from=1, padding=0.08,
      group_rows={1: "2012-2014", 4: "2015-2017"}, emphasize_columns=(2,),
      column_rules=True)
```

**The lookup table nobody reads straight through.** Twenty rows of figures somebody scans
down for one line: `row_height` stops the rows collapsing to their type and
`column_rules` is the line that parts the columns.

```python
rows = [["年份", "冠军", "top-5 错误率", "层数"], ...]
table(slide, box, rows, T, size=BODY_PT, numeric_from=2,
      row_height=0.46, column_rules=True)
```

`numeric_from=2` and not 1: it means "figures from this column rightwards", so it has to
name the first column that actually holds figures. Pointing it one column early
right-aligns a column of names against nothing, which is what `冠军` did.

`banding` is deliberately not here. Tinting alternate rows is the Office default this
module turns off on purpose -- it is the single thing that makes a table read as cheap,
and at twenty rows the row rules and the row height already give the eye its rail. Turn it
on only for a table long enough that a reader loses the line, and know that it is the look
you are spending.

**A fourth, with `marks`, which is the one way a non-numeric column earns a grid.** A
column of the words "支持 / 不支持 / 部分" is prose in a grid and wants cards. The same
column as a tick, a cross and a half-filled dot is a *state per cell that reads at a
glance*, which is what a figure does, and the row becomes scannable across. `column_rules`
earns its place here: the eye is crossing the row to compare two marks, and without a line
between the columns it has nothing to cross by. A marked column stays left-aligned so the
mark has the other side of the cell to sit in.

```python
ppt_layout.table(slide, band, rows, T,
      weights=(2.4, 1.6, 1.6, 1.4, 2.6), column_rules=True,
      marks={(1, 1): "check", (1, 2): "cross",
             (2, 1): "harvey:4", (2, 2): "harvey:2",
             (3, 1): "check", (3, 2): "partial"})
```

A mark takes the room the columns leave over, and `weights` leave none -- so with
weights declared, every column comes out exactly where you put it and each mark is drawn
inside its own column's share. Without weights the table widens by what the marks ask.
Either way the mark is drawn at the size of the type beside it, so the same table in a
tall band and in a short one has the same column widths.

**A mark cannot wrap, and copy can.** In a box too narrow for both, the columns keep
their content and the mark is what shrinks -- a ten-step scale in a 1.68in table comes
out a texture. That is visible in the render: a scale you cannot count the steps of
wants a wider box or fewer steps, not a keyword.

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
left-aligned. `numeric_from` still takes a column index for the table the measurement
reads differently: the first column to right-align, `0` for the labels as well,
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
| `minimal` | the default: the frame, an accent rule under the header, a hairline at every row boundary |
| `header_tint` | the same, with the header row filled in `accent_soft` |
| `row_rules` | the same table as `minimal`. The hairlines it used to switch on are what every style draws; the name still builds |
| `compact` | the same, with a lower floor under the rows -- 0.293in against 0.333in at 14pt |

All four draw the frame and the row hairlines; what varies is the header's surface and
the floor under a row. They are the table's own edges rather than a treatment, so no
style turns them off — a page that wants a table with neither is a page drawing its own,
which is what the recipe above is for.

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
hold what is in it, so the collapse above is already solved here. And the mark takes the
part of the cell the string does not need, on whichever side the column's alignment
leaves free -- a bar in a right-aligned column starts at the cell's left edge, and in a
left-aligned one it starts past the string, which is never given more than three fifths
of the width.

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
