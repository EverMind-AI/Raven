"""The URLs the materials cite, and which of them anything has actually opened.

Ingest extracts figures out of the files a deck holds. It cannot extract one out
of a URL a file merely *cites*, so a deck built from a document full of links has
an empty figure catalogue and every picture it could have still sitting on the
other end of a link. A live run ended exactly there: 56 cited URLs, one of them a
documentation page whose architecture diagram carries its author's own caption,
and twenty finished pages without a single image on them. The errand that asked
for the sweep was sent and read; the author replied in its second turn that the
material was "a text-based competitive analysis with no figures to fetch" and
never looked.

Two counts settle that, and only counts, because the distinction is not a
judgement: how many URLs the materials cite, and how many of those anything has
opened. Below is what makes each of them checkable.

`cited` is deliberately narrow. A URL only counts when it could be a source to
open -- namespace declarations that come out of an XML dump are not citations,
and a deck whose materials cite nothing must not be held for failing to sweep
something that is not there.

`accounted` is the other half, and it is evidence rather than assertion. A URL
this deck fetched is recorded in the source manifest by whoever fetched it, so a
sweep that brought something in needs no declaration at all. What is left is the
sweep that found nothing, which leaves no trace anywhere -- so the author records
it here, naming the URL and what came back. That record is per deck rather than
per call because `ppt_outline` runs again on every replan: a sweep restated on
each call is a sweep forgotten on one of them, and forgetting it would refuse a
plan with nothing wrong.

That record was still only an assertion, and the next live run went straight
through it: thirteen URLs declared swept, thirteen plausible sentences about what
each one held, and not one `fetch:` line in that deck's source manifest. One of
the thirteen was the mem0 page again, recorded as "text and code snippets only",
and opening it takes a single request to find an architecture diagram sitting
under its author's caption. So an entry claiming a page held nothing is now
opened here before it is believed, and one the page contradicts is refused with
the picture's address and caption attached -- which is exactly what the author
said it could not find.

Everything about that check leans one way. It opens only the pages claimed empty,
because a page fetched into the deck is already recorded and needs no entry. It
counts a picture only on terms stricter than the image extractor's. And every way
of failing to read a page -- refused, timed out, moved, not HTML, unparseable --
lets the entry stand: a gate that turned a dropped connection into a refusal would
hold decks that have nothing wrong with them.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from raven_ppt.contracts.project import Project
from raven_ppt.services.ingest import MATERIALS_FILE

SCHEMA = "raven_ppt.sweep.v1"
SWEEP_FILE = "swept.json"

# Stops at whitespace and at the delimiters a URL is quoted, bracketed or tagged
# with. The trailing characters that survive that -- a markdown link's `)`, the
# full stop that ends the sentence -- come off in `_trimmed`.
_URL = re.compile(r"https?://[^\s<>\"'`\\|]+", re.IGNORECASE)

# Punctuation that ends a sentence rather than a URL. `)` is handled separately:
# a Wikipedia article title carries one legitimately.
_TRAILING = ".,;:!?]}>*_。，；：！？"

# Hosts that appear in materials as declarations rather than as citations. A .docx
# or .pptx read as XML carries a dozen of these, and none of them is a page anyone
# can look at for a figure.
_DECLARED = (
    "w3.org",
    "openxmlformats.org",
    "schemas.microsoft.com",
    "purl.org",
    "ns.adobe.com",
    "xmlns.com",
    "docbook.org",
    "oasis-open.org",
)


@dataclass(frozen=True)
class Swept:
    """One cited URL that was opened, and what came back from it.

    `found` is the load-bearing half. A list of URLs alone is a checkbox, and a
    checkbox is cheaper to tick than the fetch it stands for; "404" or "text only,
    no figures" or "three screenshots, all UI chrome" is a claim about a specific
    page that someone had to open to write.
    """

    url: str
    found: str

    def as_dict(self) -> dict[str, str]:
        return {"url": self.url, "found": self.found}


class SweepError(ValueError):
    """A sweep entry that does not say what a look at a URL produced."""


def sweep_path(project: Project) -> Path:
    return project.state_dir / SWEEP_FILE


def key(url: str) -> str:
    """The form two spellings of one address compare equal in.

    Forgiving on purpose, and in one direction: the cost of calling two spellings
    of one page the same is a URL swept once instead of twice, and the cost of
    calling them different is a refusal the author cannot clear by doing the work.
    So the scheme goes (a materials file writing `http://` and an author fetching
    `https://` reached the same page), `www.` goes, the trailing slash goes, and
    the fragment goes -- none of them addresses a different document.
    """
    parsed = urlsplit(url.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{host}{path}{query}"


def cited(project: Project) -> tuple[str, ...]:
    """Every distinct URL the ingested materials point at, in the order they appear.

    Read off `materials.md` rather than off a record beside it, for the reason
    `_stated_chars` reads the same file: it is what the deck's evidence now says,
    including materials that were replaced by hand.
    """
    return of_text(_text(project.ingest_dir / MATERIALS_FILE))


def of_text(materials: str) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for match in _URL.finditer(materials):
        url = _trimmed(match.group(0))
        if not _is_citation(url):
            continue
        seen.setdefault(key(url), url)
    return tuple(seen.values())


def accounted(project: Project) -> set[str]:
    """The keys of the URLs something has opened: fetched into the deck, or swept."""
    from raven_ppt.services.ingest import sources

    fetched = {key(source.url) for source in sources.held(project) if source.url}
    return fetched | {key(entry.url) for entry in load_sweep(project)}


def read(
    entries: list[dict],
    name: str = "swept",
    *,
    look: Callable[[str], tuple[Picture, ...]] | None = None,
) -> tuple[Swept, ...]:
    """Sweep entries as the author sent them, or a refusal naming what is wrong.

    Refused rather than dropped. An entry silently discarded for having no `found`
    leaves its URL in the outstanding list, which reads to the author as the record
    not having been written at all -- and the next thing it tries is sending the
    same entry again.

    Two kinds of wrong, and the second one costs a request: an entry that says
    nothing came back is checked against the page it names. `look` is that request,
    injectable so a test never makes one.
    """
    swept = []
    for index, entry in enumerate(entries, start=1):
        url = str(entry.get("url") or "").strip()
        found = str(entry.get("found") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise SweepError(
                f"{name}[{index}] names {url!r}, which is not an http(s) URL. Each entry "
                "records one cited page you opened, so its `url` is that page's address as the materials "
                "cite it"
            )
        if not found:
            raise SweepError(
                f"{name}[{index}] names {url} and does not say what came back. `found` is what makes this a "
                "look rather than a claim -- '404', 'text only, no figures', 'three screenshots, all UI "
                "chrome'"
            )
        swept.append(Swept(url=url, found=found))
    refusal = _refuted(tuple(swept), look or _page_pictures)
    if refusal:
        raise SweepError(refusal)
    return tuple(swept)


# How many of the claimed-empty URLs one call opens for itself. The live run that
# prompted this declared thirteen at once, and eight is the point where the check
# still covers a sweep of that size well enough to be worth fearing while costing
# two waves of requests rather than four.
MAX_CHECKED = 8

# Four at a time: eight pages become two waves, and no host sees a burst.
AT_ONCE = 4

# A page that has not handed over its markup in this long is one the author would
# have given up on too, and this runs inside a tool with 60s for everything it does.
PAGE_TIMEOUT_S = 8.0

# Only the markup is wanted. No page needs a megabyte of it to list its <img>, and
# the cap is also what stops a slow drip from holding a worker for the whole budget.
MAX_PAGE_BYTES = 1024 * 1024

MAX_HOPS = 3

# How much of the evidence a refusal spells out. The author needs an address it can
# fetch and a caption to judge it by, not the page's whole image list.
PICTURES_SHOWN = 3

# Below this on either declared side it is a glyph, a spacer or a tracking pixel,
# never a picture worth a slide.
MIN_SIDE_PX = 64

# Parents whose <img> is furniture rather than content. The first four are what the
# image extractor in `raven/agent/tools/web.py` excludes; the rest are this check
# being stricter than it, which is the direction that only ever drops a candidate.
_FURNITURE = ("header", "footer", "nav", "aside", "form", "button", "label")

# Substrings that make an image address furniture whatever it is wrapped in. Every
# token the image extractor uses, plus a handful it does not: over-matching here
# costs a picture this check stays silent about, and under-matching costs a refusal
# the author cannot clear by doing the work.
_NOISE = (
    "logo",
    "icon",
    "avatar",
    "sprite",
    "spacer",
    "pixel",
    "badge",
    "button",
    "banner",
    "favicon",
    "emoji",
    "placeholder",
    "loading",
    "/funders/",
    "/sponsors/",
    "/partners/",
    "/branding/",
    "gravatar",
    "beacon",
    "tracking",
    "1x1",
    "arrow",
    "chevron",
    "bullet",
    "divider",
    "social",
    "profile",
    "hero",
)

# Words that grant a page had pictures on it. Their presence is the whole
# difference between "I looked and there was nothing", which is the claim this
# check can refute, and "I looked, there are three screenshots and none of them
# helps", which is the author's judgement and none of this check's business.
_PICTURE_WORDS = (
    "image",
    "picture",
    "figure",
    "diagram",
    "chart",
    "graph",
    "screenshot",
    "photo",
    "illustration",
    "thumbnail",
    "svg",
    "png",
    "jpg",
)

# The same words survive inside a denial -- "text only, no figures" names one and
# holds none -- so a denial anywhere in the sentence puts the entry back in the
# queue. Which is also why the default below is to check rather than to skip: a
# sentence this cannot read is a sentence something could be hiding behind.
_DENIALS = (
    "no ",
    "not ",
    "none",
    "n/a",
    "without",
    "zero",
    "nothing",
    "lack",
    "absent",
    "empty",
    "only",
    "404",
    "403",
    "410",
    "timeout",
    "timed out",
    "unavailable",
    "unreachable",
    "failed",
    "blocked",
    "paywall",
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}


@dataclass(frozen=True)
class Picture:
    """One content image a page serves, as the refusal needs to describe it."""

    url: str
    caption: str
    where: str


def claims_nothing(found: str) -> bool:
    """Whether this entry says the page held no picture worth taking.

    True by default, and that is the safe direction: an entry this cannot read is
    checked against its page, while an entry that already grants the page has
    pictures is left alone.
    """
    text = found.casefold()
    if any(mark in text for mark in _DENIALS):
        return True
    return not any(word in text for word in _PICTURE_WORDS)


def _refuted(swept: tuple[Swept, ...], look: Callable[[str], tuple[Picture, ...]]) -> str:
    """A refusal for the entries their own page contradicts, or "" for none."""
    claimed = [entry for entry in swept if claims_nothing(entry.found)]
    if not claimed:
        return ""
    # Sampled rather than truncated when there are more than the budget allows. The
    # first N is a rule an author can pad past by listing the page it did not open
    # ninth; a sample of unknown membership has to be true all the way down.
    checked = claimed if len(claimed) <= MAX_CHECKED else random.sample(claimed, MAX_CHECKED)
    with ThreadPoolExecutor(max_workers=AT_ONCE) as pool:
        seen = list(pool.map(lambda entry: _safely(look, entry.url), checked))
    contradicted = [(entry, pictures) for entry, pictures in zip(checked, seen) if pictures]
    return _contradiction(contradicted) if contradicted else ""


def _safely(look: Callable[[str], tuple[Picture, ...]], url: str) -> tuple[Picture, ...]:
    """Nothing rather than an exception, whatever went wrong reaching the page."""
    try:
        return look(url)
    except Exception:  # noqa: BLE001 -- any failure here means "could not tell", never "refuse"
        return ()


def _contradiction(contradicted: list[tuple[Swept, tuple[Picture, ...]]]) -> str:
    lines = [
        "A sweep entry records a look at a cited page. Each of these pages was opened here, and each one "
        "serves a picture the entry says is not on it, so these cannot be recorded as looks:",
        "",
    ]
    for entry, pictures in contradicted:
        lines.append(f"  {entry.url}")
        lines.append(f"    you recorded: {entry.found!r}")
        for picture in pictures[:PICTURES_SHOWN]:
            lines.append(f"    it serves:    {picture.url}")
            detail = f"{picture.where}: {picture.caption}" if picture.caption else picture.where
            lines.append(f"                  {detail}")
        rest = len(pictures) - PICTURES_SHOWN
        if rest > 0:
            lines.append(f"    ... and {rest} more on the same page")
        lines.append("")
    lines.append(
        "Bring the ones you will use in with ppt_fetch(project=..., url=<the picture's own address>): it lands "
        "in this deck's materials, ingest catalogues it under the caption above, and the URL stops being "
        "outstanding. Keep in `swept` only the pages that really held nothing."
    )
    return "\n".join(lines)


def _page_pictures(url: str) -> tuple[Picture, ...]:
    """The content pictures a page serves, or nothing when it cannot be read.

    Nothing is what every failure returns -- refused as a target, moved too many
    times, timed out, not HTML, unparseable. Only a page that was actually read,
    and that holds a picture, contradicts anything.

    `trust_env` is on here where the rest of this package turns it off, and the
    reason the others turn it off does not apply: nothing read here is written
    anywhere. It only decides whether to refuse, and an environment that reaches
    the web through a proxy would otherwise have this check silently pass
    everything.
    """
    from raven.security.network import validate_url_target

    current, markup = url, ""
    for _ in range(MAX_HOPS + 1):
        allowed, _why = validate_url_target(current)
        if not allowed:
            return ()
        with httpx.Client(follow_redirects=False, timeout=PAGE_TIMEOUT_S, trust_env=True) as client:
            with client.stream("GET", current, headers=_HEADERS) as response:
                if response.is_redirect:
                    location = response.headers.get("location") or ""
                    if not location:
                        return ()
                    current = str(response.url.join(location))
                    continue
                if response.status_code >= 400:
                    return ()
                if "html" not in (response.headers.get("content-type") or "").lower():
                    return ()
                markup = _markup(response)
        break
    return _pictures(markup, current) if markup else ()


def _markup(response: httpx.Response) -> str:
    deadline = time.monotonic() + PAGE_TIMEOUT_S
    chunks: list[bytes] = []
    held = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        held += len(chunk)
        if held >= MAX_PAGE_BYTES or time.monotonic() > deadline:
            break
    return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")


def _pictures(markup: str, base: str) -> tuple[Picture, ...]:
    """Every content image in this markup, captioned ones first.

    Stricter than `raven/agent/tools/web.py` on three counts, each of which only
    ever drops a candidate: `og:image` is not read at all, because a share card is
    the site's picture rather than this page's argument; an image declaring a side
    under `MIN_SIDE_PX` is furniture whatever it is called; and `<figure>` gets no
    exemption from the furniture parents. What is left is what that extractor would
    also have shown, which is what makes a refusal here answerable by calling it.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ()
    soup = BeautifulSoup(markup, "html.parser")
    seen: set[str] = set()
    ranked: list[tuple[int, Picture]] = []
    for image in soup.find_all("img"):
        if image.find_parent(_FURNITURE) is not None or _too_small(image):
            continue
        source = str(image.get("src") or image.get("data-src") or "").strip()
        if not source or source.startswith("data:"):
            continue
        absolute = urljoin(base, source)
        if not urlsplit(absolute).scheme.startswith("http") or absolute in seen:
            continue
        if any(token in absolute.lower() for token in _NOISE):
            continue
        seen.add(absolute)
        figure = image.find_parent("figure")
        legend = figure.find("figcaption") if figure is not None else None
        caption = _flat(legend.get_text(" ", strip=True)) if legend is not None else ""
        alt = _flat(str(image.get("alt") or ""))
        where = "figure with caption" if caption else ("figure" if figure is not None else "page image")
        ranked.append((0 if caption else (1 if alt else 2), Picture(absolute, (caption or alt)[:200], where)))
    ranked.sort(key=lambda item: item[0])
    return tuple(picture for _rank, picture in ranked)


