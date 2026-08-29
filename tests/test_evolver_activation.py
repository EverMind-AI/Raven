"""The evolver's offline routing probe, exercised against the real catalog.

``dry_query`` is the only caller of ``LocalSkillCatalog`` outside ``raven/``,
and it lives in a tree the runtime may not import (the fifth contract), so the
package's own suite never touches it. It is covered here because a change to
the catalog's signature would otherwise break it in silence.
"""

from __future__ import annotations

from pathlib import Path

from evolver.activation import dry_query


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
