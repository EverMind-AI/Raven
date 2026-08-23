"""`ppt_fetch`: bring something from the web into the deck's own materials.

Search finds things; this is what makes one of them usable. Without it a deck can
only be built from files a human already put on disk, which is fine for a paper and
not fine for "make me a deck about X" -- the author can read a page with web_fetch
and then has nowhere to put the figure it found on it.

Where a download lands is decided here rather than asked for, and that is the
point: a document or an image goes into the materials directory, so the next
`ppt_ingest` reads it like any other source and its figures and numbers enter the
fact index. A `.pptx` goes to the project's template slot instead, because a deck
is not a source to quote -- it is a house style to work inside.

The format is decided by looking at the bytes, not by trusting the server. A URL
ending in `.png` that returns HTML, or a `Content-Type: image/png` on a 40MB video,
are both ordinary on the open web; writing either into the materials directory
would fail later somewhere that reads like a bug in the ingest.
"""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from pathlib import Path
from typing import Any

import httpx

from raven.agent.tools.base import Tool
from raven.ppt.contracts import Project
from raven.ppt.services.ingest import sources
from raven.ppt.services.template import bind, template_dir
from raven.ppt.tools import _return

MAX_BYTES = 32 * 1024 * 1024
TIMEOUT_S = 60.0

# What the bytes may turn out to be, and where each kind belongs.
_DOCUMENT, _IMAGE, _TEMPLATE = "document", "image", "template"

# Where material lands when this deck has no directory of its own yet.


class PptFetchTool(Tool):
    name = "ppt_fetch"
    description = (
        "Download a source into this deck's materials: a PDF, an image, a text or HTML page, or a "
        '.pptx to use as the template. Search first with web_search (kind="images" for pictures), then '
        "fetch what you will actually use. Documents and images land in the materials directory, so the "
        "next ppt_ingest reads them like any other source; a .pptx lands in the project's template slot. "
        "The format is decided by the bytes rather than by the URL or the server's content type."
    )
    timeout_seconds = 120.0

    def __init__(self, workspace: Path, proxy: str | None = None, ingest: Any | None = None) -> None:
        self.workspace = workspace
        self.proxy = proxy
        self.ingest = ingest

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "the deck project, as given to ppt_ingest"},
                "url": {"type": "string", "description": "http(s) URL of the source"},
                "filename": {
                    "type": "string",
                    "description": (
                        "optional name to save it as; the extension comes from what the bytes actually are, "
                        "whatever you pass"
                    ),
                },
            },
            "required": ["project", "url"],
        }

    async def execute(
        self,
        project: str,
        url: str,
        filename: str | None = None,
        **kwargs: Any,
    ) -> str:
        from raven.security.network import validate_url_target

        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))
        try:
            validate_url_target(url)
        except (ValueError, OSError) as exc:
            return _return.failed(f"that URL cannot be fetched: {exc}")

        try:
            payload = await self._download(url)
        except httpx.HTTPError as exc:
            return _return.failed(f"the download failed: {exc}")
        except ValueError as exc:
            return _return.failed(str(exc))

        try:
            suffix, kind = _sniff(payload)
        except ValueError as exc:
            return _return.failed(str(exc))

        name = _safe_name(url, filename, suffix)
        if kind == _TEMPLATE:
            destination = template_dir(deck) / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            return self._bound(deck, destination, project, url, len(payload))

        # Into the deck's own source set, with the URL beside it. There is no
        # `materials_dir` to choose any more: a fetch that could land elsewhere
        # started a second pile, and the ingest that followed replaced the index
        # rather than adding to it -- twice in live runs the source the user supplied
        # left the deck's evidence while sitting on disk.
        source = sources.receive(deck, name, payload, f"{sources.FETCH}{url}")
        if source is None:
            return _return.failed(f"nothing here can read a {suffix} file")
        read = await self._read(deck)
        return _return.done(
            project=project,
            url=url,
            kind=kind,
            path=str(source.path),
            bytes=len(payload),
            **({"sources_read": read} if read else {}),
            asks=([] if read else ["run ppt_ingest so this reaches the fact index and the figure catalogue"]),
        )

    async def _read(self, deck: Project) -> int:
        """Read the deck's sources now that one more has arrived.

        Here rather than left to the author: the reply used to end with "run
        ppt_ingest again", and a run that fetched two papers and forgot would build a
        deck whose evidence did not include them.
        """
        if self.ingest is None:
            return 0
        try:
            outcome = await asyncio.to_thread(self.ingest, deck.sources_dir, deck.ingest_dir)
        except (OSError, ValueError, FileNotFoundError):
            return 0
        return len(outcome.source_files)

    def _bound(self, deck: Project, path: Path, project: str, url: str, size: int) -> str:
        """A downloaded .pptx, prepared the same way one handed over by path is.

        Downloading it and leaving it in the slot was the earlier behaviour, and
        it left the two entry points disagreeing: a template that arrived by URL
        was never prepared, so nothing downstream saw one. Binding here is what
        makes "is there a template?" a question with one answer.
        """
        template = bind(path, deck)
        if template is None:
            path.unlink(missing_ok=True)
            return _return.failed(
                f"{path.name} downloaded but does not open as a presentation",
                url=url,
                bytes=size,
            )
        return _return.done(
            project=project,
            url=url,
            kind=_TEMPLATE,
            template=template.source.name,
            path=str(template.source),
            bytes=size,
            canvas_in=f"{template.inventory.width_in:g}x{template.inventory.height_in:g}",
            example_pages=template.example_pages,
            asks=[
                "call ppt_template with just the project to see the pages this template ships and read "
                "them as python-pptx -- its master, theme, layouts and canvas are the deck's house style now"
            ],
        )

    async def _download(self, url: str) -> bytes:
        """The body, refused before it is held if it is too large to be a source.

        Checked twice: the declared length first, so an oversized file costs
        nothing, and then the bytes as they arrive, because a server may not
        declare one.
        """
        async with httpx.AsyncClient(proxy=self.proxy, follow_redirects=True, timeout=TIMEOUT_S) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                    raise ValueError(f"that source is {int(declared) // 1_048_576}MB; the limit is 32MB")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError("that source is larger than the 32MB limit")
                    chunks.append(chunk)
        return b"".join(chunks)


