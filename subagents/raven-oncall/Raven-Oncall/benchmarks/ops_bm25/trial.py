"""One BM25 tuning trial on the real NFCorpus (BEIR), run inside a container.

Pure Python, zero dependencies. Reads the trial config (k1, b) from the
``OPS_CONFIG_B64`` env var, evaluates BM25 over the NFCorpus corpus mounted at
``/app/data/nfcorpus``, and writes ``/job/result.json`` with the mean nDCG@10 on
the test queries. An inverted index keeps each trial to a couple of seconds so a
whole tuning grid validates the Ops orchestration quickly. The score is a
deterministic function of (k1, b), so the run is reproducible.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re

DATA = "/app/data/nfcorpus"
K_CUTOFF = 10
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "was", "were", "be", "with", "as", "by", "at", "that", "this", "it", "from",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if len(t) > 1 and t not in _STOP]


def load_corpus() -> tuple[list[str], dict[str, int]]:
    doc_ids: list[str] = []
    index_of: dict[str, int] = {}
    texts: list[str] = []
    with open(f"{DATA}/corpus.jsonl", encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            index_of[d["_id"]] = len(doc_ids)
            doc_ids.append(d["_id"])
            texts.append(f"{d.get('title', '')} {d.get('text', '')}")
    return texts, index_of


def load_queries() -> dict[str, str]:
    out: dict[str, str] = {}
    with open(f"{DATA}/queries.jsonl", encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            out[d["_id"]] = d["text"]
    return out


def load_qrels(index_of: dict[str, int]) -> dict[str, dict[int, int]]:
    qrels: dict[str, dict[int, int]] = {}
    with open(f"{DATA}/qrels/test.tsv", encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            qid, did, score = line.rstrip("\n").split("\t")
            if did in index_of and int(score) > 0:
                qrels.setdefault(qid, {})[index_of[did]] = int(score)
    return qrels


def build_index(texts: list[str]) -> tuple[dict[str, list[tuple[int, int]]], list[int], float, dict[str, int]]:
    postings: dict[str, list[tuple[int, int]]] = {}
    doc_len: list[int] = []
    df: dict[str, int] = {}
    for i, text in enumerate(texts):
        tf: dict[str, int] = {}
        toks = tokenize(text)
        for w in toks:
            tf[w] = tf.get(w, 0) + 1
        doc_len.append(len(toks))
        for w, f in tf.items():
            postings.setdefault(w, []).append((i, f))
            df[w] = df.get(w, 0) + 1
    avgdl = sum(doc_len) / len(doc_len) if doc_len else 0.0
    return postings, doc_len, avgdl, df


def ndcg_at_k(ranking: list[int], rels: dict[int, int], k: int) -> float:
    dcg = sum(rels.get(doc, 0) / math.log2(rank + 2) for rank, doc in enumerate(ranking[:k]))
    ideal = sorted(rels.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(rank + 2) for rank, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def evaluate(k1: float, b: float) -> float:
    texts, index_of = load_corpus()
    queries = load_queries()
    qrels = load_qrels(index_of)
    postings, doc_len, avgdl, df = build_index(texts)
    n = len(texts)

    total = 0.0
    for qid, rels in qrels.items():
        scores: dict[int, float] = {}
        for term in set(tokenize(queries[qid])):
            plist = postings.get(term)
            if not plist:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            for doc, f in plist:
                denom = f + k1 * (1 - b + b * doc_len[doc] / avgdl)
                scores[doc] = scores.get(doc, 0.0) + idf * (f * (k1 + 1)) / denom
        ranking = sorted(scores, key=lambda d: scores[d], reverse=True)
        total += ndcg_at_k(ranking, rels, K_CUTOFF)
    return round(total / len(qrels), 6)


def main() -> None:
    cfg = json.loads(base64.b64decode(os.environ["OPS_CONFIG_B64"]).decode())
    ndcg = evaluate(float(cfg["k1"]), float(cfg["b"]))
    with open("/job/result.json", "w", encoding="utf-8") as fh:
        json.dump({"metrics": {"ndcg": ndcg}, "config": cfg}, fh)


if __name__ == "__main__":
    main()
