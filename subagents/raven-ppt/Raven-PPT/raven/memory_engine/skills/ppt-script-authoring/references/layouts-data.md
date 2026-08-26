# Passages: charts and tables as the page's bones

The worked code for `P14`, `P15`, `P21`, `P36`-`P41`, `M25` and `M26`, behind
[deck/build/references/layouts.md](deck/build/references/layouts.md). The table entries are
the larger half: what `table()`'s dials reach without a cell being drawn by hand, and where
they stop. The dials themselves, the seven `mark` kinds and a full hand-drawn table are in
[deck/build/references/tables.md](deck/build/references/tables.md); the chart forms are in
[deck/build/references/charts.md](deck/build/references/charts.md).

Every block here runs. They assume the setup block of the skill's §3 plus `FACE`/`HAN` for
the theme's two faces, `INK`/`MUTED`/`ACCENT` for `T["foreground"]`, `T["muted"]` and
`T["accent"]`, and `frame = page()`.

### P14 -- A chart with its reading in a lane beside it

A chart alone states numbers; the lane says what to conclude. Pair this with `M11` --
accent the one item the lane is about.

```python
plot, lane = frame.body.split_left(0.66)
horizontal_bar(slide, plot, T, [("华东", 46.3), ("华北", 42.1), ("西南", 39.4), ("华南", 31.8)],
               accent="华东", unit="%")
down = stack(lane)
write(slide, down.take(0.40), "读法", size=LEAD_PT, bold=True, colour=INK, font=FACE, cjk_font=HAN)
down.skip(0.08)
points(slide, down.rest(), T, [
    "华东一地占了四成，其余三地合计才追平。",
    "华南低于线的原因是三月才接入。",
], size=BODY_PT, font=FACE, cjk_font=HAN)
```

### P15 -- Two charts read against one scale

`axis_max` on both, or the reader compares two pictures that are not comparable: the
same 60 sits at 0.79in on one chart and 2.43in on the other.

```python
left, right = frame.body.split_left(0.5)
for box, (head, data) in zip((left, right), [
    ("去年", [("Q1", 118), ("Q2", 132), ("Q3", 141), ("Q4", 152)]),
    ("今年", [("Q1", 146), ("Q2", 158), ("Q3", 171), ("Q4", 185)]),
]):
    down = stack(box)
    write(slide, down.take(0.36), head, size=BODY_PT, bold=True, colour=INK, font=FACE, cjk_font=HAN)
    down.skip(0.08)
    column(slide, down.rest(), T, data, unit="M", axis_max=200)
```

### P21 -- A table with its reading beside it

`table_size(rows, T, box=grid)` says where the table ends before a cell is drawn, so the
lane starts at the right place and the page does not have to be built twice.
[deck/build/references/tables.md](deck/build/references/tables.md) has the rest.

```python
rows = [
    ["方案", "mAP", "时延", "显存"],
    ["四套权重", "46.1", "121ms", "44GB"],
    ["合并权重", "46.3", "42ms", "12GB"],
    ["合并 + 量化", "44.8", "31ms", "7GB"],
]
grid, lane = frame.body.split_left(0.62)
laid = table_size(rows, T, box=grid)
table(slide, Box(grid.x0, grid.y0, grid.x1, grid.y0 + laid.h), rows, T, emphasize_rows=(2,))
down = stack(lane)
write(slide, down.take(0.40), "读法", size=LEAD_PT, bold=True, colour=INK, font=FACE, cjk_font=HAN)
down.skip(0.08)
points(slide, down.rest(), T, [
    "合并权重这一行是本页的结论：精度打平，时延和显存各降三分之二。",
    "量化再降一档，但 1.5 个点的精度不在预算内。",
], size=BODY_PT, font=FACE, cjk_font=HAN)
```

### P36 -- A table as the page's whole ground, the conclusion floated over it

The whole body is the table, so the page's sentence has nowhere to sit but over it -- and
that is where it belongs: a reader who has been told what to look for reads seven rows in
one pass. `table_size(rows, T, box=room)` is asked before a cell is drawn, because six
rows at `BODY_PT` and six at `LABEL_PT` are not the same page and building it is the only
other way to find out.

