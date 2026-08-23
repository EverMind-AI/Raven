"""Benchmark containment: refuse to retrieve the evaluation set itself.

Distinct from ``network.py``, which is SSRF protection (private-IP egress). This
module answers a measurement question, not a security one, so it is a separate
surface with its own switch and its own counters.

Why it has to live in the tool layer rather than in post-hoc scoring: the
harness already *detects* dataset access (``pipeline/leakscan.py``), but a
detector can only exclude items after the fact, and that exclusion is
arm-correlated. A flow whose terminal node cites its sources writes the leaked
string into its own answer; an anchor that does not cite writes nothing. Any
leak-based item exclusion therefore drops more treated items than control ones,
in a direction that flatters the control. Measured once: a live-web axis read
+2.50pp before exclusion and +0.00pp after, and neither number was
interpretable, because the exclusion rule was not arm-neutral.

Prevention has no such asymmetry: the item never enters a contaminated state, so
there is nothing to exclude and both arms lose exactly the same reachable pages.

Two things follow from that, and both are load-bearing:

* **Off by default, and when on, on for every arm in the batch.** This changes
  what the model can read, i.e. it changes the generated distribution on the
  anchor too (AGENTS.md 0.2/0.3). It ships as its own labelled version with a
  fresh anchor pair; it is never enabled on one arm.
* **Blocking is recorded, not silent.** A gate whose firing rate is unknown
  cannot be distinguished from a gate that never fired. ``counters`` is exported
  per arm so the rate can be compared across arms, and a rate that differs by
  arm is itself a finding about the arms, not about the gate.

The pattern vocabulary duplicates ``pipeline/leakscan.py`` because the two run
under different interpreters in different trees. Duplication that cannot be
removed has to be made *detectable*: ``vocabulary_fingerprint()`` is stamped
next to the counters so a silent divergence between the preventer and the
detector shows up as two different hashes rather than as a clean report.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

# Tier 1 - the act of pulling a dataset artifact. High precision: these forms do
# not occur while reading an ordinary web page.
_ARTIFACT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"huggingface\.co/datasets/", "hf_dataset"),
    (r"huggingface\.co/api/datasets/", "hf_datasets_api"),
    (r"datasets-server\.huggingface\.co", "hf_datasets_server"),
    (r"hf-mirror\.com/datasets/", "hf_mirror_dataset"),
    (r"kaggle\.com/datasets/", "kaggle_dataset"),
    (r"paperswithcode\.com/dataset", "pwc_dataset"),
    (r"\.(?:jsonl|parquet|tsv|json\.gz)(?:\?|$)", "dataset_file_ext"),
)

# Tier 2 - a benchmark named in the host or path. Lower precision on its own
# (a paper or blog post about a benchmark is legitimate reading), so it only
# fires together with a retrieval-shaped path; see ``_NAMED_CARRIER``.
_BENCHMARK_NAMES = (
    r"browsecomp(?:[-_]?plus)?|browse[-_]?comp|bc[-_]?plus|"
    r"hle|humanit(?:y|ies)'?s?[-_]?last[-_]?exam|"
    r"widesearch|wide[-_]?search|"
    r"deep[-_]?research[-_]?bench|drb|"
    r"xbench(?:[-_]?deepsearch)?|"
    r"gaia|frames|hotpotqa|seal[-_]?0|redsearcher|infoseek"
)
_NAMED = re.compile(rf"(?:^|[^a-z0-9])(?:{_BENCHMARK_NAMES})(?:[^a-z0-9]|$)", re.I)
# The carrier is what turns a mention into a retrieval: a raw-file host, a
# release/blob/resolve path, an archive extension, or a repository tree.
_NAMED_CARRIER = re.compile(
    r"raw\.githubusercontent\.com|gitee\.com/[^/]+/[^/]+/raw/|"
    r"/(?:resolve|blob|raw|releases|archive)/|"
    r"\.(?:csv|zip|tar\.gz|tgz|jsonl|parquet)(?:\?|$)|"
    r"/(?:tree|blob)/(?:main|master)/",
    re.I,
)

_COMPILED_ARTIFACT = tuple((re.compile(p, re.I), tag) for p, tag in _ARTIFACT_PATTERNS)

_BLOCK_MESSAGE = (
    "This URL is not retrievable in this environment. Continue from other "
    "sources; do not retry this URL or close variants of it."
)


def vocabulary_fingerprint() -> str:
    """Stable hash of the pattern vocabulary, for provenance next to counters.

    Any change to what this module blocks changes this value, so a batch whose
    fingerprint differs from the one the detector was calibrated against is
    flagged rather than quietly compared.
    """
    payload = "\x00".join(
        [*(f"{p}\x01{tag}" for p, tag in _ARTIFACT_PATTERNS), _BENCHMARK_NAMES,
         _NAMED_CARRIER.pattern]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def classify(url: str) -> str | None:
    """Return the reason tag if ``url`` is a benchmark artifact, else ``None``.

    Matching is on the full URL rather than the hostname because the decisive
    part is usually the path: ``huggingface.co`` hosts models and papers as well
    as datasets, and blocking the whole host would remove reachable pages that
    have nothing to do with the eval set.
    """
    if not url:
        return None
    candidate = url if "://" in url else f"https://{url}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    # Normalize away percent-encoding of the separators an attacker-free but
    # careless agent still produces (`%2F` appears in HF resolve-cache links,
    # which is exactly the form one measured leak arrived in).
    probe = candidate.replace("%2F", "/").replace("%2f", "/")
    for pattern, tag in _COMPILED_ARTIFACT:
        if pattern.search(probe):
            return tag
    if _NAMED.search(probe) and _NAMED_CARRIER.search(probe):
        return "named_benchmark_artifact"
    return None


class BenchmarkContainment:
    """Per-run gate. One instance per tool set; counters are read at teardown.

    Enabled state is explicit rather than inferred from the presence of a
    corpus endpoint: the corpus axis is already contained by construction (its
    retrieval service only serves the corpus), and the axis that needs this gate
    is precisely the one with no corpus, so tying the two together would enable
    it exactly where it is redundant and disable it where it is required.
    """

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.blocked_fetches = 0
        self.redacted_results = 0
        self.reasons: dict[str, int] = {}
        self.blocked_urls: list[str] = []

    def _record(self, reason: str, url: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        # Bounded: the list is provenance, not a log. An unbounded list on a
        # 150-iteration trajectory is a second copy of the transcript.
        if len(self.blocked_urls) < 200:
            self.blocked_urls.append(url)

    def check_fetch(self, url: str) -> str | None:
        """Return the refusal text if this fetch must not happen, else ``None``."""
        if not self.enabled:
            return None
        reason = classify(url)
        if reason is None:
            return None
        self.blocked_fetches += 1
        self._record(reason, url)
        return _BLOCK_MESSAGE

    def filter_results(self, results: list[dict], link_key: str = "link") -> list[dict]:
        """Drop search results pointing at benchmark artifacts.

        Search is filtered as well as fetch because a result row can leak
        without ever being opened: one measured contamination came from a
        dataset page's text appearing in a snippet. Rows are dropped rather
        than replaced with a placeholder - a placeholder is an instruction to
        the model that something exists at that rank, and the shortened list is
        indistinguishable from a query that simply matched fewer pages.
        """
        if not self.enabled:
            return results
        kept = []
        for item in results:
            reason = classify(str(item.get(link_key) or ""))
            if reason is None:
                kept.append(item)
                continue
            self.redacted_results += 1
            self._record(reason, str(item.get(link_key) or ""))
        return kept

    def counters(self, include_urls: bool = False) -> dict:
        """Provenance blob for the trajectory record.

        ``include_urls`` is off for the per-turn stamp: the counters are
        cumulative, so a 150-turn trajectory would carry 150 copies of a growing
        URL list. The URLs are not lost by leaving them out - a refused fetch is
        still a recorded ``tool_calls`` entry, so the primary source has them
        already, and re-deriving them there also cross-checks this gate against
        what the model actually attempted.
        """
        out = {
            "enabled": self.enabled,
            "blocked_fetches": self.blocked_fetches,
            "redacted_results": self.redacted_results,
            "reasons": dict(self.reasons),
            "vocabulary_fingerprint": vocabulary_fingerprint(),
        }
        if include_urls:
            out["blocked_urls"] = list(self.blocked_urls)
        return out
