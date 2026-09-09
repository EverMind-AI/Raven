# Passages: more than one figure, and grids, rails and cards

The worked code for `P8`-`P13`, `P28`-`P34` and the layers stacked on a comparison,
behind [deck/build/references/layouts.md](deck/build/references/layouts.md). These are the
pages that put several regions of equal or deliberately unequal weight on one canvas.

Every block here runs. They assume the setup block of the skill's §3 plus `FACE`/`HAN` for
the theme's two faces, `INK`/`MUTED`/`ACCENT` for `T["foreground"]`, `T["muted"]` and
`T["accent"]`, `FIGURES` for the figure directory, and `frame = page()`.
A passage whose bands are measured before they are drawn ends that line as
`frame = page().holding(*heights)`, so the room the body was given and nothing asked for
becomes air above and below the run instead of a band of white along the page's foot (the
skill's §6.5). Not for a run something else already spreads into the whole body:
`card_group(..., down=True)` given the body puts the leftover between its own cards, and a
body cut to the sum of their heights first leaves them touching.

A passage whose figures carry a caption asks for the page's foot as well --
`frame = page(footer=True)` -- because the caption goes on one line in `footer()`'s `note`,
joined with `；` where the page has more than one figure, and never under the picture
(`M6`); the height that frees goes back to the figure.

They also assume the seven picture helpers from
[deck/build/references/layouts-primitives.md](deck/build/references/layouts-primitives.md)
-- `cover`, `scrim`, `vignette`, `clip`, `fade`, `duotone`, `lift`. Open that file first;
nothing here redefines them.

### P8 -- Small multiples: one row, one framing, one caption block each

Not a grid of unrelated pictures. The identical framing is the message -- the reader
compares because nothing but the content differs.

```python
for box, (name, said) in zip(frame.body.columns(4), [
    ("基线", "42.1"), ("去时序颈", "38.6"), ("去语义查询", "39.4"), ("全量", "46.3"),
]):
    down = stack(box)
    shown = picture_size(f"{FIGURES}/fig6.png", down.room)
    picture_fit(slide, f"{FIGURES}/fig6.png", down.take(shown.h), T)
    down.skip(0.12)
    write(slide, down.rest(), [name, f"mAP {said}"], size=LABEL_PT, colour=INK,
          font=FACE, cjk_font=HAN, align="center")
```

### P9 -- Two columns on one baseline

Before and after, A and B, ours and theirs. Cut both panels to the taller of the two so
neither is padded out to the page, and give the side carrying the answer `accent_soft`
while the other keeps `surface` (`M3`).

**The two sides carry the same row labels.** Row two on the left and row two on the right
are the same question, so the reader compares across at a fixed height instead of matching
sentences by eye. The field name takes `LABEL_PT` in `MUTED` and its value `BODY_PT` in
the ink -- the ramp's own two steps for a label over what it labels.

```python
fields = ("权重", "新增一类", "显存")
sides = [("surface", "改造前", ("每类任务一套，四套一起上线", "重训全网", "4 x 11GB")),
         ("accent_soft", "改造后", ("一套，任务在输入侧切换", "只加一组查询", "1 x 12GB"))]
boxes = frame.body.split_left(0.5, gutter=0.5)
tall = 0.44 + 0.08 + 3 * 0.86 + 2 * (PAD + 0.10)
for box, (tint, head, values) in zip(boxes, sides):
    panel = Box(box.x0, box.y0, box.x1, box.y0 + tall)
    plane(slide, panel, T, tint=tint, radius=True)
    down = stack(panel.inset(PAD + 0.10))
    write(slide, down.take(0.44), head, size=LEAD_PT, bold=True, colour=INK, font=FACE, cjk_font=HAN)
    down.skip(0.08)
    for field, value in zip(fields, values):
        row = stack(down.take(0.86))
        write(slide, row.take(0.30), field, size=LABEL_PT, colour=MUTED, font=FACE, cjk_font=HAN)
        write(slide, row.rest(), value, size=BODY_PT, colour=INK, font=FACE, cjk_font=HAN)
preset(slide, Box.at((boxes[0].x1 + boxes[1].x0) / 2 - 0.16, frame.body.y0 + tall / 2 - 0.16,
                     w=0.32, h=0.32), T, "rightArrow", tint="accent")
```

### P10 -- One dominant figure, the supporting ones beside it

Unequal on purpose. A page where every region carries the same weight has argued
nothing.

