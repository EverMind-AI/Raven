"""One ingestion run: materials in, a deck's worth of source out.

Two artefacts, and the whole of the deck's grounding rests on them:
``materials.md`` is the text a model reads and quotes, and ``figures/`` plus its
catalogue is the imagery -- extracted, never generated, so there is no surface
on which a figure can be invented.

Deterministic and offline by construction: no model is called from here, and
running it twice on the same directory produces the same two artefacts.

Reading is also cached per source file, keyed by its bytes. That matters because the
deck's source set accumulates -- a fetch adds one document and the whole set is read
again -- and reading a PDF means parsing it and cutting its figures out, seconds
each. One live run parsed the same two papers twice for want of this. A file whose
bytes are unchanged is not read again; anything else about the run is recomputed, so
the three artefacts are still a pure function of the set.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from raven.ppt.contracts.findings import Finding, Severity
from raven.ppt.contracts.sources import AssetKind, IngestOutcome, SourceAsset
from raven.ppt.services.ingest import assets as asset_meta
from raven.ppt.services.ingest import documents
from raven.ppt.services.ingest.figures import cut_page_blocks
from raven.ppt.services.ingest.pdf import read_pdf
from raven.ppt.services.ingest.sections import stated_chars

CACHE_DIR = "cache"
MATERIALS_FILE = "materials.md"
# What was read, and how much of it was the document rather than the anchors this
# pipeline writes. Two numbers a later stage needs and cannot recover from the
# markdown alone -- an image source contributes no heading to it.
READ_FILE = "read.json"
CATALOGUE_FILE = "figures.json"
# Written by whatever fetched the material, read here for attribution.
MANIFEST_FILE = "sources.jsonl"
FIGURES_DIR = "figures"


def ingest_materials(materials_dir: Path, out_dir: Path) -> IngestOutcome:
    """Ingest every supported document under ``materials_dir`` into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    figures_dir = out_dir / FIGURES_DIR
    figures_dir.mkdir(exist_ok=True)

    sources = documents.discover(materials_dir)
    if not sources:
        supported = "/".join(sorted(suffix.lstrip(".") for suffix in documents.SUPPORTED_SUFFIXES))
        raise FileNotFoundError(f"no supported materials ({supported}) under {materials_dir}")
    urls = documents.source_urls(out_dir / MANIFEST_FILE, sources)
    # A recorded caption describes the *file*, so only a standalone image takes one:
    # for a picture the file is the figure, while for a paper it would print one line
    # under all twelve of its figures. Those already carry the caption their page
    # prints beside each of them, and a caption read off the source beats a caption
    # about the download every time.
    captions = documents.source_captions(out_dir / MANIFEST_FILE, sources)

    chunks: list[str] = []
    found: list[SourceAsset] = []
    unread: list[Path] = []
    words_only: list[Path] = []
    page_count = 0
    for source in sources:
        suffix = source.suffix.lower()
        url = urls.get(source.resolve())
        if suffix in documents.PDF_SUFFIXES or suffix in documents.OFFICE_SUFFIXES:
            readable = source
            if suffix in documents.OFFICE_SUFFIXES:
                readable = _as_pdf(source, out_dir / CACHE_DIR)
                if readable is None:
                    words = documents.office_text(source)
                    if words:
                        chunks.append(f"# Source: {source.name}\n\n{words}\n")
                        words_only.append(source)
                    else:
                        unread.append(source)
                    continue
            content = _read_pdf_cached(readable, figures_dir, out_dir / CACHE_DIR)
            chunks.append(content.text)
            found.extend(_with_url(asset, url) for asset in content.assets)
            page_count += content.page_count
        elif suffix in documents.IMAGE_SUFFIXES:
            found.append(_image_asset(source, materials_dir, figures_dir, url, captions.get(source.resolve())))
        else:
            chunks.append(f"# Source: {source.name}\n\n{documents.read_text_source(source)}\n")

    kept, _dropped = asset_meta.drop_fragments(found)
    kept.extend(_page_blocks(kept, figures_dir))

    markdown = _printable("\n\n".join(chunks))
    materials_path = out_dir / MATERIALS_FILE
    materials_path.write_text(markdown, encoding="utf-8")

    catalogue_path = out_dir / CATALOGUE_FILE
    asset_meta.write_catalogue(kept, catalogue_path)
    (out_dir / READ_FILE).write_text(
        json.dumps(
            {
                "sources": [source.name for source in sources],
                "stated_chars": stated_chars(markdown),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    chars, pages = documents.text_density(markdown)
    return IngestOutcome(
        materials_path=materials_path,
        catalogue_path=catalogue_path,
        assets=tuple(kept),
        source_files=tuple(source.name for source in sources),
        page_count=page_count,
        text_chars=chars,
        findings=tuple(
            (
                *_findings(chars, pages),
                *_unread_findings(unread, materials_dir),
                *_words_only_findings(words_only),
            )
        ),
    )


@dataclass(frozen=True)
class _Read:
    """What reading one document produced, as the cache holds it."""

    text: str
    page_count: int
    assets: tuple[SourceAsset, ...]


def _read_pdf_cached(source: Path, figures_dir: Path, cache_dir: Path) -> _Read:
    """`read_pdf`, skipped when this document's bytes have been read before.

    Keyed on the bytes rather than the name or the mtime: a source that arrived twice
    under two names is one read, and a file rewritten in place is a different one. A
    hit whose figure files have gone is treated as a miss, so cleaning `figures/` is
    always safe.
    """
    digest = documents.sha256_file(source)
    record = cache_dir / f"{digest}.json"
    cached = _cached(record, figures_dir)
    if cached is not None:
        return cached
    content = read_pdf(source, figures_dir)
    read = _Read(text=content.text, page_count=content.page_count, assets=tuple(content.assets))
    _remember(record, read)
    return read


def _cached(record: Path, figures_dir: Path) -> _Read | None:
    try:
        raw = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    assets = []
    for entry in raw.get("assets") or ():
        path = figures_dir / str(entry.get("file") or "")
        if not path.is_file():
            return None
        fields = {k: v for k, v in entry.items() if k not in ("file", "kind")}
        try:
            assets.append(
                SourceAsset(
                    asset_id=str(fields.pop("asset_id")),
                    path=path,
                    kind=AssetKind(str(entry.get("kind"))),
                    concerns=tuple(fields.pop("concerns", ()) or ()),
                    **fields,
                )
            )
        except (KeyError, TypeError, ValueError):
            return None
    return _Read(text=str(raw.get("text", "")), page_count=int(raw.get("page_count", 0)), assets=tuple(assets))


def _remember(record: Path, read: _Read) -> None:
    record.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "text": read.text,
        "page_count": read.page_count,
        "assets": [
            {
                **{k: v for k, v in asdict(asset).items() if k != "path"},
                "file": asset.path.name,
                "kind": asset.kind.value,
                "concerns": list(asset.concerns),
            }
            for asset in read.assets
        ],
    }
    record.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _as_pdf(source: Path, cache_dir: Path) -> Path | None:
    """An office document as a PDF, or None when nothing here can convert it.

    Through the converter already installed for rendering, so a `.docx` yields the
    same things a `.pdf` does -- its text, its figures, and the page geometry the
    figure filters are calibrated against. Cached on the source's bytes, because
    converting is seconds and the source set is read again whenever it grows.
    """
    from raven.ppt.services.render import office

    cache_dir.mkdir(parents=True, exist_ok=True)
    converted = cache_dir / f"{documents.sha256_file(source)}.pdf"
    if converted.is_file():
        return converted
    try:
        produced = office.to_pdf(source, cache_dir)
    except Exception:  # noqa: BLE001 -- no converter, a timeout, or a file it refuses
        return None
    if produced != converted:
        produced.replace(converted)
    return converted


def _printable(text: str) -> str:
    """The text with control bytes removed, keeping tab, newline and carriage return.

    A PDF of dense mathematics came out of extraction carrying twelve NUL bytes and
    about two hundred other control characters, and they went straight into
    materials.md -- the file a model reads and a search tool treats as binary the
    moment it sees a NUL.
    """
    return "".join(char for char in text if char in "\t\n\r" or ord(char) >= 32)


def _unread_findings(unread: list[Path], materials_dir: Path) -> list[Finding]:
    """Say which sources could not be read, rather than leaving them out in silence.

    A user's document skipped without a word is how one left a deck's evidence while
    sitting in their folder.
    """
    if not unread:
        return []
    named = ", ".join(sorted(path.name for path in unread))
    return [
        Finding(
            kind="unread_source",
            severity=Severity.WARNING,
            message=(
                f"{named} could not be read, so nothing in {'them' if len(unread) > 1 else 'it'} is among this "
                "deck's evidence -- converting an office document needs LibreOffice on this machine. Say so "
                "rather than writing pages as if the document had been read"
            ),
            detail={"unread": sorted(path.name for path in unread)},
        )
    ]


def _words_only_findings(words_only: list[Path]) -> list[Finding]:
    """Say which documents were read as text alone, so their figures are not on offer.

    Not the same report as unread: every word of these is in the evidence, so the deck
    may quote them. What is missing is what only the
    converter produces -- the figures, and a table's shape.
    """
    if not words_only:
        return []
    named = ", ".join(sorted(path.name for path in words_only))
    return [
        Finding(
            kind="words_only_source",
            severity=Severity.WARNING,
            message=(
                f"{named} could not be converted on this machine, so {'they were' if len(words_only) > 1 else 'it was'} "
                "read as text alone -- the words are in the evidence, the figures are not. Do not plan a page "
                "around a figure from a document listed here"
            ),
            detail={"words_only": sorted(path.name for path in words_only)},
        )
    ]


def _with_url(asset: SourceAsset, url: str | None) -> SourceAsset:
    return asset if url is None else replace(asset, source_url=url)


def _image_asset(
    source: Path,
    materials_dir: Path,
    figures_dir: Path,
    url: str | None,
    caption: str | None = None,
) -> SourceAsset:
    """Register a standalone image file as an asset.

    Decoded before it is copied: a truncated download is not a figure, and
    discovering that after it has been registered leaves a catalogue entry
    pointing at a file nothing can open.
    """
    from PIL import Image

    with Image.open(source) as image:
        image.verify()
    with Image.open(source) as image:
        size = (int(image.width), int(image.height))
    asset_id = documents.image_asset_id(source, materials_dir)
    # Keep the source-relative path so the author can use source_file directly
    # from PPT_FIGURES_DIR without collisions between nested material folders.
    destination = figures_dir / source.relative_to(materials_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return asset_meta.build(
        asset_id,
        destination,
        # Coverage 0.0: a supplied file is not a region of any page.
        asset_meta.measure(destination, size, 0.0),
        kind=AssetKind.IMAGE,
        source_file=str(source.relative_to(materials_dir)),
        source_url=url,
        caption=caption,
    )


def _page_blocks(kept: list[SourceAsset], figures_dir: Path) -> list[SourceAsset]:
    """Cut the content blocks out of every whole-page screenshot that survived.

    A real run placed twelve page screenshots whole -- the browser chrome
    visible on every slide -- while nothing said what to do instead. The page
    keeps its entry and gains a sentence pointing at the blocks.
    """
    blocks: list[SourceAsset] = []
    for index, page in enumerate(kept):
        if not asset_meta.is_page_screenshot(page):
            continue
        cut = cut_page_blocks(page.path, figures_dir)
        if not cut:
            continue
        for figure in cut:
            blocks.append(
                asset_meta.build(
                    figure.path.stem,
                    figure.path,
                    asset_meta.measure(figure.path, figure.size, figure.coverage),
                    kind=AssetKind.FIGURE,
                    source_file=page.source_file,
                    source_page=page.source_page,
                    source_url=page.source_url,
                )
            )
        names = ", ".join(figure.path.stem for figure in cut)
        kept[index] = asset_meta.noting(
            page, f"its content blocks are already cut and listed as {names} — place those instead"
        )
    return blocks


def _findings(chars: int, pages: int) -> list[Finding]:
    if not pages or chars / pages >= documents.TEXT_LAYER_CHARS_PER_PAGE:
        return []
    return [
        Finding(
            kind="text_layer",
            severity=Severity.WARNING,
            message=(
                f"only {chars} characters of text across {pages} source pages — these documents are "
                "image-only (scanned, or a web-print export), so nothing here can read a number off them. "
                "Read the page images and register what you read as a transcript; do not write a "
                "materials file by hand and do not fill the gap from memory"
            ),
            detail={"characters": chars, "pages": pages},
        )
    ]
