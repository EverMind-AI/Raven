"""What a page's render cache is keyed on.

A page shows its own block and the code every page runs, so the prelude belongs in
the key: a deck whose prelude changed had every page served from the old raster.
"""


def test_the_prelude_is_part_of_what_a_page_shows() -> None:
    """A page's pixels come from its own block and from the code every page runs.

    Reproduced on this branch: an `ACCENT` constant edited above page 1 moved no page
    span, so the render cache reported nothing stale and handed every pixel-based gate
    the previous PNGs -- a deck could publish against renders of code it no longer had.
    """
    from types import SimpleNamespace

    from raven_ppt.services import seen

    sources = (
        SimpleNamespace(page=1, first_line=2, last_line=3),
        SimpleNamespace(page=2, first_line=3, last_line=4),
    )
    before = "ACCENT = '#ff0000'\nfrom x import y\npage_one()\npage_two()\n"
    after = before.replace("#ff0000", "#00ff00")

    assert seen.blocks_of(before, sources) == seen.blocks_of(after, sources), "no page span moved"
    assert seen.shared_digest(before, sources) != seen.shared_digest(after, sources)
    # The lines the page spans cover are not counted twice into the shared digest.
    assert seen.shared_digest(before, sources) == seen.shared_digest(
        "ACCENT = '#ff0000'\nfrom x import y\npage_one_other()\npage_two_other()\n", sources
    )
