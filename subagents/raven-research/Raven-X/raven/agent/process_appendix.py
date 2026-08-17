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
# CJK ideographs are excluded along with the punctuation: a raw ideograph inside a URL
# would be percent-encoded in anything a reader service hands back, so a literal one is
# the sentence continuing, never the link.
_CJK = (
    "　-〿"  # CJK punctuation: , . ; : and the bracket family
    "＀-￯"  # fullwidth forms: ( ) , : ; ! ?
    "一-鿿"  # ideographs - a URL that reaches one has already ended
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
    kept, because ``?id=2`` is a different document.
    """
    u = _clean(url).strip()
    for prefix in ("https://", "http://"):
        if u.lower().startswith(prefix):
            u = u[len(prefix) :]
            break
    host, _, rest = u.partition("/")
    return f"{host.lower().removeprefix('www.')}/{rest}".rstrip("/")


@dataclass
class ResearchTrail:
    """What the ledger says happened, plus the one integrity check."""

    searches: int = 0
    distinct_queries: list[str] = field(default_factory=list)
    replays: int = 0
    zero_hit: int = 0
    pages: list[tuple[str, int, bool]] = field(default_factory=list)  # url, chars, ok
    verify_outcome: str | None = None
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
        }

    def render(self) -> str:
        opened_ok = sum(1 for _, _, ok in self.pages if ok)
        head = f"{self.searches} searches ({len(self.distinct_queries)} distinct), {opened_ok} pages read"
        if self.verify_outcome:
            head += f", reviewer: {self.verify_outcome}"
            if self.unsupported:
                head += f" ({len(self.unsupported)} open point"
                head += "s)" if len(self.unsupported) > 1 else ")"
        lines = ["", "---", "", f"**Research trail** — {head}", ""]

        if self.cited_not_opened:
            # Stated first and in plain words: it is the one line here that says
            # something is wrong with the answer above.
            lines.append(
                f"> ⚠️ {len(self.cited_not_opened)} link(s) cited above were never opened "
                f"during this research: " + ", ".join(self.cited_not_opened[:5])
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

    cited = []
    for m in _URL_RE.finditer(answer or ""):
        u = _clean(m.group(0))
        if u not in cited:
            cited.append(u)
    t.cited = cited
    # Normalise both sides, then compare exactly. A reader service can hand back a
    # canonicalised URL, so raw equality would report a correctly-read page as
    # fabricated - a false alarm on the one line here that accuses the answer.
    #
    # Prefix matching was the first fix and it is wrong in the worse direction: it
    # absolves any deep link sitting under an opened page, so a citation to
    # ``/one/appendix-c`` invented on top of a real ``/one`` would pass silently.
    # This check exists to catch fabricated citations; a rule that fails open on
    # the most plausible fabrication is not that check.
    norm_opened = {_norm(o) for o in opened}
    if opened_earlier:
        before = len(norm_opened)
        norm_opened |= {_norm(_clean(str(u))) for u in opened_earlier if u}
        t.opened_earlier = len(norm_opened) - before
    t.cited_not_opened = [u for u in cited if _norm(u) not in norm_opened]
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
