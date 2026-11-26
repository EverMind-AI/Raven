"""The reader layout's report file: the delivered reply written over the file the turn saved.

A research turn may also save its report with ``write_file``. That call runs before the
draft is reviewed and shaped, and the shape gate, the reviewer and the appendix act on
the reply alone, so the saved file and the reply drift apart - a different structure,
unreviewed text. Under the reader layout the reply already is the form a reader gets, so
at turn end the flow writes it over the report file the turn wrote, and the chat reply
and the file are one text. The research trail, and the delivery line that names the
file, stay on the reply only.

Which file is the report: a markdown file the turn wrote whole, or appended to or
edited when the turn wrote it whole first or it still holds exactly what an earlier
turn of the session left in it (:func:`still_held`) - any other file holds text
someone else put there, a user's log or their own edits between turns - and whose
``##`` headings are mostly the reply's, which a notes file or a source index saved
beside the report does not share. Of several such files the
reply names the report, and when it names none or several every file is left alone.

The path is read from the tools' own result line, which names the path the tool
resolved; the model's argument may be relative to a workspace this plugin cannot see.
Only the first line of the fenced result is read: the tools echo the model's path
argument into their results, so any later line may be text the model chose.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePath
from typing import Any

from raven.security.trust import unwrap_untrusted
from research_flow.support.process_appendix import _URL_RE as _CITED_URL_RE
from research_flow.support.process_appendix import _clean as _clean_url
from research_flow.support.report_markdown import names_sources

WRITE_TOOL = "write_file"
_EDIT_TOOL = "edit_file"

# The success lines of ``WriteFileTool.execute`` and ``EditFileTool.execute``
# (raven/agent/tools/filesystem.py).
_RESULT_PATH_RE = re.compile(
    r"Successfully (?P<verb>wrote|appended) \d+ bytes to (?P<written>.+?\.md)"
    r"|Successfully edited (?P<edited>.+?\.md)"
    r"|File unchanged: (?P<same>.+?\.md) already holds exactly these \d+ bytes, so nothing was written\.",
    re.I,
)

# A reply this much shorter than the file is a note about the file, not the report:
# a brief that asks for "reply with the path and a summary" gets a reply in the
# report's shape, and writing it over the file would replace the report with the
# note. The product run that showed this replied with 47% of the file's length.
_MIN_REPLY_SHARE = 0.8

# What the file is left with, from the text about to be written and the draft it replaces; and counts to record.
Tidy = Callable[[str, "str | None"], "tuple[str, dict[str, int]]"]

# A file the reply was drafted from shares most of its ``##`` headings with it: on the
# reader-layout product runs a report draft shared 75-100% of them, and a notes file
# saved beside the report shared none. A file below this share is not the report.
_MIN_SHARED_HEADINGS = 0.5


def result_line(content: str) -> str:
    """The tool's own line: the first of the fenced body, with observer notes after the fence ignored."""
    return str(unwrap_untrusted(content)).split("\n", 1)[0].strip()