```python
frame = page(footer=True)
lead, rest = frame.body.split_left(0.62)
picture_fit(slide, f"{FIGURES}/fig7.png", lead, T)
for box in rest.rows(2):
    picture_fit(slide, f"{FIGURES}/fig8.png", box, T)
footer(slide, frame.footer, T, note="来源：论文图 1-3；图 6 主结构；图 7 注意力图；图 8 失败样例",
       font=FACE, cjk_font=HAN)
```

### P28 -- An asymmetric collage: one dominant figure, smaller ones over its corner

The rotation and the shadow are the pattern, not decoration on it: two pictures overlapping
at right angles read as a layout error, and the same two tilted three degrees with a shadow
under each read as prints on a desk. Keep the overlaps small -- each smaller picture here
covers about an eighth of the one under it, and at three fifths `covered_shape` refuses the
deck for hiding evidence.

```python
frame = page(footer=True)
lead = Box(frame.body.x0, frame.body.y0, frame.body.x0 + 7.40, frame.body.y1)
lift(cover(slide, f"{FIGURES}/fig1.png", lead))
over = ((f"{FIGURES}/fig2.png", Box.at(lead.x1 - 1.05, lead.y0 + 0.20, w=3.10, h=2.00), -3.0),
        (f"{FIGURES}/fig3.png", Box.at(lead.x1 - 0.35, lead.y0 + 2.55, w=3.30, h=2.10), 2.5))
for figure, box, angle in over:
    picture = cover(slide, figure, box)
    picture.rotation = angle
    lift(picture)
footer(slide, frame.footer, T, note="来源：论文图 1-3；主图承担结构，压角的两张是证据",
       font=FACE, cjk_font=HAN)
```

### P29 -- Picture in picture: the detail inset over the wide shot

The inset's label goes *above* it, in the gap the wide shot leaves: written under the inset
it lands on the wide shot, where the ground is whatever the photograph happens to be there.
The `plane` one notch larger than the inset is its mount, and what separates the two
pictures without a rule.

```python
frame = page(footer=True)
wide = frame.body
cover(slide, f"{FIGURES}/fig1.png", wide)
inset = Box.at(wide.x1 - 4.30, wide.y1 - 2.90, w=4.00, h=2.60)
write(slide, Box.corners(inset.x0, inset.y0 - 0.40, inset.x1, inset.y0 - 0.06), "细节：p95 延迟",
      size=LABEL_PT, colour=T["background"], font=FACE, cjk_font=HAN)
plane(slide, inset.inset(-0.06, -0.06), T, tint="background")
lift(cover(slide, f"{FIGURES}/fig2.png", inset))
footer(slide, frame.footer, T, note="来源：论文图 1 与图 2；全景说结构，嵌进去的一张说这一处",
       font=FACE, cjk_font=HAN)
```

### P30 -- The same figure twice: the whole of it, and a zoom on the part under discussion

The page a dense table or a six-panel figure wants, and it needs no second asset: the same
file is placed again with `crop_*` set to the region's own fractions. The zoom's height is
worked out from the region and the image's pixels, because the cropped region is stretched
to the frame and a frame of the wrong aspect distorts it. `M22` marks the region on the
original and `connect` ties the two together.

```python
frame = page(footer=True)
whole, aside = frame.body.split_left(0.55, gutter=0.40)
shown = picture_fit(slide, f"{FIGURES}/table2.png", whole, T)
region = (0.06, 0.52, 0.52, 0.74)                       # left, top, right, bottom of the source
lens = Box(shown.box.x0 + shown.box.w * region[0], shown.box.y0 + shown.box.h * region[1],
           shown.box.x0 + shown.box.w * region[2], shown.box.y0 + shown.box.h * region[3])
preset(slide, lens, T, "rect", tint=None, outline="accent")
wide_px, tall_px = shown.shape.image.size
zoom = Box.at(aside.x0, aside.y0 + 0.30, w=aside.w,
              h=aside.w * (region[3] - region[1]) * tall_px / ((region[2] - region[0]) * wide_px))
picture = slide.shapes.add_picture(f"{FIGURES}/table2.png", Inches(zoom.x0), Inches(zoom.y0),
                                  width=Inches(zoom.w), height=Inches(zoom.h))
picture.crop_left, picture.crop_right = region[0], 1 - region[2]
picture.crop_top, picture.crop_bottom = region[1], 1 - region[3]
picture.line.color.rgb = rgb(ACCENT)
picture.line.width = Pt(1.5)
connect(slide, lens, zoom, T, kind="curved", colour="accent")
write(slide, Box.corners(zoom.x0, zoom.y1 + 0.14, zoom.x1, zoom.y1 + 1.20),
      "K=2 这六行是本页的结论：块大小从 128 到 8192，p50 只动了 0.05s。",
      size=BODY_PT, colour=INK, font=FACE, cjk_font=HAN)
footer(slide, frame.footer, T, note="来源：论文表 2 各基线的时延与总分；右侧是其中 K=2 六行的放大",
       font=FACE, cjk_font=HAN)
```

