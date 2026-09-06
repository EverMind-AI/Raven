"""The script route: running a program a model wrote, and surviving it.

Most of these are regressions. Each names the run it came from, because the
reason a guard exists is the only thing that stops it being simplified away.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from raven_ppt.backends.script import (
    HelperSources,
    broken_page,
    carries_a_program,
    page_blocks,
    page_sources,
    provision,
    run_script,
    script_path,
    submission_refusal,
)
from raven_ppt.contracts import Project
from tests._ppt_engine_fixtures import deck, image, noise_image, noise_png, product_page, template_file  # noqa: F401

pytest.importorskip("pptx")

DECK = textwrap.dedent(
    """
    import os
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)


    def new_slide():
        return prs.slides.add_slide(prs.slide_layouts[6])


    def title(slide, text):
        box = slide.shapes.add_textbox(Inches(0.8), Inches(0.6), Inches(11), Inches(1))
        box.text_frame.text = text


    # SLIDE 1
    one = new_slide()
    title(one, "Unified video segmentation")

    # SLIDE 2
    two = new_slide()
    title(two, "Target queries")

    prs.save(os.environ["PPT_OUTPUT"])
    """
).lstrip()


@pytest.fixture()
def project(tmp_path: Path) -> Project:
    return Project(workspace=tmp_path, slug="tarvis")


async def _build(project: Project, script: str | None = None):
    return await run_script(project, script, timeout_s=120.0)


@pytest.mark.asyncio
async def test_a_program_builds_a_deck_and_says_which_lines_drew_each_page(project: Project) -> None:
    outcome = await _build(project, DECK)

    assert outcome.ok, outcome.stderr
    assert outcome.pages == 2
    assert outcome.pptx_path is not None and outcome.pptx_path.is_file()
    # Derived from execution, not from the banners: the pages are told apart by
    # the line that created each slide.
    assert [s.page for s in outcome.sources] == [1, 2]
    first, second = outcome.sources
    assert first.last_line == second.first_line, "page spans must abut, not overlap"
    assert outcome.source_digest


@pytest.mark.asyncio
async def test_the_helpers_are_waiting_beside_the_script(project: Project) -> None:
    provision(project, HelperSources(theme="THEMES = {}\n", icons="def add_icon(*a, **k):\n    pass\n"))
    assert (project.build_dir / "ppt_theme.py").is_file()
    assert (project.build_dir / "ppt_icons.py").is_file()


def test_the_real_assets_land_as_modules_the_author_can_import(project: Project, tmp_path: Path) -> None:
    """The names are the asset service's, not the backend's.

    `from ppt_theme import THEMES, rgb` is what the author is told to write, so
    the filenames come from the service that owns the contents. This runs the
    generated modules in a subprocess because they read their data file relative
    to their own location, which is the whole point of writing them to disk -- and
    because `ppt_shapes` imports two of its neighbours, which only works if the
    build directory really is one importable place.
    """
    import subprocess
    import sys

    from raven_ppt.backends.script import asset_helpers

    build = provision(project, asset_helpers())
    assert {p.name for p in build.iterdir()} == {
        "ppt_theme.py",
        "ppt_icons.py",
        "ppt_layout.py",
        "ppt_charts.py",
        "ppt_shapes.py",
        "themes.json",
        "icons.json",
        "icon_keywords.json",
        "shapes.json",
        # The skill's reference documents, which reach the author no other way:
        # the pool reads SKILL.md and nothing beside it, and the fence then
        # refuses the path its `references/...` links resolve to.
        "references",
    }
    assert {p.name for p in (build / "references").iterdir()} == {
        "charts.md",
        # What a finished page owes, which `ppt_review` judges the render by. It lands
        # here rather than only reaching the reviewer because a requirement the author
        # never saw is a requirement nobody agreed to.
        "design-requirements.md",
        "formulas.md",
        "icons.md",
        "layouts.md",
        # The layout registry's passages, one file per family, so a page loads the
        # worked code it needs and not the other nine hundred lines.
        "layouts-data.md",
        "layouts-figures.md",
        "layouts-multiples.md",
        "layouts-primitives.md",
        "layouts-type.md",
        "shapes.md",
        "tables.md",
    }
    probe = build / "probe.py"
    probe.write_text(
        "from ppt_theme import THEMES, rgb\n"
        "from ppt_icons import ICON_NAMES, add_icon\n"
        "from ppt_shapes import PRESET_NAMES, timeline\n"
        "print(len(THEMES), len(ICON_NAMES), len(PRESET_NAMES), rgb('#FFFFFF') is not None)\n",
        encoding="utf-8",
    )
    done = subprocess.run([sys.executable, "probe.py"], cwd=build, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    themes, icons, presets, colour = done.stdout.split()
    assert int(themes) == 10 and int(icons) > 100 and int(presets) == 109 and colour == "True"


@pytest.mark.asyncio
async def test_a_helper_the_author_edited_is_restored_on_the_next_build(project: Project) -> None:
    """A quietly modified helper fails in a way that reads as a deck bug."""
    provision(project, HelperSources(theme="THEMES = {'a': 1}\n"))
    (project.build_dir / "ppt_theme.py").write_text("broken\n", encoding="utf-8")
    provision(project, HelperSources(theme="THEMES = {'a': 1}\n"))
    assert (project.build_dir / "ppt_theme.py").read_text() == "THEMES = {'a': 1}\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("submission", [" ", "\n", "# use existing build.py"])
async def test_a_submission_with_no_program_runs_what_is_there(project: Project, submission: str) -> None:
    """Both halves matter, and each cost a run.

    The file must survive: `script="."` once replaced a working forty-kilobyte
    program and every later call failed on the wreckage. And the build must still
    happen: refusing all three of these in one run is what drove an author out of
    the tool and into exec, delivering a deck no gate had seen.
    """
    await _build(project, DECK)
    outcome = await _build(project, submission)

    assert outcome.ok, outcome.stderr
    assert outcome.pages == 2
    assert script_path(project).read_text(encoding="utf-8") == DECK
    assert "left as it was" in outcome.note


@pytest.mark.asyncio
async def test_text_that_is_not_python_is_refused_by_name(project: Project) -> None:
    await _build(project, DECK)
    outcome = await _build(project, ".")

    assert not outcome.ok
    assert "not valid python" in outcome.stderr
    assert script_path(project).read_text(encoding="utf-8") == DECK


@pytest.mark.asyncio
async def test_a_shim_that_runs_another_file_is_refused_with_a_working_skeleton(project: Project) -> None:
    outcome = await _build(project, "import runpy\nrunpy.run_path('real_build.py')\n")

    assert not outcome.ok
    assert "hands the work to another file" in outcome.stderr
    # Refusing without an alternative is what pushed one author out of the tool.
    assert "# SLIDE 1" in outcome.stderr


@pytest.mark.asyncio
async def test_a_fragment_much_smaller_than_the_program_is_refused(project: Project) -> None:
    await _build(project, DECK)
    outcome = await _build(project, "prs.save(os.environ['PPT_OUTPUT'])\n")

    assert not outcome.ok
    assert "fragment sent by mistake" in outcome.stderr
    assert script_path(project).read_text(encoding="utf-8") == DECK


@pytest.mark.asyncio
async def test_a_build_that_dies_leaves_the_last_deck_alone(project: Project) -> None:
    """That deck is what a failed edit gets repaired against."""
    good = await _build(project, DECK)
    assert good.ok and good.pptx_path is not None
    before = good.pptx_path.read_bytes()

    broken = await _build(project, DECK.replace('title(two, "Target queries")', "raise RuntimeError('boom')"))

    assert not broken.ok
    assert "boom" in broken.stderr
    assert good.pptx_path.read_bytes() == before
    failure = project.review_dir / "build_failures" / "failure-001"
    assert (failure / "build.py").is_file()
    assert "boom" in (failure / "stderr.txt").read_text(encoding="utf-8")
    assert (failure / "failure.json").is_file()


@pytest.mark.asyncio
async def test_a_failed_build_discards_the_line_record_it_can_no_longer_describe(project: Project) -> None:
    await _build(project, DECK)
    await _build(project, DECK.replace("prs.save", "raise RuntimeError('boom')  # prs.save"))
    assert not (project.build_dir / ".slide_lines.json").is_file()


@pytest.mark.asyncio
async def test_a_script_that_writes_nothing_is_told_where_to_save(project: Project) -> None:
    outcome = await _build(project, "from pptx import Presentation\nprs = Presentation()\n")

    assert not outcome.ok
    assert "PPT_OUTPUT" in outcome.stderr
    failure = project.review_dir / "build_failures" / "failure-001"
    assert (failure / "build.py").is_file()
    assert "PPT_OUTPUT" in (failure / "stderr.txt").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_with_no_script_yet_the_error_is_the_instruction(project: Project) -> None:
    """And the path in it is one the file tools can take.

    Absolute is unambiguous and unusable: `write_file` resolves against the
    workspace, so an author handed an absolute path writes it verbatim and the file
    lands outside the project. A live run did exactly that -- it wrote
    `build/build.py` from the description, got this message, and spent four calls
    recovering.
    """
    outcome = await _build(project)

    assert not outcome.ok
    assert str(script_path(project).relative_to(project.workspace)) in outcome.stderr
    assert str(project.workspace) not in outcome.stderr, "an absolute path is one write_file cannot take"
    assert "write_file" in outcome.stderr


@pytest.mark.asyncio
async def test_pages_drawn_by_a_loop_cannot_be_told_apart(project: Project) -> None:
    """One call site for every page, so there is no per-page block to hand over.

    Reported as an absence rather than guessed at: pairing a render with the
    wrong code is worse than saying the pages cannot be separated.
    """
    looped = textwrap.dedent(
        """
        import os
        from pptx import Presentation
        prs = Presentation()
        for text in ("a", "b", "c"):
            prs.slides.add_slide(prs.slide_layouts[6])
        prs.save(os.environ["PPT_OUTPUT"])
        """
    ).lstrip()
    outcome = await _build(project, looped)

    assert outcome.ok, outcome.stderr
    assert outcome.pages == 3
    assert outcome.sources == ()


def test_carries_a_program_separates_empty_from_wrong() -> None:
    assert carries_a_program("x = 1") is True
    assert carries_a_program("") is False
    assert carries_a_program("   \n\n") is False
    assert carries_a_program("# only a comment") is False
    # Not empty, only wrong -- so it goes on to be refused by name.
    assert carries_a_program(".") is True


def test_a_page_keeps_its_banner_when_the_block_is_taken_from_execution() -> None:
    lines = DECK.splitlines(keepends=True)
    created = [next(i for i, line in enumerate(lines, start=1) if "one = new_slide()" in line)]
    created.append(next(i for i, line in enumerate(lines, start=1) if "two = new_slide()" in line))
    sources = page_sources(lines, created)

    assert "# SLIDE 1" in "".join(lines[sources[0].first_line : sources[0].last_line])
    assert "# SLIDE 2" in "".join(lines[sources[1].first_line : sources[1].last_line])


def test_the_last_page_stops_at_the_save_call() -> None:
    lines = DECK.splitlines(keepends=True)
    blocks = page_blocks(lines)
    _, end = blocks[2]
    assert "prs.save" not in "".join(lines[blocks[2][0] : end])


def test_banner_blocks_do_not_overlap() -> None:
    lines = DECK.splitlines(keepends=True)
    blocks = page_blocks(lines)
    assert blocks[1][1] <= blocks[2][0]


def test_a_crash_inside_a_shared_helper_is_attributed_to_the_calling_page() -> None:
    """The frame that names the page is the call site, not the deepest frame."""
    stderr = (
        '  File "/w/build/_run_build.py", line 30, in <module>\n'
        '  File "/w/build/build.py", line 15, in title\n'
        '  File "/w/build/build.py", line 25, in <module>\n'
        "RuntimeError: boom\n"
    )
    lines = DECK.splitlines(keepends=True)
    marker = next(i for i, line in enumerate(lines) if "# SLIDE 2" in line)
    stderr = stderr.replace("line 25", f"line {marker + 3}")
    assert broken_page(DECK, stderr) == 2


def test_a_crash_in_the_shared_prelude_is_not_blamed_on_a_page() -> None:
    stderr = '  File "/w/build/build.py", line 2, in <module>\nImportError: no pptx\n'
    assert broken_page(DECK, stderr) is None


def test_the_runners_own_frames_are_never_mistaken_for_the_script() -> None:
    stderr = '  File "/w/build/_run_build.py", line 40, in <module>\nRuntimeError: boom\n'
    assert broken_page(DECK, stderr) is None


def test_refusal_returns_none_for_a_real_program(tmp_path: Path) -> None:
    assert submission_refusal(DECK, tmp_path / "build.py") is None


# --- building inside a template -------------------------------------------

TEMPLATE_DECK = textwrap.dedent(
    """
    import os
    from pptx import Presentation
    from pptx.util import Inches
    from ppt_template import clone_page, drop_shape, replace_text

    prs = Presentation(os.environ["PPT_TEMPLATE"])
    original = Presentation(os.environ["PPT_TEMPLATE_SOURCE"])

    # SLIDE 1
    one = prs.slides.add_slide(prs.slide_layouts[6])
    box = one.shapes.add_textbox(Inches(0.8), Inches(0.6), Inches(11), Inches(1))
    box.text_frame.text = "Written against the template"

    # SLIDE 2
    two = clone_page(prs, original.slides[0])
    heading = next(s for s in two.shapes if getattr(s, "has_text_frame", False))
    replace_text(heading, "Cloned out of the template")
    drop_shape(two.shapes[-1])

    prs.save(os.environ["PPT_OUTPUT"])
    """
).lstrip()


async def test_a_bound_template_reaches_the_program(tmp_path: Path, template_file):
    """The whole wiring, end to end: the deck is built in the prepared copy, a page
    is cloned out of the original, and the operations python-pptx does not have
    arrive as a module the program imports."""
    from pptx import Presentation

    from raven_ppt.services.template import bind

    project = Project(workspace=tmp_path, slug="talk")
    assert bind(template_file(), project) is not None

    outcome = await run_script(project, TEMPLATE_DECK)

    assert outcome.ok, outcome.stderr
    assert outcome.pages == 2
    assert (project.build_dir / "ppt_template.py").is_file()
    built = Presentation(str(outcome.pptx_path))
    assert built.slide_width == Presentation(str(template_file())).slide_width
    assert "Cloned out of the template" in "\n".join(
        shape.text_frame.text for shape in built.slides[1].shapes if getattr(shape, "has_text_frame", False)
    )


async def test_a_deck_without_a_template_gets_neither_the_paths_nor_the_module(tmp_path: Path):
    """A build directory holding an importable `ppt_template` for a deck with no
    template is an invitation to import it, and the failure that follows is about
    a file the author did not write."""
    project = Project(workspace=tmp_path, slug="talk")

    outcome = await run_script(
        project,
        textwrap.dedent(
            """
            import os
            from pptx import Presentation

            prs = Presentation()
            # SLIDE 1
            prs.slides.add_slide(prs.slide_layouts[6])
            print("template:", os.environ.get("PPT_TEMPLATE", "none"))
            prs.save(os.environ["PPT_OUTPUT"])
            """
        ).lstrip(),
    )

    assert outcome.ok, outcome.stderr
    assert "template: none" in outcome.stdout
    assert not (project.build_dir / "ppt_template.py").exists()


# A program of the shape the two defects came in: pages started from a layout that
# carries placeholders, and a table drawn with the layout helper.
TIDY_DECK = textwrap.dedent(
    """
    import os
    from pptx import Presentation
    from pptx.util import Inches
    from ppt_layout import Box, table, write
    from ppt_theme import THEMES

    TH = THEMES["ink-graphite"]
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)

    # SLIDE 1
    one = prs.slides.add_slide(prs.slide_layouts[1])
    one.placeholders[0].text_frame.text = "实验：单一共享模型全面超过专用模型"
    table(one, Box(0.7, 1.6, 8.0, 4.4), [["方法", "YTVIS AP"], ["TarViS", "48.3"], ["VITA", "45.7"]], TH)

    prs.save(os.environ["PPT_OUTPUT"])
    """
).lstrip()


@pytest.mark.asyncio
async def test_a_built_deck_carries_neither_defect_a_render_cannot_show(project: Project) -> None:
    """The guard, rather than the fix: `tidy` runs on the way out of every build.

    Both defects it corrects were in every deck this route delivered and neither is
    visible in a render, so a change that moved the deck past `tidy` -- another build
    path, a reordered runner -- would be invisible in exactly the same way. This test
    is the thing that would not be.
    """
    from pptx import Presentation

    from raven_ppt.backends.script.workspace import asset_helpers
    from raven_ppt.services.tidy import EDGES, GALLERY_STYLE, tidy

    outcome = await run_script(project, TIDY_DECK, helpers=asset_helpers(), timeout_s=120.0)
    assert outcome.ok, outcome.stderr

    namespace = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    slide = Presentation(str(outcome.pptx_path)).slides[0]
    empty = [
        shape.name
        for shape in slide.shapes
        if shape.is_placeholder and not (getattr(shape, "text_frame", None) and shape.text_frame.text.strip())
    ]
    assert empty == [], f"'Click to add' placeholders reached the delivered deck: {empty}"

    graphic = next(shape for shape in slide.shapes if getattr(shape, "has_table", False))
    styles = [(e.text or "").upper() for e in graphic.table._tbl.iter(f"{namespace}tableStyleId")]
    assert GALLERY_STYLE not in styles, "the Office gallery style reached the delivered deck"
    properties = graphic.table.cell(0, 0)._tc.find(f"{namespace}tcPr")
    order = [element.tag.replace(namespace, "") for element in properties]
    assert order[: len(EDGES)] == list(EDGES), f"cell borders out of the schema's order: {order}"

    # And it is idempotent, which is what makes running it on every build safe.
    assert tidy(outcome.pptx_path) == ()
