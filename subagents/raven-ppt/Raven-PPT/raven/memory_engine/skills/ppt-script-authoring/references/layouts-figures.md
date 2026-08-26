# Passages: a figure and copy, a figure as a surface, a photograph as the page

The worked code for `P1`-`P7`, `P23`-`P27`, `P35` and the picture layers, behind
[deck/build/references/layouts.md](deck/build/references/layouts.md). One page needs one
of these; the registry is where you choose which.

Every block here runs. They assume the setup block of the skill's §3 plus `FACE`/`HAN` for
the theme's two faces, `INK`/`MUTED`/`ACCENT` for `T["foreground"]`, `T["muted"]` and
`T["accent"]`, `FIGURES` for the figure directory, and `frame = page()`.

They also assume the seven picture helpers from
[deck/build/references/layouts-primitives.md](deck/build/references/layouts-primitives.md)
-- `cover`, `scrim`, `vignette`, `clip`, `fade`, `duotone`, `lift`. Open that file first;
nothing here redefines them.

### P1 -- Figure left, copy right

Cut the figure's column to the figure before writing beside it: `picture_fit` centres
what is left of a band, so a column cut by eye is a strip of white over the figure and
the caption stranded at the bottom.

```python
caption = "图 1：四阶段流水线（论文原图）"
figure, said = frame.body.split_left(0.58)
tall = picture_size(f"{FIGURES}/fig1.png", figure, caption=caption).h
picture_fit(slide, f"{FIGURES}/fig1.png", Box(figure.x0, figure.y0, figure.x1, figure.y0 + tall),
            T, caption=caption)
down = stack(said)
write(slide, down.take(0.42), "读法", size=LEAD_PT, bold=True, colour=INK, font=FACE, cjk_font=HAN)
down.skip(0.10)
points(slide, down.rest(), T, [
    "四个阶段共享一套权重，切任务只换输入查询。",
    "第三阶段是唯一带监督的一步。",
    "端到端 42ms，其中第二阶段占 61%，是下一步的目标。",
], size=BODY_PT, font=FACE, cjk_font=HAN)
```

### P2 -- Figure right, copy left

The mirror, and not the same page: the eye lands left first, so this is the one to
reach for when the argument leads and the figure corroborates.

```python
caption = "图 2：验证集曲线"
said, figure = frame.body.split_left(0.42)
down = stack(said)
write(slide, down.take(0.42), "为什么是这条曲线", size=LEAD_PT, bold=True, colour=INK,
      font=FACE, cjk_font=HAN)
down.skip(0.10)
points(slide, down.rest(), T, [
    "第 4 个 epoch 之后收益转平，继续训练只买到 0.3 个点。",
    "两次回落都对应学习率重启，不是数据问题。",
    "第 9 个 epoch 起验证集与训练集分离，是过拟合的起点。",
], size=BODY_PT, font=FACE, cjk_font=HAN)
tall = picture_size(f"{FIGURES}/fig2.png", figure, caption=caption).h
picture_fit(slide, f"{FIGURES}/fig2.png", Box(figure.x0, figure.y0, figure.x1, figure.y0 + tall),
            T, caption=caption)
```

### P3 -- Figure band across the top, copy in columns under it

For a figure that is wide and short. Give the band a share of the region and let
`picture_size` cut it to the figure inside that share, or the figure takes the whole
region and `rest()` refuses with nothing left.

```python
down = stack(frame.body)
allowed = Box(down.x0, down.y0, down.x1, down.y0 + down.left * 0.62)
band = down.take(picture_size(f"{FIGURES}/fig3.png", allowed, caption="图 3：系统全貌").h)
picture_fit(slide, f"{FIGURES}/fig3.png", band, T, caption="图 3：系统全貌")
down.skip(GUTTER)
for box, (head, body) in zip(down.rest().columns(3), [
    ("采集", "四路信号，10Hz 对齐后入库。"),
    ("推理", "同一套权重，按任务拼装查询。"),
    ("下发", "结果写回工单，平均 42ms。"),
]):
    write(slide, box, [head, body], size=BODY_PT, colour=INK, font=FACE, cjk_font=HAN)
```

### P4 -- One line of copy, the figure under it

The claim in one sentence at lead size, then the whole rest of the page as the figure.
The page that wants this is the one where the figure *is* the argument.

```python
down = stack(frame.body)
said = "把任务定义搬进输入，网络本身就与任务无关：同一组权重在推理时按需拼装查询集合。"
write(slide, down.take(text_size(said, frame.body.w, size=LEAD_PT, font=FACE).h), said,
      size=LEAD_PT, colour=INK, font=FACE, cjk_font=HAN)
down.skip(GUTTER)
picture_fit(slide, f"{FIGURES}/fig4.png", down.rest(), T, caption="图 4：查询拼装（论文原图）")
```

