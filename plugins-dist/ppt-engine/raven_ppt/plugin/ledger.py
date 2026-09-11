"""The deck ledger: what a compacted deck run must not lose, read off disk.

The host's compaction replaces the head of a long turn with a model-written
handoff brief shaped for code work (files read, commands run, test output).
A deck run's head holds two other things. The user's own words -- the brief
they typed, the answers they gave ``ask_user``, the feedback they sent between
turns -- are the deck's requirements, and a summary that paraphrases them is
a deck built to a paraphrase. And the deck's working state -- the outline, the
figures ingested, the reader's open findings, what was published or refused --
is already written to disk by the tools that made it, so nothing about it needs
summarising: it is listed.

The ledger is appended once after each compaction, as a note under the summary
(the ``append_note`` grant), and says where each thing lives so the model asks
the tool for the file rather than rebuilding from memory.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from raven_ppt.contracts import Project
from raven_ppt.contracts.outline import load_outline, outline_path
from raven_ppt.services import review_ledger, state
from raven_ppt.services.publish.deliver import last_refusal, published_digests

logger = logging.getLogger(__name__)

#: The first line of every ledger note, followed by ``#<digest>`` of the summary
#: it answers: a later iteration looks for the ledger of THIS summary, so one
#: retained in the verbatim tail from an earlier compaction does not stand in for
#: it -- the host inserts each new summary before the tail it keeps.
LEDGER_MARKER = "[Deck ledger -- the user's words verbatim and the deck's state on disk]"

#: Where the hook journals the user's words as they arrive in the running turn.
#: The session record the hook is handed ends with the PREVIOUS turn (the loop
#: files a turn after it finishes), so the turn that is long enough to compact is
#: exactly the one whose inbound message and ``ask_user`` answers the record does
#: not hold yet; they are written here as they are seen, before any compaction
#: can take them out of the window.
JOURNAL_FILENAME = ".deck-words.json"

# The host's summary message begins with this line (raven.agent.loop.compaction).
# Spelled here so the plugin does not import loop internals; the test suite pins
# the two spellings against each other.
SUMMARY_MARKER = "[Context summary — earlier steps were compacted to fit the context window]"

#: The ledger quotes the user, never trims them; this is the safety stop for a
#: session of hundreds of long messages, and the note says when it was hit.
WORDS_CAP_CHARS = 24_000

# The loop writes user-role messages of its own -- the synthetic message that
# carries a tool's pictures, and empty-response recovery scaffolding -- and marks
# each with a private key, which is how the trunk keeps them out of a filed turn.
# Read the mark rather than the prose: a live run put 29 image-withdrawal notices
# and nothing else into a journal of 34 items, and matching their wording would
# only hold until the wording changed. Spelled here so the plugin does not import
# loop internals; the test suite pins these against the trunk.
LOOP_AUTHORED_KEYS = ("_attached_image", "_recovery_synthetic")

# What the loop's deterministic prune writes over a tool result it elides. It
# overwrites the body in place and leaves no key behind, so unlike the two above
# the body is the only evidence there is that the tool did not say this.
ELIDED_TOOL_BODY = "[earlier tool output elided to fit the context window]"
_ASK_SPLIT = re.compile(r"\s*User answered: ")
_ASK_TAIL = re.compile(r"[\s.]*(?:Continue\.)?\s*$")
_ASK_WRAPPER = re.compile(r"\[(?:BEGIN|END) UNTRUSTED ask_user[^\]]*\]\s*")


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def summary_index(messages: list[dict[str, Any]]) -> int | None:
    """Index of the latest compaction summary in the window, or None."""
    found = None
    for i, message in enumerate(messages):
        if message.get("role") == "user" and _text(message.get("content")).startswith(SUMMARY_MARKER):
            found = i
    return found


def summary_digest(messages: list[dict[str, Any]], index: int) -> str:
    """The identity of the summary at ``index``: a digest of its text."""
    import hashlib

    return hashlib.sha1(_text(messages[index].get("content")).encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def ledger_marker(digest: str) -> str:
    return f"{LEDGER_MARKER} #{digest}"


def ledger_stands_for(messages: list[dict[str, Any]], digest: str) -> bool:
    """Whether a ledger answering the summary with ``digest`` is anywhere in the window."""
    marker = ledger_marker(digest)
    return any(marker in _text(m.get("content")) for m in messages)


def journal_path(root: Path) -> Path:
    return root / JOURNAL_FILENAME


def read_journal(root: Path) -> list[str]:
    try:
        held = json.loads(journal_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(item) for item in held] if isinstance(held, list) else []


def journal(
    root: Path,
    *,
    inbound: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    window: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Record the user's words seen in the running turn; return the journal so far.

    ``inbound`` is the user's own text at the inbound fire (before the hook
    rewrites the model's view of it); ``messages`` is this turn's part of the live
    window, from which ``ask_user`` answers and any later user message are taken;
    ``window`` is the whole live window, from which only ``ask_user`` answers are
    taken. The host does not move the turn's base index when a compaction shrinks
    the window, so a slice from it can miss an answer given after the first
    compaction; the user messages below the slice belong to earlier turns and are
    the filed record's to quote. Exact repeats are not written twice, so calling
    this every iteration costs one small file.
    """
    held = read_journal(root)
    seen = set(held)
    fresh: list[str] = []
    if inbound and inbound.strip():
        fresh.append(f"User: {inbound.strip()}")
    fresh.extend(users_words(messages))
    fresh.extend(item for item in users_words(window) if item.startswith("Answered: "))
    added = []
    for item in fresh:
        if item not in seen:
            seen.add(item)
            added.append(item)
    if added:
        held.extend(added)
        try:
            journal_path(root).parent.mkdir(parents=True, exist_ok=True)
            journal_path(root).write_text(json.dumps(held, ensure_ascii=False, indent=0), encoding="utf-8")
        except OSError as exc:
            logger.debug("deck ledger: journal not written: %s", exc)
    return held


