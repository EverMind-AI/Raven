"""Containment gate tests.

The positive cases are real URLs taken from measured contamination events, not
invented ones: a gate calibrated against imagined leaks is a gate nobody has
fired. The negative cases are the pages this must not cost us, because
over-blocking is symmetric across arms but still lowers the ceiling of the axis.
"""

import pytest

from raven.security.benchmark_containment import (
    BenchmarkContainment,
    classify,
    vocabulary_fingerprint,
)

# Observed in batch trajectories (leakscan tier LEAK / SUSPECT hits).
MEASURED_LEAKS = [
    "https://huggingface.co/datasets/Lk123/InfoSeek/resolve/main/data/InfoSeekQA.jsonl",
    "https://huggingface.co/api/datasets/Lk123/InfoSeek",
    # The one that got through: a dataset *page*, not a resolve/raw file.
    "https://huggingface.co/datasets/cais/hle",
    "https://datasets-server.huggingface.co/rows?dataset=cais%2Fhle",
    # Percent-encoded separators, the form one measured leak actually arrived in.
    "https://huggingface.co/api/resolve-cache/datasets/RAGLAB/x/eval_datasets%2FPopQA%2Fpopqa_first_500.jsonl?download=true",
    "https://raw.githubusercontent.com/openai/simple-evals/main/browsecomp_test_set.csv",
    "https://github.com/Alibaba-NLP/WideSearch/blob/main/data/widesearch.jsonl",
    "https://www.kaggle.com/datasets/someone/browsecomp",
    "https://paperswithcode.com/dataset/gaia",
]

# Pages a research agent legitimately needs. Blocking these would be a silent
# capability cut applied to every arm.
MUST_STAY_REACHABLE = [
    "https://arxiv.org/abs/2504.12516",
    "https://huggingface.co/papers/2504.12516",
    "https://huggingface.co/Qwen/Qwen3-235B-A22B",
    "https://en.wikipedia.org/wiki/Humanity",
    # A blog post that merely names a benchmark: named without a retrieval
    # carrier, so tier 2 must not fire.
    "https://example.com/blog/why-browsecomp-is-hard",
    "https://github.com/openai/simple-evals",
    "https://www.nature.com/articles/s41586-024-07487-w",
    "https://news.ycombinator.com/item?id=40000000",
]


@pytest.mark.parametrize("url", MEASURED_LEAKS)
def test_measured_leaks_are_classified(url):
    assert classify(url) is not None, url


@pytest.mark.parametrize("url", MUST_STAY_REACHABLE)
def test_ordinary_pages_are_not_classified(url):
    assert classify(url) is None, url


def test_disabled_is_a_no_op():
    """The default must be byte-identical to no gate at all."""
    gate = BenchmarkContainment()
    assert gate.check_fetch(MEASURED_LEAKS[0]) is None
    rows = [{"link": u} for u in MEASURED_LEAKS]
    assert gate.filter_results(rows) == rows
    assert gate.blocked_fetches == 0
    assert gate.redacted_results == 0


def test_enabled_blocks_and_counts():
    gate = BenchmarkContainment(enabled=True)
    refusal = gate.check_fetch(MEASURED_LEAKS[0])
    assert refusal is not None
    # The refusal must not name the dataset or hint that the answer is there:
    # a message that says "this is the eval set" is itself a leak.
    lowered = refusal.lower()
    for token in ("dataset", "benchmark", "eval", "gold", "answer key"):
        assert token not in lowered, token
    assert gate.blocked_fetches == 1
    assert sum(gate.reasons.values()) == 1


def test_enabled_redacts_serp_rows_without_touching_others():
    gate = BenchmarkContainment(enabled=True)
    rows = [{"link": MUST_STAY_REACHABLE[0]}, {"link": MEASURED_LEAKS[0]}, {"link": MUST_STAY_REACHABLE[1]}]
    kept = gate.filter_results(rows)
    assert [r["link"] for r in kept] == [MUST_STAY_REACHABLE[0], MUST_STAY_REACHABLE[1]]
    assert gate.redacted_results == 1


def test_counters_omit_urls_by_default():
    """Per-turn stamps are cumulative; a growing URL list would be re-emitted."""
    gate = BenchmarkContainment(enabled=True)
    gate.check_fetch(MEASURED_LEAKS[0])
    assert "blocked_urls" not in gate.counters()
    assert gate.counters(include_urls=True)["blocked_urls"] == [MEASURED_LEAKS[0]]


def test_blocked_url_list_is_bounded():
    gate = BenchmarkContainment(enabled=True)
    for i in range(500):
        gate.check_fetch(f"https://huggingface.co/datasets/x/y{i}")
    assert gate.blocked_fetches == 500
    assert len(gate.counters(include_urls=True)["blocked_urls"]) == 200


def test_fingerprint_is_stable_and_short():
    a = vocabulary_fingerprint()
    assert a == vocabulary_fingerprint()
    assert len(a) == 16


def test_malformed_input_is_not_blocked():
    for bad in ("", "not a url", "ftp://x/y.jsonl", "javascript:alert(1)", None):
        assert classify(bad) is None, bad


def test_corpus_paths_never_reach_the_containment_gate():
    """The gate is a structural no-op on the corpus axis - assert it, don't re-check it.

    Both call sites live on the live-web side: ``filter_results`` inside
    ``_search`` and ``check_fetch`` after ``_fetch`` has already returned for a
    corpus endpoint. That is correct - the corpus service serves only the corpus,
    so running the gate there could only misclassify a docid - but it means
    ``benchmarkContainment: true`` on a corpus arm changes no behaviour at all.

    Why a test and not a note: a batch can set the flag, pass a flag gate, and
    record it in ``arm_env`` while nothing whatsoever fires. Someone reading that
    provenance later has every reason to believe the axis was contained by the
    knob rather than by construction, and would date the axis's contamination
    exposure from the wrong batch. If a future change does route a corpus path
    through the gate, that is a distribution change on the corpus axis and this
    test should fail until it is labelled as one.
    """
    import inspect

    from raven.agent.tools.web import WebFetchTool, WebSearchTool

    for fn in (WebSearchTool._search_corpus, WebFetchTool._fetch_corpus):
        src = inspect.getsource(fn)
        assert "containment" not in src, (
            f"{fn.__qualname__} now touches the containment gate: that is a "
            "corpus-axis distribution change and needs its own version label"
        )
