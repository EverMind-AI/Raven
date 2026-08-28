"""Every checkable claim in the skill, verified against the code that has to back it."""

import json
import re
import sys
from pathlib import Path

# Derived rather than written down: this runs as a subprocess, so it cannot import
# the test package to be told where the checkout is, and a path typed in here is one
# machine's path.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# The skill is an entry document plus the references it loads on demand, and a claim
# is no less checkable for living in one of those: the icon table and the fifteen
# chart signatures moved to `references/` and the assertions over them must follow,
# or layering would have quietly turned them off. Two of them were exactly that
# quiet -- 11b skips a signature it cannot find printed, so all fifteen chart
# signatures stopped being compared parameter-by-parameter the moment they moved,
# without a single failure to say so.
#
# `sorted()` rather than a glob's order, so the join is the same on every machine and
# a phrase that lands on a file boundary cannot pass here and fail in CI.
_SKILL_DIR = ROOT / "raven/memory_engine/skills/ppt-script-authoring"
entry = (_SKILL_DIR / "SKILL.md").read_text()
# Kept per page as well as joined: a claim about *where* something is said cannot be
# made against the join, which has no page boundaries left in it (11d).
_PAGES = {"SKILL.md": entry, **{p.name: p.read_text() for p in sorted(_SKILL_DIR.glob("references/*.md"))}}
skill = "\n".join(_PAGES.values())

from raven.ppt.contracts.findings import Severity
from raven.ppt.profiles import registry
from raven.ppt.services.assets import fonts, icons, script_helpers, shapes, themes
from raven.ppt.services.gates.registry import DISPATCH
from raven.ppt.tools.assembly import build_ppt_tools

fails, checks = [], 0


def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        fails.append(what)


# 1. every ppt_* tool the skill names is registered
tools = {t.name for t in build_ppt_tools(Path("/tmp/skillcheck"))}
named = set(re.findall(r"\bppt_[a-z_]+", skill)) - {
    "ppt_theme",
    "ppt_icons",
    "ppt_layout",
    "ppt_charts",
    "ppt_shapes",
    "ppt_template",
}
# ppt_template is both a tool and a helper module; it is a tool, so keep it
named.add("ppt_template")
for tool in sorted(named):
    ok(tool in tools, f"skill names {tool} which is not registered (registered: {sorted(tools)})")

# 2. every tool that exists is mentioned, so the skill does not hide one
for tool in sorted(tools):
    ok(tool in skill, f"{tool} exists and the skill never mentions it")

# 3. the palette: one theme, and it is the template's own.
#
# This used to hold the skill against the ten reviewed themes -- every id had to
# appear in a list, and the six measured faces had to be set-equal to
# `MEASURED_SAFE_FONTS`. Both froze a path that is not reachable: `prepare` binds a
# bundled default when the user gives no template (`fallback_default_template`), and
# `with_template_helpers` then replaces the ten themes with the template's one. So the
# skill's menu of ten named a choice the author does not have, its "pick the one whose
# character suits the subject" was an instruction into a `KeyError`, and picking the
# wrong answer is what `house_style` refuses the deck for. The test was holding the
# document to the wrong contract. These hold it to the real one.
_prepare = (ROOT / "raven/ppt/stages/prepare.py").read_text()
_workspace = (ROOT / "raven/ppt/backends/script/workspace.py").read_text()
ok("fallback_default_template()" in _prepare, "the skill says a template is always bound and prepare has no fallback")
ok(
    "{theme_name(template.inventory): theme_of(template.inventory, template.palette)}" in _workspace,
    "the skill says a bound template leaves one entry in THEMES and the build writes more",
)
ok(
    "template.palette" in _workspace,
    "the skill says the palette an author states reaches every page, and the build ignores it",
)
ok("next(iter(THEMES))" in skill, "the skill's palette sample must take the one entry THEMES holds, by iteration")
ok(
    not re.findall(r"THEMES\[\"[a-z-]+\"\]", skill),
    "the skill's code hard-codes a theme id; the one entry is named after the template's file",
)
for theme_id in sorted(themes.THEMES):
    ok(
        f"`{theme_id}`" not in skill,
        f"the skill offers theme {theme_id} as a choice; a bound template leaves exactly one",
    )

# 4. fonts, Latin and CJK. The Latin face is the template's -- nothing measures an
# author's deck against `MEASURED_SAFE_FONTS`; it constrains the shipped themes only
# (test_assets_themes.py), and those are what a template replaces.
for face in script_helpers.theme_catalog().values():
    ok(
        face["cjk_font_family"] in fonts.CJK_SAFE_FONTS,
        f"theme names CJK face {face['cjk_font_family']!r} that is not in the measured set",
    )
ok("cjk_font=HAN" in skill or "cjk_font=" in skill, "the skill promises a CJK companion and never shows how to set it")
ok(
    not re.search(r"six measured faces", skill),
    "the skill offers the six measured faces as a choice; inside a template the face is the template's",
)

# 4b. the reserve the entry document's card paragraph states. It says a measuring call
# scales a Latin run by the face it is told about and by the widest face it knows when
# it is told none -- which is the difference between the two `card_size` calls in the
# first example of 6.5, and the reason the row there is levelled to the second. Read off
# the projected module, because that is the copy an author's program imports.
_layout_source = (
    script_helpers.layout_module_source()
    if hasattr(script_helpers, "layout_module_source")
    else __import__("raven.ppt.services.assets.layout", fromlist=["x"]).layout_module_source()
)
ok(
    "_WIDEST_FACE = max(_FACE_WIDTH.values(), default=1.0)" in _layout_source,
    "the skill says an unnamed face reserves for the widest one and the module computes it some other way",
)
ok(
    "_FACE_WIDTH.get(face, _WIDEST_FACE)" in _layout_source,
    "the skill says a measuring call told no face reserves for the widest and _em_width does not fall back to it",
)

