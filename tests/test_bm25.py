"""Tests for the shared BM25 retrieval kernel (raven.utils.bm25)."""

from raven.utils.bm25 import BM25Okapi, tokenize


def test_tokenize_alphanumeric_lowercased() -> None:
    assert tokenize("Generate PDF Report") == ["generate", "pdf", "report"]


def test_tokenize_drops_one_char_words() -> None:
    assert tokenize("a ai OK x y") == ["ai", "ok"]


def test_tokenize_handles_chinese_per_char() -> None:
    # CJK ideographs each become one token; a plain word-boundary regex
    # would drop them entirely.
    out = tokenize("生成图片 image")
    assert "生" in out and "成" in out and "图" in out and "片" in out
    assert "image" in out


def test_tokenize_empty_returns_empty() -> None:
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_bm25_empty_corpus_scores_empty() -> None:
    bm25 = BM25Okapi([])
    assert bm25.get_scores(["foo"]) == []


def test_bm25_ranks_matching_doc_highest() -> None:
    corpus = [
        tokenize("create github issue tracker"),
        tokenize("send slack message channel"),
        tokenize("read local file path"),
    ]
    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(tokenize("github issue"))
    assert scores[0] == max(scores)
    assert scores[0] > 0.0


def test_bm25_idf_suppresses_common_term() -> None:
    # 'tool' appears in every doc → near-zero IDF; 'weather' is rare → high.
    corpus = [
        tokenize("tool weather forecast"),
        tokenize("tool github issue"),
        tokenize("tool slack message"),
    ]
    bm25 = BM25Okapi(corpus)
    common = bm25.get_scores(tokenize("tool"))
    rare = bm25.get_scores(tokenize("weather"))
    assert max(rare) > max(common)


def test_bm25_chinese_query_matches() -> None:
    corpus = [
        tokenize("生成图片 image generate"),
        tokenize("读取文件 read file"),
    ]
    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(tokenize("生成图片"))
    assert scores[0] == max(scores)
    assert scores[0] > 0.0
