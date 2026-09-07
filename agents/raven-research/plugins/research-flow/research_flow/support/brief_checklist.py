"""What the request asks for, read literally and handed back before the answer is written.

The 2026-09-04 head-to-head lost three dimensions to constraints that were stated in the
request and satisfied by intention rather than by checking: a required section was absent
entirely, five candidates that the stated length envelope excluded took slots in the main
table, and the do-not-bother list carried filler the request had already ruled out. None
of those is a research failure. Each is an instruction that scrolled fifteen thousand
tokens up the context by the time the report was written.

So this module reads the request for the parts a parser can settle - how many items, what
is excluded, what artefacts are named - and the reminder that already rides the user
message carries them back down to where the model is writing. Three properties are the
whole design.

**It only reads. It never judges the answer.** A bounce costs a full generation, and the
gate that owns bouncing says in its own docstring that it bounces for a missing section
and nothing else. A checklist that is wrong about what the request asked for would spend
generations arguing with the reader; a checklist that is merely incomplete costs nothing.

**It quotes rather than interprets.** The count is the range as written, the exclusions
are the names as written, the artefacts are the noun phrases as written. Nothing is
normalised into a vocabulary of ours, because a request that meant something else is then
visibly a request that meant something else, rather than a checklist item nobody can trace.

**It stays silent when it is unsure, and it never inverts.** Every extractor below returns
nothing on a shape it does not recognise, and the reminder is unchanged when all three do.
A request with no machine-readable constraints is the common case and must cost nothing.

Silence is not the floor, though - reversal is, and review found two ways in. A range was
read as the deliverable count by its position in the sentence, so "score candidates 1-5 on
relevance, then shortlist 15-25 benchmarks" handed back the scoring scale as the number of
items. And a noun phrase was read as a deliverable wherever it appeared, so "do not include
a comparison table" was quoted back as something to deliver. Both arrive in the sentence
closest to generation, under the words "deliver each one", which makes an inverted
constraint strictly worse than a missing one: a constraint this misses is one the request
still carries further up, while a constraint this reverses is one the harness invented
against the request. So a range is a count only where a counted noun follows it and no
scale word introduces it, and nothing negated inside its clause becomes a deliverable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The counted range: "15-25 benchmarks", "shortlist 15 to 25". Bounded well under a
#: thousand because a research request asks for items, and a four-digit pair is a year
#: span or a token budget.
_COUNT_RE = re.compile(r"(?<![\w.])(\d{1,3})\s*(?:-|--|–|—|to)\s*(\d{1,3})(?![\w.])")

#: Units that make a range a size rather than a count of deliverables. Checked on what
#: follows the range, because "16k-256k words" and "15-25 benchmarks" are the same shape.
_SIZE_UNIT_RE = re.compile(
    r"^\s*(?:k|m|b|kb|mb|gb|x|×|%|token|word|char|page|doc|hour|day|month|year|"
    r"gpu|billion|million|thousand)\b",
    re.I,
)

#: A clause boundary. Negation and scale words are both scoped to the clause they appear
#: in, because a request routinely rules one thing out and asks for another in the next
#: breath - "no comparison table; a narrative instead".
_CLAUSE_SPLIT_RE = re.compile(r"[.;\n]|\bbut\b", re.I)

#: What turns the rest of a clause into something the request does NOT want. Reading a
#: negated phrase back as a deliverable is the one failure mode worse than silence: this
#: sentence sits closest to generation and says "deliver each one", so it does not merely
#: miss a constraint, it reverses one. Applied to all three extractors rather than to the
#: noun phrases alone, because "do not exclude LongBench" inverts just as badly.
#:
#: Every cue negates a DIRECTIVE. A bare "not" is not here, and that is the whole lesson
#: of the list: the 2026-09-04 brief asks for "benchmarks we have not run, excluding
#: LongBench, ..." - a qualifier on what to include, standing before the request's real
#: exclusion clause. A cue that fired there would silence the exclusions the request
#: actually stated, which is the same inversion in the other direction. The hyphen in the
#: boundaries stays for the same reason: no compound may form a cue, and ``do-not-bother
#: list`` is an artefact this brief asks for by name.
_NEGATION_RE = re.compile(
    r"(?<![\w-])(?:do\s+not|does\s+not|don't|doesn't|no\s+need|without|avoid|omit|skip|"
    r"never|rather\s+than|instead\s+of)(?![\w-])",
    re.I,
)

#: What makes a range a scale to score ON rather than a number of items to deliver.
#: Looked for in the clause before the range: "score candidates 1-5 on relevance" and
#: "shortlist 15-25 benchmarks" are otherwise the same shape, and a request carrying both
#: hands the second one's requirement to the model under the first one's numbers.
#: Hyphen-aware for the reason ``_NEGATION_RE`` is: "large-scale" is not a scoring scale.
_SCALE_RE = re.compile(
    r"(?<![\w-])(?:scor(?:e|es|ed|ing)|rat(?:e|es|ed|ing)|scale|weight(?:s|ed)?|rubric|out\s+of)(?![\w-])",
    re.I,
)

#: How far back a scale marker reaches. A scale is introduced immediately before its own
#: range - "score 1-5", "score candidates 1-5", "on a scale of 1-5" - so the lookback is a
#: few words rather than the whole clause. The asymmetry with negation is deliberate: a
#: prohibition governs the list it introduces and so scopes to the clause, while a scale
#: marker earlier in the same sentence says nothing about a later range. "Score candidates
#: 1-5 on relevance, then shortlist 15-25 benchmarks" asks for 15 to 25 of them, and
#: suppressing that would hide a real requirement instead of an invented one.
_SCALE_LOOKBACK_WORDS = 4

#: A noun that makes a range a rating of things rather than a count of them, when it
#: stands between the numbers and the counted noun: "4-5 star benchmarks" rates the
#: benchmarks, it does not ask for four or five. Checked against that span alone and not
#: the wider tail, because "15-25 benchmarks scored 1-5" carries its rubric after the
#: counted noun and is a real count of fifteen to twenty-five.
_RATING_NOUN_RE = re.compile(
    r"(?<![\w-])(?:star|stars|point|points|grade|grades|tier|tiers|out\s+of)(?![\w-])",
    re.I,
)

#: A range is a count only when a counted noun follows it. A plural English word, with at
#: most two modifiers in between, so "15-25 long-context benchmarks" counts. English only
#: and deliberately so: a request in another language yields nothing here, which is the
#: silent direction rather than the inverted one.
_COUNTED_NOUN_RE = re.compile(r"^\s*(?:[\w-]+\s+){0,2}?([A-Za-z]{3,}s)\b")

#: A word that cannot begin a counted noun phrase. Without this "1-5 on three axes" reads
#: as five items, because "axes" is plural and nothing else in the tail says otherwise.
_PREPOSITION_RE = re.compile(r"^\s*(?:on|of|in|for|by|with|from|per|across|at|to|and|or|is|are)\b", re.I)

#: What introduces a list of things the request rules out.
_EXCLUDE_RE = re.compile(
    r"\b(?:excluding|exclude|excludes|except for|except|not including|other than|"
    r"already (?:run|tried|covered)|leaving out)\b[:\s]*(?P<body>[^.;\n]{1,200})",
    re.I,
)

#: The exclusion cues above, as a cue set on their own. The two guards below ask different
#: questions and so read different lists, which is what the first form of this got wrong:
#: "does this span rule a deliverable out" has to include every word the module already
#: treats as an exclusion ("exclude a comparison table", "any format other than a
#: comparison table"), while "does this span introduce a list of excluded names" is what
#: those words are FOR and may only be suppressed by a directive negation ("do not
#: exclude LongBench"). One list for both makes the exclusion extractor suppress itself.
_EXCLUDE_CUE_RE = re.compile(
    r"\b(?:excluding|exclude|excludes|except for|except|not including|other than|"
    r"already (?:run|tried|covered)|leaving out)\b",
    re.I,
)

#: How a list of names is separated inside an exclusion clause.
_LIST_SPLIT_RE = re.compile(r"\s*(?:,|/|;|\band\b|\bor\b)\s*", re.I)

#: A word that carries no name: articles and counters a request puts around its list.
_LIST_NOISE_RE = re.compile(r"^(?:the|a|an|any|all|those|these|our|its|of|from)\s+", re.I)

#: An item is a name when it carries an upper-case letter or a digit somewhere. Matching a
#: shape rather than a leading capital keeps ``xRAG`` and ``arXiv`` whole - an earlier form
#: of this rule anchored on the first character and quoted ``xRAG`` back as ``RAG``, which
#: is a checklist telling the model about a benchmark that does not exist. Prose the
#: request puts between its names ("the four we already ran") carries neither and is
#: dropped, which is the silent direction.
_NAME_SHAPE_RE = re.compile(r"[A-Z0-9]")

#: An artefact the request names: "a do-not-bother list", "a community-convention table",
#: "an appendix". At most three modifiers, so a whole clause cannot become an artefact.
_ARTEFACT_RE = re.compile(
    r"\b(?:a|an|one)\s+((?:[\w-]+\s+){0,3}?"
    r"(?:list|table|matrix|appendix|section|checklist|shortlist|ranking|breakdown|rubric))\b",
    re.I,
)

#: Artefacts that name the answer itself rather than a part of it. A request always asks
#: for "a report", and repeating that back is noise.
_ARTEFACT_STOPWORDS = frozenset({"report", "answer", "reply", "response", "summary"})

_MAX_EXCLUSIONS = 12
_MAX_ARTEFACTS = 6


@dataclass(frozen=True)
class BriefConstraints:
    """The parts of a request a parser can settle, as written."""

    count: tuple[int, int] | None = None
    exclusions: tuple[str, ...] = ()
    artefacts: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.count or self.exclusions or self.artefacts)


def _clause_start(task: str, at: int) -> int:
    """Where the clause containing ``at`` begins."""
    start = 0
    for boundary in _CLAUSE_SPLIT_RE.finditer(task[:at]):
        start = boundary.end()
    return start


def _negated(task: str, at: int) -> bool:
    """Whether a directive negation stands between the start of the clause and ``at``."""
    return _NEGATION_RE.search(task[_clause_start(task, at) : at]) is not None


def _clause_limit(task: str, at: int) -> int:
    """Where the clause containing ``at`` ends."""
    boundary = _CLAUSE_SPLIT_RE.search(task[at:])
    return at + boundary.start() if boundary else len(task)


def _excluded_names(task: str, start: int) -> tuple[list[str], int]:
    """The names an exclusion directive lists from ``start``, and how far it reaches.

    An exclusion governs what it names and stops there, and how far that is depends on
    what kind of thing it names first - which is the only signal available without a
    vocabulary of words that begin a new demand, and vocabularies of that sort are what
    keep failing in this module.

    * **A list of names.** "excluding LongBench, LooGLE and ZeroSCROLLS, with a
      do-not-bother list" takes name-shaped items and stops at the first one that is not,
      so the artefact the request goes on to ask for is outside the exclusion's reach.
      Letting it run to the end of the clause instead swallowed the next demand as a name
      ("nothing from 15-25 benchmarks") and then, once these spans also gate deliverables,
      deleted the request's own artefacts.
    * **A described thing.** "Exclude a comparison table from the report" names no
      name-shaped item at all - what it rules out IS a noun phrase - so it reaches to the
      end of its clause, and the artefact extractor must not offer that table back.

    Names listed after prose are lost with the first rule. That is a miss, and a miss is
    the direction this module is allowed to fail in.
    """
    limit = _clause_limit(task, start)
    names: list[str] = []
    pos, end = start, start
    while pos < limit:
        separator = _LIST_SPLIT_RE.search(task[pos:limit])
        item_end = pos + separator.start() if separator else limit
        cleaned = _LIST_NOISE_RE.sub("", task[pos:item_end].strip(" .,;:()")).strip()
        if not cleaned or not _NAME_SHAPE_RE.search(cleaned) or len(cleaned.split()) > 4:
            # Nothing name-shaped yet: this directive rules out a described thing, and
            # its reach is the clause rather than a list.
            return (names, limit) if not names else (names, end)
        if cleaned not in names:
            names.append(cleaned)
        end = item_end
        if not separator:
            break
        pos += separator.end()
    return names, end


def _exclusion_spans(task: str) -> list[tuple[int, int]]:
    """Each exclusion directive as the span it governs: the cue plus the list it names."""
    spans: list[tuple[int, int]] = []
    for match in _EXCLUDE_CUE_RE.finditer(task):
        if _negated(task, match.start()):
            continue
        _, end = _excluded_names(task, match.end())
        spans.append((match.start(), end))
    return spans


def _ruled_out(task: str, at: int) -> bool:
    """Whether the clause rules out whatever sits at ``at`` as a deliverable.

    Wider than ``_negated`` by the exclusion cues, because a request rules a deliverable
    out with the same words it uses to rule a name out, and this module already holds that
    vocabulary for the exclusion extractor. Reading it here too is the difference between
    "exclude a comparison table" being honoured and being quoted back as a deliverable.
    """
    if _NEGATION_RE.search(task[_clause_start(task, at) : at]):
        return True
    return any(lo <= at < hi for lo, hi in _exclusion_spans(task))


def _scored_on(task: str, at: int) -> bool:
    """Whether the words just before the range at ``at`` introduce it as a scale."""
    words = task[_clause_start(task, at) : at].split()
    return _SCALE_RE.search(" ".join(words[-_SCALE_LOOKBACK_WORDS:])) is not None


def _count_of(task: str) -> tuple[int, int] | None:
    """The first range a counted noun follows, read as a number of deliverables.

    Order in the sentence decides nothing. An earlier form took the first range that was
    not a size, so "score candidates 1-5 on relevance, then shortlist 15-25 benchmarks"
    handed the model "1 to 5 items" - a scale reported as the deliverable count, arriving
    in the sentence closest to generation and contradicting the request's real number.
    """
    for match in _COUNT_RE.finditer(task):
        low, high = int(match.group(1)), int(match.group(2))
        if not 0 < low < high:
            continue
        tail = task[match.end() :]
        if _SIZE_UNIT_RE.match(tail) or _PREPOSITION_RE.match(tail):
            continue
        counted = _COUNTED_NOUN_RE.match(tail)
        if not counted or _RATING_NOUN_RE.search(counted.group(0)):
            continue
        if _ruled_out(task, match.start()) or _scored_on(task, match.start()):
            continue
        return low, high
    return None


def _exclusions_of(task: str) -> tuple[str, ...]:
    """Names the request rules out, in the order it names them.

    Read through ``_excluded_names``, which splits the clause into items and keeps or
    drops each whole rather than scanning for name-shaped substrings: a scan returns the
    capitalised tail of a name it does not recognise, and a checklist that quotes half a
    name is worse than one that says nothing.
    """
    out: list[str] = []
    for match in _EXCLUDE_CUE_RE.finditer(task):
        if _negated(task, match.start()):
            continue
        for name in _excluded_names(task, match.end())[0]:
            if name not in out:
                out.append(name)
    return tuple(out[:_MAX_EXCLUSIONS])


def _artefacts_of(task: str) -> tuple[str, ...]:
    """Artefacts the request names, as noun phrases, in the order it names them."""
    out: list[str] = []
    for match in _ARTEFACT_RE.finditer(task):
        if _ruled_out(task, match.start()):
            continue
        phrase = " ".join(match.group(1).split())
        head = phrase.rsplit(" ", 1)[-1].lower()
        if head in _ARTEFACT_STOPWORDS or phrase.lower() in _ARTEFACT_STOPWORDS:
            continue
        # A bare "a table" says nothing a reader can check for; only a named one does.
        if " " not in phrase:
            continue
        if phrase.lower() not in {p.lower() for p in out}:
            out.append(phrase)
    return tuple(out[:_MAX_ARTEFACTS])


def read_brief(task: str) -> BriefConstraints:
    """The machine-readable constraints in one request. Empty when there are none."""
    text = task or ""
    return BriefConstraints(count=_count_of(text), exclusions=_exclusions_of(text), artefacts=_artefacts_of(text))


def render_checklist(task: str) -> str:
    """One sentence restating the request's checkable parts, or an empty string.

    Written as a reading rather than as an order - "read literally, this asks for" - so a
    misread is visibly a misread of the request and not a rule the harness invented. The
    escape clause matters as much as the list: an item that turns out not to apply is
    named in `## Limitations`, which keeps a wrong extraction from forcing a wrong report.
    """
    brief = read_brief(task)
    if not brief:
        return ""
    parts: list[str] = []
    if brief.count:
        parts.append(f"{brief.count[0]} to {brief.count[1]} items")
    if brief.exclusions:
        parts.append("nothing from " + ", ".join(brief.exclusions))
    parts.extend(f"a {phrase}" for phrase in brief.artefacts)
    return (
        " Read literally, this message asks for: "
        + "; ".join(parts)
        + ". Deliver each one, or name it in `## Limitations` and say why it is absent."
    )
