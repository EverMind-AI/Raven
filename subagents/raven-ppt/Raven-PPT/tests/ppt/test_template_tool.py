"""What the model sees when the user hands over a template.

Two calls, because the author has two questions. "What does this design look
like" is answered with pictures -- 85% of a template's visual elements are on its
example pages, so a list of layout names answers it barely at all. "How do I draw
a page in it" is answered with code, because an author given the loop that drew
six cards can draw eight, and an author given a description of them cannot.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pptx")

from raven.agent.tools.base import ToolResult  # noqa: E402
from raven.ppt.contracts import Project  # noqa: E402
from raven.ppt.tools.template import PptTemplateTool  # noqa: E402


class FakeViews:
    """Renders without LibreOffice. `pages` is asked for the template's own file."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.rendered: list[Path] = []

    async def pages(self, pptx: Path, out_dir: Path, numbers) -> dict[int, Path]:
        self.rendered.append(pptx)
        return await self.pages_of(pptx, out_dir, numbers)

    async def pdf(self, pptx: Path, out_dir: Path) -> Path | None:
        """The conversion the tool now pays for once and reads twice: pictures for the
        author, type sizes for the house style."""
        self.rendered.append(pptx)
        if not self.available:
            return None
        out_dir.mkdir(parents=True, exist_ok=True)
        pdf = out_dir / f"{pptx.stem}.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        return pdf

    async def pages_of(self, pdf: Path, out_dir: Path, numbers) -> dict[int, Path]:
        if not self.available:
            return {}
        out_dir.mkdir(parents=True, exist_ok=True)
        made = {}
        for number in numbers or ():
            path = out_dir / f"template-{number:02d}.png"
            path.write_bytes(b"\x89PNG")
            made[number] = path
        return made

    def data_uri(self, png: Path, budget: int | None = None) -> str:
        return f"data:image/png;base64,{png.stem}"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "uploads").mkdir()
    return tmp_path


@pytest.fixture
def house(workspace: Path, template_file) -> Path:
    return template_file(where=workspace / "uploads")


def _body(result) -> dict:
    return json.loads(result.model_text if isinstance(result, ToolResult) else result)


async def test_binding_a_template_by_path(workspace: Path, house: Path):
    """The user's entry point: they say "use this deck", and this is where the
    path lands. Everything downstream then asks one question -- is there a
    template -- rather than where it came from."""
    from raven.ppt.services.template import bound

    tool = PptTemplateTool(workspace, FakeViews())

    body = _body(await tool.execute(project="talk", path="uploads/house-style.pptx"))

    assert body["ok"]
    assert body["template"] == "house-style.pptx"
    assert body["example_pages"] == 2
    assert body["canvas_in"].startswith("13.33")
    assert body["layouts"]
    assert bound(Project(workspace=workspace, slug="talk")) is not None


async def test_binding_refreshes_the_script_workspace(workspace: Path, house: Path):
    marker = workspace / "provisioned"
    tool = PptTemplateTool(workspace, FakeViews(), provision=lambda _project: marker.write_text("ready"))

    await tool.execute(project="talk", path="uploads/house-style.pptx")

    assert marker.read_text() == "ready"


async def test_the_users_file_is_copied_not_taken(workspace: Path, house: Path):
    before = house.read_bytes()
    tool = PptTemplateTool(workspace, FakeViews())

    await tool.execute(project="talk", path="uploads/house-style.pptx")

    assert house.read_bytes() == before


async def test_all_example_pages_come_back_as_pictures(workspace: Path, house: Path):
    """Every example page is available so the model can choose and adapt a close fit."""
    views = FakeViews()
    tool = PptTemplateTool(workspace, views)

    result = await tool.execute(project="talk", path="uploads/house-style.pptx")

    assert isinstance(result, ToolResult)
    assert [block["type"] for block in result.blocks] == ["text", "image_url", "text", "image_url"]
    assert "Template page 1 -- the template's cover" in result.blocks[0]["text"]
    assert "Template page 2 -- the template's page" in result.blocks[2]["text"]
    body = _body(result)
    assert body["house_pages"] == {"cover": 1}
    assert len(body["template_pages"]) == 2
    next_step = body["next_step"]
    assert next_step.startswith("clone the template's structural pages")
    assert "adaptable prototypes" in body["content_pages"]


