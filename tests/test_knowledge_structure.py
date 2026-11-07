"""Unit tests for heading-aware sectioning and chunking."""

from __future__ import annotations

import asyncio

from raven.knowledge._sections import MAX_SECTION_CHARS, SECTION_ORDINAL
from raven.knowledge._structure import HeadingAwareChunker, StructuredTextParser
from raven.knowledge._types import Section, TextBlock


def _parse(text: str, filename: str) -> list[Section]:
    return asyncio.run(StructuredTextParser().parse(text, filename))


def _chunk(sections: list[Section], **kwargs) -> list:
    return asyncio.run(HeadingAwareChunker(**kwargs).chunk(sections))


def _texts(items) -> list[str]:
    return [item.content.text for item in items]


MARKDOWN = """\
Intro paragraph.

# Results

Overall summary.

## Latency

The p99 was 40ms.

## Throughput

Peaked at 900 rps.

# Appendix

Raw numbers.
"""


def test_markdown_splits_on_headings():
    sections = _parse(MARKDOWN, "report.md")

    assert [section.metadata.get("heading") for section in sections] == [
        None,
        "Results",
        "Latency",
        "Throughput",
        "Appendix",
    ]
    assert "Intro paragraph." in sections[0].content.text
    assert sections[0].metadata == {}


def test_markdown_records_the_ancestor_path():
    sections = _parse(MARKDOWN, "report.md")
    latency = next(s for s in sections if s.metadata.get("heading") == "Latency")

    assert latency.metadata["heading_path"] == ["Results", "Latency"]
    assert latency.metadata["heading_level"] == 2


def test_a_sibling_heading_pops_the_deeper_level():
    """`Appendix` is an h1 arriving after two h2s; leaving them on the stack
    would file the rest of the document under a subsection it left."""
    sections = _parse(MARKDOWN, "report.md")
    appendix = next(s for s in sections if s.metadata.get("heading") == "Appendix")

    assert appendix.metadata["heading_path"] == ["Appendix"]


def test_markdown_keeps_its_heading_markers_in_the_section_text():
    sections = _parse(MARKDOWN, "report.md")
    latency = next(s for s in sections if s.metadata.get("heading") == "Latency")

    assert latency.content.text.startswith("## Latency")


def test_a_hash_inside_a_code_fence_is_not_a_heading():
    text = "# Real\n\n```bash\n# not a heading\necho hi\n```\n"
    sections = _parse(text, "snippet.md")

    assert len(sections) == 1
    assert "# not a heading" in sections[0].content.text


def test_a_hash_without_a_space_is_not_a_heading():
    sections = _parse("#hashtag stays inline\n", "note.md")

    assert len(sections) == 1
    assert sections[0].metadata == {}


def test_a_heading_holding_only_subsections_is_not_indexed_alone():
    """It would be a chunk of nothing but its own title; the children keep the
    name in their path."""
    sections = _parse("# Parent\n## Child\n\nBody.\n", "doc.md")

    assert [s.metadata.get("heading") for s in sections] == ["Child"]
    assert sections[0].metadata["heading_path"] == ["Parent", "Child"]


def test_a_document_without_headings_stays_one_section():
    sections = _parse("Just prose.\nMore prose.\n", "plain.md")

    assert len(sections) == 1
    assert sections[0].metadata == {}
    assert sections[0].source == "plain.md"


def test_an_empty_document_still_yields_one_section():
    sections = _parse("   \n", "empty.md")

    assert len(sections) == 1


HTML = """\
<html><head><title>T</title><style>h1 { color: red }</style></head>
<body>
  <h1>Findings</h1>
  <p>Overall&nbsp;good.</p>
  <h2>Errors</h2>
  <p>Three of them.</p>
  <script>console.log("noise")</script>
  <table><tr><td>a</td><td>b</td></tr></table>
</body></html>
"""


def test_html_splits_on_heading_tags():
    sections = _parse(HTML, "report.html")

    assert [s.metadata["heading"] for s in sections] == ["Findings", "Errors"]
    assert sections[1].metadata["heading_path"] == ["Findings", "Errors"]


