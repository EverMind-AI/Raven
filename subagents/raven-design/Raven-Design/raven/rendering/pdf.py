"""Validate, inspect, and rasterize PDF documents."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Protocol

import fitz
from PIL import Image, ImageChops

from raven.rendering.models import RenderConfig, RenderError
from raven.rendering.util import crop_white_margin, file_record, sha256_file


class PdfBackend(Protocol):
    name: str

    def rasterize(
        self,
        pdf_path: Path,
        pages_dir: Path,
        page_range: str | None,
        config: RenderConfig,
        bundle_root: Path,
        *,
        crop_to_content: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]: ...

    def image_to_pdf(self, image_path: Path, pdf_path: Path) -> None: ...


class PyMuPdfBackend:
    name = "pymupdf"

    def rasterize(
        self,
        pdf_path: Path,
        pages_dir: Path,
        page_range: str | None,
        config: RenderConfig,
        bundle_root: Path,
        *,
        crop_to_content: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return rasterize_pdf(
            pdf_path,
            pages_dir,
            page_range,
            config,
            bundle_root,
            crop_to_content=crop_to_content,
        )

    def image_to_pdf(self, image_path: Path, pdf_path: Path) -> None:
        image_to_pdf(image_path, pdf_path)


def parse_page_range(value: str | None, page_count: int) -> list[int]:
    if page_count < 1:
        raise RenderError("invalid_output", "The rendered PDF has no pages.")
    if value is None:
        return list(range(page_count))
    selected: list[int] = []
    seen: set[int] = set()
    try:
        for raw_token in value.split(","):
            token = raw_token.strip()
            if not token:
                raise ValueError
            if "-" in token:
                start_text, end_text = token.split("-", 1)
                start, end = int(start_text), int(end_text)
                if start > end:
                    raise ValueError
                values = range(start, end + 1)
            else:
                values = [int(token)]
            for page in values:
                if page < 1 or page > page_count:
                    raise ValueError
                index = page - 1
                if index not in seen:
                    seen.add(index)
                    selected.append(index)
    except ValueError as exc:
        raise RenderError(
            "invalid_parameters",
            f"Invalid page range for a {page_count}-page document.",
            details={"page_range": value, "page_count": page_count},
        ) from exc
    return selected


def rasterize_pdf(
    pdf_path: Path,
    pages_dir: Path,
    page_range: str | None,
    config: RenderConfig,
    bundle_root: Path,
    *,
    crop_to_content: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    crosschecked_page_count = validate_pdf_structure(pdf_path, config)
    try:
        document = fitz.open(pdf_path)
    except Exception as exc:
        raise RenderError("invalid_output", "The generated PDF cannot be opened.") from exc
    try:
        if document.needs_pass:
            raise RenderError("encrypted_document", "The generated PDF is encrypted.")
        page_count = document.page_count
        if page_count < 1:
            raise RenderError("invalid_output", "The generated PDF has no pages.")
        if crosschecked_page_count is not None and crosschecked_page_count != page_count:
            raise RenderError(
                "invalid_output",
                "Independent PDF engines disagree on the page count.",
            )
        if page_count > config.max_pages:
            raise RenderError(
                "resource_limit_exceeded",
                "The generated PDF exceeds the page limit.",
                details={"pages": page_count, "limit": config.max_pages},
            )
        selected = parse_page_range(page_range, page_count)
        pages_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        scale = config.raster_dpi / 72
        total_pixels = 0
        for index in selected:
            page = document.load_page(index)
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                alpha=False,
                colorspace=fitz.csRGB,
            )
            if max(pixmap.width, pixmap.height) > config.max_side_pixels:
                raise RenderError(
                    "resource_limit_exceeded",
                    "A rasterized PDF page exceeds the pixel limit.",
                    details={
                        "page": index + 1,
                        "width": pixmap.width,
                        "height": pixmap.height,
                    },
                )
            total_pixels += pixmap.width * pixmap.height
            if total_pixels > config.max_total_pixels:
                raise RenderError(
                    "resource_limit_exceeded",
                    "Rasterized PDF pages exceed the total pixel limit.",
                )
            target = pages_dir / f"page-{index + 1:04d}.png"
            crop_bounds = None
            if crop_to_content:
                image = Image.frombytes(
                    "RGB",
                    (pixmap.width, pixmap.height),
                    pixmap.samples,
                )
                image, crop_bounds = crop_white_margin(image)
                image.save(target, "PNG", optimize=True)
            else:
                pixmap.save(target)
            record = file_record(target, bundle_root)
            record["page"] = index + 1
            if crop_bounds is not None:
                record["source_width"] = pixmap.width
                record["source_height"] = pixmap.height
                record["content_crop"] = {
                    "left": crop_bounds[0],
                    "top": crop_bounds[1],
                    "right": crop_bounds[2],
                    "bottom": crop_bounds[3],
                }
            records.append(record)
        pdf_record = {
            "path": pdf_path.relative_to(bundle_root).as_posix(),
            "bytes": pdf_path.stat().st_size,
            "page_count": page_count,
            "rendered_pages": [index + 1 for index in selected],
        }
        pdf_record["sha256"] = sha256_file(pdf_path)
        return pdf_record, records
    finally:
        document.close()


def validate_pdf_structure(pdf_path: Path, config: RenderConfig) -> int | None:
    if config.qpdf_path:
        try:
            result = subprocess.run(
                [
                    config.qpdf_path,
                    "--check",
                    "--warning-exit-0",
                    str(pdf_path),
                ],
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RenderError(
                "invalid_output",
                "qpdf could not validate the generated PDF.",
            ) from exc
        if result.returncode != 0:
            raise RenderError(
                "invalid_output",
                "qpdf rejected the generated PDF.",
                details={"stderr": result.stderr[-1000:]},
            )
    try:
        import pypdfium2
    except ModuleNotFoundError:
        return None
    try:
        document = pypdfium2.PdfDocument(str(pdf_path))
        try:
            page_count = len(document)
        finally:
            document.close()
    except Exception as exc:
        raise RenderError(
            "invalid_output",
            "PDFium rejected the generated PDF.",
        ) from exc
    if page_count < 1:
        raise RenderError("invalid_output", "PDFium found no pages.")
    return page_count


def image_difference(first: Path, second: Path) -> dict[str, float]:
    with Image.open(first) as left_image, Image.open(second) as right_image:
        left = _visible_rgb(left_image)
        right = _visible_rgb(right_image)
        if right.size != left.size:
            right = right.resize(left.size, Image.Resampling.LANCZOS)
        difference = ImageChops.difference(left, right)
        histogram = difference.histogram()
        pixels = left.width * left.height
        total_delta = sum(value * count for value, count in enumerate(histogram[:256]))
        total_delta += sum((value - 256) * count for value, count in enumerate(histogram[256:512], start=256))
        total_delta += sum((value - 512) * count for value, count in enumerate(histogram[512:], start=512))
        thresholded = difference.convert("L").point(lambda value: 255 if value > 12 else 0)
        changed = thresholded.histogram()[255]
        return {
            "changed_pixel_ratio": round(changed / pixels, 8),
            "mean_absolute_channel_delta": round(total_delta / (pixels * 3 * 255), 8),
        }


def _visible_rgb(image: Image.Image) -> Image.Image:
    if image.mode not in {"RGBA", "LA"} and "transparency" not in image.info:
        return image.convert("RGB")
    foreground = image.convert("RGBA")
    background = Image.new("RGBA", foreground.size, "white")
    background.alpha_composite(foreground)
    return background.convert("RGB")


def image_to_pdf(image_path: Path, pdf_path: Path) -> None:
    with Image.open(image_path) as image:
        width, height = image.size
    document = fitz.open()
    temporary = pdf_path.with_name(f".{pdf_path.stem}.tmp.pdf")
    try:
        page = document.new_page(width=width / 2, height=height / 2)
        page.insert_image(page.rect, filename=str(image_path))
        document.save(temporary, garbage=4, deflate=True)
    finally:
        document.close()
    temporary.replace(pdf_path)
