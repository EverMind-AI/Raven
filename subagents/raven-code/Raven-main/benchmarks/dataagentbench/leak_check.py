"""Pre-submission leak self-audit for DataAgentBench work.

Runs the same checks the DAB maintainers apply when auditing a submission,
so we apply them to ourselves first (design doc section 1.3):

  1. assets  -- search every ground-truth value (strings and numbers) across
     our general-purpose assets (plugin code, skills, prompt addendum). A hit
     means a per-dataset fact leaked into what must stay generic.
  2. prompts -- for each attempt's opening prompt, flag ground-truth tokens
     that do not come from the sanctioned inputs (question + db description
     + hints), any mention of ground_truth/validate.py, and hint-only lines
     when the run was configured without hints.
  3. traces  -- scan agent sessions and stdout for ground_truth/validate.py
     mentions and for the host-side checkout mount path. Network access is
     denied at the sandbox layer, so URL policing is not duplicated here.

Any finding is printed with its location and the process exits non-zero.
Findings are meant for human review: a match can be a false positive (a
generic word that happens to be a gold value), but silence must be earned.

Usage::

    python -m benchmarks.dataagentbench.leak_check --checkout <DAB checkout> \
        [--assets PATH ...] [--run-dir <agent-eval run dir>] [--use-hints]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ASSETS = (
    REPO_ROOT / "raven" / "plugin" / "data_agent",
    REPO_ROOT / "benchmarks" / "dataagentbench",
)
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".json", ".yaml", ".yml", ".sql", ".sh"}
SKIP_DIR_PARTS = {"__pycache__", ".git", ".venv", "node_modules"}
# Generic words that appear as gold values or CSV headers but carry no leak
# signal (ground_truth.csv files mix header and headerless layouts).
TOKEN_STOPLIST = {"true", "false", "none", "null", "name", "value", "count", "total", "version"}
TRACE_RED_PATTERNS = ("ground_truth", "validate.py", "/dab/checkout")


def _significant_digits(cell: str) -> int:
    digits = re.sub(r"[^0-9]", "", cell)
    return len(digits.lstrip("0"))


def gt_tokens_for_query(query_dir: Path) -> set[str]:
    """Lower-cased searchable tokens from one query's ground_truth.csv."""
    gt_path = query_dir / "ground_truth.csv"
    if not gt_path.is_file():
        return set()
    tokens: set[str] = set()
    with gt_path.open(newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.reader(fh):
            for cell in row:
                cell = cell.strip()
                if not cell:
                    continue
                lowered = cell.lower()
                if lowered in TOKEN_STOPLIST:
                    continue
                if re.search(r"[a-zA-Z]", cell):
                    if len(cell) >= 4:
                        tokens.add(lowered)
                elif _significant_digits(cell) >= 4:
                    tokens.add(lowered)
    return tokens


def iter_query_dirs(checkout: Path):
    for dataset_dir in sorted(checkout.glob("query_*")):
        for query_dir in sorted(dataset_dir.glob("query*")):
            if query_dir.name == "query_dataset" or not query_dir.is_dir():
                continue
            yield dataset_dir, query_dir


def iter_asset_files(assets: list[Path]):
    for root in assets:
        if root.is_file():
            yield root
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            if any(part in SKIP_DIR_PARTS for part in path.parts):
                continue
            yield path


def check_assets(checkout: Path, assets: list[Path]) -> list[str]:
    token_owners: dict[str, str] = {}
    for dataset_dir, query_dir in iter_query_dirs(checkout):
        owner = f"{dataset_dir.name}/{query_dir.name}"
        for token in gt_tokens_for_query(query_dir):
            token_owners.setdefault(token, owner)
    findings = []
    self_path = Path(__file__).resolve()
    for path in iter_asset_files(assets):
        if path.resolve() == self_path:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            lowered = line.lower()
            for token, owner in token_owners.items():
                if token in lowered:
                    findings.append(f"ASSET {path}:{lineno}: contains GT value {token!r} of {owner}")
    return findings


def _sanctioned_text(dataset_dir: Path, query_dir: Path, use_hints: bool) -> str:
    parts = []
    query_json = query_dir / "query.json"
    if query_json.is_file():
        loaded = json.loads(query_json.read_text(encoding="utf-8"))
        parts.append(loaded if isinstance(loaded, str) else json.dumps(loaded))
    names = ["db_config.yaml", "db_description.txt"]
    if use_hints:
        names.append("db_description_withhint.txt")
    for name in names:
        path = dataset_dir / name
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts).lower()


