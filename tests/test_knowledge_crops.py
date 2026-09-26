"""The picture of where a chunk came from, and when there is not one.

Two altitudes: the cutting itself, against chunks assembled by hand so the
geometry is the thing under test, and the whole path through a manager, which
is where the lifecycle questions live -- written on index, replaced on reindex,
gone with the document.
"""

from __future__ import annotations

import io

import pytest

from raven.knowledge import _crops
from raven.knowledge._embedding import EmbeddingConfig
from raven.knowledge._manager import KnowledgeManager, chunk_ids_for
from raven.knowledge._types import Chunk, TextBlock

DIM = 8


def _page(document, pymupdf, number: int) -> None:
    document.new_page()
    page = document[-1]
    page.insert_text(pymupdf.Point(60, 90), f"Heading on page {number}", fontsize=18)
    for index in range(6):
        page.insert_text(
            pymupdf.Point(60, 140 + index * 26), f"Page {number} line {index} of ordinary prose.", fontsize=10
        )


def _pdf(pages: int = 2) -> bytes:
    import pymupdf

    document = pymupdf.open()
    for number in range(1, pages + 1):
        _page(document, pymupdf, number)
    raw = document.tobytes()
    document.close()
    return raw


def _chunk(metadata: dict, text: str = "some text") -> Chunk:
    return Chunk(content=TextBlock(text=text), source="f.pdf", chunk_index=0, total_chunks=1, metadata=metadata)


def _box(x0=60.0, top=80.0, x1=400.0, bottom=200.0) -> dict:
    return {"x0": x0, "top": top, "x1": x1, "bottom": bottom}


def _size(image: bytes) -> "tuple[int, int]":
    from PIL import Image

    with Image.open(io.BytesIO(image)) as opened:
        assert opened.format == "WEBP"
        return opened.size


# ── which regions a chunk claims ──────────────────────────────────


def test_a_merged_chunk_is_read_from_its_spans() -> None:
    """Each piece kept its own page and box when they were merged."""
    chunk = _chunk(
        {
            "elements": [
                {"char_start": 0, "char_end": 4, "page_number": 1, "bbox": _box()},
                {"char_start": 5, "char_end": 9, "page_number": 2, "bbox": _box(top=300.0, bottom=400.0)},
            ]
        }
    )

    assert [page for page, _ in _crops.regions_of(chunk)] == [1, 2]


def test_an_unmerged_chunk_is_read_from_its_own_fields() -> None:
    """The case that is most of a document. A chunk that merged nothing has no
    spans at all -- its one page and one box are at the top level -- so reading
    only the spans would leave it with no crop."""
    chunk = _chunk({"page_number": 3, "bbox": _box()})

    assert _crops.regions_of(chunk) == [(3, (60.0, 80.0, 400.0, 200.0))]


def test_a_chunk_that_knows_where_it_is_prefers_its_spans() -> None:
    """A merged chunk keeps a top-level page as well: the page it starts on.
    Taken as the whole chunk's region it would crop the first page's box and
    silently drop everything the chunk holds after it."""
    chunk = _chunk(
        {
            "page_number": 1,
            "bbox": _box(),
            "elements": [
                {"char_start": 0, "char_end": 4, "page_number": 1, "bbox": _box()},
                {"char_start": 5, "char_end": 9, "page_number": 2, "bbox": _box()},
            ],
        }
    )

    assert len(_crops.regions_of(chunk)) == 2


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"page_number": 1},
        {"bbox": _box()},
        {"page_number": 1, "bbox": {"x0": 1.0, "x1": 2.0}},
    ],
    ids=["nothing", "page without a box", "box without a page", "half a box"],
)
def test_a_chunk_with_no_position_claims_nothing(metadata) -> None:
    """A flow format knows some of a box and not the rest -- a Word paragraph
    has a horizontal band and no vertical one, because that exists only once
    Word has laid the text out. Half a box is not a region."""
    assert _crops.regions_of(_chunk(metadata)) == []


# ── the cutting ──────────────────────────────────────────────────


def test_a_chunk_gets_an_image_of_its_own_region() -> None:
    chunks = [_chunk({"page_number": 1, "bbox": _box()})]
    ids = chunk_ids_for("d1", [c.text for c in chunks])

    cut = _crops.crops_for(_pdf(), chunks, ids)

    assert set(cut) == set(ids)
    width, height = _size(cut[ids[0]])
    # The box is 340x120 points at 1.5x, plus the margin on each side.
    assert 500 < width < 540 and 170 < height < 210


def test_two_regions_are_stacked_into_one_image() -> None:
    """RAGFlow's shape, and the reason a merged chunk is showable at all: one
    picture a chunk, however many pages it was cut from."""
    one = [_chunk({"page_number": 1, "bbox": _box()})]
    both = [
        _chunk(
            {
                "elements": [
                    {"char_start": 0, "char_end": 4, "page_number": 1, "bbox": _box()},
                    {"char_start": 5, "char_end": 9, "page_number": 2, "bbox": _box()},
                ]
            }
        )
    ]
    ids = chunk_ids_for("d1", [c.text for c in one])
    document = _pdf()

    single = _size(_crops.crops_for(document, one, ids)[ids[0]])
    stacked = _size(_crops.crops_for(document, both, ids)[ids[0]])

    assert stacked[0] == single[0], "the same width"
    assert stacked[1] > single[1] * 1.9, "and both regions' height, plus the gap between them"


def test_a_chunk_with_no_position_gets_no_image() -> None:
    """Absent rather than blank. A text file's chunks have nowhere to point,
    and an empty rectangle would say the page was blank, which is a different
    claim."""
    chunks = [_chunk({}), _chunk({"page_number": 1, "bbox": _box()}, text="other")]
    ids = chunk_ids_for("d1", [c.text for c in chunks])

    cut = _crops.crops_for(_pdf(), chunks, ids)

    assert set(cut) == {ids[1]}


