"""Rewriting one page of the program without breaking the other seventeen.

The design pass returns replacement code for a single page. Applying it is not a
string substitution: the program is one file, the pages share a prelude, and a
replacement can damage the deck in ways that are invisible in the replacement
itself. Every check here is a round that was lost.

The division of labour is deliberate. Mechanical discipline -- a banner that must
be present -- is the harness's job, because the caller already knows which page
this is. Ambiguity -- a banner naming a *different* page -- is refused, because no
machine should decide whether that is a typo or another page's code in the wrong
envelope.
"""

from __future__ import annotations

import re

from raven.ppt.backends.script.blocks import page_blocks

_MARKER = re.compile(r"^\s*#.*\bslide\s*0*(\d+)\b", re.IGNORECASE)

# Lines that carry the deck's identity rather than its layout. A palette may be
# the user's choice rather than the deck's to revisit, so a design pass may not
# quietly recolour it.
_IDENTITY = re.compile(r"^\s*(?:TH\s*=|\w+\s*=\s*rgb\(|FNT\s*=|SER\s*=)")


def prelude_names(prelude: str) -> set[str]:
    """Top-level names a prelude defines: helpers, constants, imports."""
    names: set[str] = set()
    for line in prelude.splitlines():
        if line[:1].isspace() or not line.strip():
            continue
        definition = re.match(r"(?:def|class)\s+(\w+)", line)
        if definition:
            names.add(definition.group(1))
            continue
        for assignment in re.finditer(r"(?:^|,)\s*(\w+)\s*(?==[^=]|,\s*\w+\s*=)", line):
            names.add(assignment.group(1))
        imported = re.match(r"(?:from\s+\S+\s+)?import\s+(.+)", line)
        if imported:
            for piece in imported.group(1).split(","):
                names.add(piece.split(" as ")[-1].strip().split(".")[0])
    return {name for name in names if name.isidentifier()}


def prelude_rejection(replacement: str, original: str, blocks: str) -> str | None:
    """Why a rewritten prelude cannot be trusted, or None.

    A page block that loses one helper fails one page. A prelude that loses one
    fails every page at once, and it fails at import time rather than in the
    render, so the deck goes from finished to unbuildable in a single edit.
    """
    if not replacement.strip():
        return "the replacement prelude was empty"
    missing = sorted(
        name
        for name in prelude_names(original) - prelude_names(replacement)
        if re.search(rf"\b{re.escape(name)}\b", blocks)
    )
    if missing:
        return (
            f"the rewritten prelude no longer defines {', '.join(missing[:8])}, which the page blocks "
            "call -- every page would fail at once. Keep every name the pages use, whatever you change "
            "about what it produces"
        )
    before, after = _identity_lines(original), _identity_lines(replacement)
    if before and before != after:
        # Name the first line that differs: a whole-prelude rewrite that trips
        # this over one reformatted constant has no other way to find out which.
        changed = ""
        for name in sorted(set(before) | set(after)):
            if before.get(name) != after.get(name):
                changed = ((after.get(name) or before.get(name)) or [""])[0]
                break
        return (
            "the rewritten prelude changed the deck's palette or font family "
            f"(around `{changed[:80]}`). That identity may be the user's rather than the deck's, so it is "
            "not this pass's to replace -- keep those lines exactly as they were and improve the deck "
            "inside them"
        )
    return None


# Every way a page comes into existence. `add_slide` is python-pptx's; `clone_page`
# and `adapt` are the template operations, and both go through `add_slide` inside --
# which the execution mapping sees and this text scan did not. That gap cost the
# design pass every deck built in a template: with the structural pages cloned, the
# block that draws slide 1 holds an `adapt` call and no `add_slide`, so the scan read
# it as drawing no page and refused the whole pass. Measured on two live runs: 20
# build replies each, the pass refused on every one of them where it was not already
# switched off, and the reason was this list.
_CREATES_A_SLIDE = ("add_slide", "clone_page", "adapt")


def slide_creators(prelude: str) -> list[str]:
    """Names that create a slide: the built-in ones plus prelude helpers wrapping them."""
    names = list(_CREATES_A_SLIDE)
    current: str | None = None
    for line in prelude.splitlines():
        definition = re.match(r"\s*def\s+(\w+)\s*\(", line)
        if definition:
            current = definition.group(1)
        elif current and any(f"{name}(" in line for name in _CREATES_A_SLIDE):
            names.append(current)
            current = None
    return names


