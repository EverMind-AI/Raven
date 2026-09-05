"""CI gate against non-English source additions outside the named exemption zones."""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ZERO_SHA = "0" * 40

#: The zones where CJK is admissible, per AGENTS.md section 1.3: the two
#: engine wheels with their fixtures, the zh catalog and prompt pack, and the
#: zh-i18n fixture prefix. Markdown files govern themselves wherever they
#: live (owner rule) -- AGENTS.md itself carries zh counter-examples.
EXEMPT_PREFIXES = (
    "plugins-dist/ppt-engine/",
    "tests/test_ppt_engine_",
    "plugins-dist/design-engine/",
    "tests/test_design_engine_",
    "raven/i18n/",
    "raven/templates/prompts/zh/",
    "tests/test_i18n_",
)

#: The fixture prefix holds only while the test exercises zh machinery: the
#: file must import one of these modules (AGENTS.md section 1.3), or the
#: exemption is revoked and the ordinary passes decide.
I18N_FIXTURE_PREFIX = "tests/test_i18n_"
ZH_MODULES = ("raven.i18n", "raven_ppt", "raven_design")

#: Han (unified + extension A + compatibility), kana, hangul, CJK punctuation
#: and full-width forms. Deliberately narrower than "any non-ASCII": an
#: em-dash in an English docstring is not a language violation.
CJK_RUN = re.compile(
    "["
    "\u3000-\u303f"  # CJK symbols and punctuation
    "\u3040-\u30ff"  # hiragana and katakana
    "\u3400-\u4dbf"  # CJK unified ideographs extension A
    "\u4e00-\u9fff"  # CJK unified ideographs
    "\uac00-\ud7af"  # hangul syllables
    "\uf900-\ufaff"  # CJK compatibility ideographs
    "\uff00-\uffef"  # halfwidth and fullwidth forms
    "]+"
)


@dataclass(frozen=True)
class SourceLanguageViolation:
    path: str
    line_number: int
    line: str


@dataclass(frozen=True)
class AddedLine:
    path: str
    line_number: int
    text: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when a PR adds CJK source lines outside the exemption zones.")
    parser.add_argument("revision_range", nargs="?", default=_default_range())
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not args.revision_range:
        print("No commit range detected; skipping source language check")
        return 0

    added, removed = diff_lines(args.revision_range)
    violations = find_violations(
        added,
        removed_runs=cjk_runs(line.text for line in removed),
        base_had_cjk=_base_reader(args.revision_range),
        head_source=_head_reader(args.revision_range),
    )
    if not violations:
        return 0

    print("Non-English source additions are not allowed in PRs:", file=sys.stderr)
    for violation in violations:
        print(
            f"- {violation.path}:{violation.line_number}: {violation.line.strip()}",
            file=sys.stderr,
        )
    print(
        "Move the text to an exemption zone named in AGENTS.md section 1.3, or write it in English.",
        file=sys.stderr,
    )
    print(
        "A zh test fixture is exempt only as tests/test_i18n_* AND importing raven.i18n or a zh plugin module.",
        file=sys.stderr,
    )
    return 1


def find_violations(
    added: list[AddedLine],
    *,
    removed_runs: set[str],
    base_had_cjk,
    head_source,
) -> list[SourceLanguageViolation]:
    """Apply the passes: zone, fixture import proof, relocation, carrier.

    ``base_had_cjk`` and ``head_source`` are each called at most once per
    offending path, so the gate costs one ``git show`` per file that actually
    needs the carrier rule or the fixture rule.
    """
    violations: list[SourceLanguageViolation] = []
    carrier_cache: dict[str, bool] = {}
    fixture_cache: dict[str, bool] = {}
    for line in added:
        runs = CJK_RUN.findall(line.text)
        if not runs:
            continue
        if is_exempt_path(line.path):
            needs_import_proof = line.path.startswith(I18N_FIXTURE_PREFIX) and not line.path.endswith(".md")
            if not needs_import_proof:
                continue
            if line.path not in fixture_cache:
                fixture_cache[line.path] = imports_zh_machinery(head_source(line.path))
            if fixture_cache[line.path]:
                continue
        if all(run in removed_runs for run in runs):
            continue
        if line.path not in carrier_cache:
            carrier_cache[line.path] = base_had_cjk(line.path)
        if carrier_cache[line.path]:
            continue
        violations.append(SourceLanguageViolation(path=line.path, line_number=line.line_number, line=line.text))
    return violations