### P31 -- A montage of figures under one band of type

Six figures at 0.04in apart read as one surface; the band across them is the page's
sentence. `rect` and not `scrim`, because the band carries type over six different grounds
and needs one flat colour under the words -- and at 0.72 it stays under the line at which
each tile beneath it would be reported as hidden.

```python
tiles = Box.corners(0.0, 0.0, CANVAS_W, CANVAS_H).grid(3, 2, gutter=0.04)
for tile, figure in zip(tiles, ("fig1", "fig2", "fig3", "fig4", "fig5", "fig6")):
    cover(slide, f"{FIGURES}/{figure}.png", tile)
band = Box.corners(0.0, 2.90, CANVAS_W, 4.60)
rect(slide, band, INK, 0.72)
down = stack(band.inset(MARGIN, 0.24))
write(slide, down.take(0.86), "六张图，一句话", size=TITLE_PT + 4, bold=True,
      colour=T["background"], font=FACE, cjk_font=HAN)
write(slide, down.rest(), "论文里所有的证据都在讲同一件事：切任务只换输入。", size=BODY_PT,
      colour=T["accent_soft"], font=FACE, cjk_font=HAN)
```

### P11 -- An equal grid of cells

`box.grid(cols, rows)` is row-major. Level the row with `card_size` -- cards handed a
raw grid cell are each as tall as the region, and two lines of copy in a 2.5in card is
mostly void.

```python
said = [
    ("database", "统一数据面", "四路信号在入库时对齐。"),
    ("route", "统一控制面", "调度只认查询，不认任务。"),
    ("shield_check", "统一校验", "同一套断言跑在四条链路上。"),
    ("gauge", "统一度量", "延迟和精度写进同一张表。"),
    ("users", "统一值班", "一个班组覆盖四类工单。"),
    ("history", "统一回放", "任一时刻可复现。"),
]
cells = frame.body.grid(3, 2, gutter=GUTTER)
height = max(card_size(cells[0].w, icon=i, title=h, body=b, font=FACE).h for i, h, b in said)
for cell, (icon, head, body) in zip(cells, said):
    card(slide, Box(cell.x0, cell.y0, cell.x1, cell.y0 + height), T,
         icon=icon, title=head, body=body, font=FACE, cjk_font=HAN)
```

### P12 -- A grid with one cell given to copy

The missing tile is what makes the grid a composition instead of a contact sheet. Do
not fill every slot just because there is a grid.

```python
frame = page(footer=True)
cells = frame.body.grid(3, 2, gutter=GUTTER)
write(slide, cells[0], ["六个站点", "同一套权重，六种现场。"], size=LEAD_PT, colour=INK,
      font=FACE, cjk_font=HAN, anchor="middle")
for cell in cells[1:]:
    picture_fit(slide, f"{FIGURES}/fig9.png", cell, T)
footer(slide, frame.footer, T, note="来源：五个站点同一天的现场照；按行依次为华东、华北、西南、华南、东北",
       font=FACE, cjk_font=HAN)
```

### P13 -- A full-height rail down one side

The rail carries the header, so this page builds its own `Frame` rather than calling
`page()` and `heading()`. A deck may do this -- what it may not do is give each page a
different header, so if one page reads this way the rest of that section does too.

The frame's fourth box is the foot, and here it has to be a real strip rather than the
placeholder a hand-built `Frame` starts with: the rail is 2.38in wide inside its margin,
which is narrower than the lane a note needs, so the foot goes under the figure beside the
rail and the figure's own region stops above it.

