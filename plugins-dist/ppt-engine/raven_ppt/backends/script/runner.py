"""Running the author's program, and surviving it failing.

Everything here exists because the program is written by a model and may do
anything: not compile, compile and crash, crash halfway through after writing a
partial file, hang, or finish without writing anything at all. The deck that last
built is what a failed edit gets repaired against, so none of those outcomes may
destroy it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from raven_ppt.backends.script.blocks import level_that_separates_pages, page_sources
from raven_ppt.backends.script.submission import carries_a_program, submission_refusal
from raven_ppt.backends.script.workspace import (
    HelperSources,
    deck_path,
    provision,
    script_path,
    slide_lines_path,
    with_template_helpers,
)
from raven_ppt.contracts import BuildOutcome, DeckPlan, Project
from raven_ppt.services.template import bound
from raven_ppt.services.tidy import tidy

log = logging.getLogger(__name__)

BUILD_TIMEOUT_S = 300.0
MAX_OUTPUT_CHARS = 20_000

# Runs the script and records which of its lines created each slide. Reading that
# from a `# SLIDE n` comment would make annotation the author's job and let a
# stale number pair one page's render with another page's code; execution knows
# the answer exactly.
_RUNNER = """import json, os, runpy, sys

script = sys.argv[1]
record = os.environ.get("PPT_SLIDE_LINES")
sys.argv = [script]

created = []
if record:
    import inspect

    from pptx.slide import Slides

    original = Slides.add_slide
    target = os.path.realpath(script)

    def add_slide(self, *args, **kwargs):
        slide = original(self, *args, **kwargs)
        # Every frame in the script, outermost first. Which level identifies the
        # page depends on how the script is shaped, so record them all and let
        # the caller pick the level whose lines differ per slide.
        stack = [
            frame.lineno for frame in reversed(inspect.stack()) if os.path.realpath(frame.filename) == target
        ]
        created.append(stack)
        return slide

    Slides.add_slide = add_slide

try:
    runpy.run_path(script, run_name="__main__")
