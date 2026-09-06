"""The second reader's findings, carried from one reading to the next until answered.

A reading used to live only in the reply that carried it. The author answered the
entries it chose and the rest were gone with the turn: on one 15-page live run the
reader named a paragraph buried under an illustration on page 14 at the twelfth
build, nothing answered it, and the deck was delivered three builds later with the
paragraph still buried. Nothing in the run ever said "page 14 is still open".

The ledger is that sentence. Every finding a reading reports is an entry with a
status: `open` until a later reading of the same page no longer reports it (`fixed`),
or until the author looks at the page and says why it stays (`dismissed`, with the
reason). The build reply lists what is open, and the list is the same list every
build until it is answered -- which is the whole difference from a reply.

Matching across readings is by page, kind and the words the reader used, because
the reader is a model and never says the same thing twice: "the paragraph under the
whiteboard" and "body copy hidden behind the illustration" are one finding. An
entry matched on a re-reading keeps its id and counts how often it has been seen.

It refuses nothing. The reader's list never blocked a build and the ledger does not
start to; it only keeps the list from disappearing.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from raven_ppt.contracts import Project

LEDGER_FILE = "ledger.json"
SCHEMA = "raven_ppt.review_ledger.v1"

OPEN = "open"
FIXED = "fixed"
DISMISSED = "dismissed"

# How much of the reader's wording two entries must share to be one finding: the
# Jaccard overlap of their word sets, `where` and `what` together. Set from the live
# run's own repeats -- the same page-14 illustration reported as "模板插画压住正文末行"
# and "clip art laid over the body paragraph" shares almost nothing at the word level,
# which is why kind and page carry most of the match and the words only have to agree
# this much.
_SAME_WORDS = 0.2

_WORD = re.compile(r"[\w一-鿿]+")


def ledger_path(deck: Project) -> Path:
    return deck.review_dir / LEDGER_FILE


def load_ledger(deck: Project) -> dict[str, Any]:
    """The ledger, or an empty one -- an unreadable file is an empty ledger, not an error."""
    try:
        held = json.loads(ledger_path(deck).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": SCHEMA, "findings": []}
    if not isinstance(held, dict) or not isinstance(held.get("findings"), list):
        return {"schema": SCHEMA, "findings": []}
    return held


def open_findings(deck: Project) -> list[dict[str, Any]]:
    """Every entry still waiting for an answer, in page order."""
    return sorted(
        (entry for entry in load_ledger(deck)["findings"] if entry.get("status") == OPEN),
        key=lambda entry: (int(entry.get("page") or 0), str(entry.get("id"))),
    )


def record_reading(
    deck: Project,
    read: dict[int, list[dict[str, str]]],
    versions: dict[int, str],
    reading: int,
) -> dict[str, int]:
    """Fold one reading into the ledger: open what is new, keep what repeats, close what is gone.

    `read` holds every page this reading covered, an empty list for a page that came
    back clean -- a clean page closes its open entries, which is what a page that was
    not read must never do. `versions` is the render fingerprint per page, kept on the
    entry so a later reader can tell "fixed" from "the reader changed its mind": an
    entry closed while its page's pixels never moved is the second.
    """
    ledger = load_ledger(deck)
    findings: list[dict[str, Any]] = ledger["findings"]
    opened = kept = fixed = 0
    for page, problems in read.items():
        current = [entry for entry in findings if entry.get("page") == page and entry.get("status") == OPEN]
        version = versions.get(page, "")
        unmatched = list(current)
        for problem in problems:
            match = _closest(problem, unmatched)
            if match is not None:
                unmatched.remove(match)
                match["times_seen"] = int(match.get("times_seen") or 1) + 1
                match["last_seen"] = reading
                match["where"], match["what"], match["fix"] = problem["where"], problem["what"], problem.get("fix", "")
                match["render"] = version
                kept += 1
                continue
            findings.append(
                {
                    "id": _finding_id(page, problem, findings),
                    "page": page,
                    "kind": problem["kind"],
                    "where": problem["where"],
                    "what": problem["what"],
                    "fix": problem.get("fix", ""),
                    "status": OPEN,
                    "first_seen": reading,
                    "last_seen": reading,
                    "times_seen": 1,
                    "render": version,
                }
            )
            opened += 1
        for entry in unmatched:
            entry["status"] = FIXED
            entry["closed_at"] = reading
            entry["render_changed"] = bool(version) and version != entry.get("render", "")
            fixed += 1
    _write(deck, ledger)
    return {"opened": opened, "kept": kept, "fixed": fixed}


# A reason that answers a `figure` entry by saying whose picture it is. The template's
# illustrations are placeholders, and "it came with the template" says nothing about
# whether the picture depicts the page: a live run dismissed every such entry on eight
# pages of an elderly-care deck with these words, and shipped a whiteboard meeting on
# each of them. A reason has to say what the picture shows and why that is the page.
_OWNERSHIP_REASONS = (
    "模板自带",
    "模板原有",
    "模板原型",
    "模板设计",
    "模板插画",
    "沿用模板",
    "保持统一",
    "template's own",
    "template design",
    "part of the template",
    "came with the template",
    "keep the template",
    "template style",
)
_DEPICTS = ("depict", "shows", "画的是", "画面是", "内容是", "表现的是", "与本页", "切题", "相关")

REFUSED_FIGURE_REASON = (
    "a template illustration is a placeholder, so whose picture it is does not answer a figure entry; "
    "say what the picture depicts and why that is this page, or replace it with one that is"
)


def dismiss(deck: Project, verdicts: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    """Close entries the author has looked at and answered; the reason rides on the entry.

    Returns what was dismissed, what could not be found, and what was refused. An id
    that is not open -- already fixed, already dismissed, never issued -- is named back
    rather than silently accepted, because a dismissal of nothing looks like a dismissal.
    A `figure` entry dismissed for being the template's own picture is refused: see
    `_OWNERSHIP_REASONS`.
    """
    ledger = load_ledger(deck)
    by_id = {entry.get("id"): entry for entry in ledger["findings"]}
    done: list[str] = []
    unknown: list[str] = []
    refused: list[str] = []
    for verdict in verdicts:
        ident = str(verdict.get("id") or "").strip()
        entry = by_id.get(ident)
        if entry is None or entry.get("status") != OPEN:
            unknown.append(ident or "?")
            continue
        reason = str(verdict.get("reason") or "").strip()
        if entry.get("kind") == "figure" and _only_ownership(reason):
            refused.append(ident)
            continue
        entry["status"] = DISMISSED
        entry["reason"] = reason
        done.append(ident)
    if done:
        _write(deck, ledger)
    return done, unknown, refused


def _only_ownership(reason: str) -> bool:
    lowered = reason.lower()
    return any(mark in lowered for mark in _OWNERSHIP_REASONS) and not any(mark in lowered for mark in _DEPICTS)


# Which open entries the reply presses on. A reader at low effort writes about three
# entries a page, and on a replayed 15-page deck the ledger held 43 after two readings
# -- most of them oversized_shape and listed, the two kinds that are a reading of taste
# as often as of a defect. Pressing all 43 on the author trades the finish-line problem
# for a never-finishing one, so the reply carries the ones that cost a reader something
# (a claim the page does not make, a figure over the copy, a table nobody can compare,
# a mark that means the wrong thing, a region too full to read) and the ones the reader
# has now said twice, at most three a page; the rest stay in the file, and the count in
# the reply says how many.
PRESSING_KINDS = ("claim", "figure", "too_full", "marks", "table")
PRESSED_PER_PAGE = 3


def pressing(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The open entries the reply puts in front of the author, page order kept."""
    chosen: list[dict[str, Any]] = []
    per_page: dict[int, int] = {}
    ranked = sorted(
        entries,
        key=lambda entry: (
            int(entry.get("page") or 0),
            -int(entry.get("times_seen") or 1),
            entry.get("kind") not in PRESSING_KINDS,
            str(entry.get("id")),
        ),
    )
    for entry in ranked:
        page = int(entry.get("page") or 0)
        urgent = int(entry.get("times_seen") or 1) > 1 or entry.get("kind") in PRESSING_KINDS
        if urgent and per_page.get(page, 0) < PRESSED_PER_PAGE:
            chosen.append(entry)
            per_page[page] = per_page.get(page, 0) + 1
    return chosen


def summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The open list as a reply carries it: the pressing entries per page, and the count of all."""
    pages: dict[str, list[dict[str, Any]]] = {}
    shown = pressing(entries)
    for entry in shown:
        pages.setdefault(str(entry["page"]), []).append(
            {
                "id": entry["id"],
                "kind": entry["kind"],
                "where": entry["where"],
                "what": entry["what"],
                **({"seen": entry["times_seen"]} if int(entry.get("times_seen") or 1) > 1 else {}),
            }
        )
    said: dict[str, Any] = {"count": len(entries), "pages": pages}
    if len(shown) < len(entries):
        # Ids only, so the rest can still be dismissed by id; their words are in the file.
        pressed = {entry["id"] for entry in shown}
        others: dict[str, list[str]] = {}
        for entry in entries:
            if entry["id"] not in pressed:
                others.setdefault(str(entry["page"]), []).append(f"{entry['id']} ({entry['kind']})")
        said["shown"] = len(shown)
        said["others"] = others
        said["rest"] = f"{len(entries) - len(shown)} more in {LEDGER_FILE} under deck/review, none of them pressing"
    return said


def ask(entries: list[dict[str, Any]]) -> str:
    """What the reply says about the open list, once."""
    shown = pressing(entries)
    pages = sorted({int(entry["page"]) for entry in shown}) or sorted({int(entry["page"]) for entry in entries})
    repeated = sum(1 for entry in entries if int(entry.get("times_seen") or 1) > 1)
    said = (
        f"{len(entries)} finding(s) from the second reader are still open"
        + (f", {len(shown)} of them pressing," if len(shown) < len(entries) else "")
        + f" on page(s) {', '.join(str(page) for page in pages)} -- see open_findings. An entry stays open until a "
        "re-reading of its page no longer sees it, or you look at the page and dismiss it by id with what "
        'you saw: ppt_review(project, dismiss=[{"id": "p14-a1b2c3", "reason": "..."}]). A deck '
        "delivered with open entries is a deck whose reader was not answered"
    )
    if repeated:
        said += f"; {repeated} of them have now been reported on more than one reading"
    return said


def _closest(problem: dict[str, str], candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The open entry this problem repeats, or None when it is new to the page."""
    best, score = None, 0.0
    words = _words(problem)
    for entry in candidates:
        if entry.get("kind") != problem.get("kind"):
            continue
        shared = _overlap(words, _words(entry))
        if shared > score:
            best, score = entry, shared
    if best is not None and score >= _SAME_WORDS:
        return best
    # Same kind and the only open entry of that kind on the page: the reader's second
    # wording of one defect, however far the words drift.
    same_kind = [entry for entry in candidates if entry.get("kind") == problem.get("kind")]
    return same_kind[0] if len(same_kind) == 1 else None


def _words(entry: dict[str, Any]) -> set[str]:
    text = f"{entry.get('where', '')} {entry.get('what', '')}".lower()
    found: set[str] = set()
    for token in _WORD.findall(text):
        # CJK runs carry no spaces, so they are matched as overlapping bigrams rather
        # than as one word per run -- otherwise two Chinese sentences share nothing.
        if re.search(r"[一-鿿]", token):
            found.update(token[i : i + 2] for i in range(max(1, len(token) - 1)))
        else:
            found.add(token)
    return found


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _finding_id(page: int, problem: dict[str, str], existing: list[dict[str, Any]]) -> str:
    taken = {entry.get("id") for entry in existing}
    seed = f"{page}|{problem.get('kind')}|{problem.get('where')}|{problem.get('what')}"
    digest = hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()
    for width in range(6, len(digest) + 1):
        ident = f"p{page}-{digest[:width]}"
        if ident not in taken:
            return ident
    return f"p{page}-{digest}-{len(existing)}"


def _write(deck: Project, ledger: dict[str, Any]) -> None:
    """Never fail a reading over the ledger: an unwritable directory costs the record, not the list."""
    try:
        deck.review_dir.mkdir(parents=True, exist_ok=True)
        ledger_path(deck).write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