def test_a_full_page_crop_is_scaled_down_to_the_cap() -> None:
    """A chunk that swallowed a whole page must not cost twenty times what a
    paragraph costs."""
    chunks = [_chunk({"page_number": 1, "bbox": _box(x0=0.0, top=0.0, x1=595.0, bottom=842.0)})]
    ids = chunk_ids_for("d1", [c.text for c in chunks])

    width, height = _size(_crops.crops_for(_pdf(), chunks, ids)[ids[0]])

    assert max(width, height) == _crops.MAX_EDGE


def test_a_region_on_a_page_that_is_not_there_is_skipped() -> None:
    """A page number out of range is a parser bug or a truncated file, and
    neither is a reason to fail the whole document's crops."""
    chunks = [_chunk({"page_number": 99, "bbox": _box()}), _chunk({"page_number": 1, "bbox": _box()}, text="b")]
    ids = chunk_ids_for("d1", [c.text for c in chunks])

    cut = _crops.crops_for(_pdf(pages=2), chunks, ids)

    assert set(cut) == {ids[1]}


def test_a_chunk_missing_one_of_its_regions_gets_none_of_them() -> None:
    """Showing part of a chunk's region with nothing saying which part is worse
    than showing none: a reader comparing the text to the picture would be
    comparing it to the wrong half."""
    chunks = [
        _chunk(
            {
                "elements": [
                    {"char_start": 0, "char_end": 4, "page_number": 1, "bbox": _box()},
                    {"char_start": 5, "char_end": 9, "page_number": 99, "bbox": _box()},
                ]
            }
        )
    ]
    ids = chunk_ids_for("d1", [c.text for c in chunks])

    assert _crops.crops_for(_pdf(), chunks, ids) == {}


# ── through the manager ──────────────────────────────────────────


class _Stub:
    model = "stub-embed"
    dimensions = DIM
    declared_dimensions = None

    async def probe_dimensions(self) -> int:
        return DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * DIM for _ in texts]


@pytest.fixture
def manager(tmp_path, monkeypatch):
    built = KnowledgeManager(
        tmp_path / "knowledge",
        embedding=EmbeddingConfig(model="stub-embed", base_url="https://x/v1", api_key="k", dimensions=DIM),
    )
    monkeypatch.setattr(built, "_client", lambda: _Stub())
    return built


async def _indexed(manager, filename: str = "report.pdf", content: bytes | None = None):
    base = await manager.create_base(name="base")
    document = manager.add_document(base.id, filename=filename, content=content if content is not None else _pdf())
    return base, await manager.index_document(document.id)


async def test_indexing_a_pdf_writes_a_crop_for_its_pieces(manager) -> None:
    _, document = await _indexed(manager)
    held, _ = await manager.document_chunks(document.id)

    assert held, "the document indexed"
    assert any(manager.crop_path(document.id, piece.chunk_id) for piece in held)


async def test_a_format_with_no_pages_writes_none(manager) -> None:
    """A Word file is rendered to PDF for the viewer, on demand. Converting
    every upload eagerly to cut thumbnails is a conversion nobody asked for."""
    _, document = await _indexed(manager, filename="notes.md", content=b"# Title\n\nSome prose about alpha.\n")
    held, _ = await manager.document_chunks(document.id)

    assert held
    assert not any(manager.crop_path(document.id, piece.chunk_id) for piece in held)


async def test_reindexing_replaces_the_crops(manager) -> None:
    """Ids are derived from chunk text, so a document that changed leaves its
    old pictures under ids nothing lists any more."""
    _, document = await _indexed(manager)
    before = {piece.chunk_id for piece in (await manager.document_chunks(document.id))[0]}

    await manager.replace_document(document.id, filename="report.pdf", content=_pdf(pages=1))
    await manager.index_document(document.id)

    after = {piece.chunk_id for piece in (await manager.document_chunks(document.id))[0]}
    assert before - after, "the shorter document cut differently"
    assert not any(manager.crop_path(document.id, gone) for gone in before - after)


async def test_deleting_the_document_takes_its_crops(manager) -> None:
    _, document = await _indexed(manager)
    held, _ = await manager.document_chunks(document.id)
    kept = [piece.chunk_id for piece in held if manager.crop_path(document.id, piece.chunk_id)]
    assert kept

    await manager.delete_document(document.id)

    assert not any(manager.crop_path(document.id, chunk_id) for chunk_id in kept)


async def test_deleting_the_base_takes_them_too(manager) -> None:
    base, document = await _indexed(manager)
    held, _ = await manager.document_chunks(document.id)
    kept = [piece.chunk_id for piece in held if manager.crop_path(document.id, piece.chunk_id)]
    assert kept

    await manager.delete_base(base.id)

    assert not any(manager.crop_path(document.id, chunk_id) for chunk_id in kept)


async def test_a_chunk_id_is_checked_before_it_becomes_a_path(manager) -> None:
    """It arrives from the page, and it is about to be a path segment."""
    _, document = await _indexed(manager)

    for hostile in ("../../records.json", "..", "a/b", "", "x" * 65):
        assert manager.crop_path(document.id, hostile) is None


async def test_a_document_whose_pages_cannot_be_read_still_indexes(manager, monkeypatch) -> None:
    """A thumbnail is worth a line in the log and nothing more."""

    def explode(*args, **kwargs):
        raise RuntimeError("no renderer here")

    monkeypatch.setattr(_crops, "crops_for", explode)

    _, document = await _indexed(manager)

    assert document.status == "ready"
    assert (await manager.document_chunks(document.id))[1] > 0
