"""Deterministic research trail appended to a product answer (dr@2.8).

An auditable report needs a record of how the answer was reached. The obvious
way - asking the model in the contract to describe its search strategy - buys a
*self-report*: unverifiable, competing with evidence for the context window, and
free to be wrong in the direction that flatters the turn. This repo has already
paid for trusting one: ``arm_env``'s tool-surface stamp is a probe rather than an
enumeration, so it misses tools nobody happened to call.

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
# clause after it. ``https://coldiq.com/blog/serper-pricing(原文:...)`` - one token,
# in full-width punctuation - matched no fetch record, and a page the run really had
# opened was reported as a fabricated citation. Fixed direction, and it fires only on
# Chinese output, which is this product's entire surface. Multi-turn made it visible
# rather than causing it: the research memo hands later turns more URLs to cite, so
# what used to be a rare line became four flagged citations in an eight-turn run.
#
# CJK ideographs are NOT excluded. They were until 2026-08-26, on the theory that a
# reader service hands back percent-encoded paths so a literal ideograph must be prose.
# The theory ignored the citing side: models decode the escapes for readability, so
# ``https://zh.wikipedia.org/wiki/李彦宏`` is a routine citation of a page fetched as
# ``.../wiki/%E6%9D%8E...``. Truncating at the ideograph made every such citation a
# false "never opened" - and two different pages truncate to the SAME prefix, so the
# accusation multiplied. The residual ambiguity runs the other way now: prose glued
# directly onto a URL without punctuation is swallowed into it and fails the match.
# That stays a false alarm rather than a false pass, which is the direction this
# check must fail in (see the prefix-matching note in ``build_trail``).
_CJK = (
    "　-〿"  # CJK punctuation: , . ; : and the bracket family
    "＀-￯"  # fullwidth forms: ( ) , : ; ! ?
    "—…·"  # em dash, ellipsis, middle dot: outside both blocks
)
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]`" + _CJK + r"]+", re.I)
# Backtick and asterisk join the trailing set for the markdown case (`url`, **url**),
# which the character class above cannot catch for the asterisk without banning it
# from paths outright.
_TRAILING = ".,;:!?`*"

_MAX_LISTED = 40
# Below this a fetch returned a redirect stub, an empty JSON, or an error page.
# Observed: a run guessing a ClickBench path got 199 and 203 characters back before
# listing the directory and finding the real file.
_THIN_PAGE_CHARS = 400


def _clean(url: str) -> str:
    return url.rstrip(_TRAILING)


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
    """
    u = unquote(_clean(url).strip())
    for prefix in ("https://", "http://"):
        if u.lower().startswith(prefix):
            u = u[len(prefix):]
            break
    host, _, rest = u.partition("/")
    return f"{host.lower().removeprefix('www.')}/{rest}".rstrip("/")


def _match_form(url: str, known: set[str]) -> str | None:
    """The form of ``url`` that ``known`` holds, or ``None``.

    Tried as written first. On a miss, trailing ideographs come off one at a time:
    the extractor keeps ideographs (a model cites ``.../wiki/李彦宏`` in decoded
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
        if u and "一" <= u[-1] <= "鿿":
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
        prefixed = [k for k in known if k.startswith(n) and k != n]
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
    salvaged: bool = False
    """The shipped answer is a salvage synthesis, which the reviewer never sees
    (observer order, ``dr.py``). Rendered so an unreviewed answer does not wear
    the same trail as a reviewed one, and counted so the salvage share of any
    arm is measurable next to ``verify`` outcomes."""
    unsupported: list[str] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)
    cited_not_opened: list[str] = field(default_factory=list)
    opened_earlier: int = 0
    """How many URLs the grounding check accepted on an earlier turn's authority.

    Reported because it widens the check's denominator, and a check whose scope can
    change without saying so is one nobody can compare across runs. ``0`` on every
    single-turn run, which is every measured arm."""

    def counters(self) -> dict[str, Any]:
        opened_ok = sum(1 for _, _, ok in self.pages if ok)
        return {
            "emitted": True,
            "searches": self.searches,
            "distinct_queries": len(self.distinct_queries),
            "replays": self.replays,
            "zero_hit_searches": self.zero_hit,
            "pages_opened": len(self.pages),
            "pages_ok": opened_ok,
            "urls_cited": len(self.cited),
            # The integrity number. Null-safe on purpose: a rate over zero
            # citations is not 1.0, it is undefined, and reporting 1.0 would make
            # an answer that cites nothing look perfectly grounded.
            "cited_not_opened": len(self.cited_not_opened),
            "citation_grounding_rate": (
                round(1 - len(self.cited_not_opened) / len(self.cited), 4) if self.cited else None
            ),
            # Emitted so the batch-level "share of answers that cited nothing" is
            # computable without re-deriving it from a null. The rate is a ratio
            # whose denominator the measured arm chooses, so it may not be read
            # without this and ``urls_cited`` next to it.
            "cites_nothing": not self.cited,
            # dr@3.0: non-zero means the grounding rate above was computed over this
            # conversation's fetches, not this turn's. Two runs whose scopes differ
            # are not comparable on the rate, so the scope travels with it.
            "opened_earlier": self.opened_earlier,
            "verify_outcome": self.verify_outcome,
            "verify_open_points": len(self.unsupported),
            "salvaged": self.salvaged,
        }

    def render(self) -> str:
        opened_ok = sum(1 for _, _, ok in self.pages if ok)
        head = (
            f"{self.searches} searches ({len(self.distinct_queries)} distinct), "
            f"{opened_ok} pages read"
        )
        if self.verify_outcome:
            head += f", reviewer: {self.verify_outcome}"
            if self.unsupported:
                head += f" ({len(self.unsupported)} open point"
                head += "s)" if len(self.unsupported) > 1 else ")"
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
        lines = ["", "---", "", f"**Research trail** — {head}", ""]

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
            lines.append("> No links were cited above, so there was nothing to check.")
            lines.append("")
        elif self.cited_not_opened:
            # Stated first and in plain words: it is the one line here that says
            # something is wrong with the answer above.
            lines.append(
                f"> ⚠️ {len(self.cited_not_opened)} of {len(self.cited)} link(s) cited "
                "above were never opened during this research: "
                + ", ".join(self.cited_not_opened[:5])
            )
            lines.append("")
        else:
            n = len(self.cited)
            lines.append(
                f"> ✓ All {n} cited link{'s' if n > 1 else ''} "
                f"{'were' if n > 1 else 'was'} opened during this research."
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
            substantive = [p for p in self.pages if p[2] and p[1] >= _THIN_PAGE_CHARS]
            thin = opened_ok - len(substantive)
            failed = len(self.pages) - opened_ok
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
            claims = r.get("unsupported_claims")
            if isinstance(claims, list):
                t.unsupported = [str(c) for c in claims]
        elif op == "force_finalize" and r.get("event") == "salvage":
            t.salvaged = True

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