def _hint_only_lines(dataset_dir: Path) -> set[str]:
    plain = dataset_dir / "db_description.txt"
    withhint = dataset_dir / "db_description_withhint.txt"
    if not plain.is_file() or not withhint.is_file():
        return set()
    plain_lines = {line.strip().lower() for line in plain.read_text(encoding="utf-8", errors="replace").splitlines()}
    return {
        stripped
        for line in withhint.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(stripped := line.strip().lower()) >= 8 and stripped not in plain_lines
    }


def _case_query_dir(checkout: Path, case_name: str) -> tuple[Path, Path] | None:
    match = re.fullmatch(r"dab-(.+)-(query\d+)", case_name)
    if match is None:
        return None
    dataset_dir = checkout / f"query_{match.group(1)}"
    query_dir = dataset_dir / match.group(2)
    if not query_dir.is_dir():
        return None
    return dataset_dir, query_dir


def check_prompts(checkout: Path, run_dir: Path, use_hints: bool) -> list[str]:
    findings = []
    for case_dir in sorted((run_dir / "cases").glob("dab-*")):
        located = _case_query_dir(checkout, case_dir.name)
        if located is None:
            findings.append(f"PROMPT {case_dir.name}: no matching query dir in checkout")
            continue
        dataset_dir, query_dir = located
        sanctioned = _sanctioned_text(dataset_dir, query_dir, use_hints)
        tokens = gt_tokens_for_query(query_dir)
        hint_lines = () if use_hints else _hint_only_lines(dataset_dir)
        for prompt_path in sorted(case_dir.glob("attempt_*/agent/prompt.txt")):
            prompt = prompt_path.read_text(encoding="utf-8", errors="replace").lower()
            for token in tokens:
                if token in prompt and token not in sanctioned:
                    findings.append(f"PROMPT {prompt_path}: GT value {token!r} outside sanctioned inputs")
            for pattern in ("ground_truth", "validate.py"):
                if pattern in prompt:
                    findings.append(f"PROMPT {prompt_path}: mentions {pattern!r}")
            for line in hint_lines:
                if line in prompt:
                    findings.append(f"PROMPT {prompt_path}: hint-only line in a hints-off run: {line[:80]!r}")
    return findings


def check_traces(run_dir: Path) -> list[str]:
    findings = []
    trace_paths = list(run_dir.glob("cases/dab-*/attempt_*/agent/stdout.log"))
    trace_paths += list(run_dir.glob("cases/dab-*/attempt_*/agent/sessions/**/*.jsonl"))
    for path in sorted(trace_paths):
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    lowered = line.lower()
                    for pattern in TRACE_RED_PATTERNS:
                        if pattern in lowered:
                            findings.append(f"TRACE {path}:{lineno}: mentions {pattern!r}")
        except OSError:
            continue
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checkout", type=Path, required=True, help="DataAgentBench checkout root")
    parser.add_argument(
        "--assets",
        type=Path,
        nargs="*",
        default=list(DEFAULT_ASSETS),
        help="files/directories holding our prompts, skills and plugin code",
    )
    parser.add_argument("--run-dir", type=Path, default=None, help="agent-eval run dir to audit prompts and traces")
    parser.add_argument("--use-hints", action="store_true", help="the run was configured with hints enabled")
    args = parser.parse_args(argv)

    if not args.checkout.is_dir():
        print(f"checkout not found: {args.checkout}", file=sys.stderr)
        return 2

    findings = check_assets(args.checkout, args.assets)
    if args.run_dir is not None:
        if not (args.run_dir / "cases").is_dir():
            print(f"run dir has no cases/: {args.run_dir}", file=sys.stderr)
            return 2
        findings += check_prompts(args.checkout, args.run_dir, args.use_hints)
        findings += check_traces(args.run_dir)

    for finding in findings:
        print(finding)
    if findings:
        print(f"\n{len(findings)} finding(s) -- review each before any submission.", file=sys.stderr)
        return 1
    print("leak check clean: 0 findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
