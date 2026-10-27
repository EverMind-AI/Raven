"""The compaction checkpoint -- what stands where removed messages were.

A checkpoint is one message in the history, written by the strategy rather than
by the conversation, that represents everything compaction has taken out of the
window so far. It carries the span it covers, the id of the checkpoint it
replaces, and eight fixed sections of prose whose lines each point back at the
messages they came from.

Three properties this module exists to hold:

- **Fixed shape.** The eight sections are always present, in one order, and an
  empty one says ``[None]`` rather than being absent. A reader never has to
  distinguish "nothing to report" from "the model forgot the heading", and the
  parser never branches on which sections arrived.
- **A chain, not a pile.** Each generation supersedes the one before it and
  covers a span that starts no later, so "what was in the window three
  summaries ago" is a walk rather than an archaeology.
- **Every line is checkable.** A line whose refs were dropped is an assertion
  nobody can verify against the log. The model is asked to carry them, and what
  it does not carry is attached here, which turns a request into a guarantee.
- **It is enough to resume from.** A checkpoint records the ordinals it
  retired as well as the span it covers, so a process that starts after it was
  written splices the same message at the same place without recompacting.

Two kinds share the shape. A ``summary`` is written by a model. A ``fold`` is
written by arithmetic -- a count, a token estimate and a ref -- and is what a
deployment gets when it will not spend a model call on compaction, or what a
turn falls back to when the call fails. Both supersede, both are stored, and
both are folded forward by the next generation, so the chain does not care
which produced any particular link.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping, Sequence

from raven.utils.refs import REF_PATTERN, SourceRef

SECTION_GUIDE: dict[str, str] = {
    "Goal": "what the user is trying to achieve, as first stated and as it has changed since",
    "User Constraints": "what the user required, forbade, or corrected, in the user's own words, quoted",
    "Decisions": "a choice already made in this conversation, and the reason for it",
    "Artifacts": "an exact path, identifier, command, or signature the work produced or changed, "
    "and what changed about it",
    "Findings": "a fact established -- a root cause, an error string, a measurement -- one bullet each, "
    "and no claim about a set whose members you have not all listed",
    "Open Threads": "a question still unanswered, or work asked for and not yet done",
    "Next Step": "the single next action, in line with the most recent request",
    "Pointers": "a bare [[ref ...]] tag for a span too large to summarise that may still be needed; no prose",
}
"""The eight headings and what each one is for, in the order they are written.

Named for concepts Raven already had rather than borrowed from another
harness: ``Goal`` / ``Decisions`` / ``Open Threads`` are the Curator's working
state, so a reader moving between strategies meets the same words. ``Pointers``
is the one no other harness can have -- a section of refs with no prose only
means something where a ref can be cashed.

Each heading owns a sentence saying what belongs under it, and the instruction
renders that sentence beside the heading. A heading with nothing beside it is
answered from whatever the surrounding text makes it mean: a bare
``## Constraints`` sitting under a numbered list of rules came back holding
those rules, and headings with no stated purpose came back holding commentary
about the request rather than about the conversation. ``User Constraints``
names its owner for the same reason -- the request already said whose
constraints it meant, one rule further down, and the nearer reading won.
"""

SECTIONS: tuple[str, ...] = tuple(SECTION_GUIDE)
"""The eight headings alone -- derived, so the two cannot drift apart."""

EMPTY_SECTION = "[None]"

SUMMARY_TAG = "compacted-summary"
FOLD_TAG = "compacted-fold"

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(?P<name>[A-Za-z][A-Za-z ]*?)\s*:?\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_EMPTY_MARKERS = frozenset({"[none]", "(none)", "none", "n/a", "-", "(n/a)", "(empty)"})
_REF_SHAPED = re.compile(r"\[\[\s*refs?\b[^\]]*\]\]", re.IGNORECASE)
"""Anything written to look like a ref. Wider than ``REF_PATTERN`` on purpose:
what this matches and that one does not is exactly the text that reads as a
pointer and resolves to nothing. Still narrow enough to leave ``[[ -f auth.ts
]]`` in a preserved command alone."""

_GUIDE_ECHOES = frozenset(guide.lower() for guide in SECTION_GUIDE.values())
"""The bracketed lines, as a reply may hand them back.

