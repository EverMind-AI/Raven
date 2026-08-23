"""The fact gate, seeded with the hallucination shapes it was built against.

World-knowledge fill-ins, derived or rounded numbers, and fabricated specs.
Domain-generic patterns only -- no benchmark case content.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.ppt.contracts.findings import Audience, Severity
from raven.ppt.contracts.sources import SourceIndex
from raven.ppt.services.gates.facts import check_text, fact_findings, number_mentions
from raven.ppt.services.ingest.facts import build_source_index, load_source_index, write_source_index

MATERIAL = """
The new tablet is powered by the M4 chip with a 10-core CPU.
It delivers up to 4x faster AI image generation than the model with M1.
Total revenues were $30,972 million, up 14% year over year.
The vehicle length is 4230 mm and the width is 1590 mm.
LeapGNN achieves a 4.3x speedup over P3 on large graphs.
"""


def _index() -> SourceIndex:
    return build_source_index(MATERIAL)


def _tokens(text: str, **kwargs) -> set[str]:
    return {str(finding.detail["token"]) for finding in check_text(text, _index(), **kwargs)}


def test_a_page_quoting_the_materials_verbatim_passes() -> None:
    assert (
        check_text("Length: 4230 mm. Revenue reached $30,972 million, +14% YoY. Powered by the M4 chip.", _index())
        == []
    )


def test_an_unanchored_claim_blocks_and_goes_to_the_author() -> None:
    """A statement about the materials the materials do not make.

    No layout can mitigate it and no design pass may touch it, so it is the one
    class of finding that refuses the deck outright.
    """
    finding = check_text("Introducing the all-new M5 chip", _index())[0]

    assert finding.kind == "fact"
    assert finding.severity is Severity.BLOCKING
    assert finding.audience is Audience.AUTHOR
    assert finding.detail["claim"] == "entity"
    assert finding.detail["token"] == "M5"
    assert "M5" in str(finding.detail["context"])
    assert "copy the source figure verbatim" in finding.message or "outside knowledge" in finding.message


def test_a_fabricated_spec_number_is_refused() -> None:
    assert "85" in _tokens("Neural Engine delivers 85 TOPS of compute")


def test_a_rounded_number_is_refused() -> None:
    """$30,972M reprinted as $30.7B is arithmetic the deck was not asked to do.

    The gate compares the printed figure, not float closeness, so a plausible
    rounding is exactly as unanchored as an invented number.
    """
    assert any("30.7" in token for token in _tokens("Operating income was $30.7 billion"))


def test_a_perturbed_dimension_is_refused() -> None:
    assert "4279" in _tokens("Length: 4279 mm")


def test_a_derived_multiplier_is_refused() -> None:
    assert any("4.2" in token for token in _tokens("Up to 4.2x speedup over the state of the art"))


def test_thousands_separators_normalize_to_the_same_figure() -> None:
    """`30,972` in the materials and `30972` on the page are one number."""
    assert check_text("Revenue of 30972 million", _index()) == []
    assert check_text("Revenue of $30,972 million", _index()) == []


def test_a_percentage_written_out_normalizes() -> None:
    """ "14%" in the materials anchors "14 percent" on the page."""
    assert check_text("grew 14 percent year over year", _index()) == []


def test_a_trailing_zero_does_not_make_a_new_number() -> None:
    """`31.0` and `31` collide intentionally: the gate checks printed identity."""
    index = build_source_index("Margin was 31 percent.")
    assert check_text("Margin was 31.0%", index) == []


def test_a_scale_stated_once_over_a_table_anchors_its_bare_rows() -> None:
    index = build_source_index("Financial highlights (in millions)\nOperating income $ 25.0\nNet income $ 19")

    assert check_text("Operating income: $25 million", index) == []
    assert check_text("Net income: $19 million", index) == []


def test_a_markdown_table_carries_its_headings_scale() -> None:
    index = build_source_index("| Metric | Value (in millions) |\n| --- | --- |\n| Operating income | 25.0 |")

    assert check_text("Operating income: 25 million", index) == []


def test_a_scale_does_not_leak_across_a_paragraph_boundary() -> None:
    """Otherwise one heading would authorize any figure anywhere below it."""
    index = build_source_index("Financial highlights (in millions)\nRevenue 25\n\nCustomer accounts reached 800")

    assert {"800 million"} <= {str(finding.detail["token"]) for finding in check_text("800 million", index)}


def test_a_scale_stops_at_a_line_that_is_not_a_row() -> None:
    index = build_source_index(
        "Financial highlights (in millions)\nRevenue 25\nThis narrative explains the business.\nCustomer accounts 800"
    )

    assert {"800 million"} <= {str(finding.detail["token"]) for finding in check_text("800 million", index)}


def test_small_bare_integers_need_no_source() -> None:
    """List ordinals, slide numbering, "3 steps" counts."""
    assert check_text("3 key takeaways across 5 sections", _index()) == []


def test_a_bare_integer_past_the_free_bound_does_not() -> None:
    assert "21" in _tokens("21 benchmarks")


def test_the_free_bound_can_be_switched_off() -> None:
    """A page of pure ordinals is exempt by default and gateable on request."""
    assert check_text("7 key takeaways", _index()) == []
    assert "7" in _tokens("7 key takeaways", allow_free_int=False)


def test_the_whitelist_covers_deck_local_tokens() -> None:
    """The requested slide count and theme names are legitimately not in the
    materials, and the caller is the only thing that knows them."""
    assert check_text("Agenda: 26 slides", _index(), extra_allowed={"26"}) == []
    assert "26" in _tokens("Agenda: 26 slides")


def test_the_whitelist_covers_entities_case_insensitively() -> None:
    assert check_text("Built on the Helios stack, HELIOS-9", _index(), extra_allowed={"helios-9"}) == []


def test_short_plain_acronyms_are_ordinary_english() -> None:
    """Only digit-bearing spec tokens gate; CPU and AI would drown the signal."""
    assert check_text("The CPU and AI stack", _index()) == []


def test_a_longer_acronym_the_materials_never_use_still_gates() -> None:
    assert {"SOTA", "HELM"} <= _tokens("Achieves SOTA on HELM")


def test_a_grounded_multiword_label_may_be_set_in_caps() -> None:
    assert check_text("TOTAL REVENUES", _index()) == []


def test_lowercase_words_do_not_globally_authorize_a_caps_phrase() -> None:
    """The exemption is the materials' own vocabulary in the materials' own
    order, not all-caps amnesty."""
    index = build_source_index("A fast model completes each step while improving patient care.")
    tokens = {str(finding.detail["token"]) for finding in check_text("FAST STEP CARE", index)}

    assert {"FAST", "STEP", "CARE"} <= tokens


def test_an_index_without_caps_phrases_keeps_gating() -> None:
    legacy = SourceIndex(numbers={"4230"}, entities={"M4"})

    assert {str(f.detail["token"]) for f in check_text("The MODEL", legacy)} == {"MODEL"}


def test_number_mentions_keep_currency_and_scale_apart() -> None:
    """32 percent and 32 million print alike and are not the same figure."""
    mentions = number_mentions("Revenue was $30,972 million, margin was 32%, and units reached 4,230.")

    assert [(mention.text, mention.dimension) for mention in mentions] == [
        ("$30,972 million", "usd:e6"),
        ("32%", "percent"),
        ("4,230", "count"),
    ]


def test_the_deck_pass_attaches_the_page_to_every_finding() -> None:
    findings = fact_findings([(1, "Powered by the M4 chip"), (3, "Introducing the M5 chip")], _index())

    assert [(finding.page, finding.detail["token"]) for finding in findings] == [(3, "M5")]


def test_a_deck_with_no_index_behind_it_is_not_gated() -> None:
    """Refusing every number because there is nothing to check it against would
    leave the author with no move at all."""
    assert fact_findings([(1, "Introducing the M5 chip with 85 TOPS")], None) == []


def test_an_index_round_trips_through_the_file_ingest_writes(tmp_path: Path) -> None:
    index = _index()
    path = tmp_path / "fact_index.json"
    path.write_text(
        json.dumps(
            {
                "numbers": sorted(index.numbers),
                "entities": sorted(index.entities),
                "stated_numbers": sorted(index.stated_numbers or set()),
                "caps_phrases": sorted(index.caps_phrases),
            }
        ),
        encoding="utf-8",
    )

    loaded = load_source_index(path)

    assert loaded.numbers == index.numbers
    assert loaded.entities == index.entities
    assert loaded.caps_phrases == index.caps_phrases
    assert check_text("Introducing the all-new M5 chip", loaded)


def test_the_gate_reads_exactly_what_the_ingest_wrote(tmp_path: Path) -> None:
    """The two sides were separately implemented once, and drifted.

    The ingest wrote `stated_numbers` while the gate read `chart_numbers`, so the
    field that decides whether a chart value may be justified by a page number
    arrived empty -- and there were two copies of the normalisation rules on
    either side of that file. The builder and the loader are now one pair, and
    this is the round trip that says so.
    """
    index = build_source_index("The M4 chip delivers 30,972 million units and 14% growth. STATE OF THE ART.")
    path = tmp_path / "fact_index.json"
    write_source_index(index, path, sources=["paper.pdf"])
    read = load_source_index(path)

    assert read.numbers == index.numbers
    assert read.stated_numbers == index.stated_numbers and read.stated_numbers
    assert read.caps_phrases == index.caps_phrases and read.caps_phrases
    assert check_text("30,972 million units", read) == []
    derived = check_text("31 billion units", read)
    assert derived and derived[0].kind == "fact"


def test_a_single_ordinary_word_set_in_caps_is_not_a_spec_name() -> None:
    """Twenty of these blocked a real deck whose numbers were all correct.

    A deck labels a section AGENDA, TASK or VIDEO; the entity pattern reads each as
    an unknown acronym; and the phrase exemption never saw them, because a single
    word has no multi-word window to match. The author's next move was to leave the
    tool and deliver the deck itself, past every gate -- which is what a
    false-positive refusal costs.
    """
    index = build_source_index("The agenda covers video segmentation and the task inputs.")
    assert check_text("AGENDA", index) == []
    assert check_text("VIDEO SEGMENTATION", index) == []
    assert check_text("TASK", index) == []


def test_a_caps_word_the_materials_never_use_is_reported_and_not_refused() -> None:
    """A bare acronym is vocabulary until it is a name, so it warns rather than blocks.

    The cut used to be length: CPU and AI passed at three letters and SOTA was
    refused at four, which cost a live outline a round over "state-of-the-art on 5/7
    benchmarks" -- not a fact any source can be checked for. What still refuses is a
    number or an alphanumeric identifier, which is where a checkable claim lives.
    """
    index = build_source_index("The agenda covers video segmentation.")
    reported = check_text("RECIPE", index)
    assert [(f.kind, f.severity) for f in reported] == [("unfamiliar_name", Severity.WARNING)]
    assert check_text("SOTA", index)[0].severity is Severity.WARNING
    assert {f.kind for f in check_text("the A800 cluster", index)} == {"fact"}


def test_image_only_sources_stand_the_gate_down() -> None:
    """A scan yields no text, and the only way to read it is to look at the page.

    Which the author can do -- ingest puts the pages in the figure catalogue -- so
    every figure read that way would be refused as invented, and the deck would have
    no move. Ingest reports image-only sources as a `text_layer` warning, so nobody
    is told the material was read.
    """
    index = build_source_index("# Source: scan.pdf\n\n## Page 1\n")
    assert index.stated_chars == 0
    assert fact_findings([(1, "44.0 AP on VIPSeg")], index) == []


def test_prose_without_digits_keeps_the_gate_armed() -> None:
    """Having no numbers to check against is not the same as having read nothing.

    A qualitative document -- and in CJK, one with no Latin acronyms either -- leaves
    every token set empty while its text was read in full. A figure on a slide is
    invented there, and the gate that stood down on emptiness of the sets would pass
    it.
    """
    index = build_source_index("统一目标式分割把若干任务写成同一个问题，接口一致。")
    assert index.stated_numbers == frozenset() and index.entities == frozenset()
    refused = fact_findings([(1, "提速 37%")], index)
    assert [finding.detail["token"] for finding in refused] == ["37%"]


def test_an_index_written_before_stated_chars_keeps_the_gate_armed() -> None:
    """Unknown is not zero: an index from an older project must not fail open."""
    index = SourceIndex(numbers=frozenset({"41.7e6"}), stated_numbers=frozenset({"41.7e6"}))
    assert index.stated_chars is None
    assert fact_findings([(1, "Revenue 99.9M")], index)
