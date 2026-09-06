"""Every element this route writes, in an order PowerPoint will accept.

The one class of defect a pipeline that judges decks by rendering them cannot see.
ECMA-376 fixes the order of a great many elements' children; LibreOffice does not
check it and PowerPoint does, dropping what it cannot parse. So a deck can render
perfectly here, measure clean on every gate, and open in the room with its tables
restyled -- which is exactly what shipped: 175 cells across five tables in two
delivered decks, all carrying `lnB, lnT, lnR, lnL` where the schema says
`lnL, lnR, lnT, lnB`.

Scanned over a deck built only from these helpers, so a violation is this code's
and not some user template's. One page per primitive, so a new helper that writes
XML nobody has scanned before -- `a:prstGeom` with an `a:avLst` in it, an `a:ln`
carrying an arrowhead -- is covered the moment it is added rather than the next
time somebody remembers.
"""

from __future__ import annotations

import textwrap
import zipfile
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("pptx", reason="ppt extra not installed")

from lxml import etree

from raven_ppt.backends.script import run_script
from raven_ppt.backends.script.workspace import asset_helpers
from raven_ppt.contracts import Project

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"

# The fill group: any one of these, in the one position the group occupies.
_FILLS = ("noFill", "solidFill", "gradFill", "blipFill", "pattFill", "grpFill")

# The sequences, as ordered groups of alternatives. Only the elements these helpers
# actually write are listed -- a table of the whole schema would be a copy of the
# standard, and what this guards is our own output.
SEQUENCES: dict[str, list[tuple[str, ...]]] = {
    f"{_A}rPr": [
        ("ln",),
        _FILLS,
        ("effectLst", "effectDag"),
        ("highlight",),
        ("uLnTx", "uLn"),
        ("uFillTx", "uFill"),
        ("latin",),
        ("ea",),
        ("cs",),
        ("sym",),
        ("hlinkClick",),
        ("hlinkMouseOver",),
        ("rtl",),
        ("extLst",),
    ],
    f"{_A}tcPr": [
        ("lnL",),
        ("lnR",),
        ("lnT",),
        ("lnB",),
        ("lnTlToBr",),
        ("lnBlToTr",),
        ("cell3D",),
        _FILLS,
        ("headers",),
        ("extLst",),
    ],
    f"{_A}bodyPr": [
        ("prstTxWarp",),
        ("noAutofit", "normAutofit", "spAutoFit"),
        ("scene3d",),
        ("sp3d", "flatTx"),
        ("extLst",),
    ],
    f"{_P}spPr": [
        ("xfrm",),
        ("custGeom", "prstGeom"),
        _FILLS,
        ("ln",),
        ("effectLst", "effectDag"),
        ("scene3d",),
        ("sp3d",),
        ("extLst",),
    ],
    f"{_A}ln": [
        _FILLS,
        ("prstDash", "custDash"),
        ("round", "bevel", "miter"),
        ("headEnd",),
        ("tailEnd",),
        ("extLst",),
    ],
    f"{_A}blipFill": [("blip",), ("srcRect",), ("tile", "stretch")],
    f"{_A}pPr": [
        ("lnSpc",),
        ("spcBef",),
        ("spcAft",),
        ("buClrTx", "buClr"),
        ("buSzTx", "buSzPct", "buSzPts"),
        ("buFontTx", "buFont"),
        ("buNone", "buAutoNum", "buChar"),
        ("tabLst",),
        ("defRPr",),
        ("extLst",),
    ],
}
_RANK = {
    tag: {name: index for index, group in enumerate(groups) for name in group} for tag, groups in SEQUENCES.items()
}


