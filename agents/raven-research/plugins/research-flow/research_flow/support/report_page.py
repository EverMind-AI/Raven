"""The report's reading page as a file: where it goes, and the line that names it.

A research turn's reply is the report, in markdown, and the conversation shows it as
markdown. The reading page is the same report as one HTML file a person can open, keep
or forward. It is written at turn end.

The page and the markdown report file hold the same text. When the turn has a report
file, the page shows what that file holds at turn end, whatever its shape: the reply
written over it, or the file itself when it stayed because the reply is the shorter
telling. Both are tidied the same way first (:func:`tidy`). Without a report file the
page shows the reply, tidied. The research trail is on neither.

Where the page goes: beside the markdown report file when the turn wrote one (same
name, ``.html``), else in the turn's working directory under a name taken from the
report's own text, so a turn that runs again writes the same file. A file already
standing under the page's name is written over only when it is the report's own: a
page this module wrote, or an HTML twin of the report that this turn wrote itself. The
contract tells the model to write no such twin; a turn asked for "the report as HTML"
can still write one by hand, unreviewed and in a look of its own, and the page takes
its place the way the reply takes the markdown file's. Any other file of that name is
someone else's and is left alone.

What the file and the page leave out of the reply: the lines of the answer that name
the report's files, and a line the reply puts before its answer when the turn's own
draft of the report does not hold it. The model wrote that draft as the report, so a
line it holds nowhere is talk to whoever receives the reply ("both files are saved,
here is the report"), not part of the report. With no draft to compare, or when the
draft holds the line, it is kept. A paragraph that runs its steps together is written
as a list in both, and a numbered list that stands directly under a label line gets the
blank line that makes it a list.

Both close with the report's list of sources, after its limits
(``report_html.close_with_sources``): a report that wrote none gets the list the page
numbers, so the file holds it too.

With ``remove_markdown`` on, the page is the one report file the turn leaves. After the
page is written from the markdown report file, the flow removes that file, and the line
that names the page says so (:func:`page_line`). The reply's own text is not rewritten:
it has gone out as deltas by then, and only a line added to its end reaches a streaming
surface. A file that could not be removed stays.

Nothing here raises: the reply has already gone out, and a page that could not be
written is recorded, not turned into a failed turn.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent import workdir
from research_flow.gates.ask_user import scaffold_language
from research_flow.support.report_file import (
    WRITE_TOOL,
    answer_span,
    file_text,
    read_text,
    result_line,
    write_text,
)
from research_flow.support.report_highlights import select_highlights
from research_flow.support.report_html import (
    GENERATOR,
    Highlights,
    close_with_sources,
    fixed_closing,
    render_page,
)
from research_flow.support.report_markdown import (
    list_steps,
    parse_blocks,
    part_numbered_lists,
    plain_units,
    split_chunks,
)

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider
    from research_flow.config import ReportPageConfig

_LINE_LEAD = "Report page (HTML file for the reader): "

# The success line of ``WriteFileTool.execute`` for a file written whole (raven/agent/tools/filesystem.py).
_WROTE_PAGE_RE = re.compile(r"Successfully wrote \d+ bytes to (?P<path>.+?\.html?)", re.I)
_SPACE_RE = re.compile(r"\s+")


def page_line(path: str, replaces: str | None = None) -> str:
    """The line that names the page to whoever receives the reply.

    A dispatched research turn hands its reply to the agent that dispatched it, and
    that agent delivers files to the person. It can deliver only a file it is told of.
    ``replaces`` is the markdown report file the flow removed: the reply above may still
    name it, so the line says that it is gone.
    """
    line = f"{_LINE_LEAD}`{path}`"
    return f"{line}. It replaces `{replaces}`, which is removed." if replaces else line


def written_pages(messages: Sequence[dict[str, Any]], since: int = 0) -> list[str]:
    """HTML paths the turn starting at ``messages[since]`` wrote whole, once each, in order.

    Read off the tool's own result line, as the report file's path is: the line names
    the path the tool resolved, and only its first line is the tool's.
    """
    paths: list[str] = []
    for message in messages[since:]:
        if message.get("role") != "tool" or message.get("name") != WRITE_TOOL:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        hit = _WROTE_PAGE_RE.fullmatch(result_line(content))
        if hit is not None and hit.group("path") not in paths:
            paths.append(hit.group("path"))
    return paths


def _today() -> str:
    return date.today().isoformat()


def _above_answer(lines: Sequence[str]) -> int:
    """How many lines stand above the answer, or 0 when none carries text or the reply has no answer."""
    start, end = answer_span(lines)
    spoken = any(line.strip() and not line.startswith("# ") for line in lines[:start])
    return start if end > start and spoken else 0


def _squeeze(text: str) -> str:
    return _SPACE_RE.sub("", text)


def _without_talk(report: str, draft: str | None) -> tuple[str, int]:
    """``report`` without the paragraphs above its answer that ``draft`` does not hold; the lines left out."""
    lines = report.strip().splitlines()
    above = _above_answer(lines)
    if not above or not draft:
        return report, 0
    held = [_squeeze(unit) for unit in plain_units(parse_blocks(draft))]
    kept: list[str] = []
    dropped = 0
    for chunk in split_chunks(lines[:above]):
        units = [_squeeze(unit) for unit in plain_units(parse_blocks("\n".join(chunk)))]
        if chunk[0].startswith("# ") or all(any(unit in text for text in held) for unit in units):
            kept.extend([*chunk, ""])
        else:
            dropped += len(chunk)
    return "\n".join([*kept, *lines[above:]]).strip(), dropped


def tidy(text: str, draft: str | None, limits: bool = True) -> tuple[str, dict[str, int]]:
    """``text`` as the report's file and its page hold it, and what was changed.

    ``draft`` is the report file as it stood before ``text`` replaces it, or None.
    Without ``limits`` a section under the fixed limits heading is left out.
    """
    language = scaffold_language(text)
    text, talk = _without_talk(text, draft)
    text, lists = list_steps(text)
    text, parted = part_numbered_lists(text)
    text, fixed = fixed_closing(text, language, limits)
    text, closing = close_with_sources(text, language)
    counts: dict[str, int] = {}
    if talk:
        counts["talk_lines"] = talk
    if lists:
        counts["step_lists"] = lists
    if parted:
        counts["lists_parted"] = parted
    counts.update(fixed)
    counts.update(closing)
    return text, counts


def _named(report: str) -> str:
    return f"research-report-{hashlib.sha256(report.encode('utf-8')).hexdigest()[:8]}.html"


class ReportPage:
    """Writes the reading page of one flow configuration's reports."""

    def __init__(
        self,
        cfg: ReportPageConfig,
        provider: LLMProvider | None = None,
        model: str | None = None,
        *,
        limits: bool = True,
    ) -> None:
        self._cfg = cfg
        self._provider = provider
        self._model = model or cfg.highlights_model
        self._limits = limits

    @property
    def removes_file(self) -> bool:
        """Whether the markdown report file a page was written from is removed."""
        return self._cfg.remove_markdown

    def _target(self, report: str, beside: str | None, own: Iterable[str]) -> PurePath | None:
        """The path the page is written to, or None when there is nowhere to write it."""
        if beside:
            sibling = PurePath(beside).with_suffix(".html")
            # A file that is there but cannot be read (binary, another encoding) is not
            # the page's to write over: only an absent file, a page this module wrote,
            # or an HTML twin the turn wrote itself is.
            exists = Path(sibling).exists()
            held = read_text(str(sibling)) if exists else None
            if not exists or (held is not None and GENERATOR in held) or str(sibling) in set(own):
                return sibling
            return sibling.with_name(_named(report))
        bound = workdir.current()
        return PurePath(bound) / _named(report) if bound is not None else None

    async def write(
        self,
        report: str,
        *,
        beside: str | None = None,
        own: Iterable[str] = (),
        task: str = "",
        day: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """Render ``report`` and write its page; the page's path, and the observer record.

        ``report`` is the reply a reader sees, without the trail. ``beside`` is the
        turn's markdown report file as the sync left it, when there is one: the page
        shows what it holds. ``own`` are the HTML files this turn wrote itself
        (:func:`written_pages`). ``task`` is the name the turn's dispatcher gave its
        task: the page's title when neither the reply nor the file has one. The path is None when no page was written, and the
        record says why.
        """
        try:
            target = self._target(report, beside, own)
            if target is None:
                return None, {"written": False, "reason": "no_working_directory"}
            record: dict[str, Any] = {"path": str(target)}
            held = (read_text(beside) or "").strip() if beside else ""
            if held:
                report = held
                record["follows"] = "file"
            else:
                report, counts = tidy(report, None, self._limits)
                record.update(counts)
                # The reply stands in for a report file: like one, it leaves out the line that names it.
                report, _ = file_text(report, str(target))
            if str(target) in set(own):
                record["replaced_own_draft"] = True
            highlights = Highlights()
            if self._cfg.highlights and self._provider is not None:
                highlights, record["highlights"] = await select_highlights(
                    self._provider,
                    report,
                    model=self._model,
                    max_tokens=self._cfg.highlights_max_tokens,
                    reasoning_effort=self._cfg.highlights_reasoning_effort,
                    timeout_seconds=self._cfg.highlights_timeout_seconds,
                )
            page = render_page(
                report,
                title=" ".join(task.split()),
                day=day if day is not None else _today(),
                language=scaffold_language(report),
                highlights=highlights,
                show_limits=self._cfg.show_limits,
                limits_last=self._limits,
            )
            write_text(target, page)
        except OSError as exc:
            return None, {"written": False, "reason": f"write_failed:{type(exc).__name__}"}
        except Exception as exc:  # noqa: BLE001 - a page that cannot be rendered never fails the turn
            logger.opt(exception=True).warning("report-page: the page could not be rendered")
            return None, {"written": False, "reason": f"render_failed:{type(exc).__name__}"}
        record.update(written=True, bytes=len(page.encode("utf-8")))
        return str(target), record


__all__ = ["ReportPage", "page_line", "tidy", "written_pages"]
