"""The report's markdown as the reading page reads it: blocks, inline spans, plain text.

The reader covers the subset a research report is written in and nothing wider. What it
cannot classify must stay text, so the page never shows fewer characters than the reply.
"""

from __future__ import annotations

import time

import pytest
from research_flow.support.report_markdown import (
    Span,
    list_steps,
    parse_blocks,
    parse_inline,
    part_numbered_lists,
    plain_units,
    span_text,
    split_chunks,
)


def _kinds(text: str) -> list[str]:
    return [block.kind for block in parse_blocks(text)]


def test_the_reader_layouts_blocks_are_read_in_order():
    text = "# Title\n\n> the answer\n> in two lines\n\n## Part\nbody\n\n---\n\n```py\nx = 1\n```\n"
    blocks = parse_blocks(text)
    assert [b.kind for b in blocks] == ["heading", "quote", "heading", "paragraph", "rule", "code"]
    assert (blocks[0].level, blocks[0].text) == (1, "Title")
    assert blocks[1].children[0].text == "the answer\nin two lines"
    assert (blocks[5].language, blocks[5].text) == ("py", "x = 1")


def test_a_list_keeps_its_items_apart_and_its_nesting():
    blocks = parse_blocks("- one\n- two\n  continued\n  - nested\n\n3. third\n4. fourth\n")
    bullets, numbered = blocks
    assert [item[0].text for item in bullets.items] == ["one", "two\ncontinued"]
    assert bullets.items[1][1].kind == "list" and bullets.items[1][1].items[0][0].text == "nested"
    assert (numbered.ordered, numbered.start) == (True, 3)
    assert [item[0].text for item in numbered.items] == ["third", "fourth"]


def test_a_blank_line_between_items_keeps_one_list():
    (only,) = parse_blocks("- one\n\n- two\n")
    assert [item[0].text for item in only.items] == ["one", "two"]


def test_a_line_that_opens_with_a_number_does_not_cut_the_paragraph_above_it():
    """A sentence wrapped before a year reads as prose; only an item numbered 1 starts a list there."""
    assert _kinds("released in\n2024. It then grew.") == ["paragraph"]
    assert _kinds("the steps:\n1. first\n2. second") == ["paragraph", "list"]


def test_a_table_carries_its_alignment_and_pads_a_short_row():
    (table,) = parse_blocks("| Tool | Start | Note |\n|:--|--:|:-:|\n| Vite | 1.2 s |\n| a \\| b | 2 | x |\n")
    assert table.aligns == ("left", "right", "center")
    assert table.rows == (("Tool", "Start", "Note"), ("Vite", "1.2 s", ""), ("a | b", "2", "x"))


def test_a_rule_under_a_line_with_a_bar_is_not_a_table():
    assert _kinds("a | b\n---\n") == ["paragraph", "rule"]


def test_an_unclosed_fence_runs_to_the_end():
    (code,) = parse_blocks("```\nno closing fence\n## not a heading")
    assert code.text == "no closing fence\n## not a heading"


def _shape(spans: tuple[Span, ...]) -> list:
    return [(s.kind, s.text or s.url or _shape(s.children)) for s in spans]


def test_emphasis_nests_and_leaves_names_with_underscores_alone():
    assert _shape(parse_inline("***both*** and *a **b** c* and snake_case_name")) == [
        ("strong", [("em", [("text", "both")])]),
        ("text", " and "),
        ("em", [("text", "a "), ("strong", [("text", "b")]), ("text", " c")]),
        ("text", " and snake_case_name"),
    ]


def test_arithmetic_and_lone_marks_stay_text():
    assert span_text(parse_inline("2 * 3 * 4 and a_b and ~not struck")) == "2 * 3 * 4 and a_b and ~not struck"
    assert [s.kind for s in parse_inline("2 * 3 * 4")] == ["text"]


@pytest.mark.parametrize(
    ("text", "url", "label"),
    [
        ("[docs](https://a.example/x)", "https://a.example/x", "docs"),
        ("[a [b]](https://a.example/p_(q))", "https://a.example/p_(q)", "a [b]"),
        ('[t](<https://a.example/a b> "title")', "https://a.example/a b", "t"),
        ("![chart](https://a.example/c.png)", "https://a.example/c.png", "chart"),
        ("<https://a.example/>", "https://a.example/", "https://a.example/"),
    ],
    ids=["plain", "nested_brackets_and_parens", "angle_target_with_title", "image", "autolink"],
)
def test_a_link_keeps_its_address_and_its_label(text, url, label):
    (link,) = parse_inline(text)
    assert (link.kind, link.url, span_text(link.children)) == ("link", url, label)


