# Page structures, and the layers you stack on them

Called from §3.5 and §8 of the skill. A registry of the shapes a page can take, whose
job is to make the list you choose from wider than the one you would have thought of.
The failure it answers is not that a composed page comes out *wrong* -- it is that every
composed page in a deck comes out the *same*.

**This is a registry and not a tutorial.** No worked code and no decision tables: Part 1
is the **page structures**, eleven skeletons with every id folded into the one it is a
variant of, and Part 2 the **modifier layers**, which stack on any structure. The two
tables are the whole list -- there is no count to carry and no entry hiding further down.
A page is one structure plus any number of modifiers, and `ppt_outline` is where you write
down which: `layout` takes the structure id and `layers` the modifiers. The ids are stable
names and not an order.

**The code lives in five passage files**, one per family, each loadable on its own: every
id has a passage, run and its render looked at, and a page opens the one file it needs
rather than nine hundred lines it does not. [The passages](#the-passages) at the foot of
this page lists every id under its file, so a passage is found by the id `layout` names.

## Part 1 -- page structures

**Eleven skeletons, and the forty-one ids are which one plus what changed.** Measured off
the forty-one pages themselves, drawn and rendered: `P1`, `P14`, `P21` and `P19` are one
division with a chart, a table or a number where the figure goes, and `P9`, `P15` and `P40`
are one. So the list to choose from is this table's eleven rows, and the id you write in
`layout` is the row plus the variant -- `P14` says "the two-column division, and the visual
is a chart", which is a sentence about the page and not a lookup.

The numbers are what the pages measured, not a house style: a share of the body's width or
height, so they hold at any canvas. Where a row gives a range, the range is what the drawn
pages spanned.

| the skeleton, and every id that is it | the bones, with the numbers | must not become |
|---|---|---|
| **a visual and its reading, in two columns** -- `P1` figure left; `P2` figure right; `P26` the figure bleeding to the page's edge; `P6` hotspots on the figure, the legend in the reading column; `P14` the visual a chart; `P21` a table; `P19` a number at display size; `P33` the reading column's cards staggered; `P34` the visual column two bands, an ambient one over an evidence one | two shares, and which one depends on what the visual is *for*. Evidence to be read -- a figure, a chart, a table -- takes **0.55-0.64** of the body's width and the reading the rest. A visual that is a mark rather than evidence takes **0.37-0.42** and the reading takes the larger share: `P19`'s number measured 0.37 against 0.61 of reasoning, `P33`'s hero 0.42. `GUTTER` between, and a mirror is the complement (`split_left(0.42)` puts the visual right at 0.58). The reading's bands `spread` to the visual's own bottom edge, so both columns end on one line -- measured level to 0.000in on all nine. Cut the visual's column to the visual with `picture_size`/`table_size`/`chart_size` **before** writing beside it | a picture in one column and unrelated bullets in the other. The reading has to name what the visual shows; a lane that would read the same with the visual removed is a caption, and the page is `P22` with a decoration |
| **bands across the page** -- `P3` a figure band over copy columns; `P4` one line at lead size over the figure | a statement band is **0.17-0.23** of the body's height and what it heads takes **0.53-0.77**; the row under it is 2-4 blocks at **0.32w** (three) or **0.23w** (four). `frame.laying(lead_h, figure_h)` spends the leftover between them | three bands of equal height. One band is the page's sentence and the rest serve it -- equal thirds say the page has three unrelated things on it |
| **two comparable things on one baseline** -- `P9` two columns of evidence; `P15` two charts on one scale; `P40` a table and a chart of the same numbers | **0.48-0.51** each, both cut to the taller of the two, `accent_soft` on exactly the side that carries the answer. Same row labels down both, same order, and one `axis_max` where they are charts | two panels whose rows are in different orders or whose labels differ. Then the reader is matching sentences by eye, which is the one thing this shape exists to remove |
| **an equal grid of cells** -- `P11` all cells the same kind; `P12` one cell given to copy instead; `P8` one row, one framing, small multiples; `P32` each cell a picture card; `P38` each cell a row of a table; `P20` each cell a number and its label | three columns are **0.32w** each and four are **0.23w**, two rows **0.41-0.47h**, `GUTTER` between -- which is `box.grid(cols, rows)`, row-major. `card_group` per band, or `card_size`, so a row levels itself; without it every cell is as tall as the region and two lines sit in a void | filling every slot because there is a grid. Six cells means the page had six things to say; `P12` gives one back to copy, and that missing tile is what makes a grid a composition instead of a contact sheet |
| **one dominant visual, subordinates with it** -- `P10` the small ones beside it; `P28` over its corner; `P29` one inset in it; `P30` the same asset again, cropped to the part under discussion | the dominant takes **0.53-0.61** of the body's width, the subordinates the rest, stacked. Any overlap stays well under **0.60** of what is under it or `covered_shape` refuses the deck; an inset's own label goes **above** it, where the wide shot is not behind the words | regions of equal weight. The unequal division is the argument -- it says which figure is the evidence and which corroborate. Three equal figures is `P8`, and it says something else |
| **a visual as the page's ground, type floated on it** -- `P23` the whole canvas; `P5` notes laid over it; `P25` a strip beside a display title; `P24` a belt edge to edge, copy above and below; `P31` a montage under one band of type; `P35` two of unequal weight over an oversized section number | the whole canvas (**0.62-0.83** of its height, and `P23` is the upper end), or a belt **0.19-0.24** of the canvas height, or a strip **0.23** of its width. `P35`'s two are **0.68** and **0.32** of the width at one height, and `P31`'s tiles **0.33** wide with the band **0.24** of the height over them. Place with `cover` to a box that *ends at* the edge -- never past it, `off_page` reports every crossing and the render is identical. The type sits wholly inside a scrim's opaque end (`M15`) or on a flat plate (`M14`) | type laid straight onto a gradient. The ground under a text box is read off the render's modal pixel, so a transition makes every ground pixel different, the type's own colour becomes the mode, and the page comes back `unreadable` at 1.0:1 -- blocking, on a page that looks fine |
| **a rail down one side** -- `P13` | the rail is **0.22-0.28** of the body's width and its full height, and it carries the page's header -- so this page builds its own `Frame` rather than calling `page()`. The cells beside it are an ordinary grid (**0.32w** for three) | a different header on every page. A deck may hand one page its own frame; what it may not do is stop having a house |
| **a spine with stops on it** -- `P16` a timeline; `P17` a process row; `P27` serpentine, the stops alternating side | 4-6 stops. The spine band takes **0.44-0.65** of the body's height and what it explains goes under it -- a spine alone is a third of a page of content. The stops need not be equal: `P17`'s five sit at **0.16-0.21w** on purpose, because the steps are not the same size | five rectangles with gaps between them. The spine is what makes them a sequence; without it they are `P11` with arrows drawn on |
| **a hub with spokes out to what it reaches** -- `P7` | the hub centred at **0.32w**, three to five spokes, `connect` picking its own edges from where the two boxes sit -- so a hub needs no coordinate. Spokes in `MUTED`; in `grid` they disappear | spokes to regions that do not answer the hub. Two spokes is a `P9`; a spoke per noun in the title is a diagram of the title |
| **the table as the page** -- `P41` groups, indented detail, totals under a rule; `P36` a conclusion band over it; `P37` a two-axis matrix whose cells are marks; `P39` a grouped header spanning columns | the table spans the box it is handed, in its columns' own proportions, and spreads its rows into it from five rows up -- so hand it `frame.body` and it runs margin to margin. Six rows or more for the whole page; four that need explaining is the two-column division with a lane | a table given the body with `weights` guessed to make up a difference the box already settles. And `P37`'s cells are *empty* -- `marks` fills them, because a row of ticks is read across far faster than a row of the words for them |
| **one statement, the rest air** -- `P18` content under 40% of the canvas; `P22` the sentence at display size with one quiet line under it | content under **0.40** of the canvas, the run centred with `frame.holding(*heights)` rather than left at the body's top. `excessive_whitespace` exists to catch this happening by accident | a page that ran out. This is the page a reader remembers and there is about one of them in a deck -- spend it where the argument turns, never as a way of stopping early |

## Part 2 -- modifier layers

Stack these on any structure above. The requirement is the skill's §3.5 -- every band of
the page declared, more than one layer on the region that carries the claim -- and none of
these is decoration: each says something the boxes alone do not.

| id | the layer | how |
|---|---|---|
| | ***type, marks and grounds*** | passages: [deck/build/references/layouts-multiples.md](deck/build/references/layouts-multiples.md) |
| `M1` | an icon at the head of a card, a point or a row | `card(..., icon=)` owns the geometry and `card_group` passes it through for a whole row or column -- a helper of your own that reads the items itself is how a delivered deck dropped the icon on seven groups; `add_icon` for the ones not on a card. Pick it for what the region argues, not for a noun in its title. [deck/build/references/icons.md](deck/build/references/icons.md) |
| `M2` | a tinted ground under a region | `plane` in `surface`, `radius=True` for a card corner -- grouping, so that a zone is visible rather than implied |
| `M3` | `accent_soft` on the one region that carries the answer | the same `plane`, one tint up, on exactly one region of the page. Two of them and neither is the answer |
| `M4` | a hairline under a heading or beside a number | `rule` -- 0.06in below the box it underlines and at most 1.05in long, and horizontal: a vertical divider is a narrow `plane`. Not under every title |
| `M5` | a numbered badge | `preset(slide, dot, T, "ellipse", tint="accent")` with the numeral written on it in `T["background"]`. What turns a list into an ordered one a reader can point at |
| `M6` | a figure's caption, in the page's foot | `footer(slide, frame.footer, T, note="来源：xxx；图 1 主街三段式动线")` -- the source and the figure notes on one line, joined with `；`, and the height that frees goes back to the figure. Every placed figure is accounted for and each says what is shown rather than a claim the pixels do not prove. **Not** `picture_fit(..., caption=)`: a line of small type under every picture puts a second row of furniture in the middle of the body, and on a page with two figures it puts two. A number or a label *on* the figure is not a caption and stays where it is |
| `M7` | a leader line from a note to the thing it annotates | `connect`, whose `kind` is "straight", "elbow" or "curved" -- it picks its own edges from where the two boxes sit, and either end may be an `(x, y)` point, for the arrow that goes to a place rather than to a region |
| `M8` | a mark: a rating, a delta, a share, a verdict | `mark(slide, box, T, "progress", "0.62")` and its six other kinds. A length compares at a glance where a number has to be read. [deck/build/references/tables.md](deck/build/references/tables.md) |
| `M9` | a kicker over a region | one line at `KICKER_PT` in `MUTED`, naming what the region below it is. `page()` gives the page's own; a band inside the page can have its own too |
| `M10` | an outlined frame around a region | `preset(slide, box, T, "roundRect", tint=None, outline="accent")` -- draws the eye to one part without painting over it |
| `M11` | one item brought forward in a chart | `accent=` on any chart form, by label or index. Everything else goes quiet, which is what makes the accented one mean something. [deck/build/references/charts.md](deck/build/references/charts.md) |
| | ***what happens to a picture*** | where one needs raw XML the passage says so and shows the three lines -- passages: [deck/build/references/layouts-figures.md](deck/build/references/layouts-figures.md) |
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
rectangle and three points, page after page. If your pages' `layout` column reads like
that, or their `layers` are empty, not one of the entries above was chosen.

**Type on a picture is measured, so treat it as a build step and not a finish.** The
ground under a text box is read off the render's modal pixel: a flat plate (`M14`) or the
opaque end of a scrim (`M15`) gives that crop one dominant colour and the reading is the
real one, while type laid straight onto a gradient makes every ground pixel slightly
different, the type's own colour becomes the mode, and the page comes back `unreadable`
at 1.0:1 -- blocking, on a page that looks fine. Keep the whole text block inside the
plateau, never in the transition.

**Declare what you used.** `ppt_outline` takes three fields per page: `layout`, **one**
structure id from Part 1 (`"P14"`); `layers`, the modifier ids stacked on it
(`["M4", "M11"]`), one for every band the page divides into (§3.5); and `anti_pattern`,
the way this page would go wrong, in a line. Both id fields are closed lists -- a run
that wrote `P01`, `P04`, `P07` into the free-text field it used to be named nothing at
all, and its pages came out as nine variations on a card grid. That declaration is what
makes the choice reviewable before anything is drawn. It is also measured: a deck whose composed pages concentrate on one or two
structures comes back as `layout_variety`, counted off the built file's own shapes, so a
declaration cannot answer for a page drawn some other way.

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
at its head, and each says where `page().holding(*heights)` goes -- a passage that measures
its bands first cuts the body to them, rather than leaving the page's leftover at its foot.

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
#### P17 -- A process row
#### P19 -- The number at display size
#### P20 -- A metric row across one band
#### P18 -- Negative space dominant
#### P22 -- A typographic page
