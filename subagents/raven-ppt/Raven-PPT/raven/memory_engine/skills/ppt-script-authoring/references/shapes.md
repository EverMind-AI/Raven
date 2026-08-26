# Processes, flows and timelines

Called from §4 and §7.5 of the skill.

## The presets and the two layouts built on them

| | |
| --- | --- |
| `chevron_row(slide, box, theme, n, *, adj=0.5, tint="accent", flat_start=True, outline=None)` | → `[Step(shape, box)]`, n interlocking chevrons filling `box`; `n` may be the labels instead, and `len` is the count |
| `timeline(slide, box, theme, n, *, tint="accent", arrow=True, colour=None)` | → `Track(spine, stops)`; each stop has `mark`, `box` under the spine and `above` over it; `n` may be the stops instead |
| `connect(slide, start, end, theme, *, kind="straight", arrow=True, colour=None, width_pt=1.5)` | an arrow between two boxes; `kind` is "straight", "elbow" or "curved" |
| `preset(slide, box, theme, name, *, adj=None, tint="accent", outline=None, width_pt=1.5)` | any preset filling `box`; `adj` is one fraction per knob |
| `find_presets(term)`, `preset_intent(name)`, `preset_adjustments(name)` | the near names, the line saying what one is for, the knobs it takes |

Constants: `PRESET_NAMES`.

## Drawing with them

```python
from ppt_layout import the_largest_step_this_copy_takes
from ppt_shapes import chevron_row, connect, preset, timeline   # 109 Office preset names

labels = ("采集", "清洗", "标注", "训练", "评测")
steps = chevron_row(slide, band, T, labels)
size = min(the_largest_step_this_copy_takes(label, one.box, font=F) for one, label in zip(steps, labels))
for one, label in zip(steps, labels):
    write(slide, one.box, label, size=size, colour=T["background"], font=F, cjk_font=HAN,
          align="center", anchor="middle")
```

**A sequence is drawn with the shape that means sequence.** Five rectangles with gaps
between them is a list; five chevrons that interlock is a process, and the difference
is legible from the back of the room. The same for a decision (`flowChartDecision`), a
milestone scale (`timeline`), a route between two regions (`connect`). These are real
Office presets, so they stay editable, take the deck's theme, and keep the text
rectangle that holds a label clear of the point.

**Never compute the geometry yourself.** `chevron_row` solves the row: every step the
same width, each notch landing exactly on the previous point, the run filling the box
to both edges. A hand-placed row is off by a fraction and reads as five shapes that
nearly touch. It hands back `step.box` — the shape's own text rectangle, already inset
past the points — and that is where the label goes. Writing into the shape's full box
instead is what puts a word on an arrow. Neither call writes the copy for you, so hand
it the labels or hand it their count, whichever you have.

**Copy on a fill needs the ground colour.** On `tint="accent"` set it to
`T["background"]`; on `tint="accent_soft"` or `"surface"`, `T["foreground"]`. Nothing
here takes a type size, so the label keeps the deck's own ramp.

**Ask how big the label may be; do not name a step.** A step's box is over an inch
tall and a label in it is two or three characters, so the smallest step on the ramp
puts type at a fifth the height of the thing it names — measured on the render, and
it is what naming `LABEL_PT` here produced. `the_largest_step_this_copy_takes` walks
the ramp the other way and answers the biggest step the copy still fits at: the five
labels above came back at 30pt where the named step was 14. Ask once per step and
take the `min`, because the row has to share one size or it reads as five unrelated
words — and the longest label is what the row can afford.

Names are the DrawingML ones — `chevron`, `rightArrow`, `flowChartDecision`,
`roundRect` — and `find_presets(term)` answers a near miss before the build does.
`preset(..., adj=...)` takes one value per knob, in the order `preset_adjustments(name)`
lists them: `adj=0.06` is a small radius on `roundRect`, `adj=0.5` a stadium.

**A preset's name is not a description -- read `preset_intent(name)` before you draw one
you have not drawn before.** `chevron` is "Compact V-notched directional stage for
repeated sequence or handoff" and `homePlate` is "Pentagon-like horizontal stage body
with one pointed destination edge": the same arrow, one for a step in a run and one for
where the run ends, and neither name says which. `find_presets` gets you the candidates;
this is how you choose between them.

**`PRESET_NAMES` is 109 of Office's 177.** All of them were rendered at their default
adjustments and looked at, and sixty-eight came back unusable: the action buttons bake a
grey glyph into the shape and the pseudo-3D ones (`cube`, `can`, `ribbon`, the curved
arrows) a grey second face, neither of which the theme can reach; the leader-line
callouts and the single-sided braces (`borderCallout1`, `leftBrace`) render broken at
their defaults; the bursts, the gears and the rest of the clip art are decoration. Reach
for one and `preset` refuses it by name, saying what the render showed and what to use
instead — `wedgeRectCallout` for a callout, `bracePair` for a brace, `rect` or
`roundRect` for the rest. `find_presets` still finds them, marked as out.

**Not every knob is a fraction**, and the one that bites is silent. On the seven
presets built out of a sector — `arc`, `blockArc`, `chord`, `pie`, `circularArrow`,
`leftCircularArrow`, `leftRightCircularArrow` — the knobs that place the ends of the
sweep are **angles in degrees**, measured clockwise from three o'clock. So a
three-quarter pie is `adj=(0, 270)`, not `adj=(0, 0.75)`: 0.75 there asks for three
quarters of one degree, and you get a hairline, drawn without complaint. On `blockArc`
the third knob is still a fraction (the ring's thickness), so one call can carry both
units. `preset_adjustments(name)` names the knobs in order; four more — `adj2` on each
of the three circular arrows and on `mathNotEqual` — are angles the module will not
scale for you, and it raises saying which one rather than drawing something wrong.

What not to reach for: a preset as decoration (a star, a burst, a callout bubble on a
page that has no annotation to make), a connector standing in for a rule under a title,
or a shape whose outline has to be traced as a freeform. If the page has no sequence,
no branch and no route, it needs none of this.