def test_a_bare_address_stops_before_the_sentences_punctuation():
    spans = parse_inline("see https://a.example/x_(y). Then (https://b.example/z), done")
    assert [s.url for s in spans if s.kind == "link"] == ["https://a.example/x_(y)", "https://b.example/z"]
    assert span_text(spans) == "see https://a.example/x_(y). Then (https://b.example/z), done"


def test_a_bracket_with_no_address_is_text():
    assert _shape(parse_inline("as in [1] and [see](")) == [("text", "as in [1] and [see](")]


def test_code_spans_and_escapes_are_literal():
    assert _shape(parse_inline("`a*b*` then \\*not em\\* and ``x ` y``")) == [
        ("code", "a*b*"),
        ("text", " then *not em* and "),
        ("code", "x ` y"),
    ]


def test_a_tag_in_the_report_is_text_not_markup():
    assert _shape(parse_inline("<script>alert(1)</script>")) == [("text", "<script>alert(1)</script>")]
    assert _kinds("<div>\nhello\n</div>") == ["paragraph"]


def test_a_hard_break_is_kept_and_a_soft_one_is_a_space():
    assert [s.kind for s in parse_inline(parse_blocks("one  \ntwo\nthree")[0].text)] == ["text", "break", "text"]
    assert span_text(parse_inline(parse_blocks("one  \ntwo\nthree")[0].text)) == "one two three"


def test_plain_units_are_what_a_reader_sees_one_per_sentence_home():
    text = (
        "## Head\n\nA **bold** [link](https://a.example).\n\n- item `one`\n  - inner\n\n| a | b |\n|--|--|\n| 1 | 2 |\n"
    )
    assert plain_units(parse_blocks(text)) == ["Head", "A bold link.", "item one inner", "a b", "1 2"]