def all_words(root: Path, history: list[dict[str, Any]] | None) -> list[str]:
    """The user's words across the session: the filed record (previous turns), then
    what the journal saw in the running turn that the record does not hold yet."""
    words = users_words(history)
    seen = set(words)
    for item in read_journal(root):
        if item not in seen:
            seen.add(item)
            words.append(item)
    return words


def users_words(history: list[dict[str, Any]] | None) -> list[str]:
    """Every message the user sent and every answer they gave, verbatim, in order.

    Read off the session record rather than the working window: the window's
    inbound text is the staging block the hook wrote over it, the record keeps
    the user's own words. ``ask_user`` answers arrive as the tool's result
    text; the wrapper the host puts around untrusted data is dropped, the
    question and answer are kept as the tool printed them.

    A user-role message is not proof the user spoke: the loop writes its own
    (``LOOP_AUTHORED_KEYS``), and it overwrites an elided tool body in place
    (``ELIDED_TOOL_BODY``). Both are skipped, because the journal is fed from
    the live window where the trunk has not yet stripped them, and the ledger
    spends a 24k budget on what it finds.
    """
    words: list[str] = []
    for message in history or []:
        if any(message.get(key) for key in LOOP_AUTHORED_KEYS):
            continue
        role = message.get("role")
        if role == "user":
            text = _text(message.get("content")).strip()
            if text and not text.startswith(SUMMARY_MARKER) and LEDGER_MARKER not in text:
                words.append(f"User: {text}")
        elif role == "tool" and message.get("name") == "ask_user":
            text = _ASK_WRAPPER.sub("", _text(message.get("content"))).strip()
            if text == ELIDED_TOOL_BODY:
                continue
            parts = _ASK_SPLIT.split(text)
            answers = [_ASK_TAIL.sub("", part).strip() for part in parts[1:]] if len(parts) > 1 else [text]
            words.extend(f"Answered: {answer}" for answer in answers if answer)
    return words


