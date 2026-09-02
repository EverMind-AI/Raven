# Charts, drawn as shapes

Called from §6 of the skill. The rules a drawn chart obeys stay there; this is the
vocabulary, the signatures, and -- from ["When none of the twenty-three is the shape of
the argument"](#when-none-of-the-twenty-three-is-the-shape-of-the-argument) -- the base
they are all built on, which is yours to draw with when none of them is the shape of
what the page argues.

**The twenty-three below are shortcuts, not a ceiling.** Reach for one when it is the
form your page wants; write your own when it is not. Picking the nearest of twenty-three
for an argument none of them makes is the failure this reference now has a second half
to prevent.

## The forms, by signature

| | |
| --- | --- |
| `column(slide, box, theme, data, *, accent=None, unit="", axis_max=None)` | `data` is `[(label, value)]` or `{label: value}` |
| `horizontal_bar(slide, box, theme, data, *, accent=None, unit="", axis_max=None)` | same `data`, in the order it should read — sort it yourself |
| `dot_plot(slide, box, theme, categories, series, *, accent=None, unit="", axis_max=None)` | `grouped_bar`'s arguments drawn as marks: a position per series per row, and twelve rows where four bars do not fit |
| `grouped_bar(slide, box, theme, categories, series, *, accent=None, unit="", axis_max=None)` | `series` is `[(name, values)]` or `{name: values}`, each as long as `categories`; `accent` names a **series** |
| `stacked_bar(slide, box, theme, categories, series, *, accent=None, unit="", share=False, direction="column")` | `share=True` normalises each category; `direction="bar"` is the 100% bar that replaces a pie |
| `marimekko(slide, box, theme, categories, series, *, accent=None, unit="")` | `stacked_bar`'s arguments again, each column as wide a share of the plot as its own total is of everything on it |
| `treemap(slide, box, theme, data, *, accent=None, unit="")` | `data` is `[(label, value)]`, every value above zero; the area is the share |
| `butterfly(slide, box, theme, data, *, sides=(), accent=None, unit="")` | `data` is `[(label, left, right)]`; `sides` names the two wings |
| `progress_bar(slide, box, theme, data, *, accent=None)` | `data` is `[(label, share)]`; a share reads `"76%"`, `"0.76"` or `76` |
| `bullet(slide, box, theme, data, *, accent=None, unit="", axis_max=None)` | `data` is `[(label, actual, target)]`, all on one scale |
| `dumbbell(slide, box, theme, data, *, sides=(), accent=None, unit="", axis_max=None)` | `data` is `[(label, before, after)]`; the first of the four axes here that do not start at zero |
| `line(slide, box, theme, categories, series, *, accent=None, unit="", axis_max=None)` | `categories` in the order they are read against; a mark at every value given and nothing interpolated between two of them |
| `combo(slide, box, theme, data, curve, *, sides=(), accent=None, unit="", curve_unit="", axis_max=None)` | `data` is the columns and `curve` is one value per column in a second unit; `sides` names the two scales |
| `waterfall(slide, box, theme, data, *, accent=None, unit="", totals=())` | `data` is `[(label, step)]`; `totals` names the levels, e.g. `(0, -1)` |
| `pareto(slide, box, theme, data, *, accent=None, unit="", threshold=0.8)` | sorted descending here; the cumulative share rides over the bars |
| `funnel(slide, box, theme, data, *, accent=None, unit="")` | `data` is `[(label, value)]` in the order the stages happen, and is never sorted here |
| `histogram(slide, box, theme, data, *, accent=None, bins=None, unit="")` | `data` is the observations, or bins you counted as `[(label, count)]` |
| `box_plot(slide, box, theme, data, *, accent=None, unit="", axis_max=None)` | `data` is `[(group, [values])]`, or the five numbers already worked out as `[(group, low, q1, median, q3, high)]` |
| `gantt(slide, box, theme, data, *, accent=None, ticks=None)` | `data` is `[(task, start, end)]`, ISO dates or numbers |
| `milestone(slide, box, theme, data, *, accent=None)` | `data` is `[(event, moment)]`, `gantt`'s two spellings of a moment; sorted into time order here |
| `heatmap(slide, box, theme, rows, columns, values, *, unit="", scale=None)` | `values` is row-major; `scale` fixes the `(low, high)` the tint spans |
| `matrix_2x2(slide, box, theme, data, *, axes=(), quadrants=(), accent=None, limits=None)` | `data` is `[(label, x, y)]`; `quadrants` reads top-left, top-right, bottom-left, bottom-right |
| `scatter(slide, box, theme, data, *, axes=(), accent=None, limits=None)` | `data` is `[(label, x, y)]`, or `[(label, x, y, magnitude)]` for bubbles |

`accent` names **which item** to bring forward -- by index, by label, or several of
either, and the label is one of the ones you just passed in `data`. It takes the
theme's accent and everything else goes quiet. It is never a colour: a `#RRGGBB` is
read as a label, matches no row, and comes back refused with the items it would have
taken instead. Which items those are follows the form -- the categories for the
one-series charts, the series names for `grouped_bar`, `stacked_bar`, `dot_plot`,
`line` and `marimekko`, the task names for `gantt`, the event names for `milestone`,
the stage names for `funnel`, the group names for `box_plot`, the part names for
`treemap`, the column names for `combo`, the point names for `scatter` and
`matrix_2x2`, and, `histogram` having no names of its own, the bin's index.
`pareto` sorts before it accents, so 0 there is the largest item rather than the
first one written. Name nothing and a single-series chart is drawn in
`chart_series[0]`; `grouped_bar`, `dot_plot` and `line` take the series colours in
order, and a stack takes one hue stepped from its deepest data paint to its palest
readable one, in the order the parts were given, because segments of one
whole read as one quantity divided rather than as unrelated categories. `funnel`,
`treemap` and `marimekko` are that same case and take the same line -- one quantity
whittled down, one whole cut into areas, one whole cut two ways. `combo` is the
exception: its curve is the accent, and where the columns are already painted that
colour it steps to the next series paint they are not using, because two units in
one paint is the reading the form exists to keep apart.

## What the other knobs set

`axis_max` raises the top of a scale and never lowers it, so no bar is ever cut
short: against `[185, 142, 128]`, `axis_max=100` draws the same three bars as no
`axis_max` at all. It is there for two charts on one page that have to be read
against each other. In the same 6.00x3.00in box, a `column` of `[185, 142, 128]` puts
60 at 0.79in and a `column` of `[60, 44, 31]` puts the same 60 at 2.43in; give both
`axis_max=200` and 60 is 0.73in on each. Where the bars are too narrow to carry their
own values it is the raised top that the scale reading states -- twelve columns
topping out at 111 are headed `111M` and, with `axis_max=400`, `400M`. `bullet` and
`grouped_bar` share a scale the same way; on `dumbbell`, `line`, `dot_plot` and
`box_plot`, whose axes do not start at zero, it raises the snapped top and leaves
the bottom where it was. On `combo` it raises the columns' scale and not the
curve's, which is snapped to its own data in `curve_unit` -- one knob moving two
readings is a page that cannot say which of them it set.

`limits` is `(x_low, x_high, y_low, y_high)` -- both ends of x, then both ends of y --
and any other number of values is refused saying so. Left off, the four are measured
off the data, padded a twentieth and snapped out to a round step, so x readings of 10
to 50 give an axis of 0 to 60. Giving them is how a `matrix_2x2` crossing becomes a
decided threshold instead of the middle of whatever was plotted: the cross sits at the
plot's middle either way, so `limits=(0, 100, 0, 1000)` puts the reading 50 on it and
an item at x=60 to its right, while `limits=(0, 200, 0, 1000)` puts 100 on it and that
same item to its left. Nothing is clipped to the ends you set. A point at x=500 under
`limits=(0, 100, 0, 1000)` is drawn at the inch that reading maps to, which measured
28.5in across a 13.3in page -- so limits narrower than the data are a decision to put
those points off the plot, and usually the wrong one.

`threshold` on `pareto` is the hairline drawn across the plot at that share of the
cumulative curve's axis, labelled with the share beside the right-hand percentages;
`threshold=None` draws neither the line nor the label. It is a share of the whole and
not a percentage -- `0.8` reads `80%`, and `80` reads `8000%` and lands the hairline
195in above the plot.

`bins` on `histogram` is how many equal-width bins to cut the observations into, not
where the edges fall. It applies only where `data` is the flat sequence of
observations; where `data` is `[(label, count)]` the binning is already done and
`bins` is ignored. Left off it is the square root of the number of observations, held
between five and twelve -- 25 observations take 5 bins, 100 take 10, 400 take 12. The
readings written under a binned histogram are the edges, one more of them than there
are bins, thinned to every second, third or fourth of them -- whatever the widest of
them needs against the width of a bin -- so a dense histogram still has a ruler.

`ticks` on `gantt` is how many marks the time axis is asked for. Five where it is left
off; an ask of one comes back as two and an ask of forty as eight. What is asked for
is not what is drawn, because a mark whose label would land on its neighbour's is
dropped and named in `drawn.readings_not_written` -- eight marks over a January-to-July
schedule in a 6.00in box came back as seven, `06-18` dropped.

## Asking by name, and reading the refusal

`the_smallest_box_a_chart_needs`, `whether_a_chart_fits` and `what_a_chart_will_do`
each take a chart's **name** as a string as readily as the function itself, which is
what a page whose own plan already holds the word "column" has to hand:
`the_smallest_box_a_chart_needs("column", T, rows)` returns the box
`the_smallest_box_a_chart_needs(column, T, rows)` does, and knobs pass through either
way. A name the module does not draw is refused with the near
misses -- `"collumn"` comes back `closest: column` -- or, with nothing close, the
whole list.

`TooSmall` carries three sizes and not one. `had` is the box the chart was handed,
`needs` is the box it wants, both `(across, down)` in inches, and `short` is the
difference with a side that was already big enough floored at zero. A three-task
`gantt` refused a 2.60x2.00in box needing 3.85x1.01in, so `short` was `(1.25, 0.0)`:
the height was never the problem, and the box plus `short` took the chart. `what` is
the chart's own name, which is what the message reads back.

## Annotating a chart you did not draw

A form takes no colour and no annotation, and that is not the end of what a page can
put on one. What comes back is a `Drawn`: it **is** the plot's rectangle, and it
carries `where`, the chart's own map from a reading to an inch on that plot. So a
threshold, a band, a note against one bar and a second series over the first are all
things you place *at a reading* rather than by dividing the box and hoping the two
agree:

```python
from ppt_charts import column, hline, write_label
from ppt_layout import Box, KICKER_PT

drawn = column(slide, cell, T, rows, unit="M")
target = drawn.where(120)                       # the inch 120 lands on
hline(slide, drawn.x0, drawn.x1, target, T["muted"])
write_label(slide, Box(drawn.x0, target - 0.22, drawn.x1, target), "plan 120M", T,
            size=KICKER_PT, colour=T["muted"], align="right")
```

`where(value)` is one inch for the forms with a single value axis and `where(x, y)` is
the pair for `scatter` and `matrix_2x2`; each form's own docstring says which. It is
the same function the chart drew with, so a rule at 120 is where the chart put 120 and
cannot drift from it. `what_a_chart_will_do` hands back the same `Drawn` with nothing
written, so the placement can be worked out before the chart is on the page.

The rest of the `Drawn` is what the chart gave up: `names_not_written`,
`readings_not_written`, `marks_not_to_scale`, and `nothing_was_dropped` for the three
in one boolean. A page that reads them knows what its own chart is missing without
counting a render -- and a reading the chart dropped is one to write yourself, at
`where` of the value it belonged to.

## Pick the form from the question it answers

**Reference — not a constraint.** Each row says what a form *encodes* — which
relationship between numbers it makes visible — read as *pick it for this; skip it when
that → draw the other instead*. It ranks nothing, recommends nothing, sets no threshold
and does not stand in for your reading of the data in front of you: the question is what
the page argues, not what shape the numbers happen to suggest, and a page that ends up
drawing no chart at all is an answer too.

| The page asks | Draw | Skip it when → instead |
|---|---|---|
| Which items rank highest, when the names read as words | `horizontal_bar`, sorted by value | the names are short enough to sit under a baseline → columns |
| What one value is per category | `column` | the category names will not clear each other across the axis → sorted bars; more than one series per category → grouped bars |
| How several series compare across the same categories (YoY, by segment) | `grouped_bar`: a gap between groups, none inside one | the split *inside* each category is the point → stacked bars; two states per item → gap rows; more rows than a bar can be deep in → `dot_plot` |
| How one quantity reads several ways, or many items read two or three ways | `dot_plot`: a mark per series on a shared scale, one row each | the lengths from zero are the argument → grouped bars, where a bar is the value |
| How far each item moved between two states | `dumbbell`: a mark at each state, the gap between them | more than two states, and ordered → `line`; unordered → grouped bars; a single snapshot → sorted bars |
| How two mirrored sides compare on one shared axis (A/B, cost vs revenue, pyramid) | `butterfly` | more than two sides → grouped bars |
| Which few items account for most of the total (80/20) | `pareto` | the cumulative share is not the claim → sorted bars alone |
| What the parts of one whole are | `stacked_bar(share=True, direction="bar")` with one category | the parts are too even for a segment to be told from its neighbour → sorted bars; too many or too uneven for one to carry its own name → `treemap` |
| How category totals split internally, totals still comparable | `stacked_bar` | only the totals matter → columns; only the mix matters → one 100% bar per category; the totals themselves are very unequal → `marimekko` |
| Which segments are big, and who holds what inside each | `marimekko`: the width is the segment's share of everything, the height its split | the segments are of a size → stacked bars, which do not spend width saying so |
| How one whole divides into very unequal parts | `treemap`: area is the share, largest first, kept near square | three or four parts of a size → one 100% bar, whose segments each carry their own name |
| How a starting value became an ending value through gains and losses | `waterfall` | there is no running total → columns |
| Which way a series moved along an ordered axis | `line`: a mark at every stated value, straight segments between, nothing interpolated | a second series in another unit → `combo`; unevenly spaced categories → columns |
| How a quantity and a second one in another unit moved together | `combo`: the first as columns from zero, the second as a line on its own scale, both ends of both written | both are the same unit → grouped bars, and no second axis to mistrust |
| When each task runs and for how long | `gantt` | the events have no duration → `milestone` |
| What happened when, where nothing has a length | `milestone`: a mark on one dated line, blocks alternating above and below it | the events run for a while → `gantt`, whose bars are that length |
| How a price moved open-high-low-close over dates | a thin rectangle for the body, a hairline for the range | only the close direction matters → a polyline |
| How observations spread across numeric bins | `histogram` | named categories → columns; the spread compared per group → `box_plot` |
| How the distribution differs per group — median, quartiles, outliers | `box_plot`: the middle half as a box, the median ruled through it, whiskers to Tukey's fence and a mark for anything past it | only the average per group matters → columns |
| Whether two numeric variables move together, or which points are outliers | `scatter`, both axes labelled, points labelled directly | a third magnitude also matters → the same marks sized by it |
| Where items fall on two dimensions at once | `matrix_2x2` | the quadrants are qualitative and hold text rather than values → named regions, not a plot |
| What the value is at every row-column intersection | `heatmap` | the rows are simply ranked → sorted bars |
| How far along each item is | `progress_bar` | each item has an explicit target as well as an actual → target ticks |
| How each KPI stands against its target | `bullet` | one metric only → the number at display size with its target beside it |
| Where an ordered run of stages loses what it started with | `funnel`: each stage as wide as its own value, what it kept written in the air between two of them | the stages carry no loss → numbered regions |
| What the exact values are, read off side by side | a drawn table (below) | a single comparison per row → bars, which the eye reads without arithmetic |

**How many categories a form takes is measured, not ranged.** A range would only ever
stand in for the same question -- whether *this* data's own labels have room -- and the
module answers that question directly, off the data you are about to pass and the box
you are about to pass it: `the_smallest_box_a_chart_needs` gives the smallest box this
chart takes **this** data in, `whether_a_chart_fits` answers the same thing against the
box you have, and `what_a_chart_will_do` hands back the same `Drawn` a real draw does,
so its `names_not_written` says which names would have nowhere to go if you drew it
anyway. Signatures in §4. Twelve categories named "Manufacturing" want 8.05x0.90in for
`column` and a quadrant of the page has 5.90x2.50in -- so that page draws a ranking,
decided before a shape is written rather than after a render.

## When none of the twenty-three is the shape of the argument

The table above is where to start and not where to stop. A form that encodes what your
page argues is a shortcut worth taking; a form that encodes something *near* it is a
page arguing the wrong thing in a tidy way, and the tidiness is what makes it hard to
notice. **Drawing your own is a design decision, not a rule broken.** What you may not
do is invent a palette, a type size or an axis while you are at it -- the base below is
public precisely so you do not have to.

### What the twenty-three cannot say

Each row is a claim with no form here, and the reason is a property of the forms rather
than of the data. Reaching for the nearest one anyway is how a page ends up saying
something it does not mean.

| The page argues | Why no form here says it |
|---|---|
| Two directions off a shared centre -- agree against disagree, over against under plan | `stacked_bar` stacks from zero and refuses negative parts; `butterfly` takes exactly one value per side, so a five-part answer cannot be split around its own midpoint |
| A quantity that holds a level and then steps -- a price schedule, a policy in force | `line` joins two marks with a straight segment, which claims the value moved between them; it did not |
| A reading with an interval around it -- a mean and its confidence bounds, p50 with p90 | `bullet` ticks one target and `box_plot` draws a *distribution's* quartiles off observations. An interval you computed drawn as a box plot reads as a spread that was measured |
| A path through two dimensions in time order -- a connected scatter | `scatter` places points and never joins them; `line` has one value per category and no second axis |
| Several dimensions read at once for two or three entities -- a radar | radial axes; nothing here leaves the cartesian plane |
| A flow that splits and rejoins -- a sankey, an allocation | `funnel` is one narrowing sequence and `waterfall` is one running total. Neither branches |
| A share as counted units -- "one in eight", a waffle | `progress_bar` fills a track, which is a length. A count of units is a count |
| Anything over orders of magnitude -- a log axis | every scale here is linear, which is what `span` and `linear` say in their names |
| A quantity as the area under a curve -- an area or stream chart | needs a closed filled polygon. That is `poly(fill=...)` below, and it is a primitive rather than a form |

Two that read like this list and are not. **Small multiples** -- the same form repeated
per segment -- are not a hand-drawn chart at all: `box.grid(cols, rows)` and one stock
form per cell, with `axis_max` shared so the cells are comparable. And **a table** is
often the honest answer where the page wants exact values rather than a comparison.

### The base the twenty-three are built on

Everything below is public on `ppt_charts` and imports the same way a form does. It is
the same code the forms call, so a chart you write is measured, painted and fitted by
the arithmetic that already decides those things for the deck.

The ink -- the only calls here that put a shape on the slide:

| | |
| --- | --- |
| `rect(slide, box, colour, opacity=1.0)` | a filled rectangle, no outline and no shadow: every bar, band, swatch and tick |
| `disc(slide, x, y, diameter, colour, opacity=1.0)` | a filled circle centred on a point -- a mark whose reading is where it sits |
| `ring(slide, x, y, diameter, colour)` | the same circle, open: the mark that is deliberately not carrying an area |
| `hline(slide, x0, x1, y, colour, thickness=HAIRLINE)` | a horizontal hairline with its **top edge** at `y`, so a bar drawn to `y` sits on it |
| `vline(slide, x, y0, y1, colour, thickness=HAIRLINE)` | a vertical hairline centred on `x` -- a centre line, a threshold, a tick |
| `poly(slide, points, colour, width=0.020, fill=None, opacity=1.0)` | a polyline through `points`; `fill` closes it and paints the inside, and `opacity` applies to that fill and not to the outline |
| `write_label(slide, box, text, theme, *, size=LABEL_PT, colour=None, align="left", anchor="top", bold=False)` | a label set in **both** of the theme's faces, so a Han character in a category name is not left to the renderer's fallback |

`disc` and `ring` are a pair, and which one you draw is a claim. A filled disc is read
as an area, so it is the mark for a plot where size means something and for a plain
point on a line; `ring` is the same mark at the same place with the fill taken off, for
the one that is deliberately **not** carrying an area -- a magnitude too small to draw
to scale, a reference point that is not part of the series. Drawing the second as a
disc is a size the reader believes.

**`opacity` is how two readings share the same inches.** It reads the three spellings a
share does -- `0.45`, `45` and `"45%"` are the same paint -- and 1.0, the default, is
what every form here draws. Below 1 the shape *layers* instead of stacking: a fill at
0.45 lets what is under it through, so a second filled polygon is a second polygon
rather than a replacement for the first, and a band over a plot marks a range without
erasing the marks inside it. Two things follow. **Draw order still decides** -- the
translucent one goes down last, because an opaque shape on top hides whatever the alpha
below it was for. And **`opacity` is not a way to soften a colour**: `shades` is, and a
shade is the same colour wherever it lands, while a translucent paint is a different
colour over every different thing it crosses. Reach for it to layer two readings on
purpose, never to get a paler bar.

The scale -- the half of a chart that must not be arithmetic by eye:

| | |
| --- | --- |
| `span(values, axis_max=None, axis_min=None)` | the ends of a scale that always contains zero, for the plots where a mark is a **length** |
| `snap(low, high)` | the ends widened to a round step, for the plots where a mark is a **position** and zero would flatten them |
| `linear(low, high, near, far)` | the map itself: hands back `at(value)`, the inch a reading lands on. `low` on `near`, `high` on `far` -- so a downward axis is `linear(low, high, plot.y1, plot.y0)` |

The palette -- where a colour comes from, so a page never types one:

| | |
| --- | --- |
| `series_paints(theme, names, accent)` | one paint per series: the accented one loud, the others quiet and still distinct |
| `stack_paints(theme, names, accent)` | one paint per part of a stack: depths of one line, because a stack is one quantity divided |
| `shades(theme, count, quiet=False)` | `count` paints off one line, **deepest first**, every one of them readable on the page. `quiet=True` is the line a chart uses for what it is not accenting |
| `emphasis(theme, labels, accent)` | `(fills, inks)` per item -- the whole of "only one thing is accented", done by construction |
| `ink_on(ground, theme)` | which of the page's two inks is legible on that ground: what a number written *inside* a segment is set in |
| `contrast(one, other)` | the WCAG ratio between two paints, which is the number the deck's own measurement reads off the render |

The fitting -- asked before anything is drawn:

| | |
| --- | --- |
| `type_face(theme)` | the face the deck is set in; every width estimate below takes it as `face=` |
| `text_width(texts, size, face=None)` | how wide a box must be for these strings to stay on one line -- the test for whether a reading fits its own segment |
| `pick_size(texts, room, largest=LABEL_PT, face=None)` | the largest step of the ramp these labels fit in `room`, or `None` when even the smallest will not: that is a label to drop, not a size to invent |
| `line_height(size)` | the height one line at that step needs, for reserving a band before you fill it |

The furniture, and reading a value:

| | |
| --- | --- |
| `key(slide, box, theme, names, paints, size=KICKER_PT)` | the swatch-and-name key, wrapped to the box -- for the plot no direct label can reach |
| `scale_top(slide, plot, theme, high, unit, size=KICKER_PT)` | the top of the scale on a hairline over the plot, which is what makes a length readable when the marks are too narrow to carry their own numbers |
| `number(value, label=None)` | a reading out of a number or a string with one in it (`"48.3%"` is `48.3`), refusing a boolean, a NaN and an infinity by name |
| `fmt(value, unit="", sign=False)` | the shortest string that is still the value: trailing zeros go, nothing else does, and never an exponent |

`TooSmall` and `Drawn` are yours to raise and to return, and doing both buys the rest of
the module. Raise `TooSmall(name, (box.w, box.h), (across, down))` where the box will
not read, hand back `Drawn(plot, at, size, readings=dropped)` where it will, and
`whether_a_chart_fits`, `what_a_chart_will_do` and `the_smallest_box_a_chart_needs` all
work on **your** function -- they run it against a slide that swallows shapes, which the
seven ink calls above already return early on. The `Drawn` also composes with everything
in ["Annotating a chart you did not draw"](#annotating-a-chart-you-did-not-draw).

### A worked example: two directions off one centre

The first row of the table above, drawn. Every number the reader takes off this page is
a length the code computed, every colour came out of `ppt_theme` through `shades`, and
the readings that would not fit their own segment are reported rather than shrunk. It
runs as it stands.

```python
from ppt_charts import Drawn, TooSmall, fmt, hline, ink_on, key, line_height, linear
from ppt_charts import number, pick_size, rect, shades, span, text_width, type_face
from ppt_charts import vline, write_label
from ppt_layout import Box, KICKER_PT, LABEL_PT


def diverging_bar(slide, box, theme, data, parts, *, unit="%"):
    """A survey read for and against: parts stacked either side of one centre line.

    `data` is [(question, [share per part])] and `parts` names them in order from
    the most negative to the most positive, with the middle one straddling the line.
    """
    face = type_face(theme)
    names = [str(name) for name, _ in data]
    rows = [[number(value, name) for value in values] for name, values in data]
    middle = len(parts) // 2

    lefts = [sum(row[:middle]) + row[middle] / 2 for row in rows]
    rights = [row[middle] / 2 + sum(row[middle + 1 :]) for row in rows]
    low, high = span([-max(lefts), max(rights)])

    size = pick_size(names, box.w * 0.32, LABEL_PT, face) or KICKER_PT
    names_w = min(box.w * 0.32, text_width(names, size, face))
    legend_h = line_height(KICKER_PT) * 2
    plot = Box(box.x0 + names_w, box.y0, box.x1, box.y1 - legend_h - line_height(KICKER_PT))
    if plot.w < 2.0 or plot.h < 0.5 * len(rows):
        raise TooSmall("diverging_bar", (box.w, box.h), (names_w + 2.0, 0.5 * len(rows) + legend_h))

    at = linear(low, high, plot.x0, plot.x1)
    pitch = plot.h / len(rows)
    bar_h = pitch * 0.62

    # One shade line each way off the centre: the quiet one carries the parts that
    # disagree and the neutral part at its pale end, the loud one carries the parts
    # that agree, deepest furthest out. Both are asked for one step more than they
    # keep, so neither side ends on the palest step the line has.
    quiet = shades(theme, middle + 1, quiet=True)
    loud = shades(theme, len(parts) - middle)
    paints = quiet + list(reversed(loud[: len(parts) - middle - 1]))

    vline(slide, at(0), plot.y0, plot.y1, theme["grid"])

    dropped = []
    for index, (name, row) in enumerate(zip(names, rows)):
        top = plot.y0 + index * pitch + (pitch - bar_h) / 2
        write_label(slide, Box(box.x0, top, box.x0 + names_w - 0.08, top + bar_h), name,
                    theme, size=size, colour=theme["foreground"], anchor="middle")
        cursor = -lefts[index]
        for part, value, paint in zip(parts, row, paints):
            here = Box(at(cursor), top, at(cursor + value), top + bar_h)
            rect(slide, here, paint)
            reading = fmt(value, unit)
            if here.w >= text_width([reading], size, face):
                write_label(slide, here, reading, theme, size=size,
                            colour=ink_on(paint, theme), align="center", anchor="middle")
            else:
                dropped.append(f"{name}/{part}")
            cursor += value

    hline(slide, plot.x0, plot.x1, plot.y1, theme["grid"])
    for value, align in ((low, "left"), (high, "right")):
        write_label(slide, Box(plot.x0, plot.y1 + 0.02, plot.x1, plot.y1 + line_height(KICKER_PT)),
                    fmt(abs(value), unit), theme, size=KICKER_PT, colour=theme["muted"], align=align)
    key(slide, Box(plot.x0, box.y1 - legend_h, plot.x1, box.y1), theme, parts, paints)
    return Drawn(plot, at, size, readings=dropped)
```

Called with five questions and
`["Strongly disagree", "Disagree", "Neutral", "Agree", "Strongly agree"]` in a
`frame.body`, it reported four readings it could not place -- the three-per-cent slivers
at the far left of three rows. That is the shape of the answer: the neutral part is
split down the middle, every row's midpoint is on one line, and which questions net
positive is readable across the column without adding anything up. The same data through
`stacked_bar(share=True, direction="bar")` is five rows of equal length whose agree
block starts at a different place on every one of them -- a correct chart of a different
claim.

### What a chart you drew still owes the deck

Freedom over the form is not freedom over the deck's look, and the measurements do not
care who drew the shape.

- **Colour comes out of `ppt_theme`, through the calls above.** `shades`,
  `series_paints`, `stack_paints` and `emphasis` are how; `theme["grid"]`,
  `theme["muted"]` and `theme["foreground"]` are the three roles a chart's furniture
  uses directly. A `#RRGGBB` you typed is a colour the deck does not contain, and it
  will be the one thing on the page that looks generated.
- **Type comes off the ramp.** `write_label` defaults to `LABEL_PT` and `KICKER_PT` is
  the step below it; `pick_size` is how you choose between them. Nothing goes under the
  floors in §3, and a label that will not fit at the smallest step is one to drop and
  report -- never one to set at 9pt.
- **A length starts at zero.** `span` is the call that guarantees it. `snap` is for the
  plots where every mark is a position and nothing is a length; using it for bars draws
  a bar out of proportion to its own number, which is the one defect no gate downstream
  will catch for you.
- **The label is on the mark, and what will not fit is reported.** Keep a `dropped`
  list and hand it back on the `Drawn`, the way the forms do. A number silently missing
  is worse than a number the page admits it could not place. Where the marks are too
  narrow for their numbers as a class -- twelve columns, four series deep -- the answer
  is not smaller type: `scale_top` writes the top of the scale over the plot on a
  hairline, and a length stays readable against it.
- **The gates still run on it.** Type landing on type, and content hidden behind a
  shape drawn after it, refuse the deck; type under the floors, type thin against its
  ground (under 3:1), a shape over the page edge, a filled colour bar carrying nothing
  and copy that does not fit its box are all reported against the page you drew it on.
  `contrast` and `text_width` are how you answer those before the render rather than
  after it.
- **The covering check does not read `opacity`.** Any fill that is not "none" counts as
  a shape that hides, so a translucent band over **copy, a picture or a table** still
  refuses the deck once it covers three fifths of one, however much of it a reader can
  actually see. Over bars, marks and rules it is fine -- those are not what that gate
  calls content -- which is where a band belongs anyway. Put the label for a band
  outside the band.
- **Look at it.** §10. A hand-drawn chart is the one thing on the page with no second
  opinion behind it.

### Still out of reach, and what replaces it

A **sector at a computed angle** is the one construction with no primitive: nothing here
draws an arc. A filled polygon is not one of them -- `poly(fill=...)` closes and paints
one -- so the rows below say what such a form is worth rather than whether it can be
reached at all.

| Wanted | Where it stands | Draw |
|---|---|---|
| pie, donut, pie-of-pie, bar-of-pie | an arc; unreachable | one 100% bar, segments labelled; a centre total becomes the number at display size beside it |
| gauge | an arc over a bounded domain; unreachable | the number at display size on a straight track, the threshold ticked |
| sunburst | concentric angular rings; unreachable | `treemap` of the one level the page argues about, which is the level a ring past the second stops being readable at anyway |
| radar | drawable with `poly(fill=..., opacity=...)`, and worth it for **two or three** entities | fill every series and layer them: the first opaque, each one after it around `0.45`, so the overlaps read as overlaps. Worked below. Past three the polygons stop being separable however they are painted -- that is a limit of the form, not of the paint -- and it is one bar row per dimension with entities as grouped bars |
| area, stacked area, stream | drawable with `poly(fill=...)` | fill it only where the **area** is the reading -- a volume accumulated, a composition over time. Direction alone is `line`, and an area under it claims a quantity the page is not making a claim about |
| sankey | ribbons: each is a filled polygon, so drawable | `funnel` where the flow is linear. A branching flow is a real use of `poly(fill=...)`, and it is a lot of code for a page that a table of magnitudes usually says better |
| word cloud | type-size packing; no | the ranked terms as sorted bars, which is what the weights were for |

### A second worked example: a radar, layered rather than stacked

A radar is the shortest thing that needs `opacity`, and it is where this reference was
wrong. It used to say to fill one series and outline the rest, on the grounds that the
export had no transparency. It has, and the old advice was working around a fill that
was simply left opaque. Every series here is filled, each one after the first at `0.45`,
and the deepest paint goes down first so the layers build outward.

```python
import math

from ppt_charts import Drawn, key, line_height, linear, number, poly, shades, write_label
from ppt_layout import Box, KICKER_PT


def radar(slide, box, theme, axes, series):
    """One closed polygon per entity over shared radial axes, layered by opacity.

    `axes` names the dimensions, clockwise from the top; `series` is
    [(name, [one value per axis])]. Deepest paint first, so each translucent layer
    lands on what is already there rather than replacing it.
    """
    radius = min(box.w, box.h) / 2 - line_height(KICKER_PT) * 1.8
    cx, cy = (box.x0 + box.x1) / 2, box.y0 + box.h / 2 - line_height(KICKER_PT) / 2
    high = max(number(value) for _, values in series for value in values)
    reach = linear(0, high, 0.0, radius)

    def corner(index, value):
        angle = -math.pi / 2 + 2 * math.pi * index / len(axes)
        return (cx + reach(value) * math.cos(angle), cy + reach(value) * math.sin(angle))

    for ring in (0.25, 0.5, 0.75, 1.0):
        web = [corner(index, high * ring) for index in range(len(axes))]
        poly(slide, web + [web[0]], theme["grid"], width=0.008)

    names = [str(name) for name, _ in series]
    paints = shades(theme, len(series))
    for index, ((name, values), paint) in enumerate(zip(series, paints)):
        corners = [corner(axis, number(value, name)) for axis, value in enumerate(values)]
        poly(slide, corners + [corners[0]], paint, width=0.022, fill=paint,
             opacity=1.0 if index == 0 else 0.45)

    for index, axis in enumerate(axes):
        x, y = corner(index, high * 1.18)
        write_label(slide, Box(x - 0.75, y - line_height(KICKER_PT) / 2,
                               x + 0.75, y + line_height(KICKER_PT) / 2),
                    str(axis), theme, size=KICKER_PT, colour=theme["muted"],
                    align="center", anchor="middle")
    key(slide, Box(box.x0, box.y1 - line_height(KICKER_PT), box.x1, box.y1), theme, names, paints)
    return Drawn(Box(cx - radius, cy - radius, cx + radius, cy + radius), reach, KICKER_PT)
```

Called with `[("v3", [72, 65, 48, 81, 55, 60]), ("v4", [88, 79, 63, 86, 74, 71])]` over
six axes, both shapes come off the raster: v3's deep fill reads through v4's pale one
everywhere they overlap, and the crescent where v4 reaches past v3 is the page's whole
claim. Filling both at 1.0 gives one polygon -- whichever went down last.

The other everyday use is a band across a plot. Drawn **after** the columns it crosses,
it marks a target range without erasing anything inside it:

```python
band = Box(plot.x0, drawn.where(85), plot.x1, drawn.where(65))
rect(slide, band, T["accent"], opacity="30%")
```

At 1.0 that is a block with four column tops missing. At 0.30 every column is legible
through it and which ones reach the range is the reading. Keep the band's own label
outside the band, for the covering check above.