Saying what belongs under a heading is what stops the heading being answered
from its surroundings, and it is also one more line a model can copy instead of
answering -- copying the request verbatim is the behaviour these lines exist to
correct. An echo carries no content, so it reads as an empty section rather
than being stored as a fact about the conversation.
"""

_SECTION_ALIASES = {"constraints": "User Constraints"}
"""Shorter spellings a reply may reach for. Emitting one name and accepting
several is the same trade as ``_EMPTY_MARKERS``: a heading the parser does not
recognise is dropped with everything under it, and dropping the user's own
words is the most expensive way this can go wrong."""

_SECTION_BY_KEY = {**{name.lower(): name for name in SECTIONS}, **_SECTION_ALIASES}


@dataclass(frozen=True)
class SummaryEntry:
    """One line of a checkpoint section, and where it came from."""

    text: str
    refs: tuple[SourceRef, ...] = ()

    def render(self, *, inline_refs: bool = True) -> str:
        """The line as it appears in the window, refs trailing it.

        A ref already written into the text is not repeated: the model is asked
        to keep the ones it was shown, and a duplicate would read as two
        citations for one fact.
        """
        if not inline_refs or not self.refs:
            return self.text
        trailing = [ref.render() for ref in self.refs if ref.render() not in self.text]
        return " ".join([self.text, *trailing]) if trailing else self.text


@dataclass(frozen=True)
class CompactionCheckpoint:
    """One generation of compaction, as a storable, renderable record."""

    checkpoint_id: str
    session_key: str
    created_at: str
    generation: int
    covers: SourceRef
    """The span of session ordinals this checkpoint speaks for -- its own
    generation's and every earlier one's, since each folds the last in."""

    supersedes: str | None
    sections: dict[str, tuple[SummaryEntry, ...]]
    model: str
    byte_prefix_reused: bool
    """Whether the request went out as the recorded request's own bytes.

    A local fact about what was sent, and the *precondition* for a provider
    cache read -- never evidence that one happened. No provider this talks to
    reports a cache hit on a plain completion, so the honest reading of a true
    here is "cache-eligible, unobserved". Named for what it measures after the
    older ``prefix_reused`` was read as a hit and quoted in a cost argument.
    """

    kind: Literal["summary", "fold"] = "summary"
    folded_messages: int = 0
    folded_tokens: int = 0

    retired: tuple[int, ...] = ()
    """The session ordinals this checkpoint stands in for, in order.

    Exactly what the retention planner removed, this turn and every earlier
    one -- never what the trimmer additionally dropped to fit the budget,
    which is re-decided every turn because nothing here points at it. Narrower
    than ``covers`` can say on its own: a pinned exchange inside the covered
    span stays in the window and is not retired. Later turns select around
    exactly this set, which is what keeps the history they send byte-stable.
    """

    written_at_ns: int = field(default=0, compare=False)
    """When the store wrote this checkpoint. Orders two files of one
    generation -- a crash between writing one and naming it, or two writers
    -- so the newest written is the one that stands, whatever its id."""

    cache_status: Literal["hit", "miss", "unobservable"] = "unobservable"
    """What the provider said about serving this request from its cache.

    Three states, not two, because "no cache read" and "no way to tell" are
    different answers and only the first is a measurement: ``miss`` means the
    provider reported a cache count and it was zero, ``unobservable`` that it
    reported none at all. A cost argument may cite ``hit`` and nothing else.
    """

    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    summary_input_tokens: int = 0
    summary_output_tokens: int = 0
    """What the summarising call itself cost, as the provider counted it.

    Recorded so the net saving can be worked out from provider usage rather
    than inferred from the local message shape: the removed span is only worth
    removing if it costs less than the call that removed it.
    """

    @property
    def scope(self) -> str:
        """The session revision scope this checkpoint was written under."""
        return self.covers.scope

    def message(self, *, inline_refs: bool = True) -> dict[str, Any]:
        """The single history message this checkpoint occupies.

        ``user`` rather than a role of its own: only a message can reach the
        model, and inventing a role would make every provider's shape check the
        strategy's problem.
        """
        return {"role": "user", "content": self.render(inline_refs=inline_refs)}

    def render(self, *, inline_refs: bool = True) -> str:
        body = self._fold_body() if self.kind == "fold" else self._summary_body(inline_refs=inline_refs)
        tag = FOLD_TAG if self.kind == "fold" else SUMMARY_TAG
        return f'<{tag} generation="{self.generation}">\n{body}\n</{tag}>'

    def _summary_body(self, *, inline_refs: bool) -> str:
        lines: list[str] = []
        for name in SECTIONS:
            lines.append(f"## {name}")
            entries = self.sections.get(name, ())
            if not entries:
                lines.append(EMPTY_SECTION)
                continue
            lines.extend(f"- {entry.render(inline_refs=inline_refs)}" for entry in entries)
        return "\n".join(lines)

    def _fold_body(self) -> str:
        return (
            f"[[folded {self.folded_messages} messages, ~{self.folded_tokens:,} tokens]] {self.covers.render()}\n"
            "Use context_retrieve to read any of it."
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "session_key": self.session_key,
            "created_at": self.created_at,
            "generation": self.generation,
            "covers": self.covers.render(),
            "supersedes": self.supersedes,
            "sections": {
                name: [{"text": entry.text, "refs": [ref.render() for ref in entry.refs]} for entry in entries]
                for name, entries in self.sections.items()
            },
            "model": self.model,
            "byte_prefix_reused": self.byte_prefix_reused,
            "cache_status": self.cache_status,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "summary_input_tokens": self.summary_input_tokens,
            "summary_output_tokens": self.summary_output_tokens,
            "kind": self.kind,
            "folded_messages": self.folded_messages,
            "folded_tokens": self.folded_tokens,
            "retired": ordinal_runs(self.retired),
            "written_at_ns": self.written_at_ns,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "CompactionCheckpoint":
        covers = SourceRef.parse(str(data.get("covers", "")))
        if covers is None:
            raise ValueError(f"checkpoint {data.get('checkpoint_id')!r} has no readable covers ref")
        sections: dict[str, tuple[SummaryEntry, ...]] = {}
        raw_sections = data.get("sections") or {}
        for name in SECTIONS:
            entries = raw_sections.get(name) or [] if isinstance(raw_sections, Mapping) else []
            sections[name] = tuple(
                SummaryEntry(
                    text=str(entry.get("text", "")),
                    refs=tuple(ref for ref in (SourceRef.parse(r) for r in entry.get("refs") or []) if ref),
                )
                for entry in entries
                if isinstance(entry, Mapping)
            )
        return cls(
            checkpoint_id=str(data.get("checkpoint_id", "")),
            session_key=str(data.get("session_key", "")),
            created_at=str(data.get("created_at", "")),
            generation=int(data.get("generation", 0)),
            covers=covers,
            supersedes=data.get("supersedes") or None,
            sections=sections,
            model=str(data.get("model", "")),
            byte_prefix_reused=bool(data.get("byte_prefix_reused", data.get("prefix_reused", False))),
            cache_status=_cache_status(data.get("cache_status")),
            cache_read_tokens=int(data.get("cache_read_tokens", 0) or 0),
            cache_write_tokens=int(data.get("cache_write_tokens", 0) or 0),
            summary_input_tokens=int(data.get("summary_input_tokens", 0) or 0),
            summary_output_tokens=int(data.get("summary_output_tokens", 0) or 0),
            kind="fold" if data.get("kind") == "fold" else "summary",
            folded_messages=int(data.get("folded_messages", 0)),
            folded_tokens=int(data.get("folded_tokens", 0)),
            retired=tuple(sorted(_retired_from_runs(data.get("retired")))),
            written_at_ns=int(data.get("written_at_ns", 0) or 0),
        )


def ordinal_runs(ordinals: "Sequence[int] | set[int] | frozenset[int]") -> list[list[int]]:
    """``ordinals`` as ``[lo, hi]`` runs -- how a set of ordinals is stored.

    A retired span is usually hundreds of consecutive ordinals with a pinned
    hole or two in it, so runs keep the file proportional to the holes rather
    than to the conversation.
    """
    runs: list[list[int]] = []
    for index in sorted(set(ordinals)):
        if runs and index == runs[-1][1] + 1:
            runs[-1][1] = index
        else:
            runs.append([index, index])
    return runs


ORDINAL_CEILING = 1_000_000
"""No session log holds this many messages; a stored ordinal past it is
corruption, refused before anything is expanded."""


def valid_runs(raw: Any) -> list[tuple[int, int]]:
    """The well-formed ``[lo, hi]`` runs of ``raw``, unexpanded."""
    runs: list[tuple[int, int]] = []
    for run in raw if isinstance(raw, list) else []:
        if (
            isinstance(run, list)
            and len(run) == 2
            and all(isinstance(edge, int) and not isinstance(edge, bool) for edge in run)
            and 0 <= run[0] <= run[1]
        ):
            runs.append((run[0], run[1]))
    return runs


def ordinals_from_runs(raw: Any, *, limit: int = ORDINAL_CEILING) -> frozenset[int]:
    """The inverse of :func:`ordinal_runs`, below ``limit``.

    Clipped before it is expanded, so a corrupt ``[0, 10**12]`` costs what the
    log is long rather than what the number says. Anything malformed reads as
    empty.
    """
    bound = min(limit, ORDINAL_CEILING)
    found: set[int] = set()
    for lo, hi in valid_runs(raw):
        if lo < bound:
            found.update(range(lo, min(hi, bound - 1) + 1))
    return frozenset(found)


def runs_end(raw: Any) -> int:
    """One past the highest ordinal ``raw`` names, ``0`` for none -- without expanding it."""
    return max((hi + 1 for _lo, hi in valid_runs(raw)), default=0)


def _retired_from_runs(raw: Any) -> frozenset[int]:
    """A checkpoint's retired set; a run past :data:`ORDINAL_CEILING` makes the file unreadable.

    Refused rather than clipped: a checkpoint that silently stood for fewer
    messages than it claims would put retired messages back in the window.
    """
    if runs_end(raw) > ORDINAL_CEILING:
        raise ValueError("checkpoint retires ordinals past any real session log")
    return ordinals_from_runs(raw)


def empty_sections() -> dict[str, tuple[SummaryEntry, ...]]:
    return {name: () for name in SECTIONS}


_ID_SUFFIX_CHARS = 8


def new_checkpoint_id(generation: int) -> str:
    return f"cp-{generation:03d}-{uuid.uuid4().hex[:_ID_SUFFIX_CHARS]}"


def timestamp(now_fn: Callable[[], datetime] | None = None) -> str:
    """When a checkpoint was written, to the second, in UTC unless ``now_fn`` says otherwise."""
    moment = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    return moment.isoformat(timespec="seconds")


def fold_checkpoint(
    *,
    session_key: str,
    generation: int,
    covers: SourceRef,
    previous: "CompactionCheckpoint | None",
    folded_messages: int,
    folded_tokens: int,
    created_at: str,
) -> CompactionCheckpoint:
    """A checkpoint written by arithmetic: how much went, and where it is.

    No model, so nothing in it can be wrong about the conversation. What it
    costs is content: the turn knows something was removed and can ask for
    it, but cannot read it without asking.
    """
    sections = empty_sections()
    sections["Pointers"] = (
        SummaryEntry(text=f"{folded_messages} removed message(s), ~{folded_tokens:,} tokens", refs=(covers,)),
    )
    return CompactionCheckpoint(
        checkpoint_id=new_checkpoint_id(generation),
        session_key=session_key,
        created_at=created_at,
        generation=generation,
        covers=covers,
        supersedes=previous.checkpoint_id if previous is not None else None,
        sections=sections,
        model="",
        byte_prefix_reused=False,
        kind="fold",
        folded_messages=folded_messages,
        folded_tokens=folded_tokens,
    )


def section_template() -> str:
    """The headings as a request has to ask for them: each one followed by the
    bracketed line saying what belongs under it."""
    return "\n\n".join(f"## {name}\n- [{guide}]" for name, guide in SECTION_GUIDE.items())


def _cache_status(raw: Any) -> Literal["hit", "miss", "unobservable"]:
    """Read a stored status, defaulting anything unrecognised to unobservable.

    Checkpoints written before the field existed carry no cache evidence, and
    the absence of evidence is exactly what unobservable names.
    """
    return raw if raw in ("hit", "miss", "unobservable") else "unobservable"


def seen_headings(text: str) -> set[str]:
    """The template headings a reply actually carried.

    Separates a section the model answered with its emptiness marker from one
    it never reached: both parse to no entries, and only the second one means
    the reply stopped early. The instruction says never to drop a section, so a
    heading that is simply absent is the signature of a reply that ended before
    it was finished -- whatever the provider called its finish reason.
    """
    seen: set[str] = set()
    for raw in text.splitlines():
        heading = _HEADING.match(raw)
        if heading is None:
            continue
        name = _SECTION_BY_KEY.get(heading["name"].strip().lower())
        if name is not None:
            seen.add(name)
    return seen


def parse_sections(text: str) -> dict[str, tuple[SummaryEntry, ...]] | None:
    """Split a model's reply into the eight sections, or ``None``.

    ``None`` means the reply is not a checkpoint at all -- no heading this
    module recognises appeared -- which is the case a caller has to answer by
    falling back rather than by storing an empty summary. A reply that is
    merely incomplete parses: the missing sections come back empty, which is
    indistinguishable from ``[None]`` and is the reading that loses least.

    Headings the template does not name are dropped along with their bodies. A
    model that invents a ninth section has written something no reader consumes,
    and carrying it forward would let the shape drift one generation at a time.

    A line that is the template's own bracketed description handed back reads
    as no line at all, for the same reason an emptiness marker does: it says
    nothing about the conversation.
    """
    found: dict[str, list[SummaryEntry]] = {}
    current: str | None = None
    for raw in text.splitlines():
        heading = _HEADING.match(raw)
        if heading is not None:
            current = _SECTION_BY_KEY.get(heading["name"].strip().lower())
            if current is not None:
                found.setdefault(current, [])
            continue
        if current is None:
            continue
        # Read past the bullet before deciding, so the emptiness marker is
        # recognised whichever way the model wrote it: the template shows every
        # section as a bullet, which is an invitation to answer "nothing here"
        # as one too.
        body = _strip(raw)
        if not body or body.lower() in _EMPTY_MARKERS or body.strip("[]").strip().lower() in _GUIDE_ECHOES:
            continue
        bullet = _BULLET.match(raw)
        if bullet is None and found[current]:
            # A wrapped line: providers hard-wrap prose, and starting a new
            # entry at every newline would cut a path or an error string in
            # half. Refs are re-read over the joined text below.
            previous = found[current][-1]
            found[current][-1] = replace(previous, text=f"{previous.text} {body}")
            continue
        found[current].append(SummaryEntry(text=body))
    if not found:
        return None
    return {name: tuple(_with_own_refs(entry) for entry in found.get(name, ())) for name in SECTIONS}


def merge_refs(
    sections: Mapping[str, Sequence[SummaryEntry]],
    previous: CompactionCheckpoint | None,
) -> dict[str, tuple[SummaryEntry, ...]]:
    """Union each new entry's refs with those of the entries it overlaps.

    The template asks the model to carry every ref it was shown, and that ask
    is probabilistic: rewriting a sentence is exactly when a tag gets dropped,
    and a summary line without one is an assertion nobody can check against the
    log. The new text always wins -- the model has just decided what is still
    true -- but the refs are pooled, so the way back survives a rewrite.

    Overlap is by span, not by equality: a new line covering ``0-40`` and an old
    one covering ``12-25`` are about the same stretch of conversation, and the
    narrower ref is the more useful of the two to keep.
    """
    if previous is None:
        return {name: tuple(sections.get(name, ())) for name in SECTIONS}
    merged: dict[str, tuple[SummaryEntry, ...]] = {}
    for name in SECTIONS:
        old = previous.sections.get(name, ())
        merged[name] = tuple(_merged_entry(entry, old) for entry in sections.get(name, ()))
    return merged


def ground_refs(
    sections: Mapping[str, Sequence[SummaryEntry]],
    covers: SourceRef,
) -> dict[str, tuple[SummaryEntry, ...]]:
    """Leave every entry carrying refs that can actually be cashed.

    Rendering a ref where the model can read it promises the model can ask for
    what it names and get the bytes back. Three things break that promise, and
    each is answered here.

    **A line with no ref at all.** The request the model answers is a verbatim
    prefix of the conversation, so nothing in it labels a message with its
    ordinal; the only refs it can carry forward are the ones already sitting in
    pruned tool results. Left alone, most lines would be uncheckable. The
    covering span is coarse, but it names a stretch that really does contain
    the line's source, and it resolves.

    **A line naming a log that is not this one.** Asked to preserve tags, a
    model that has seen few writes one that looks right. A ``session`` ref for
    another scope is removed -- from the entry and from the prose it was
    written into, since rendering skips a ref the text already holds. Refs into
    another store are left alone: that store owns what its scopes mean, and
    this one has no standing to judge them.

    **A line carrying something that only looks like a ref.** ``[[ref
    auth:retry:1]]`` is not the grammar, so nothing downstream reads it as a
    ref -- not the retrieval tool, and not the pass above, which only sees
    tokens that parsed. Left alone it reaches the next turn as prose that is
    indistinguishable from a pointer, and the turn spends a tool call finding
    out it names nothing. It is cut out of the text; a line that was nothing
    but one is dropped, since a pointer to nowhere is worse than no line.
    """
    grounded: dict[str, tuple[SummaryEntry, ...]] = {}
    for name in SECTIONS:
        entries: list[SummaryEntry] = []
        for entry in sections.get(name, ()):
            resolvable = _resolvable_only(entry)
            if resolvable is None:
                continue
            invented = {ref for ref in resolvable.refs if not _belongs(ref, covers)}
            standing = _without(resolvable, invented) if invented else resolvable
            entries.append(standing if standing.refs else replace(standing, refs=(covers,)))
        grounded[name] = tuple(entries)
    return grounded


def _belongs(ref: SourceRef, covers: SourceRef) -> bool:
    """Whether ``ref`` is one this checkpoint may stand behind: this log's own,
    or another store's, whose scopes this one does not speak for."""
    return ref.store != covers.store or ref.scope == covers.scope


