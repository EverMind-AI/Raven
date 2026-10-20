"""Every pinchbench task's id, and every id that names one, agree.

The drift this pins. Two task files were renamed to make room for two
insertions, and their front-matter ids were left behind:

    task_18_market_research.md      id: task_16_market_research
    task_19_spreadsheet_summary.md  id: task_18_spreadsheet_summary

Three surfaces disagreed with each other as a result. The **bench loads the
front-matter id** (``task_loader._load_task`` reads ``metadata["id"]``) and
selects by it (``_select_tasks`` matches ``t.task_id``), so a runner asked
for the id in the filename -- which is what a reader sees -- matched
nothing and ran nothing at all: a silent no-op that looks like a run that
found nothing to do. The **routing classifier** named the tasks by their
filenames, so it dispatched to ids no task file carried. And the **routing
snapshot**, the offline fallback for the leaderboard, named the old ids, so
the fallback pointed at tasks that did not exist.

The filenames are the current series: the two files were renamed precisely
because the numbers were taken, and the ids are the stale half. So the id
is the thing that has to follow the file, and the file is the thing a
reader sees. This test pins that, and pins every other surface that names
a task, so the next rename cannot leave one of them behind.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TASKS = REPO / "benchmarks" / "pinchbench" / "tasks"


def _task_files() -> list[Path]:
    return sorted(TASKS.glob("task_*.md"))


def _front_matter_id(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    assert match, f"{path.name} has no front matter"
    found = re.search(r"^id:\s*(\S+)", match.group(1), re.M)
    assert found, f"{path.name} has no id"
    return found.group(1)


def _ids_in_module(path: Path, *names: str) -> set[str]:
    """Every string value under the named module-level dicts.

    Parsed rather than imported: the modules pull in a config and an
    embeddings client at import time, and a test about ids should not need
    either to run.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        # Both shapes a module-level table takes: ``X = {...}`` and the
        # annotated ``X: dict[str, str] = {...}``.
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        else:
            continue
        if not any(name in targets for name in names):
            continue
        if not isinstance(value, ast.Dict):
            continue
        # A task id may sit on either side: the classifier keys by category
        # and names the task in the value, the embeddings table keys by task
        # id. Both are read.
        for node in list(value.keys) + [v for v in value.values]:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("task_"):
                    found.add(node.value)
    return found


def _snapshot_ids() -> set[str]:
    raw = json.loads((REPO / "raven" / "routing" / "snapshot.json").read_text(encoding="utf-8"))
    ids: set[str] = set()
    for entry in raw.get("models", []):
        for row in entry.get("taskScores", []):
            if isinstance(row.get("taskId"), str):
                ids.add(row["taskId"])
    return ids


def test_a_task_file_ids_itself_by_its_own_filename() -> None:
    """The id is the filename's stem, with no exceptions."""
    files = _task_files()
    assert files, "no task files found"
    for path in files:
        assert _front_matter_id(path) == path.stem, (
            f"{path.name} answers to {_front_matter_id(path)!r}; the filename is what a reader "
            f"types and the bench selects on the id"
        )


def test_the_two_renamed_tasks_answer_to_their_filenames() -> None:
    """The two files that were renamed to make room are the drift this found."""
    ids = {_front_matter_id(p): p.name for p in _task_files()}
    assert ids.get("task_18_market_research") == "task_18_market_research.md"
    assert ids.get("task_19_spreadsheet_summary") == "task_19_spreadsheet_summary.md"
    # And the old ids are gone, so nothing can still be dispatched to them.
    assert "task_16_market_research" not in ids
    assert "task_18_spreadsheet_summary" not in ids


def test_the_routing_classifier_names_tasks_that_exist() -> None:
    known = {_front_matter_id(p) for p in _task_files()}
    named = _ids_in_module(REPO / "raven" / "routing" / "classifier.py", "TASK_CATEGORIES")
    assert named, "the classifier names no tasks; the parse found nothing to check"
    missing = named - known
    assert not missing, f"the classifier dispatches to ids no task carries: {sorted(missing)}"


def test_the_routing_embeddings_table_names_tasks_that_exist() -> None:
    known = {_front_matter_id(p) for p in _task_files()}
    named = _ids_in_module(
        REPO / "raven" / "routing" / "generate_embeddings.py", "TASK_DESCRIPTIONS", "TASK_EMBEDDING_INPUTS"
    )
    if not named:
        return
    missing = named - known
    assert not missing, f"the embeddings table names ids no task carries: {sorted(missing)}"


def test_the_routing_snapshot_names_tasks_that_exist() -> None:
    """The offline fallback must not point at a task that is not there."""
    known = {_front_matter_id(p) for p in _task_files()}
    named = _snapshot_ids()
    assert named, "the snapshot carries no task ids"
    missing = named - known
    assert not missing, f"the routing fallback names ids no task carries: {sorted(missing)}"


def test_every_task_file_is_reachable_by_the_id_the_bench_selects_on() -> None:
    """The bench selects on the front-matter id, so that is the name a runner
    types. Every file must answer to exactly one id, and no two may share
    one."""
    ids = [_front_matter_id(p) for p in _task_files()]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"two task files answer to the same id: {sorted(duplicates)}"