### P27 -- Serpentine: three rows, the figure changing side

`picture_fit` and not `cover`, because these are figures: the rows come out unlevel and
that is the right trade -- cropping three figures to a common band throws away the parts
of them the page is citing. The step number down the copy side is what makes the zigzag
read as an order rather than as three unrelated rows.

```python
rows = frame.body.rows(3, gutter=0.16)
serpentine = ((f"{FIGURES}/fig1.png", "left", "抽取阶段：四路信号在入库时对齐。"),
              (f"{FIGURES}/fig2.png", "right", "检索阶段：同一套权重按任务拼装查询。"),
              (f"{FIGURES}/fig3.png", "left", "写回阶段：结果落到工单，平均 42ms。"))
for index, (row, (figure, side, said)) in enumerate(zip(rows, serpentine)):
    if side == "left":
        picture, copy = row.split_left(0.34, gutter=GUTTER)
    else:
        copy, picture = row.split_left(0.62, gutter=GUTTER)
    picture_fit(slide, figure, picture, T)
    down = stack(copy)
    write(slide, down.take(0.36), f"0{index + 1}", size=LEAD_PT, bold=True, colour=MUTED,
          font=FACE)
    write(slide, down.rest(), said, size=BODY_PT, colour=INK, font=FACE, cjk_font=HAN,
          anchor="middle")
```

### P5 -- Figure as the page's ground, notes laid over it

Put the notes in the figure's calm region and size them with `card_size`, not with a
fraction of the page: two cards stretched to half the height read as one panel.

```python
ground = Box.corners(0.0, frame.body.y0 - 0.10, CANVAS_W, CANVAS_H)
picture_fit(slide, f"{FIGURES}/fig5.png", ground, T)
notes = [
    ("alert_circle", "瓶颈在第二阶段", "61% 的端到端时延落在这里。"),
    ("check", "第三阶段可停用", "关掉只掉 0.4 个点。"),
]
lane = Box.corners(MARGIN, CANVAS_H - MARGIN - 1.30, CANVAS_W - MARGIN, CANVAS_H - MARGIN)
for box, (icon, head, body) in zip(lane.columns(2, gutter=GUTTER), notes):
    tall = card_size(box.w, icon=icon, title=head, body=body, font=FACE).h
    card(slide, Box(box.x0, box.y1 - tall, box.x1, box.y1), T, tint="accent_soft",
         icon=icon, title=head, body=body, font=FACE, cjk_font=HAN)
```

### P6 -- Numbered hotspots on the figure, the legend down the side

The hotspots go on fractions of the box `picture_fit` hands back, which is the figure's
real extent and not the region it was given -- a fraction of the region puts the marks
in the white space beside it.

```python
figure, legend = frame.body.split_left(0.62)
shown = picture_fit(slide, f"{FIGURES}/fig5.png", figure, T, caption="图 5：标注了三处的流水线")
for index, (fx, fy) in enumerate([(0.20, 0.42), (0.46, 0.30), (0.72, 0.42)], start=1):
    dot = Box.at(shown.box.x0 + shown.box.w * fx, shown.box.y0 + shown.box.h * fy, w=0.34, h=0.34)
    preset(slide, dot, T, "ellipse", tint="accent")
    write(slide, dot, str(index), size=LABEL_PT, bold=True, colour=T["background"],
          font=FACE, align="center", anchor="middle")
down = stack(legend)
for index, said in enumerate([
    "输入侧只保留时序颈，其余分支在这一步丢弃。",
    "查询拼装，任务切换发生在这里。",
    "解码头共享，不随任务变化。",
], start=1):
    band = down.take(max(0.46, text_size(said, legend.w - 0.5, size=BODY_PT, font=FACE).h))
    badge = Box.at(band.x0, band.y0, w=0.32, h=0.32)
    preset(slide, badge, T, "ellipse", tint="accent")
    write(slide, badge, str(index), size=LABEL_PT, bold=True, colour=T["background"],
          font=FACE, align="center", anchor="middle")
    write(slide, Box(band.x0 + 0.46, band.y0, band.x1, band.y1), said,
          size=BODY_PT, colour=INK, font=FACE, cjk_font=HAN)
    down.skip(0.14)
```

### P7 -- One thing at the centre, leader lines out to what it reaches

`connect` picks its own edges from where the two boxes sit, so a hub-and-spokes needs
no coordinate. Draw the spokes in `MUTED`: in `T["grid"]` they disappear on white.