def _read_bytes(path: str) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def read_text(path: str) -> str | None:
    data = _read_bytes(path)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def write_text(path: str | PurePath, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def file_digest(path: str) -> str | None:
    """The sha256 of the file's bytes, or None when it cannot be read."""
    data = _read_bytes(path)
    return None if data is None else hashlib.sha256(data).hexdigest()


def remove_file(path: str) -> bool:
    """Remove the file at ``path``; whether it is gone. Never raises."""
    target = Path(path)
    try:
        target.unlink(missing_ok=True)
    except OSError:
        return False
    return not target.exists()


def still_held(left: Mapping[str, str]) -> dict[str, str]:
    """The entries of ``left`` (path to the digest the session left in it) the file still matches.

    Read at turn start, before the turn's tools run: a file anyone changed since the
    session last wrote it now holds their text too, and stops being the session's.
    """
    return {path: digest for path, digest in left.items() if file_digest(path) == digest}


def written_markdown(messages: Sequence[dict[str, Any]], since: int = 0, held: Iterable[str] = ()) -> list[str]:
    """Markdown paths the turn starting at ``messages[since]`` wrote, oldest write first.

    A file counts once this turn wrote it whole, or when it is in ``held``, the files that
    still hold what an earlier turn of the session left in them (:func:`still_held`): an
    append or an edit to any other file adds to text that was already there, and the
    reply written over it would erase that text. A later turn that only edits the
    report an earlier turn wrote still has that report, unless someone changed it since.
    """
    paths: list[str] = []
    whole: set[str] = set(held)
    for message in messages[since:]:
        if message.get("role") != "tool" or message.get("name") not in (WRITE_TOOL, _EDIT_TOOL):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        hit = _RESULT_PATH_RE.fullmatch(result_line(content))
        if hit is None:
            continue
        path = hit.group("written") or hit.group("edited") or hit.group("same")
        if hit.group("edited") or (hit.group("verb") or "").lower() == "appended":
            if path not in whole:
                continue
        else:
            whole.add(path)
        if path in paths:
            paths.remove(path)
        paths.append(path)
    return paths


# Every success line that leaves a markdown file changed on disk, ``edit_file``'s included.
_TOUCHED_RE = re.compile(
    r"Successfully (?:(?:wrote|appended) \d+ bytes to|edited) (?P<path>.+?\.md)",
    re.I,
)
# Bounded: on a line of ``](`` that never closes, an unbounded target scans the rest of the line each time.
_LINK_TARGET_RE = re.compile(r"\]\([^)\s]{0,2048}\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
# CJK unified ideographs and extension A: one character is one unit of a Chinese length.
_CJK_RE = re.compile("[\u3400-\u4dbf\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[.'-][A-Za-z0-9]+)*")


def touched_markdown(messages: Iterable[dict[str, Any]]) -> list[str]:
    """Markdown paths these ``write_file`` / ``edit_file`` results changed, in order, once each."""
    paths: list[str] = []
    for message in messages:
        if message.get("role") != "tool" or message.get("name") not in (WRITE_TOOL, _EDIT_TOOL):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        hit = _TOUCHED_RE.fullmatch(result_line(content))
        if hit is not None and hit.group("path") not in paths:
            paths.append(hit.group("path"))
    return paths


def measure_report(text: str) -> dict[str, Any]:
    """Characters of CJK and words of everything else, prose and tables apart, links not counted.

    A brief sets its length in the unit of its language and may leave a table or a
    section out of it, so the counts come split both ways and the model applies the
    brief's own rule. ``sections`` is ``(heading, cjk, words)`` per ``##`` part, prose
    and tables together, so a section the length leaves out can be subtracted whole.
    ``urls`` is the number of distinct addresses anywhere in the text: a link-check
    table that lists every one the report cites has exactly that many rows.
    """
    counts: dict[str, Any] = {"prose_cjk": 0, "prose_words": 0, "table_cjk": 0, "table_words": 0}
    sections: list[list[Any]] = [["(opening)", 0, 0]]
    for raw in text.splitlines():
        if raw.startswith("## "):
            sections.append([raw[3:].strip(), 0, 0])
        line = _BARE_URL_RE.sub("", _LINK_TARGET_RE.sub("]", raw))
        cjk = len(_CJK_RE.findall(line))
        words = len(_WORD_RE.findall(_CJK_RE.sub(" ", line)))
        part = "table" if line.lstrip().startswith("|") else "prose"
        counts[f"{part}_cjk"] += cjk
        counts[f"{part}_words"] += words
        sections[-1][1] += cjk
        sections[-1][2] += words
    counts["sections"] = [tuple(row) for row in sections if row[1] or row[2]]
    # Read the way the research trail reads the same reply, so the count the model states
    # and the trail's cited-links line count one set of addresses.
    counts["urls"] = len({_clean_url(m.group(0)) for m in _CITED_URL_RE.finditer(text)})
    return counts


# A length the task sets: "5000-7500 \u5b57", "5k-7.5k\u5b57", "1500 to 2000 words",
# "\u4e0d\u5c11\u4e8e3000\u5b57" (at least), "3000\u5b57\u4ee5\u5185" (at most). One unit
# of length is one CJK character or one other word, the way the count below reads a
# mixed Chinese report. A length the parse cannot place gets no verdict.
_NUMBER = r"(\d+(?:\.\d+)?)\s*([kK\u5343wW\u4e07])?"
_UNIT = r"(?:\u5b57\u7b26|\u5b57|\u8bcd|characters?|chars?|words?)"
_RANGE_RE = re.compile(
    _NUMBER + r"\s*(?:-|~|\uff5e|\u2013|\u2014|to|\u5230|\u81f3)\s*" + _NUMBER + r"\s*" + _UNIT, re.I
)
_AT_LEAST_RE = re.compile(
    r"(?:\u4e0d\u5c11\u4e8e|\u81f3\u5c11|\u4e0d\u4f4e\u4e8e|at least|no fewer than)\s*" + _NUMBER + r"\s*" + _UNIT, re.I
)
_AT_MOST_RE = re.compile(
    r"(?:\u4e0d\u8d85\u8fc7|\u4e0d\u591a\u4e8e|\u6700\u591a|at most|no more than|up to)\s*" + _NUMBER + r"\s*" + _UNIT,
    re.I,
)
_WITHIN_RE = re.compile(_NUMBER + r"\s*" + _UNIT + r"\s*(?:\u4ee5\u5185|\u4ee5\u4e0b|\u5185)", re.I)
_SCALE = {"k": 1000, "\u5343": 1000, "w": 10000, "\u4e07": 10000}
#: The share of a bound that still meets it, as the report clause states it.
LENGTH_TOLERANCE = 0.05


def _amount(number: str, scale: str | None) -> int:
    return round(float(number) * _SCALE.get((scale or "").lower(), 1))


def length_bound(task: str) -> tuple[int | None, int | None] | None:
    """The (floor, ceiling) of length the task sets, either side None when open; None when it sets none."""
    if hit := _RANGE_RE.search(task or ""):
        high = _amount(hit.group(3), hit.group(4))
        low = _amount(hit.group(1), hit.group(2))
        # "5-7.5k": the scale written once, after the second number, covers both.
        if hit.group(2) is None and hit.group(4) and _amount(hit.group(1), hit.group(4)) <= high:
            low = _amount(hit.group(1), hit.group(4))
        return (low, high) if 0 < low <= high else None
    if hit := _AT_LEAST_RE.search(task or ""):
        return _amount(hit.group(1), hit.group(2)), None
    if hit := _AT_MOST_RE.search(task or "") or _WITHIN_RE.search(task or ""):
        return None, _amount(hit.group(1), hit.group(2))
    return None


#: The heading of a section that shows the links were checked. The report clause leaves
#: it out of the task's length, as it does the list of sources.
_LINK_CHECK_RE = re.compile(
    r"(?:\b(?:links?|urls?)\b|(?<!\u4f9b\u5e94)\u94fe\u63a5(?!\u53e3)).{0,8}(?:check|verif|status|\u6838\u9a8c|\u9a8c\u8bc1|\u68c0\u67e5|\u6821\u9a8c|\u72b6\u6001)"
    r"|(?:\u6838\u9a8c|\u9a8c\u8bc1|\u68c0\u67e5|\u6821\u9a8c)\u7684?\u94fe\u63a5(?!\u53e3)",
    re.I,
)


def body_length(counts: Mapping[str, Any]) -> int:
    """CJK characters plus other words, the list of sources and a link-check section left out."""
    return sum(
        cjk + words
        for name, cjk, words in counts["sections"]
        if not names_sources(name) and not _LINK_CHECK_RE.search(name)
    )


def length_verdict(total: int, bound: tuple[int | None, int | None]) -> str:
    """Where ``total`` stands against the task's bound, a twentieth either side counted as meeting it."""
    low, high = bound
    asked = f"{low}-{high}" if low and high else f"at least {low}" if low else f"at most {high}"
    if low and total < low * (1 - LENGTH_TOLERANCE):
        return f"{total} against the task's {asked}: {low - total} under its floor, so the body needs more"
    if high and total > high * (1 + LENGTH_TOLERANCE):
        return f"{total} against the task's {asked}: {total - high} over its ceiling, so trim that much"
    return (
        f"{total} against the task's {asked}: within it (a twentieth either side counts), so the "
        "length is done - do not lengthen or trim the report for it"
    )


def length_note(path: str, bound: tuple[int | None, int | None] | None = None) -> str | None:
    """The ``[report length: ...]`` line for one file as it now stands, or None if unreadable.

    With the task's ``bound`` it also says whether the body, counted as CJK characters
    plus other words without the list of sources or a link-check section, meets it.
    """
    text = read_text(path)
    if text is None:
        return None
    c = measure_report(text)
    parts = "; ".join(f"{name[:24]} {cjk}/{words}" for name, cjk, words in c["sections"])
    verdict = (
        f"; length, CJK characters plus other words, sources list and link-check section left out: {length_verdict(body_length(c), bound)}"
        if bound
        else ""
    )
    return (
        f"[report length: {path} - total {c['prose_cjk'] + c['table_cjk']} CJK characters and "
        f"{c['prose_words'] + c['table_words']} other words (prose {c['prose_cjk']}/{c['prose_words']}, "
        f"tables {c['table_cjk']}/{c['table_words']}); "
        f"by section, prose and tables, CJK/words: {parts}; link targets not counted above; "
        f"{c['urls']} distinct URLs cited{verdict}]"
    )


_FENCE_MARKS = ("```", "~~~")
_SECTION_HEADING_RE = re.compile(r"#{2,6}[ \t]")


def answer_span(lines: Sequence[str]) -> tuple[int, int]:
    """Lines ``[start, end)`` of the answer: the first blockquote above the first ``##`` heading.

    The contract opens the reply with the answer. A model still puts a line of its own
    before it now and then (what it saved, "here is the report"), and the blockquote
    under that line is the answer all the same. Both numbers are equal when the reply
    has no such blockquote.
    """
    fenced = False
    for start, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(_FENCE_MARKS):
            fenced = not fenced
        if fenced:
            continue
        if _SECTION_HEADING_RE.match(line):
            break
        if stripped.startswith(">"):
            end = start
            while end < len(lines) and lines[end].lstrip().startswith(">"):
                end += 1
            return start, end
    return 0, 0


def file_text(reply: str, path: str) -> tuple[str, int]:
    """The reply as the file holds it, and how many lines were left out.

    The contract puts the delivery facts a brief asks back for (the path, counts)
    on the answer blockquote's last line, naming the file. A report that names its
    own path reads as a receipt, and the host strips it by rewriting the report's
    opening, so that line stays in the reply only. Only quote lines naming this
    file are dropped, and never the whole quote: the answer is not a receipt.
    """
    lines = reply.strip().splitlines()
    start, end = answer_span(lines)
    names = (path, PurePath(path).name)
    quote = [line for line in lines[start:end] if not any(name in line for name in names)]
    while quote and quote[-1].strip() == ">":
        quote.pop()
    dropped = (end - start) - len(quote)
    if not quote or not dropped:
        return reply.strip(), 0
    return "\n".join(lines[:start] + quote + lines[end:]).strip(), dropped


def _report_target(paths: list[str], reply: str) -> str | None:
    """The one file the reply is the report for, or None when that is ambiguous.

    Write order cannot decide it: a report followed by a notes file would put the
    notes last, and the longer report reply would replace them while the report
    draft stayed stale.
    """
    if len(paths) == 1:
        return paths[0]
    named = [p for p in paths if p in reply or PurePath(p).name in reply]
    return named[0] if len(named) == 1 else None


def _headings(text: str) -> set[str]:
    return {line[3:].strip() for line in text.splitlines() if line.startswith("## ")}


def _drafts_reply(path: str, reply_headings: set[str]) -> bool:
    """Whether the file at ``path`` reads as a draft of the reply, by its ``##`` headings."""
    text = read_text(path)
    if text is None:
        return False
    own = _headings(text)
    return bool(own) and len(own & reply_headings) >= len(own) * _MIN_SHARED_HEADINGS


def title_line(text: str) -> str | None:
    first = next((line for line in text.splitlines() if line.strip()), "")
    return first if first.startswith("# ") else None


def sync_report_file(paths: list[str], reply: str, *, tidy: Tidy | None = None) -> dict[str, Any]:
    """Write ``reply`` over the turn's report file; the observer payload, or ``{}``.

    ``tidy`` is applied to the text the file is left with, given the draft it replaces,
    and its counts are recorded. A file that stays because the reply is the shorter
    telling (``kept``) is tidied too, so whatever is rendered from the file afterwards
    reads the same as the file.

    Never raises: the reply has already gone out, and a file that could not be
    rewritten is recorded rather than turned into a failed turn. Only a file that
    reads as a draft of the reply (:func:`_drafts_reply`) can be the report, and of
    several, :func:`_report_target` picks; no file is touched when it cannot tell. A
    reply well short of the file is left out of it, so a summary never replaces the
    report, and the delivery line naming the file stays in the reply
    (:func:`file_text`). A ``#`` title the file opens with is kept when the reply has
    none: the reply omits it as chat, and the file is still a document.
    """
    if not paths:
        return {}
    reply_headings = _headings(reply)
    drafts = [p for p in paths if _drafts_reply(p, reply_headings)]
    if not drafts:
        return {"files_written": len(paths), "synced": False, "reason": "no_report_draft"}
    chosen = _report_target(drafts, reply)
    if chosen is None:
        return {"files_written": len(paths), "synced": False, "reason": "report_file_ambiguous"}
    target = PurePath(chosen)
    record: dict[str, Any] = {"path": str(target), "files_written": len(paths)}
    text, dropped = file_text(reply, str(target))
    if dropped:
        record["reply_only_lines"] = dropped
    # What the reply itself says, before ``tidy``: the source list tidy adds is the
    # page's, and counting it would let a summary with many links pass for the report.
    reply_chars = len(text)
    held_text = (read_text(chosen) or "").strip()
    counts: dict[str, int] = {}
    if tidy is not None:
        text, counts = tidy(text, held_text)
    title = title_line(held_text)
    if title and not title_line(text):
        text = f"{title}\n\n{text}"
        reply_chars += len(title) + 2
        record["title_kept"] = True
    held = len(held_text)
    if reply_chars < held * _MIN_REPLY_SHARE:
        record.update(synced=False, reason="reply_shorter_than_file", reply_chars=reply_chars, file_chars=held)
        record["kept"] = True
        if tidy is not None:
            kept, _ = file_text(held_text, str(target))
            kept, counts = tidy(kept, None)
            record.update(counts)
            if kept != held_text:
                try:
                    write_text(target, kept + "\n")
                except OSError as exc:
                    record["tidy_failed"] = type(exc).__name__
        return record
    try:
        write_text(target, text + "\n")
    except OSError as exc:
        record.update(synced=False, reason=f"write_failed:{type(exc).__name__}")
    else:
        record.update(counts)
        record["synced"] = True
    return record


__all__ = [
    "WRITE_TOOL",
    "Tidy",
    "answer_span",
    "file_digest",
    "file_text",
    "length_note",
    "measure_report",
    "read_text",
    "remove_file",
    "result_line",
    "still_held",
    "sync_report_file",
    "title_line",
    "touched_markdown",
    "write_text",
    "written_markdown",
]
