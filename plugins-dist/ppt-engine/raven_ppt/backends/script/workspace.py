"""The directory the author's program runs in, and what is waiting there.

The helpers are passed in rather than imported. They are a projection of the
asset service -- the reviewed themes and the icon set, rendered as two importable
modules -- and the backend's job is to put them where the script can reach them,
not to decide what is in them. Injected, this module is testable without any
assets at all, and the asset service stays free to change what it emits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from raven_ppt.contracts import Project


@dataclass(frozen=True)
class HelperSources:
    """Module sources to write beside the author's script.

    `theme` becomes `ppt_theme.py` and `icons` becomes `ppt_icons.py`, the two
    names the author is told to import. Extra entries are written verbatim, which
    is how a route adds a helper without this module learning about it.
    """

    theme: str = ""
    icons: str = ""
    extra: tuple[tuple[str, str], ...] = ()

    def files(self) -> tuple[tuple[str, str], ...]:
        named = (("ppt_theme.py", self.theme), ("ppt_icons.py", self.icons))
        return tuple((name, body) for name, body in (*named, *self.extra) if body)


def script_path(project: Project) -> Path:
    return project.build_dir / "build.py"


# How the author's program is read, by every reader of it. utf-8-sig drops the
# byte-order mark an editor may have put in front of the file, once, at the door:
# past it the mark is a SyntaxError to compile(), invisible to `str.lstrip()` and
# to `\s`, and so hides the first banner or the comment above it from the readers
# that split the file into pages.
SCRIPT_ENCODING = "utf-8-sig"


def read_script(project: Project) -> str:
    """The author's program as text, read the one way every reader reads it."""
    return script_path(project).read_text(encoding=SCRIPT_ENCODING)


def deck_path(project: Project) -> Path:
    return project.build_dir / "deck.pptx"


def slide_lines_path(project: Project) -> Path:
    """Where the runner records which script line created each slide."""
    return project.build_dir / ".slide_lines.json"


def page_failures_path(project: Project) -> Path:
    """Where a build records the pages whose block raised and were stood in for."""
    return project.state_dir / "page_failures.json"


# The name the author imports the template operations under, so the reference a
# page carries -- "clone it with ppt_template.clone_page" -- is a line that runs.
TEMPLATE_HELPER = "ppt_template.py"


def with_template_helpers(helpers: HelperSources | None, template) -> HelperSources:
    """The same helpers, adjusted for a deck built inside a template.

    Two changes, and the second is the one that carries a guarantee.

    The clone-and-replace operations are added, as `ppt_template.py`. Only when a
    deck has a template: a build directory holding an importable `ppt_template`
    for a deck with none is an invitation to import it, and the failure that
    follows is about a file the author did not write.

    And the ten reviewed themes become the template's one. The author is told to
    take its palette from `ppt_theme`, and with a template bound that instruction
    is wrong; the correction was a sentence in a tool description, so an author
    that picked `ink-graphite` inside a green corporate template produced a deck
    that failed nothing. Now there is one theme in there and it is the
    template's, so the instruction and the guarantee agree.

    That one theme is the template's palette as the author read it off the renders,
    where it read one, and derived for whatever it did not say. The colours a file
    declares and the colours its pages paint are not the same colours -- see
    `services/template/palette.py` for the count -- so a reading taken off the pages
    wins over one computed from the theme part.
    """
    from raven_ppt.services.assets import script_helpers
    from raven_ppt.services.template import helper_source, theme_name, theme_of

    base = helpers or HelperSources()
    extra = {name: body for name, body in base.extra if name != script_helpers.THEME_DATA_FILENAME}
    extra[script_helpers.THEME_DATA_FILENAME] = json.dumps(
        {theme_name(template.inventory): theme_of(template.inventory, template.palette)},
        ensure_ascii=False,
        indent=1,
    )
    extra[TEMPLATE_HELPER] = helper_source()
    return replace(base, extra=tuple(sorted(extra.items())))


def asset_helpers() -> HelperSources:
    """The reviewed themes and the icon set, as modules the author can import.

    Assembled here rather than in the asset service because the service does not
    touch the filesystem, and the *names* come from the service rather than from
    here because the author imports them by name -- `from ppt_theme import THEMES`
    is not something a backend gets to choose.
    """
    from raven_ppt.services.assets import script_helpers

    files = dict(script_helpers.script_helper_files())
    theme = files.pop(script_helpers.THEME_MODULE_FILENAME, "")
    icons = files.pop(script_helpers.ICON_MODULE_FILENAME, "")
    return HelperSources(theme=theme, icons=icons, extra=tuple(sorted(files.items())))


def provision(project: Project, helpers: HelperSources | None = None) -> Path:
    """Make the build directory ready to run in, and return it.

    Rewritten every time rather than written once: the author can edit anything
    in this directory, and a helper it edited by accident would fail in a way that
    reads as a bug in the deck rather than as a modified helper.
    """
    from raven_ppt.services.assets import script_helpers

    project.build_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (helpers or HelperSources()).files():
        (project.build_dir / name).write_text(body, encoding="utf-8")
    # The skill's reference documents, under the relative path SKILL.md links to
    # them by. They reach the author no other way: the skill pool reads SKILL.md
    # and nothing beside it, and the workspace fence then refuses the path those
    # links resolve to. A name here carries a directory, so it gets one.
    for name, body in script_helpers.reference_files().items():
        target = project.build_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return project.build_dir
