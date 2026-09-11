"""Whether a program's pages can be told apart, one from another.

`page_blocks` finds the `# SLIDE <n>` banners; this decides whether what it found
can be trusted. A mapping can be wrong in ways that are invisible in the result: a
page deleted without renumbering pairs one page's render with another page's code,
and a loop drawing several pages has no per-page block at all. Either one silently
attaches a measurement to the wrong page.

This module used to be four times this size. The rest of it verified a *replacement*
for one page's code -- that the prelude still defined the same names, that the copy
had not been quietly rewritten, that no page had been added -- for a stage that
returned rewritten blocks. That stage is gone (design doc D18) and nothing rewrites
a block now, so those checks had no caller left and went with it.
"""

from __future__ import annotations

import re

_CREATES_A_SLIDE = ("add_slide", "clone_page")


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
    if derived == "execution":
        return None
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
                "a render cannot be matched to the code that drew it unless each page has its own block"
            )
    return None
