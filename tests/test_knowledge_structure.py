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
    from raven.knowledge._parser import TextParser

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