def _sniff(payload: bytes) -> tuple[str, str]:
    """(suffix, kind) from the bytes themselves."""
    if payload.startswith(b"%PDF-"):
        return ".pdf", _DOCUMENT
    if payload.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile as exc:
            raise ValueError("that download is a broken ZIP archive") from exc
        if {"[Content_Types].xml", "ppt/presentation.xml"}.issubset(names):
            return ".pptx", _TEMPLATE
        raise ValueError("that download is a ZIP archive but not a PowerPoint file")
    if (image := _image_suffix(payload)) is not None:
        return image, _IMAGE
    text = _as_text(payload)
    if text is None:
        raise ValueError("that download is not a PDF, a PowerPoint file, an image, or text")
    # Markup or not, it is text a reader can use, and the ingest strips the tags.
    # The distinction is kept because the ingest reads the two differently.
    return (".html" if "<html" in text[:2000].casefold() else ".md"), _DOCUMENT


def _as_text(payload: bytes) -> str | None:
    """The payload as text, or None when it only happens to decode as some.

    Decoding as UTF-8 is a weaker test than it looks: a run of control bytes
    decodes cleanly and is not a document. So a NUL anywhere near the start
    disqualifies it outright, and a body that is mostly unprintable is treated as
    the binary it is -- otherwise a truncated video saved as `.md` becomes the
    ingest's problem to explain.
    """
    head = payload[:8192]
    if b"\x00" in head:
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    sample = text[:8192]
    if not sample.strip():
        return None
    printable = sum(1 for char in sample if char.isprintable() or char in "\t\r\n")
    return text if printable / len(sample) >= 0.95 else None


def _image_suffix(payload: bytes) -> str | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
            fmt = (image.format or "").upper()
    except (ImportError, OSError, ValueError):
        return None
    return {"GIF": ".gif", "JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(fmt)


def _safe_name(url: str, requested: str | None, suffix: str) -> str:
    raw = Path(requested or url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or "download")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.name).strip("._")[:120] or "download"
    base = safe[: -len(raw.suffix)] if raw.suffix else safe
    return f"{base or 'download'}{suffix}"