def _flat(text: str) -> str:
    """One line of it. A caption wraps in the markup, and the refusal lists one
    picture per line -- a newline through the middle of one reads as a second."""
    return " ".join(text.split())


def _too_small(image) -> bool:
    for side in ("width", "height"):
        declared = str(image.get(side) or "").strip().removesuffix("px")
        if declared.isdigit() and int(declared) < MIN_SIDE_PX:
            return True
    return False


def record(project: Project, swept: tuple[Swept, ...]) -> tuple[Swept, ...]:
    """Merge these into the deck's sweep record and return the whole of it."""
    if not swept:
        return load_sweep(project)
    kept = {key(entry.url): entry for entry in load_sweep(project)}
    for entry in swept:
        kept[key(entry.url)] = entry
    merged = tuple(kept.values())
    write_sweep(merged, sweep_path(project))
    return merged


def load_sweep(project: Project) -> tuple[Swept, ...]:
    """What this deck has swept, or nothing when there is no record or it is unreadable."""
    try:
        raw = json.loads(sweep_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if not isinstance(raw, dict):
        return ()
    return tuple(
        Swept(url=str(entry.get("url") or ""), found=str(entry.get("found") or ""))
        for entry in raw.get("swept") or ()
        if isinstance(entry, dict) and str(entry.get("url") or "").strip()
    )


def write_sweep(swept: tuple[Swept, ...], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "swept": [entry.as_dict() for entry in swept]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _trimmed(url: str) -> str:
    trimmed = url.rstrip(_TRAILING)
    while trimmed.endswith(")") and trimmed.count(")") > trimmed.count("("):
        trimmed = trimmed[:-1].rstrip(_TRAILING)
    return trimmed


def _is_citation(url: str) -> bool:
    host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    if "." not in host or host.endswith("."):
        return False
    return not any(host == declared or host.endswith(f".{declared}") for declared in _DECLARED)


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
