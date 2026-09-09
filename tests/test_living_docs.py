"""Every repo path a living document cites is a path that exists.

A refactor moves a module and the prose that pointed at it goes stale in
silence: the reader follows the pointer, finds nothing, and cannot tell whether
the document or the tree is wrong. Four such pointers survived this branch --
into `raven/tracing/semconv.py`, `raven/cli/_cron_handler.py`,
`raven/memory_engine/backend.py` and a comparison doc that was deleted.

Living documents only. `docs/plans/` is a historical archive: a plan describes
the tree as it was when the plan was written, and holding it to today's layout
would make it lie about its own date.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: A backticked token that looks like a repo path: has a slash or a known
#: extension, no spaces, and does not start with a scheme or a leading dot.
#: Repo-rooted only. A path written relative to a package (`agent/tools/x.py`,
#: which CONTEXT.md uses throughout), a file raven writes at runtime
#: (`user.md`, `graph.json`), and a skill bundle's own `scripts/` are all
#: legitimate and none of them resolves from the repo root -- so the scan asks
#: only about references that claim a repo-rooted path and can be checked.
ROOTS = (
    "raven/",
    "plugins-dist/",
    "tests/",
    "docs/",
    "ui-tui/",
    "ui-web/",
    "evolver/",
    "benchmarks/",
    ".github/",
)
PATH_LIKE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|md|json|toml|ts|tsx|mjs|yml|yaml|sh))`")


#: Documents that name a path which is deliberately not there: a changelog
#: records what a module used to be called, and AGENTS.md's naming table is a
#: list of counterexamples.
HISTORY = {"CHANGELOG.md", "AGENTS.md", "CLAUDE.md"}


def _living_docs() -> list[Path]:
    docs = [p for p in REPO.glob("*.md") if p.name not in HISTORY]
    docs += [p for p in (REPO / "docs").glob("*.md")]
    docs += [p for p in REPO.glob("raven/**/README.md")]
    docs += [p for p in (REPO / "docs" / "sandbox").glob("*.md")]
    docs += [p for p in REPO.glob("ui-tui/CONTEXT.md")]
    docs += [p for p in REPO.glob("ui-tui/README.md")]
    return sorted(p for p in docs if "plans" not in p.parts and "node_modules" not in p.parts)


def test_the_scan_has_documents_to_scan() -> None:
    """A glob that matched nothing would make the assertion below vacuous."""
    docs = _living_docs()

    assert len(docs) >= 8, [p.name for p in docs]
    assert any(PATH_LIKE.search(p.read_text(encoding="utf-8")) for p in docs)


def test_a_living_document_points_at_something_that_is_there() -> None:
    dangling: list[str] = []
    for doc in _living_docs():
        text = doc.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for cited in PATH_LIKE.findall(line):
                if not cited.startswith(ROOTS):
                    continue
                if (REPO / cited).exists():
                    continue
                dangling.append(f"{doc.relative_to(REPO)}:{lineno} -> {cited}")

    assert dangling == [], "a living document cites a path that is not there: " + "; ".join(dangling)
