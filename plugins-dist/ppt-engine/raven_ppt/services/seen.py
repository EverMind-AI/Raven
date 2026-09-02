"""Which of the deck's pages the author has actually been shown.

Every tool description in this package tells the author to look at every page and
iterate, and none of it was enforceable: the deck is published inside the build
stage before the renders are even fetched, no field recorded a viewing, and a model
that called `ppt_build(slides=[1])` on an eighteen-page deck shipped seventeen pages
nobody had seen. A skill document in every prompt did not stop it either -- the
predecessor's said the same thing and stage skipping is what it got.

What can be recorded is not "the author looked" but "the tool handed this page
over", which is the closest thing that exists and is enough to kill the skip.

The record is keyed on the code that drew each page, not on the page number, and
that is the whole design. A page counts as seen while the block that drew it is the
block that was rendered; edit page 3 and page 3 is unseen again, while the pages you
did not touch stay seen. Keying on the number alone would let one look cover every
later revision, and resetting the record on every build would make a deck longer
than one batch impossible to finish.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

SEEN_FILE = "seen.json"
SCHEMA = "raven_ppt.seen.v1"


def seen_path(project) -> Path:
    return project.state_dir / SEEN_FILE


def blocks_of(script: str, sources) -> dict[int, str]:
    """page -> a fingerprint of the code that drew it.

    Taken from the line spans the build derived, so it is the same notion of "this
    page's code" that the author edits and the renders are matched by.
    """
    lines = script.splitlines(keepends=True)
    fingerprints: dict[int, str] = {}
    for source in sources or ():
        body = "".join(lines[source.first_line : source.last_line])
        fingerprints[source.page] = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return fingerprints


def record(project, shown: dict[int, str]) -> None:
    """Remember that these pages were handed over, as they were then."""
    if not shown:
        return
    known = _load(project)
    known.update({str(page): digest for page, digest in shown.items()})
    path = seen_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": SCHEMA, "pages": known}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def unseen(project, blocks: dict[int, str]) -> tuple[int, ...]:
    """The pages whose current code has never been rendered back to the author."""
    known = _load(project)
    return tuple(page for page, digest in sorted(blocks.items()) if known.get(str(page)) != digest)


def forget(project) -> None:
    seen_path(project).unlink(missing_ok=True)


def _load(project) -> dict[str, str]:
    try:
        raw = json.loads(seen_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pages = raw.get("pages") if isinstance(raw, dict) else None
    return {str(k): str(v) for k, v in (pages or {}).items()} if isinstance(pages, dict) else {}