def is_exempt_path(path: str) -> bool:
    return path.startswith(EXEMPT_PREFIXES) or path.endswith(".md")


def imports_zh_machinery(source: str) -> bool:
    """True when the module imports ``raven.i18n`` or a zh plugin module.

    The machine-checkable proxy AGENTS.md section 1.3 names for "the test
    exercises zh functionality": a ``tests/test_i18n_*`` fixture keeps its
    exemption only when its imports prove it drives the zh machinery.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        else:
            continue
        for module in modules:
            if any(module == zh or module.startswith(f"{zh}.") for zh in ZH_MODULES):
                return True
    return False


def cjk_runs(lines) -> set[str]:
    runs: set[str] = set()
    for line in lines:
        runs.update(CJK_RUN.findall(line))
    return runs


def diff_lines(revision_range: str) -> tuple[list[AddedLine], list[AddedLine]]:
    output = subprocess.check_output(
        ["git", "diff", "--unified=0", "--no-color", revision_range],
        text=True,
        errors="replace",
    )
    added: list[AddedLine] = []
    removed: list[AddedLine] = []
    path: str | None = None
    new_line = 0
    for raw in output.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif raw.startswith("@@ "):
            match = re.search(r"\+(\d+)", raw)
            new_line = int(match.group(1)) if match else 0
        elif path is not None and raw.startswith("+") and not raw.startswith("+++"):
            added.append(AddedLine(path=path, line_number=new_line, text=raw[1:]))
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            removed.append(AddedLine(path=path or "", line_number=0, text=raw[1:]))
    return added, removed


def _base_reader(revision_range: str):
    base = _range_base(revision_range)

    def base_had_cjk(path: str) -> bool:
        if base is None:
            return False
        result = subprocess.run(
            ["git", "show", f"{base}:{path}"],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if result.returncode != 0:
            return False
        return bool(CJK_RUN.search(result.stdout))

    return base_had_cjk


def _head_reader(revision_range: str):
    head = _range_head(revision_range)

    def head_source(path: str) -> str:
        if head is None:
            try:
                return Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
        result = subprocess.run(
            ["git", "show", f"{head}:{path}"],
            capture_output=True,
            text=True,
            errors="replace",
        )
        return result.stdout if result.returncode == 0 else ""

    return head_source


def _range_base(revision_range: str) -> str | None:
    if "..." in revision_range:
        left, _, right = revision_range.partition("...")
        result = subprocess.run(
            ["git", "merge-base", left, right or "HEAD"],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or None if result.returncode == 0 else None
    if ".." in revision_range:
        return revision_range.split("..", 1)[0] or None
    return None


def _range_head(revision_range: str) -> str | None:
    """The side of the diff whose file contents the added lines belong to.

    ``None`` means a bare revision was given: ``git diff <rev>`` compares
    against the working tree, so the fixture rule reads files from disk.
    """
    if "..." in revision_range:
        right = revision_range.partition("...")[2]
        return right or "HEAD"
    if ".." in revision_range:
        right = revision_range.split("..", 1)[1]
        return right or "HEAD"
    return None


def _default_range() -> str | None:
    pr_base = os.environ.get("GITHUB_PR_BASE_SHA", "").strip()
    if pr_base:
        return f"{pr_base}..HEAD"

    before = os.environ.get("GITHUB_EVENT_BEFORE", "").strip()
    if before and before != ZERO_SHA:
        return f"{before}..HEAD"

    return os.environ.get("RANGE", "").strip() or None


if __name__ == "__main__":
    raise SystemExit(main())