```python
rows = [
    ["站点", "接入", "日均调用", "P95", "失败率", "口径"],
    ["华东一区", "2025-04", "182 万", "42ms", "0.03%", "网关日志"],
    ["华东二区", "2025-06", "141 万", "47ms", "0.04%", "网关日志"],
    ["华北", "2025-09", "96 万", "88ms", "0.11%", "网关日志"],
    ["西南", "2025-11", "74 万", "91ms", "0.13%", "网关日志"],
    ["华南", "2026-03", "38 万", "94ms", "0.21%", "网关日志，仅两月"],
    ["东北", "2026-03", "21 万", "97ms", "0.24%", "网关日志，仅两月"],
]
down = stack(frame.body)
write(slide, down.take(0.72), "只有两个华东站点把 P95 压在 50ms 以内，其余四地都卡在同一处：共享的旧网关。",
      size=LEAD_PT, bold=True, colour=INK, font=FACE, cjk_font=HAN)
down.skip(0.14)
room = down.rest()
size = BODY_PT if table_size(rows, T, box=room, size=BODY_PT).h <= room.h else LABEL_PT
table(slide, room, rows, T, size=size, style="row_rules", emphasize_rows=(1, 2))
```

### P37 -- A two-axis matrix, the cell being the answer

The cell is the answer, so the cells hold nothing: a mark handed an empty cell takes the
whole of it, and four columns of ticks and crosses are read across a row faster than four
columns of the words for them. The legend is not optional -- a tick, a cross and a
half-filled dot are conventions, and the page states them once, drawn with the same `mark`
that filled the cells. `weights` because the content is marks and not strings: without
them a matrix sizes itself off its labels and sits in half the page.

```python
rows = [
    ["", "增量索引", "跨区一致", "秒级回放", "冷启动"],
    ["现有网关", "", "", "", ""],
    ["自研调度", "", "", "", ""],
    ["托管方案", "", "", "", ""],
]
verdict = {
    (1, 1): "check", (1, 2): "cross", (1, 3): "partial", (1, 4): "cross",
    (2, 1): "check", (2, 2): "check", (2, 3): "check", (2, 4): "partial",
    (3, 1): "check", (3, 2): "partial", (3, 3): "cross", (3, 4): "check",
}
down = stack(frame.body)
table(slide, down.take(3.10), rows, T, size=BODY_PT, marks=verdict, style="header_tint",
      weights=(1.6, 1.0, 1.0, 1.0, 1.0), align=("left", "center", "center", "center", "center"),
      emphasize_rows=(2,))
down.skip(0.18)
key = down.take(0.34).split_left(0.62)[0]
for cell, (kind, name) in zip(key.columns(4, gutter=0.10), [
    ("check", "已支持"), ("partial", "部分支持"), ("cross", "不支持"), ("status_dot", "未评估"),
]):
    at, label = cell.split_left(0.14, gutter=0.06)
    mark(slide, at, T, kind)
    write(slide, label, name, size=LABEL_PT, colour=MUTED, font=FACE, cjk_font=HAN, anchor="middle")
write(slide, down.rest(), "四项里自研调度只在冷启动上让步，这一行是本页要投的票。",
      size=BODY_PT, colour=INK, font=FACE, cjk_font=HAN)
```

### P38 -- The rows dealt out as cards in a grid

The header becomes the field order and every card repeats it, which is the whole difference
between this and `P11`: the reader compares down the grid because the third line of every
card is the same field. Level the row with `card_size` off the tallest, and give the empty
cell to the sentence rather than inventing a sixth option to fill it.

```python
head = ["时延", "显存", "适用"]
records = [
    ("四套权重", ["121ms", "44GB", "任务少、算力足"]),
    ("合并权重", ["42ms", "12GB", "四类任务同时在线"]),
    ("合并 + 量化", ["31ms", "7GB", "边缘盒子，精度可让"]),
    ("蒸馏小模型", ["18ms", "3GB", "单任务、长尾不管"]),
    ("级联两段", ["66ms", "16GB", "长尾优先，时延可让"]),
]
cells = frame.body.grid(3, 2, gutter=GUTTER)
copy = [[f"{field}　{value}" for field, value in zip(head, values)] for _, values in records]
height = max(card_size(cells[0].w, title=name, body=lines, font=FACE).h
             for (name, _), lines in zip(records, copy))
for cell, (name, _), lines in zip(cells, records, copy):
    card(slide, Box(cell.x0, cell.y0, cell.x1, cell.y0 + height), T,
         title=name, body=lines, font=FACE, cjk_font=HAN)
write(slide, cells[5], "五套方案，同一组字段，同一个顺序。", size=BODY_PT, colour=MUTED,
      font=FACE, cjk_font=HAN, anchor="middle")
```

### P39 -- A grouped header spanning columns, sub-labels under it

