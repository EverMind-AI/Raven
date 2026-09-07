"""Deterministic research trail appended to a product answer (dr@2.8).

An auditable report needs a record of how the answer was reached. The obvious
way - asking the model in the contract to describe its search strategy - buys a
*self-report*: unverifiable, competing with evidence for the context window, and
free to be wrong in the direction that flatters the turn. This project has already
paid for trusting one: a bench tool-surface stamp that was a probe rather than an
enumeration, so it missed tools nobody happened to call.

The real trail is already on disk, written at the moment each event happened. So
the body of the report is written by the model and this appendix is computed from
the ledger: zero model tokens, zero room to invent, and every line checkable
against the record.

**It is display-only, and that is load-bearing.** The ledger's contract is that
nothing reads it at run time, because an instrument that can steer what it
measures is not an instrument. Reading it here does not break that: this runs
after the turn's last generation, the text is attached to the value returned to
the caller and not to the persisted message, so the model never sees it - not in
this turn, and not as history in the next one. Any future caller that wants this
in the transcript is proposing a different change with a different risk.

The one number here that is not bookkeeping: **cited-but-never-opened URLs**. A
link in the answer that appears in no fetch record is a fabricated citation, and
it is detectable deterministically, without a judge and without rewarding length.

**But it is post-treatment, and it has a gaming channel.** Citing less raises it;
an answer with three careful citations scores above one with twelve, and an answer
citing nothing is excluded entirely. So three rules travel with it and are not
optional:

* Never report ``citation_grounding_rate`` without ``urls_cited`` beside it, and
  without the share of answers that cited nothing. The rate alone is a ratio whose
  denominator the arm being measured chooses.
* Use it as an **intra-arm integrity guard**, never as a cross-arm quality
  scoreboard. Two arms that cite at different rates are not comparable on it, and
  the arm that browses more has more chances to mis-cite.
* Its denominator is **cited URLs, not claims**. An answer can be 100% grounded
  and entirely wrong; this measures whether the links are real, not whether the
  reasoning is.

Twin of the fork's ``raven/agent/process_appendix.py``: the same module body,
kept identical by ``tests/test_agents_research_process_appendix.py`` (both files
compared with docstrings and comments stripped), so a fix that lands on one side
cannot ship without the other. Only the prose differs: this file avoids literal
CJK and full-width characters in code and names the trunk's seams.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from loguru import logger

# Trailing punctuation is stripped because a URL at the end of a sentence or
# inside markdown parentheses is the common case, and keeping the bracket would
# make a correctly-cited page look uncited.
# ``re.I``: a model writes "HTTP://" often enough, and a citation this regex
# does not extract is not a citation this check can call fabricated - it simply
# vanishes from both the numerator and the denominator.
#
# The stop set was ASCII-only until dr@3.0, and on a Chinese answer that is not a
# near-miss: nothing terminated the match, so a cited URL swallowed the rest of the
# clause after it. ``https://coldiq.com/blog/serper-pricing`` followed directly by a
# full-width parenthesis and a source note - one token, in full-width punctuation -
# matched no fetch record, and a page the run really had opened was reported as a
# fabricated citation. Fixed direction, and it fires only on
# Chinese output, which is this product's entire surface. Multi-turn made it visible
# rather than causing it: the research memo hands later turns more URLs to cite, so
# what used to be a rare line became four flagged citations in an eight-turn run.
#
# CJK ideographs are NOT excluded. They were until 2026-08-26, on the theory that a
# reader service hands back percent-encoded paths so a literal ideograph must be prose.
# The theory ignored the citing side: models decode the escapes for readability, so
# ``https://zh.wikipedia.org/wiki/<ideographs>`` is a routine citation of a page fetched as
# ``.../wiki/%E6%9D%8E...``. Truncating at the ideograph made every such citation a
# false "never opened" - and two different pages truncate to the SAME prefix, so the
# accusation multiplied. The residual ambiguity runs the other way now: prose glued
# directly onto a URL without punctuation is swallowed into it and fails the match.
# That stays a false alarm rather than a false pass, which is the direction this
# check must fail in (see the prefix-matching note in ``build_trail``).
_CJK = (
    "\u3000-\u303f"  # CJK punctuation: , . ; : and the bracket family
    "\uff00-\uffef"  # fullwidth forms: ( ) , : ; ! ?
    "\u2014\u2026\u00b7"  # em dash, ellipsis, middle dot: outside both blocks
)
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]`" + _CJK + r"]+", re.I)
# ★ 20260901 (Framework, report-comparison audit). Disclosure only, and the word
# "only" is the whole design. A product run was observed writing every one of its
# 43 references as a bare host - ``kyutai.org/blog/2026-04-28-arc-encoder/`` - so
# ``_URL_RE`` above matched nothing, ``urls_cited`` was 0, and the grounding check
# correctly reported that it did not apply. The appendix then said "nothing to
# check" over an answer that had read 49 pages, which is true and useless.
#
# The fix people reach for is to widen ``_URL_RE`` to accept a missing scheme. That
# is refused here: ``citation_grounding_rate`` has readings on disk, and widening
# its extractor moves that number's denominator while its name stays put - the
# failure this repo has logged more often than any other. So this second pattern
# feeds a *separate* counter that never touches ``cited``, the rate, or either
# ``cited_*`` list. It answers one question and no other: when the check found no
# links, was that because the answer cited nothing, or because it cited in a shape
# the check cannot read? Those two need opposite responses - a prompt fix versus an
# integrity alarm - and one number could not tell them apart.
#
# The TLD list is closed, and the direction of its error is the point. A TLD it
# lacks means this counter undercounts, and the appendix falls back to the generic
# "nothing to check" - exactly today's behaviour, so a miss costs nothing new. An
# over-broad list turns paths this repo writes constantly (``pipeline/lib/isolate.sh``,
# ``notes/README.md``) into cited sources, and a disclosure counter with false
# positives gets ignored, which costs the whole mechanism. ``sh`` and ``md`` are real
# TLDs and are absent for that reason.
#
# A path separator is required. Without it every ``Fig.2`` and ``v1.5`` is a host,
# and a disclosure counter that cries wolf gets ignored, which costs more than not
# having it. The lookbehind keeps it off the tail of a scheme-ful URL that
# ``_URL_RE`` already owns, and off e-mail addresses.
_SCHEMELESS_RE = re.compile(
    r"(?<![\w/:.@-])"
    r"((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:com|org|net|io|ai|edu|gov|dev|co|info|blog|uk|cn|de|fr|jp|eu|xyz|tech|cloud)"
    r"/[^\s<>\"'\)\]`" + _CJK + r"]*)",
    re.I,
)
# ★ 20260902 (Framework, product feedback). The third disclosure shape, and the one
# the 20260901 audit measured as dominant: 75 of one product run's 87 citation
# handles were ``"<claim>" (web_fetch #cd8187b0)`` - the security fence's nonce,
# the shortest token in context that reads like a handle. The report templates now
# say out loud that the fence tag is not a citation, but a template asks and a
# model sometimes does otherwise, and when it does the grounding check reported
# "nothing to check" over an answer whose every reference pointed at a page the
# run really had opened - the false-green this module exists to prevent.
#
# Disclosure ONLY, like ``_SCHEMELESS_RE`` above and for the same reason: the
# nonce is minted by ``wrap_untrusted`` at context-assembly time and never reaches
# the ledger, so a tag cannot be resolved to a URL here - by design, a security
# boundary is not a citation index. Feeding these into ``cited`` would put
# unresolvable strings under ``citation_grounding_rate``'s unchanged name. So the
# counter answers one question: when the check found no links, did the answer cite
# fence tags instead? That is a generation-side formatting failure with a page
# behind every reference, and it must not wear the costume of "cited nothing".
#
# The id is 4-16 hex characters rather than exactly the 8 ``token_hex(4)`` mints,
# because the model is quoting from context and a truncated quote of a fence tag
# is still a fence tag, not a citation this check could suddenly read.
_FENCE_TAG_RE = re.compile(r"\b(web_fetch|web_search)\s*#([0-9a-f]{4,16})\b", re.I)
# Backtick and asterisk join the trailing set for the markdown case (`url`, **url**),
# which the character class above cannot catch for the asterisk without banning it
# from paths outright.
_TRAILING = ".,;:!?`*"

# ★ 20260902 (Framework, product feedback). The verify outcomes whose shipped
# answer carries NO verdict at all. ``budget_spent_reviewed`` is deliberately
# absent: its whole point is that the shipped draft WAS reviewed for the record.
# A ``None`` outcome is also not here - an arm with no reviewer configured made a
# config choice, and a banner on every such run would cry wolf until ignored.
_UNREVIEWED_OUTCOMES = {
    "budget_spent": "the revision budget was spent before any review ran",
    "unavailable": "the reviewer was unavailable and the gate failed open",
}

_MAX_LISTED = 40
# Below this a fetch returned a redirect stub, an empty JSON, or an error page.
# Observed: a run guessing a ClickBench path got 199 and 203 characters back before
# listing the directory and finding the real file.
_THIN_PAGE_CHARS = 400


def _clean(url: str) -> str:
    return url.rstrip(_TRAILING)


# ── two addresses that are one page ───────────────────────────────────
# ★ 20260906 (Framework, product feedback). Both folds serve the same check:
# ``cited_never_surfaced``, the module's heaviest accusation. It fires on a
# citation the run never saw, so every form of an address the run DID see has to
# reach it as that address, or the appendix calls a correctly-read page
# fabricated. The 2026-09-04 run supplied both misses.

# arXiv serves one paper at four path prefixes, so a run that opened the abstract
# and an answer that cites the PDF - routine now that a thin abstract is re-read
# at its PDF - differ only in the parts that do not name the paper. A trailing
# ``.pdf`` comes off with the prefix. The id itself is non-greedy so an old-style
# ``cs/0501001`` keeps its slash.
#
# ★ 20260907 (review). A pinned ``vN`` is KEPT. arXiv's own version documentation
# says each version stays retrievable and may carry corrections, expanded content,
# a translation, or content that changed entirely, so ``2401.12345v1`` and
# ``2401.12345v2`` are two documents rather than two addresses of one. Erasing the
# version let the grounding check pass a citation to content the run never read,
# which is the one thing this check may not do. A bare address and a pinned one are
# distinct for the same reason - see the note above ``_match_form``.
_ARXIV_PAPER_RE = re.compile(r"^(?:abs|pdf|html|format)/(?P<id>.+?)(?P<version>v\d+)?(?:\.pdf)?$")

# The forges resolve an owner and a repository case-insensitively and redirect to
# the owner's own casing, so ``github.com/Microsoft/LLMLingua`` and
# ``github.com/microsoft/llmlingua`` are one page. Only the naming segments fold:
# a path INSIDE a repository is served case-sensitively, and folding it would let
# a fabricated ``blob/main/RESULTS.md`` absolve itself against a real
# ``blob/main/results.md`` - which is the fabrication this check exists to catch.
_FORGE_NAME_SEGMENTS = {
    "github.com": 2,
    "bitbucket.org": 2,
    "huggingface.co": 2,
}
# On the Hub only a model sits at the bare pair; a dataset or a space carries its
# kind first, so the pair that names the artefact sits one segment deeper.
_HF_KIND_SEGMENTS = {"datasets", "spaces", "models"}

# ★ 20260907 (review). GitLab is not in the table above because its namespace
# nests: ``gitlab.com/gitlab-org/quality/testcases`` is one project three segments
# deep, and folding a fixed two left ``TestCases`` cased and had the check accuse
# a page the run had opened. GitLab does publish the boundary - everything before
# ``/-/`` is the project path, everything after is served from inside it - so the
# separator decides the depth instead of a constant.
_GITLAB_PROJECT_SEPARATOR = "/-/"

_QUERY_RE = re.compile(r"[?#]")


def _fold_gitlab(head: str) -> str:
    """A GitLab path with its project part folded and the rest left alone.

    With ``/-/`` present the split is exact. Without it the whole path is the
    project path on current GitLab, so it all folds; that does mis-fold a
    pre-2019 in-project URL (``/group/project/blob/master/File.txt``), which is
    the direction that marks a real citation opened rather than accusing one, and
    those URLs no longer appear in a page this agent can fetch today.
    """
    project, separator, inside = head.partition(_GITLAB_PROJECT_SEPARATOR)
    return project.lower() + separator + inside


def _fold_path(host: str, path: str) -> str:
    """The path with the parts that do not name the document folded away.

    Query and fragment are held out of both folds and reattached unchanged: they
    carry a document's identity (``?id=2`` is a different page, which is why
    ``_norm`` keeps them), and lower-casing a value inside one would fold two
    documents into one on the side that must not fold.
    """
    m = _QUERY_RE.search(path)
    head, tail = (path[: m.start()], path[m.start() :]) if m else (path, "")
    if host == "arxiv.org":
        paper = _ARXIV_PAPER_RE.match(head)
        if paper:
            return f"abs/{paper.group('id')}{paper.group('version') or ''}" + tail
        return head + tail
    if host == "gitlab.com":
        return _fold_gitlab(head) + tail
    depth = _FORGE_NAME_SEGMENTS.get(host)
    if depth is None:
        return path
    segments = head.split("/")
    if host == "huggingface.co" and segments[0].lower() in _HF_KIND_SEGMENTS:
        depth += 1
    return "/".join([s.lower() for s in segments[:depth]] + segments[depth:]) + tail


# ★ 20260907 (review, second round). There is deliberately NO bare-versus-pinned
# exception here. A bare arXiv address serves the latest revision, and this ledger
# records the URL a fetch REQUESTED rather than the revision that address resolved
# to - so opening ``/abs/X`` while the paper stands at v3 proves nothing about a
# citation to ``v1``, and opening ``v1`` does not make a bare citation, which sends
# the reader to v3, the same document. Neither direction establishes identity, so
# both stay distinct. The equivalence that remains is the one that is provable from
# the address alone: the four path prefixes and a trailing ``.pdf``, with the
# version pinned or absent on both sides alike. Re-enabling the match needs the
# effective revision in the ledger, not a rule about which form is more general.


def _norm(url: str) -> str:
    """Fold the differences a reader service introduces, and nothing more.

    Scheme, host case and one trailing slash: a page fetched as ``https://X/a/``
    and cited as ``http://x/a`` is the same page. Query strings and fragments are
    kept, because ``?id=2`` is a different document. Percent-escapes are decoded
    on both sides, because a page is fetched as ``.../wiki/%E6%9D%8E...`` and
    cited in its decoded form - raw equality would accuse every such citation.
    Decoding does fold ``a%2Fb`` with ``a/b`` (and ``%23`` with ``#``), a known
    tension with keeping query/fragment distinctions; accepted because a false
    fold marks a real citation opened, the harmless direction.

    The path is then handed to ``_fold_path``, which folds the parts of two named
    hosts' addresses that do not name the document - see it for why those two and
    why only those parts.
    """
    u = unquote(_clean(url).strip())
    for prefix in ("https://", "http://"):
        if u.lower().startswith(prefix):
            u = u[len(prefix) :]
            break
    host, _, rest = u.partition("/")
    host = host.lower().removeprefix("www.")
    return f"{host}/{_fold_path(host, rest)}".rstrip("/")


_ARXIV_REVISION_SUFFIX_RE = re.compile(r"^v\d+$")


def _arxiv_revision_of(cited: str, opened: str) -> bool:
    """Whether ``opened`` is ``cited`` plus nothing but an arXiv version suffix.

    The unique-prefix rule below exists for a path the model truncated when citing, and
    a version suffix is not a truncation of a path: ``abs/X`` and ``abs/Xv2`` are two
    revisions, kept apart deliberately. Without this guard the prefix rule would quietly
    restore the very match the version rule removes, because ``abs/X`` is a strict string
    prefix of ``abs/Xv2`` and nothing else about it looks unusual.
    """
    if not cited.startswith("arxiv.org/"):
        return False
    return _ARXIV_REVISION_SUFFIX_RE.match(opened[len(cited) :]) is not None


def _match_form(url: str, known: set[str]) -> str | None:
    """The form of ``url`` that ``known`` holds, or ``None``.

    Tried as written first. On a miss, trailing ideographs come off one at a time:
    the extractor keeps ideographs (a model cites ``.../wiki/<ideographs>`` in decoded
    form), so prose glued straight onto a URL is swallowed into the match, and the
    only way to tell glue from path is whether a fetch record exists for the
    shorter form. Never trims an ASCII character on that pass, so the extraction
    every published reading was measured on cannot move.

    Two more recoveries run after that, both set-aware (they rewrite the cited
    form only when a fetch record exists for the result, so they cannot invent a
    match):

    * a ``,``/``;`` tail comes off whole - a model citing ``(url,2026-01-09)`` or
      gluing two citations with ``;`` hands the extractor a comma-joined string,
      and both separators are rare inside real paths but routine in citation
      prose;
    * a cited URL that is a strict prefix of exactly ONE opened page matches that
      page - observed live as a long ideograph path the model truncated when
      citing. Unique-prefix only: two opened pages sharing the prefix leave the
      citation ambiguous, and the check must accuse rather than guess. A path
      floor keeps bare-domain and short-segment citations out - a string prefix
      is not a path prefix, and the fabrication this check exists to catch lives
      in the short segments.
    """
    u = url
    while True:
        if _norm(u) in known:
            return u
        if u and "\u4e00" <= u[-1] <= "\u9fff":
            u = u[:-1]
            continue
        break
    clipped = re.split(r"[,;]", url, maxsplit=1)[0]
    if clipped != url and _norm(clipped) in known:
        return clipped
    n = _norm(clipped)
    host, _, path = n.partition("/")
    # The path floor keeps this out of short-segment territory, where a string
    # prefix is not a path prefix ("/a" would absolve itself against "/about").
    if len(path) >= 8:
        prefixed = [k for k in known if k.startswith(n) and k != n and not _arxiv_revision_of(n, k)]
        if len(prefixed) == 1:
            return clipped
    return None


@dataclass
class ResearchTrail:
    """What the ledger says happened, plus the one integrity check."""

    searches: int = 0
    distinct_queries: list[str] = field(default_factory=list)
    replays: int = 0
    zero_hit: int = 0
    pages: list[tuple[str, int, bool]] = field(default_factory=list)  # url, chars, ok
    verify_outcome: str | None = None
    verify_model: str | None = None
    """Which model reviewed, off the verify rows. A mode may move the reviewer per
    session, so the trail names it: a reader comparing two runs' verdicts needs
    to know whether the same reviewer gave them."""
    salvaged: bool = False
    """The shipped answer is a salvage synthesis, which the reviewer never sees
    (observer order, ``flow.py``). Rendered so an unreviewed answer does not wear
    the same trail as a reviewed one, and counted so the salvage share of any
    arm is measurable next to ``verify`` outcomes."""
    unsupported: list[str] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)
    cited_not_opened: list[str] = field(default_factory=list)
    cited_never_surfaced: list[str] = field(default_factory=list)
    """The subset of ``cited_not_opened`` that no search ever returned either.

    ★ 20260828 (Framework, product-surface audit). A strict subset, added because
    the one line in this appendix that accuses the answer could not tell its two
    causes apart. "Cited a link the search listed but never opened" breaks the
    contract's first rule - answer from fetched pages, never from the listing -
    and the reader can still go read it. "Cited a link that appears nowhere in
    this run at all" is a fabricated citation. One warning covered both, in the
    same words, and the second is the only one worth interrupting a reader for.

    The ledger always carried what was needed: ``web.py``'s search rows write an
    ordered ``urls`` list, and the fold simply discarded it.

    ``cited_not_opened`` is deliberately left alone, because
    ``citation_grounding_rate`` is computed from it and has readings on disk.
    Splitting the number would have moved a published metric while its name
    stayed put - this repo's most repeated failure."""
    span_seconds: float | None = None
    """Wall-clock from the turn's first ledger row to its last, or ``None`` for one row.

    ★ 20260901 (Framework). Derived, not instrumented: every ledger row already carries
    ``ts``, so this needed no new plumbing and cannot fail to be written for a run that
    did any research at all.

    **Caliber, and it is not the turn's wall clock.** It spans the first research event
    to the last, so it excludes the final generation that happens after the last tool
    call - measured against one product run's own header, 1,815s here against 1,884s for
    the turn, an undercount of 69s. Naming it ``research_seconds`` rather than
    ``elapsed`` is the point: a number that gets compared against a turn duration would
    make the gap look like a discrepancy instead of a definition.

    It exists because the product surface has no wall-clock budget of any kind, and
    one observed product question spent 31m24s. A caller that can see the number can
    set a timeout at the process level, which is the only place a timeout is safe here.

    ⚠️ A deadline INSIDE the loop is deliberately not built. Stopping research at a clock
    and then asking for an answer is the shape this repo has already measured: replaying
    25 genuinely stuck questions through a repaired forced-finalize produced content on
    23 of them, hit gold on **0**, and every one read as a confident wrong answer. It
    converts a detectable zero into an undetectable error, and it removes the trigger the
    best-measured lever in the project (conditional rerun on a dud) depends on. So the
    number is surfaced and the decision is left outside."""

    cited_schemeless: list[str] = field(default_factory=list)
    """Scheme-less references in the answer that ``_URL_RE`` structurally cannot see.

    ★ 20260901 (Framework). Never folded into ``cited``, and never into the
    grounding rate - see ``_SCHEMELESS_RE``. Its only job is to give the
    "no links were cited" line a cause, so a formatting problem in the answer
    stops looking like an answer that cited nothing."""

    cited_fence_tags: list[str] = field(default_factory=list)
    """Distinct fence tags cited in the answer, in their own words (``web_fetch #cd81...``,
    ``web_search #aa11...``): the tool name travels with the id, so the rendered line
    quotes what the answer wrote and can never claim a ``web_search`` tag was a
    ``web_fetch`` - an integrity appendix that misstates the event it discloses
    would be its own counterexample.

    ★ 20260902 (Framework, product feedback). Never folded into ``cited`` and never
    into the rate - see ``_FENCE_TAG_RE``. A fence tag has a fetched page behind it
    that this module structurally cannot name (the nonce never reaches the ledger),
    so its job is the same as ``cited_schemeless``'s: give the "no links were
    cited" line its cause, so a report whose every reference points at a really
    opened page stops reading as one that cited nothing."""

    opened_earlier: int = 0
    """How many URLs the grounding check accepted on an earlier turn's authority.

    Reported because it widens the check's denominator, and a check whose scope can
    change without saying so is one nobody can compare across runs. ``0`` on every
    single-turn run, which is every measured arm."""

    def _page_split(self) -> tuple[list[tuple[str, int, bool]], int, int]:
        """``(substantive, thin, failed)``.

        ★ 20260901 (Framework). Extracted so ``counters()`` and ``render()`` cannot
        drift: the thin-page count was rendered from dr@2.8 on but never emitted as a
        counter, so the one number that says "this fetch returned a stub, not a page"
        was human-readable and machine-invisible. Two implementations of the same
        split is the shape this repo keeps paying for; one is enough."""
        substantive = [p for p in self.pages if p[2] and p[1] >= _THIN_PAGE_CHARS]
        opened_ok = sum(1 for _, _, ok in self.pages if ok)
        return substantive, opened_ok - len(substantive), len(self.pages) - opened_ok

    def counters(self) -> dict[str, Any]:
        opened_ok = sum(1 for _, _, ok in self.pages if ok)
        _, thin, failed = self._page_split()
        return {
            "emitted": True,
            "searches": self.searches,
            "distinct_queries": len(self.distinct_queries),
            "replays": self.replays,
            "zero_hit_searches": self.zero_hit,
            "pages_opened": len(self.pages),
            "pages_ok": opened_ok,
            # ★ 20260901 (Framework). Rendered since dr@2.8, emitted only now.
            # ``ok`` is a transport verdict, not a content one, and every downstream
            # fetch-productivity reading was computed over ``pages_ok``.
            #
            # ⚠️ The threshold is deliberately NOT moved here, and the reason is worth
            # keeping: the motivating observation was four OpenReview forum fetches in
            # one product run that each returned exactly 440 characters of JavaScript
            # shell - and 440 is **above** ``_THIN_PAGE_CHARS``, so this counter does
            # not catch them. It catches the 199-to-391-character stubs it was
            # calibrated on. Raising the bar to 600 would cover that run (9 thin pages
            # becomes 13) on the evidence of a single trajectory, which is how this
            # repo has twice adopted a direction that reversed on the next batch.
            #
            # The signal that would actually catch a JS shell is not length at all: it
            # is **four different URLs returning byte-identical lengths**, which no
            # threshold can express. Left unbuilt on purpose - it is a new mechanism on
            # n=1 evidence, and this change set is about making existing numbers stop
            # lying, not about adding detectors.
            "thin_pages": thin,
            "pages_failed": failed,
            "urls_cited": len(self.cited),
            # The integrity number. Null-safe on purpose: a rate over zero
            # citations is not 1.0, it is undefined, and reporting 1.0 would make
            # an answer that cites nothing look perfectly grounded.
            "cited_not_opened": len(self.cited_not_opened),
            # Additive, and a strict subset of the key above, so every landed
            # reading of ``cited_not_opened`` and of the rate keeps its meaning.
            "cited_never_surfaced": len(self.cited_never_surfaced),
            "citation_grounding_rate": (
                round(1 - len(self.cited_not_opened) / len(self.cited), 4) if self.cited else None
            ),
            # Emitted so the batch-level "share of answers that cited nothing" is
            # computable without re-deriving it from a null. The rate is a ratio
            # whose denominator the measured arm chooses, so it may not be read
            # without this and ``urls_cited`` next to it.
            "cites_nothing": not self.cited,
            # ★ 20260901 (Framework). The two keys below split ``cites_nothing`` by
            # cause, because it had two and they need opposite responses.
            #
            # ``read_but_cited_nothing``: pages were opened and the answer names no
            # link at all. This is the state in which the grounding check reports
            # that it did not apply - correct, and easy to read as "passed" when
            # scanned in a table of runs. It is emitted so a batch can count how
            # often its integrity check was inapplicable rather than satisfied.
            #
            # ``cited_schemeless``: how many references the answer wrote in a shape
            # ``_URL_RE`` cannot see. Non-zero next to ``urls_cited: 0`` means the
            # answer did cite its sources and the check simply could not read them -
            # a generation-side formatting fix, not an integrity alarm.
            "read_but_cited_nothing": bool(self.pages) and opened_ok > 0 and not self.cited,
            # Rounded to whole seconds: sub-second precision on a multi-minute research
            # turn is noise that invites false comparison between runs.
            "research_seconds": None if self.span_seconds is None else round(self.span_seconds),
            "cited_schemeless": len(self.cited_schemeless),
            "cited_fence_tags": len(self.cited_fence_tags),
            # dr@3.0: non-zero means the grounding rate above was computed over this
            # conversation's fetches, not this turn's. Two runs whose scopes differ
            # are not comparable on the rate, so the scope travels with it.
            "opened_earlier": self.opened_earlier,
            "verify_outcome": self.verify_outcome,
            "verify_model": self.verify_model,
            "verify_open_points": len(self.unsupported),
            "salvaged": self.salvaged,
        }

    def render(self) -> str:
        opened_ok = sum(1 for _, _, ok in self.pages if ok)
        substantive, thin, failed = self._page_split()
        # ★ 20260901 (Framework). "N distinct" was read as "N different things were
        # looked for". It is not: the rule is whitespace/case folding, deliberately the
        # same key ``WebSearchTool.execute`` uses for its repeat cache, so that this
        # line cannot contradict the ``replay`` flags printed from the same rows. Seven
        # near-synonym rewrites of one intent all count as distinct, and calling that
        # "distinct" flatters the run. The wording changes; the rule and the
        # ``distinct_queries`` counter key do not, because that key has landed readings
        # and renaming a number while its definition stays put is how a metric loses
        # its history.
        head = f"{self.searches} searches ({len(self.distinct_queries)} unique query strings), {opened_ok} pages read"
        if thin:
            # Beside "pages read", because the number above counts the stubs too.
            head += f" ({thin} returned almost nothing)"
        if self.span_seconds is not None and self.span_seconds >= 60:
            # Minutes only, and only past a minute: a product answer that took half an
            # hour is a fact the reader is entitled to, and one that took 40 seconds is
            # not worth a clause.
            head += f", {int(self.span_seconds // 60)}m of research"
        if self.verify_outcome:
            head += f", reviewer: {self.verify_outcome}"
            if self.unsupported:
                head += f" ({len(self.unsupported)} open point"
                head += "s)" if len(self.unsupported) > 1 else ")"
            if self.verify_model:
                head += f" via {self.verify_model}"
            if self.salvaged:
                # The common salvage path runs THROUGH a verdict: reject ->
                # revision -> empty visible answer -> salvage. The verdict was
                # about a draft that never shipped, so it must not wear the
                # trail alone.
                head += ", shipped answer: salvaged (not reviewed)"
        elif self.salvaged:
            # A salvaged answer never reaches the reviewer (observer order). Saying
            # nothing here would let it wear the same trail as a reviewed turn.
            head += ", reviewer: skipped (salvaged answer)"
        lines = ["", "---", ""]
        # ★ 20260902 (Framework, product feedback). A run whose shipped answer no
        # reviewer ever saw used to disclose that as one word in the head above -
        # ``reviewer: budget_spent`` - which a reader scanning for a verdict reads
        # right past. The state changes how much the whole answer can be trusted,
        # so it leads the block instead of trailing it. The head keeps the word:
        # this banner names the state, the head stays the machine-greppable record.
        if self.salvaged:
            unreviewed = "the shipped text is a salvage synthesis the reviewer never saw"
        else:
            unreviewed = _UNREVIEWED_OUTCOMES.get(self.verify_outcome or "")
        if unreviewed:
            lines.append(f"> ⚠️ **This answer shipped unreviewed** — {unreviewed}.")
            lines.append("")
        lines.append(f"**Research trail** — {head}")
        lines.append("")

        # The grounding line, stated in both directions. Until dr@3.3 only the
        # failing direction was rendered, so a reader saw a warning when something
        # was wrong and *nothing at all* when everything checked out - which reads
        # as "this was not checked", not as "this passed". A check whose success is
        # invisible teaches its audience to treat its silence as absence.
        #
        # Always as a fraction, never as a percentage, because the module rule is
        # that this number may not appear without ``urls_cited`` beside it: the
        # denominator is chosen by the answer, so "100%" over one citation and over
        # forty are different claims wearing the same digits.
        if not self.cited:
            # Explicitly not 1.0. An answer that cites nothing has an undefined
            # grounding rate, and the sentence has to say the check did not apply
            # rather than let a missing warning imply it passed.
            # ★ 20260901 (Framework). The sentence was true and unactionable. Two
            # very different runs reach it: one that answered from nothing, and one
            # that cited every source as a bare host the extractor cannot parse. The
            # second was observed writing 43 references and reading 49 pages, and the
            # reader was told there was nothing to check. Naming the cause is the
            # whole fix; the rate itself stays undefined either way.
            # Both causes can be present in one answer, and each is rendered on its
            # own line: folding them into one sentence would make the count of one
            # explain the other.
            if self.cited_fence_tags:
                lines.append(
                    f"> ⚠️ No links were cited, but the answer references "
                    f"{len(self.cited_fence_tags)} tool-result fence tag(s) "
                    f"(e.g. `{self.cited_fence_tags[0]}`). A fence tag is a data "
                    "boundary, not a URL - the grounding check cannot resolve it "
                    "to a page. Nothing above was verified."
                )
            if self.cited_schemeless:
                lines.append(
                    f"> ⚠️ No links were cited in a checkable form, but the answer "
                    f"names {len(self.cited_schemeless)} source(s) without a URL "
                    f"scheme (e.g. `{self.cited_schemeless[0]}`), so the grounding "
                    "check could not read them. Nothing above was verified."
                )
            if not (self.cited_fence_tags or self.cited_schemeless):
                if opened_ok:
                    lines.append(
                        f"> ⚠️ {opened_ok} page(s) were read but the answer cites no "
                        "link, so nothing above can be traced to a source."
                    )
                else:
                    lines.append("> No links were cited above, so there was nothing to check.")
            lines.append("")
        elif self.cited_not_opened:
            # Stated first and in plain words: it is the one line here that says
            # something is wrong with the answer above. Two sentences rather than
            # one, because the two causes need different things from the reader -
            # a listed-but-unopened link is a link they can still go read, an
            # unseen one is a citation with nothing behind it.
            listed = [u for u in self.cited_not_opened if u not in set(self.cited_never_surfaced)]
            if self.cited_never_surfaced:
                lines.append(
                    f"> ⚠️ {len(self.cited_never_surfaced)} of {len(self.cited)} link(s) cited "
                    "above appear nowhere in this run - no search returned them and no page "
                    "was opened at them: " + ", ".join(self.cited_never_surfaced[:5])
                )
            if listed:
                lines.append(
                    f"> ⚠️ {len(listed)} of {len(self.cited)} link(s) cited above were "
                    "returned by a search but never opened, so nothing here rests on "
                    "reading them: " + ", ".join(listed[:5])
                )
            lines.append("")
        else:
            n = len(self.cited)
            lines.append(
                f"> ✓ All {n} cited link{'s' if n > 1 else ''} "
                f"{'were' if n > 1 else 'was'} opened during this research."
            )
            lines.append("")

        if self.cited and self.cited_fence_tags:
            # The scope note for the mixed case: with real URLs present the check
            # runs and may even print its ✓, and without this line that ✓ silently
            # covers references it never saw.
            lines.append(
                f"> ({len(self.cited_fence_tags)} further reference(s) are "
                f"tool-result fence tags (e.g. `{self.cited_fence_tags[0]}`), "
                "which are not URLs and could not be checked.)"
            )
            lines.append("")

        if self.cited and self.opened_earlier:
            # The scope travels with the number, for the same reason ``counters()``
            # emits it: a rate computed over the conversation and one computed over
            # the turn are not the same measurement, and a reader comparing two
            # answers has no way to tell them apart from the fraction alone.
            lines.append(
                f"> ({self.opened_earlier} of the cited links "
                f"{'were' if self.opened_earlier > 1 else 'was'} opened on an "
                "earlier turn of this conversation.)"
            )
            lines.append("")

        if self.distinct_queries:
            shown = self.distinct_queries[:_MAX_LISTED]
            lines.append("<details><summary>Queries run</summary>")
            lines.append("")
            lines += [f"- `{q}`" for q in shown]
            if len(self.distinct_queries) > len(shown):
                lines.append(f"- … and {len(self.distinct_queries) - len(shown)} more")
            lines += ["", "</details>", ""]

        if self.pages:
            # Split by whether the page actually yielded anything. Measured on the
            # first two product runs, a research turn spends several fetches finding
            # the right path - guessing a URL, getting a 200-with-nothing, listing a
            # directory, then hitting it - and listing those beside the substantive
            # reads made the appendix 26% of one answer. The dead ends still get a
            # line, because "23 pages read" over 9 real ones is the kind of number
            # that stops being audit and starts being decoration; they just do not
            # each get a URL.
            lines.append("<details><summary>Pages read</summary>")
            lines.append("")
            lines += [f"- {u} ({c:,} chars)" for u, c, _ in substantive[:_MAX_LISTED]]
            if len(substantive) > _MAX_LISTED:
                lines.append(f"- … and {len(substantive) - _MAX_LISTED} more")
            if thin:
                lines.append(f"- ({thin} fetch(es) returned almost nothing)")
            if failed:
                lines.append(f"- ({failed} page(s) could not be retrieved)")
            lines += ["", "</details>", ""]

        if self.unsupported:
            lines.append("<details><summary>Reviewer's open points</summary>")
            lines.append("")
            lines += [f"- {c}" for c in self.unsupported[:_MAX_LISTED]]
            lines += ["", "</details>", ""]

        return "\n".join(lines).rstrip() + "\n"


def read_ledger(path: str | Path) -> list[dict[str, Any]]:
    """Display-only read of one question's ledger. Never called during generation."""
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            # A torn last line is normal on an append-only file; counting it as a
            # parse failure is more useful than pretending the trail is complete.
            logger.debug("process-appendix: skipping unparseable ledger line")
    return rows


def build_trail(
    rows: list[dict[str, Any]], answer: str, opened_earlier: "Iterable[str] | None" = None
) -> ResearchTrail:
    """``opened_earlier``: URLs an earlier turn of the same conversation opened.

    dr@3.0, and it is a correction rather than a feature. The fabricated-citation
    check compares the answer's links against a fetch record, and until multi-turn
    existed those two had the same scope - one turn. The research memo then began
    handing a later turn the URLs earlier turns had read, the model cited them
    correctly, and the check called them fabricated: measured on the first
    three-turn run, turn three flagged both of turn one's pages. A per-turn
    denominator under a per-conversation numerator is a false-positive channel with
    a fixed direction, and it fires on exactly the well-behaved case - a follow-up
    that credits where a fact came from.

    Passing them in rather than reading the memo here keeps this module's rule
    intact: it still only trusts records of fetches that happened. It is simply told
    about more of them. ``pages`` is untouched, so the rendered trail still lists
    what THIS turn opened and does not claim credit for earlier reading.
    """
    t = ResearchTrail()
    seen_q: set[str] = set()
    opened: set[str] = set()
    surfaced: set[str] = set()
    for r in rows:
        op = r.get("op")
        if op == "search":
            t.searches += 1
            if r.get("replay"):
                t.replays += 1
            if r.get("zero_hit"):
                t.zero_hit += 1
            q = str(r.get("query") or "").strip()
            # Normalised exactly as ``WebSearchTool.execute`` keys its repeat cache.
            # Any other rule would let the trail call two queries distinct that the
            # tool served as a repeat - the appendix would then contradict the
            # ``replay`` flags printed beside it, from the same rows.
            key = " ".join(q.lower().split())
            if key and key not in seen_q:
                seen_q.add(key)
                t.distinct_queries.append(q)
            # Ordered on disk, membership here: rank is what the "on screen and
            # never opened" diagnosis needs, and this check only asks whether the
            # run ever saw the URL at all. Normalised on the way in so it meets
            # the cited side under one rule.
            for u in r.get("urls") or ():
                cleaned = _clean(str(u or ""))
                if cleaned:
                    surfaced.add(_norm(cleaned))
        elif op == "fetch":
            url = _clean(str(r.get("url") or ""))
            if not url:
                continue
            ok = bool(r.get("ok"))
            t.pages.append((url, int(r.get("chars") or 0), ok))
            if ok:
                opened.add(url)
        elif op == "verify":
            # Last verdict wins: the turn's outcome is the one it ended on.
            t.verify_outcome = r.get("outcome") or t.verify_outcome
            t.verify_model = r.get("model") or t.verify_model
            claims = r.get("unsupported_claims")
            if isinstance(claims, list):
                t.unsupported = [str(c) for c in claims]
        elif op == "force_finalize" and r.get("event") == "salvage":
            t.salvaged = True

    stamps = [float(r["ts"]) for r in rows if isinstance(r.get("ts"), (int, float))]
    if len(stamps) > 1:
        t.span_seconds = max(stamps) - min(stamps)

    raw_cited = []
    for m in _URL_RE.finditer(answer or ""):
        u = _clean(m.group(0))
        if u not in raw_cited:
            raw_cited.append(u)
    # Normalise both sides, then compare exactly. A reader service can hand back a
    # canonicalised URL, so raw equality would report a correctly-read page as
    # fabricated - a false alarm on the one line here that accuses the answer.
    #
    # Prefix matching in the DEEPER direction was the first fix and it is wrong: it
    # absolves any deep link sitting under an opened page, so a citation to
    # ``/one/appendix-c`` invented on top of a real ``/one`` would pass silently.
    # This check exists to catch fabricated citations; a rule that fails open on
    # the most plausible fabrication is not that check. ``_match_form`` carves the
    # bounded exceptions the extractor's own behavior requires - trailing
    # ideographs, a ``,``/``;`` citation tail, and a cited form that is a strict
    # prefix of exactly one opened page (the model truncated a long path when
    # citing; the SHALLOWER direction, which invents nothing deeper than what was
    # actually read).
    norm_opened = {_norm(o) for o in opened}
    # ``opened_earlier`` counts CITED links accepted on an earlier turn's authority,
    # not every page an earlier turn read. The first implementation counted set
    # growth - memo pages nobody cited inflated it, and the rendered "(N of those)"
    # then named a number with no relation to the links listed beside it.
    norm_earlier: set[str] = set()
    if opened_earlier:
        norm_earlier = {_norm(_clean(str(u))) for u in opened_earlier if u} - norm_opened
    for u in raw_cited:
        this_turn = _match_form(u, norm_opened)
        earlier = None if this_turn else _match_form(u, norm_earlier)
        form = this_turn or earlier or u
        if form in t.cited:
            continue
        t.cited.append(form)
        if earlier:
            t.opened_earlier += 1
        elif this_turn is None:
            t.cited_not_opened.append(form)
    # Through ``_match_form``, not bare set membership: the recoveries it makes
    # against opened pages (a truncated path, glued prose, a citation tail) are
    # the extractor's own artefacts, and a link a search did list must not be
    # called fabricated -- the heavier accusation -- for a defect the opened
    # check forgives.
    t.cited_never_surfaced = [u for u in t.cited_not_opened if _match_form(u, surfaced) is None]
    # ★ 20260901 (Framework). Disclosure pass, deliberately last and deliberately
    # read-only with respect to everything above: ``cited``, ``cited_not_opened``,
    # ``cited_never_surfaced`` and the rate derived from them are already final at
    # this point, so no reading on disk can move because of this block.
    #
    # Anything ``_URL_RE`` already claimed is excluded under the same normalisation
    # the rest of the module uses, so a scheme-ful citation is never counted twice
    # and a run that cites properly reports zero here.
    norm_cited = {_norm(u) for u in raw_cited}
    seen_bare: set[str] = set()
    for m in _SCHEMELESS_RE.finditer(answer or ""):
        u = _clean(m.group(1))
        k = _norm(u)
        if not k or k in norm_cited or k in seen_bare:
            continue
        seen_bare.add(k)
        t.cited_schemeless.append(u)
    # ★ 20260902 (Framework, product feedback). Same contract as the pass above:
    # read-only with respect to ``cited`` and the rate, distinct ids only. No
    # ledger lookup is attempted - the nonce never reaches the ledger, so there
    # is nothing to look up (see ``_FENCE_TAG_RE``).
    seen_tags: set[str] = set()
    for m in _FENCE_TAG_RE.finditer(answer or ""):
        tag = f"{m.group(1).lower()} #{m.group(2).lower()}"
        if tag in seen_tags:
            continue
        seen_tags.add(tag)
        t.cited_fence_tags.append(tag)
    return t


def build_appendix(
    ledger_path: str | None, answer: str, opened_earlier: "Iterable[str] | None" = None
) -> tuple[str, dict[str, Any]]:
    """Return ``(appendix_text, counters)``; text is ``""`` when there is no trail.

    A missing ledger yields no appendix **and says so in the counters**. Emitting
    an empty trail would render "0 searches, 0 pages read" over an answer that did
    plenty of both - the shape of every "missing data read as zero" bug this
    project has logged.
    """
    if not ledger_path:
        return "", {"emitted": False, "reason": "ledger_not_configured"}
    rows = read_ledger(ledger_path)
    if not rows:
        return "", {"emitted": False, "reason": "ledger_empty"}
    trail = build_trail(rows, answer, opened_earlier)
    if not trail.searches and not trail.pages:
        return "", {"emitted": False, "reason": "no_research_events"}
    return trail.render(), trail.counters()


__all__ = ["ResearchTrail", "build_appendix", "build_trail", "read_ledger"]
