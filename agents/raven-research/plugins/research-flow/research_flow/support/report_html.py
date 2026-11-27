"""The report's reading page: one self-contained HTML file rendered from the report's markdown.

The markdown stays the report. This module only lays it out for reading: a cover that
carries the answer, a table of contents, one panel for each ``##`` section, numbered
citations with a source list. A report that closes with a source list of its own, as a
list or a table, keeps it: the page numbers the citations into that list and adds no
second one.

Inside a section the page sets four shapes of markdown as blocks of their own. A list
whose items each open with a name in bold is a list of terms, or of numbered points when
it is numbered; so is a run of paragraphs that each open with one. The name is a label
that a colon or a dash sets off, or a whole lead sentence. A blockquote is a note, or a
quotation when it ends with a line that names its source. Two to four short ``###``
parts that make up a section's subsections are cards. The colon or dash between a name
and its text is not printed: the layout sets the two apart. Any other list, quote or
subsection stays as it is.

Two more blocks come from what the report already writes. Three or more ``###`` parts in
a row whose headings name a step each ("Step 2: ...") are stages on one line. A body
paragraph that opens with a risk or note label (``**Risk**:``, ``Note:`` or their
Chinese twins) is set as a note under that label, where it stands and with every word
it has - except inside cards, where one boxed card among plain peers reads as a
different kind of part.

The report's text closes the same way before it reaches the page or the report file
(:func:`close_with_sources`): the limits, then the list of sources. So the file holds
the list the page shows.

The research trail the flow appends to the reply is not on the page, its warnings
included. It records how the turn worked, for whoever ran it; the markdown report file
leaves it out too, and the page holds what that file holds.

Three rules keep the page safe and faithful:

* Every character on the page comes from the report it is given, the highlights the
  flow checked against that report, or the fixed labels below. Nothing is paraphrased here.
* All text is escaped and a link keeps its address only for ``http``, ``https`` and
  ``mailto``. The report quotes pages from the web, so its text is not trusted.
* The page loads nothing: no script, no image, no font, no remote stylesheet. The
  content security policy in its head refuses them even if one were written.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, replace
from html import escape
from urllib.parse import urlsplit, urlunsplit

from raven.i18n import t_in
from research_flow.support.report_file import measure_report
from research_flow.support.report_markdown import (
    Block,
    Span,
    names_sources,
    parse_blocks,
    parse_inline,
    plain_units,
    section_starts,
    span_text,
    step_number,
)

_LINK_SCHEMES = ("http", "https", "mailto")
#: A table of contents helps from this many sections; below it the page is one column.
_CONTENTS_FROM = 3
_TITLE_CHARS = 90
#: Reading speed: CJK characters and other words per minute.
_CJK_PER_MINUTE = 400
_WORDS_PER_MINUTE = 220

#: Marks a file as a page this module wrote: only such a file is written over.
GENERATOR = '<meta name="generator" content="raven-research-report">'

#: A citation mark in running text, and one that opens a line of a reference list.
_MARK_RE = re.compile(r"\[(\d{1,3})\]")
_ENTRY_RE = re.compile(r"\[(\d{1,3})\][ \t]*")
#: The name a mark is glued to, when it is glued to one.
_GLUED_NAME_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_]+\Z")
#: A list or table lists sources when this share of its entries carry an address.
_REFERENCE_SHARE = 0.8
#: The same share under a heading that names the sources: there an entry citing a paper by
#: its arXiv number alone is still a source, and a list missing a few links is still the list.
_HEADED_SHARE = 0.5
#: A list of sources is the report's own when it holds this share of the addresses the report links.
_COVERED_SHARE = 0.5
#: A table's first cell that is the row's own number: ``3``, ``[3]``, ``3.``.
_ROW_NUMBER_RE = re.compile(r"\[?(\d{1,3})\]?\.?")
# A run of sources in one paragraph: a bar, a semicolon, an enumeration comma or a line end before each next link.
_RUN_SPLIT_RE = re.compile(r"[|\uff5c;\uff1b\u3001\n][ \t]*(?=\[|<?https?://)", re.IGNORECASE)
_RUN_OPEN_RE = re.compile(r"\[|<?https?://", re.IGNORECASE)
_RUN_LABEL_CHARS = 40
#: The heading the flow gives a list of sources it writes itself, in each language it has.
_SOURCE_LABELS = frozenset(t_in(language, "Sources") for language in ("en", "zh"))
#: The fixed heading of the limits, as the report clause names it (gates/report_shape.py).
#: The fixed limits heading alone, numbered or not: a section that shares it with other
#: content ("Advice and limitations") keeps that content, so the page never leaves it off.
_LIMITS_LABEL_RE = re.compile(
    r"(?:\(?\d{1,2}[.)\u3001]|[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{1,3}\u3001)?"
    r"\s*(?:limitations?|\u672a\u6838\u9a8c\u9879)\s*",
    re.I,
)
#: A heading that carries its own number: ``3.``, ``3)``, ``(3)``, a Chinese numeral and its comma.
_NUMBERED_HEADING_RE = re.compile(
    r"\(?\d{1,2}[.)\u3001]|[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{1,3}\u3001"
)
#: A key figure wider than these (a CJK character counts 2) is set one size smaller, so a tile holds it on one line.
_FIGURE_WIDE = 8
_FIGURE_WIDER = 13

#: A bold lead longer than this is a paragraph in bold, not a name or a lead sentence.
_NAME_CHARS = 80
#: A paragraph that opens with a bold subsection number - "(1)" in Chinese parentheses with a
#: Chinese numeral, "(1)", "1.1" - is that subsection's heading written as a bold lead. A "1.1"
#: is a number only with a closing point after it or a name past a space: "2.40x", "1.2%" and
#: "1.5 s" are figures.
_SUBHEAD_LEAD_RE = re.compile(
    r"\*\*(?P<name>(?:\uff08[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{1,3}\uff09"
    r"|\([0-9]{1,2}\)|[0-9]{1,2}\.[0-9]{1,2}(?:\.|(?=[ \t]+(?:[A-Z]|[^\x00-\x7f]))))[^*\n]{1,78}?)\*\*"
    r"[ \t]*(?:[:\uff1a][ \t]*)?(?P<rest>.*)\Z",
    re.DOTALL,
)
_SUBHEAD_END_RE = re.compile(r"[ \t]*[.:\u3002\uff1a]+\Z")
#: A run of paragraphs that each open with a name is set as numbered points from this many on.
_POINTS_FROM = 3
#: A bold lead that is a whole sentence: it ends where the sentence does, a closing quote or bracket allowed after.
_NAME_STOP_RE = re.compile(r"[.!?\u3002\uff01\uff1f][\"'\u201d\u300d\u300f)\uff09]*\Z")
# What separates a name from its text: a colon, a dash, or a hyphen with a space after it.
_NAME_END_RE = re.compile(r"[ \t]*[:\uff1a][ \t]*\Z")
#: A short label a body paragraph opens with, set in bold: one word (``Risk:``), or two to six CJK characters.
_LABEL_RE = re.compile(r"(?P<label>[\u4e00-\u9fff]{2,6}|[A-Z][a-z]{2,11})(?P<colon>[:\uff1a])(?=[ \t]*[^ \t:\uff1a])")
#: A table whose first column is all names this short reads by row: the first cell of a row is set in bold.
_KEY_WIDTH = 24
#: The widest bold that opens a paragraph and still names it (a CJK character counts 2).
_NAME_WIDTH = 32
#: Bold this short is a name even when it ends as a sentence does: ``**Industry.** text``.
#: The labels the outline clause asks a paragraph to open with (``prompts._OUTLINE_STYLE_EDITS``):
#: a paragraph that opens with one is a verdict or a caveat, not a named point.
_LABEL_WORDS = frozenset({"\u7ed3\u8bba", "\u98ce\u9669", "\u6ce8\u610f", "conclusion", "risk", "note"})
#: The labels that set a paragraph as a note: a risk or a caveat, not a verdict.
_NOTE_WORDS = _LABEL_WORDS - {"\u7ed3\u8bba", "conclusion"}
#: An absolute local path, which a report may cite in its list of sources in place of an address:
#: ``/data/notes.md``, a file at the root (``/notes.md``, its extension telling it from a
#: fraction such as ``a /b``), or a Windows path (``C:\notes\a.md``).
_LOCAL_PATH_RE = re.compile(
    r"(?<![\w/:.~\\])"
    r"(?:~?/(?:[^\s/()\[\]<>`]+/)+[^\s/()\[\]<>`]+"
    r"|~?/[^\s/()\[\]<>`.]+\.\w+"
    r"|[A-Za-z]:[\\/](?:[^\s\\/()\[\]<>`]+[\\/])*[^\s\\/()\[\]<>`]+)"
)
_SHORT_NAME_WIDTH = 20
_TEXT_START_RE = re.compile(r"[ \t]*(?:[:\uff1a]|\u2014{1,2}|\u2013|-(?=[ \t]))[ \t]*")
#: The last line of a quotation that names who said it: it opens with a dash.
_ATTRIBUTION_RE = re.compile(r"(?:\u2014{1,2}|\u2013|--)[ \t]*\S")
_ATTRIBUTION_CHARS = 160
#: Subsections become cards when a section has this many and each is this short (a CJK character counts 2).
_CARDS_FROM = 2
_CARDS_TO = 4
_CARD_WIDTH = 640
#: A run of step subsections is set as stages from this many on.
_STAGES_FROM = 3
#: A paragraph wider than this is no note: a box that long is a section, not a remark (a CJK character counts 2).
_NOTE_WIDTH = 640

_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"

STYLE = """
:root{--ink:#17140c;--ink-2:#3c3b38;--ink-3:#676561;--ink-4:#908f8c;--line:#e0e0e0;--hair:#ebebeb;
--canvas:#f5f5f5;--panel:#fffefc;--chip:rgb(23 20 12/.05);--shade:#f7f6f3;--brand:#ffce5c;--brand-tint:#fef1d1;
--glow:rgb(255 206 92/.18);--note-tint:#fff8e6;--note-line:#f3e2b3;
--shadow:0 1px 2px rgb(23 20 12/.04),0 8px 24px rgb(23 20 12/.05);
--sans:"Inter Variable","Inter","MiSans UI","MiSans",-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
"Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
--display:"Atkinson Hyperlegible Next Variable","Atkinson Hyperlegible Next",var(--sans);
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;background:radial-gradient(56% 400px at 100% 0,var(--glow),transparent 70%) no-repeat,var(--canvas);
color:var(--ink-2);font:16px/1.85 var(--sans);letter-spacing:.02em;line-break:strict;text-autospace:normal;
overflow-wrap:break-word;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
a{color:var(--ink);text-decoration:underline;text-decoration-color:var(--ink-4);text-decoration-thickness:1px;
text-underline-offset:3px}
a:hover{text-decoration-color:var(--ink)}
.page{max-width:1160px;margin:0 auto;padding:56px 24px 96px;display:grid;
grid-template-columns:208px minmax(0,1fr);gap:56px}
.page.is-single{grid-template-columns:minmax(0,820px);justify-content:center}
.contents{position:sticky;top:28px;align-self:start;max-height:calc(100vh - 56px);overflow:auto;letter-spacing:0}
.contents-title{margin:0 0 10px;padding-left:10px;font-size:12px;font-weight:600;letter-spacing:.08em;
text-transform:uppercase;color:var(--ink-4)}
.contents ol{list-style:none;margin:0;padding:0;counter-reset:entry}
.contents a{display:grid;grid-template-columns:22px minmax(0,1fr);gap:6px;padding:7px 10px;border-radius:8px;
color:var(--ink-3);font-size:13px;line-height:1.5;text-decoration:none;counter-increment:entry}
.contents a::before{content:counter(entry,decimal-leading-zero);font:11px/19.5px var(--mono);color:var(--ink-4)}
.contents a:hover{background:var(--panel);color:var(--ink)}
.cover{padding:4px 4px 0}
.kicker{display:flex;align-items:center;gap:8px;margin:0 0 14px;font-size:13px;font-weight:500;color:var(--ink-3)}
.kicker::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--brand)}
h1{margin:0 0 22px;font:700 36px/1.25 var(--display);letter-spacing:-.015em;color:var(--ink);text-wrap:balance;
word-break:auto-phrase}
.answer{margin:0;padding:2px 0 2px 20px;border-left:3px solid var(--brand);color:var(--ink);font-size:17px;
line-height:1.8}
.answer p{margin:0 0 12px}
.answer>:first-child{margin-top:0}
.answer>:last-child{margin-bottom:0}
.meta{display:flex;flex-wrap:wrap;gap:6px 18px;margin:18px 0 0;font-size:13px;color:var(--ink-4)}
.section{margin-top:56px;scroll-margin-top:24px}
main>.panel{margin-top:16px}
.panel{padding:32px;background:var(--panel);border:1px solid var(--hair);border-radius:20px;
box-shadow:var(--shadow)}
.panel>:first-child{margin-top:0}
.panel>:last-child{margin-bottom:0}
h2{margin:0 0 16px;padding:0 4px;font:700 23px/1.35 var(--display);letter-spacing:-.005em;color:var(--ink);
text-wrap:balance;word-break:auto-phrase}
h3{margin:32px 0 8px;font-size:18px;line-height:1.45;letter-spacing:0;color:var(--ink)}
h4{margin:24px 0 8px;font-size:16px;line-height:1.5;letter-spacing:0;color:var(--ink)}
p{margin:0 0 16px}
ul,ol{margin:0 0 16px;padding-left:1.4em}
li{margin:6px 0}
li::marker{color:var(--ink-4)}
li>p{margin:0 0 6px}
strong{color:var(--ink);font-weight:600}
blockquote{margin:0 0 16px;padding:12px 16px;border-left:2px solid var(--line);border-radius:0 8px 8px 0;
background:var(--canvas)}
blockquote>:last-child{margin-bottom:0}
hr{border:0;border-top:1px solid var(--hair);margin:24px 0}
code{font:.875em var(--mono);letter-spacing:0;padding:.15em .4em;border-radius:4px;background:var(--chip);
color:var(--ink)}
pre{margin:0 0 16px;padding:14px 16px;overflow:auto;border:1px solid var(--hair);border-radius:12px;
background:var(--shade);line-height:1.6}
pre code{padding:0;background:none;font-size:13.5px}
.table-scroll{margin:4px 0 18px;overflow-x:auto;border:1px solid var(--hair);border-radius:12px}
table{width:100%;border-collapse:collapse;font-size:14px;line-height:1.6;letter-spacing:.01em;
font-variant-numeric:tabular-nums}
th,td{padding:11px 14px;text-align:left;vertical-align:top;border-bottom:1px solid var(--hair)}
th{background:var(--shade);color:var(--ink-3);font-size:13px;font-weight:600;white-space:nowrap}
tr:last-child>td{border-bottom:0}
.keyed td:first-child{color:var(--ink);font-weight:600}
.cite{font-size:.66em;line-height:0;margin:0 1px 0 2px;letter-spacing:0}
.cite a{padding:1px 5px;border-radius:999px;background:var(--chip);color:var(--ink-3);font-weight:600;
text-decoration:none}
.cite a:hover{background:var(--brand-tint);color:var(--ink)}
.panel.is-limits{background:var(--note-tint);border-color:var(--note-line);box-shadow:none}
.band-title{margin:0 0 14px;font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;
color:var(--ink-4)}
.figures{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:16px;margin-top:24px}
.figure{padding:20px 24px;background:var(--panel);border:1px solid var(--hair);border-radius:16px;
box-shadow:var(--shadow)}
.figure-value{margin:0;font-family:var(--display);font-size:28px;font-weight:700;line-height:1.2;
letter-spacing:-.01em;color:var(--ink)}
.figure-value.is-wide{font-size:22px;line-height:1.3}
.figure-value.is-wider{font-size:17px;line-height:1.4}
.figure-label{margin:8px 0 0;font-size:13px;line-height:1.5;color:var(--ink-3)}
.findings{list-style:none;margin:0;padding:0;counter-reset:finding}
.findings li{position:relative;margin:0;padding:12px 0 12px 42px;border-top:1px solid var(--hair);
counter-increment:finding;color:var(--ink)}
.findings li:first-child{border-top:0;padding-top:0}
.findings li::before{content:counter(finding);position:absolute;left:0;top:13px;width:26px;height:26px;
border-radius:50%;background:var(--brand-tint);color:var(--ink);font-size:13px;font-weight:700;letter-spacing:0;
line-height:26px;text-align:center}
.findings li:first-child::before{top:1px}
.timeline{list-style:none;margin:0;padding:0 0 0 18px;border-left:2px solid var(--hair)}
.timeline li{position:relative;margin:0;padding:0 0 16px 8px}
.timeline li:last-child{padding-bottom:0}
.timeline li::before{content:"";position:absolute;left:-25px;top:10px;width:12px;height:12px;border-radius:50%;
background:var(--brand);border:2px solid var(--panel)}
.when{display:block;font-size:13px;font-weight:600;color:var(--ink-3)}
.sources{list-style:none;margin:0;padding:0;font-size:14px;line-height:1.7}
.sources li{display:grid;grid-template-columns:30px minmax(0,1fr);gap:0 8px;margin:0;padding:9px 8px;
border-radius:8px}
.sources li:target{background:var(--brand-tint)}
.source-number{color:var(--ink-4);font-weight:600;letter-spacing:0}
.source-host{display:block;color:var(--ink);font-weight:600}
.source-link{overflow-wrap:anywhere;font-size:13px}
.sources li>span:last-child,.source-text{overflow-wrap:anywhere}
.source-text>:last-child{margin-bottom:0}
td.source-number{width:1%;white-space:nowrap}
tr:target>td{background:var(--brand-tint)}
.terms{margin:0 0 16px}
.terms>div{padding:14px 0;border-top:1px solid var(--hair)}
.terms>div:first-child{padding-top:0;border-top:0}
.terms>div:last-child{padding-bottom:0}
.terms dt{margin:0 0 2px;font-weight:600;color:var(--ink)}
.terms dd{margin:0}
.terms dd>:last-child,.steps>li>:last-child,.stages>li>:last-child,.note>:last-child,.card>:last-child{margin-bottom:0}
.steps{list-style:none;margin:0 0 16px;padding:0;counter-reset:step}
.steps>li{position:relative;margin:0;padding:14px 0 14px 44px;border-top:1px solid var(--hair);
counter-increment:step}
.steps>li:first-child{padding-top:0;border-top:0}
.steps>li:last-child{padding-bottom:0}
.steps>li::before{content:counter(step);position:absolute;left:0;top:15px;width:28px;height:28px;border-radius:50%;
background:var(--brand-tint);color:var(--ink);font-size:13px;font-weight:700;letter-spacing:0;line-height:28px;
text-align:center}
.steps>li:first-child::before{top:1px}
.step-name{margin:0 0 2px;font-weight:600;color:var(--ink)}
.stages{list-style:none;margin:8px 0 16px;padding:0}
.stages>li{position:relative;margin:0;padding:0 0 26px 30px}
.stages>li:last-child{padding-bottom:0}
.stages>li::before{content:"";position:absolute;left:0;top:7px;width:12px;height:12px;border-radius:50%;
background:var(--brand);box-shadow:0 0 0 4px var(--brand-tint)}
.stages>li::after{content:"";position:absolute;left:5.5px;top:28px;bottom:2px;width:1px;background:var(--line)}
.stages>li:last-child::after{display:none}
.stages h3{margin:0 0 8px}
.note{margin:4px 0 18px;padding:14px 18px;border:1px solid var(--note-line);border-radius:12px;
background:var(--note-tint);font-size:15px;line-height:1.75}
.is-limits .note{background:var(--panel)}
.note-name{margin:0 0 4px;font-size:13px;font-weight:600;letter-spacing:.04em;color:var(--ink)}
.quote{margin:4px 0 18px;padding:16px 20px;border-left:3px solid var(--brand);border-radius:0 12px 12px 0;
background:var(--shade)}
.quote blockquote{margin:0;padding:0;border:0;background:none;color:var(--ink);font-size:17px;line-height:1.75}
.quote figcaption{margin-top:10px;font-size:13px;color:var(--ink-3)}
.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin:4px 0 18px}
.cards.is-3{grid-template-columns:repeat(3,minmax(0,1fr))}
.card{padding:18px 20px;border:1px solid var(--hair);border-radius:14px;background:var(--shade);font-size:14.5px;
line-height:1.7}
.card h3{margin:0 0 8px;font-size:16px}
.preface{margin:16px 0 0;font-size:13.5px;line-height:1.7;color:var(--ink-3)}
.preface>:last-child{margin-bottom:0}
@media screen and (prefers-color-scheme:dark){:root{--ink:#f5f3ee;--ink-2:#d3d0c8;--ink-3:#a5a29a;--ink-4:#7c7a75;
--line:#3a3835;--hair:#2e2c29;--canvas:#141311;--panel:#1c1b19;--chip:rgb(245 243 238/.08);--shade:#211f1d;
--brand:#ffce5c;--brand-tint:#3a3018;--glow:rgb(255 206 92/.07);--note-tint:#252013;--note-line:#4a3f1f;--shadow:none}}
@media (max-width:900px){.page{grid-template-columns:minmax(0,1fr);gap:24px;padding:32px 16px 56px}
.contents{display:none}h1{font-size:28px}h2{font-size:20px}.section{margin-top:40px}
.panel{padding:24px 16px;border-radius:16px}.cards,.cards.is-3{grid-template-columns:minmax(0,1fr)}}
@media print{body{background:#fff}.page{display:block;max-width:none;padding:0}.contents{display:none}
.panel,.figure{border-color:#ccc;box-shadow:none}.figure,.note,.quote,.card,.steps>li{break-inside:avoid}}
""".strip()


@dataclass(frozen=True)
class Figure:
    """One key figure: a value and what it measures, both in the report's own words."""

    value: str
    label: str


@dataclass(frozen=True)
class Moment:
    """One dated event of the report's timeline, both parts in the report's own words."""

    when: str
    what: str


@dataclass(frozen=True)
class Highlights:
    """What the page shows above the body. Each string is the report's text."""

    figures: tuple[Figure, ...] = ()
    findings: tuple[str, ...] = ()
    timeline: tuple[Moment, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.figures or self.findings or self.timeline)


class _Sources:
    """The addresses the report cites, by number.

    A report that carries a source list of its own keeps it (``listed``): its links,
    and its ``[n]`` marks when the list's numbers are its own (``marked``), point at
    that list, and the page adds no second one. Any other report gets a list from the
    page, numbered in the order the report first links each address.
    """

    def __init__(self, listed: dict[int, str] | None = None, marked: bool = True) -> None:
        self.listed = listed or {}
        self.marked = marked
        self._listed_by_url = {_key(url): number for number, url in reversed(self.listed.items())}
        self.numbers: dict[str, int] = {}

    def number(self, url: str) -> int | None:
        if self.listed:
            return self._listed_by_url.get(_key(url))
        return self.numbers.setdefault(url, len(self.numbers) + 1)

    @property
    def count(self) -> int:
        return len(self.listed) or len(self.numbers)


def _key(url: str) -> str:
    """An address as two spellings of it compare: no fragment, no closing slash, the host in lower case."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def _cite(number: int) -> str:
    return f'<sup class="cite"><a href="#source-{number}">{number}</a></sup>'


def _scheme(url: str) -> str:
    try:
        return urlsplit(url).scheme.lower()
    except ValueError:
        return ""


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _inline(spans: tuple[Span, ...], sources: _Sources | None, in_link: bool = False) -> str:
    """The spans as HTML. ``in_link`` is on inside an anchor's label, where a link is its words.

    A model cites ``[https://x](https://x)``: the label parses as a link of its own, and
    an anchor inside an anchor is not HTML. Inside a label a link renders as its text.
    """
    out: list[str] = []
    for span in spans:
        if span.kind == "text":
            out.append(_plain(span.text, sources))
        elif span.kind == "code":
            out.append(f"<code>{escape(span.text, quote=False)}</code>")
        elif span.kind == "break":
            out.append("<br>")
        elif span.kind == "link":
            out.append(_link(span, sources, nested=in_link))
        else:
            out.append(f"<{span.kind}>{_inline(span.children, sources, in_link)}</{span.kind}>")
    return "".join(out)


def _plain(text: str, sources: _Sources | None) -> str:
    """Escaped text. A ``[n]`` that names an entry of the report's own numbered source list becomes a citation."""
    if sources is None or not sources.listed or not sources.marked:
        return escape(text, quote=False)
    out: list[str] = []
    at = 0
    for mark in _MARK_RE.finditer(text):
        number = int(mark.group(1))
        if number in sources.listed and not _indexes(text, mark.start()):
            out.append(escape(text[at : mark.start()], quote=False) + _cite(number))
            at = mark.end()
    out.append(escape(text[at:], quote=False))
    return "".join(out)


def _indexes(text: str, at: int) -> bool:
    """Whether the mark at ``at`` indexes a name rather than cites: ``a[1]``, ``my_list[2]``.

    Only a one-letter name or one with an underscore counts. A word or a number glued to
    a mark is how reports cite (``caching[3]``, ``35%[2]``), and that stays a citation.
    """
    glued = _GLUED_NAME_RE.search(text, max(0, at - 64), at)
    if glued is None:
        return False
    name = glued.group()
    return "_" in name or (len(name) == 1 and name.isalpha())


def _link(span: Span, sources: _Sources | None, nested: bool = False) -> str:
    label = _inline(span.children, None, in_link=True)
    if nested:
        # A link inside an anchor's label is its words: an anchor cannot hold another.
        return label
    scheme = _scheme(span.url)
    if scheme not in _LINK_SCHEMES:
        return label
    link = f'<a href="{escape(span.url)}" target="_blank" rel="noopener noreferrer">{label}</a>'
    if sources is None or scheme == "mailto":
        return link
    number = sources.number(span.url)
    return link if number is None else link + _cite(number)


def _text(markdown: str, sources: _Sources | None) -> str:
    return _inline(parse_inline(markdown), sources)


def _blocks(blocks: tuple[Block, ...], sources: _Sources | None, forms: bool = False) -> str:
    return "".join(_block(block, sources, forms) for block in blocks)


def _named(text: str) -> tuple[tuple[Span, ...], tuple[Span, ...]] | None:
    """The name a paragraph opens with and the text after it: ``**name**: text``, ``**A lead sentence.** text``.

    Bold alone is not a name: ``**Vitest** runs on Vite`` is one sentence, and setting its
    subject on a line of its own would break it. So a separator has to follow the bold or
    close it, the bold has to end as a sentence does, or it has to be the whole paragraph.
    """
    spans = parse_inline(text)
    if not spans or spans[0].kind != "strong" or not spans[0].children:
        return None
    name, rest = list(spans[0].children), list(spans[1:])
    closed = name[-1].kind == "text" and _NAME_STOP_RE.search(name[-1].text) is not None
    if name[-1].kind == "text" and (end := _NAME_END_RE.search(name[-1].text)):
        closed = True
        name[-1] = replace(name[-1], text=name[-1].text[: end.start()])
    if rest and rest[0].kind == "text" and (start := _TEXT_START_RE.match(rest[0].text)):
        closed = True
        rest[0] = replace(rest[0], text=rest[0].text[start.end() :])
    if rest and rest[0].kind == "break":
        closed = True
        rest = rest[1:]
    if rest and rest[0].kind == "text":
        rest[0] = replace(rest[0], text=rest[0].text.lstrip())
    name = [span for span in name if span.kind != "text" or span.text]
    rest = [span for span in rest if span.kind != "text" or span.text]
    if not name or not (closed or not rest) or len(span_text(tuple(name)).strip()) > _NAME_CHARS:
        return None
    return tuple(name), tuple(rest)


def _named_items(block: Block) -> list[tuple[tuple[Span, ...], tuple[Span, ...], tuple[Block, ...]]] | None:
    """Each item's name, text and further blocks, when every item of the list opens with a name."""
    if len(block.items) < 2:
        return None
    out = []
    for item in block.items:
        named = _named(item[0].text) if item and item[0].kind == "paragraph" else None
        if named is None or not (named[1] or len(item) > 1):
            return None
        out.append((*named, item[1:]))
    return out


def _named_text(rest: tuple[Span, ...], more: tuple[Block, ...], sources: _Sources | None) -> str:
    text = _inline(rest, sources)
    return (f"<p>{text}</p>" if text and more else text) + _blocks(more, sources, True)


def _a_name(spans: tuple[Span, ...]) -> bool:
    """Whether bold that opens a paragraph is a name, not a lead sentence: short, and not ended as a sentence.

    A report that opens paragraph after paragraph with a bold sentence is prose with
    its points marked; set as terms or numbered points, it loses its paragraphs.
    """
    text = span_text(spans).strip()
    width = _width(text)
    return 0 < width <= _NAME_WIDTH and (width <= _SHORT_NAME_WIDTH or _NAME_STOP_RE.search(text) is None)


def _a_label(spans: tuple[Span, ...], words: frozenset[str] = _LABEL_WORDS) -> bool:
    """Whether bold that opens a paragraph is one of the clause's labels (``**Risk**:``)."""
    return span_text(spans).strip().rstrip(":\uff1a").strip().lower() in words


def _term_rows(blocks: list[Block], sources: _Sources | None) -> dict[int, str]:
    """The paragraphs of a section that stand in a run of two or more, each opening with a name: a row of terms each.

    None when one such paragraph of the section stands alone. There the bold opens a
    passage of several paragraphs, and terms for some passages and not for others would
    set one section in two ways.
    """
    named = [_named(block.text) if block.kind == "paragraph" else None for block in blocks]
    named = [
        pair if pair is not None and pair[1] and _a_name(pair[0]) and not _a_label(pair[0]) else None for pair in named
    ]
    rows: dict[int, str] = {}
    at = 0
    while at < len(named):
        if named[at] is None:
            at += 1
            continue
        end = at
        while end + 1 < len(named) and named[end + 1] is not None:
            end += 1
        if end == at:
            return {}
        points = end - at + 1 >= _POINTS_FROM
        for place in range(at, end + 1):
            name, rest = named[place]  # type: ignore[misc]
            if points:
                row = f'<li><p class="step-name">{_inline(name, sources)}</p>{_inline(rest, sources)}</li>'
                opening, closing = '<ol class="steps">', "</ol>"
            else:
                row = f"<div><dt>{_inline(name, sources)}</dt><dd>{_inline(rest, sources)}</dd></div>"
                opening, closing = '<dl class="terms">', "</dl>"
            rows[place] = (opening if place == at else "") + row + (closing if place == end else "")
        at = end + 1
    return rows


def _named_list(block: Block, sources: _Sources | None) -> str:
    """A list of names and their texts: numbered points when it is numbered, terms when not. Empty for any other list."""
    items = _named_items(block)
    if items is None:
        return ""
    if not block.ordered:
        rows = "".join(
            f"<div><dt>{_inline(name, sources)}</dt><dd>{_named_text(rest, more, sources)}</dd></div>"
            for name, rest, more in items
        )
        return f'<dl class="terms">{rows}</dl>'
    rows = "".join(
        f'<li><p class="step-name">{_inline(name, sources)}</p>{_named_text(rest, more, sources)}</li>'
        for name, rest, more in items
    )
    return f'<ol class="steps" style="counter-reset:step {block.start - 1}">{rows}</ol>'


def _aside(block: Block, sources: _Sources | None) -> str:
    """A blockquote inside a section: a quotation when its last line names a source, else a note."""
    children = block.children
    if children and children[-1].kind == "paragraph":
        lines = children[-1].text.split("\n")
        last = lines[-1].strip()
        if (len(lines) > 1 or len(children) > 1) and len(last) <= _ATTRIBUTION_CHARS and _ATTRIBUTION_RE.match(last):
            quoted = children[:-1]
            if len(lines) > 1:
                quoted += (replace(children[-1], text="\n".join(lines[:-1]).rstrip("\\")),)
            return (
                f'<figure class="quote"><blockquote>{_blocks(quoted, sources)}</blockquote>'
                f"<figcaption>{_text(last, sources)}</figcaption></figure>"
            )
    named = _named(children[0].text) if children and children[0].kind == "paragraph" else None
    if named is not None and (named[1] or len(children) > 1):
        name, rest = named
        text = _inline(rest, sources)
        return (
            f'<aside class="note"><p class="note-name">{_inline(name, sources)}</p>'
            f"{f'<p>{text}</p>' if text else ''}{_blocks(children[1:], sources, True)}</aside>"
        )
    return f'<aside class="note">{_blocks(children, sources, True)}</aside>'


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _card_parts(blocks: list[Block]) -> list[tuple[int, int]]:
    """The ``###`` parts of a section the page sets as cards, each as its first block and the one past it.

    Every ``###`` part of the section has to be short, and no ``####`` may divide one: a
    section that is half cards and half subsections reads as two layouts.
    """
    heads = [at for at, block in enumerate(blocks) if block.kind == "heading"]
    if not _CARDS_FROM <= len(heads) <= _CARDS_TO or any(blocks[at].level != 3 for at in heads):
        return []
    parts = [(at, end) for at, end in zip(heads, [*heads[1:], len(blocks)])]
    for at, end in parts:
        inside = blocks[at + 1 : end]
        if not inside or any(block.kind not in ("paragraph", "list") for block in inside):
            return []
        if _width(" ".join(plain_units(tuple(inside)))) > _CARD_WIDTH:
            return []
    return parts


def _carded(blocks: list[Block], rendered: list[str]) -> str:
    """A section's rendered blocks, its ``###`` parts set as cards when they are few and short."""
    parts = _card_parts(blocks)
    if not parts:
        return "".join(rendered)
    heads = [at for at, _ in parts]
    cards = "".join(f'<article class="card">{"".join(rendered[at:end])}</article>' for at, end in parts)
    size = " is-3" if len(parts) == 3 else ""
    return "".join(rendered[: heads[0]]) + f'<div class="cards{size}">{cards}</div>'


def _staged(blocks: list[Block], rendered: list[str]) -> str | None:
    """A section's rendered blocks with its run of step subsections set as stages; None when it has no such run.

    The run is ``###`` parts in a row whose headings each open with a step's name, numbered
    on from 0 or 1 without a gap. Each stage keeps its heading whole and all that stands under it.
    """
    heads = [at for at, block in enumerate(blocks) if block.kind == "heading" and block.level <= 3]
    numbers = [step_number(span_text(parse_inline(blocks[at].text)).strip()) for at in heads]
    first = next((place for place, number in enumerate(numbers) if number in (0, 1)), None)
    if first is None:
        return None
    last = first
    while last + 1 < len(heads) and numbers[last + 1] == (numbers[last] or 0) + 1:
        last += 1
    if last - first + 1 < _STAGES_FROM:
        return None
    bounds = [*heads[first : last + 1], heads[last + 1] if last + 1 < len(heads) else len(blocks)]
    stages = "".join(f"<li>{''.join(rendered[at:end])}</li>" for at, end in zip(bounds, bounds[1:]))
    return "".join(rendered[: bounds[0]]) + f'<ol class="stages">{stages}</ol>' + "".join(rendered[bounds[-1] :])


def _block(block: Block, sources: _Sources | None, forms: bool = False) -> str:
    """One block as HTML. ``forms`` is on inside a section: there a list of names and a blockquote get their own form."""
    if forms and block.kind == "list" and (named := _named_list(block, sources)):
        return named
    if forms and block.kind == "quote":
        return _aside(block, sources)
    if block.kind == "paragraph":
        return f"<p>{_labelled(block.text, sources) if forms else _text(block.text, sources)}</p>"
    if block.kind == "heading":
        tag = "h3" if block.level <= 3 else "h4"
        return f"<{tag}>{_text(block.text, sources)}</{tag}>"
    if block.kind == "code":
        return f"<pre><code>{escape(block.text, quote=False)}</code></pre>"
    if block.kind == "quote":
        return f"<blockquote>{_blocks(block.children, sources)}</blockquote>"
    if block.kind == "list":
        tag = "ol" if block.ordered else "ul"
        start = f' start="{block.start}"' if block.ordered and block.start != 1 else ""
        items = "".join(f"<li>{_item(item, sources, forms)}</li>" for item in block.items)
        return f"<{tag}{start}>{items}</{tag}>"
    if block.kind == "table":
        return _table(block, sources)
    return "<hr>"


def _labelled(markdown: str, sources: _Sources | None) -> str:
    """A paragraph's text, the short label it opens with in bold: ``Risk: ...``."""
    hit = _LABEL_RE.match(markdown)
    if hit is None:
        return _text(markdown, sources)
    label = escape(hit["label"], quote=False)
    return f"<strong>{label}</strong>{hit['colon']}{_text(markdown[hit.end() :], sources)}"


def _item(item: tuple[Block, ...], sources: _Sources | None, forms: bool = False) -> str:
    """A list item. One paragraph alone is written without its ``<p>``, so a tight list stays tight."""
    if len(item) >= 1 and item[0].kind == "paragraph" and all(later.kind == "list" for later in item[1:]):
        return _text(item[0].text, sources) + _blocks(item[1:], sources, forms)
    return _blocks(item, sources, forms)


def _table(block: Block, sources: _Sources | None, numbers: tuple[int, ...] = (), numbered: bool = False) -> str:
    """A table. ``numbers`` makes each body row a source the citations link to; ``numbered`` adds them as a column."""

    def cells(row: tuple[str, ...], tag: str) -> str:
        out = []
        for index, cell in enumerate(row):
            align = block.aligns[index] if index < len(block.aligns) else ""
            style = f' style="text-align:{align}"' if align else ""
            out.append(f"<{tag}{style}>{_text(cell, sources)}</{tag}>")
        return "".join(out)

    head = ("<th></th>" if numbered else "") + cells(block.rows[0], "th")
    keyed = (
        not numbers
        and len(block.rows) > 1
        and len(block.rows[0]) >= 2
        and all(row and 0 < _width(span_text(parse_inline(row[0]))) <= _KEY_WIDTH for row in block.rows[1:])
    )
    kind = ' class="keyed"' if keyed else ""
    body = []
    for index, row in enumerate(block.rows[1:]):
        number = numbers[index] if index < len(numbers) else None
        anchor = f' id="source-{number}"' if number is not None else ""
        lead = f'<td class="source-number">{number}</td>' if numbered and number is not None else ""
        body.append(f"<tr{anchor}>{lead}{cells(row, 'td')}</tr>")
    return f'<div class="table-scroll"><table{kind}><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


@dataclass
class _Section:
    heading: str
    blocks: list[Block]


@dataclass
class _References:
    """The report's own source list: its section, and per block of it each entry's number and markdown.

    ``own`` says the numbers are the report's (a numbered list, ``[n]`` leaders, a
    number column); else the page numbered the entries in order. ``quiet`` are the
    report's other listings of the same addresses (a link-check table): shown as
    written, with no citation beside an address that is the entry itself.
    """

    section: int
    parts: dict[int, list[tuple[int, str]]]
    own: bool = True
    quiet: frozenset[tuple[int, int]] = frozenset()

    @property
    def entries(self) -> list[tuple[int, str]]:
        return [entry for at in sorted(self.parts) for entry in self.parts[at]]

    @property
    def listed(self) -> dict[int, str]:
        return {number: url for number, text in self.entries if (url := _first_address(text))}


@dataclass
class _Report:
    title: str
    answer: tuple[Block, ...]
    preface: list[Block]
    lead: list[Block]
    sections: list[_Section]
    references: _References | None = None


def _links(markdown: str) -> list[Span]:
    """The links of ``markdown`` to an ``http`` address, in order."""
    found: list[Span] = []

    def walk(spans: tuple[Span, ...]) -> None:
        for span in spans:
            if span.kind == "link" and _scheme(span.url) in ("http", "https"):
                found.append(span)
            walk(span.children)

    walk(parse_inline(markdown))
    return found


def _addresses(markdown: str) -> list[str]:
    return [link.url for link in _links(markdown)]


def _names_source(markdown: str) -> bool:
    """Whether an entry is about its address: it opens with its link, or writes the address out.

    A step or a finding that links a page in passing does neither.
    """
    links = _links(markdown)
    if not links:
        return False
    label = span_text(links[0].children).strip()
    return label == links[0].url or span_text(parse_inline(markdown)).lstrip().startswith(label)


def _first_address(markdown: str) -> str:
    return next(iter(_addresses(markdown)), "")


def _cited(blocks: Iterable[Block]) -> list[str]:
    """The addresses ``blocks`` link, in the order they stand."""
    found: list[str] = []
    for block in blocks:
        if block.kind in ("heading", "paragraph"):
            found.extend(_addresses(block.text))
        elif block.kind == "quote":
            found.extend(_cited(block.children))
        elif block.kind == "list":
            for item in block.items:
                found.extend(_cited(item))
        elif block.kind == "table":
            found.extend(url for row in block.rows for cell in row for url in _addresses(cell))
    return found


def _linked(blocks: Iterable[Block]) -> set[str]:
    """The addresses ``blocks`` link, each as :func:`_key` spells it."""
    return {_key(url) for url in _cited(blocks)}


def _entries(block: Block) -> list[tuple[int, str]]:
    """The entries ``block`` numbers itself: an ordered list, lines that each open with ``[n]``, a number column.

    A list item may carry more below its first paragraph (a note under a source). Its
    first paragraph is the entry; the list counts whole only when every item has one.
    """
    if block.kind == "list" and block.ordered:
        return [
            (block.start + offset, item[0].text)
            for offset, item in enumerate(block.items)
            if item and item[0].kind == "paragraph"
        ]
    if block.kind == "table":
        numbers = [_ROW_NUMBER_RE.fullmatch(row[0]) for row in block.rows[1:]]
        if not numbers or not all(numbers):
            return []
        return [(int(number.group(1)), " ".join(row[1:])) for number, row in zip(numbers, block.rows[1:]) if number]
    if block.kind == "list":
        lines = [item[0].text for item in block.items if item and item[0].kind == "paragraph"]
    elif block.kind == "paragraph":
        lines = block.text.split("\n")
    else:
        return []
    marks = [(_ENTRY_RE.match(line), line) for line in lines]
    if not marks or not all(mark for mark, _ in marks):
        return []
    return [(int(mark.group(1)), line[mark.end() :]) for mark, line in marks if mark]


def _rows(block: Block) -> list[tuple[int, str]]:
    """The entries of a list or table that numbers nothing itself, numbered in the order they stand."""
    if block.kind == "list":
        texts = [item[0].text for item in block.items if item and item[0].kind == "paragraph"]
    elif block.kind == "table":
        texts = [" ".join(row) for row in block.rows[1:]]
    else:
        return []
    return list(enumerate(texts, 1))


def _whole(block: Block) -> int:
    return len(block.items) if block.kind == "list" else len(block.rows) - 1 if block.kind == "table" else 0


def _cites_file(markdown: str) -> bool:
    """Whether ``markdown`` names a local file, in its text or as a link's target."""
    return _LOCAL_PATH_RE.search(span_text(parse_inline(markdown))) is not None or (
        _LOCAL_PATH_RE.search(markdown) is not None
    )


def _lists_sources(block: Block, entries: list[tuple[int, str]], files: bool = False) -> bool:
    """Whether ``entries`` are all of ``block`` and nearly each carries an address, or with ``files`` a local path."""
    whole = _whole(block) or len(entries)
    addressed = sum(1 for _, text in entries if _first_address(text) or (files and _cites_file(text)))
    return len(entries) >= 2 and len(entries) == whole and addressed >= whole * _share(files)


def _share(headed: bool) -> float:
    return _HEADED_SHARE if headed else _REFERENCE_SHARE


def _marked(blocks: Iterable[Block]) -> set[int]:
    """The numbers the ``[n]`` marks in ``blocks`` name."""
    return {int(number) for unit in plain_units(tuple(blocks)) for number in _MARK_RE.findall(unit)}


def _source_run(text: str) -> tuple[str, list[str]] | None:
    """A paragraph that is a run of sources: its label, and the entries, each opening with its link.

    ``**Primary**: [A](a) (2026) | [B](b) (v5)`` is one. A single entry needs the label's
    colon: a sentence that opens with a link reads the same otherwise.
    """
    opens = _RUN_OPEN_RE.search(text)
    if opens is None:
        return None
    label = text[: opens.start()].strip()
    if len(span_text(parse_inline(label))) > _RUN_LABEL_CHARS:
        return None
    entries = [part.strip() for part in _RUN_SPLIT_RE.split(text[opens.start() :]) if part.strip()]
    if not entries or not all(_names_source(entry) for entry in entries):
        return None
    if len(entries) == 1 and not label.rstrip("*_ ").endswith((":", "\uff1a")):
        return None
    return label, entries


def _source_lines(text: str) -> list[str] | None:
    """A paragraph that lists one source per line, each line carrying its address or a file's path; else None.

    ``Publisher, Title, https://...`` on each line, with no list marker, is how a model
    sometimes writes the list under the sources heading.
    """
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) < 2 or not all((_addresses(line) and _names_source(line)) or _cites_file(line) for line in lines):
        return None
    return lines


def _listing(blocks: list[Block], lines: bool = False) -> tuple[dict[int, list[tuple[int, str]]], bool] | None:
    """A section's blocks as a list of sources: per block its entries, and whether the numbers are its own.

    The section holds one table, or lists and runs of sources (:func:`_source_run`),
    beside headings and paragraphs. Each entry is about an address
    (:func:`_names_source`). Several blocks, as under one label per kind of source,
    are numbered through in the order they stand. With ``lines``, which a heading that
    names the sources grants, a paragraph of one source per line counts too
    (:func:`_source_lines`), and so does an entry that cites a local file by its path;
    elsewhere such a paragraph is body text that cites, and a list of paths is files.
    """

    def names(text: str) -> bool:
        return _names_source(text) or (lines and _cites_file(text))

    if any(block.kind not in ("paragraph", "heading", "list", "table") for block in blocks):
        return None
    held = [at for at, block in enumerate(blocks) if block.kind in ("list", "table")]
    if len(held) == 1:
        block = blocks[held[0]]
        own = _entries(block)
        entries = own or _rows(block)
        named = sum(1 for _, text in entries if names(text))
        if not _lists_sources(block, entries, lines) or named < len(entries) * _share(lines):
            return None
        if own or block.kind == "table":
            return {held[0]: entries}, bool(own)
    texts: dict[int, list[str]] = {}
    for at, block in enumerate(blocks):
        if block.kind == "table":
            return None
        if block.kind == "list":
            texts[at] = [text for _, text in _rows(block)]
            if len(texts[at]) != len(block.items):
                return None
        elif block.kind == "paragraph" and (run := _source_run(block.text)) is not None:
            texts[at] = run[1]
        elif block.kind == "paragraph" and lines and (listed := _source_lines(block.text)) is not None:
            texts[at] = listed
        elif block.kind == "paragraph" and not held and _addresses(block.text):
            return None
    every = [text for at in texts for text in texts[at]]
    named = sum(1 for text in every if names(text))
    if len(every) < 2 or named < len(every) * _share(lines):
        return None
    parts: dict[int, list[tuple[int, str]]] = {}
    number = 0
    for at in sorted(texts):
        parts[at] = [(number + offset, text) for offset, text in enumerate(texts[at], 1)]
        number += len(texts[at])
    return parts, False


def _label(section: _Section) -> str:
    return span_text(parse_inline(section.heading)).strip()


def _sole_source(section: _Section) -> tuple[dict[int, list[tuple[int, str]]], bool] | None:
    """A section under the flow's own "Sources" label that lists one source.

    One entry is otherwise no list of sources (:func:`_lists_sources`): a single item
    with a link reads like any other. Under that label it is one, since the flow writes
    the section itself for a report that cites one page (:func:`close_with_sources`).
    """
    if _label(section) not in _SOURCE_LABELS or len(section.blocks) != 1 or section.blocks[0].kind != "list":
        return None
    rows = _rows(section.blocks[0])
    if len(rows) != 1 or len(section.blocks[0].items) != 1 or not _names_source(rows[0][1]):
        return None
    return {0: rows}, False


def _closing_lists(sections: list[_Section]) -> list[_References]:
    """The lists of sources the report closes with.

    A section counts when it is a list of sources (:func:`_listing`) and only such
    sections and the last one (the limits) follow it. A table in the body with a
    source column is therefore not one: the report goes on after it.
    """
    found: list[_References] = []
    for index in range(len(sections) - 1, -1, -1):
        section = sections[index]
        listing = _listing(section.blocks, lines=names_sources(_label(section))) or _sole_source(section)
        if listing is not None:
            found.append(_References(index, listing[0], listing[1]))
        elif index != len(sections) - 1:
            break
    return found[::-1]


def _weight(entries: list[tuple[int, str]]) -> int:
    """How much the entries say beside their addresses: a list of sources names them, a link check does not."""
    total = 0
    for _, text in entries:
        plain = span_text(parse_inline(text))
        for url in _addresses(text):
            plain = plain.replace(url, "")
        total += len(plain.strip())
    return total


def _numbered_on(blocks: list[Block], at: int, entries: list[tuple[int, str]]) -> dict[int, list[tuple[int, str]]]:
    """The cited list at ``at`` with the section's other lists that number on from it.

    A report that lists its sources under one label per kind numbers them through
    (1-21, then 22-23): the lists are one source list, and a ``[22]`` cites into it.
    Another list counts when each entry carries an address and no number is taken.
    """
    parts = {at: entries}
    taken = {number for number, _ in entries}
    for other, block in enumerate(blocks):
        more = _entries(block) if other != at and block.kind != "table" else []
        numbers = {number for number, _ in more}
        if not more or len(more) != (_whole(block) or len(more)) or numbers & taken:
            continue
        if all(_first_address(text) for _, text in more):
            parts[other] = more
            taken |= numbers
    return parts


def _source_tables(sections: list[_Section]) -> set[tuple[int, int]]:
    """The tables that are listings of addresses, wherever they stand.

    A link check and a second list of the same sources both look like this. They are
    shown as written, with no citation beside an address that is the entry itself.
    """
    return {
        (index, at)
        for index, section in enumerate(sections)
        for at, block in enumerate(section.blocks)
        if block.kind == "table" and _listing([block]) is not None
    }


def _references(sections: list[_Section], opening: Iterable[Block] = ()) -> _References | None:
    """The report's own source list, when it has one.

    First the last numbered list of addresses that a ``[n]`` elsewhere in the report
    names (``opening`` is what stands before the sections), in a section whose heading
    names the sources. Numbered entries that mostly carry an address are not enough for
    that: a list of steps, each with its link, reads the same, and its heading says it
    is a body section.

    Else a list of sources the report closes with (:func:`_closing_lists`) that holds
    the addresses the rest of the report links. A report can close with two of them, its
    sources and a check of the same links: the one that says most about each entry is
    the source list, and the other is shown as written.

    Without either the page numbers the report's links itself.
    """
    opening = tuple(opening)
    closing = _closing_lists(sections)
    places = {(found.section, at) for found in closing for at in found.parts}
    quiet = places | _source_tables(sections)
    for index in range(len(sections) - 1, -1, -1):
        if not names_sources(_label(sections[index])):
            continue
        for at, block in enumerate(sections[index].blocks):
            # Not a table here: a ranking numbers its rows too, and it is no list of sources.
            entries = _entries(block) if block.kind != "table" else []
            if not _lists_sources(block, entries, files=True):
                continue
            elsewhere = _marked(
                [
                    *opening,
                    *(b for i, s in enumerate(sections) for j, b in enumerate(s.blocks) if (i, j) != (index, at)),
                ]
            )
            if elsewhere & {number for number, _ in entries}:
                parts = _numbered_on(sections[index].blocks, at, entries)
                return _References(index, parts, quiet=frozenset(quiet - {(index, other) for other in parts}))
    if not closing:
        return None
    cited = _linked(
        [*opening, *(b for i, s in enumerate(sections) for j, b in enumerate(s.blocks) if (i, j) not in places)]
    )

    def covers(found: _References) -> bool:
        own = {_key(url) for _, text in found.entries for url in _addresses(text)}
        return len(cited & own) >= len(cited) * _COVERED_SHARE

    covering = [found for found in closing if covers(found)]
    if not covering:
        return None
    best = max(covering, key=lambda found: (_weight(found.entries), -found.section))
    return replace(best, quiet=frozenset(quiet - {(best.section, at) for at in best.parts}))


def _answer_lines(blocks: tuple[Block, ...]) -> tuple[Block, ...]:
    """The answer with each of its lines a paragraph: the contract writes one statement to a line."""
    out: list[Block] = []
    for block in blocks:
        if block.kind == "paragraph":
            out.extend(replace(block, text=line.rstrip("\\")) for line in block.text.split("\n") if line.strip())
        else:
            out.append(block)
    return tuple(out)


def _subheaded(block: Block) -> list[Block]:
    """A paragraph that opens with a numbered subsection's name in bold, as that ``###`` heading and its text.

    A model asked for ``### (1) ...`` subheadings writes ``**(1) Name.** Text`` about as often;
    read as the heading it stands for, the subsection gets the subsection's layout.
    """
    hit = _SUBHEAD_LEAD_RE.match(block.text) if block.kind == "paragraph" else None
    if hit is None:
        return [block]
    name = _SUBHEAD_END_RE.sub("", hit.group("name").strip())
    rest = hit.group("rest").strip()
    return [Block("heading", name, level=3)] + ([Block("paragraph", rest)] if rest else [])


def answer_blocks(report: str) -> tuple[Block, ...]:
    """The report's answer, one line to a paragraph; its first paragraph when it opens with no blockquote."""
    parts = _read(report)
    if parts.answer:
        return parts.answer
    return next(((block,) for block in [*parts.preface, *parts.lead] if block.kind == "paragraph"), ())


def _read(report: str) -> _Report:
    """The reader layout's parts: an optional title, the answer blockquote, then the ``##`` sections.

    The answer is the first blockquote above the first ``##`` heading, which is where
    the report file's own reading finds it (``report_file.answer_span``). The contract
    opens the reply with it, and a model still writes a line before it now and then.
    That line is kept as the preface, so the cover carries the answer either way.
    """
    blocks = list(parse_blocks(report))
    body = next((i for i, b in enumerate(blocks) if b.kind == "heading" and b.level >= 2), len(blocks))
    quote = next((i for i in range(body) if blocks[i].kind == "quote"), None)
    title = ""
    answer: tuple[Block, ...] = ()
    preface: list[Block] = []
    if quote is None:
        if blocks and blocks[0].kind == "heading" and blocks[0].level == 1:
            title = blocks.pop(0).text
    else:
        for block in blocks[:quote]:
            if block.kind == "heading" and not title:
                title = block.text
            else:
                preface.append(block)
        answer = _answer_lines(blocks[quote].children)
        blocks = blocks[quote + 1 :]
    lead: list[Block] = []
    sections: list[_Section] = []
    for block in blocks:
        if block.kind == "heading" and block.level <= 2:
            sections.append(_Section(block.text, []))
        elif sections:
            sections[-1].blocks.extend(_subheaded(block))
        else:
            lead.append(block)
    return _Report(title, answer, preface, lead, sections, _references(sections, [*answer, *preface, *lead]))


def _exchanged(text: str, sections: list[_Section]) -> str | None:
    """``text`` with its last two sections exchanged whole, or None when their lines cannot be told."""
    lines = text.split("\n")
    starts = section_starts(lines)
    if len(starts) < 2:
        return None
    first, last = starts[-2], starts[-1]
    moved = "\n".join(lines[first:last]).strip()
    kept = "\n".join(lines[last:]).strip()
    out = "\n".join(lines[:first]).rstrip() + f"\n\n{kept}\n\n{moved}"
    headings = [section.heading for section in sections]
    # Read back: a heading inside a list item is a line this cut would mistake for a section.
    if [section.heading for section in _read(out).sections] != [*headings[:-2], headings[-1], headings[-2]]:
        return None
    return out


_FENCE_RE = re.compile(r"^[ \t]{0,3}(```|~~~)")
_LEVEL_TWO_RE = re.compile(r"^##[ \t]+(.+?)[ \t#]*$")


def fixed_closing(report: str, language: str = "en", limits: bool = True) -> tuple[str, dict[str, int]]:
    """``report`` with its closing headings as the contract fixes them, and what was changed.

    The list of sources that closes the report goes under the "Sources" label of
    ``language``, whatever the model called it; without ``limits`` a section under the
    fixed limits heading is left out, the report's rule having asked for none.
    """
    lines = report.split("\n")
    heads: list[int] = []
    fenced = False
    for index, line in enumerate(lines):
        if _FENCE_RE.match(line):
            fenced = not fenced
        elif not fenced and _LEVEL_TWO_RE.match(line):
            heads.append(index)

    def label(index: int) -> str:
        return span_text(parse_inline(_LEVEL_TWO_RE.match(lines[index]).group(1))).strip()

    counts: dict[str, int] = {}
    if not limits:
        for at in reversed(heads):
            if _LIMITS_LABEL_RE.fullmatch(label(at)):
                end = next((h for h in heads if h > at), len(lines))
                del lines[at:end]
                heads = [h if h < at else h - (end - at) for h in heads if h != at]
                counts["limits_dropped"] = counts.get("limits_dropped", 0) + 1
    if heads and names_sources(label(heads[-1])) and not _LIMITS_LABEL_RE.fullmatch(label(heads[-1])):
        fixed = f"## {t_in(language, 'Sources')}"
        if lines[heads[-1]].strip() != fixed:
            lines[heads[-1]] = fixed
            counts["sources_heading"] = 1
    if not counts:
        return report, counts
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip(), counts


def close_with_sources(report: str, language: str = "en") -> tuple[str, dict[str, int]]:
    """``report`` closing with its list of sources, and what was changed to make it so.

    The contract closes a report with its limits and then its sources, and the report
    file and the page hold this text. Two reports are brought to that close here, both
    without a word of the report rewritten:

    * One that lists its sources in the section before its last has the two sections
      exchanged whole. Numbered headings stay where they are: the report's order is
      then the task's, or the exchange would leave the numbers out of order.
    * One with no source list of its own gets the list the page would number: each
      address it links, once, in the order it first links them, under the "Sources"
      label of ``language``.

    A report that links nothing is returned as it is.
    """
    text = report.replace("\r\n", "\n").replace("\r", "\n").rstrip()
    parts = _read(text)
    sections, references = parts.sections, parts.references
    if references is not None:
        if references.section != len(sections) - 2 or references.quiet:
            return report, {}
        if any(_NUMBERED_HEADING_RE.match(_label(section)) for section in sections[-2:]):
            return report, {}
        exchanged = _exchanged(text, sections)
        return (report, {}) if exchanged is None else (exchanged, {"sources_moved": 1})
    if sections and _label(sections[-1]) in _SOURCE_LABELS:
        return report, {}
    cited = _cited([*parts.answer, *parts.preface, *parts.lead])
    for section in sections:
        cited.extend(_addresses(section.heading))
        cited.extend(_cited(section.blocks))
    urls: dict[str, str] = {}
    for url in cited:
        urls.setdefault(_key(url), url)
    if not urls:
        return report, {}
    rows = []
    for url in urls.values():
        address = url if any(char in url for char in "<> ") else f"<{url}>"
        host = _host(url)
        rows.append(f"- {host}: {address}" if host else f"- {address}")
    listed = "\n".join(rows)
    return f"{text}\n\n## {t_in(language, 'Sources')}\n\n{listed}", {"sources_listed": len(urls)}


def _source_list(block: Block, entries: list[tuple[int, str]], own: bool) -> str:
    """One block of the report's own source list, each entry the target of its citations.

    A table stays a table. A run of sources in a paragraph becomes a list under its label.
    A list entry that carries more below its first paragraph keeps it: the page shows
    the entry the report wrote, not only its first line.
    """
    if block.kind == "table":
        return _table(block, None, tuple(number for number, _ in entries), numbered=not own)
    label = ""
    if block.kind == "paragraph" and (run := _source_run(block.text)) is not None and run[0]:
        label = f"<p>{_text(run[0], None)}</p>"
    if block.kind == "list" and len(entries) == len(block.items):

        def cell(item: tuple[Block, ...]) -> str:
            body = _item(item, None)
            return f"<span>{body}</span>" if len(item) == 1 else f'<div class="source-text">{body}</div>'

        rows = "".join(
            f'<li id="source-{number}"><span class="source-number">{number}</span>{cell(item)}</li>'
            for (number, _), item in zip(entries, block.items)
        )
    else:
        rows = "".join(
            f'<li id="source-{number}"><span class="source-number">{number}</span><span>{_text(text, None)}</span></li>'
            for number, text in entries
        )
    return f'{label}<ol class="sources">{rows}</ol>'


def _layout(parts: _Report) -> tuple[set[int], int]:
    """The sections that list the report's sources, and the index of the limits (-1 when the body has one section)."""
    references = parts.references
    listings = {references.section, *(index for index, _ in references.quiet)} if references else set()
    bodies = [index for index in range(len(parts.sections)) if index not in listings]
    return listings, bodies[-1] if len(bodies) >= 2 else -1


def _note(block: Block, sources: _Sources | None) -> str | None:
    """A body paragraph that opens with a risk or note label, as a note under that label; else None."""
    if block.kind != "paragraph" or _width(span_text(parse_inline(block.text))) > _NOTE_WIDTH:
        return None
    named = _named(block.text)
    if named is not None and named[1] and _a_label(named[0], _NOTE_WORDS):
        label, text = span_text(named[0]).strip().rstrip(":\uff1a").strip(), _inline(named[1], sources)
    elif named is None and (hit := _LABEL_RE.match(block.text)) and hit["label"].lower() in _NOTE_WORDS:
        label, text = hit["label"], _text(block.text[hit.end() :].lstrip(), sources)
    else:
        return None
    return f'<aside class="note"><p class="note-name">{escape(label, quote=False)}</p><p>{text}</p></aside>'


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _minutes(report: str) -> int:
    counts = measure_report(report)
    cjk = counts["prose_cjk"] + counts["table_cjk"]
    words = counts["prose_words"] + counts["table_words"]
    return max(1, math.ceil(cjk / _CJK_PER_MINUTE + words / _WORDS_PER_MINUTE))


def _figure_size(value: str) -> str:
    """The class that sets a long key figure smaller. A tile is narrow, and a number cut across two lines misreads."""
    width = sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in value)
    return " is-wider" if width > _FIGURE_WIDER else " is-wide" if width > _FIGURE_WIDE else ""


def _highlights(highlights: Highlights, language: str) -> str:
    out: list[str] = []
    if highlights.figures:
        tiles = "".join(
            f'<div class="figure"><p class="figure-value{_figure_size(figure.value)}">'
            f"{escape(figure.value, quote=False)}</p>"
            f'<p class="figure-label">{escape(figure.label, quote=False)}</p></div>'
            for figure in highlights.figures
        )
        out.append(f'<div class="figures" aria-label="{escape(t_in(language, "Key figures"))}">{tiles}</div>')
    if highlights.findings:
        items = "".join(f"<li>{escape(finding, quote=False)}</li>" for finding in highlights.findings)
        title = escape(t_in(language, "Key findings"), quote=False)
        out.append(
            f'<section class="panel"><p class="band-title">{title}</p><ol class="findings">{items}</ol></section>'
        )
    if highlights.timeline:
        items = "".join(
            f'<li><span class="when">{escape(moment.when, quote=False)}</span>{escape(moment.what, quote=False)}</li>'
            for moment in highlights.timeline
        )
        title = escape(t_in(language, "Timeline"), quote=False)
        out.append(
            f'<section class="panel"><p class="band-title">{title}</p><ol class="timeline">{items}</ol></section>'
        )
    return "".join(out)


def render_page(
    report: str,
    *,
    title: str = "",
    day: str = "",
    language: str = "en",
    highlights: Highlights | None = None,
    show_limits: bool = True,
    limits_last: bool = True,
) -> str:
    """The reading page of ``report``, markdown in the reader layout.

    ``title`` is used when the report has no ``#`` title of its own. Without
    ``show_limits`` the section that closes the body under the fixed limits heading is
    left off the page; the report file keeps it. Without ``limits_last`` the report is not
    in the reader layout, so its last section is body like the rest, never the limits.
    ``day`` is the date the cover shows. ``language`` selects the fixed labels. ``highlights`` come from
    :mod:`~research_flow.support.report_highlights`, already checked.
    """
    parts = _read(report)
    references = parts.references
    sources = _Sources(references.listed, references.own) if references else _Sources()
    heading = span_text(parse_inline(parts.title or title)).strip()
    answer = _blocks(parts.answer, sources)
    body: list[str] = []
    if parts.lead:
        body.append(
            f'<section class="section"><div class="panel">{_blocks(tuple(parts.lead), sources, True)}</div></section>'
        )
    entries: list[tuple[str, str]] = []
    # The limits are the last section of the body; the report's lists of sources are not the body.
    quiet = references.quiet if references else frozenset()
    listings, limits_at = _layout(parts)
    limits_at = limits_at if limits_last else -1
    for index, section in enumerate(parts.sections):
        if not show_limits and index == limits_at and _LIMITS_LABEL_RE.fullmatch(_label(section)):
            continue
        anchor = f"section-{index + 1}"
        limits = " is-limits" if index == limits_at else ""
        entries.append((anchor, span_text(parse_inline(section.heading))))
        rows = {} if index in listings else _term_rows(section.blocks, sources)
        boxed = index not in listings and index != limits_at
        carded = {at for first, end in _card_parts(section.blocks) for at in range(first, end)}
        rendered = [
            rows[at]
            if at in rows
            else _source_list(block, references.parts[at], references.own)
            if references is not None and index == references.section and at in references.parts
            else note
            if boxed and at not in carded and (note := _note(block, sources)) is not None
            else _block(block, None if (index, at) in quiet else sources, index not in listings)
            for at, block in enumerate(section.blocks)
        ]
        if index in listings:
            inner = "".join(rendered)
        else:
            inner = _staged(section.blocks, rendered) or _carded(section.blocks, rendered)
        body.append(
            f'<section class="section" id="{anchor}"><h2>{_text(section.heading, sources)}</h2>'
            f'<div class="panel{limits}">{inner}</div></section>'
        )
    if sources.numbers:
        entries.append(("sources", t_in(language, "Sources")))
        rows = "".join(
            f'<li id="source-{number}"><span class="source-number">{number}</span><span>'
            f'<span class="source-host">{escape(_host(url) or url, quote=False)}</span>'
            f'<a class="source-link" href="{escape(url)}" target="_blank" rel="noopener noreferrer">'
            f"{escape(url, quote=False)}</a></span></li>"
            for url, number in sources.numbers.items()
        )
        body.append(
            f'<section class="section" id="sources"><h2>{escape(t_in(language, "Sources"), quote=False)}</h2>'
            f'<div class="panel"><ol class="sources">{rows}</ol></div></section>'
        )

    contents = ""
    if len(parts.sections) >= _CONTENTS_FROM:
        links = "".join(f'<li><a href="#{anchor}">{escape(label, quote=False)}</a></li>' for anchor, label in entries)
        label = escape(t_in(language, "Contents"))
        contents = (
            f'<nav class="contents" aria-label="{label}"><p class="contents-title">{label}</p><ol>{links}</ol></nav>'
        )

    kicker = " · ".join(part for part in (t_in(language, "Research report"), day) if part)
    meta = [t_in(language, "{minutes} min read", minutes=_minutes(report))]
    if sources.count:
        meta.append(t_in(language, "Sources: {count}", count=sources.count))
    cover = [f'<p class="kicker">{escape(kicker, quote=False)}</p>']
    if heading:
        cover.append(f"<h1>{escape(heading, quote=False)}</h1>")
    if answer:
        cover.append(f'<div class="answer">{answer}</div>')
    if parts.preface:
        cover.append(f'<div class="preface">{_blocks(tuple(parts.preface), sources)}</div>')
    cover.append('<p class="meta">' + "".join(f"<span>{escape(item, quote=False)}</span>" for item in meta) + "</p>")

    window_title = heading or _clip(" ".join(span_text(parse_inline(b.text)) for b in parts.answer), _TITLE_CHARS)
    window_title = window_title or t_in(language, "Research report")
    page_class = "page" if contents else "page is-single"
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{"zh-CN" if language == "zh" else "en"}">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">\n'
        '<meta name="referrer" content="no-referrer">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"{GENERATOR}\n"
        f"<title>{escape(window_title, quote=False)}</title>\n"
        f"<style>{STYLE}</style>\n</head>\n<body>\n"
        f'<div class="{page_class}">{contents}<main>'
        f'<header class="cover">{"".join(cover)}</header>'
        f"{_highlights(highlights, language) if highlights else ''}"
        f"{''.join(body)}"
        "</main></div>\n</body>\n</html>\n"
    )


__all__ = [
    "GENERATOR",
    "STYLE",
    "Figure",
    "Highlights",
    "Moment",
    "answer_blocks",
    "close_with_sources",
    "fixed_closing",
    "render_page",
]