@pytest.mark.parametrize(
    "unit",
    ["*x ", "_x ", "~~x ", "**x ", "[", "![", "[a]("],
    ids=["em", "underscore", "strike", "strong", "bracket", "image", "address"],
)
def test_a_mark_repeated_for_kilobytes_is_read_once(unit):
    """A model that degenerates repeats one mark. Each unclosed one scanned the rest of the text:
    12 KB of ``*x `` took 32 s on the host's event loop, and the time grew with the cube of the size."""
    text = unit * (60_000 // len(unit))
    started = time.perf_counter()
    spans = parse_inline(text)
    assert time.perf_counter() - started < 5
    assert span_text(spans) == text.replace("\n", " ")


def test_a_run_of_blank_lines_in_a_list_is_read_once():
    text = "- a\n" + "\n" * 60_000 + "  b\n\nafter"
    started = time.perf_counter()
    blocks = parse_blocks(text)
    assert time.perf_counter() - started < 5
    assert [block.kind for block in blocks] == ["list", "paragraph"]
    assert [item.text for item in blocks[0].items[0]] == ["a", "b"]


def test_an_emphasis_closes_only_inside_its_own_paragraph_and_after_an_unclosed_one():
    # An opening that never closes does not stop a later one of its kind from closing.
    spans = parse_inline("*a b\n\n*c* and **d** and *e ")
    assert [(span.kind, span_text((span,))) for span in spans if span.kind != "text"] == [("em", "c"), ("strong", "d")]


def test_chunks_keep_a_fenced_block_whole():
    lines = ["one", "", "```", "a", "", "b", "```", "two", "", "", "~~~", "open", "", "end"]
    assert split_chunks(lines) == [["one"], ["```", "a", "", "b", "```", "two"], ["~~~", "open", "", "end"]]


_ZH_STEPS = (
    "\u7b2c 0 \u6b65\uff08\u534a\u5929\uff09\uff1a\u91cf\u51fa CI \u4e2d\u4f4d\u6570\u3002"
    "\u7b2c 1 \u6b65\uff1a\u6ee1\u8db3 Vite \u2265 6.4.0\uff08[\u8fc1\u79fb\u9875](https://vitest.dev/guide/migration/)\uff09\u3002"
    "\u7b2c 2 \u6b65\uff1a\u65b0\u5efa `vitest.config.ts`\u3002"
)


def test_a_paragraph_that_runs_its_steps_together_is_written_as_a_list():
    """A real reply wrote nine steps of a migration as one paragraph."""
    text, lists = list_steps(
        "> a\n\n## Path\nTake it in order. Step 1: measure\nthe run. Step 2: pilot. **Step 3** go on.\n"
    )
    assert lists == 1
    assert text == (
        "> a\n\n## Path\nTake it in order.\n\n- **Step 1**: measure\n  the run.\n- **Step 2**: pilot.\n- **Step 3** go on.\n"
    )
    # The rewrite is a list to this reader, each step with its words; a second pass finds nothing to do.
    listed = parse_blocks(text)[-1]
    assert listed.kind == "list" and len(listed.items) == 3 and list_steps(text) == (text, 0)
    zh, lists = list_steps("## Path\n" + _ZH_STEPS + "\n## Next\nb\n")
    assert lists == 1 and zh.count("\n- **\u7b2c ") == 3 and "- **\u7b2c 0 \u6b65**\uff08\u534a\u5929\uff09" in zh
    assert [block.kind for block in parse_blocks(zh)] == ["heading", "list", "heading", "paragraph"]
    assert "[\u8fc1\u79fb\u9875](https://vitest.dev/guide/migration/)" in zh and "`vitest.config.ts`" in zh


@pytest.mark.parametrize(
    "text",
    [
        "Step 1 and Step 2 can run together, and Step 3 follows them.",
        "Step 1: a. Step 2: b.",
        "Step 1: a. Step 2: b. Step 4: d.",
        "Step 2: b. Step 3: c. Step 4: d.",
        "Step 1: a. See [the guide. Step 2: b. Step 3](https://vite.dev/guide/why) for c.",
        "> Step 1: a. Step 2: b. Step 3: c.",
        "- Step 1: a. Step 2: b. Step 3: c.\n\n  Step 1: a. Step 2: b. Step 3: c.",
        "```\nStep 1: a. Step 2: b. Step 3: c.\n\nStep 1: a. Step 2: b. Step 3: c.\n```",
        "| Step 1: a. Step 2: b. Step 3: c. | x |\n| --- | --- |\n| y | z |",
    ],
    ids=[
        "steps_named_in_a_sentence",
        "two_steps",
        "a_gap",
        "no_first_step",
        "a_step_inside_a_link",
        "in_a_quote",
        "in_a_list_item",
        "in_a_code_block",
        "in_a_table",
    ],
)
def test_text_that_only_mentions_steps_or_holds_them_elsewhere_is_left_as_it_is(text):
    assert list_steps(text + "\n\nafter\r\n") == (text + "\n\nafter\n", 0)


_GROUPED = (
    "## Sources\n\nDocs\n1. [A](https://a.example)\n2. [B](https://b.example)\n\n"
    "Repositories\n3. [C](https://c.example)\n\nCommunity\n4. [D](https://d.example)\n5. [E](https://e.example)\n"
)


def test_a_numbered_list_under_a_label_line_gets_the_blank_line_that_makes_it_a_list():
    """A real report grouped its sources under label lines: each group after the first read as one paragraph."""
    assert _kinds(_GROUPED) == ["heading", "paragraph", "list", "paragraph", "paragraph"]
    text, parted = part_numbered_lists(_GROUPED)
    assert parted == 2
    assert text == _GROUPED.replace("Repositories\n", "Repositories\n\n").replace("Community\n", "Community\n\n")
    assert _kinds(text) == ["heading", "paragraph", "list", "paragraph", "list", "paragraph", "list"]
    assert [block.start for block in parse_blocks(text) if block.kind == "list"] == [1, 3, 4]
    assert part_numbered_lists(text) == (text, 0)
    # With no numbered line above, two lines that count on are a list.
    assert part_numbered_lists("Do this\n3. a\n4. b\n") == ("Do this\n\n3. a\n4. b\n", 1)


@pytest.mark.parametrize(
    "text",
    [
        "The release came in\n2024. It changed the runner.",
        "Note\n3. a\n5. b",
        "## A\n\n1. a\n2. b\n\n## B\n\nlabel\n3. c",
        "> label\n3. a\n4. b",
        "- item\nlabel\n3. a\n4. b",
        "```\nlabel\n3. a\n4. b\n```",
        "| label | x |\n| --- | --- |\n3. a\n4. b",
    ],
    ids=[
        "a_wrapped_sentence",
        "a_gap",
        "another_section",
        "under_a_quote",
        "in_a_list_item",
        "in_a_code_block",
        "under_a_table",
    ],
)
def test_a_numbered_line_that_is_no_list_under_a_label_is_left_as_it_is(text):
    assert part_numbered_lists(text + "\n\nafter\r\n") == (text + "\n\nafter\n", 0)
