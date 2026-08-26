"""What a caption written by looking at a figure is allowed to assert.

Built from a real run. A banner reading "OPEN SKILLS FOR REAL AGENTS / Curate the
ecosystem. Retrieve what matters. / 821K -> 96,401 open skills, curated" was fetched
with no source caption, inspected, and captioned as the architecture of a system
called SkillCorpus. The word appears nowhere in the 95KB of materials that deck was
built from -- only in the filename the fetch assigned -- and the page then credited
the picture to a product.
"""

from __future__ import annotations

from raven.ppt.contracts.findings import Severity
from raven.ppt.services.measure.captions import KIND, caption_findings

# What that deck was given to read: Mem0's docs and paper, and its own product
# notes. It never says SkillCorpus.
MATERIALS = (
    "Mem0 maintains simple per-User/Session/Agent isolation and persistence.\n"
    "EverOS adds group memory, execution traces and Skills that migrate across agents.\n"
)

BANNER = {
    "skillcorpus_paper-1b9bc44f2e": {
        "width_px": 1672,
        "height_px": 941,
        "source_file": "skillcorpus_paper.jpg",
        "source_url": "https://framerusercontent.com/images/I92foHZGBdZhUvyrspVhNPRurA.jpg",
        "source_label": None,
        "caption": None,
        "file": "skillcorpus_paper.jpg",
        "kind": "image",
        "visual_caption": (
            "该信息图展示了包含技能库构建（SkillCorpus）、任务检索匹配以及智能体执行验证全流程的开放技能处理系统架构图。"
        ),
    }
}


def test_a_caption_naming_a_system_the_materials_never_mention_is_reported() -> None:
    findings = caption_findings(BANNER, MATERIALS)

    assert [f.kind for f in findings] == [KIND]
    finding = findings[0]
    assert finding.severity is Severity.WARNING
    assert finding.page is None, "a catalogue entry belongs to no page"
    assert "SkillCorpus" in finding.message
    assert finding.detail["unsupported"] == ("SkillCorpus",)


def test_the_figures_own_filename_is_not_evidence_for_what_its_caption_claims() -> None:
    """The fetch named the file `skillcorpus_paper.jpg`, so the word is on disk and in
    the figure id. Counting either as text the deck states is how the caption passed:
    a filename is not something a page can cite."""
    findings = caption_findings(BANNER, MATERIALS)

    assert findings, "the name is in the filename and the id, and in nothing the deck says"


def test_a_name_the_materials_do_state_is_left_alone() -> None:
    materials = MATERIALS + "SkillCorpus curates 96,401 open skills from 821K candidates.\n"

    assert caption_findings(BANNER, materials) == []


def test_a_name_only_the_sources_own_caption_states_is_left_alone() -> None:
    """A source caption is the deck's text too, and one figure's establishes the name
    for the whole catalogue."""
    catalogue = dict(BANNER)
    catalogue["other"] = {"caption": "Figure 2. The SkillCorpus construction pipeline."}

    assert caption_findings(catalogue, MATERIALS) == []


def test_ordinary_capitalised_prose_is_not_a_name() -> None:
    """The misfire that would make this check worth switching off: a caption whose
    first word is capitalised because it starts a sentence."""
    catalogue = {"fig": {"visual_caption": "Three curves compared against a baseline, plotted left to right."}}

    assert caption_findings(catalogue, MATERIALS) == []


def test_a_generic_acronym_is_not_a_name() -> None:
    catalogue = {"fig": {"visual_caption": "A screenshot of an API console showing a JSON response."}}

    assert caption_findings(catalogue, MATERIALS) == []


def test_a_numbered_panel_is_not_a_name() -> None:
    catalogue = {"fig": {"visual_caption": "两个并排面板，右侧标注为 III。"}}

    assert caption_findings(catalogue, MATERIALS) == []


def test_two_captions_naming_different_things_are_reported() -> None:
    """The reconciliation case. Both names are in the materials, so neither is
    invented -- what is worth a reader's second is that the figure's author and its
    inspector do not agree on what the picture is of."""
    catalogue = {
        "fig": {
            "caption": "Figure 2. Mem0 extraction pipeline.",
            "visual_caption": "the EverOS console, with a group-memory panel open",
        }
    }

    findings = caption_findings(catalogue, MATERIALS)

    assert [f.kind for f in findings] == [KIND]
    assert findings[0].severity is Severity.WARNING
    assert "Mem0" in findings[0].message and "EverOS" in findings[0].message
    assert findings[0].detail["source_names"] == ("Mem0",)
    assert findings[0].detail["inspected_names"] == ("EverOS",)


def test_two_captions_naming_the_same_thing_pass() -> None:
    catalogue = {
        "fig": {
            "caption": "Figure 2. Mem0 extraction pipeline.",
            "visual_caption": "the Mem0 pipeline drawn as three stages left to right",
        }
    }

    assert caption_findings(catalogue, MATERIALS) == []


def test_a_source_caption_that_names_nothing_is_not_a_disagreement() -> None:
    """Two captions is not itself a finding: most source captions name no system at
    all, and reporting every pair would be reporting the normal case."""
    catalogue = {
        "fig": {
            "caption": "Figure 2. Overview of the extraction pipeline.",
            "visual_caption": "three stages drawn left to right",
        }
    }

    assert caption_findings(catalogue, MATERIALS) == []


def test_a_figure_with_only_a_source_caption_is_not_this_checks_business() -> None:
    catalogue = {"fig": {"caption": "Figure 2. Mem0 extraction pipeline."}}

    assert caption_findings(catalogue, MATERIALS) == []


def test_without_materials_the_check_says_nothing() -> None:
    """No ground truth, no finding -- the house rule for every check here. Flagging
    every name in sight because nothing was ingested is the opposite of the point."""
    assert caption_findings(BANNER, "") == []
    assert caption_findings(BANNER, "   \n") == []


def test_without_a_catalogue_the_check_says_nothing() -> None:
    assert caption_findings(None, MATERIALS) == []
    assert caption_findings({}, MATERIALS) == []
