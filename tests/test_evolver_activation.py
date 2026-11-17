"""The evolver's offline routing probe, exercised against the real catalog.

``dry_query`` is the only caller of ``LocalSkillCatalog`` outside ``raven/``,
and it lives in a tree the runtime may not import (the fifth contract), so the
package's own suite never touches it. It is covered here because a change to
the catalog's signature would otherwise break it in silence.
"""

from __future__ import annotations

from pathlib import Path

from evolver.activation import dry_query
from evolver.activation.ledger import (
    beacon_workspace,
    mark_beacons_enabled,
    per_task_density,
    read_fired_tasks,
)


def test_dry_query_runs_the_real_catalog_and_router() -> None:
    names = dry_query("write a unit test for the parser")
    assert isinstance(names, list)
    assert all(isinstance(n, str) for n in names)


def test_dry_query_surfaces_a_skill_mounted_from_library_root(tmp_path: Path) -> None:
    skill = tmp_path / "tb2_gap_fill" / "csv_repair" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\n"
        "name: csv_repair\n"
        "description: Repair a malformed CSV export before parsing it.\n"
        "---\n\n"
        "Read the file, find the ragged rows, and pad them.\n",
        encoding="utf-8",
    )

    names = dry_query("repair a malformed CSV export", library_root=tmp_path)

    assert "csv_repair" in names


def test_dry_query_returns_each_name_once(tmp_path: Path) -> None:
    for n in ("alpha", "beta"):
        d = tmp_path / "tb2_gap_fill" / n
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {n}\ndescription: Handle a {n} task end to end.\n---\n\nBody.\n",
            encoding="utf-8",
        )

    names = dry_query("handle an alpha task", library_root=tmp_path)

    assert len(names) == len(set(names))


def _record_beacon(out_dir: Path, task_id: str, k: int, *, fired: bool) -> None:
    ws = beacon_workspace(out_dir, task_id, k)
    ws.mkdir(parents=True, exist_ok=True)
    if fired:
        (ws / "activation_ledger.jsonl").write_text('{"kind": "beacon"}\n', encoding="utf-8")
    else:
        (ws / "activation_ledger.jsonl").touch()


def test_per_task_density_is_none_without_the_collection_marker(tmp_path: Path) -> None:
    _record_beacon(tmp_path, "task_a", 0, fired=True)

    assert per_task_density([tmp_path], ["task_a"]) is None


def test_per_task_density_counts_attempts_that_fired(tmp_path: Path) -> None:
    mark_beacons_enabled(tmp_path)
    _record_beacon(tmp_path, "task_a", 0, fired=True)
    _record_beacon(tmp_path, "task_a", 1, fired=False)
    _record_beacon(tmp_path, "task_a", 2, fired=True)
    _record_beacon(tmp_path, "task_b", 0, fired=False)
    _record_beacon(tmp_path, "task_b", 1, fired=False)

    density = per_task_density([tmp_path], ["task_a", "task_b", "task_c"])

    assert density == {"task_a": 2 / 3, "task_b": 0.0}


def test_per_task_density_omits_tasks_with_no_attempt(tmp_path: Path) -> None:
    mark_beacons_enabled(tmp_path)
    _record_beacon(tmp_path, "task_a", 0, fired=True)

    density = per_task_density([tmp_path], ["task_a", "never_run"])

    assert density == {"task_a": 1.0}
    assert "never_run" not in density


def test_per_task_density_reads_the_rerun_ladder(tmp_path: Path) -> None:
    first = tmp_path / "confirm"
    rerun = tmp_path / "confirm_rerun1"
    mark_beacons_enabled(first)
    mark_beacons_enabled(rerun)
    _record_beacon(first, "task_a", 0, fired=True)
    _record_beacon(first, "task_a", 1, fired=False)
    _record_beacon(rerun, "task_a", 1, fired=True)

    density = per_task_density([first, rerun], ["task_a"])

    assert density == {"task_a": 2 / 3}


def test_per_task_density_ignores_an_empty_beacon_file(tmp_path: Path) -> None:
    mark_beacons_enabled(tmp_path)
    _record_beacon(tmp_path, "task_a", 0, fired=False)

    density = per_task_density([tmp_path], ["task_a"])

    assert density == {"task_a": 0.0}


def test_per_task_density_escapes_a_glob_metacharacter_in_the_task_id(tmp_path: Path) -> None:
    """A task id names a directory; ``[``, ``*`` and ``?`` in it are not a pattern.

    The workspaces exist on disk under exactly that name, so globbing the raw id
    reads the bracket as a character class, matches nothing, and the task drops
    out of the affinity map without an error.
    """
    mark_beacons_enabled(tmp_path)
    ids = ["task[1]", "task*2", "task?3"]
    for tid in ids:
        _record_beacon(tmp_path, tid, 0, fired=True)

    density = per_task_density([tmp_path], ids)

    assert density == {"task[1]": 1.0, "task*2": 1.0, "task?3": 1.0}


def test_read_fired_tasks_escapes_a_glob_metacharacter_in_the_task_id(tmp_path: Path) -> None:
    mark_beacons_enabled(tmp_path)
    _record_beacon(tmp_path, "task[1]", 0, fired=True)
    _record_beacon(tmp_path, "task[1]", 1, fired=False)

    assert read_fired_tasks([tmp_path], ["task[1]"]) == {"task[1]"}