# 5. icon count, preset count, and helper names
ok(f"{len(icons.icon_names())} Tabler" in skill, f"icon count wrong; code has {len(icons.icon_names())}")
drawable = shapes.drawable_presets()
helper = script_helpers.script_helper_files()
# The count the skill prints is the vocabulary `ppt_shapes` exposes and not the size of
# the packaged catalogue: all 177 were rendered at their default adjustments and 68 were
# reviewed out, so `PRESET_NAMES` is 109 while the data still carries every preset the
# measurements read geometry from. Taken off the module's own table rather than restated
# here, because a number in two places drifts in one of them -- and the table is run
# rather than matched: a regex over it found 65 of the 68, since the three names that end
# a one-line group carry no comma, and it reported a vocabulary three names too large
# without failing.
_table = "_OUT_OF_VOCABULARY" + helper["ppt_shapes.py"].split("_OUT_OF_VOCABULARY")[1].split("PRESET_NAMES =")[0]
_namespace: dict = {}
exec(_table, _namespace)
_dropped = set(_namespace["_OUT_OF_VOCABULARY"])
_vocabulary = [name for name in drawable if name not in _dropped]
ok(
    _dropped and _dropped < set(drawable),
    "ppt_shapes' out-of-vocabulary table is empty, or names something that is not a preset",
)
ok(f"{len(_vocabulary)} Office preset" in skill, f"preset count wrong; the vocabulary has {len(_vocabulary)}")
for name in re.findall(r"`(chevron|rightArrow|flowChartDecision|roundRect)`", skill):
    ok(name in _vocabulary, f"the skill names preset {name!r} and the vocabulary does not carry it")
for module in ("ppt_theme", "ppt_icons", "ppt_layout", "ppt_charts", "ppt_shapes"):
    ok(f"{module}.py" in helper or f"from {module} import" in skill, f"{module} named but not emitted")
for fn in ("add_icon", "find_icons", "ICON_NAMES"):
    ok(fn in helper["ppt_icons.py"], f"skill promises {fn} and ppt_icons.py does not define it")
# Every name in the skill's icon table resolves. The table is hand-written and the set
# it names is thirteen hundred; a name that drifts out of the data reads to an author
# as one it may write, and fails inside the build with the error blamed on its program.
_icon_table = re.search(r"\| Charts and measurement \|.*?\n\n", skill, re.S)
ok(_icon_table is not None, "the skill no longer carries the icon table")
if _icon_table:
    _listed = {name for row in _icon_table.group(0).splitlines() for name in re.findall(r"`([a-z0-9_]+)`", row)}
    _unknown = sorted(_listed - set(icons.icon_names()))
    ok(not _unknown, f"the icon table names {len(_unknown)} icons that do not exist: {_unknown[:5]}")
    ok(len(_listed) > 300, f"the icon table shrank to {len(_listed)} names")
for fn in ("THEMES", "rgb"):
    ok(fn in helper["ppt_theme.py"], f"skill promises {fn} and ppt_theme.py does not define it")
# Every name the skill imports from a helper is defined there. Read out of the skill
# rather than listed here, so a helper the skill starts promising is covered without
# this file being edited -- the failure it stops is an author following the skill into
# an ImportError halfway through a build.
# 11b. every signature the skill prints matches the function it names. The table
# exists so an author does not read 19k tokens of helper source to find a parameter;
# a table that has drifted sends them back to the source, which is worse than having
# no table. Parameter names rather than the whole line, because the skill wraps and
# writes "•" where the source writes an escape.
import ast


def _as_written(default):
    """A default as the skill would print it, or None for one no table can carry."""
    if isinstance(default, ast.Name):
        return default.id
    if isinstance(default, ast.Constant):
        return repr(default.value) if isinstance(default.value, str) else str(default.value)
    if isinstance(default, ast.UnaryOp) and isinstance(default.op, ast.USub):
        return f"-{_as_written(default.operand)}"
    if isinstance(default, (ast.Tuple, ast.List)) and not default.elts:
        return "()"
    return None


def _same_default(shown, wrote):
    """Whether the table's default and the source's are the same default.

    Written two ways for the same value all over: `()` for an empty tuple and `[]` for
    the same, `0` and `0.0`, `"center"` in one quote or the other, and the table's own
    backticks around either.
    """
    shown, wrote = shown.strip("`,.*"), wrote.strip("`")
    if shown == wrote:
        return True
    if {shown, wrote} <= {"()", "[]", "None"}:
        return shown == wrote or {shown, wrote} == {"()", "[]"}
    unquoted = (shown.strip("\"'"), wrote.strip("\"'"))
    if unquoted[0] == unquoted[1]:
        return True
    try:
        return float(shown) == float(wrote)
    except ValueError:
        return False


# The skill wraps, so a signature is read off the text with its line breaks closed up:
# `add_icon(slide, name, left,\ntop, size, colour, width_pt=1.75)` was four parameters
# short of its own signature when each line was searched on its own. Only what is inside
# backticks, which is where a signature is printed and where an example in a fenced block
# is not.
_UNWRAPPED = re.sub(r"[ \t]*\n[ \t]*", " ", skill)