finally:
    if record:
        import hashlib

        with open(script, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        with open(record, "w", encoding="utf-8") as handle:
            # The digest of the text these line numbers were read from. Edit the
            # script and they point at whatever now sits on those lines.
            json.dump({"lines": created, "script_sha256": digest}, handle)
"""


class ScriptBackend:
    """Backend protocol over `run_script`, for a pipeline that holds a backend."""

    name = "script"

    def __init__(self, helpers: HelperSources | None = None) -> None:
        self.helpers = helpers

    async def compose(self, project: Project, plan: DeckPlan | None = None) -> BuildOutcome:
        del plan  # the program is the plan on this route
        return await run_script(project, helpers=self.helpers)


def _relative(project: Project, path: Path) -> str:
    """A path the file tools can take. Absolute is unambiguous and unusable: the
    file tools resolve against the workspace, so an author handed an absolute path
    writes it verbatim and the file lands outside the project -- which a live run
    did, then spent four calls recovering."""
    try:
        return str(path.relative_to(project.workspace))
    except ValueError:
        return str(path)


async def run_script(
    project: Project,
    script: str | None = None,
    *,
    helpers: HelperSources | None = None,
    timeout_s: float = BUILD_TIMEOUT_S,
) -> BuildOutcome:
    """Execute the author's program with the project's figures on hand.

    Passing `script` submits a whole program inline. Omitting it -- or passing
    something with no program in it -- runs the build.py already in the build
    directory, and that is the path meant for a real deck: a script long enough
    to lay out twenty pages runs to hundreds of lines, inline submission makes
    every revision cost a full regeneration of all of them, and a generation that
    long is long enough for the connection under it to drop, which returns
    nothing at all rather than a partial script.
    """
    # Asked once per build rather than passed in: whether this deck has a
    # template is a fact about the project on disk, and a caller that had to
    # remember to pass it would forget on the path that matters -- the design
    # pass rebuilding after an edit.
    template = bound(project)
    workdir = provision(project, with_template_helpers(helpers, template) if template else helpers)
    source = script_path(project)
    note = ""

    if script is not None and carries_a_program(script):
        refusal = submission_refusal(script, source)
        if refusal:
            return BuildOutcome(ok=False, stderr=refusal)
        source.write_text(script, encoding="utf-8")
    elif script is not None and source.is_file():
        note = (
            f"the submitted script held no program, so {source.name} was left as it was and that is what "
            "ran. Omitting script does the same thing; pass one only to replace the program."
        )
    elif not source.is_file():
        return BuildOutcome(
            ok=False,
            stderr=(
                f"no build script yet: write the program to {_relative(project, source)} with write_file -- "
                "that whole path, relative to the workspace, because a bare build/build.py lands somewhere "
                "this does not look. Then run the build again with just the project. ppt_layout.py, "
                "ppt_charts.py, ppt_shapes.py, ppt_theme.py and ppt_icons.py are already beside it."
                + (
                    "\nThis deck has a template: open it with "
                    "`Presentation(os.environ['PPT_TEMPLATE'])` instead of `Presentation()`, and take its "
                    "palette and type from ppt_theme, which holds the template's own and nothing else. "
                    "ppt_template.py is beside the others, to clone a page out of PPT_TEMPLATE_SOURCE."
                    if template
                    else ""
                )
            ),
        )

    target = deck_path(project)
    # The script writes to a staging file and the deck is replaced only on
    # success. A build that dies mid-script must not also destroy the last deck
    # that built, because that deck is what a failed edit gets repaired against.
    staging = target.with_name(target.name + ".building")
    lines_map = slide_lines_path(project)
    staging.unlink(missing_ok=True)
    lines_map.unlink(missing_ok=True)

    env = dict(os.environ)
    # The two paths the program needs, so it never has to guess or reach outside
    # the project.
    env["PPT_FIGURES_DIR"] = str(project.figures_dir)
    env["PPT_OUTPUT"] = str(staging)
    env["PPT_SLIDE_LINES"] = str(lines_map)
    # No MPLBACKEND: matplotlib is not a dependency of this route. It sat here as a
    # leftover from the schema route's SVG stack, where it belongs and where
    # pyproject still declares it -- and on this route the only thing it did was
    # suggest an import that raises. Charts here are drawn with python-pptx, which
    # keeps them editable and on the deck's palette.
    if template:
        # Two paths, because they answer different questions. The prepared copy is
        # what the deck is built in -- the template with its example pages removed,
        # so a page added to it inherits the master, the theme and the canvas. The
        # original still holds those pages, and it is the only place the two thirds
        # of them that python-pptx cannot redraw can be reached at all: a page is
        # cloned out of it and edited.
        env["PPT_TEMPLATE"] = str(template.prepared)
        env["PPT_TEMPLATE_SOURCE"] = str(template.source)

    runner = workdir / "_run_build.py"
    runner.write_text(_RUNNER, encoding="utf-8")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(runner),
        str(source),
        cwd=str(workdir),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        _save_failure(
            project,
            source,
            staging,
            "timeout",
            "",
            f"the build script exceeded {timeout_s:.0f}s and was stopped",
        )
        _discard(staging, lines_map)
        return BuildOutcome(ok=False, stderr=f"the build script exceeded {timeout_s:.0f}s and was stopped")

    stdout = raw_out.decode("utf-8", "replace")[-MAX_OUTPUT_CHARS:]
    stderr = raw_err.decode("utf-8", "replace")[-MAX_OUTPUT_CHARS:]

    if process.returncode != 0:
        # The line record describes the script that failed, not the deck on disk.
        _save_failure(project, source, staging, "crash", stdout, stderr or f"exit code {process.returncode}")
        _discard(staging, lines_map)
        return BuildOutcome(ok=False, stdout=stdout, stderr=stderr or f"exit code {process.returncode}", note=note)
    if not staging.is_file():
        # The runner recorded line numbers under this script's hash, but the deck
        # those numbers would be paired with is still the old one -- keeping the
        # record would create exactly the mismatch the hash guards against.
        message = f"the script finished but wrote no deck at {target.name}; save to os.environ['PPT_OUTPUT']"
        _save_failure(project, source, staging, "no_output", stdout, message)
        _discard(staging, lines_map)
        return BuildOutcome(
            ok=False,
            stdout=stdout,
            note=note,
            stderr=message,
        )

    # The defects every deck arrives with, corrected before anything measures it:
    # the empty placeholders a template's layout leaves on a page ("Click to add
    # title" in Office), the Office gallery style python-pptx stamps on every table,
    # and the theme drop shadow it references on every shape it draws. Only the last
    # of those shows up in a render -- see raven_ppt/services/tidy.py.
    for line in tidy(staging):
        log.debug("tidy: %s", line)
    staging.replace(target)
    sources, digest = _sources(source, lines_map)
    return BuildOutcome(
        ok=True,
        pptx_path=target,
        pages=_count_slides(target),
        stdout=stdout,
        stderr=stderr,
        note=note,
        sources=sources,
        source_digest=digest,
    )


def _discard(*paths: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _save_failure(
    project: Project,
    source: Path,
    staging: Path,
    kind: str,
    stdout: str,
    stderr: str,
) -> Path:
    """Keep the failed script and logs so the next edit can continue from it."""
    root = project.review_dir / "build_failures"
    index = 1
    while (root / f"failure-{index:03d}").exists():
        index += 1
    destination = root / f"failure-{index:03d}"
    destination.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        shutil.copy2(source, destination / "build.py")
    if staging.is_file():
        shutil.copy2(staging, destination / "deck.pptx.building")
    (destination / "stdout.txt").write_text(stdout, encoding="utf-8")
    (destination / "stderr.txt").write_text(stderr, encoding="utf-8")
    (destination / "failure.json").write_text(
        json.dumps({"kind": kind, "script": str(source), "staging": str(staging)}, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def _sources(source: Path, lines_map: Path):
    """Page spans from the execution record, or nothing if it cannot be trusted.

    Line numbers are only true of the text they were read from, so a record whose
    digest no longer matches the script is discarded rather than believed. A
    caller getting an empty tuple is being told the pages cannot be told apart,
    which is a finding -- not a reason to guess.
    """
    try:
        payload = json.loads(lines_map.read_text(encoding="utf-8"))
        body = source.read_bytes()
    except (OSError, ValueError):
        return (), ""
    digest = hashlib.sha256(body).hexdigest()
    if payload.get("script_sha256") != digest:
        return (), ""
    stacks = payload.get("lines")
    if not isinstance(stacks, list) or not stacks:
        return (), digest
    chains = [[int(v) for v in stack] for stack in stacks if isinstance(stack, list) and stack]
    if len(chains) != len(stacks):
        return (), digest
    created = level_that_separates_pages(chains)
    if not created:
        return (), digest
    lines = body.decode("utf-8", "replace").splitlines(keepends=True)
    return page_sources(lines, created), digest


def _count_slides(pptx_path: Path) -> int:
    from pptx import Presentation

    return len(Presentation(str(pptx_path)).slides)