def blocks_rejection(
    lines: list[str], blocks: dict[int, tuple[int, int]], slide_count: int, derived: str = "comments"
) -> str | None:
    """Why these blocks cannot be trusted to be one page each, or None.

    A mapping can be wrong in ways invisible in the result: a page deleted
    without renumbering pairs one page's render with another page's code, and a
    loop drawing several pages has no per-page block at all. Both would edit the
    wrong page silently. What to do about it depends on where the mapping came
    from -- a comment the author can renumber, or execution, which is already
    exact and so means the script itself has no one block per page.
    """
    if len(blocks) != slide_count:
        return (
            f"build.py holds {len(blocks)} page block(s) but the deck has {slide_count} slide(s), so a "
            "render cannot be matched to the code that drew it. "
            + (
                "Draw each page from its own block rather than from a shared loop"
                if derived == "execution"
                else "Give each slide its own `# SLIDE <n>` block, numbered in the order they are created"
            )
        )
    if sorted(blocks) != list(range(1, slide_count + 1)):
        return (
            f"the page numbers in build.py are {sorted(blocks)}, which is not 1..{slide_count}; "
            "renumber them to match the order the slides are created"
        )
    prelude = "".join(lines[: min(span[0] for span in blocks.values())])
    creators = slide_creators(prelude)
    for number, (start, end) in sorted(blocks.items()):
        body = "".join(lines[start:end])
        # A word boundary, and a dot is not one: `prs.slides.add_slide(` is the ordinary
        # way to make a page, and excluding a leading dot to keep `ppt_template.page(`
        # from being counted made every drawn page read as drawing nothing.
        made = sum(len(re.findall(rf"(?<!\w){re.escape(name)}\s*\(", body)) for name in creators)
        if made != 1:
            where = "the block that draws slide" if derived == "execution" else "the block marked `# SLIDE"
            marker = f"{number}" if derived == "execution" else f"{number}`"
            return (
                f"{where} {marker} creates {made} slides, not one (looking for {', '.join(creators)}); "
                "a block that draws several pages cannot be polished one page at a time"
            )
    return None


