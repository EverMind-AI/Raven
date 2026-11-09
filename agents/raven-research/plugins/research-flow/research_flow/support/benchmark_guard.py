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

What this does not reach: a host rule cannot see a gold answer inside a paper's
case study, a trajectory dump on a code host, or a forum post quoting the
question. Of the 21 leaked items above this rule reaches 13, so a run under it
is less contaminated, not clean, and the per-item leak review still applies.

Nothing here is a security control. Egress policy lives in ``raven.security``;
this module only declines to read pages that would answer the question for the
model, and it is inert unless an evaluation profile turns it on.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlparse

__all__ = [
    "BENCHMARK_HOSTS",
    "blocks_query",
    "blocks_url",
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