```python
cells = frame.body.grid(3, 3, gutter=0.30)
middle = cells[4]
preset(slide, middle, T, "ellipse", tint="accent_soft")
write(slide, middle, "统一查询集合", size=LEAD_PT, bold=True, colour=INK,
      font=FACE, cjk_font=HAN, align="center", anchor="middle")
for index, icon, said in ((1, "category", "分类"), (3, "layers", "分割"),
                          (5, "route", "跟踪"), (7, "search", "检索")):
    leaf = cells[index]
    connect(slide, middle, leaf, T, kind="straight", colour=MUTED)
    ink = the_ink_an_icon_covers(icon, 0.34)
    add_icon(slide, icon, Inches(leaf.x0 + leaf.w / 2 - ink.w / 2 - ink.x0),
             Inches(leaf.y0 + leaf.h / 2 - 0.42), Inches(0.34), ACCENT)
    write(slide, Box(leaf.x0, leaf.y0 + leaf.h / 2, leaf.x1, leaf.y1), said,
          size=BODY_PT, bold=True, colour=INK, font=FACE, cjk_font=HAN, align="center")
```

### P23 -- A photograph as the whole page, the title floated on it

The scrim is what makes this publishable rather than a picture with words on it. Give the
title stack the end where the gradient is nearly opaque, and keep the whole of it there --
copy that starts inside the transition is the one the render will not carry.

```python
cover(slide, f"{FIGURES}/photo.jpg", Box.corners(0.0, 0.0, CANVAS_W, CANVAS_H))
scrim(slide, Box.corners(0.0, 0.0, CANVAS_W, CANVAS_H), INK, 0.88, 0.05, angle=0.0)
down = stack(Box.corners(MARGIN, 2.30, 7.20, 6.20))
write(slide, down.take(0.40), "01 / 06 · 立论", size=KICKER_PT, colour=T["accent_soft"],
      font=FACE, cjk_font=HAN)
down.skip(0.10)
write(slide, down.take(2.20), "记忆不是一层缓存，\n是系统的第二套状态。", size=TITLE_PT + 10,
      bold=True, colour=T["background"], font=FACE, cjk_font=HAN, spacing=1.20)
write(slide, down.take(0.44), "2026 年战略审议 · 架构评审组", size=LABEL_PT,
      colour=T["accent_soft"], font=FACE, cjk_font=HAN)
```

### P24 -- An image belt across the middle, copy above and below

The belt is built from the canvas rather than from `frame.body`, because a band that stops
at the safe margin is a picture in a box. Both long edges get a short gradient to the
page's own background: without them the belt has two horizontal rules nobody drew.

```python
down = stack(frame.body)
write(slide, down.take(0.52), "上半页说这条带子里有什么", size=LEAD_PT, bold=True, colour=INK,
      font=FACE, cjk_font=HAN)
down.skip(0.14)
band = down.take(2.10)
belt = Box.corners(0.0, band.y0, CANVAS_W, band.y1)
cover(slide, f"{FIGURES}/photo.jpg", belt)
scrim(slide, Box.corners(0.0, belt.y0, CANVAS_W, belt.y0 + 0.40), T["background"], 1.0, 0.0, angle=270.0)
scrim(slide, Box.corners(0.0, belt.y1 - 0.40, CANVAS_W, belt.y1), T["background"], 1.0, 0.0, angle=90.0)
down.skip(0.16)
for box, said in zip(down.rest().columns(3, gutter=GUTTER), (
    "带子出血到两边画布边缘，页内没有一条竖直的白边。",
    "上下两个方向各有一层渐变，图带与页面的接缝看不出来。",
    "带子里不放字：字在带子上下，读起来是一页而不是两页。",
)):
    write(slide, box, said, size=BODY_PT, colour=INK, font=FACE, cjk_font=HAN)
```

### P25 -- A narrow full-height image strip beside a display-size title

This page carries its own header, like `P13`, so `page()` and `heading` are not called and
the rest of that section reads the same way. The strip is under a quarter of the width; at
a third it stops being a strip and starts being a column that owes the reader information.

```python
strip = Box.corners(0.0, 0.0, 3.10, CANVAS_H)
cover(slide, f"{FIGURES}/photo.jpg", strip)
plane(slide, Box.corners(strip.x1, 0.0, strip.x1 + 0.06, CANVAS_H), T, tint="accent")
down = stack(Box.corners(strip.x1 + 0.70, 1.60, CANVAS_W - MARGIN, CANVAS_H - MARGIN))
write(slide, down.take(0.40), "03 / 06 · 分野", size=KICKER_PT, colour=MUTED, font=FACE, cjk_font=HAN)
down.skip(0.12)
write(slide, down.take(2.40), "一条窄图带，\n一个大标题。", size=TITLE_PT + 14, bold=True,
      colour=INK, font=FACE, cjk_font=HAN, spacing=1.18)
points(slide, down.rest(), T, [
    "窄带占满整页高度，宽度不到四分之一，剩下的都留给字。",
    "带子里的内容不承担信息，它只承担这一页的语气。",
], size=BODY_PT, font=FACE, cjk_font=HAN)
```

