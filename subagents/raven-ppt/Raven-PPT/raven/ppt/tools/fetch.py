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

A picture's caption comes in with it. Nothing here can see the page a picture sits
on -- the words beside it are in the HTML, not in the bytes -- so the caption is a
parameter, and the author who just read that page with web_fetch is the only thing
holding both. Without it every web figure reached the catalogue with `caption: null`
while a paper's figure arrived with the line printed under it, and a deck given a
marketing banner and no source words captioned it as an architecture diagram.
"""

from __future__ import annotations

import asyncio
import json
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

# Long enough for a paper's caption, short enough that a paragraph pasted in is
# not one.
CAPTION_MAX_CHARS = 500
# EXIF 0x010E is `ImageDescription`; a PNG keeps the same thing in a `tEXt` or
# `iTXt` chunk, which Pillow surfaces in `Image.info` under the chunk's keyword.
_EXIF_IMAGE_DESCRIPTION = 0x010E
_TEXT_CHUNK_KEYS = ("Description", "ImageDescription", "Title")
# Said rather than dropped in silence: a caption belongs to one picture, and a
# document holds many.
_CAPTION_ON_A_DOCUMENT = (
    "the caption was not recorded: it describes one picture, and this download is a document. Its own "
    "figures reach the catalogue with the captions their pages print"
)

# Where material lands when this deck has no directory of its own yet.


def _figure_id(deck, path: Path) -> str | None:
    """The id the figure catalogue keys this file under, once ingest has run.

    Handed back because nothing else can be derived from what this tool returns: the
    catalogue keys a picture as `<stem>-<digest>`, and `ppt_figure_inspect` takes those
    ids and nothing else. A measured run fetched ten images, called inspect with the
    filenames it had just passed here, was told none of them was in the catalogue,
    guessed at the digests on its second try and got their length wrong -- two
    iterations spent recovering a string this tool already had.

    None when the catalogue is not there yet or holds no entry for this file, which is
    the state before ingest has read it.
    """
    from raven.ppt.services.ingest.pipeline import CATALOGUE_FILE

    try:
        loaded = json.loads((deck.ingest_dir / CATALOGUE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    assets = loaded.get("assets") if isinstance(loaded, dict) else None
    for name, entry in (assets or {}).items():
        if isinstance(entry, dict) and entry.get("source_file") == path.name:
            return str(name)
    return None


def _why(exc: Exception) -> str:
    """The exception as something to read.

    `httpx.ConnectTimeout("")` renders as the empty string, so the message that
    reached one run was `the download failed: ` with nothing after the colon --
    no cause, no class, nothing to act on.
    """
    said = str(exc).strip()
    return f"{type(exc).__name__}: {said}" if said else type(exc).__name__


def _reach_hint(exc: Exception, proxy: str | None) -> dict[str, str]:
    """What to try when the host was never reached, as opposed to refusing.

    A status is the server talking and needs no hint. A connect or read timeout
    is the network, and on a machine that reaches the internet through a proxy it
    is almost always that this tool is not using one: every client here is built
    `trust_env=False`, so `HTTPS_PROXY` in the environment -- the setting anyone
    would reach for first -- is deliberately ignored and the tool goes direct.
    Measured on one run: three fetches timed out at 50s each while curl through
    the very same proxy answered in 0.86s, and nothing in the refusal said why.
    """
    if isinstance(exc, httpx.HTTPStatusError) or proxy:
        return {}
    if not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)):
        return {}
    return {
        "hint": "the host was never reached and no proxy is configured. This tool ignores HTTPS_PROXY in "
        "the environment on purpose; set tools.web.proxy in the config instead, which the web tools and "
        "this one all read"
    }


class PptFetchTool(Tool):
    name = "ppt_fetch"
    description = (
        "Download a source into this deck's materials: a PDF, an image, a text or HTML page, or a "
        '.pptx to use as the template. Search first with web_search (kind="images" for pictures), then '
        "fetch what you will actually use. A document or an image is ingested on arrival, so its text and "
        "figures are in the deck's evidence when this returns; a .pptx is bound as the deck's template. "
        "The format is decided by the bytes rather than by the URL or the server's content type. When you "
        "fetch a picture, pass the words the page printed about it as caption -- that is the only way they "
        "reach the figure catalogue, since nothing here can see the page the picture came off. A picture "
        "comes back with the figure_id the catalogue keys it under; that is what ppt_figure_inspect and a "
        "page's figure take, and it is not the filename you asked for."
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
                "project": {"type": "string", "description": "the deck project, as given to ppt_prepare"},
                "url": {"type": "string", "description": "http(s) URL of the source"},
                "filename": {
                    "type": "string",
                    "description": (
                        "optional name to save it as; the extension comes from what the bytes actually are, "
                        "whatever you pass"
                    ),
                },
                "caption": {
                    "type": "string",
                    "description": (
                        "for a picture: the source's own words about it -- the caption printed beside it, "
                        'its figure label, its alt text, whatever web_fetch(extractMode="images") listed '
                        "for it. Copy those words across; do not write your own summary of what you think "
                        "the picture shows. This lands in the figure catalogue as the caption a page may "
                        "credit, so an invented one is a claim about evidence that nobody made. Leave it out "
                        "when the page said nothing"
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
        caption: str | None = None,
        **kwargs: Any,
    ) -> str:
        from raven.security.network import validate_url_target

        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))
        # Reads the verdict rather than waiting for a throw: this returns
        # `(ok, why)` and raises nothing, so catching an exception here let every
        # refused address through -- loopback and the cloud metadata endpoint
        # among them -- for as long as the call has been written this way.
        allowed, refusal = validate_url_target(url)
        if not allowed:
            return _return.failed(f"that URL cannot be fetched: {refusal}")

        try:
            payload = await self._download(url)
        except httpx.HTTPError as exc:
            return _return.failed(f"the download failed: {_why(exc)}", **_reach_hint(exc, self.proxy))
        except ValueError as exc:
            return _return.failed(str(exc))

        try:
            payload, suffix, kind = _sniff(payload)
        except ValueError as exc:
            return _return.failed(str(exc))

        name = _safe_name(url, filename, suffix)
        if kind == _TEMPLATE:
            destination = template_dir(deck) / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            return self._bound(deck, destination, project, url, len(payload))

        # The caller's words beat the file's own metadata: they are what the page
        # printed next to this picture, while an `ImageDescription` field is whatever
        # the file happened to be exported with. The metadata is the fallback for a
        # fetch that arrives with nothing, which is how every web picture arrived
        # before this parameter existed.
        described = (_clean(caption) or _embedded_caption(payload)) if kind == _IMAGE else None

        # Into the deck's own source set, with the URL beside it. There is no
        # `materials_dir` to choose any more: a fetch that could land elsewhere
        # started a second pile, and the ingest that followed replaced the index
        # rather than adding to it -- twice in live runs the source the user supplied
        # left the deck's evidence while sitting on disk.
        source = sources.receive(deck, name, payload, f"{sources.FETCH}{url}", caption=described)
        if source is None:
            return _return.failed(f"nothing here can read a {suffix} file")
        read = await self._read(deck)
        figure_id = _figure_id(deck, source.path) if kind == _IMAGE else None
        return _return.done(
            project=project,
            url=url,
            kind=kind,
            path=str(source.path),
            bytes=len(payload),
            **({"figure_id": figure_id} if figure_id else {}),
            **({"caption": source.caption} if source.caption else {}),
            **({"note": _CAPTION_ON_A_DOCUMENT} if caption and kind != _IMAGE else {}),
            **({"sources_read": read} if read else {}),
            asks=([] if read else ["run ppt_ingest so this reaches the deck's materials and figure catalogue"]),
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
        async with httpx.AsyncClient(
            proxy=self.proxy,
            follow_redirects=True,
            timeout=TIMEOUT_S,
            trust_env=False,
        ) as client:
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


def _sniff(payload: bytes) -> tuple[bytes, str, str]:
    """(payload, suffix, kind) from the bytes themselves.

    The payload comes back because one kind is not usable as it arrives: a brand
    ships its mark as SVG, python-pptx places raster images only, and an SVG is
    text -- so it fell through to the document branch and a logo was saved as
    `mem0-logo.md` holding path data. The deck that followed had a competitor
    analysis with none of the competitor's marks on it. Rasterised here rather
    than at placement time, because every reader downstream -- the ingest
    catalogue, the figure inspector, the author -- asks the same question of it,
    and one of them answering "it is a document" is what happened.
    """
    if payload.startswith(b"%PDF-"):
        return payload, ".pdf", _DOCUMENT
    if payload.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile as exc:
            raise ValueError("that download is a broken ZIP archive") from exc
        if {"[Content_Types].xml", "ppt/presentation.xml"}.issubset(names):
            return payload, ".pptx", _TEMPLATE
        raise ValueError("that download is a ZIP archive but not a PowerPoint file")
    if (image := _image_suffix(payload)) is not None:
        return payload, image, _IMAGE
    text = _as_text(payload)
    if text is None:
        raise ValueError("that download is not a PDF, a PowerPoint file, an image, or text")
    if (raster := _rasterised_svg(payload, text)) is not None:
        return raster, ".png", _IMAGE
    # Markup or not, it is text a reader can use, and the ingest strips the tags.
    # The distinction is kept because the ingest reads the two differently.
    return payload, (".html" if "<html" in text[:2000].casefold() else ".md"), _DOCUMENT


# Wide enough that a mark stays crisp across a title bar, and small enough that a
# logo is not a megabyte. A vector has no size of its own, so one has to be chosen.
SVG_WIDTH_PX = 1600


def _rasterised_svg(payload: bytes, text: str) -> bytes | None:
    """An SVG as PNG bytes, or None when this is not an SVG or cannot be drawn.

    None rather than an error on a failure to convert: an SVG that will not
    rasterise is still text, and the document branch below can still keep it.
    """
    if "<svg" not in text[:4096].casefold():
        return None
    try:
        import cairosvg
    except (ImportError, OSError):
        # cairocffi raises OSError, not ImportError, when the native cairo library
        # is missing under an installed cairosvg. Catching only the one leaves the
        # other to end the fetch, and an SVG kept as text is the whole point here.
        return None
    try:
        return cairosvg.svg2png(bytestring=payload, output_width=SVG_WIDTH_PX)
    except Exception:  # noqa: BLE001 -- any failure here means "not an image after all"
        return None


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


def _clean(text: str | None) -> str | None:
    """One caption line, or None when there is nothing usable in ``text``.

    Control bytes go first and separately: an EXIF string is NUL-padded, and a
    whitespace collapse leaves those in place -- straight into the catalogue the
    ingest writes and a slide's source note reads.
    """
    if not text:
        return None
    collapsed = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", text)).strip()
    return collapsed[:CAPTION_MAX_CHARS] or None


def _embedded_caption(payload: bytes) -> str | None:
    """The description an image file carries about itself, if it carries one.

    Only what the bytes hold. A page's `alt` text and its `<figcaption>` are not
    here -- they live in the HTML around the picture, which this never sees -- and
    that is why the caller is asked for them rather than this being the whole
    answer. What a real file does sometimes carry is an EXIF `ImageDescription`, or
    a PNG text chunk keyed `Description` or `Title`, both written by whatever
    exported it.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(payload)) as image:
            candidates = [image.getexif().get(_EXIF_IMAGE_DESCRIPTION)]
            candidates.extend(image.info.get(key) for key in _TEXT_CHUNK_KEYS)
    except Exception:  # noqa: BLE001 -- metadata this cannot read is metadata this does not have
        return None
    for candidate in candidates:
        if isinstance(candidate, bytes):
            candidate = candidate.decode("utf-8", "replace")
        if isinstance(candidate, str) and (found := _clean(candidate)) is not None:
            return found
    return None


def _safe_name(url: str, requested: str | None, suffix: str) -> str:
    raw = Path(requested or url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or "download")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.name).strip("._")[:120] or "download"
    base = safe[: -len(raw.suffix)] if raw.suffix else safe
    return f"{base or 'download'}{suffix}"
