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
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from raven.security.trust import unwrap_untrusted
from research_flow.support.process_appendix import _URL_RE as _CITED_URL_RE
from research_flow.support.process_appendix import _clean as _clean_url

_WRITE_TOOL = "write_file"
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

# A file the reply was drafted from shares most of its ``##`` headings with it: on the
# reader-layout product runs a report draft shared 75-100% of them, and a notes file
# saved beside the report shared none. A file below this share is not the report.
_MIN_SHARED_HEADINGS = 0.5


def _result_line(content: str) -> str:
    """The tool's own line: the first of the fenced body, with observer notes after the fence ignored."""
    return str(unwrap_untrusted(content)).split("\n", 1)[0].strip()


def file_digest(path: str) -> str | None:
    """The sha256 of the file's bytes, or None when it cannot be read."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


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
        if message.get("role") != "tool" or message.get("name") not in (_WRITE_TOOL, _EDIT_TOOL):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        hit = _RESULT_PATH_RE.fullmatch(_result_line(content))
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
_LINK_TARGET_RE = re.compile(r"\]\([^)\s]*\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
# CJK unified ideographs and extension A: one character is one unit of a Chinese length.
_CJK_RE = re.compile("[\u3400-\u4dbf\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[.'-][A-Za-z0-9]+)*")


def touched_markdown(messages: Iterable[dict[str, Any]]) -> list[str]:
    """Markdown paths these ``write_file`` / ``edit_file`` results changed, in order, once each."""
    paths: list[str] = []
    for message in messages:
        if message.get("role") != "tool" or message.get("name") not in (_WRITE_TOOL, _EDIT_TOOL):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        hit = _TOUCHED_RE.fullmatch(_result_line(content))
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
            sections.append([raw[3:].strip()[:24], 0, 0])
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


def length_note(path: str) -> str | None:
    """The ``[report length: ...]`` line for one file as it now stands, or None if unreadable."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    c = measure_report(text)
    parts = "; ".join(f"{name} {cjk}/{words}" for name, cjk, words in c["sections"])
    return (
        f"[report length: {path} - total {c['prose_cjk'] + c['table_cjk']} CJK characters and "
        f"{c['prose_words'] + c['table_words']} other words (prose {c['prose_cjk']}/{c['prose_words']}, "
        f"tables {c['table_cjk']}/{c['table_words']}); "
        f"by section, prose and tables, CJK/words: {parts}; link targets not counted above; "
        f"{c['urls']} distinct URLs cited]"
    )


def file_text(reply: str, path: str) -> tuple[str, int]:
    """The reply as the file holds it, and how many lines were left out.

    The contract puts the delivery facts a brief asks back for (the path, counts)
    on the opening blockquote's last line, naming the file. A report that names its
    own path reads as a receipt, and the host strips it by rewriting the report's
    opening, so that line stays in the reply only. Only quote lines naming this
    file are dropped, and never the whole quote: the answer is not a receipt.
    """
    lines = reply.strip().splitlines()
    start = 0
    while start < len(lines) and (not lines[start].strip() or lines[start].startswith("# ")):
        start += 1
    end = start
    while end < len(lines) and lines[end].lstrip().startswith(">"):
        end += 1
    names = (path, Path(path).name)
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
    named = [p for p in paths if p in reply or Path(p).name in reply]
    return named[0] if len(named) == 1 else None


def _headings(text: str) -> set[str]:
    return {line[3:].strip() for line in text.splitlines() if line.startswith("## ")}


def _drafts_reply(path: str, reply_headings: set[str]) -> bool:
    """Whether the file at ``path`` reads as a draft of the reply, by its ``##`` headings."""
    try:
        own = _headings(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False
    return bool(own) and len(own & reply_headings) >= len(own) * _MIN_SHARED_HEADINGS


def _title_line(text: str) -> str | None:
    first = next((line for line in text.splitlines() if line.strip()), "")
    return first if first.startswith("# ") else None


def sync_report_file(paths: list[str], reply: str) -> dict[str, Any]:
    """Write ``reply`` over the turn's report file; the observer payload, or ``{}``.

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
    target = Path(chosen)
    record: dict[str, Any] = {"path": str(target), "files_written": len(paths)}
    text, dropped = file_text(reply, str(target))
    if dropped:
        record["reply_only_lines"] = dropped
    try:
        held_text = target.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        held_text = ""
    title = _title_line(held_text)
    if title and not _title_line(text):
        text = f"{title}\n\n{text}"
        record["title_kept"] = True
    held = len(held_text)
    if len(text) < held * _MIN_REPLY_SHARE:
        record.update(synced=False, reason="reply_shorter_than_file", reply_chars=len(text), file_chars=held)
        return record
    try:
        target.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        record.update(synced=False, reason=f"write_failed:{type(exc).__name__}")
    else:
        record["synced"] = True
    return record


__all__ = [
    "file_digest",
    "file_text",
    "length_note",
    "measure_report",
    "still_held",
    "sync_report_file",
    "touched_markdown",
    "written_markdown",
]