def deck_state_lines(root: Path) -> list[str]:
    """The deck's working state as the tools left it on disk, one line per fact."""
    lines: list[str] = []
    try:
        project = Project(workspace=root, slug="deck")
    except ValueError:
        return ["deck: no project under this folder"]
    if not project.root.is_dir():
        return [f"deck: nothing built yet under {project.root}"]
    try:
        held = state.read(project)
    except Exception as exc:  # a half-written deck must not take the note down
        logger.debug("deck ledger: state unreadable: %s", exc)
        held = None
    if held is not None:
        if held.brief is not None:
            brief = held.brief
            budget = getattr(brief, "pages", None)
            lines.append(
                "brief: "
                + ", ".join(
                    part
                    for part in (
                        f"language={getattr(brief, 'language', '') or '?'}",
                        f"audience={getattr(brief, 'audience', '') or '?'}",
                        f"pages={budget}" if budget is not None else "",
                    )
                    if part
                )
            )
            for note in getattr(brief, "notes", ()) or ():
                lines.append(f"brief note: {note}")
            for rule in getattr(brief, "forbidden", ()) or ():
                lines.append(f"brief forbids: {rule}")
        if held.template is not None:
            source = getattr(held.template, "source", None)
            inventory = getattr(held.template, "inventory", None)
            layouts = len(getattr(inventory, "layouts", ()) or ())
            lines.append(f"template: {Path(source).name if source else 'bound'}, {layouts} layout(s), see ppt_template")
        lines.append(f"sources: {len(held.sources)} file(s) under {project.sources_dir}")
        if held.figures:
            lines.append(f"figures: {len(held.figures)} in the catalogue; inspect one with ppt_figure_inspect")
            for figure in held.figures[:40]:
                label = figure.label or figure.caption or figure.visual_caption
                lines.append(f"  {figure.figure_id} ({figure.kind}) {label[:80]}".rstrip())
        lines.append(f"build script: {'present' if held.script else 'none'} under {project.build_dir}")
    outline = None
    try:
        outline = load_outline(outline_path(project))
    except Exception as exc:
        logger.debug("deck ledger: outline unreadable: %s", exc)
    if outline is not None:
        lines.append(f"outline: {len(outline.pages)} page(s) at {outline_path(project)}; takeaway: {outline.takeaway}")
        for page in outline.pages:
            figures = f" figures={','.join(page.figures)}" if page.figures else ""
            layout = f" [{page.layout}]" if page.layout else ""
            lines.append(f"  p{page.page}{layout} {page.claim}{figures}")
    else:
        lines.append("outline: none written yet")
    try:
        findings = review_ledger.open_findings(project)
    except Exception as exc:
        logger.debug("deck ledger: review ledger unreadable: %s", exc)
        findings = []
    if findings:
        lines.append(f"reader findings still open: {len(findings)} (answer or dismiss them through ppt_build)")
        for entry in findings[:30]:
            what = " -- ".join(str(entry.get(k) or "") for k in ("where", "what") if entry.get(k))
            seen = int(entry.get("times_seen") or 1)
            lines.append(
                f"  p{entry.get('page')} {entry.get('kind')}: {what[:160]}" + (f" (seen {seen}x)" if seen > 1 else "")
            )
    else:
        lines.append("reader findings still open: none")
    published = published_digests(project.state_dir)
    lines.append(f"published: {len(published)} deck(s) so far under {project.exports_dir}")
    if refusal := last_refusal(project.state_dir):
        lines.append(f"last build refused: {refusal[:300]}")
    return lines


def deck_ledger(root: Path, history: list[dict[str, Any]] | None, digest: str = "") -> str:
    """The note appended under the compaction summary identified by ``digest``."""
    words = all_words(root, history)
    body: list[str] = [
        ledger_marker(digest) if digest else LEDGER_MARKER,
        "",
        "The user's words, verbatim (requirements; the summary above may paraphrase them):",
    ]
    used = 0
    for item in words:
        if used + len(item) > WORDS_CAP_CHARS:
            body.append(
                f"[{len(words) - words.index(item)} earlier message(s) left out to stay under {WORDS_CAP_CHARS} characters]"
            )
            break
        body.append(f"- {item}")
        used += len(item)
    if not words:
        body.append("- (none recorded)")
    body.append("")
    body.append("The deck on disk (read these files rather than rebuilding from memory):")
    body.extend(f"- {line}" if not line.startswith("  ") else line for line in deck_state_lines(root))
    return "\n".join(body)


def ledger_json(root: Path, history: list[dict[str, Any]] | None) -> str:
    """Machine-readable twin, for a harness that checks what survived."""
    return json.dumps({"words": all_words(root, history), "deck": deck_state_lines(root)}, ensure_ascii=False, indent=1)