### P26 -- A figure running to the canvas edge, the copy in the clear

The box ends *at* `CANVAS_W`. A shape whose box crosses the edge is reported by `off_page`
on every build, and it buys nothing: `cover` already throws away what does not fit, so a
box to the edge and a box past it look the same on the page.

```python
said, _ = frame.body.split_left(0.46)
down = stack(said)
write(slide, down.take(0.50), "读法", size=LEAD_PT, bold=True, colour=INK, font=FACE, cjk_font=HAN)
down.skip(0.10)
points(slide, down.rest(), T, [
    "图跑到画布边缘，页面没有一条框住图的边。",
    "跑到边上的一侧不能承载信息：那里只能是背景或延伸的纹理。",
    "字全部留在左侧的干净区域，不与图重叠。",
], size=BODY_PT, font=FACE, cjk_font=HAN)
cover(slide, f"{FIGURES}/fig1.png", Box.corners(6.80, frame.body.y0, CANVAS_W, frame.body.y1))
```

### P35 -- A chapter banner: two images of unequal weight over an oversized section number

A divider page, and the one place a deck can spend a whole page on almost nothing. The
number is the ground the title sits against, and its colour is a measured trade rather
than a taste: on this template `accent_soft` came back `unreadable` at 1.2:1 and refused
the deck, `grid` came back `thin_contrast` at 2.1:1 and passed with a note, and `MUTED`
is clean. A ghost number is not free.

```python
upper = Box.corners(0.0, 0.0, CANVAS_W, 3.90)
small, dominant = upper.split_left(0.32)
cover(slide, f"{FIGURES}/fig3.png", small)
cover(slide, f"{FIGURES}/photo.jpg", dominant)
write(slide, Box.corners(MARGIN, 3.90, CANVAS_W - MARGIN, CANVAS_H - MARGIN), "04",
      size=TITLE_PT + 92, bold=True, colour=MUTED, font=FACE, anchor="middle")
down = stack(Box.corners(MARGIN + 2.60, 4.40, CANVAS_W - MARGIN, CANVAS_H - MARGIN))
write(slide, down.take(0.40), "第四节", size=KICKER_PT, colour=MUTED, font=FACE, cjk_font=HAN)
write(slide, down.take(1.10), "破局路线与行动卡", size=TITLE_PT + 8, bold=True, colour=INK,
      font=FACE, cjk_font=HAN)
write(slide, down.take(0.44), "两层产品矩阵，三条集成路径，一张销售应对卡", size=LABEL_PT,
      colour=MUTED, font=FACE, cjk_font=HAN)
```

### M12-M24 -- the picture layers, stacked

They compose, and each is one call on the picture a structure already placed. A figure
cover-cropped into a hexagon and re-graded into the deck's two colours is three lines; a
photograph as a page ground with a spotlight on the one thing being discussed is two.
`box` below is whichever region the structure put the picture in.

```python
clip(duotone(cover(slide, f"{FIGURES}/photo.jpg", box), INK, T["accent_soft"]), "hexagon")

shown = picture_fit(slide, f"{FIGURES}/fig1.png", box, T)      # M19 on a figure kept whole
shown.shape.line.color.rgb = rgb(ACCENT)
shown.shape.line.width = Pt(2)

cover(slide, f"{FIGURES}/photo.jpg", Box.corners(0.0, 0.0, CANVAS_W, CANVAS_H))
vignette(slide, Box.corners(0.0, 0.0, CANVAS_W, CANVAS_H), INK, centre=0.0, edge=0.88)
plate = Box.corners(3.20, 3.00, 10.20, 4.50)
rect(slide, plate, INK, 0.62)                                  # the words go on the plate
write(slide, plate, "一束光打在要看的那一处", size=TITLE_PT + 6, bold=True,
      colour=T["background"], font=FACE, cjk_font=HAN, align="center", anchor="middle")

fade(cover(slide, f"{FIGURES}/photo.jpg", Box.corners(0.0, 0.0, CANVAS_W, CANVAS_H)), 0.22)
```

**What this renderer does not do.** The deck's own review renders through LibreOffice, so
a treatment PowerPoint would show and it drops is invisible to every check and to you.
`a:grayscl` and `a:blur` in a picture's `a:blip` are both accepted into the file and both
come back unchanged in the render -- desaturating or blurring a picture has to be done to
the pixels before they reach the deck. A picture fill inside run properties (letterforms
revealing the image through them) is accepted and renders as flat type, so text-as-mask is
not reachable on this route at all.
