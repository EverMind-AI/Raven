"""Checking the deck against what was agreed, not only against the materials.

Two checks, and both exist because the alternative was asking a user to decide
something and then ignoring their answer.

Page count: the predecessor's schema route enforced it in code and the script
route enforced nothing, so a brief asking for sixteen to twenty pages was a
sentence in a prompt. It is the author's to fix and it fixes cleanly -- merge two
pages, split one, cut one -- so unlike a type-size finding it does not fight
anything else on the way.

Language: a deck written in the wrong language is not a defect of degree. The
check is deliberately coarse -- which script the characters belong to, not which
language the words are -- because that is the part that can be measured without a
model, and it catches the failure that actually happens: a Chinese-language brief
answered with an English deck, or the reverse.
"""

from __future__ import annotations

import unicodedata

from raven_ppt.contracts import DeckBrief, Finding, Severity
from raven_ppt.services.measure.geometry import deck_text

# Below this share of CJK characters a deck is not written in a CJK language, and
# above it a deck is not written in a Latin one. Wide apart on purpose: a Chinese
# deck cites English benchmark names and an English deck quotes a Chinese term,
# and neither is the failure this looks for.
_CJK_PRESENT = 0.15
_CJK_DOMINANT = 0.5

_CJK_LANGUAGES = ("中文", "chinese", "zh", "汉语", "日本", "japanese", "ja", "한국", "korean", "ko")


# What one page's worth of material is, from the two runs that bracket it.
#
# 793 characters of material at eight pages -- 99 per page -- produced 1224
# characters of finished copy: a 1.5x expansion, which is what turning a source
# sentence into a heading and a label looks like, and that was the one run of five
# with no invented figure on any page. The same 793 characters at twelve pages -- 66
# per page -- put six invented numbers on the pages, "deployment cost from 4x to 1x"
# among them. At twenty pages the finished copy was 7056 and 8413 characters: nine
# and eleven times the material, so nine tenths of what a reader saw was the model's.
#
# So the line is between 66 and 99, and 80 is where it sits: it clears the run that
# invented nothing and catches the run that invented six things. Two bracketing
# points, which is thin -- worth revisiting as more runs land, and deliberately a
# warning rather than a refusal partly for that reason.
CHARS_PER_PAGE = 80
# What a finished page actually carries, for saying the gap in the author's terms.
# Measured across four real decks rather than assumed: their content pages run 200 to
# 467 characters with a median of 250.
COPY_PER_PAGE = 250


def material_findings(stated_chars: int | None, brief: DeckBrief | None) -> list[Finding]:
    """Whether there is enough material to fill the pages that were agreed.

    A warning and never blocking: how to close the gap is the author's call, and both
    ways are legitimate -- go and get more material, or agree fewer pages. What is not
    legitimate is the third way, which is what happens by default when nobody says
    anything: the pages past what the material carries get filled by inventing, and
    nothing downstream can tell an invented number from a read one.

    Said at the point the page count is recorded, because that is the first moment
    both numbers exist and the cheapest moment to act on either.
    """
    if brief is None or not stated_chars:
        return []
    wanted = brief.pages.low * CHARS_PER_PAGE
    if stated_chars >= wanted:
        return []
    carries = max(1, stated_chars // CHARS_PER_PAGE)
    return [
        Finding(
            kind="thin_material",
            severity=Severity.WARNING,
            message=(
                f"the materials hold {stated_chars} characters and the brief agreed {brief.pages} pages, "
                f"which is {stated_chars // brief.pages.low} characters of source per page against the "
                f"~{COPY_PER_PAGE} a finished page carries. This material says enough for about {carries} "
                f"page(s). Go and get more -- web_search for what is missing, then ppt_fetch to bring a "
                f"source into the deck, and ppt_ingest again -- or agree fewer pages. Pages past what the "
                f"sources say get filled by inventing, and nothing downstream can tell those from the read ones"
            ),
            detail={"stated_chars": stated_chars, "pages_low": brief.pages.low, "carries": carries},
        )
    ]


def page_budget_findings(pages: int, brief: DeckBrief | None) -> list[Finding]:
    """Whether the deck is the length that was agreed."""
    if brief is None or brief.pages.holds(pages):
        return []
    direction = "more" if pages < brief.pages.low else "fewer"
    return [
        Finding(
            kind="page_budget",
            severity=Severity.BLOCKING,
            message=(
                f"the deck has {pages} pages and the brief agreed {brief.pages}. "
                f"It needs {direction}: merge two pages, split one, or cut the weakest -- "
                "do not answer this by shrinking what is on a page"
            ),
            detail={"pages": pages, "low": brief.pages.low, "high": brief.pages.high},
        )
    ]


def language_findings(pptx_path, brief: DeckBrief | None) -> list[Finding]:
    """Whether the deck is written in the language the audience reads."""
    if brief is None:
        return []
    wanted_cjk = any(marker in brief.language.casefold() for marker in _CJK_LANGUAGES)
    letters, cjk = _script_census(deck_text(pptx_path))
    if letters < 200:
        # Too little text to judge; a deck this sparse has other problems and this
        # check would be guessing.
        return []
    share = cjk / letters
    if wanted_cjk and share < _CJK_PRESENT:
        return [_wrong_language(brief, share, "almost no Chinese, Japanese or Korean text")]
    if not wanted_cjk and share > _CJK_DOMINANT:
        return [_wrong_language(brief, share, "mostly Chinese, Japanese or Korean text")]
    return []


def _wrong_language(brief: DeckBrief, share: float, saw: str) -> Finding:
    return Finding(
        kind="language",
        severity=Severity.BLOCKING,
        message=(
            f"the brief agreed the deck would be in {brief.language}, and the pages carry {saw}. "
            "Rewrite the copy in the agreed language; a deck the audience cannot read is not a "
            "layout problem"
        ),
        detail={"agreed": brief.language, "cjk_share": round(share, 3)},
    )


def _script_census(pairs) -> tuple[int, int]:
    """(letters, CJK letters) across every run of text on every page."""
    letters = cjk = 0
    for _page, text in pairs:
        for char in text:
            if not char.isalpha():
                continue
            letters += 1
            if "CJK" in unicodedata.name(char, "") or "HIRAGANA" in unicodedata.name(char, ""):
                cjk += 1
            elif "KATAKANA" in unicodedata.name(char, "") or "HANGUL" in unicodedata.name(char, ""):
                cjk += 1
    return letters, cjk