`table()` has no column spans, so this one is drawn: `columns(5, gutter=0.0, weights=...)`
gives the five cells, a `stack` down the same box gives the bands, and a box across a span
is its first and last cells' own edges. Two things measured rather than guessed: `rule` is
capped at 1.05in and starts 0.06in *below* the box it underlines, so a rule the width of a
table is a thin `plane`; and the span band has to be tinted, or the two levels of header
read as one row of five labels.

```python
spans = [("", 1), ("旧网关（3 月）", 2), ("新网关（4 月）", 2)]
sub = ["站点", "P95", "失败率", "P95", "失败率"]
body = [
    ["华东一区", "88ms", "0.11%", "42ms", "0.03%"],
    ["华北", "121ms", "0.19%", "51ms", "0.05%"],
    ["西南", "134ms", "0.22%", "63ms", "0.07%"],
    ["华南", "141ms", "0.28%", "74ms", "0.09%"],
]
down = stack(frame.body)
grid = down.take(3.60)
cells = grid.columns(5, gutter=0.0, weights=(1.5, 1.0, 1.0, 1.0, 1.0))
rows_down = stack(grid)


def across(band, first, span=1):
    return Box(cells[first].x0, band.y0, cells[first + span - 1].x1, band.y1)


span_band = rows_down.take(0.44)
at = 0
for name, width in spans:
    room = across(span_band, at, width)
    if name:
        plane(slide, Box(room.x0 + 0.03, room.y0, room.x1 - 0.03, room.y1 - 0.06), T, tint="surface")
        write(slide, room, name, size=LABEL_PT, bold=True, colour=INK, font=FACE, cjk_font=HAN,
              align="center", anchor="middle")
    at += width
head_band = rows_down.take(0.42)
for index, label in enumerate(sub):
    write(slide, across(head_band, index), label, size=LABEL_PT, bold=True, colour=MUTED,
          font=FACE, cjk_font=HAN, align="left" if index == 0 else "right", anchor="bottom")
# `rule` is capped at 1.05in and offset 0.06in below its box, so a rule the width of
# a table is a thin `plane` instead.
plane(slide, Box(grid.x0, head_band.y1 - 0.015, grid.x1, head_band.y1 + 0.015), T, tint="accent")
for line in body:
    row = rows_down.take(0.52)
    for index, value in enumerate(line):
        write(slide, across(row, index), value, size=BODY_PT, colour=INK, font=FACE, cjk_font=HAN,
              align="left" if index == 0 else "right", anchor="middle")
    plane(slide, Box(grid.x0, row.y1 - 0.007, grid.x1, row.y1 + 0.007), T, tint="grid")
write(slide, down.rest(), "两组字段同名，所以上面那一层不是装饰：没有它，四列 P95 读起来是四个站点。",
      size=BODY_PT, colour=MUTED, font=FACE, cjk_font=HAN)
```

### P40 -- A table and a chart of the same numbers, on one scale

The same numbers twice, on purpose: the table is the reading and the bars are the shape.
One scale (`axis_max` on the chart), one order, and the accented row and the accented bar
the same item. What they will not do is line up -- `horizontal_bar` lays out its own rows,
so five bars and five table rows land at five different heights. The pairing is by order
and by the accent; do not draw a rule between the two implying otherwise.

```python
calls = [("华东一区", 182), ("华东二区", 141), ("华北", 96), ("西南", 74), ("华南", 38)]
detail = {"华东一区": ("42ms", "0.03%"), "华东二区": ("47ms", "0.04%"), "华北": ("88ms", "0.11%"),
          "西南": ("91ms", "0.13%"), "华南": ("94ms", "0.21%")}
rows = [["站点", "日均调用（万）", "P95", "失败率"]]
rows += [[name, f"{value}", *detail[name]] for name, value in calls]
grid, plot = frame.body.split_left(0.52)
laid = table_size(rows, T, box=grid, size=BODY_PT)
table(slide, Box(grid.x0, grid.y0, grid.x1, grid.y0 + laid.h), rows, T, size=BODY_PT,
      weights=(1.5, 1.3, 1.0, 1.0), emphasize_rows=(1,))
horizontal_bar(slide, Box(plot.x0, plot.y0, plot.x1, plot.y0 + laid.h), T, calls,
               accent="华东一区", unit="万", axis_max=200)
```

### P41 -- A statement: groups, indented detail, a total under a rule

