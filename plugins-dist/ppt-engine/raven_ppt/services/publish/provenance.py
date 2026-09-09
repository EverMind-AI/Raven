"""Strip a template vendor's marks from a built deck, so the file says whose it is.

Every bundled template was cut from a commercial kit, and the kit signs the file
in places a slide never shows: the core properties (`dc:title` "iSlide PowerPoint
Template", `dc:creator` "iSlide"), the application properties (`Company`), the
theme's own name and the names of its font and format schemes, an add-in's
`p:tag` records and custom properties, a credit run on a layout, and a bare
"www.islide.cc" on the cover and closing pages. A deck built from the template
inherits all of it, and the PDF rendered from the deck takes its title from the
core properties -- so a reader opening the preview saw the vendor's name in the
title bar over a deck about their own subject.

This runs on the built file before it is measured, staged or rendered, so the
deck the gates describe is the deck delivered, and the PDF beside it carries the
same title. Only the vendor's marks go; the theme's colours, fonts and layouts,
which are what the template was chosen for, are not touched, and neither is
anything the deck says: a paragraph on a slide leaves only when it is nothing
but the vendor's name or address, since a deck about the vendor is the deck's
business.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import uuid
import zipfile
from pathlib import Path

from lxml import etree

VENDOR = re.compile(r"islide", re.IGNORECASE)
"""The vendor whose kits the bundled templates were cut from."""

THEME_NAME = "Raven"
"""What a scheme or theme the vendor had named after itself is called instead."""

CREATOR = "Raven"

_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "vt": "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
}

_DECK_PARTS = re.compile(r"^ppt/(slides|notesSlides)/[^/]+\.xml$")
_TEMPLATE_PARTS = re.compile(r"^ppt/(slideLayouts|slideMasters)/[^/]+\.xml$")
_THEME_PARTS = re.compile(r"^ppt/theme/[^/]+\.xml$")
_TAG_PARTS = re.compile(r"^ppt/tags/[^/]+\.xml$")

_ADDRESS = re.compile(r"(?:https?://)?(?:www\.)?islide\.[a-z]{2,6}(?:/\S*)?", re.IGNORECASE)
"""The vendor's web address, the one credit the bundled templates leave on a slide."""


def strip_vendor_marks(pptx: Path, *, title: str | None = None) -> list[str]:
    """Rewrite `pptx` in place without the vendor's marks; say which parts changed.

    A file that is not a package (a test's placeholder bytes, a build that wrote
    something else) is left alone and reported as nothing changed: this is a
    finishing step, not a gate, and the measurement that follows is what refuses
    a deck that is not a deck.
    """
    if not zipfile.is_zipfile(pptx):
        return []
    changed: list[str] = []
    temporary = pptx.parent / f".{pptx.name}.{uuid.uuid4().hex}.tmp"
    try:
        with zipfile.ZipFile(pptx) as source, zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                data = source.read(info.filename)
                rewritten = _rewrite(info.filename, data, title)
                if rewritten is not None and rewritten != data:
                    changed.append(info.filename)
                    data = rewritten
                target.writestr(info, data)
        os.replace(temporary, pptx)
    finally:
        temporary.unlink(missing_ok=True)
    return changed


def _rewrite(name: str, data: bytes, title: str | None) -> bytes | None:
    if name == "docProps/core.xml":
        return _core(data, title)
    if name == "docProps/app.xml":
        return _app(data)
    if _THEME_PARTS.match(name):
        return _theme(data)
    if _TAG_PARTS.match(name):
        return _tags(data)
    if name == "docProps/custom.xml":
        return _custom(data)
    if _TEMPLATE_PARTS.match(name):
        return _text(data, credits_only=False)
    if _DECK_PARTS.match(name):
        return _text(data, credits_only=True)
    return None


def _parse(data: bytes) -> etree._Element | None:
    try:
        return etree.fromstring(data)
    except etree.XMLSyntaxError:
        return None