_SIGNATURES = {
    "ppt_layout": script_helpers.layout_module_source()
    if hasattr(script_helpers, "layout_module_source")
    else __import__("raven.ppt.services.assets.layout", fromlist=["x"]).layout_module_source(),
    "ppt_charts": __import__("raven.ppt.services.assets.charts", fromlist=["x"]).chart_module_source(),
    "ppt_shapes": script_helpers.shape_module_source(),
    "ppt_icons": script_helpers.icon_module_source(),
    "ppt_theme": script_helpers.theme_module_source(),
    "ppt_template": __import__("raven.ppt.services.template.compose", fromlist=["x"]).helper_source(),
}
# The helpers an author may not be told about. Every public function of a projected
# module is one their program can `from ppt_x import` -- the top-level `def`s the
# module emits, which is the same set `exec`ing the projection and asking `inspect`
# for its own functions returns, bar `ppt_template.page`, an alias kept working for
# programs written against an older reference and deliberately not taught (two modules
# cannot both offer `page` to one import list).
#
# Named here one by one, with the reason, rather than matched by a pattern: a pattern
# is how the next helper joins them without anyone deciding it should.
_UNDOCUMENTED = {
    # The emitter, not part of what it emits. It returns this module's own text
    # (`Path(__file__).read_text()`) so the file can be written beside the build
    # program, and it is a function *in* the projection only because the projection
    # is the module's own source. An author's program never calls it.
    "ppt_template.helper_source",
}

for module, source in _SIGNATURES.items():
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        printed = re.findall(rf"`{node.name}\([^`]{{0,400}}`", _UNWRAPPED)
        # A helper the document never prints at all used to leave here on a `continue`,
        # which made the one gap this loop cannot see -- a function the skill does not
        # mention -- the one gap it also could not fail on. So a projected module could
        # grow a helper that never reached the skill, indefinitely, in silence.
        # `ppt_template.fill` did: the table taught `units`, `boxes`, `arrangement` and
        # `place`, and the function that writes items into the units those find was in
        # no line of the document.
        #
        # A printed *call* and not a printed signature, because those are two claims.
        # The table is where a parameter list is checked, and it is not the only place
        # a helper is taught: `rgb` is taught by the setup block every program starts
        # from, which is documentation. Only presence is read this loosely -- the
        # parameter and default comparisons below still read the table.
        if f"{module}.{node.name}" not in _UNDOCUMENTED:
            ok(
                re.search(rf"\b{node.name}\(", _UNWRAPPED),
                f"{module}.{node.name} is a helper an author can import and the skill never prints a call to it",
            )
        if not printed:
            continue
        wanted = [arg.arg for arg in node.args.args + node.args.kwonlyargs]
        missing = [name for name in wanted if not any(name in line for line in printed)]
        ok(not missing, f"the skill's signature for {module}.{node.name} omits {', '.join(missing)}")
        # And the default it prints for a parameter off the ramp is that parameter's own
        # default. Names were compared and values were not, so `card`'s table row went on
        # saying `size=LABEL_PT, title_size=BODY_PT` after both moved a step up the ramp
        # -- and a live page wrote `card(..., size=14, title_size=20)`, which is the row
        # read back as a number. A stale default is not a stale document; it is an
        # instruction to set the copy one step smaller than the deck's own body size.
        positional = node.args.posonlyargs + node.args.args
        defaults = list(zip(positional[len(positional) - len(node.args.defaults) :], node.args.defaults))
        defaults += list(zip(node.args.kwonlyargs, node.args.kw_defaults))
        # Only the line that prints the whole signature. Every other line naming the
        # function is a usage example, where `font=FONT` and `box=band` are arguments
        # rather than defaults -- compared as defaults, a correct example fails.
        named = [arg.arg for arg, _ in defaults]
        rows = [line for line in printed if all(re.search(rf"\b{name}=", line) for name in named)]
        for arg, default in defaults:
            wrote = _as_written(default)
            if wrote is None:
                continue
            for line in rows:
                shown = re.search(rf"\b{arg.arg}=(\(\)|\[\]|[^,)|\s]+)", line)
                if shown is None:
                    continue
                ok(
                    _same_default(shown.group(1), wrote),
                    f"the skill's signature for {module}.{node.name} gives {arg.arg} the default "
                    f"{shown.group(1)}, and the source says {wrote}",
                )

# 11d. a helper whose signature is printed in a table is written about somewhere else too.
#
# 11b holds the printed signature to the source; this holds the document to the author.
# One live run imported `lines_needed`, `fits`, `text_size`, `points`, `picture_size`,
# `whether_a_chart_fits`, `the_smallest_box_a_chart_needs`, `add_icon`, `find_icons` and
# `drop_shape` and called none of them -- and every one of those names was in a
# signature table already, so "does the document name it" was the wrong question. A
# table row says what a call looks like; what makes an author reach for one is a
# sentence saying what goes wrong without it, and the helpers that do get called
# (`find_presets`, `preset_adjustments`) are the ones that have one.
#
# The cut is the table *block* -- the run of `|` lines the signature is printed in --
# and not the section, because there is more than one signature table (section 4 has
# four -- the frame, the measurements, the chart measurements, the template -- and
# charts.md and shapes.md carry their own) and section numbers move, while the run of
# `|` lines around a signature is where it is printed on any day. Everything outside
# that block counts as saying when to call it: prose, a
# callout, a worked example, and another table, because charts.md's "which form" table
# is a column of triggers ("What the value is at every row-column intersection" ->
# `heatmap`) and reads to an author exactly as a sentence would. The references count
# as elsewhere for the same reason 5b reads them at all: they are written into the
# build directory beside the modules, so a trigger in references/charts.md is one the
# author can open.
#
# Only a signature printed in a table puts a helper in this population. One printed in
# a sentence -- `the_ink_an_icon_covers(name, size)` is the box the strokes really
# cover, `ppt_icons` gives `add_icon(...)` -- arrives with the sentence around it; one
# printed in a cell arrives with nothing but a paraphrase of itself.
_ICON_NAMES = set(icons.icon_names())


