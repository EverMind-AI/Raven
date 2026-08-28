# Passages: sequence, numbers, and the pages that are mostly not there

The worked code for `P16`-`P20` and `P22`, behind
[deck/build/references/layouts.md](deck/build/references/layouts.md). No figure and no grid
in any of them: these pages are built out of type, shapes and air.

Every block here runs. They assume the setup block of the skill's §3 plus `FACE`/`HAN` for
the theme's two faces, `INK`/`MUTED`/`ACCENT` for `T["foreground"]`, `T["muted"]` and
`T["accent"]`, and `frame = page()`.
A passage whose bands are measured before they are drawn ends that line as
`frame = page().holding(*heights)`, so the room the body was given and nothing asked for
becomes air above and below the run instead of a band of white along the page's foot (the
skill's §6.5). Not for a run something else already spreads into the whole body:
`card_group(..., down=True)` given the body puts the leftover between its own cards, and a
body cut to the sum of their heights first leaves them touching.

### P16 -- A timeline spine

`timeline` hands back a `Track`; each stop carries `box` under the spine and `above`
over it, which is where the label and the date go. Fill the rest of the page -- a spine
alone is a third of a page of content.

Where the numbers came from is a source note, and the page has a strip for it:
`page(footer=True)` gives up the foot of the body, and `frame.footer` is the box it hands
back.

```python
stops = ("立项", "试点", "灰度", "全量", "复盘")
dates = ("01-08", "03-02", "05-19", "07-30", "09-15")
said = ("四人两周", "两条产线", "10% 流量", "全部产线", "口径归档")
frame = page(footer=True)
down = stack(frame.body)
track = timeline(slide, down.take(1.70), T, stops)
for stop, name, when in zip(track.stops, stops, dates):
    write(slide, stop.above, when, size=LABEL_PT, colour=MUTED, font=FACE, cjk_font=HAN,
          align="center", anchor="bottom")
    write(slide, stop.box, name, size=BODY_PT, bold=True, colour=INK, font=FACE, cjk_font=HAN,
          align="center")
down.skip(GUTTER)
for box, one in zip(down.take(0.40).columns(5), said):
    write(slide, box, one, size=LABEL_PT, colour=MUTED, font=FACE, cjk_font=HAN, align="center")
down.skip(GUTTER)
write(slide, down.rest(), "灰度到全量之间隔了两个月，是等一条产线的检修窗口，不是技术原因。",
      size=BODY_PT, colour=INK, font=FACE, cjk_font=HAN)
write(slide, frame.footer, "来源：五个节点按同一批日志统计，口径与附录 A 一致。",
      size=LABEL_PT, colour=MUTED, font=FACE, cjk_font=HAN)
```

### P17 -- A chevron process row

Five rectangles with gaps between them is a list; five chevrons that interlock is a
process. `the_largest_step_this_copy_takes` per step, then the `min`, or a two-character
label sits at a fifth the height of the shape naming it.
[deck/build/references/shapes.md](deck/build/references/shapes.md) has the rest.

```python
labels = ("采集", "清洗", "标注", "训练", "评测")
down = stack(frame.body)
steps = chevron_row(slide, down.take(1.30), T, labels)
size = min(the_largest_step_this_copy_takes(name, one.box, font=FACE) for one, name in zip(steps, labels))
for one, name in zip(steps, labels):
    write(slide, one.box, name, size=size, colour=T["background"], font=FACE, cjk_font=HAN,
          align="center", anchor="middle")
down.skip(GUTTER)
for box, said in zip(down.rest().columns(5), (
    "四路信号", "对齐到 10Hz", "双人标注", "同一套权重", "四类任务一起跑",
)):
    write(slide, box, said, size=LABEL_PT, colour=MUTED, font=FACE, cjk_font=HAN, align="center")
```

### P19 -- The number at display size

One number, big enough to read from the back, with the reasoning beside it rather than
under it. `NUMBER_PT` is the ramp's step for this; a display number may go above it.

Each card's own title is the label, so the generic "这个数字怎么来的" heading over the
column has nothing left to say. `tint="background"` on the cards is what keeps them
visible on the `surface` ground under them: a card the same colour as its ground is not a
card. The ground is not only ink either: a display number is type, and `evidence` counts
a page as showing something at four filled shapes, so the three cards come to three and
the ground is the fourth.