```python
rail = Box.corners(0.0, 0.0, MARGIN + 3.10, CANVAS_H)
plane(slide, rail, T, tint="accent_soft")
inner = rail.inset(MARGIN, MARGIN)
foot = Box.corners(rail.x1 + GUTTER, CANVAS_H - MARGIN - 0.30, CANVAS_W - MARGIN, CANVAS_H - MARGIN)
own = Frame(Box(inner.x0, inner.y0, inner.x1, inner.y0 + 0.30),
            Box(inner.x0, inner.y0 + 0.34, inner.x1, inner.y0 + 1.90),
            Box(inner.x0, inner.y0 + 2.10, inner.x1, inner.y1),
            foot)
write(slide, own.kicker, "03 / 06 · 结构", size=KICKER_PT, colour=MUTED, font=FACE, cjk_font=HAN)
write(slide, own.title, "一侧通栏侧栏", size=TITLE_PT, bold=True, colour=INK, font=FACE, cjk_font=HAN)
write(slide, own.body, "侧栏承担页眉，正文区整块留给图。", size=LABEL_PT, colour=MUTED,
      font=FACE, cjk_font=HAN)
body = Box.corners(rail.x1 + GUTTER, MARGIN, CANVAS_W - MARGIN, foot.y0 - GUTTER)
picture_fit(slide, f"{FIGURES}/fig10.png", body, T)
footer(slide, own.footer, T, note="来源：论文图 9；本节要讲的结构", font=FACE, cjk_font=HAN)
```

### P32 -- Image navigation cards: a contents page whose entries are pictures

The agenda page as a preview of the deck rather than a list of its section names. The
type sits on a flat plate at the foot of each card with a short gradient above it, which
is the combination that survives both checks: the plate gives `contrast` one colour to
read, and the gradient loses the plate's top edge without covering enough of the picture
for `covered_shape` to see a hidden figure. A single gradient over the whole card fails
both.

```python
entries = (("01", "竞争全景", "fig1"), ("02", "架构分野", "fig2"),
           ("03", "评测口径", "fig3"), ("04", "破局路线", "fig4"))
for box, (number, name, figure) in zip(frame.body.columns(4, gutter=0.22), entries):
    cover(slide, f"{FIGURES}/{figure}.png", box)
    plate = Box(box.x0, box.y1 - 1.90, box.x1, box.y1)
    rect(slide, plate, INK, 0.74)
    scrim(slide, Box(plate.x0, plate.y0 - 0.80, plate.x1, plate.y0), INK, 0.74, 0.0, angle=90.0)
    down = stack(plate.inset(PAD, PAD))
    write(slide, down.take(0.66), number, size=NUMBER_PT, bold=True,
          colour=T["accent_soft"], font=FACE)
    write(slide, down.take(0.44), name, size=LEAD_PT, bold=True, colour=T["background"],
          font=FACE, cjk_font=HAN)
    write(slide, down.rest(), "本节要落定的一件事", size=LABEL_PT, colour=T["accent_soft"],
          font=FACE, cjk_font=HAN)
```

### P33 -- A side hero image with staggered evidence cards opposite

The stagger is the whole point: three cards flush left beside a hero is a grid with a
picture in it. Each card indents further and gets shorter, so the eye walks down them.
`card_size` per card, because a staggered column of equal-height cards is a rhythm nobody
asked for.

```python
hero = Box(frame.body.x0, frame.body.y0, frame.body.x0 + 5.20, frame.body.y1)
cover(slide, f"{FIGURES}/photo.jpg", hero)
notes = (("target", "口径统一", "四条链路同一套断言。", 0.00),
         ("gauge", "时延对齐", "P95 从 121ms 降到 42ms。", 0.45),
         ("shield_check", "回放可复现", "任一时刻可重放。", 0.90))
lane = Box.corners(hero.x1 + 0.50, frame.body.y0, frame.body.x1, frame.body.y1)
top = lane.y0
for icon, head, said, offset in notes:
    width = lane.w - offset
    tall = card_size(width, icon=icon, title=head, body=said, font=FACE).h
    card(slide, Box(lane.x0 + offset, top, lane.x0 + offset + width, top + tall), T,
         tint="surface", icon=icon, title=head, body=said, font=FACE, cjk_font=HAN)
    top += tall + 0.26
```

### P34 -- An ambient banner over an evidence figure, the copy in a panel beside

Two pictures doing two different jobs on one page, which is the argument for it: the banner
gives the page somewhere to be and the figure gives it a number. The banner is
cover-cropped to a band that runs off the left edge; the figure is `picture_fit`, whole,
because it is the evidence -- and what each of the two is goes in the page's foot with the
source (`M6`), not under either of them.