async def test_a_machine_that_cannot_render_still_binds(workspace: Path, house: Path):
    """LibreOffice is optional everywhere else in this package and stays optional
    here: without it the author reads the pages as code instead of looking at
    them, which is a worse workflow and not a broken one."""
    tool = PptTemplateTool(workspace, FakeViews(available=False))

    body = _body(await tool.execute(project="talk", path="uploads/house-style.pptx"))

    assert body["ok"]
    assert "code" in body["renders"]


async def test_the_binding_holds_without_the_path(workspace: Path, house: Path):
    tool = PptTemplateTool(workspace, FakeViews())
    await tool.execute(project="talk", path="uploads/house-style.pptx")

    body = _body(await tool.execute(project="talk"))

    assert body["ok"]
    assert body["template"] == "house-style.pptx"


async def test_asking_for_a_template_nobody_bound(workspace: Path):
    tool = PptTemplateTool(workspace, FakeViews())

    body = _body(await tool.execute(project="talk"))

    assert not body["ok"]
    assert "no template is bound" in body["error"]
    assert "ppt_template with the path" in body["hint"]


async def test_a_path_outside_the_workspace_is_refused(workspace: Path, tmp_path: Path, template_file):
    """Every path that arrives from a model is checked. This one is a file the
    user names, which makes it exactly the path worth checking."""
    outside = template_file(name="elsewhere.pptx", where=tmp_path.parent)
    tool = PptTemplateTool(workspace, FakeViews())

    body = _body(await tool.execute(project="talk", path=f"../{outside.name}"))

    assert not body["ok"]
    assert "could not be used as a template" in body["error"]


async def test_something_that_is_not_a_deck_is_refused(workspace: Path):
    (workspace / "uploads" / "notes.txt").write_text("not a deck")
    tool = PptTemplateTool(workspace, FakeViews())

    body = _body(await tool.execute(project="talk", path="uploads/notes.txt"))

    assert not body["ok"]
    assert ".pptx" in body["hint"]


async def test_pages_come_back_as_source_the_author_can_paste(workspace: Path, house: Path):
    tool = PptTemplateTool(workspace, FakeViews())
    await tool.execute(project="talk", path="uploads/house-style.pptx")

    result = await tool.execute(project="talk", pages=[1])

    assert isinstance(result, ToolResult)
    code = result.blocks[0]["text"]
    assert "# page 0 of the template" in code
    assert "slide.shapes.add_textbox" in code
    assert _body(result)["pages_read"] == [1]


async def test_a_pages_pictures_land_where_its_code_looks_for_them(workspace: Path, house: Path):
    """`add_picture("template_00.png", ...)` in the reference has to be a line the
    author can paste and run, and the program runs in the build directory."""
    tool = PptTemplateTool(workspace, FakeViews())
    await tool.execute(project="talk", path="uploads/house-style.pptx")

    result = await tool.execute(project="talk", pages=[1])

    build = Project(workspace=workspace, slug="talk").build_dir
    named = [line for line in result.blocks[0]["text"].splitlines() if "add_picture(" in line]
    assert named
    assert (build / named[0].split('"')[1]).is_file()


async def test_a_page_that_cannot_be_redrawn_says_so_and_names_the_way_round(workspace: Path, house: Path):
    """Two thirds of real template pages hold something python-pptx cannot write.
    The reply names those pages and the operation that gets them into the deck
    anyway, because an author that only sees code assumes code is enough."""
    tool = PptTemplateTool(workspace, FakeViews())
    await tool.execute(project="talk", path="uploads/house-style.pptx")

    body = _body(await tool.execute(project="talk", pages=[1]))

    assert body["cannot_be_redrawn"]["1"]
    assert "clone_page" in body["next_step"]


async def test_asking_for_pages_the_template_does_not_have(workspace: Path, house: Path):
    tool = PptTemplateTool(workspace, FakeViews())
    await tool.execute(project="talk", path="uploads/house-style.pptx")

    body = _body(await tool.execute(project="talk", pages=[40]))

    assert not body["ok"]
    assert "2 example pages" in body["hint"]


async def test_a_project_name_that_is_not_one(workspace: Path):
    tool = PptTemplateTool(workspace, FakeViews())

    body = _body(await tool.execute(project="Not A Slug", path="uploads/x.pptx"))

    assert not body["ok"]
    assert "not a usable project name" in body["error"]
