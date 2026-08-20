"""Audit prompt-bearing sources for DataAgentBench-specific content.

The not-tuned claim rests on one invariant: nothing injected up-front (methodology,
tool descriptions, gate correction text) mentions DAB's datasets, schemas, entities,
or ground-truth values. Humans drift; this check does not. Run it before any
submission-grade claim, alongside leak_check.

The script reads ground-truth files itself so a human never has to: matches are
reported by location and token *category*, with the token redacted unless it is a
schema identifier (those are safe to print).

Usage:
    uv run python -m benchmarks.dataagentbench.prompt_contamination_check \
        --checkout ~/workspace/DataAgentBench \
        [--prompt-file <path> ...] [--extra-dir <path>]

Exit code 0 = clean, 1 = findings.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path

_DEFAULT_PROMPT_GLOBS = (
    # The data-domain discipline block, injected into every data-domain
    # identity prompt (see context_engine/segments/render.py). It moved out of
    # the plugin so the renderer need not import one; the audit follows it.
    "raven/context_engine/segments/prompts/data/methodology.md",
    "raven/context_engine/segments/prompts/data/*.txt",
    "raven/plugin/data_agent/*.py",
)

_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")
# Generic words that legitimately appear both in schemas and in generic prose.
_STOP = {
    "name", "type", "types", "date", "dates", "year", "years", "count", "counts",
    "value", "values", "title", "titles", "description", "region", "regions",
    "category", "categories", "price", "prices", "rating", "ratings", "amount",
    "table", "tables", "column", "columns", "database", "databases", "field",
    "fields", "text", "article", "articles", "author", "authors", "publication",
    "business", "review", "reviews", "user", "users", "state", "city", "address",
    "identifier", "unique", "primary", "index", "string", "integer", "number",
    "total", "average", "answer", "question", "data", "record", "records",
}
# Technology and format names: schema descriptions mention them, generic prose does too.
_TECH = {
    "duckdb", "mongodb", "postgresql", "postgres", "sqlite", "json", "jsonl",
    "yyyy", "pass", "fail", "dataframe", "varchar", "bigint", "double", "utf",
    "csv", "parquet", "mongosh", "pymongo", "http", "https", "ascii",
}


def _schema_tokens(checkout: Path) -> set[str]:
    """Column/table-style identifiers from every dataset's db_description."""
    tokens: set[str] = set()
    for desc in checkout.glob("query_*/db_description*.txt"):
        for token in _IDENT_RE.findall(desc.read_text(encoding="utf-8", errors="replace")):
            low = token.lower()
            if low in _STOP or low in _TECH:
                continue
            # Schema-style only: contains an underscore or mixes case inside.
            # Plain long English words in description prose are not identifiers.
            if "_" in token or token[1:] != token[1:].lower():
                tokens.add(token)
    return tokens


def _dataset_tokens(checkout: Path) -> set[str]:
    return {p.name.removeprefix("query_") for p in checkout.glob("query_*") if p.is_dir()}


def _ground_truth_values(checkout: Path) -> set[str]:
    """Literal GT cell values worth flagging (never printed un-redacted)."""
    values: set[str] = set()
    for gt in checkout.glob("query_*/query*/ground_truth.csv"):
        try:
            text = gt.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for row in csv.reader(io.StringIO(text)):
            for cell in row:
                cell = cell.strip()
                # Only distinctive literals are worth flagging: ordinary English
                # words in GT cells ("proportion") collide with generic prose.
                # Known limitation: a single short common word as a GT value
                # cannot be reliably distinguished and is not flagged.
                distinctive = (
                    any(ch.isdigit() for ch in cell)
                    or "_" in cell
                    or sum(ch.isupper() for ch in cell) >= 2
                    or len(cell) >= 15
                )
                if len(cell) >= 6 and distinctive and not cell.replace(".", "").replace("-", "").isdigit():
                    values.add(cell)
    return values


def _prompt_files(repo_root: Path, explicit: list[str], extra: list[str]) -> list[Path]:
    files: list[Path] = []
    if explicit:
        files = [Path(p) for p in explicit]
    else:
        for pattern in _DEFAULT_PROMPT_GLOBS:
            files.extend(sorted(repo_root.glob(pattern)))
    files.extend(Path(p) for p in extra)
    return [f for f in files if f.is_file()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--prompt-file", action="append", default=[])
    parser.add_argument("--extra-file", action="append", default=[],
                        help="extra prompt-bearing file to scan (e.g. the fork's adapter.py, "
                        "delivery_gate.py, harnesses/raven.py)")
    args = parser.parse_args(argv)

    checkout = Path(args.checkout).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[2]
    files = _prompt_files(repo_root, args.prompt_file, args.extra_file)
    if not files:
        print("no prompt files found to scan", file=sys.stderr)
        return 1

    datasets = _dataset_tokens(checkout)
    schema = _schema_tokens(checkout)
    gt_values = _ground_truth_values(checkout)

    findings = 0
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        for lineno, line in enumerate(lines, start=1):
            low = line.lower()
            for ds in datasets:
                if re.search(rf"\b{re.escape(ds.lower())}\b", low):
                    findings += 1
                    print(f"{path}:{lineno}: dataset name '{ds}'")
            for token in schema:
                if re.search(rf"\b{re.escape(token)}\b", line):
                    findings += 1
                    print(f"{path}:{lineno}: schema identifier '{token}'")
            for value in gt_values:
                if value in line:
                    findings += 1
                    print(f"{path}:{lineno}: ground-truth literal (redacted, {len(value)} chars, "
                          f"starts '{value[:2]}...')")
    print(f"prompt contamination check: {findings} findings over {len(files)} files")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