The panel is already the ground, so the bands sit straight on the tint, each with its own
icon (`M1`): a card on `accent_soft` is a surface on a surface. Measure the bands off
their own copy with `text_size` and hand the leftover height to the stack's gutter --
bands cut to a third of the panel each leave an inch of air inside every one of them, and
the set stops reading as a set.

```python
frame = page(footer=True)
left, panel = frame.body.split_left(0.62, gutter=0.34)
down = stack(left, gutter=0.18)
cover(slide, f"{FIGURES}/photo.jpg", Box.corners(0.0, down.take(1.90).y0, left.x1, down.y - 0.18))
picture_fit(slide, f"{FIGURES}/fig2.png", down.rest(), T)
plane(slide, panel, T, tint="accent_soft", radius=True)
inner = stack(panel.inset(PAD + 0.10))
write(slide, inner.take(0.46), "这一页在说什么", size=LEAD_PT, bold=True, colour=INK,
      font=FACE, cjk_font=HAN)
inner.skip(0.10)
notes = [("image", "上面那张是氛围", "它只负责让这一页有现场。"),
         ("gauge", "下面那张是证据", "它负责这一页的数字。"),
         ("quote", "面板里的字是结论", "颜色和图都不参与。")]
lane = inner.rest()
bands = [0.38 + text_size(body, lane.w, size=LABEL_PT, font=FACE).h for _, _, body in notes]
rows = stack(lane).spread(*bands)
for (icon, head, body), tall in zip(notes, bands):
    band = rows.take(tall)
    add_icon(slide, icon, Inches(band.x0), Inches(band.y0 + 0.03), Inches(0.28), INK)
    write(slide, Box.corners(band.x0 + 0.42, band.y0, band.x1, band.y0 + 0.34), head,
          size=BODY_PT, bold=True, colour=INK, font=FACE, cjk_font=HAN, anchor="middle")
    write(slide, Box.corners(band.x0, band.y0 + 0.38, band.x1, band.y1), body,
          size=LABEL_PT, colour=MUTED, font=FACE, cjk_font=HAN)
footer(slide, frame.footer, T, note="来源：图 4 检索时延分布（论文原图）；氛围照取自公开图库",
       font=FACE, cjk_font=HAN)
```

### M1-M11 -- five of them at once, on top of `P9`

```python
down = stack(frame.body)
band = down.take(2.30)
left, right = band.split_left(0.5)
plane(slide, right, T, tint="accent_soft", radius=True)                       # M3
for box, tint, (head, said) in ((left, "surface", ("现状", "四套权重并行")),
                                (right, None, ("目标", "一套权重覆盖四类"))):
    if tint:
        plane(slide, box, T, tint=tint, radius=True)                          # M2
    inner = stack(box.inset(PAD + 0.08))
    badge = Box.at(inner.x0, inner.take(0.36).y0, w=0.32, h=0.32)             # M5
    preset(slide, badge, T, "ellipse", tint="accent")
    write(slide, badge, "1" if tint else "2", size=LABEL_PT, bold=True, colour=T["background"],
          font=FACE, align="center", anchor="middle")
    write(slide, Box(inner.x0 + 0.46, badge.y0, box.x1 - PAD, badge.y1), head,
          size=LEAD_PT, bold=True, colour=INK, font=FACE, cjk_font=HAN, anchor="middle")
    write(slide, inner.take(0.50), said, size=BODY_PT, colour=INK, font=FACE, cjk_font=HAN)
    mark(slide, Box.at(box.x1 - PAD - 1.30, box.y1 - PAD - 0.34, w=1.30, h=0.30), T,
         "progress", "0.62" if tint else "1.0")                               # M8
down.skip(GUTTER)
lane = down.rest()
write(slide, Box(lane.x0, lane.y0, lane.x1, lane.y0 + 0.30), "口径", size=KICKER_PT,
      colour=MUTED, font=FACE, cjk_font=HAN)                                  # M9
rule(slide, Box(lane.x0, lane.y0, lane.x0 + 1.05, lane.y0 + 0.30), T)         # M4
write(slide, Box(lane.x0, lane.y0 + 0.48, lane.x1, lane.y1), "两栏取自同一批日志，进度条是覆盖率。",
      size=LABEL_PT, colour=MUTED, font=FACE, cjk_font=HAN)
```
