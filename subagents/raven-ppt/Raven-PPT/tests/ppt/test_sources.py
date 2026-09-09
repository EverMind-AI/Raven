"""The deck's source set: what comes in, what is left, and what is said about it."""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation

from raven.ppt.contracts.project import Project
from raven.ppt.services.ingest import sources


def test_a_pptx_in_a_materials_folder_is_a_template_not_unreadable_material(tmp_path: Path) -> None:
    """The commonest way anyone hands over a template: it is in the folder.

    The first version of the left-behind report called it a file nothing could read,
    which is wrong and the opposite of the next move -- binding it as the house style.
    """
    workspace = tmp_path / "ws"
    materials = workspace / "papers"
    materials.mkdir(parents=True)
    (materials / "paper.md").write_text("TarViS reaches 91.2 mIoU.\n", encoding="utf-8")
    Presentation().save(str(materials / "house-style.pptx"))
    (materials / "notes.zip").write_bytes(b"PK\x03\x04rubbish")

    mirrored = sources.mirror(Project(workspace=workspace, slug="deck"), materials)

    assert mirrored.taken == 1
    assert mirrored.templates == ("house-style.pptx",)
    assert mirrored.left_behind == ("notes.zip",)


def test_mirroring_the_source_set_into_itself_changes_nothing(tmp_path: Path) -> None:
    """A model does ask for it, and doing it once rewrote a fetched paper's origin."""
    workspace = tmp_path / "ws"
    project = Project(workspace=workspace, slug="deck")
    folder = sources.sources_dir(project)
    folder.mkdir(parents=True)
    (folder / "paper.md").write_text("91.2 mIoU\n", encoding="utf-8")

    mirrored = sources.mirror(project, folder)

    assert (mirrored.taken, mirrored.dropped, mirrored.left_behind, mirrored.templates) == (0, 0, (), ())
