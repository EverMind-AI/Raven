"""Finding the source documents and reading the ones that are just text.

Everything a deck may claim has to be traceable to something here, so this
layer is deliberately dull: which files count as material, how each one becomes
text, and whether a downloaded file is still the file that was downloaded.

PDFs are the interesting case and live in :mod:`pdf`; what remains is markdown,
plain text, CSV, HTML and standalone images.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

TEXT_SUFFIXES = frozenset({".csv", ".htm", ".html", ".md", ".txt", ".markdown"})
PDF_SUFFIXES = frozenset({".pdf"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif"})

# Office documents, read by converting them to PDF first. They were absent, which
# meant a user who handed over a .docx -- the likeliest thing anyone hands over for a
# deck -- had it skipped by `discover` and never learned: their document was not in
# the deck's evidence and nothing said so.
#
# Converted rather than parsed, because the conversion is one call to something
# already installed for rendering and it yields everything the PDF path already knows
# how to take: the text, the figures, the page geometry the figure filters are
# calibrated against. Parsing the package yields the text and loses the rest, which is
# why it is the fallback below rather than the road taken.
#
# `.pptx` is deliberately not here. It is the template's suffix everywhere else in
# this package -- a fetched or attached one binds as the house style -- and a deck in
# a materials folder is genuinely ambiguous rather than obviously a source.
OFFICE_SUFFIXES = frozenset({".docx", ".doc", ".odt", ".rtf", ".xlsx", ".xls", ".ods"})

SUPPORTED_SUFFIXES = TEXT_SUFFIXES | PDF_SUFFIXES | IMAGE_SUFFIXES | OFFICE_SUFFIXES

# The zip-packaged half of the office formats, which hold their words as XML inside
# the package. That is the fallback when no converter is installed, and it is worth
# having because the alternative is not "read it later by hand": a `.docx` is
# unreachable to the author too -- `read_file` on a zip answers with a codec error --
# and every figure it states is missing from the fact index, so quoting one is
# refused. Words only: no figures, no page geometry, no table shape.
ZIP_OFFICE_SUFFIXES = frozenset({".docx", ".xlsx", ".odt", ".ods"})

# Below this many characters per source page the PDFs carry no usable text
# layer: they are scans, or a web-print export of an image-only page. The fact
# gate then has nothing to anchor a claim to, which is worth saying out loud
# rather than letting the deck be written against an empty index.
TEXT_LAYER_CHARS_PER_PAGE = 60

_PAGE_HEADING_RE = re.compile(r"^##\s", re.MULTILINE)

# Local tag names, namespace dropped: the same walk has to serve WordprocessingML
# and OpenDocument, whose namespaces differ and whose local names do not. Word keeps
# its words on `w:t`; OpenDocument keeps them on the paragraph and on inline spans.
# Named rather than "every text node in the file", for the reason the HTML extractor
# below skips `script`: a node no reader sees puts numbers in the fact index anyway,
# and that is a licence to state them on a slide. `w:instrText` -- field codes, URLs
# among them -- is exactly such a node.
_TEXT_TAGS = {"t", "p", "h", "span", "a"}
_INLINE_TAGS = {"t", "span", "a", "s", "tab", "br", "line-break"}
_CELL_TAGS = {"tc", "table-cell"}
_LINE_TAGS = {"p", "h", "tr", "table-row"}


def discover(materials_dir: Path) -> list[Path]:
    """Every supported file under ``materials_dir``, in a stable order."""
    return sorted(
        path for path in materials_dir.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def unreadable(materials_dir: Path) -> list[Path]:
    """Files here that nothing can read, so their absence can be reported.

    `discover` filtering silently is how a user's document went missing from a deck's
    evidence without anyone finding out. A caller that lists what it took should be
    able to list what it left.
    """
    return sorted(
        path
        for path in materials_dir.rglob("*")
        if path.is_file() and path.suffix.lower() not in SUPPORTED_SUFFIXES and not path.name.startswith((".", "~$"))
    )


def office_text(path: Path) -> str:
    """The words inside a zip-packaged office document, without a converter.

    Only reached when :func:`raven.ppt.services.render.office.to_pdf` could not run;
    see ``ZIP_OFFICE_SUFFIXES``. Returns "" for anything this cannot open, which the
    caller reports as unread rather than treating as an empty document.
    """
    if path.suffix.lower() not in ZIP_OFFICE_SUFFIXES:
        return ""
    try:
        with zipfile.ZipFile(path) as package:
            if path.suffix.lower() == ".xlsx":
                return _spreadsheet_text(package)
            part = "word/document.xml" if path.suffix.lower() == ".docx" else "content.xml"
            return _xml_text(package.read(part))
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return ""


def _xml_text(payload: bytes) -> str:
    """Every visible text node of one package part, with the breaks its tags imply.

    Walked in document order rather than through ``iterparse``, which ends a child
    before its parent and would report a paragraph's leading words after the inline
    run that follows them.
    """
    lines: list[str] = []
    current: list[str] = []

    def walk(element: ElementTree.Element) -> None:
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in _TEXT_TAGS and element.text:
            current.append(element.text)
        for child in element:
            walk(child)
        if tag in _CELL_TAGS:
            current.append("\t")
        if tag in _LINE_TAGS:
            line = "".join(current).strip()
            current.clear()
            if line:
                lines.append(line)
        if tag in _INLINE_TAGS and element.tail:
            current.append(element.tail)

    walk(ElementTree.fromstring(payload))
    trailing = "".join(current).strip()
    if trailing:
        lines.append(trailing)
    return "\n".join(lines)


def _spreadsheet_text(package: zipfile.ZipFile) -> str:
    """A workbook as tab-separated rows, one sheet after another.

    Written out rather than run through :func:`_xml_text` because a cell of type
    ``s`` holds an *index* into the shared string table, and dumping the raw text
    node would put that index into the fact index as a number -- a licence to state
    a figure no reader of the sheet ever saw.
    """
    shared = _shared_strings(package)
    sheets = sorted(name for name in package.namelist() if name.startswith("xl/worksheets/sheet"))
    out: list[str] = []
    for sheet in sheets:
        root = ElementTree.fromstring(package.read(sheet))
        for row in root.iter():
            if row.tag.rsplit("}", 1)[-1] != "row":
                continue
            cells = [_cell_value(cell, shared) for cell in row if cell.tag.rsplit("}", 1)[-1] == "c"]
            if any(cells):
                out.append("\t".join(cells))
    return "\n".join(out)


def _shared_strings(package: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(package.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in entry.iter() if node.tag.rsplit("}", 1)[-1] == "t")
        for entry in root
        if entry.tag.rsplit("}", 1)[-1] == "si"
    ]


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter() if node.tag.rsplit("}", 1)[-1] == "t").strip()
    raw = next((node.text or "" for node in cell if node.tag.rsplit("}", 1)[-1] == "v"), "")
    if kind == "s":
        try:
            return shared[int(raw)].strip()
        except (ValueError, IndexError):
            return ""
    return raw.strip()


def read_text_source(path: Path) -> str:
    """One text-ish file as text; HTML reduced to what a reader would see."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() not in {".htm", ".html"}:
        return text
    parser = _HTMLTextExtractor()
    parser.feed(text)
    return parser.text()


