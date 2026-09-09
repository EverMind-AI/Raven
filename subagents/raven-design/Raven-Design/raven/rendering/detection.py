"""Detect supported visual document formats and motion signals."""

from __future__ import annotations

import gzip
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image

from raven.rendering.models import Detection, RenderError

MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "html": "text/html",
    "svg": "image/svg+xml",
    "animated_gif": "image/gif",
    "apng": "image/apng",
    "animated_webp": "image/webp",
}
_MAX_OOXML_EXPANDED_BYTES = 200 * 1024 * 1024
_MAX_SVGZ_EXPANDED_BYTES = 16 * 1024 * 1024
_SNIFF_BYTES = 1024 * 1024


def _motion_signals(text: str) -> list[str]:
    patterns = {
        "smil": r"<(?:animate|animateTransform|animateMotion|set)\b",
        "css_animation": r"@keyframes|\banimation\s*:",
        "css_transition": r"\btransition\s*:",
        "script": r"<script\b",
        "request_animation_frame": r"\brequestAnimationFrame\b",
        "timer": r"\b(?:setTimeout|setInterval)\b",
        "canvas": r"<canvas\b|\bgetContext\s*\(",
        "media": r"<(?:video|audio)\b",
        "event_handler": r"\bon(?:click|load|mouse|pointer|touch|key|wheel)\w*\s*=",
        "user_interaction": (
            r"\bon(?:click|mouse|pointer|touch|key|wheel|input|change|submit)\w*\s*="
            r"|\baddEventListener\s*\(\s*['\"]"
            r"(?:click|dblclick|mouse\w*|pointer\w*|touch\w*|key\w*|wheel|input|change|submit)"
        ),
        "hover": r":hover\b",
        "web_animations": r"\.animate\s*\(",
    }
    return [name for name, pattern in patterns.items() if re.search(pattern, text, re.IGNORECASE)]


def _safe_zip(path: Path, max_expanded_bytes: int) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise RenderError("unsafe_input", "The OOXML ZIP container is invalid.") from exc
    total = 0
    for item in archive.infolist():
        member = PurePosixPath(item.filename)
        if member.is_absolute() or ".." in member.parts:
            archive.close()
            raise RenderError("unsafe_input", "The ZIP container contains an unsafe path.")
        total += item.file_size
        if total > max_expanded_bytes:
            archive.close()
            raise RenderError(
                "resource_limit_exceeded",
                "The expanded OOXML container exceeds the safety limit.",
            )
    return archive


def _xml_root_name(data: bytes) -> str | None:
    try:
        return ET.fromstring(data).tag.rsplit("}", 1)[-1].lower()
    except ET.ParseError:
        return None


def _core_properties(archive: zipfile.ZipFile) -> dict[str, Any]:
    if "docProps/core.xml" not in archive.namelist():
        return {"title": None, "author": None}
    try:
        root = ET.fromstring(archive.read("docProps/core.xml"))
    except ET.ParseError:
        return {"title": None, "author": None}
    values: dict[str, str | None] = {"title": None, "author": None}
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1]
        if local == "title":
            values["title"] = element.text
        elif local == "creator":
            values["author"] = element.text
    return values


def _required_xml(archive: zipfile.ZipFile, path: str) -> ET.Element:
    try:
        return ET.fromstring(archive.read(path))
    except (KeyError, ET.ParseError) as exc:
        raise RenderError(
            "unsafe_input",
            f"The OOXML part is invalid: {path}.",
        ) from exc


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _relationship_id(node: ET.Element) -> str | None:
    return next(
        (
            value
            for key, value in node.attrib.items()
            if key == "r:id" or key.startswith("{") and _local_name(key) == "id"
        ),
        None,
    )


def _relationship_targets(
    archive: zipfile.ZipFile,
    relationship_path: str,
    base_path: str,
) -> dict[str, str]:
    if relationship_path not in archive.namelist():
        return {}
    try:
        root = ET.fromstring(archive.read(relationship_path))
    except ET.ParseError:
        return {}
    targets: dict[str, str] = {}
    for node in root:
        if _local_name(node.tag) != "Relationship":
            continue
        relationship_id = node.attrib.get("Id")
        target = node.attrib.get("Target")
        if not relationship_id or not target or node.attrib.get("TargetMode", "").lower() == "external":
            continue
        normalized = posixpath.normpath(
            target.lstrip("/") if target.startswith("/") else posixpath.join(base_path, target)
        )
        if normalized.startswith(("../", "/")):
            continue
        targets[relationship_id] = normalized
    return targets