```python
number, said = frame.body.split_left(0.42)
down = stack(number)
write(slide, down.take(1.50), "42ms", size=NUMBER_PT + 26, bold=True, colour=ACCENT, font=FACE)
write(slide, down.take(0.42), "端到端时延，四类任务合并统计", size=LABEL_PT, colour=MUTED,
      font=FACE, cjk_font=HAN)
notes = [("ruler", "口径", "P95，2 月 1 日至 3 月 31 日，剔除冷启动。"),
         ("history", "改造前", "121ms，四套权重串行跑完。"),
         ("target", "下一步", "其中 61% 落在第二阶段。")]
plane(slide, said, T, tint="surface")
lane = said.inset(PAD)
tall = max(card_size(lane.w, icon=i, title=h, body=b, font=FACE).h for i, h, b in notes)
rows = stack(lane).spread(*[tall] * len(notes))
for icon, head, body in notes:
    card(slide, rows.take(tall), T, tint="background", icon=icon, title=head, body=body,
         font=FACE, cjk_font=HAN)
```

### P20 -- A metric row across one band

Three to five numbers on one line, each with its own label and icon, and the band's own
reading under it. The icons are the difference between this and four boxes with numbers
in them.

`page(footer=True)` again, the same split as `P16`: the strip at the foot takes the basis
the four numbers share, and the band under the tiles is left to their reading.

```python
frame = page(footer=True)
down = stack(frame.body)
band = down.take(1.90)
for box, (value, name, icon) in zip(band.columns(4), [
    ("42ms", "端到端时延 P95", "stopwatch"),
    ("46.3", "mAP，四类任务均值", "target"),
    ("1x", "上线权重套数", "package"),
    ("-64%", "显存占用", "trending_down"),
]):
    plane(slide, box, T, tint="surface", radius=True)
    inner = stack(box.inset(PAD + 0.06))
    add_icon(slide, icon, Inches(inner.x0), Inches(inner.take(0.34).y0), Inches(0.30), ACCENT)
    write(slide, inner.take(0.90), value, size=NUMBER_PT, bold=True, colour=INK, font=FACE)
    write(slide, inner.rest(), name, size=LABEL_PT, colour=MUTED, font=FACE, cjk_font=HAN)
down.skip(GUTTER)
write(slide, down.rest(), "显存下降来自权重合并，不是量化：精度同期还高了 0.2。",
      size=BODY_PT, colour=INK, font=FACE, cjk_font=HAN)
write(slide, frame.footer, "来源：2 月至 3 月线上日志，四个数字同一批，口径见附录 A。",
      size=LABEL_PT, colour=MUTED, font=FACE, cjk_font=HAN)
```

### P18 -- Negative space dominant

Content under 40% of the canvas, and the air is the design. This is the one structure
`excessive_whitespace` exists to catch by accident, so spend it on a page that has one
thing to say -- a section's turn, a conclusion -- and never as a way of stopping early.

```python
said = "四类任务，一套权重。"
room = frame.body.rows(3)[1].columns(3, weights=(1, 3, 1))[1]
write(slide, room, said, size=TITLE_PT + 6, bold=True, colour=INK, font=FACE, cjk_font=HAN,
      align="center", anchor="middle")
rule(slide, Box.at(room.x0 + room.w / 2 - 0.52, room.y1 - 0.10, w=1.05, h=0.03), T)
```

### P22 -- A typographic page

No panel, no card, no figure: the sentence at display size and one quiet line under it.
A deck of these is a document being read out; one of them, where the argument turns, is
the page a reader remembers.

```python
down = stack(frame.body)
write(slide, down.take(2.10), "把任务定义搬进输入，\n网络本身就与任务无关。", size=TITLE_PT + 8,
      bold=True, colour=INK, font=FACE, cjk_font=HAN, spacing=1.25)
down.skip(0.10)
write(slide, down.take(0.46), "这句话是全篇唯一要记住的一句", size=BODY_PT, colour=MUTED,
      font=FACE, cjk_font=HAN)
```