class _HTMLTextExtractor(HTMLParser):
    """Visible text only.

    Script and style bodies are not material -- indexing them puts numbers in
    the fact index that no reader of the page could ever have seen, which is a
    licence to state them on a slide.
    """

    _SKIP = {"script", "style", "svg"}
    _BREAK = {"br", "div", "h1", "h2", "h3", "li", "p", "section", "tr"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._ignored_depth:
            self._ignored_depth -= 1
        elif self._ignored_depth == 0 and tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)

    def text(self) -> str:
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


def text_density(markdown: str) -> tuple[int, int]:
    """``(characters of body text, source pages)`` in an assembled materials file.

    Headings are excluded because the ingest writes them itself: counting its
    own page anchors as text would make a scanned PDF look like it had one.
    """
    chars = sum(len(line.strip()) for line in markdown.splitlines() if not line.lstrip().startswith("#"))
    return chars, len(_PAGE_HEADING_RE.findall(markdown))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_urls(manifest_path: Path, sources: list[Path]) -> dict[Path, str]:
    """Where each downloaded source came from, verified against its hash.

    A fetch tool records what it downloaded in ``sources.jsonl``; the URL from
    that record is the only attribution a web asset has, so a slide crediting a
    source is crediting this file. Which means the file has to still be the one
    that was fetched: a hash mismatch fails the ingest rather than attributing
    a figure to a page it did not come from.
    """
    if not manifest_path.is_file():
        return {}
    wanted = {source.resolve() for source in sources}
    urls: dict[Path, str] = {}
    for line in manifest_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            continue
        recorded = Path(record["path"]).resolve()
        if recorded not in wanted:
            continue
        expected = record.get("content_sha256")
        if not isinstance(expected, str) or sha256_file(recorded) != expected:
            raise ValueError(f"source manifest hash mismatch: {recorded.name}")
        url = record.get("final_url") or record.get("url")
        if isinstance(url, str) and url:
            urls[recorded] = url
    return urls


def image_asset_id(source: Path, materials_dir: Path) -> str:
    """A referenceable id for a standalone image file.

    The stem alone collides -- two directories of downloads both holding
    ``chart.png`` -- and a collision means the second image silently replaces
    the first, so the relative path is hashed in.
    """
    relative = source.relative_to(materials_dir)
    slug = re.sub(r"[^a-z0-9_-]+", "-", source.stem.casefold()).strip("-_") or "image"
    digest = hashlib.sha256(str(relative).encode("utf-8")).hexdigest()[:10]
    return f"{slug[:48]}-{digest}"