def _xlsx_sheet_metadata(
    archive: zipfile.ZipFile,
    workbook: ET.Element,
) -> tuple[list[dict[str, Any]], int]:
    relationships = _relationship_targets(
        archive,
        "xl/_rels/workbook.xml.rels",
        "xl",
    )
    active_index = 0
    for node in workbook.iter():
        if _local_name(node.tag) != "workbookView":
            continue
        try:
            active_index = int(node.attrib.get("activeTab", "0"))
        except ValueError:
            active_index = 0
        break
    print_areas: dict[int, str] = {}
    for node in workbook.iter():
        if _local_name(node.tag) != "definedName" or node.attrib.get("name") != "_xlnm.Print_Area" or node.text is None:
            continue
        try:
            print_areas[int(node.attrib["localSheetId"])] = node.text
        except (KeyError, ValueError):
            continue
    sheets: list[dict[str, Any]] = []
    for index, node in enumerate(item for item in workbook.iter() if _local_name(item.tag) == "sheet"):
        relationship_id = _relationship_id(node)
        sheet_path = relationships.get(relationship_id or "")
        sheet: dict[str, Any] = {
            "index": index,
            "name": node.attrib.get("name"),
            "state": node.attrib.get("state", "visible"),
            "active": index == active_index,
            "xml_path": sheet_path,
            "used_range": None,
            "print_area": print_areas.get(index),
            "page_setup": {},
            "column_widths": [],
            "formula_count": 0,
            "merge_count": 0,
            "conditional_format_count": 0,
            "drawing_count": 0,
            "table_count": 0,
            "auto_filter_range": None,
            "frozen_pane": None,
        }
        if not sheet_path or sheet_path not in archive.namelist():
            sheets.append(sheet)
            continue
        try:
            sheet_root = ET.fromstring(archive.read(sheet_path))
        except ET.ParseError:
            sheets.append(sheet)
            continue
        for sheet_node in sheet_root.iter():
            local = _local_name(sheet_node.tag)
            if local == "dimension" and sheet["used_range"] is None:
                sheet["used_range"] = sheet_node.attrib.get("ref")
            elif local == "pageSetup":
                sheet["page_setup"] = dict(sheet_node.attrib)
            elif local == "col":
                sheet["column_widths"].append(
                    {
                        key: sheet_node.attrib[key]
                        for key in (
                            "min",
                            "max",
                            "width",
                            "customWidth",
                            "bestFit",
                            "hidden",
                        )
                        if key in sheet_node.attrib
                    }
                )
            elif local == "f":
                sheet["formula_count"] += 1
            elif local == "mergeCell":
                sheet["merge_count"] += 1
            elif local == "conditionalFormatting":
                sheet["conditional_format_count"] += 1
            elif local == "drawing":
                sheet["drawing_count"] += 1
            elif local == "tablePart":
                sheet["table_count"] += 1
            elif local == "autoFilter" and sheet["auto_filter_range"] is None:
                sheet["auto_filter_range"] = sheet_node.attrib.get("ref")
            elif local == "pane" and sheet_node.attrib.get("state") in {
                "frozen",
                "frozenSplit",
            }:
                sheet["frozen_pane"] = {
                    key: sheet_node.attrib[key]
                    for key in ("xSplit", "ySplit", "topLeftCell", "activePane")
                    if key in sheet_node.attrib
                }
        sheets.append(sheet)
    return sheets, active_index


def _pptx_slide_metadata(
    archive: zipfile.ZipFile,
    presentation: ET.Element,
) -> list[dict[str, Any]]:
    relationships = _relationship_targets(
        archive,
        "ppt/_rels/presentation.xml.rels",
        "ppt",
    )
    slide_paths = []
    for node in presentation.iter():
        if _local_name(node.tag) != "sldId":
            continue
        relationship_id = _relationship_id(node)
        path = relationships.get(relationship_id or "")
        if path:
            slide_paths.append(path)
    if not slide_paths:
        slide_paths = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.search(r"\d+", Path(name).stem).group()),
        )
    slides = []
    for index, path in enumerate(slide_paths):
        if path not in archive.namelist():
            continue
        data = archive.read(path)
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            continue
        slides.append(
            {
                "index": index,
                "xml_path": path,
                "hidden": str(root.attrib.get("show", "1")).lower() in {"0", "false"},
                "transition_count": sum(_local_name(node.tag) == "transition" for node in root.iter()),
                "timing_tree_count": sum(_local_name(node.tag) == "timing" for node in root.iter()),
            }
        )
    return slides


