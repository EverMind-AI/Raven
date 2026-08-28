"""What a caption nobody's source wrote is claiming.

A figure arrives with up to two captions and they are not the same kind of claim.
`ppt_ingest` records the one the figure's own source printed under it: evidence,
which a page may quote and credit. `ppt_figure_inspect` writes a second one by
looking at the pixels: a reading, which a page may describe and must not credit.
Downstream read `caption or visual_caption` and could not tell them apart.

This reads them back apart. Two states are reported, and both are shapes rather
than judgements about wording -- deciding whether a sentence is *true* of a
picture needs a model, and a check that fired on a paraphrase would be firing on
taste:

* an inspected caption asserting a name this deck's materials never mention. The
  run that motivated the check captioned a marketing banner as the architecture of
  a system called SkillCorpus; the word appears nowhere in the 95KB of materials
  that deck was built from, and the page then credited the picture to a product;
* a pair whose two captions name different things, which is the disagreement worth
  a reader's second: the figure's author called it one thing and the inspector
  called it another.

The name a caption "asserts" is deliberately narrow -- see `_names`. Ordinary
capitalised prose is not a name, so an English caption's first word cannot fire
this, and a Chinese caption still carries a latin product name intact.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from raven.ppt.contracts.findings import Finding, Severity

KIND = "inferred_caption"

# A latin word as a caption writes one. Hyphens and apostrophes separate rather
# than join: "Mem0-style" has to be checked as "Mem0", because the deck's text is
# searched for the name and would not hold the compound the caption built from it.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")

# Below this a token carries no signal: initials, "AI", roman "II".
MIN_NAME_CHARS = 3

# Roman numerals, which a caption uses to point at a panel rather than to name
# anything: "the right panel, III" is not a claim about provenance.
_ROMAN_RE = re.compile(r"^[IVXLCDM]+$")

# Generic technical nouns that happen to be written in capitals. Deliberately an
# allowlist and not a blacklist of provenance words: this can only make the check
# quieter, never make it fire on an ordinary caption, which is the failure mode a
# list of forbidden words would have.
_GENERIC = frozenset(
    {
        "api",
        "cli",
        "cpu",
        "csv",
        "gpu",
        "html",
        "http",
        "https",
        "json",
        "llm",
        "pdf",
        "png",
        "rag",
        "ram",
        "sdk",
        "svg",
        "url",
        "xml",
    }
)


def caption_findings(
    catalogue: Mapping[str, Mapping[str, object]] | None,
    materials: str = "",
) -> list[Finding]:
    """Figures whose inspected caption claims more than the deck can support.

    Silent without materials, and that is the point rather than an omission: the
    deck's own text is the only thing a name can be checked against, and a check
    with no ground truth reports nothing instead of flagging every name it sees.
    """
    if not catalogue or not materials.strip():
        return []
    known = _known(catalogue, materials)
    findings: list[Finding] = []
    for figure_id, entry in sorted(catalogue.items()):
        inspected = _text(entry, "visual_caption")
        if not inspected:
            continue
        unsupported = tuple(name for name in _names(inspected) if name.casefold() not in known)
        if unsupported:
            findings.append(_unsupported(figure_id, inspected, unsupported))
            continue
        printed = _text(entry, "caption")
        said, seen = _names(printed), _names(inspected)
        if printed and said and seen and not (set(said) & set(seen)):
            findings.append(_disagreement(figure_id, printed, said, inspected, seen))
    return findings


def _unsupported(figure_id: str, inspected: str, unsupported: tuple[str, ...]) -> Finding:
    named = ", ".join(unsupported)
    return Finding(
        kind=KIND,
        severity=Severity.WARNING,
        message=(
            f"the caption for {figure_id} was written by looking at the picture, and it names {named}, "
            "which this deck's materials never mention. Caption it with what the picture shows, or ingest "
            "the source that establishes the name -- and do not credit the figure to it"
        ),
        detail={"figure": figure_id, "unsupported": unsupported, "inspected_caption": inspected},
    )


def _disagreement(
    figure_id: str,
    printed: str,
    said: tuple[str, ...],
    inspected: str,
    seen: tuple[str, ...],
) -> Finding:
    return Finding(
        kind=KIND,
        severity=Severity.WARNING,
        message=(
            f"{figure_id} carries two captions that name different things: its source printed "
            f"{', '.join(said)} and inspection saw {', '.join(seen)}. Caption the page from the source's "
            "words, and use the inspected one only to describe what is visible"
        ),
        detail={
            "figure": figure_id,
            "source_caption": printed,
            "source_names": said,
            "inspected_caption": inspected,
            "inspected_names": seen,
        },
    )


def _known(catalogue: Mapping[str, Mapping[str, object]], materials: str) -> str:
    """Everything this deck states in words, casefolded, as one haystack.

    The materials plus what the sources themselves printed about their figures.
    Not the figure ids, filenames or URLs: those are names the fetch assigned, and
    the run this check comes from had `skillcorpus_paper.jpg` on disk while the
    materials never said the word -- counting the filename as evidence is exactly
    how that caption passed for something the deck could support.

    A substring search rather than a set of tokens, because the two sides tokenise
    differently: materials that write a name inside a URL or in lower case would
    not yield it as a name, and the caption naming it would then be reported for
    something the deck does say. Erring towards silence is the right direction for
    a check whose whole value is that it does not misfire.
    """
    printed = [_text(entry, field) for entry in catalogue.values() for field in ("caption", "source_label")]
    return "\n".join([materials, *printed]).casefold()


def _names(text: str) -> tuple[str, ...]:
    """The coined names in a piece of text, in order, without repeats.

    A coined name is a token with a capital past its first letter, a digit among
    its letters, or nothing but capitals -- "SkillCorpus", "Mem0", "TarViS",
    "RECIPE". A word capitalised only at the front is not one, because at the head
    of a sentence that shape is indistinguishable from ordinary prose, and flagging
    "Three" for a deck whose materials say "three" would be the misfire that makes
    a check worth switching off.
    """
    found: list[str] = []
    for token in _WORD_RE.findall(text):
        if len(token) < MIN_NAME_CHARS or token in found:
            continue
        if token.casefold() in _GENERIC or _ROMAN_RE.match(token):
            continue
        coined = any(c.isupper() for c in token[1:]) or (any(c.isdigit() for c in token) and token[0].isupper())
        if coined:
            found.append(token)
    return tuple(found)


def _text(entry: Mapping[str, object], field: str) -> str:
    value = entry.get(field)
    return value.strip() if isinstance(value, str) else ""