def block_rejection(replacement: str, original: str) -> str | None:
    """Why a returned block cannot be used, or None when it can."""
    if "prs.save(" in replacement and "prs.save(" not in original:
        return "the block tried to save the deck; saving belongs at the end of the program"
    body = [line for line in replacement.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not body:
        return "the block came back with no code in it"
    if len(body) * 4 < len([line for line in original.splitlines() if line.strip()]):
        return f"the block came back at {len(body)} lines against the original's length; it dropped content"
    marked, returned = first_marker(original), first_marker(replacement)
    if marked is not None and returned is not None and returned != marked:
        return (
            f"the block leads with `# SLIDE {returned}` but this is slide {marked}'s block -- if this is "
            "the right page's code, fix the banner; if not, it belongs to another page"
        )
    return copy_rejection(replacement, original)


# What a piece of copy has to be before this holds it: shorter than this and it is a
# label the pass may legitimately re-letter ("01" to "1", "AP" to "mAP").
COPY_MIN_CHARS = 4


def copy_rejection(replacement: str, original: str) -> str | None:
    """Why this block's copy is not the copy it was given, or None.

    The design pass may rearrange a page and may not rewrite it -- its own brief says so
    in the first paragraph -- and one round did both anyway. Measured on a polished deck,
    page by page: 224 of 3420 characters gone across four pages, a whole bullet dropped
    from a page about query construction, a bar chart's caption and a page's conclusion
    removed, and three surviving lines quietly reworded ("训练时用匈牙利匹配指派" came
    back as "训练时匈牙利匹配指派", "只给目标内一个点" as "只给目标内一点").

    Nothing measured it, because the words are in the *program*, not in a place any check
    of the built file could compare against an earlier version. Here they can be
    compared: every string the original block set is a string the replacement has to
    still set. Merging two into one is fine -- the test is containment in the whole
    block's copy, not equality per literal -- and so is reordering; deleting a sentence
    and shaving a character out of one are not.
    """
    kept = _copy_of(replacement)
    for piece in _copy_pieces(original):
        if len(piece) < COPY_MIN_CHARS or piece in kept:
            continue
        return (
            f"the block dropped or rewrote copy the page carries: {piece[:48]!r} is not in what came back. "
            "Rearranging a page is yours; what it says is not -- move the words, do not edit them"
        )
    return None


def _copy_pieces(block: str) -> list[str]:
    """Each string literal in a block, normalised to its letters and digits.

    Punctuation and spacing are dropped because the pass reflows lines and a comma moved
    across a line break is not an edit to the copy; letters and digits going missing is.
    """
    found = []
    for single, double in re.findall(r"'([^'\n]{2,})'|\"([^\"\n]{2,})\"", block):
        piece = "".join(re.findall(r"[0-9A-Za-z\u4e00-\u9fff]+", single or double))
        if piece:
            found.append(piece)
    return found


def _copy_of(block: str) -> str:
    """All of a block's copy, run together -- what a piece has to be found inside."""
    return "".join(_copy_pieces(block))


def first_marker(block: str) -> int | None:
    """The slide number of the first `# SLIDE <n>` banner in a block, if any."""
    for line in block.splitlines():
        match = _MARKER.match(line)
        if match:
            return int(match.group(1))
    return None


def with_banner(number: int, block: str) -> str:
    """The block with its `# SLIDE n` banner guaranteed.

    The banner is load-bearing -- it is how a build failure finds the block to
    repair -- and keeping it is mechanical discipline, which is the harness's job.
    The caller knows which page this block is, so a dropped banner is put back
    rather than bounced back for a retry that costs a model call.
    """
    if first_marker(block) == number:
        return block
    return f"# SLIDE {number}\n{block}"


def applied_lines(lines: list[str], blocks: dict[int, tuple[int, int]], replacements: dict[int, str]) -> list[str]:
    """The script with the replacements in.

    Highest page first, so replacing one block cannot shift the line spans of the
    blocks still to be replaced.
    """
    new: list[str] = list(lines)
    for number in sorted(replacements, reverse=True):
        start, end = blocks[number]
        new[start:end] = [replacements[number]]
    # Re-split before returning: a replacement lands as one multi-line string, and
    # every per-line reader downstream -- the banner count above all -- would
    # otherwise see only the first line of such an element.
    return "".join(new).splitlines(keepends=True)


def apply_verified(
    lines: list[str], blocks: dict[int, tuple[int, int]], replacements: dict[int, str]
) -> tuple[dict[int, str], dict[int, str]]:
    """(replacements safe to write, replacements refused) by page number.

    Each block carries its own banner by construction, but one replacement can
    still cost the script a page: a block that draws two slides swallows its
    neighbour's identity in ways no per-block check sees, and the round that
    shipped one built eighteen slides from seventeen mapped blocks. So the write
    is verified as a whole -- the banner count must not drop -- and when it would,
    the culprits are found by leaving one block out at a time, so one bad block
    costs itself rather than the round.
    """

    def banners(selected: dict[int, str]) -> int:
        return len(page_blocks(applied_lines(lines, blocks, selected)))

    before = len(page_blocks(lines))
    kept = dict(replacements)
    dropped: dict[int, str] = {}
    reason = (
        "applying this block would leave the script with fewer `# SLIDE` banners than pages -- it likely "
        "draws more than one slide. Return one page's code under one banner"
    )
    while kept and banners(kept) < before:
        count = banners(kept)
        culprit = next(
            (number for number in sorted(kept) if banners({k: v for k, v in kept.items() if k != number}) > count),
            None,
        )
        if culprit is None:
            # No single block explains the loss; refusing them all keeps the
            # script whole, and the next round starts from something that maps.
            dropped.update(dict.fromkeys(kept, reason))
            kept = {}
            break
        dropped[culprit] = reason
        kept.pop(culprit)
    return kept, dropped


def _identity_lines(text: str) -> dict[str, list[str]]:
    """Identity assignments grouped by name, order kept within each group.

    Whole lines, not just the name assigned: `ACC = rgb(TH["accent"])` and
    `ACC = rgb("FF0000")` differ only in the part that matters. Grouped and
    ordered within the group because moving `ACC` past `MUT` is reorganisation,
    while swapping two assignments to the same name changes which one wins --
    a new palette wearing the old lines. Inner whitespace is normalised: a
    rewrite that re-aligns the block must not read as a recolour.
    """
    groups: dict[str, list[str]] = {}
    for line in text.splitlines():
        if _IDENTITY.match(line):
            groups.setdefault(line.split("=", 1)[0].strip(), []).append(re.sub(r"\s+", " ", line.strip()))
    return groups