def _office_detection(
    path: Path,
    extension: str,
    max_expanded_bytes: int,
) -> Detection:
    with _safe_zip(path, max_expanded_bytes) as archive:
        names = archive.namelist()
        if "word/document.xml" in names:
            detected = "docx"
            primary = archive.read("word/document.xml")
            _required_xml(archive, "word/document.xml")
            metadata = {
                "section_count": primary.count(b"<w:sectPr"),
                "table_count": primary.count(b"<w:tbl>"),
                "drawing_count": primary.count(b"<w:drawing"),
                "header_count": sum(name.startswith("word/header") and name.endswith(".xml") for name in names),
                "footer_count": sum(name.startswith("word/footer") and name.endswith(".xml") for name in names),
            }
        elif "ppt/presentation.xml" in names:
            detected = "pptx"
            presentation = _required_xml(archive, "ppt/presentation.xml")
            slides = _pptx_slide_metadata(archive, presentation)
            metadata = {
                "slide_count": len(slides),
                "hidden_slide_count": sum(slide["hidden"] for slide in slides),
                "notes_count": sum(
                    re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name) is not None for name in names
                ),
                "media_count": sum("/media/" in name for name in names),
                "chart_count": sum("/charts/chart" in name and name.endswith(".xml") for name in names),
                "transition_count": sum(slide["transition_count"] for slide in slides),
                "timing_tree_count": sum(slide["timing_tree_count"] for slide in slides),
                "slides": slides,
            }
        elif "xl/workbook.xml" in names:
            detected = "xlsx"
            workbook = _required_xml(archive, "xl/workbook.xml")
            sheets, active_index = _xlsx_sheet_metadata(archive, workbook)
            active_sheet = sheets[active_index]["name"] if 0 <= active_index < len(sheets) else None
            metadata = {
                "sheet_count": len(sheets),
                "visible_sheet_count": sum(sheet["state"] == "visible" for sheet in sheets),
                "hidden_sheet_count": sum(sheet["state"] != "visible" for sheet in sheets),
                "active_sheet": active_sheet,
                "sheets": sheets,
                "chart_count": sum("/charts/chart" in name and name.endswith(".xml") for name in names),
                "pivot_table_count": sum(
                    name.startswith("xl/pivotTables/pivotTable") and name.endswith(".xml") for name in names
                ),
                "external_link_count": sum(
                    name.startswith("xl/externalLinks/externalLink") and name.endswith(".xml") for name in names
                ),
            }
        else:
            raise RenderError(
                "unsupported_format",
                "The ZIP container is not a supported OOXML document.",
            )
        metadata.update(_core_properties(archive))
        metadata["has_macros"] = any(name.endswith("vbaProject.bin") for name in names)
        metadata["embedded_object_count"] = sum("/embeddings/" in name for name in names)
        metadata["package_part_count"] = len(names)
    support = "guaranteed" if extension == f".{detected}" else "best_effort"
    return Detection(
        format=detected,
        family="office",
        mime=MIME[detected],
        declared_extension=extension,
        support_level=support,
        metadata=metadata,
    )


def _web_detection(path: Path, extension: str, data: bytes) -> Detection:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    root_name = _xml_root_name(data)
    if root_name == "svg" or extension in {".svg", ".svgz"} and "<svg" in text.lower():
        try:
            root = ET.fromstring(data)
            metadata = {
                "width": root.attrib.get("width"),
                "height": root.attrib.get("height"),
                "view_box": root.attrib.get("viewBox") or root.attrib.get("viewbox"),
                "element_count": sum(1 for _ in root.iter()),
                "script_count": sum(node.tag.rsplit("}", 1)[-1].lower() == "script" for node in root.iter()),
            }
        except ET.ParseError as exc:
            raise RenderError("unsafe_input", "The SVG XML is invalid.") from exc
        return Detection(
            format="svg",
            family="browser",
            mime=MIME["svg"],
            declared_extension=extension,
            support_level="guaranteed",
            metadata=metadata,
            motion_signals=_motion_signals(text),
        )
    if extension in {".html", ".htm"} or re.search(r"<!doctype\s+html|<html\b", text, re.IGNORECASE):
        title = re.search(r"<title\b[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        metadata = {
            "title": re.sub(r"\s+", " ", title.group(1)).strip() if title else None,
            "script_count": len(re.findall(r"<script\b", text, re.IGNORECASE)),
            "canvas_count": len(re.findall(r"<canvas\b", text, re.IGNORECASE)),
            "inline_svg_count": len(re.findall(r"<svg\b", text, re.IGNORECASE)),
            "external_asset_count": len(re.findall(r"""(?:src|href)\s*=\s*["'](?:https?:)?//""", text, re.IGNORECASE)),
        }
        return Detection(
            format="html",
            family="browser",
            mime=MIME["html"],
            declared_extension=extension,
            support_level="guaranteed",
            metadata=metadata,
            motion_signals=_motion_signals(text),
        )
    raise RenderError("unsupported_format", "The document is not valid HTML or SVG.")