def _blocks(page: str):
    """The page as runs of one kind of line: fenced code, table rows, prose.

    Lines are joined inside a run for the reason `_UNWRAPPED` exists -- the entry
    document is hand-wrapped, so a signature printed in prose spans two lines -- while
    the runs keep the boundary this check needs, which the join throws away.
    """
    runs: list[tuple[str, str, int]] = []
    held: list[str] = []
    kind, fenced, opened = None, False, 1
    for number, line in enumerate(page.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            fenced, this = not fenced, "fence"
        elif fenced:
            this = "fence"
        else:
            this = "table" if line.lstrip().startswith("|") else "prose"
        if this != kind and held:
            runs.append((kind, " ".join(h.strip() for h in held), opened))
            held, opened = [], number
        kind = this
        held.append(line)
    if held:
        runs.append((kind, " ".join(h.strip() for h in held), opened))
    return runs


def _is_icon_catalogue(kind: str, text: str) -> bool:
    """Whether this table is icon data rather than anything about a helper.

    references/icons.md lists the names worth knowing by heart and several of them --
    `table`, `plane`, `timeline`, `boxes`, `stack`, `mark` -- are helper names too.
    Counted as mentions they hand a pass to a helper the document never discusses:
    `boxes` was "mentioned" in the icon table and in `overlaps(boxes, tolerance=0.01)`,
    which is a parameter of something else. Recognised by how many of its own cells are
    packaged icon names rather than by the heading above it, so renaming the section
    does not turn the whole table back into evidence.
    """
    return kind == "table" and sum(1 for s in re.findall(r"`([^`]+)`", text) if s in _ICON_NAMES) > 20


_RUNS = [(page, kind, text, line) for page, body in _PAGES.items() for kind, text, line in _blocks(body)]
_public, _tabled = 0, 0

for module, source in _SIGNATURES.items():
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        params = [a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs]
        _public += 1
        in_a_table, mentioned = [], []
        for page, kind, text, line in _RUNS:
            if _is_icon_catalogue(kind, text):
                continue
            spans = re.findall(r"`[^`]*`", text)
            # A reference to this helper is a call to it, or its name alone in
            # backticks. Its name inside another signature's parameter list is not one,
            # and neither is an import: `from ppt_icons import add_icon` is what the run
            # that called nothing wrote, so counting it would count the defect as its
            # own cure. Prose is searched inside its code spans only -- `points`,
            # `table`, `card`, `rule` and `line` are also English words, and a document
            # about slides says them on nearly every page.
            called = re.search(rf"(?<![\w.]){node.name}\(", text if kind == "fence" else " ".join(spans))
            bare = any(s.strip("`") == node.name for s in spans)
            # A signature and not a use: the cell prints the call with every parameter
            # the function takes. 11b fails on a table that prints fewer, so a row this
            # reads as a mention is already a failure there.
            if kind == "table" and any(s.startswith(f"`{node.name}(") and all(p in s for p in params) for s in spans):
                in_a_table.append(f"{page}:{line}")
            elif called or bare:
                mentioned.append(f"{page}:{line}")
        _tabled += bool(in_a_table)
        ok(
            not in_a_table or mentioned,
            f"{module}.{node.name} is printed as a signature ({', '.join(in_a_table)}) and written about nowhere "
            "else -- no sentence, callout, example or other table says when to call it, which is the shape of "
            "every helper a live run imported and never called",
        )

# And a guard on the population, because the check above only sees a helper whose
# signature is printed in a table: rewriting the tables as prose or as a fenced block
# would take every helper out of it and leave 0 failures, which reads as the document
# having improved. Most of what the six modules export is in one of those tables today.
ok(
    _tabled * 2 > _public,
    f"only {_tabled} of {_public} public helpers have their signature printed in a table; "
    "the trigger-sentence check above now covers almost nothing",
)

# 11f. the two measuring helpers the document used to teach in prose only.
#
# `fits` and `card_body_box` were named seven times across the entry document and called
# in no example anywhere -- and the first example of 6.5 shipped a card whose copy the
# render set one line past its own bottom edge, with the call that refuses that height
# described three paragraphs above it and demonstrated nowhere. A helper an author has
# only ever read about is one they do not reach for, which 11d makes the same argument
# about from the other side: there it is a helper with no sentence, here it is a helper
# with no line of code.
_in_an_example = " ".join(text for _, kind, text, _ in _RUNS if kind == "fence")
for _guard in ("fits(", "card_body_box("):
    ok(
        _guard in _in_an_example,
        f"the document teaches {_guard} in prose and no example in it or in the references calls it",
    )

for module in ("ppt_layout", "ppt_charts", "ppt_icons", "ppt_shapes", "ppt_theme", "ppt_template"):
    for names in re.findall(rf"from {module} import ([^\n#]+)", skill):
        for name in (n.strip() for n in names.split(",")):
            if not name or not name.replace("_", "").isalnum():
                continue
            body = helper.get(f"{module}.py")
            if body is None:  # ppt_template is written by the template service
                continue
            ok(
                f"def {name}" in body or f"{name} =" in body or f"class {name}" in body,
                f"skill imports {name} from {module} and it is not defined there",
            )

# 5b. every chart the module draws is named in the skill's table. A primitive the
# author cannot find is one it draws by hand out of rectangles, which is the defect
# ppt_charts exists to close -- so the table has to list all of them.
# Off `chart_names`, which is the module's own answer to "what does this draw":
# `ppt_charts` also carries the two functions that measure a chart before it is
# drawn, and those take a chart rather than a slide and belong in the skill's prose
# rather than in its table of forms.
drawn = list(__import__("raven.ppt.services.assets.charts", fromlist=["x"]).chart_names())
ok(len(drawn) >= 15, f"ppt_charts defines {len(drawn)} charts and there should be at least 15")
for name in drawn:
    ok(f"`{name}(" in skill, f"ppt_charts draws {name} and the skill's table never names it")

# 6. the theme fields the code sample reads.
#
# Off the entries `theme_catalog` actually builds rather than off
# `_EXPORTED_THEME_FIELDS`, which is only the palette half of one: the emitter adds
# `cjk_font_family` to every entry beside that tuple, and a document reading it -- as
# `ppt_theme.py`'s own docstring does -- was failed here for reading a field the build
# demonstrably writes. Reading the built entry keeps every field the tuple carried and
# covers whatever is added beside it next.
_THEME_FIELDS = set().union(*(set(entry) for entry in script_helpers.theme_catalog().values()))
ok("cjk_font_family" in _THEME_FIELDS, "the theme the build writes carries no CJK face")

for field in re.findall(r'T\["([a-z_]+)"\]', skill):
    ok(field in _THEME_FIELDS, f"code sample reads T[{field!r}] which is not exported")

# 7. type floors
# Off the measurement that enforces them, which is now the only place they are
# stated: the design pass's `TypeFloors` quoted the same two numbers to its brief and
# went with it.
from raven.ppt.services.measure.type_size import BODY_FLOOR_PT, MIN_FLOOR_PT

ok(f"**{BODY_FLOOR_PT:g}pt**" in skill, f"body floor drift; code says {BODY_FLOOR_PT}")
ok(f"**{MIN_FLOOR_PT:g}pt**" in skill, f"min floor drift; code says {MIN_FLOOR_PT}")

# 8. env vars
runner = (ROOT / "raven/ppt/backends/script/runner.py").read_text()
for var in re.findall(r"`(PPT_[A-Z_]+)`", skill):
    ok(f'"{var}"' in runner, f"skill names {var}; the runner never sets it")
for var in re.findall(r'env\["(PPT_[A-Z_]+)"\]', runner):
    ok(var in skill or var == "PPT_SLIDE_LINES", f"the runner sets {var} and the skill never says so")

# 9. template helpers
compose = (ROOT / "raven/ppt/services/template/compose.py").read_text()
for fn in ("clone_page", "replace_text", "replace_picture", "drop_shape"):
    ok(f"def {fn}" in compose, f"skill promises {fn} and compose.py does not define it")

# 10. what refuses a deck and what only reports, held against the code in both
# directions.
#
# This used to be six substring checks in one direction: a phrase the refused
# paragraph had to contain, a phrase the reported paragraph had to contain, and
# nothing at all stopping a kind from being described on the wrong side. It said
# nothing while the section drifted on a dozen counts -- `word_collision` written up
# as a report when it refuses, a refusal for "a number no source printed" that no
# code has emitted since the fact gate was deleted, and four kinds that stop a
# publication (`unplaced_figure`, `house_page`, `figure`,
# `design_pass_broke_the_build`) never mentioned at all. One of those substrings even
# asserted the wrong section outright, freezing the error.
#
# So: one phrase per kind, and three assertions over the join. A refusing kind's
# phrase must be in the refused paragraph and out of the reported one; a warning's
# the other way round; and every kind that can refuse a deck on this route has to be
# in the map, so a new one cannot be added in silence.
_KIND_PHRASES = {
    # Refusals: BLOCKING at the check, or fatal by this route's own declaration.
    "citation": "citing one figure while showing another",
    "page_budget": "length the brief did not agree",
    "language": "the wrong language",
    "house_style": "not the bound template's",
    "unmapped_page": "cannot map back to",
    "unseen_page": "never been shown",
    "unplaced_figure": "promised a figure and that shows no picture",
    "unplaced_table": "planned a table and that shows none",
    "literal_escape": "printing an escape",
    "covered_shape": "hidden behind an opaque shape",
    "unreadable": "cannot make out",
    "word_collision": "colliding in the render",
    "placeholder_copy": "placeholder text",
    "template_underlay": "new text boxes laid over",
    "figure": "figure id the catalogue does not hold",
    "house_page": "cover, index and closing",
    # Reports.
    "band": "filled colour bar",
    "type_floor": "type under the floors",
    "evidence": "too few content pages showing anything",
    "wide_table": "table too wide to read",
    "native_table": "wearing Office's own look",
    "table_grid": "fewer columns or rows than its plan names",
    "table_room": "no room for the table its plan describes",
    "rule_strike": "rule struck",
    "card_overflow": "escaping a card",
    "crowded_panel": "crowding its panel",
    "off_page": "over the page edge",
    "spilled_copy": "painted off the page",
    "over_layout_art": "on the layout's artwork",
    "flat_formula": "expression set as prose",
    "listed_claims": "read as a list to be read out",
    "orphan_line": "label the render broke",
    "wrapped_label": "in a box too narrow for it",
    "overset_copy": "copy that does not fit",
    "clipped_copy": "clips instead of wrapping",
    "excessive_whitespace": "large blank field",
    "unseparated_blocks": "no more air between them",
    "type_drift": "one slot the deck sets at several sizes",
    "type_scale": "not a step of the ramp",
    "title_row": "different left edges",
    "layout_variety": "nearly all resolve to one page structure",
    "page_mapping": "pages cannot be told apart",
    "template_adherence": "none of whose pages came from",
    "template_picture": "photographs still showing",
    "prototype_kept": "prototype other than the one its outline named",
    "unswept_citations": "cited page nobody opened",
    "invented_layout": "layout id the catalogue does not carry",
}

blocking = registry.get("script_author").blocking_kinds
# Whitespace collapsed before matching. The document is hand-wrapped at 88 columns, so
# where a phrase happens to break is an accident of the paragraph around it -- and a
# check that fails when a sentence is rewrapped is a check people learn to edit around.
_flat = " ".join(skill.split())
refused_section = _flat.split("**Refused**")[1].split("**Reported**")[0]
reported_section = _flat.split("**Reported**")[1]


def _refuses(kind: str) -> bool:
    """Whether this kind stops a publication on the script route.

    Two ways in, which is exactly what the skill kept getting wrong: the check's own
    severity, and the route's fatal list -- `unplaced_figure` is a WARNING that the
    route declares fatal, and it refused decks for a release while the skill did not
    mention it at all.
    """
    return kind in blocking or (kind in DISPATCH and DISPATCH[kind] is Severity.BLOCKING) or kind in _BLOCKS_OUTSIDE


# Kinds built outside the registry -- in the outline tool, the build stage -- that
# carry BLOCKING severity, and beside them every kind the code names at all. Read off
# the source rather than listed, because a list is the thing nobody updates: three of
# the four the skill was missing are here.
_BLOCKS_OUTSIDE = set()
_EMITTED = set()
for _source in sorted((ROOT / "raven/ppt").rglob("*.py")):
    _text = _source.read_text(encoding="utf-8")
    _EMITTED |= set(re.findall(r'kind="([a-z_]+)"', _text))
    for _kind in re.findall(r'kind="([a-z_]+)",\s*\n\s*severity=Severity\.BLOCKING', _text):
        _BLOCKS_OUTSIDE.add(_kind)

ok(_BLOCKS_OUTSIDE, "nothing in raven/ppt emits a blocking finding, which cannot be right")
for kind in sorted(_BLOCKS_OUTSIDE | set(blocking) | {k for k, sev in DISPATCH.items() if sev is Severity.BLOCKING}):
    ok(kind in _KIND_PHRASES, f"{kind} can refuse a deck and the skill's section 12 never describes it")

# The direction this map had no check in, and it kept a deleted kind alive for as long
# as the sentence describing it stayed put. `thin_contrast` was retired -- its row, its
# `checks()` entry and its threshold all deleted -- and `_refuses` answered False for
# the ordinary reason a warning does, so the loop below went on asserting that section
# 12 describes it under Reported, which it did. A gate whose whole job is keeping the
# document honest about gate kinds was holding the document to a kind the code no
# longer has. So: a kind is describable only while something still names it.
for kind in sorted(_KIND_PHRASES):
    ok(
        kind in DISPATCH or kind in _EMITTED,
        f"section 12 describes {kind}, which nothing in raven/ppt emits any more -- "
        "delete the sentence and this row together",
    )

for kind, phrase in sorted(_KIND_PHRASES.items()):
    if _refuses(kind):
        ok(phrase in refused_section, f"{kind} refuses the deck and section 12 does not say so ({phrase!r})")
        ok(
            phrase not in reported_section,
            f"{kind} refuses the deck and section 12 describes it under Reported ({phrase!r})",
        )
    else:
        ok(phrase in reported_section, f"{kind} is reported and section 12 does not say so ({phrase!r})")
        ok(
            phrase not in refused_section,
            f"{kind} only reports and section 12 describes it under Refused ({phrase!r})",
        )

# And the claims section 12 must NOT make. The fact gate was deleted (design doc D3a)
# and its promise outlived it here by a release: an author told a number is checked
# plans differently from one told it is not.
for gone in ("no source printed", "fact index", "checked against the materials"):
    ok(gone not in refused_section, f"section 12 still promises a gate that does not exist: {gone!r}")

# a route may not call fatal a kind the checks only report -- the band gate spent a
# release downgraded in one place and fatal in the other, and this line said nothing
# because it ended in `or True`. The full assertion lives in test_profiles.py.
ok(
    not {k for k in blocking if k in DISPATCH and DISPATCH[k].value != "blocking"},
    "a route refuses on a kind the gate only reports",
)

# 11. the script path and write_file mode.
#
# Taken off the function that writes the file rather than typed out here. The layout
# went from a deck per slug (`ppt_projects/<project>/`) to one deck per workspace, and
# a path spelled out in this file goes on asserting the old one against a document
# that has moved -- which is the same drift every other check here exists to catch.
from raven.ppt.backends.script.workspace import script_path
from raven.ppt.contracts.project import Project

_deck = Project(workspace=Path("/tmp/skillcheck"), slug="skillcheck")
_script_path = script_path(_deck).relative_to(_deck.workspace)
ok(str(_script_path) in skill, f"the script path is not the real one; the build writes {_script_path}")
fs = (ROOT / "raven/agent/tools/filesystem.py").read_text()
ok('"append"' in fs, "the skill says mode=append and write_file has no such mode")
ok('mode="append"' in skill, "the skill no longer names the append mode")
ok(
    "# SLIDE" in skill and "# SLIDE" in (ROOT / "raven/ppt/backends/script/blocks.py").read_text(),
    "the SLIDE banner is not what the block reader looks for",
)

# 11b. the two preconditions the skill calls refusals really are refusals
build_tool = (ROOT / "raven/ppt/tools/build.py").read_text()
build_stage = (ROOT / "raven/ppt/stages/build.py").read_text()
ok("has not been read" in build_tool, "the skill says the build refuses an unread task and it does not")
ok("no brief recorded" in build_tool, "the skill says the build refuses without a brief and it does not")
ok('kind="unseen_page"' in build_stage, "the skill says an unshown page refuses the deck and nothing emits that")
ok(
    "Severity.BLOCKING" in build_stage.split('kind="unseen_page"')[1][:200],
    "the unseen-page finding is not blocking, and the skill says it refuses",
)
# The box-width warning, in the one place it is now made. It used to be pinned to the
# section 10 checklist, where it was the sixth restatement of something `wrapped_label`
# and `orphan_line` already measure; section 4 is where it earns its length, because it
# is the geometry the author has to get right before the build can say anything.
ok("wraps it onto a second line" in _flat, "the box-width warning left the skill")
ok(
    "wrapped_label" in (ROOT / "raven/ppt/services/gates/registry.py").read_text(),
    "the skill promises a narrow-box finding and no check produces one",
)

# 11c. the outline stage's claims
outline_tool = (ROOT / "raven/ppt/tools/outline.py").read_text()
ok("ppt_outline" in skill, "the outline stage is not in the skill")
ok("no outline recorded" in build_tool, "the skill says the build refuses without an outline and it does not")
for promised in ("claim", "carries", "figures", "says", "needs"):
    ok(
        f'"{promised}"' in outline_tool,
        f"the skill names the outline field {promised!r} and the schema has no such field",
    )
ok("page_budget" in outline_tool, "the skill says the page count is checked at outline time and it is not")
# Whitespace-collapsed, like section 12's: the sentence is hand-wrapped and where it
# breaks is an accident of the paragraph around it.
ok(
    "template, the face `ppt_theme` hands you is the template's own" in _flat,
    "the skill no longer says the face inside a template is the template's own",
)

# 11c. the two places that tell an author which modules are beside the program name all
# of them. Both listed ppt_theme and ppt_icons and stopped -- so the workspace's own
# TOOLS.md and the message a first build answers with both left out ppt_layout,
# ppt_charts and ppt_shapes, which is every helper this skill teaches. The skill is one
# document an author may not have open; these two they cannot miss.
_TOOLS_DOC = (ROOT / "raven/templates/TOOLS.md").read_text(encoding="utf-8")
_FIRST_BUILD = (ROOT / "raven/ppt/backends/script/runner.py").read_text(encoding="utf-8")
for _module in sorted(name for name in script_helpers.script_helper_files() if name.endswith(".py")):
    ok(_module in _TOOLS_DOC, f"TOOLS.md does not tell an author that {_module} is beside the program")
    ok(_module in _FIRST_BUILD, f"the message a first build answers with does not mention {_module}")

# 11e. the capabilities a word search cannot find, because the word is already there.
#
# Each of these is a thing the route can do that the document only alluded to: the
# palette an author states, the example page read back as source, the program's own
# stdout, and the two fields of the brief that are not the three questions. A lexical
# sweep passed all of them -- `palette` appeared once as an aside, `decompiled page`
# once in a subordinate clause -- so what is asserted here is the shape of the claim
# and not the presence of the word.

_template_tool = next(t for t in build_ppt_tools(Path("/tmp/skillcheck")) if t.name == "ppt_template")
_brief_tool = next(t for t in build_ppt_tools(Path("/tmp/skillcheck")) if t.name == "ppt_brief")
_image_tool = next(t for t in build_ppt_tools(Path("/tmp/skillcheck")) if t.name == "ppt_generate_image")

# The palette. Three roles come off the file and the rest are mixed from them, which is
# what the skill now tells an author to correct rather than the `bg2` reading it used to
# name -- and `bg2` is no longer read at all, so the old sentence described a derivation
# that had gone.
from raven.ppt.services.template import palette as _palette_mod
from raven.ppt.services.template import theme as _theme_mod

ok("palette" in _template_tool.parameters["properties"], "the skill states a palette and the tool takes none")
ok(
    {name for name, _ in _theme_mod._ROLES} == {"background", "foreground", "accent"},
    "the skill says three roles are read off the template and the derivation reads others",
)
ok(
    'said.get("surface") or _plane(accent, ground)' in (ROOT / "raven/ppt/services/template/theme.py").read_text(),
    "the skill says a stated accent re-derives the plane and the plane is derived some other way",
)
for _role in ("surface", "accent_soft", "accent_ink", "grid", "muted"):
    ok(_role in _palette_mod.DERIVED, f"the skill says {_role} is a role the derivation finishes and it is not")
ok(
    "chart_series" in _flat and _palette_mod.SERIES == "chart_series",
    "the skill names chart_series as the role a stated accent does not move",
)

# The twelve bundled templates and the colour every one of them declares and none of
# them paints. Measured here rather than written down, because the count and the value
# are both claims in the document.
_bundled = sorted((ROOT / "raven/ppt/assets/templates").glob("*.pptx"))
ok(len(_bundled) == 12, f"the skill says twelve bundled templates and {len(_bundled)} ship")
ok(
    "all twelve bundled templates declare their second background as `#F0F0F0`" in _flat,
    "the skill no longer states what every bundled template declares and none of them paints",
)
try:
    from raven.ppt.services.template.inventory import inspect_template

    _seconds = set()
    for _one in _bundled:
        _held = dict(inspect_template(_one).theme_colours)
        _seconds.add(_held.get("lt2") or _held.get("bg2"))
    ok(_seconds == {"#F0F0F0"}, f"the skill says every bundled template declares #F0F0F0 and they declare {_seconds}")
except ImportError:  # pragma: no cover - python-pptx ships with the extra
    pass

# The example page read back as python-pptx. The skill alluded to "the decompiled page"
# and to a reference that "prints" the shape index, and never said which call produces
# either -- so the one way to reach the template's real numbers was named only in the
# tool schema.
from raven.ppt.services.template.decompile import _UNWRITABLE

ok("pages" in _template_tool.parameters["properties"], "the skill says a page can be read as source and it cannot")
_max_pages = _template_tool.parameters["properties"]["pages"].get("maxItems")
ok(_max_pages == 6, f"the skill says six pages a call and the schema allows {_max_pages}")
for _what in ("a custom-drawn shape", "a gradient", "a pattern fill", "a semi-transparent fill"):
    ok(_what in _flat, f"the skill no longer names {_what!r} as something the source cannot reproduce")
    ok(
        any(_what.lstrip("a ") in said for _, said in _UNWRITABLE),
        f"the skill says {_what!r} comes back as a comment and decompile does not name it",
    )
ok(
    'add_picture("template_' in _flat,
    "the skill no longer shows the picture line a read page hands back",
)
_decompile_src = (ROOT / "raven/ppt/services/template/decompile.py").read_text()
ok(
    'name = f"template_{ordinal:02d}.{image.ext}"' in _decompile_src,
    "the skill shows template_NN.png and the decompiler writes some other name",
)
ok(
    'head += [f"# {line}" for line in _needed_imports(self.source)]' in _decompile_src,
    "the skill says the imports head the block as comments and they are emitted some other way",
)

# The program's own stdout, which is the only channel the measuring helpers have.
from raven.ppt.backends.script.runner import MAX_OUTPUT_CHARS

ok('payload["stdout"] = outcome.stdout' in build_tool, "the skill says print() comes back and the reply drops it")
ok(
    f"{MAX_OUTPUT_CHARS:,}" in _flat,
    f"the skill states a stdout budget the runner does not keep; the runner returns {MAX_OUTPUT_CHARS}",
)

# The brief's fourth field, and the one beside it that binds differently.
for _field in ("forbidden", "notes"):
    ok(_field in _brief_tool.parameters["properties"], f"the skill names ppt_brief's {_field} and it has none")
ok('payload["forbidden"] = list(agreed.forbidden)' in build_tool, "the skill says every build quotes the rules back")
ok(
    ".notes" not in build_tool,
    "the skill says a note is kept with the brief and not restated, and the build restates it",
)

# The generated image's shape.
_ratios = _image_tool.parameters["properties"]["aspect_ratio"]["enum"]
for _ratio in _ratios:
    ok(f"`{_ratio}`" in skill, f"the skill omits the aspect ratio {_ratio}, which the tool accepts")
ok(
    _image_tool.parameters["properties"]["aspect_ratio"]["default"] == "16:9",
    "the skill says a generated image defaults to 16:9 and the tool defaults to something else",
)

# The layout catalogue's real range. Read off the shipped reference the way the outline
# gate reads it, so a passage added to layouts.md moves both the refusal and the
# document that describes it.
from raven.ppt.tools.outline import catalogue_ids

_known = catalogue_ids()
_structures = sorted((one for one in _known if one.startswith("P")), key=lambda one: int(one[1:]))
_modifiers = sorted((one for one in _known if one.startswith("M")), key=lambda one: int(one[1:]))
ok(
    f"`{_structures[0]}` to `{_structures[-1]}`" in _flat,
    f"the skill states a structure range the catalogue does not carry; it holds {_structures[0]}-{_structures[-1]}",
)
ok(
    f"`{_modifiers[0]}`\nto `{_modifiers[-1]}`" in skill or f"`{_modifiers[0]}` to `{_modifiers[-1]}`" in _flat,
    f"the skill states a modifier range the catalogue does not carry; it holds {_modifiers[0]}-{_modifiers[-1]}",
)

# 12. canvas
ok("13.3 x 7.5in" in skill, "canvas size claim changed")

# 13. frontmatter parses and says always.
#
# Off `entry`, not the join. Splitting the join would happen to work today -- the
# entry leads it and opens with `---`, so segment 1 is still its frontmatter -- but
# only for those two reasons, and `---` is also a horizontal rule: one reference
# growing a rule and one edit to the join's order are each enough to leave segment 1
# pointing at reference prose, where the regex finds no `metadata:` and this reads
# `None.group(1)`. Frontmatter belongs to the entry document, so name it.
head = entry.split("---")[1]
meta = json.loads(re.search(r"metadata: (\{.*\})", head).group(1))
ok(meta["raven"]["always"] is True, "the skill is not always-on")
ok("requires" not in meta["raven"], "the skill gates itself behind a requirement")

print(f"{checks} claims checked, {len(fails)} failed")
for f in fails:
    if f:
        print("  FAIL:", f)