The four dials that make a statement a statement, on one call: `group_rows` for the
section bands, `indent_rows` for the detail under them, `total_rows` for the bold row under
a rule, and `align` because a column of figures that is not right-aligned cannot be added
up down its length. A group's own row is a placeholder in `rows` -- its cells are never
measured and never set a column's width -- and a `delta` on the one figure the page is
about is the whole of the emphasis.

```python
rows = [
    ["科目（万元）", "2025", "2026E", "变动"],
    ["", "", "", ""],
    ["订阅", "4,120", "5,380", "+30.6%"],
    ["用量", "1,860", "2,940", "+58.1%"],
    ["收入合计", "5,980", "8,320", "+39.1%"],
    ["", "", "", ""],
    ["算力", "2,240", "2,610", "+16.5%"],
    ["人力", "1,510", "1,720", "+13.9%"],
    ["成本合计", "3,750", "4,330", "+15.5%"],
    ["毛利", "2,230", "3,990", "+78.9%"],
]
table(slide, frame.body, rows, T, size=BODY_PT,
      group_rows={1: "收入", 5: "成本"},
      indent_rows=(2, 3, 6, 7),
      total_rows=(4, 8, 9),
      align=("left", "right", "right", "right"),
      marks={(9, 3): "delta"})
```

### M25 -- A whole column given to one mark kind

One kind, one column, every row: what turns a column of "4.5/5" into something the eye
ranks without reading it. The number stays, because a mark takes the part of the cell its
own string does not need, so the column is a length *and* a reading. Keyed per cell rather
than per column, because that is what `marks` takes -- and a numeric kind given no value
reads the cell's own string, so the rating is written once.

```python
rows = [
    ["基线", "mAP", "端到端时延", "综合评价"],
    ["四套权重", "46.1", "121ms", "2/5"],
    ["合并权重", "46.3", "42ms", "4.5/5"],
    ["合并 + 量化", "44.8", "31ms", "3.5/5"],
    ["蒸馏小模型", "41.2", "18ms", "1.5/5"],
]
table(slide, frame.body.rows(2)[0], rows, T, size=BODY_PT, emphasize_rows=(2,),
      marks={(row, 3): "harvey" for row in range(1, len(rows))})
```

### M26 -- Cells tinted by their own value

A tint is a ranking and the figure is still the reading, so the cells keep their numbers.
The constraint is measured: `table` sets every cell's type in the theme's foreground, so
the deep end of the ramp is whatever `contrast` says still carries it. Four steps mixed
from the page's own ground toward its accent, and a legend under the grid saying what the
steps are -- without the legend a tint is decoration.

```python
rows = [
    ["场景", "华东", "华北", "西南", "华南"],
    ["检索", "0.94", "0.91", "0.88", "0.71"],
    ["排序", "0.89", "0.86", "0.79", "0.62"],
    ["改写", "0.81", "0.74", "0.66", "0.48"],
]


def tint(share):
    """`share` of the way from the page's ground to its accent, and nothing else."""
    near, far = T["background"].lstrip("#"), T["accent"].lstrip("#")
    channels = [int(near[at:at + 2], 16) + (int(far[at:at + 2], 16) - int(near[at:at + 2], 16)) * share
                for at in (0, 2, 4)]
    return "#" + "".join(f"{round(channel):02X}" for channel in channels)


# `table` sets every cell's type in the theme's foreground, so the deep end of the
# ramp is whatever still carries it: asked here rather than picked by eye.
deepest = max(share for share in (0.70, 0.60, 0.50, 0.40, 0.30, 0.20) if contrast(tint(share), INK) >= 4.5)
steps = [tint(deepest * (index + 1) / 4) for index in range(4)]
band = {}
for r, line in enumerate(rows[1:], start=1):
    for c, value in enumerate(line[1:], start=1):
        band[(r, c)] = steps[min(int(float(value) * len(steps)), len(steps) - 1)]
down = stack(frame.body)
table(slide, down.take(2.60), rows, T, size=BODY_PT, fills=band,
      weights=(1.4, 1.0, 1.0, 1.0, 1.0), align=("left", "center", "center", "center", "center"))
down.skip(0.16)
key = down.take(0.32).split_left(0.46)[0]
for cell, paint, name in zip(key.columns(4, gutter=0.08), steps, ("0.25", "0.50", "0.75", "1.00")):
    swatch, label = cell.split_left(0.40, gutter=0.06)
    rect(slide, swatch, paint)
    write(slide, label, name, size=LABEL_PT, colour=MUTED, font=FACE, anchor="middle")
```
