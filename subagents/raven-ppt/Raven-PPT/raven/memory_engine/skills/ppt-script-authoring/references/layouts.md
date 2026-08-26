# Page structures, and the layers you stack on them

Called from §3.5 and §8 of the skill. A registry of the shapes a page can take, whose job
is to make the list you choose from wider than the one you would have thought of. The
measured failure is not that a composed page comes out *wrong* -- it is that every
composed page in a deck comes out the *same*: one delivered 20-page deck drew eleven pages
and four of them were the same eight lines, a table, one rounded `plane`, three `points`.

**This is a registry and not a tutorial.** One line per entry, no worked code, no decision
tables: Part 1 is the **page structures**, the bones of a page, and Part 2 the **modifier
layers**, which stack on any structure. The two tables are the whole list -- there is no
count to carry and no entry hiding further down. A page is one or more structures plus any
number of modifiers, and `ppt_outline`'s `layout` field is where you write down which --
`"P14 + M4 + M11"`. The ids are stable names and not an order.

**The code lives in five passage files**, one per family, each loadable on its own: every
entry has a passage, run and its render looked at, and a page opens the one file it needs
rather than nine hundred lines it does not. The family rows below name the file, and
[the passages](#the-passages) at the foot of this page lists every id under its own.

## Part 1 -- page structures

| id | the page's bones | how, and what goes wrong |
|---|---|---|
| | ***a figure and copy, divided*** | passages: [deck/build/references/layouts-figures.md](deck/build/references/layouts-figures.md) |
| `P1` | figure left, copy right | cut the figure's column to the figure with `picture_size` before writing beside it; a column cut by eye is a strip of white over the figure with the caption stranded under it |
| `P2` | figure right, copy left | the mirror, and not the same page: the eye lands left first, so this is the one for when the argument leads and the figure corroborates |
| `P3` | a figure band across the top, copy in columns under it | for a figure that is wide and short. Give the band a share of the region and cut it to the figure inside that share, or the figure takes the region and `rest()` refuses with nothing left |
| `P4` | one sentence at lead size, the figure under it | the page where the figure *is* the argument |
| `P27` | serpentine: three rows, the figure changing side | the zigzag is the reading order. `picture_fit` per row, so the rows are deliberately not level -- cover-cropping figures to level them throws evidence away |
| | ***a figure as the page's surface, native shapes on top*** | the family that opens the most room and the one most likely to be skipped: the figure carries the world, and the cards, badges, leader lines and labels drawn over it carry the information, stay editable and take the deck's palette -- passages: [deck/build/references/layouts-figures.md](deck/build/references/layouts-figures.md) |
| `P5` | the figure as the page's ground, notes laid over it | put the notes in the figure's calm region and size them with `card_size`, not with a fraction of the page: two cards stretched to half the height read as one panel |
| `P6` | numbered hotspots on the figure, the legend down the side | the hotspots go on fractions of the box `picture_fit` hands back, which is the figure's real extent -- a fraction of the region puts them in the white space beside it |
| `P7` | one thing at the centre, leader lines out to what it reaches | `connect` picks its own edges from where the two boxes sit, so a hub needs no coordinate. Spokes in `MUTED`; in `grid` they disappear |
| | ***a photograph as the page*** | passages: [deck/build/references/layouts-figures.md](deck/build/references/layouts-figures.md) |
| `P23` | a photograph as the whole page, the title floated on it | `cover` (`M12`) to the canvas, a two-stop scrim (`M15`), the title stack wholly inside the scrim's opaque end. A cover, a section opener, a closing page |
| `P24` | an image belt across the middle, copy above and below | the belt runs edge to edge; fade both its long edges into the page (`M24`) or the seam reads as a rule someone drew |
| `P25` | a narrow full-height image strip beside a display-size title | under a quarter of the width. The strip carries the page's tone, not its information |
| `P26` | a figure running to the canvas edge, the copy in the clear | `cover` to a box that *ends at* the edge. Do not extend the shape past it -- `off_page` reports every shape that crosses one, and the reading is identical |
| `P35` | a chapter banner: two images of unequal weight over an oversized section number | for a divider page. The number is a graphic element and still measured type; the passage says what each tint costs |
| | ***more than one figure*** | passages: [deck/build/references/layouts-multiples.md](deck/build/references/layouts-multiples.md) |
| `P8` | small multiples: one row, one framing, one caption block each | not a grid of unrelated pictures. The identical framing is the message -- the reader compares because nothing but the content differs |
| `P9` | two columns on one baseline | before and after, ours and theirs. Cut both panels to the taller of the two, and give the side carrying the answer `accent_soft` (`M3`) |
| `P10` | one dominant figure, the supporting ones beside it | unequal on purpose. A page where every region carries the same weight has argued nothing |
| `P28` | an asymmetric collage: one dominant figure, smaller ones over its corner | `M21` and `M18` are what make it a stack of prints rather than a mistake. Keep each overlap well under 60% of what is under it, or `covered_shape` refuses the deck |
| `P29` | picture in picture: the detail inset over the wide shot | the inset's own label goes above it, where the wide shot is not behind the words |
| `P30` | the same figure twice: the whole of it, and a zoom on the part under discussion | a second copy of the same file cropped to the region, `M22` on the original, `connect` between the two. No second asset, and nothing cropped out of the evidence |
| `P31` | a montage of figures under one band of type | one translucent band across the whole montage, the type in `background`. The band is the page's sentence and the tiles are what it is about |
| | ***grids, rails and cards*** | passages: [deck/build/references/layouts-multiples.md](deck/build/references/layouts-multiples.md) |
| `P11` | an equal grid of cells | `box.grid(cols, rows)` is row-major. Level the row with `card_size`, or every card is as tall as the region and two lines of copy sit in a void |
| `P12` | a grid with one cell given to copy | the missing tile is what makes the grid a composition instead of a contact sheet. Do not fill every slot because there is a grid |
| `P13` | a full-height rail down one side | the rail carries the header, so this page builds its own `Frame`. A deck may do that; what it may not do is give each page a different header |
| `P32` | image navigation cards: a contents page whose entries are pictures | one card per section, a flat plate at the foot of each for the type (`M14`) and a short gradient above the plate to lose its edge |
| `P33` | a side hero image with staggered evidence cards opposite | stagger the cards' left edge. A rigid column beside a hero is two grids on one page |
| `P34` | an ambient banner over an evidence figure, the copy in a panel beside | the banner gives the page a place, the figure gives it a number, the panel holds the conclusion. Useful when one image sets the scene and another proves the claim |
| | ***charts as the page's bones*** | passages: [deck/build/references/layouts-data.md](deck/build/references/layouts-data.md) |
| `P14` | a chart with its reading in a lane beside it | a chart alone states numbers; the lane says what to conclude. Pair it with `M11` and accent the item the lane is about |
| `P15` | two charts read against one scale | `axis_max` on both, or the reader compares two pictures that are not comparable |
| | ***tables as the page's bones*** | one structure used to stand for all of them, and every deck's tables came out looking alike -- passages: [deck/build/references/layouts-data.md](deck/build/references/layouts-data.md) |
| `P21` | a table with its reading beside it | `table_size` says where the table ends before a cell is drawn, so the lane starts in the right place. [deck/build/references/tables.md](deck/build/references/tables.md) has the rest |
| `P36` | the table as the whole page, the conclusion set over it | for six rows or more, where the numbers *are* the argument. `table_size(rows, T, box=room)` before a cell is drawn, because the row count is what picks the type size; the conclusion on one line above it, and `emphasize_rows` on what that line names. Skip it for four rows that need explaining -- `P21`'s lane has the room to say why, and this page has none |
| `P37` | a two-axis matrix, the cell being the answer | rows are the options, columns the criteria, and the cells are *empty*: `marks` fills them with a tick, a cross or a half-dot, and a row of those is read across far faster than a row of the words for them. Wants `weights`, or the matrix sizes itself off its labels and sits in half the page, and a stated legend. Skip it when both axes are continuous -- that is `matrix_2x2` in [deck/build/references/charts.md](deck/build/references/charts.md) -- or when the comparison is one number per row, which is `P21` |
| `P38` | the rows dealt out as cards in a grid | five options across three attributes, one card each, the header becoming the field order every card repeats -- which is the whole difference from `P11`, whose cells are unrelated. Level them with `card_size`. Skip it past four attributes, and skip it when the values are figures meant to be compared down a column: a grid cannot be scanned that way and `P36` can |
| `P39` | a grouped header spanning columns, sub-labels under it | two levels of header, for when the same field names repeat under two groups. `table()` has no column spans, so the cells are drawn -- `columns(n, gutter=0.0, weights=)` for the grid, a `stack` for the bands -- and `rule` is capped at 1.05in, so the full-width rules are thin `plane`s. Skip it if one level of header will do: drawing by hand gives up every dial `table()` has |
| `P40` | a table and a chart of the same numbers, on one scale | the table is the reading and the bars are the shape: `axis_max` fixed on the chart, one order, the same item accented in both. They will not line up -- a chart lays out its own rows -- so pair them by order and draw no rule implying more. Skip it when the exact figures do not matter (`P14`), or when one bar per cell will do (`M25`) |
| `P41` | a statement: groups, indented detail, totals under a rule | `group_rows` for the section bands, `indent_rows` for the detail under them, `total_rows` for the added-up row, `align` because a column of figures that is not right-aligned cannot be totted up down its length. The one shape where a total is structural. Skip it where nothing is summed -- the bands then divide rows that were never a group |
| | ***sequence*** | passages: [deck/build/references/layouts-type.md](deck/build/references/layouts-type.md) |
| `P16` | a timeline spine | `timeline` hands back a `Track` whose stops carry `box` under the spine and `above` over it. Fill the rest of the page: a spine alone is a third of a page of content |
| `P17` | a chevron process row | five rectangles with gaps between them is a list; five chevrons that interlock is a process. [deck/build/references/shapes.md](deck/build/references/shapes.md) has the rest |
| | ***pages that are mostly not there*** | passages: [deck/build/references/layouts-type.md](deck/build/references/layouts-type.md) |
| `P18` | negative space dominant | content under 40% of the canvas and the air is the design. `excessive_whitespace` exists to catch this happening by accident, so spend it on a page with one thing to say and never as a way of stopping early |
| `P22` | a typographic page | no panel, no card, no figure: the sentence at display size and one quiet line under it. One of these, where the argument turns, is the page a reader remembers |
| | ***numbers as the page*** | passages: [deck/build/references/layouts-type.md](deck/build/references/layouts-type.md) |
| `P19` | the number at display size | with the reasoning beside it rather than under it. `NUMBER_PT` is the ramp's step for this and a display number may go above it |
| `P20` | a metric row across one band | three to five numbers on one line, each with its own label and icon, and the band's own reading under it. The icons are the difference between this and four boxes with numbers in them |

## Part 2 -- modifier layers

case, not the exception.** None is decoration: each says something the boxes alone do not.


| id | the layer | how |
|---|---|---|
| | ***type, marks and grounds*** | passages: [deck/build/references/layouts-multiples.md](deck/build/references/layouts-multiples.md) |
| `M1` | an icon at the head of a card, a point or a row | `card(..., icon=)` owns the geometry; `add_icon` for the ones not on a card. Pick it for what the region argues, not for a noun in its title. [deck/build/references/icons.md](deck/build/references/icons.md) |
| `M2` | a tinted ground under a region | `plane` in `surface`, `radius=True` for a card corner -- grouping, so that a zone is visible rather than implied |
| `M3` | `accent_soft` on the one region that carries the answer | the same `plane`, one tint up, on exactly one region of the page. Two of them and neither is the answer |
| `M4` | a hairline under a heading or beside a number | `rule` -- 0.06in below the box it underlines and at most 1.05in long, and horizontal: a vertical divider is a narrow `plane`. Not under every title |
| `M5` | a numbered badge | `preset(slide, dot, T, "ellipse", tint="accent")` with the numeral written on it in `T["background"]`. What turns a list into an ordered one a reader can point at |
| `M6` | a caption under a figure | `picture_fit(..., caption=)`. Every placed figure carries one, and it says what is shown rather than a claim the pixels do not prove |
| `M7` | a leader line from a note to the thing it annotates | `connect`, whose `kind` is "straight", "elbow" or "curved" -- it picks its own edges from where the two boxes sit, and either end may be an `(x, y)` point, for the arrow that goes to a place rather than to a region |
| `M8` | a mark: a rating, a delta, a share, a verdict | `mark(slide, box, T, "progress", "0.62")` and its six other kinds. A length compares at a glance where a number has to be read. [deck/build/references/tables.md](deck/build/references/tables.md) |
| `M9` | a kicker over a region | one line at `KICKER_PT` in `MUTED`, naming what the region below it is. `page()` gives the page's own; a band inside the page can have its own too |
| `M10` | an outlined frame around a region | `preset(slide, box, T, "roundRect", tint=None, outline="accent")` -- draws the eye to one part without painting over it |
| `M11` | one item brought forward in a chart | `accent=` on any chart form, by label or index. Everything else goes quiet, which is what makes the accented one mean something. [deck/build/references/charts.md](deck/build/references/charts.md) |
| | ***what happens to a picture*** | every one of these was checked against what python-pptx can actually do; where it needs raw XML the passage says so and shows the three lines -- passages: [deck/build/references/layouts-figures.md](deck/build/references/layouts-figures.md) |
| `M12` | a figure that fills its region instead of being centred in it | `cover`: place at the box's own width *and* height, then trim `crop_left`/`crop_right` (or `crop_top`/`crop_bottom`) to give the visible part its aspect back. For a photograph. A figure that is evidence loses part of itself under this and stays in `picture_fit` |
| `M13` | a picture clipped to a shape rather than to its rectangle | `clip`: set `prst` on the picture's own `a:prstGeom` -- `ellipse`, `roundRect`, `hexagon`, any preset -- and add an `a:gd` named `adj` for the corner radius. Raw XML, three lines |
| `M14` | a flat plate under type that has to sit on a picture | `rect` from `ppt_charts`, with an opacity **under 0.8**: at 0.8 and above the fill reads as opaque, and a picture more than 60% under it is refused as `covered_shape` |
| `M15` | a two-stop scrim: opaque where the type is, clear over the subject | `scrim`, which writes `a:alpha` into the two gradient stops. `gradient_angle` puts the first stop at the left at 0, the bottom at 90, the right at 180 and the top at 270 |
| `M16` | a picture pulled toward the deck's own palette | `rect` in `accent` at a fifth or a quarter for a tint; `duotone`, an `a:duotone` in the picture's `a:blip`, to re-grade it into two of the theme's colours outright |
| `M17` | the picture's own transparency | `fade`, an `a:alphaModFix` in its `a:blip`. This is the watermark and the texture wash, and the reason to prefer it: the picture fades and nothing is laid over it, so no check reads it as a cover |
| `M18` | a drop shadow under an image panel | `lift`, an `a:outerShdw` in the picture's `a:effectLst`. `shadow.inherit = False` first, or there is no `effectLst` to put it in |
| `M19` | a thin matte frame on a figure | `.line` on the picture `picture_fit` hands back -- `picture_fit(...).shape.line`. One rule, one colour, and it keeps the figure whole |
| `M20` | a cutout PNG placed with nothing behind it | alpha in the file needs no help from the program: a fetched logo or mark lands on the deck's own ground with no white box around it. The worst thing to do with a cutout is box it |
| `M21` | a slight rotation | `.rotation` on the picture, two to five degrees, and it wants `M18` with it or it reads as a slip |
| `M22` | a lens rectangle over a sub-region | `preset(slide, box, T, "rect", tint=None, outline="accent")`, positioned on fractions of the box `picture_fit` handed back. Draws the eye to one detail without painting over the rest |
| `M23` | a vignette, or a spotlight | `vignette`, an `a:path` gradient. Only over a picture that is the page's ground: over a smaller one it is refused as `covered_shape`, because python-pptx states no alpha for a gradient fill and the check reads it as opaque |
| `M24` | an image edge dissolved into the deck's background | `scrim` whose far colour is `background` at full alpha. The picture stops having a rectangle, which is what makes a band read as part of the page |
| | ***what happens to a table*** | passages: [deck/build/references/layouts-data.md](deck/build/references/layouts-data.md) |
| `M25` | a whole column given to one mark kind | `marks={(row, 3): "harvey" for row in ...}` -- the length ranks the rows and the string stays, because a mark takes only the room its cell's own text does not need. A numeric kind given no value reads that string, so the rating is written once. `M8` is the same mark drawn on its own, off a table |
| `M26` | cells tinted by their own value | `fills={(r, c): "#RRGGBB"}`, the steps mixed from the page's ground toward its accent. `table` sets every cell's type in `foreground`, so the deep end of the ramp is whatever `contrast` says still carries it -- and the legend under the grid is what makes a tint a reading rather than a wash. For a grid of numbers and nothing else, `heatmap` in [deck/build/references/charts.md](deck/build/references/charts.md) carries its own scale |

## Composing with this

**Combine across structures.** A comparison (`P9`) whose two sides are each a chart with
its own reading lane (`P14`) is one page, not two patterns fighting. A grid (`P11`) whose
first cell is upgraded to the number at display size (`P19`) reads as one composition. A
side-by-side (`P9`) whose panels are cover-cropped and clipped (`M12` + `M13`) is the
same page with a finish on it. The reflex of one structure per page, no modifier, is what
leaves most of this registry unused.

**The failure this file exists for is the opposite of overreach.** It is a deck whose
composed pages all resolve to a bare `P1` or `P9` with no modifier at all -- one tinted
rectangle and three points, page after page, because that was the only shape anyone had
seen. If your pages' `layout` fields read like that, not one of the entries above was chosen.

**Type on a picture is measured, so treat it as a build step and not a finish.** The
ground under a text box is read off the render's modal pixel: a flat plate (`M14`) or the
opaque end of a scrim (`M15`) gives that crop one dominant colour and the reading is the
real one, while type laid straight onto a gradient makes every ground pixel slightly
different, the type's own colour becomes the mode, and the page comes back `unreadable`
at 1.0:1 -- blocking, on a page that looks fine. Keep the whole text block inside the
plateau, never in the transition.

**Declare what you used.** `ppt_outline` takes a `layout` per page -- the structure ids
and the modifier ids, `"P14 + M4 + M11"` -- and that declaration is what makes the choice
reviewable before anything is drawn. It is also measured: a deck whose composed pages
concentrate on one or two structures comes back as `layout_variety`, counted off the
built file's own shapes, so a declaration cannot answer for a page drawn some other way.

**None of this outranks §3.5.** The structure follows what the page has to say. A page
that ends up as prose because prose is the shape of the argument is a right answer, and
so is one that reuses the structure of the page before it because the two are a series
the reader is meant to compare. What is not an answer is every page taking the same shape
because no other shape was considered.

---

# The passages

Every id above has one. They are grouped by family into the five files below, and each
carries its id as its own heading -- `### P37` -- so a passage is found by the id the
`layout` field already names. All of them assume the setup block of the skill's §3 plus
`FACE`/`HAN`, `INK`/`MUTED`/`ACCENT`, `FIGURES` and `frame = page()`; each file says so
at its head.

### [deck/build/references/layouts-primitives.md](deck/build/references/layouts-primitives.md)

`cover`, `scrim`, `vignette`, `clip`, `fade`, `duotone`, `lift` -- the seven picture
treatments python-pptx has no API for, and the three things they cost on the checks. No
id of its own, and the first file to open when a page has a photograph on it: every
passage in the next two assumes these are defined.

### [deck/build/references/layouts-figures.md](deck/build/references/layouts-figures.md)

One figure and the copy beside it, a figure carrying the page as its surface, a
photograph as the page.

#### P1 -- Figure left, copy right
#### P2 -- Figure right, copy left
#### P3 -- Figure band across the top, copy in columns under it
#### P4 -- One line of copy, the figure under it
#### P27 -- Serpentine: three rows, the figure changing side
#### P5 -- Figure as the page's ground, notes laid over it
#### P6 -- Numbered hotspots on the figure, the legend down the side
#### P7 -- One thing at the centre, leader lines out to what it reaches
#### P23 -- A photograph as the whole page, the title floated on it
#### P24 -- An image belt across the middle, copy above and below
#### P25 -- A narrow full-height image strip beside a display-size title
#### P26 -- A figure running to the canvas edge, the copy in the clear
#### P35 -- A chapter banner: two images of unequal weight over an oversized section number
#### M12-M24 -- the picture layers, stacked

### [deck/build/references/layouts-multiples.md](deck/build/references/layouts-multiples.md)

Several figures on one canvas, and the grids, rails and cards that hold regions of equal
or deliberately unequal weight.

#### P8 -- Small multiples: one row, one framing, one caption block each
#### P9 -- Two columns on one baseline
#### P10 -- One dominant figure, the supporting ones beside it
#### P28 -- An asymmetric collage: one dominant figure, smaller ones over its corner
#### P29 -- Picture in picture: the detail inset over the wide shot
#### P30 -- The same figure twice: the whole of it, and a zoom on the part under discussion
#### P31 -- A montage of figures under one band of type
#### P11 -- An equal grid of cells
#### P12 -- A grid with one cell given to copy
#### P13 -- A full-height rail down one side
#### P32 -- Image navigation cards: a contents page whose entries are pictures
#### P33 -- A side hero image with staggered evidence cards opposite
#### P34 -- An ambient banner over an evidence figure, the copy in a panel beside
#### M1-M11 -- five of them at once, on top of `P9`

### [deck/build/references/layouts-data.md](deck/build/references/layouts-data.md)

Charts and tables as the page's bones. The table half is the larger one: how far
`table()`'s dials reach, and where a cell has to be drawn by hand.

#### P14 -- A chart with its reading in a lane beside it
#### P15 -- Two charts read against one scale
#### P21 -- A table with its reading beside it
#### P36 -- A table as the page's whole ground, the conclusion floated over it
#### P37 -- A two-axis matrix, the cell being the answer
#### P38 -- The rows dealt out as cards in a grid
#### P39 -- A grouped header spanning columns, sub-labels under it
#### P40 -- A table and a chart of the same numbers, on one scale
#### P41 -- A statement: groups, indented detail, a total under a rule
#### M25 -- A whole column given to one mark kind
#### M26 -- Cells tinted by their own value

### [deck/build/references/layouts-type.md](deck/build/references/layouts-type.md)

No figure and no grid -- a spine, a process row, a number at display size, a metric
band, and the two pages that are mostly air.

#### P16 -- A timeline spine
#### P17 -- A chevron process row
#### P19 -- The number at display size
#### P20 -- A metric row across one band
#### P18 -- Negative space dominant
#### P22 -- A typographic page