# One page per primitive, so a violation names the helper that wrote it.
EVERY_PRIMITIVE = textwrap.dedent(
    """
    import os
    from pptx import Presentation
    from pptx.util import Inches
    from ppt_charts import (
        bullet, butterfly, column, dumbbell, gantt, grouped_bar, heatmap, histogram,
        horizontal_bar, matrix_2x2, pareto, progress_bar, scatter, stacked_bar, waterfall,
    )
    from ppt_icons import add_icon
    from ppt_layout import (
        Box, GUTTER, card, formula, heading, mark, page, plane, points, rule, table, write,
    )
    from ppt_shapes import connect, preset, timeline
    from ppt_theme import THEMES

    T = THEMES["ink-graphite"]
    F, C = T["font_family"], T["cjk_font_family"]
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)

    # SLIDE 1
    one = prs.slides.add_slide(prs.slide_layouts[6])
    frame = page(footer=True)  # this page cites, so it asks for the strip to cite in
    write(one, frame.kicker, "方法", size=12, colour=T["accent"], font=F, cjk_font=C)
    write(one, frame.title, "架构：Backbone 到共享解码器", size=30, bold=True, colour=T["foreground"], font=F, cjk_font=C)
    rule(one, frame.title, T)
    plane(one, frame.body.rows(2)[1], T, radius=True)
    write(one, frame.footer, "来源：Table 1", size=12, colour=T["muted"], font=F, cjk_font=C)
    add_icon(one, "target", Inches(0.72), Inches(2.1), Inches(0.42), T["accent"])

    # SLIDE 2
    two = prs.slides.add_slide(prs.slide_layouts[6])
    body = page().body
    table(two, body.rows(2)[0], [["方法", "AP"], ["TarViS", "48.3"], ["VITA", "45.7"]], T, numeric_from=1)
    for box, name in zip(body.rows(2)[1].columns(3, gutter=GUTTER), ("target", "clock", "chart-bar")):
        card(two, box, T, icon=name, title="语义查询是必要的", body="去掉后全线下降")

    # SLIDE 3 -- and with `points`, the a:pPr a bullet writes: buClr, buFont and
    # buChar or buAutoNum, all four of which sit after the paragraph's own lnSpc and
    # spcBef in the schema's order. Both list kinds, because they are different tags.
    three = prs.slides.add_slide(prs.slide_layouts[6])
    third = page()
    heading(three, third, T, "架构：共享解码器", kicker="方法")
    top, bottom = third.body.split_top(0.4)
    formula(three, top, "掩码 logits = (F_4, Q'_{inst})；分类 = concat(Q'_{sem}, Q'_{bg})", T)
    marked, counted = bottom.columns(2)
    points(three, marked, T, ["语义查询是必要的", "去掉它以后全线下降"])
    points(three, counted, T, ["采集", "清洗", "标注"], numbered=True)

    # SLIDE 4
    four = prs.slides.add_slide(prs.slide_layouts[6])
    grid = page().body
    table(
        four,
        grid.rows(2)[0],
        [["能力", "选项 A", "选项 B", "评分", "达成"],
         ["", "", "", "", ""],
         ["单点登录", "", "", "", "62%"],
         ["审计留痕", "", "", "", "88%"],
         ["小计", "2", "1", "", "+15%"]],
        T,
        numeric_from=3,
        emphasize_columns=(1,),
        group_rows={1: "核心访问"},
        indent_rows=(2, 3),
        total_rows=(4,),
        marks={
            (2, 1): "check", (2, 2): "cross", (3, 1): "partial", (3, 2): "check",
            (2, 3): "harvey:3.5", (3, 3): "harvey:4:accent",
            (2, 4): "progress", (3, 4): "progress:88%",
            (4, 1): "status_dot:accent", (4, 4): "delta",
        },
    )
    for box, spec in zip(grid.rows(2)[1].columns(4), ("harvey:2.5", "delta:0", "progress:0%", "status_dot:muted")):
        kind, _, value = spec.partition(":")
        mark(four, box, T, kind, value or None)

    # SLIDE 5
    five = prs.slides.add_slide(prs.slide_layouts[6])
    rows = page().body.rows(3)
    steps = timeline(five, rows[0], T, 5)
    for stop, label in zip(steps.stops, ("采集", "清洗", "标注", "训练", "评测")):
        write(five, stop.above, label, size=16, colour=T["foreground"], font=F, cjk_font=C, align="center")
    track = timeline(five, rows[1], T, 4)
    for stop, when in zip(track.stops, ("Q1", "Q2", "Q3", "Q4")):
        write(five, stop.box, when, size=14, colour=T["muted"], font=F, cjk_font=C, align="center")
    left, right = rows[2].columns(2)
    preset(five, left, T, "flowChartDecision", tint="accent_soft", outline="accent")
    preset(five, right, T, "roundRect", adj=0.06, tint="surface")
    connect(five, left, right, T)

    # SLIDE 6 -- every chart, and with them every construction ppt_charts writes:
    # filled rectangles (bars, tracks, tints, quadrant grounds), hairline rectangles
    # (baselines, axes, connectors, gridlines), ovals (marks), freeform polylines
    # (the cumulative curve) and the labels on all of them.
    six = prs.slides.add_slide(prs.slide_layouts[6])
    grid = page().body.grid(3, 2)
    column(six, grid[0], T, [("东部", 185), ("南部", 142), ("北部", 128)], accent="东部", unit="M")
    horizontal_bar(six, grid[1], T, {"TarViS": 48.3, "VITA": 45.7, "IDOL": 43.9}, accent=0)
    grouped_bar(six, grid[2], T, ["Q1", "Q2"], [("A", [120, 145]), ("B", [85, 98])], accent="A")
    stacked_bar(six, grid[3], T, ["FY24", "FY25"], {"授权": [140, 160], "服务": [90, 100]})
    progress_bar(six, grid[4], T, [("单点登录", "88%"), ("审计留痕", 0.62)], accent=0)
    bullet(six, grid[5], T, [("收入", 182, 200), ("留存", 92, 90)], accent="留存")

    # SLIDE 7
    seven = prs.slides.add_slide(prs.slide_layouts[6])
    cells = page().body.grid(3, 2)
    butterfly(seven, cells[0], T, [("18-24", 320, 280), ("25-34", 460, 510)], sides=("成本", "收入"))
    dumbbell(seven, cells[1], T, [("结账", 61, 78), ("搜索", 44, 71)], sides=("前", "后"), unit="%")
    waterfall(seven, cells[2], T, [("期初", 280), ("增长", 120), ("成本", -45), ("期末", 355)], totals=(0, -1))
    pareto(seven, cells[3], T, [("功能", 350), ("外观", 220), ("缺件", 150)], accent=0)
    histogram(seven, cells[4], T, [12, 15, 18, 22, 25, 31, 38, 44, 52, 61, 72, 80])
    gantt(seven, cells[5], T, [("调研", "2026-01-06", "2026-02-14"), ("构建", "2026-02-01", "2026-04-20")], accent="构建")

    # SLIDE 8
    eight = prs.slides.add_slide(prs.slide_layouts[6])
    left, right = page().body.split_left(0.5)
    top, bottom = left.split_top(0.5)
    heatmap(eight, top, T, ["北", "南"], ["Q1", "Q2", "Q3"], [[12, 18, 24], [9, 11, 14]])
    scatter(eight, bottom, T, [("在线", 48, 420, 28), ("线下", 52, 410, 12)], axes=("投入", "收入"), accent="在线")
    matrix, whole = right.split_top(0.66)
    matrix_2x2(
        eight,
        matrix,
        T,
        [("单点登录", 8, 3), ("审计", 6, 7)],
        axes=("投入", "影响"),
        quadrants=("速赢", "重注", "填空", "陷阱"),
        accent="审计",
        limits=(0, 10, 0, 10),
    )
    stacked_bar(eight, whole, T, ["FY25"], {"授权": [220], "服务": [130]}, share=True, direction="bar")

    prs.save(os.environ["PPT_OUTPUT"])
    """
).lstrip()