def test_html_tags_and_scripts_do_not_reach_the_index():
    sections = _parse(HTML, "report.html")
    body = "\n".join(_texts(sections))

    assert "<p>" not in body
    assert "console.log" not in body
    assert "color: red" not in body
    assert "Overall good." in body


def test_html_is_detected_without_an_html_extension():
    sections = _parse("<h1>Title</h1><p>Body.</p>", "export")

    assert sections[0].metadata["heading"] == "Title"


def test_a_markdown_extension_is_never_sniffed_as_html():
    """A fenced HTML example inside a .md file must not switch parsers."""
    sections = _parse("# Doc\n\n```html\n<html><h1>x</h1></html>\n```\n", "guide.md")

    assert sections[0].metadata["heading"] == "Doc"
    assert "<html>" in sections[0].content.text


def test_chunks_never_span_two_sections():
    chunks = _chunk(_parse(MARKDOWN, "report.md"))

    assert all("Latency" not in text or "Throughput" not in text for text in _texts(chunks))
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert {chunk.total_chunks for chunk in chunks} == {len(chunks)}


def test_a_split_section_carries_its_heading_path_on_later_chunks():
    long_body = "word " * 400
    sections = _parse(f"# Top\n\n## Deep\n\n{long_body}\n", "long.md")
    chunks = _chunk(sections, chunk_size=64, overlap=8)

    assert len(chunks) > 1
    assert chunks[0].content.text.startswith("## Deep")
    assert all(chunk.content.text.startswith("Top > Deep\n\n") for chunk in chunks[1:])


def test_a_headingless_section_is_chunked_unchanged():
    sections = [Section(content=TextBlock(text="a " * 400), source="plain.txt")]
    chunks = _chunk(sections, chunk_size=64, overlap=8)

    assert len(chunks) > 1
    assert not any(chunk.content.text.startswith(" > ") for chunk in chunks)
    assert all(chunk.metadata == {} for chunk in chunks)


