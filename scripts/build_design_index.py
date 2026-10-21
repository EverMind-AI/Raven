#!/usr/bin/env python3
"""Build the design index: a machine-readable inventory of the repo's design documents.

The repo's own design backlog is the plan's largest asset and its most
exploited one. ``docs/specs/`` and ``docs/plans/`` hold the specifications the
runtime was built from, and several of them record a decision *not* to act,
with the measurement behind it. A contributor who does not know that will
re-propose what was already declined, and a reader who cannot tell a draft
from a shipped design will cite a proposal as if it were the design.

This script makes that inventory explicit and machine-readable, so the
question "does this already exist?" has an answer that does not require
reading a hundred documents to find.

What it reads: the title, the date, the status, the baseline commit, the
supersede relation and the language of every document, from the conventions
the documents already use rather than from a form to be filled in.

What it does **not** do: decide. A document whose status it cannot read is
recorded as ``unknown``, not guessed at, and a Chinese document with no
English rendering is recorded as having none. The index's job is to be
authoritative about what it knows and honest about what it does not.

The output is deterministic: no timestamp, sorted by id. ``tests/test_design_index.py``
regenerates it and compares, so a new document without an index entry fails
CI rather than quietly joining the backlog.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS = ROOT / "docs" / "specs"
PLANS = ROOT / "docs" / "plans"
INDEX_JSON = ROOT / "docs" / "design-index.json"
INDEX_MD = ROOT / "docs" / "design-index.md"

#: How much of a document's opening is scanned for its metadata. The status
#: and baseline live in the first screen of every document that has them.
HEAD_CHARS = 4000

#: A document is treated as Chinese when this share of its characters is
#: CJK. Below it, the document is English; above it, the document is a
#: Chinese design that has no English rendering.
CJK_SHARE = 0.2

#: The Chinese words and punctuation the documents' headers are written in,
#: held as escapes so this file carries no non-English source (AGENTS.md
#: section 1.3). Each is named for what it means.
_STATUS_ZH = "\u72b6\u6001"  # "status"
_BASELINE_ZH = "\u57fa\u7ebf"  # "baseline"
_RESEARCH_BASELINE_ZH = "\u8c03\u7814\u57fa\u7ebf"  # "research baseline"
_SUPERSEDES_ZH = "\u53d6\u4ee3"  # "supersedes"
_EXTENDED_ZH = "\u5df2\u88ab\u6269\u5c55"  # "has been extended"
_SUPERSEDED_ZH = "\u5df2\u88ab\u53d6\u4ee3"  # "has been superseded"
_REFUSED_ZH = "\u62d2\u7edd"  # "refuse"
_NOT_DOING_ZH = "\u4e0d\u505a"  # "not to do"
_DESIGN_DRAFT_ZH = "\u8bbe\u8ba1\u7a3f"  # "design draft"
_IMPLEMENTED_ZH = "\u5df2\u5b9e\u73b0"  # "implemented"
_LANDED_ZH = "\u843d\u5730"  # "landed"
_FINAL_ZH = "\u5df2\u5b9a\u7a3f"  # "finalized"
_FULLWIDTH_COLON = "\uff1a"  # ":"
_FULLWIDTH_OPEN = "\uff08"  # "("
_FULLWIDTH_CLOSE = "\uff09"  # ")"

#: The punctuation the markdown view is written in, held as escapes for the
#: same reason.
_EM_DASH = "\u2014"  # an em dash
_MIDDLE_DOT = "\u00b7"  # a middle dot

_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_STATUS = re.compile(
    rf"^\s*(?:>\s*)?(?:\*\*)?(?:Status|{_STATUS_ZH})(?:\*\*)?\s*[:{_FULLWIDTH_COLON}]\s*(.+?)\s*$",
    re.M,
)
_BASE = re.compile(
    rf"^\s*(?:>\s*)?(?:\*\*)?(?:Base|{_BASELINE_ZH}|{_RESEARCH_BASELINE_ZH})(?:\*\*)?\s*"
    rf"[:{_FULLWIDTH_COLON}]\s*(.+?)\s*$",
    re.M,
)
_SUPERSEDES = re.compile(
    rf"(?:\*\*{_SUPERSEDES_ZH}\*\*|Supersedes?|{_SUPERSEDES_ZH})\s*[:{_FULLWIDTH_COLON}]?"
    rf"\s*`?([0-9A-Za-z._/-]+\.md)`?"
)
_EXTENDED = re.compile(
    rf"{_EXTENDED_ZH}[{_FULLWIDTH_OPEN}(]\s*(\d{{4}}-\d{{2}}-\d{{2}})\s*[{_FULLWIDTH_CLOSE})]"
)
_LINK_MD = re.compile(r"\]\(([0-9A-Za-z._/-]+\.md)\)")
_IN_PROGRESS = re.compile(r"\*\*\s*(?=\w)")
#: A line that opens a *new* field. A status that continues onto the next
#: line must stop there, or it swallows the fields that follow it.
_NEW_FIELD = re.compile(rf"^\s*(?:\*\*)?[^*]{{1,40}}(?:\*\*)?\s*[:{_FULLWIDTH_COLON}]")
_CJK = re.compile(r"[\u4e00-\u9fff]")

#: The decision buckets. A status line's text decides which bucket a document
#: is in; the bucket is what a reader acts on, and the raw status is kept
#: beside it so the bucketing can be checked by a human.
#: The order matters, and it is the order of specificity rather than of
#: importance. "approved, not yet implemented" is a design, so the needles for
#: a design are read before the needles for an implementation; a Chinese status
#: that reports the implementation as done and not yet reviewed is an
#: implementation, so a review state is not a design state.
_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("declined", ("declin", "not to act", _REFUSED_ZH, _NOT_DOING_ZH, "not as a plan", "inventory, not")),
    ("extended", (_EXTENDED_ZH, "extended")),
    ("superseded", (_SUPERSEDED_ZH, "superseded")),
    (
        "design",
        (
            "not yet implemented",
            "not implemented",
            "not started",
            "sign-off",
            _DESIGN_DRAFT_ZH,
            "draft",
            "proposed",
            "proposal",
            "design",
        ),
    ),
    (
        "implemented",
        (
            "implement",
            _IMPLEMENTED_ZH,
            _LANDED_ZH,
            "executed",
            "complete",
            "passed",
            _FINAL_ZH,
            "built",
            "accepted",
        ),
    ),
    ("plan", ("implementation plan", "plan")),
)


@dataclass
class Document:
    """One design document, as the index records it."""

    id: str
    path: str
    title: str
    date: str
    language: str
    status: str
    decision: str
    baseline: str
    supersedes: list[str] = field(default_factory=list)
    superseded_by: list[str] = field(default_factory=list)
    bytes: int = 0
    cjk_share: float = 0.0
    has_english_rendering: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "date": self.date,
            "language": self.language,
            "status": self.status,
            "decision": self.decision,
            "baseline": self.baseline,
            "supersedes": sorted(self.supersedes),
            "superseded_by": sorted(self.superseded_by),
            "bytes": self.bytes,
            "cjk_share": round(self.cjk_share, 4),
            "has_english_rendering": self.has_english_rendering,
        }


def _first_line(text: str, pattern: re.Pattern[str]) -> str:
    """The first match of a header pattern, with its continuation lines folded in.

    A status that runs onto the next line is one status, not two: the
    documents write ``Status: fully executed. This document is the
    historical planning record,`` and then continue the sentence on a line
    that starts with ``**``.
    """
    match = pattern.search(text)
    if not match:
        return ""
    value = match.group(1).strip()
    rest = text[match.end() : match.end() + 400]
    for line in rest.splitlines()[1:]:
        if not _IN_PROGRESS.match(line):
            break
        if _NEW_FIELD.match(line):
            # The next line opens another field. This one said what it had to
            # say; carrying on would swallow the field that follows.
            break
        value += " " + line.strip().lstrip("*").strip()
    return value.strip()


def _title(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _bucket(status: str, title: str) -> str:
    """Which decision bucket a document is in.

    The status text decides it. The title is consulted only when there is no
    status, and only to separate a plan from a design -- the plan family is
    titled "Implementation Plan" and says nothing else in its header, so
    reading the title as the status would file every plan as implemented.
    """
    if status.strip():
        haystack = status.lower()
        for name, needles in _BUCKETS:
            if any(needle in haystack for needle in needles):
                return name
        return "unknown"
    lowered = title.lower()
    if "implementation plan" in lowered:
        return "plan"
    if "design" in lowered or "proposal" in lowered:
        return "design"
    return "unknown"


def _read(path: Path) -> Document:
    text = path.read_text(encoding="utf-8", errors="replace")
    head = text[:HEAD_CHARS]
    cjk = len(_CJK.findall(text))
    share = cjk / max(1, len(text))

    status = _first_line(head, _STATUS)
    baseline_text = _first_line(head, _BASE)
    if not baseline_text:
        # Some documents name their baseline in prose: "Line numbers are
        # against `main` at `829eb28e1`."
        sha = _SHA.search(head)
        if sha and re.search(r"against|line numbers|baseline", head, re.I):
            baseline_text = sha.group(0)
    # The index records the commit, not the sentence around it. One document
    # writes a whole paragraph in its baseline field; the commit is the part
    # a reader can check, and the prose is still in the document.
    baseline = ""
    sha = _SHA.search(baseline_text)
    if sha:
        baseline = sha.group(0)

    supersedes: list[str] = []
    extended_by: list[str] = []
    found = _SUPERSEDES.search(head)
    if found:
        supersedes.append(found.group(1))
    if _EXTENDED.search(head):
        # The Chinese marker for *extended* means *this* document was extended
        # *by* the one it links to. The relation runs the other way from the
        # marker for *supersedes*.
        for name in _LINK_MD.findall(head):
            extended_by.append(name)
            break

    title = _title(text)
    doc_date = ""
    date_match = _DATE.match(path.name)
    if date_match:
        doc_date = date_match.group(1)
    else:
        inline = re.search(
            rf"^\s*\*{{0,2}}Date\s*[:{_FULLWIDTH_COLON}]\*{{0,2}}\s*(\d{{4}}-\d{{2}}-\d{{2}})",
            head,
            re.M,
        )
        doc_date = inline.group(1) if inline else ""

    return Document(
        id=path.stem,
        path=path.relative_to(ROOT).as_posix(),
        title=title,
        date=doc_date,
        language="zh" if share > CJK_SHARE else "en",
        status=status,
        decision=_bucket(status, title),
        baseline=baseline,
        supersedes=[Path(name).stem for name in supersedes if name.endswith(".md")],
        superseded_by=[Path(name).stem for name in extended_by if name.endswith(".md")],
        bytes=len(text.encode("utf-8")),
        cjk_share=share,
    )


def build() -> list[Document]:
    """Every design document, with the supersede relation resolved both ways."""
    paths = sorted(SPECS.glob("*.md")) + sorted(PLANS.glob("*.md"))
    documents = [_read(path) for path in paths]
    by_id = {doc.id: doc for doc in documents}

    for doc in documents:
        for name in doc.supersedes:
            target = by_id.get(Path(name).stem)
            if target is not None and doc.id not in target.superseded_by:
                target.superseded_by.append(doc.id)
        for name in doc.superseded_by:
            target = by_id.get(Path(name).stem)
            if target is not None and doc.id not in target.supersedes:
                target.supersedes.append(doc.id)
        doc.supersedes = [name for name in doc.supersedes if Path(name).stem in by_id]
        # A Chinese design has an English rendering only if a twin exists.
        # None do today, and the index says so rather than implying otherwise.
        doc.has_english_rendering = doc.language == "en" or (ROOT / doc.path).with_suffix(".en.md").exists()
    return documents


def render_markdown(documents: list[Document]) -> str:
    """The human view of the same inventory."""
    counts: dict[str, int] = {}
    for doc in documents:
        counts[doc.decision] = counts.get(doc.decision, 0) + 1
    languages: dict[str, int] = {}
    for doc in documents:
        languages[doc.language] = languages.get(doc.language, 0) + 1

    lines = [
        "# Design index",
        "",
        "Generated by `scripts/build_design_index.py` from the headers of the documents",
        "themselves. Do not edit this file by hand: `tests/test_design_index.py`",
        "regenerates it and fails when it is out of date, so a new design document",
        "cannot join the backlog without an index entry.",
        "",
        f"**{len(documents)} documents** {_EM_DASH} "
        + ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
        + f" {_MIDDLE_DOT} "
        + ", ".join(f"{count} {name}" for name, count in sorted(languages.items())),
        "",
        "| Date | Document | Decision | Status | Baseline | Supersedes |",
        "|---|---|---|---|---|---|",
    ]
    for doc in sorted(documents, key=lambda item: (item.date, item.id)):
        status = doc.status.replace("|", "\\|")
        if len(status) > 90:
            status = status[:87] + "..."
        supersedes = ", ".join(Path(name).stem for name in doc.supersedes) or _EM_DASH
        lines.append(
            f"| {doc.date or _EM_DASH} | [{doc.title or doc.id}]({doc.path}) | {doc.decision} | {status} | "
            f"{doc.baseline or _EM_DASH} | {supersedes} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    documents = build()
    payload = {
        "schema": "raven.design-index/1",
        "generated_by": "scripts/build_design_index.py",
        "count": len(documents),
        "documents": [doc.to_dict() for doc in sorted(documents, key=lambda item: item.id)],
    }
    # The index is read by machines as well as by people, and it is committed
    # to the repo, so it is written as ASCII with the non-ASCII text escaped.
    INDEX_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    INDEX_MD.write_text(render_markdown(documents), encoding="utf-8")
    print(f"indexed {len(documents)} design documents -> {INDEX_JSON.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