def _resolvable_only(entry: SummaryEntry) -> "SummaryEntry | None":
    """The entry with ref-shaped text that no store can resolve taken out, or
    ``None`` when that was the whole line."""
    text = _REF_SHAPED.sub(lambda m: m[0] if REF_PATTERN.fullmatch(m[0]) else "", entry.text)
    if text == entry.text:
        return entry
    text = " ".join(text.split())
    return None if not text and not entry.refs else replace(entry, text=text)


def _without(entry: SummaryEntry, invented: "set[SourceRef]") -> SummaryEntry:
    """The entry with those refs gone, out of the prose as well as the list."""
    text = entry.text
    for ref in invented:
        text = text.replace(ref.render(), "")
    return replace(
        entry,
        text=" ".join(text.split()),
        refs=tuple(ref for ref in entry.refs if ref not in invented),
    )


def _strip(raw: str) -> str:
    line = raw.strip()
    bullet = _BULLET.match(line)
    return line[bullet.end() :].strip() if bullet else line


def _with_own_refs(entry: SummaryEntry) -> SummaryEntry:
    """Read back the refs the model left in the line's own text."""
    return replace(entry, refs=tuple(dict.fromkeys(SourceRef.find_all(entry.text))))


def _merged_entry(entry: SummaryEntry, old: Sequence[SummaryEntry]) -> SummaryEntry:
    inherited = [ref for other in old if _overlaps_any(entry.refs, other.refs) for ref in other.refs]
    return replace(entry, refs=tuple(dict.fromkeys([*entry.refs, *inherited])))


def _overlaps_any(left: Sequence[SourceRef], right: Sequence[SourceRef]) -> bool:
    return any(_overlaps(a, b) for a in left for b in right)


def _overlaps(a: SourceRef, b: SourceRef) -> bool:
    return a.store == b.store and a.scope == b.scope and a.lo <= b.hi and b.lo <= a.hi


__all__ = [
    "EMPTY_SECTION",
    "ORDINAL_CEILING",
    "FOLD_TAG",
    "SECTIONS",
    "SECTION_GUIDE",
    "SUMMARY_TAG",
    "CompactionCheckpoint",
    "SummaryEntry",
    "empty_sections",
    "fold_checkpoint",
    "ground_refs",
    "merge_refs",
    "new_checkpoint_id",
    "ordinal_runs",
    "ordinals_from_runs",
    "runs_end",
    "valid_runs",
    "parse_sections",
    "section_template",
    "seen_headings",
    "timestamp",
]