def test_the_prefix_is_paid_for_out_of_the_chunk_budget():
    """Prefixing after the split would push a chunk past the size the embedding
    model was configured for."""
    sections = _parse("# A\n\n## B\n\n" + "word " * 400, "long.md")
    chunks = _chunk(sections, chunk_size=64, overlap=8)

    assert all(len(chunk.content.text.encode("utf-8")) // 4 <= 64 for chunk in chunks)


def test_a_split_section_stores_a_verbatim_copy_of_itself():
    """Reassembling a section from its chunks after the fact is unsafe on
    repetitive text, so the split records the original instead."""
    body = "One clause covering the cell, the system and the harness. " * 40
    section = _parse(f"## Deep\n\n{body}\n", "long.md")[0]
    chunks = _chunk([section], chunk_size=64, overlap=8)

    stored = [c.metadata.get("section_text") for c in chunks if "section_text" in c.metadata]
    assert len(chunks) > 1
    assert stored == [section.content.text]


def test_a_section_that_fits_in_one_chunk_stores_no_copy():
    """The chunk already is the section; a copy would double it for nothing."""
    chunks = _chunk(_parse("## Small\n\nShort body.\n", "s.md"))

    assert all("section_text" not in chunk.metadata for chunk in chunks)


def test_an_oversized_section_is_not_copied():
    """It would never be inlined, so storing it only inflates the index."""
    section = _parse("## Big\n\n" + "x" * (MAX_SECTION_CHARS + 100), "big.md")[0]
    chunks = _chunk([section], chunk_size=64, overlap=8)

    assert all("section_text" not in chunk.metadata for chunk in chunks)


# ------------------------------------------------------------ section identity


def test_each_section_gets_its_own_ordinal():
    sections = _parse("# A\n\nbody a\n\n# B\n\nbody b\n", "doc.md")

    assert [s.metadata[SECTION_ORDINAL] for s in sections] == [0, 1]


def test_two_same_named_siblings_are_told_apart():
    """The failure no guard on the reading side could close: one parent with two
    same-named children gives both the same full heading path, and a reader keying
    on the path hands a hit in the second the text of the first. The ordinal is
    what separates them, and it can only be recorded here."""
    text = "## Usage\n\nlead\n\n### Example\n\nfirst body\n\n### Example\n\nsecond body\n"
    sections = _parse(text, "doc.md")

    examples = [s for s in sections if s.metadata.get("heading") == "Example"]
    assert len(examples) == 2
    assert examples[0].metadata["heading_path"] == examples[1].metadata["heading_path"]
    assert examples[0].metadata[SECTION_ORDINAL] != examples[1].metadata[SECTION_ORDINAL], (
        "the two sections share an identity"
    )


def test_a_document_with_no_headings_records_no_ordinal():
    """One section cannot be ambiguous, and the no-headings case is documented to
    degrade to the previous behaviour exactly -- empty metadata included."""
    sections = _parse("just prose, no headings at all\n", "doc.md")

    assert len(sections) == 1
    assert sections[0].metadata == {}


def test_every_chunk_of_a_section_carries_its_ordinal():
    """The ordinal has to reach the chunks, not only the sections: expansion reads
    it off a chunk to find the section's siblings."""
    long_body = "sentence. " * 400
    text = f"## Usage\n\nlead\n\n### Example\n\n{long_body}\n"
    chunks = _chunk(_parse(text, "doc.md"))

    example = [c for c in chunks if c.metadata.get("heading") == "Example"]
    assert len(example) > 1, "this case needs a split section"
    assert {c.metadata[SECTION_ORDINAL] for c in example} == {1}


def test_the_structured_parser_only_claims_what_it_can_cut():
    """This parser claims only the two formats it can find headings in, so a
    registry holding it alone would leave plain text, CSV, JSON, YAML and RST
    with no parser at all. TextParser has to stay alongside it for the rest."""
    from raven.knowledge.parser.text_parser import TextParser

    assert StructuredTextParser.supported_media_types == ["text/markdown", "text/html"]
    left_behind = set(TextParser.supported_media_types) - set(StructuredTextParser.supported_media_types)
    assert left_behind, "TextParser would be redundant, and the service could drop it"
    assert "text/plain" in left_behind


def test_html_that_omits_the_head_close_is_still_sectioned():
    """`</head>` is optional in HTML and `html.parser` never closes it, so the
    drop counter used to stay above zero for the rest of the file: every body came
    out empty, no sections were produced, and `parse` fell back to one whole-file
    Section carrying the raw markup -- the exact input this module exists to avoid.
    """
    html = (
        "<html>\n<head><title>Quarterly</title><style>body{color:red}</style>\n"
        "<body>\n<h1>Findings</h1><p>Revenue rose.</p>\n"
        "<h2>Risks</h2><p>Supply chain.</p>\n</body></html>"
    )
    sections = _parse(html, "q.html")

    assert [s.metadata["heading"] for s in sections] == ["Findings", "Risks"]
    body = "\n".join(s.content.text for s in sections)
    assert "<style" not in body and "<title" not in body
    assert "Revenue rose." in body and "Supply chain." in body


def test_a_script_inside_the_body_is_still_dropped():
    """The counter reset keys on `<body>`, which must not weaken the guard for
    tags that legitimately open after it."""
    html = "<html><body><h1>A</h1><script>steal()</script><p>one</p></body></html>"
    sections = _parse(html, "q.html")

    body = "\n".join(s.content.text for s in sections)
    assert "one" in body
    assert "steal()" not in body


# -- cutting where the document allows -----------------------------


def _with_elements(parts: list[str]) -> Section:
    """A section whose element spans say where its parts begin and end.

    What a parser records when it knows: the docx parser writes one span per
    paragraph, table and figure. Built by hand here so the rule can be tested
    without a document format in the way.
    """
    text = "\n\n".join(parts)
    spans = []
    at = 0
    for index, part in enumerate(parts):
        spans.append({"reading_order": index, "layout_type": "text", "char_start": at, "char_end": at + len(part)})
        at += len(part) + 2
    return Section(content=TextBlock(text=text), source="handbook.docx", metadata={"elements": spans})


def test_a_paragraph_is_never_cut_in_half():
    """A paragraph is the unit a person wrote and the unit a reader reads. Half
    of one is embedded as the half-thought it has become, and reads back as a
    sentence that stops."""
    paragraphs = [f"Paragraph {n}. " + " ".join(f"word{i}" for i in range(60)) for n in range(6)]
    section = _with_elements(paragraphs)

    chunks = _chunk([section], chunk_size=200, overlap=20)

    assert len(chunks) > 1, "the section is too big for one chunk, so this is a real test"
    for paragraph in paragraphs:
        assert sum(paragraph in chunk.content.text for chunk in chunks) == 1, paragraph[:20]


def test_chunks_are_packed_with_whole_elements():
    """Packed until the next one will not fit, so a chunk is as full as whole
    paragraphs allow rather than as full as bytes allow."""
    section = _with_elements(["First.", "Second.", "Third."])

    chunks = _chunk([section], chunk_size=200, overlap=20)

    assert _texts(chunks) == ["First.\n\nSecond.\n\nThird."]


def test_an_element_larger_than_the_budget_is_still_split():
    """One paragraph can be longer than any window. Refusing to cut it would
    mean refusing to index it."""
    long_one = " ".join(f"word{i}" for i in range(400))
    section = _with_elements([long_one, "A short one after it."])

    chunks = _chunk([section], chunk_size=100, overlap=10)

    assert len(chunks) > 1
    assert "".join(c.content.text for c in chunks).count("word399") == 1


def test_a_split_inside_a_paragraph_falls_between_words():
    """Where it has to cut, it cuts at a break rather than through a word: two
    halves of `paragraph` are not a word either half of the index knows."""
    section = _with_elements([" ".join(f"word{i}" for i in range(400))])

    chunks = _chunk([section], chunk_size=100, overlap=10)

    for chunk in chunks[:-1]:
        assert chunk.content.text.endswith(" ") or chunk.content.text[-1].isalnum()
        # The tail of every piece but the last is a whole token.
        tail = chunk.content.text.rstrip().rsplit(" ", 1)[-1]
        assert tail.startswith("word") and tail[4:].isdigit(), tail


def test_a_section_with_no_element_spans_is_cut_as_before():
    """Markdown and plain text carry no spans, and nothing about them
    changed."""
    section = Section(content=TextBlock(text="x" * 4000), source="notes.md", metadata={})

    chunks = _chunk([section], chunk_size=100, overlap=10)

    assert len(chunks) > 1


def _tokens(text: str) -> int:
    """The same estimate the chunker sizes with."""
    return len(text.encode("utf-8")) // 4


def test_the_heading_prefix_is_paid_out_of_the_chunk_budget():
    """The prefix is prepended to every piece after the first, so the split has
    to be made against what is left after it. Split at the full size instead,
    every later chunk comes back over the limit the budget exists to keep --
    which is an embedding input limit, not a preference."""
    path = ["Handbook", "Operations", "Escalation policy"]
    section = Section(
        content=TextBlock(text=" ".join(f"word{n}" for n in range(400))),
        source="handbook.docx",
        metadata={"heading_path": path},
    )

    chunks = _chunk([section], chunk_size=64, overlap=8)

    assert len(chunks) > 1, "the section has to be split for this to mean anything"
    assert max(_tokens(chunk.content.text) for chunk in chunks) <= 64


def test_an_element_too_big_on_its_own_is_split_against_the_budget_too():
    """The other branch, and the same rule: one paragraph longer than any
    window still has to come back in pieces that fit with the prefix on."""
    section = _with_elements([" ".join(f"word{n}" for n in range(400))])
    section.metadata["heading_path"] = ["Handbook", "Operations", "Escalation policy"]

    chunks = _chunk([section], chunk_size=64, overlap=8)

    assert len(chunks) > 1
    assert max(_tokens(chunk.content.text) for chunk in chunks) <= 64


def test_a_section_with_no_heading_still_cuts_at_the_full_size():
    """Nothing is prefixed to it, so nothing is taken off its budget."""
    section = Section(content=TextBlock(text=" ".join(f"word{n}" for n in range(400))), source="n.md", metadata={})

    chunks = _chunk([section], chunk_size=64, overlap=8)

    assert max(_tokens(chunk.content.text) for chunk in chunks) <= 64
    assert len(chunks) < 20, "and is not cut finer than it was asked to be"