def _animated_image_detection(
    path: Path,
    extension: str,
    max_frames: int,
    max_total_pixels: int,
) -> Detection:
    try:
        with Image.open(path) as image:
            frames = getattr(image, "n_frames", 1)
            if frames <= 1:
                raise RenderError(
                    "native_read_preferred",
                    "The input is a static image; use the native media reader.",
                )
            if frames > max_frames or image.width * image.height * frames > max_total_pixels:
                raise RenderError(
                    "resource_limit_exceeded",
                    "The animated image exceeds the decode safety limits.",
                )
            image.seek(0)
            durations = []
            for index in range(frames):
                image.seek(index)
                durations.append(int(image.info.get("duration", 0)))
            if image.format == "GIF":
                detected = "animated_gif"
            elif image.format == "PNG":
                detected = "apng"
            elif image.format == "WEBP":
                detected = "animated_webp"
            else:
                raise RenderError("unsupported_format", "The animated image format is unsupported.")
            metadata = {
                "width": image.width,
                "height": image.height,
                "frame_count": frames,
                "duration_seconds": sum(durations) / 1000,
                "frame_durations_ms": durations,
                "loop_count": image.info.get("loop"),
                "mode": image.mode,
            }
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError("unsafe_input", "The animated image cannot be decoded.") from exc
    return Detection(
        format=detected,
        family="animated_image",
        mime=MIME[detected],
        declared_extension=extension,
        support_level="guaranteed",
        metadata=metadata,
        motion_signals=["container_animation"],
    )


def detect_file(
    path: Path,
    *,
    max_expanded_bytes: int = _MAX_OOXML_EXPANDED_BYTES,
    max_animation_frames: int = 1000,
    max_total_pixels: int = 500_000_000,
) -> Detection:
    extension = path.suffix.lower()
    if extension == ".svgz":
        try:
            with gzip.open(path, "rb") as handle:
                data = handle.read(_MAX_SVGZ_EXPANDED_BYTES + 1)
        except OSError as exc:
            raise RenderError("unsafe_input", "The SVGZ stream is invalid.") from exc
        if len(data) > _MAX_SVGZ_EXPANDED_BYTES:
            raise RenderError(
                "resource_limit_exceeded",
                "The expanded SVGZ document exceeds the safety limit.",
            )
        return _web_detection(path, extension, data)
    if extension == ".svg":
        if path.stat().st_size > max_expanded_bytes:
            raise RenderError(
                "resource_limit_exceeded",
                "The SVG document exceeds the safety limit.",
            )
        return _web_detection(path, extension, path.read_bytes())
    with path.open("rb") as handle:
        head = handle.read(_SNIFF_BYTES)
    if head.startswith(b"PK\x03\x04"):
        return _office_detection(path, extension, max_expanded_bytes)
    if head.startswith(b"%PDF-"):
        raise RenderError("native_read_preferred", "PDF is already the normalized render output.")
    if head.startswith((b"GIF87a", b"GIF89a", b"\x89PNG\r\n\x1a\n", b"RIFF")):
        return _animated_image_detection(
            path,
            extension,
            max_animation_frames,
            max_total_pixels,
        )
    if extension in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        raise RenderError(
            "native_read_preferred",
            "The input is a static image; use the native media reader.",
        )
    if extension in {
        ".txt",
        ".md",
        ".csv",
        ".tsv",
        ".py",
        ".js",
        ".json",
        ".yaml",
        ".yml",
    }:
        raise RenderError(
            "native_read_preferred",
            "The input is directly readable and does not require visual rendering.",
        )
    return _web_detection(path, extension, head)
