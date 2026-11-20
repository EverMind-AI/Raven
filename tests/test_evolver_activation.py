"""The evolver's offline routing probe, exercised against the real catalog.

``dry_query`` is the only caller of ``LocalSkillCatalog`` outside ``raven/``,
and it lives in a tree the runtime may not import (the fifth contract), so the
package's own suite never touches it. It is covered here because a change to
the catalog's signature would otherwise break it in silence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from evolver.activation import dry_query
from evolver.activation.ledger import (
    beacon_workspace,
    mark_beacons_enabled,
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


def test_read_fired_tasks_escapes_a_bracket_in_the_task_id(tmp_path: Path) -> None:
    """A task id names a directory; a bracket in it is not a character class.

    The workspace exists on disk under exactly that name, so globbing the raw id
    reads the bracket as pattern syntax, matches nothing, and the task drops out
    without an error. A bracket is also legal in a Windows filename, so this
    case runs on every platform.
    """
    mark_beacons_enabled(tmp_path)
    _record_beacon(tmp_path, "task[1]", 0, fired=True)
    _record_beacon(tmp_path, "task[1]", 1, fired=False)

    assert read_fired_tasks([tmp_path], ["task[1]"]) == {"task[1]"}


@pytest.mark.skipif(sys.platform == "win32", reason="'*' and '?' are reserved in Windows path components")
def test_read_fired_tasks_does_not_match_a_neighbour_workspace(tmp_path: Path) -> None:
    """``*`` and ``?`` in an id are literals, not wildcards.

    ``task*2`` names its workspace directory literally, but an unescaped glob
    expands the star as well and also matches ``taskX2_k0``. The neighbour fired
    and the task itself did not, so the false match reports ``task*2`` as fired
    and Gate-b credits it with an activation it never had.
    """
    mark_beacons_enabled(tmp_path)
    _record_beacon(tmp_path, "task*2", 0, fired=False)
    _record_beacon(tmp_path, "taskX2", 0, fired=True)
    _record_beacon(tmp_path, "task?3", 0, fired=False)
    _record_beacon(tmp_path, "taskY3", 0, fired=True)

    assert read_fired_tasks([tmp_path], ["task*2", "task?3"]) == set()


def test_read_fired_tasks_does_not_claim_a_neighbour_workspace_sharing_the_k_stem(tmp_path: Path) -> None:
    """``t`` names ``t_k*``, and a sibling task ``t_k1`` names ``t_k1_k*``.

    The same pattern reaches the sibling's workspace, whose beacon line says the
    sibling fired, not ``t``. Only the attempt index may follow ``<id>_k``, so
    ``t`` must read the empty ``t_k0`` and nothing else, while ``t_k1`` keeps
    reading its own workspace.
    """
    mark_beacons_enabled(tmp_path)
    _record_beacon(tmp_path, "t", 0, fired=False)
    _record_beacon(tmp_path, "t_k1", 0, fired=True)

    assert read_fired_tasks([tmp_path], ["t"]) == set()
    assert read_fired_tasks([tmp_path], ["t_k1"]) == {"t_k1"}


def test_read_fired_tasks_reads_the_attempt_index_of_a_short_id(tmp_path: Path) -> None:
    """Excluding the neighbour must not drop the task's own attempts."""
    mark_beacons_enabled(tmp_path)
    _record_beacon(tmp_path, "t", 0, fired=False)
    _record_beacon(tmp_path, "t", 12, fired=True)

    assert read_fired_tasks([tmp_path], ["t"]) == {"t"}
