"""The documentation site's pages keep the three properties a broken move costs.

A repository-relative link in a .md page is already caught by mkdocs build
--strict; this guard catches non-.md targets (e.g., docker/.env) that --strict
misses, and runs in the ordinary test suite without needing the docs uv group.
A page added in one language only leaves the other language's reader on a
fallback they cannot tell from a translation. A brand class names a rule in a
stylesheet no build step connects it to, so renaming either side errors
nowhere, reddens nothing, and is visible only to someone looking at the page.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from markdown import markdown
from markdown.extensions.toc import slugify

REPO = Path(__file__).resolve().parent.parent
SITE = REPO / "docs-site" / "docs"
STYLESHEET = SITE / "stylesheets" / "evermind.css"

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
HEADING = re.compile(r"^(#{2,6})\s+(.+?)\s*$", re.M)
ANCHOR = re.compile(r"\{\s*#([\w-]+)\s*\}")
CLASS_ATTR = re.compile(r'class="([^"]*)"')
CSS_SELECTOR = re.compile(r"\.(em-[a-z][a-z0-9-]*)")

# Published fragments are a compatibility contract, independent of current headings.
PROACTIVITY_LEGACY_SECTIONS = {
    "proactivity": {
        "proactivity-design.md#components-and-assembly": ("architecture-overview",),
        "proactivity-design.md#context-and-decision-contracts": (
            "1-data-types-sentineltypespy",
            "plannerdecision",
            "plannercontext",
            "3-context-assembly-contextassembler-sentinelpredictorcontext_assemblerpy",
            "4-decision-layer-proactiveplanner-sentinelplannerpy",
        ),
        "proactivity-design.md#tick-lifecycle": (
            "2-orchestration-sentinelrunner-sentinelexecutorrunnerpy",
            "one-tick",
            "fast-path-rules-skip-only",
            "scheduled-fire-fast-path",
            "drive-modes",
            "tickoutcome",
        ),
        "proactivity-design.md#action-routing": (
            "action",
            "degradation",
            "6-the-three-nudge-execution-paths",
            "nudgedispatcher-sentinelexecutordispatcherpy",
            "nudgeinjector-sentinelexecutorinjectorpy",
            "defermanager-sentinelexecutordefer_managerpy",
            "7-the-spawn_agent-path-proactivespawn-sentinelexecutorspawnpy",
        ),
        "proactivity-design.md#policy-boundaries": (
            "5-the-gate-nudgepolicy-sentineltrigger_policypolicypy",
            "layered-checks",
            "adaptive-multiplier",
            "readwrite-split",
            "personalization-and-persistence",
        ),
        "proactivity-design.md#state-and-feedback": (
            "8-feedback-loop-nudgefeedbacktracker-the-nudge-feedback-tool",
            "9-state-files",
        ),
        "proactivity-design.md#routines-and-task-discovery": ("12-task-discovery-anticipatory-menus",),
        "proactivity-design.md#cron-heartbeat-and-the-spine": (
            "10-cron-schedulerscron",
            "11-heartbeat-and-event-driven-wake",
            "13-spine-integration-and-the-user-inbound-gates",
            "mid-turn-user-input-busypolicyinject",
            "ask_user-pausing-a-turn-to-ask-the-user",
        ),
    },
    "proactivity-design": {
        "#design-rationale": (
            "1-three-layers-of-proactivity",
            "2-the-core-idea-periodic-planner-plus-on-demand-spawn",
            "7-scenarios",
            "l2-routine-automation",
            "l3-memory-linked-reminder",
            "l3-context-aware-resumption",
            "l3-proactive-status-check",
        ),
        "#components-and-assembly": ("3-components",),
        "#context-and-decision-contracts": (
            "proactiveplanner-periodic-reasoner",
            "contextassembler-input-packaging",
            "planner-decision-quality",
        ),
        "#action-routing": (
            "4-action-space",
            "proactivespawn-multi-step-execution-bridge",
        ),
        "#policy-boundaries": (
            "nudgepolicy-the-shared-anti-spam-gate",
            "5-anti-spam-the-nudgepolicy-gate",
            "9-risks-and-mitigations",
            "over-notification",
        ),
        "#routines-and-task-discovery": (
            "routinelearner-behavior-pattern-learning",
            "task-discovery-anticipatory-menus",
            "history-format-drift",
        ),
        "#cron-heartbeat-and-the-spine": ("6-delivery-and-turn-transport-the-spine",),
        "proactivity.md#costs-and-safety-limits": ("8-cost", "spawn-safety"),
    },
}


def _pages() -> list[Path]:
    return sorted(SITE.glob("*.md"))


def test_every_page_has_both_languages() -> None:
    english = {path.stem for path in _pages() if not path.name.endswith(".zh.md")}
    chinese = {path.name[: -len(".zh.md")] for path in _pages() if path.name.endswith(".zh.md")}

    assert english == chinese, (
        f"English page without a Chinese twin: {sorted(english - chinese)}; "
        f"Chinese page without an English twin: {sorted(chinese - english)}"
    )
    assert english, "no pages found -- did SITE move?"


def test_no_page_links_into_the_repository_by_a_relative_path() -> None:
    pages = _pages()
    assert pages, "no pages found -- did SITE move?"
    offenders: list[str] = []
    for page in pages:
        for target in LINK.findall(page.read_text(encoding="utf-8")):
            link = target.split("#", 1)[0].split(" ", 1)[0]
            if not link or link.startswith(("http://", "https://", "mailto:")):
                continue
            if "/" not in link and link.endswith(".md"):
                continue
            offenders.append(f"{page.name} -> {target}")

    assert not offenders, (
        "repository-relative links in non-.md targets are not caught by mkdocs "
        "build --strict; make them absolute github.com URLs: "
        f"{offenders}"
    )


def test_pages_do_not_link_to_external_webpages() -> None:
    offenders: list[str] = []
    for page in _pages():
        for target in LINK.findall(page.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://")):
                offenders.append(f"{page.name} -> {target}")

    assert not offenders, f"site pages must keep documentation links internal: {offenders}"


def test_brand_classes_used_by_a_page_are_the_ones_the_stylesheet_defines() -> None:
    used: set[str] = set()
    for page in _pages():
        for attr in CLASS_ATTR.findall(page.read_text(encoding="utf-8")):
            used |= {name for name in attr.split() if name.startswith("em-")}
    defined = set(CSS_SELECTOR.findall(STYLESHEET.read_text(encoding="utf-8")))

    assert used, "no em-* class found on any page -- did the brand markup move?"
    assert used == defined, (
        f"class used by a page with no rule to style it: {sorted(used - defined)}; "
        f"rule no page reaches: {sorted(defined - used)}"
    )


def _headings(text: str) -> list[tuple[int, str, str]]:
    """Every heading below the title, with the anchor it will render with."""
    found = []
    for hashes, raw in HEADING.findall(text):
        pinned = ANCHOR.search(raw)
        title = re.sub(r"<[^>]+>", "", ANCHOR.sub("", raw)).strip()
        found.append((len(hashes), title, pinned.group(1) if pinned else slugify(title, "-")))
    return found


def test_chinese_headings_pin_the_anchor_their_english_twin_gets_for_free() -> None:
    """A CJK heading slugifies to nothing, so Markdown numbers it `_1`, `_2`, ...

    Those anchors are positional: inserting one heading renumbers every anchor
    below it, silently breaking the links other pages and outside readers hold.
    An English title slugifies on its own, so the Chinese page has to say the
    same anchor out loud for the two to stay reachable by one link.
    """
    offenders: list[str] = []
    for english in sorted(page for page in _pages() if not page.name.endswith(".zh.md")):
        chinese = english.with_name(f"{english.stem}.zh.md")
        if not chinese.exists():
            continue
        left = _headings(english.read_text(encoding="utf-8"))
        right = _headings(chinese.read_text(encoding="utf-8"))
        if [depth for depth, _, _ in left] != [depth for depth, _, _ in right]:
            offenders.append(
                f"{english.stem}: heading levels differ, {[d for d, _, _ in left]} vs {[d for d, _, _ in right]}"
            )
            continue
        for (_, en_title, en_anchor), (_, zh_title, zh_anchor) in zip(left, right):
            if not zh_anchor:
                offenders.append(f"{chinese.name}: {zh_title!r} has no anchor, so Markdown will number it")
            elif zh_anchor != en_anchor:
                offenders.append(
                    f"{chinese.name}: {zh_title!r} lands on #{zh_anchor}, but {en_title!r} lands on #{en_anchor}"
                )

    assert not offenders, "\n".join(offenders)


def _render_page(name: str) -> ET.Element:
    html = markdown(
        (SITE / name).read_text(encoding="utf-8"),
        extensions=["admonition", "attr_list", "md_in_html", "tables", "toc", "fenced_code"],
    )
    return ET.fromstring(f"<article>{html}</article>")


@pytest.mark.parametrize("language", ["", ".zh"])
@pytest.mark.parametrize("page", PROACTIVITY_LEGACY_SECTIONS)
def test_proactivity_legacy_sections_reach_current_content(page: str, language: str) -> None:
    root = _render_page(f"{page}{language}.md")
    elements = list(root.iter())
    ids = [element.attrib["id"] for element in elements if "id" in element.attrib]
    assert len(ids) == len(set(ids)), f"duplicate IDs in {page}{language}"
    by_id = {element.attrib["id"]: element for element in elements if "id" in element.attrib}
    parents = {child: parent for parent in elements for child in parent}

    for target, legacy_ids in PROACTIVITY_LEGACY_SECTIONS[page].items():
        target_page, target_id = target.split("#")
        destination = _render_page(f"{Path(target_page).stem}{language}.md") if target_page else root
        assert any(element.get("id") == target_id for element in destination.iter("h2")), target

        for legacy_id in legacy_ids:
            assert legacy_id in by_id, f"{page}{language}.md lost #{legacy_id}"
            anchor = by_id[legacy_id]
            if target_page:
                container = parents[anchor]
                assert container.tag in {"li", "p"}, f"#{legacy_id} has no visible migration entry"
                assert any(
                    link.get("href") == target and "".join(link.itertext()).strip() for link in container.iter("a")
                ), f"#{legacy_id} has no migration link to {target}"
            else:
                next_heading = next(
                    element for element in elements[elements.index(anchor) + 1 :] if element.tag == "h2"
                )
                assert next_heading.get("id") == target_id, f"#{legacy_id} lands before the wrong section"


@pytest.mark.parametrize("language", ["", ".zh"])
@pytest.mark.parametrize("page", PROACTIVITY_LEGACY_SECTIONS)
def test_proactivity_preserves_published_title_anchors(page: str, language: str) -> None:
    title_id = "_1" if language else "proactivity-reference" if page == "proactivity" else page
    root = _render_page(f"{page}{language}.md")
    assert root.find(f"h1[@id='{title_id}']") is not None