def _written(path: Path) -> set[str]:
    """Every element name the deck carries.

    A sequence is only guarded while something still writes it, and an unwritten one
    passes this scan in silence: `a:pPr`'s bullet children were listed here for as
    long as no page called `points`, and nothing said so.
    """
    names: set[str] = set()
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith(".xml"):
                names.update(element.tag.split("}")[-1] for element in etree.fromstring(archive.read(name)).iter())
    return names


def _violations(path: Path) -> Counter:
    found: Counter = Counter()
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml"):
                continue
            for element in etree.fromstring(archive.read(name)).iter():
                ranks = _RANK.get(element.tag)
                if ranks is None:
                    continue
                written = [ranks[child.tag.split("}")[-1]] for child in element if child.tag.split("}")[-1] in ranks]
                if written != sorted(written):
                    order = tuple(child.tag.split("}")[-1] for child in element)
                    found[(element.tag.split("}")[-1], order)] += 1
    return found


@pytest.mark.asyncio
async def test_nothing_this_route_writes_is_out_of_the_schema_s_order(tmp_path: Path) -> None:
    project = Project(workspace=tmp_path, slug="orders")
    outcome = await run_script(project, EVERY_PRIMITIVE, helpers=asset_helpers(), timeout_s=120.0)
    assert outcome.ok, outcome.stderr

    # Nothing in the Office default carries either of these, so they are here only
    # because a page called `points` -- and if one stops, the a:pPr sequence above
    # goes back to guarding nothing.
    assert {"buClr", "buAutoNum", "buChar", "buFont"} <= _written(outcome.pptx_path)

    found = _violations(outcome.pptx_path)
    assert found == Counter(), "elements out of the schema's order: " + "; ".join(
        f"{count}x {tag} {list(order)}" for (tag, order), count in found.most_common()
    )


def test_the_check_would_catch_the_defect_that_shipped(tmp_path: Path) -> None:
    """The scan itself, against the XML two delivered decks actually carried."""
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(6), Inches(1.5)).table
    for row in table.rows:
        for cell in row.cells:
            properties = cell._tc.get_or_add_tcPr()
            for edge in ("lnL", "lnR", "lnT", "lnB"):
                element = properties.makeelement(f"{_A}{edge}", {})
                element.append(element.makeelement(f"{_A}noFill", {}))
                properties.insert(0, element)  # what shipped
    built = tmp_path / "reversed.pptx"
    presentation.save(str(built))

    found = _violations(built)
    assert sum(found.values()) == 4, found
    assert next(iter(found))[1][:4] == ("lnB", "lnT", "lnR", "lnL")
