# Tool Usage Notes

Signatures arrive through function calling. This file records the constraints that
are not visible in a signature.

## The build script

It goes at `deck/build/build.py` — that whole path, relative to the workspace.
A bare `build/build.py` lands somewhere the build does not look.

The program runs with python-pptx and Pillow, in the build directory, and reads its
paths from the environment: `PPT_OUTPUT`, where the program's last statement saves
it -- `prs.save(os.environ["PPT_OUTPUT"])`, there and nowhere else. A program that
draws every page and ends without that line is reported back as having built no
deck, and the pages are gone. Then `PPT_FIGURES_DIR`, and — when the deck has a
template — `PPT_TEMPLATE` (the template with its example pages removed, open this
one) and `PPT_TEMPLATE_SOURCE` (the original, to clone a page out of). Every figure
is reached through `PPT_FIGURES_DIR`: the program runs in the build directory and
there is no `figures/` under it, so a path written relative to one resolves to
nothing.

Beside it, rewritten on every build: `ppt_layout.py` (the page's regions, the copy
and table helpers, and the measurements that answer before anything is drawn),
`ppt_charts.py` (the charts, drawn as shapes), `ppt_shapes.py` (Office presets, and
the process and timeline layouts built on them), `ppt_theme.py` (palettes and `rgb`),
`ppt_icons.py` (`add_icon`, `find_icons`, `ICON_NAMES`), and `ppt_template.py`
(`prototype` and `clone_page` to copy a page in, `replace_text` and `replace_picture`
to write this deck's content into it, `units`, `boxes` and `place` to re-flow a
repeating run, `drop_shape` to remove a shape) when a template is bound. `deck/build/references/` beside them holds the tables,
charts, icons, shapes and formulas documents in full: read those rather than the
modules, whose source is an order of magnitude longer than the documents that
describe it. **Do not write a module into the build directory whose name shadows a
standard one** — a `copy.py` there breaks python-pptx itself, and the traceback will
not mention your file.

Draw the pages in `build.py` itself, one block per page. A `build.py` that runs
another file, or a loop that draws every page from one call, is refused: the build
matches each render back to the code that drew it by that block.

## exec

Available, and not the delivery path. A deck copied out by hand is a deck no gate
has seen. Publication happens inside `ppt_build`.
