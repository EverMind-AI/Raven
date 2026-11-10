"""Refuse published benchmark copies during an evaluation run. Off by default.

A deep-research benchmark publishes its questions and its gold answers in one
file, so a run can score by reading its own answer off a dataset page instead of
researching it. The guard exists because that is measurable, not hypothetical:
on one 400-item evaluation, 21 of the 265 items answered correctly had the gold
in a published benchmark copy as the first tool result that carried it.

The rule is the one BigBang-v1 ships in ``evaluation/tools/policy.py``: treat a
URL as a benchmark host by substring *or* by host suffix, and refuse a search
whose query names the host at all. Three departures from it:

* the URL is unwrapped before it is matched. Serper returns
  ``google.com/goto?url=<target>`` rows, and a host-based match reads the
  wrapper's host rather than the target's -- the bypass measured against that
  exact filter.
* the refusal is returned as text, as both BigBang-v1 and MiroFlow do, rather
  than raised. A fetch that fails looks like a transport fault and gets retried;
  a fetch that is refused with a reason does not.
* the substring half carries a host boundary. Theirs is a bare ``in`` test, which
  also matches ``nothuggingface.com`` -- ``huggingface.co`` being a prefix of
  ``huggingface.com`` -- and a wrongly blocked source is invisible to the model,
  indistinguishable from a page that does not exist.

A host rule alone reaches 14 of those 21, and a smoke run showed exactly how the
rest arrive. With the host rule on, the model's three answer-seeking queries
returned 21 organic rows of which 17 were Hugging Face copies; every one was
dropped and none reached the model. It then scored anyway off
``openevalz.com/trace/<id>`` -- a judge-prompt dump carrying ``[correct_answer]``
verbatim -- and opened a second copy through ``cdn-avatars.qwak.ai/datasets/...``,
which is a Hugging Face dataset page served under a hostname the rule cannot see.
A host list cannot be made long enough to fix that.

So there is a second, host-independent rule: :func:`blocks_page` refuses a fetched
page that reproduces the question it was asked. Quoting the prompt is what makes a
published copy findable in the first place, so it is the one thing every copy has
in common whatever host it sits on.

Measured over the same 400-item evaluation at a 60-character run, counting each
rule only where it actually applies -- this one on ``web_fetch`` alone, so a gold
that arrived in a search result is out of its reach:

* leaks prevented rise from 14/21 to 17/21. The content rule alone reaches 12,
  3 of them ones the host rule misses. Of the 4 that escape both, 3 took their
  gold from a search result and 1 never had the gold in a tool result at all
* 13 of the 243 clean correct items lose at least one fetched page (18 pages).
  On 5 the lost page carried the gold, so they were unlabelled leaks rather than
  false stops, leaving ~8 items exposed, about 3%
* a question shorter than the run length puts the rule out of reach entirely --
  44 of the 400 questions here, which is the deliberate cost of not blocking
  pages on a short, unspecific string

Those 18 stopped pages are worth knowing by kind, because the rate alone hides
what the rule is doing. Seven were themselves dumps nobody had labelled (a
``harbor-datasets`` file on raw.githubusercontent, ``autobenchaudit.com`` twice,
an ``hle_bio_chem_med`` wiki page, and three eval sites). Nine were primary
sources the question was written from -- arXiv four times, OEIS, MathNet,
ACL Anthology, eLife -- and on one item all three such pages carried the gold,
which says the question was authored out of them; whether refusing those is a
false stop or a leak caught is a judgement, not a measurement. The remaining two
are the one clear false positive: a site-search URL echoing the query back,
because the model had pasted the whole question into a search box.

The run length is 60 because the sweep flattens there: 50 prevents the same leaks
and stops roughly twice as many clean items, and 80 prevents one fewer.

This rule has to run on the page as extracted and **before the digest**. The
smoke's decisive page is why: its digest did not quote the question, its 112k
characters of raw trace did, at every run length up to 120.

What still escapes both rules: a gold that arrives in a search result rather than
a page, a gold paraphrased rather than quoted, and a page carrying the answer
without the question. A run under this is less contaminated, not clean, and the
per-item leak review still applies.

Nothing here is a security control. Egress policy lives in ``raven.security``;
this module only declines to read pages that would answer the question for the
model, and it is inert unless an evaluation profile turns it on.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlparse

__all__ = [
    "BENCHMARK_HOSTS",
    "QUESTION_RUN_CHARS",
    "blocks_page",
    "blocks_query",
    "blocks_url",
    "page_refusal",
    "query_refusal",
    "unwrap_url",
    "url_refusal",
]

# Hosts that publish question-and-answer pairs. Kept to the ones a run was
# actually measured reaching: a wider list is a wider false-positive surface and
# this guard runs on every search row and every fetch.
BENCHMARK_HOSTS: tuple[str, ...] = ("huggingface.co", "hf.co")

# The substring half of the rule, with a host boundary BigBang-v1's plain
# ``"huggingface.co" in url`` does not have. Theirs also matches
# ``nothuggingface.com`` -- a different domain, because ``huggingface.co`` is a
# prefix of ``huggingface.com`` -- and blocking an unrelated site is the one
# failure mode this guard must not have, since a blocked source is invisible to
# the model and indistinguishable from a page that does not exist.
_HOST_RE = re.compile(
    r"(?:^|[^a-z0-9.\-])(?:" + "|".join(re.escape(h) for h in BENCHMARK_HOSTS) + r")(?:[/:?#]|$)",
    re.IGNORECASE,
)

# BigBang-v1's query rule, which has no equivalent anywhere else in the field:
# a model that searches for the dataset by name is asking for the answer key, so
# the search is declined before it is sent. Measured to fire on 3 of the 21
# leaked runs above, independently of what their first gold-bearing URL was.
_QUERY_RE = re.compile(r"(?:hugging\s*face|huggingface|hf\.co)", re.IGNORECASE)

# Wrapper parameters that carry the real target. ``q`` is included for
# ``webcache.googleusercontent.com/search?q=cache:<url>``, which is why the value
# is searched for an embedded URL rather than required to be one.
_WRAPPER_PARAMS: tuple[str, ...] = ("url", "u", "q", "target", "dest", "redirect", "redirect_url")
_EMBEDDED_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_UNWRAP_DEPTH = 3

_URL_REFUSAL = (
    "Blocked: this URL is a published copy of a benchmark's questions and answers, "
    "so it is not a source for this task. Research the question instead."
)
_QUERY_REFUSAL = (
    "Blocked: this query searches for a published benchmark copy rather than for the "
    "subject of the question. Search for the subject instead."
)
_PAGE_REFUSAL = (
    "Blocked: this page reproduces the question you were asked, so it is a published "
    "copy of the benchmark rather than a source for it. Research the question instead."
)

# A verbatim run this long shared between the question and the page. See the module
# docstring for the sweep that chose it; it is a knob on BenchmarkGuardConfig because
# a benchmark of much shorter questions would want a different one, not because this
# value is unsettled.
QUESTION_RUN_CHARS = 60

_WS_RE = re.compile(r"\s+")


def url_refusal() -> str:
    """The tool-result text for a refused URL."""
    return _URL_REFUSAL


def query_refusal() -> str:
    """The tool-result text for a refused query."""
    return _QUERY_REFUSAL


def unwrap_url(value: str, *, depth: int = _UNWRAP_DEPTH) -> str:
    """The innermost target of a redirect wrapper, or ``value`` unchanged.

    Bounded rather than recursive-until-stable: a wrapper that re-wraps its own
    output would otherwise spin, and three levels covers every form measured
    (``google.com/goto?url=``, ``r.jina.ai/<url>``, Google's cache).
    """
    current = (value or "").strip()
    for _ in range(max(0, depth)):
        nxt = _unwrap_once(current)
        if nxt == current:
            break
        current = nxt
    return current


def _unwrap_once(value: str) -> str:
    if not value:
        return value
    try:
        parsed = urlparse(value)
    except ValueError:
        return value
    # Path-embedded form: ``r.jina.ai/https://example.org/x``. Taken before the
    # query form because such a URL often has no query at all.
    path_hit = _EMBEDDED_URL_RE.search(unquote(parsed.path or ""))
    if path_hit and path_hit.start() > 0:
        return path_hit.group(0)
    for key, raw in parse_qsl(parsed.query or "", keep_blank_values=False):
        if key.lower() not in _WRAPPER_PARAMS:
            continue
        candidate = unquote(raw).strip()
        hit = _EMBEDDED_URL_RE.search(candidate)
        if hit:
            return hit.group(0)
    return value


def blocks_url(value: str) -> bool:
    """Whether this URL -- or a URL it wraps -- is a benchmark copy.

    The substring test and the host test are both kept, exactly as BigBang-v1
    has them: the substring catches a path-embedded host that ``urlparse`` would
    attribute to the wrapper, and the host test catches the subdomains a
    substring on ``"<host>/datasets"`` would miss. One of our measured leaks
    (``datasets-server.huggingface.co``) is reached only by the host test, and
    another (an ``huggingface.co`` path outside ``/datasets``) only because
    neither test looks at the path.
    """
    for candidate in (value, unwrap_url(value)):
        raw = (candidate or "").strip().lower()
        if not raw:
            continue
        if _HOST_RE.search(raw):
            return True
        try:
            parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        except ValueError:
            continue
        host = (parsed.hostname or "").rstrip(".")
        if host in BENCHMARK_HOSTS or host.endswith(tuple(f".{h}" for h in BENCHMARK_HOSTS)):
            return True
    return False


def blocks_query(value: str) -> bool:
    """Whether this search query names a benchmark host."""
    return bool(_QUERY_RE.search(value or ""))


def page_refusal() -> str:
    """The tool-result text for a page that reproduces the question."""
    return _PAGE_REFUSAL


def _flatten(value: str) -> str:
    """Case, Unicode form and whitespace folded, so a reflowed quote still matches.

    Whitespace is collapsed rather than stripped: dropping it entirely would let a
    60-character run match across word boundaries that are not in the question, and
    the width folding NFKC does is what makes a full-width CJK quote comparable.
    """
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", value or "")).strip().lower()


def blocks_page(task: str, body: str, *, run_chars: int = QUESTION_RUN_CHARS) -> bool:
    """Whether this page reproduces the question, i.e. is a published copy of it.

    True when some ``run_chars``-character run of the question appears verbatim in
    the page. A question shorter than ``run_chars`` is never matched: the rule has
    to be able to tell a benchmark copy from a page that happens to share a short
    phrase, and below that length it cannot.

    Both sides are flattened first, so a copy that reflowed the prompt across lines
    still matches. The scan is every offset rather than a fixed window, because the
    copies seen truncate the prompt at arbitrary points -- one ended mid-word with
    an ellipsis, and a first-window-only test would have missed it.
    """
    q, page = _flatten(task), _flatten(body)
    if run_chars <= 0 or len(q) < run_chars or not page:
        return False
    return any(q[i : i + run_chars] in page for i in range(len(q) - run_chars + 1))