def _serialise(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _core(data: bytes, title: str | None) -> bytes | None:
    root = _parse(data)
    if root is None:
        return None
    marked = False
    for tag, value in (("dc:title", title), ("dc:creator", CREATOR), ("cp:lastModifiedBy", CREATOR)):
        node = root.find(tag, _NS)
        current = (node.text or "") if node is not None else ""
        wanted = (
            tag == "cp:lastModifiedBy" or VENDOR.search(current) or (tag == "dc:title" and title and current != title)
        )
        # The last writer is always this build, whoever the kit's editor was; the
        # creator changes only when it is the vendor, so a user's own template
        # keeps its author.
        if not wanted or current == (value or ""):
            continue
        marked = True
        if node is None:
            prefix, local = tag.split(":")
            node = etree.SubElement(root, f"{{{_NS[prefix]}}}{local}")
        node.text = value if value else None
        if tag == "dc:title" and not value:
            root.remove(node)
    for tag in ("cp:keywords", "dc:description", "dc:subject", "cp:category"):
        node = root.find(tag, _NS)
        if node is not None and VENDOR.search(node.text or ""):
            root.remove(node)
            marked = True
    if not marked:
        return None
    modified = root.find("dcterms:modified", _NS)
    if modified is not None:
        modified.text = _dt.datetime.now(_dt.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _serialise(root)


def _app(data: bytes) -> bytes | None:
    root = _parse(data)
    if root is None:
        return None
    marked = False
    for node in root.iter():
        if isinstance(node.tag, str) and node.text and VENDOR.search(node.text):
            local = etree.QName(node).localname
            node.text = "" if local == "Company" else THEME_NAME
            marked = True
    return _serialise(root) if marked else None


def _theme(data: bytes) -> bytes | None:
    root = _parse(data)
    if root is None:
        return None
    marked = False
    for node in root.iter():
        name = node.get("name")
        if name and VENDOR.search(name):
            node.set("name", THEME_NAME)
            marked = True
    return _serialise(root) if marked else None


def _tags(data: bytes) -> bytes | None:
    root = _parse(data)
    if root is None:
        return None
    marked = False
    for tag in list(root.findall("p:tag", _NS)):
        if VENDOR.search(tag.get("name", "")) or VENDOR.search(tag.get("val", "")):
            root.remove(tag)
            marked = True
    return _serialise(root) if marked else None


def _custom(data: bytes) -> bytes | None:
    root = _parse(data)
    if root is None:
        return None
    marked = False
    for prop in list(root):
        if VENDOR.search(prop.get("name", "")) or VENDOR.search("".join(prop.itertext())):
            root.remove(prop)
            marked = True
    return _serialise(root) if marked else None


def _is_credit(text: str) -> bool:
    """Nothing but the vendor's name or address: "iSlide", "www.islide.cc".

    Any other word makes the paragraph the deck's, however short: "PPT by iSlide"
    is a heading somebody wrote, and a credit the template phrased that way lives
    on its layout, where the whole paragraph goes regardless.
    """
    residue = VENDOR.sub("", _ADDRESS.sub("", text))
    return not re.search(r"\w", residue)


def _text(data: bytes, *, credits_only: bool) -> bytes | None:
    """Drop the vendor's paragraphs; on a slide, only a paragraph that is a credit.

    A layout or master is the template's, so a paragraph there that names the
    vendor is the vendor's and goes whole: "Designed by iSlide" minus the name is
    not a caption anybody wanted either. A slide is the deck's; a bare
    "www.islide.cc" the template left on a cover goes, but anything else that
    names the vendor, a heading or a sentence, is what the deck chose to say
    and stays.
    """
    root = _parse(data)
    if root is None:
        return None
    marked = False
    for paragraph in root.iter(f"{{{_NS['a']}}}p"):
        text = "".join(t.text or "" for t in paragraph.iter(f"{{{_NS['a']}}}t"))
        if not VENDOR.search(text) or (credits_only and not _is_credit(text)):
            continue
        for run in list(paragraph):
            if etree.QName(run).localname in ("r", "fld", "br"):
                paragraph.remove(run)
        marked = True
    for node in root.iter(f"{{{_NS['p']}}}cNvPr"):
        for attribute in ("name", "descr", "title"):
            value = node.get(attribute)
            if value and VENDOR.search(value):
                node.set(attribute, VENDOR.sub("", value).strip() or "Shape")
                marked = True
    return _serialise(root) if marked else None


__all__ = ["strip_vendor_marks", "VENDOR", "THEME_NAME", "CREATOR"]
