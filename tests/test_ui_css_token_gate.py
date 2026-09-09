"""The CSS token gate rejects what it claims to reject.

``ui-web/scripts/check-css.mjs`` is the only thing keeping "which color is this
role" answerable in one place per theme: it fails CI when a color is written
as a literal instead of a token. A gate is only worth its line in the
pipeline if it actually catches the thing it names, and two earlier revisions
of this one passed rules it was written to reject -- one because the check
was positional (a colored rule above the reset was exempt, the identical rule
at the end of the file was not), one because the shadow exemption was shaped
by property rather than by what the literal is (``box-shadow: 0 0 0 2px
#b4402f`` is a role color frozen to one theme).

So each case is pinned here: the stylesheet is copied, one rule is appended,
and the gate is run over the copy. Both directions matter -- a gate that
rejects everything is as useless as one that rejects nothing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# The gate is a node script, and this suite is the only place that checks what
# it REJECTS: running it over the current stylesheet, which is all the GitHub
# workflow's page job does, proves the gate runs and that the file passes, and
# stays green against a gate that allowlists everything.
#
# So skipping is a real loss, not a formality, and it is here only because the
# pipeline that gates a merge request cannot run it: GitLab's `tests` job (the
# whole of `.gitlab-ci.yml`, which lives on this project's `ci` branch) uses
# `python:3.12-slim` and installs git and nothing else. That branch is not
# reachable from this one. The GitHub workflow's unit job now installs node, so
# this suite runs there; closing the GitLab gap is a change to the `ci` branch.
pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="the gate is a node script and the GitLab test image has no node -- see the note above",
)

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "ui-web" / "scripts" / "check-css.mjs"
_CSS = _ROOT / "ui-web" / "src" / "styles" / "page.css"

# (name, rule appended to the stylesheet, is it supposed to pass)
_CASES = [
    # Rejected: a role color pinned to one theme, however it is spelled.
    ("plain hex background", ".probe { background: #ffd0a0; }", False),
    ("bare rgba overlay", ".probe { background: rgba(0, 0, 0, .5); }", False),
    ("opaque hex in a shadow", ".probe { box-shadow: 0 0 0 2px #b4402f; }", False),
    (
        "opaque stops in a gradient",
        ".probe { background-image: linear-gradient(180deg, #d96a5b, #b4402f); }",
        False,
    ),
    # Accepted: the literal is a ramp over whatever is behind it, or is data.
    (
        "multi-line shadow with alpha",
        ".probe { box-shadow:\n  0 1px 2px rgba(0, 0, 0, .06),\n  0 10px 24px -12px rgba(0, 0, 0, .2); }",
        True,
    ),
    ("eight-digit hex shadow", ".probe { box-shadow: 0 18px 40px -24px #000c; }", True),
    (
        "gradient with alpha stops",
        ".probe { background-image: linear-gradient(180deg, rgba(0,0,0,.2), transparent); }",
        True,
    ),
    ("slash-alpha rgb in a shadow", ".probe { box-shadow: 0 2px 4px rgb(0 0 0 / 20%); }", True),
    ("mask addressed by luminance", ".probe { mask-image: linear-gradient(180deg, #000, transparent); }", True),
    # A data URI carries an unescapable `;`, and cutting the declaration there
    # judged its tail as a rule of its own -- on a line that is a token.
    (
        "data-uri token holding a color",
        ":root { --probe-ico: url('data:image/svg+xml;utf8,<svg fill=\"rgb(247,242,228)\"></svg>'); }",
        True,
    ),
    ("a rule written in tokens", ".probe { background: var(--surface); color: var(--text); }", True),
    # A verdict must not depend on the declaration BEFORE it. The gate once
    # shared one global regex between its "does this contain a literal" test
    # and its scan, and `.test()` on a global regex moves `lastIndex`, so a
    # declaration was skipped whenever the previous one left an offset past
    # its end. 73 of the file's 167 literal-bearing declarations went
    # unexamined, and every case above missed it by having only one.
    ("a token def, then a raw color", ".probe { --x: #ffffff; color: #f00; }", False),
    (
        "an allowlisted rule, then a raw color",
        ".probeB { mask-image: linear-gradient(#000, transparent); }\n.probeC { color: #f00; }",
        False,
    ),
    ("an exempt shadow, then a raw color", ".probe { box-shadow: 0 1px 2px #0001; color: #f00; }", False),
]


def _run_gate_over(tmp_path: Path, extra_rule: str) -> int:
    """Run the gate against a copy of the tree with ``extra_rule`` appended."""
    ui = tmp_path / "ui"
    (ui / "scripts").mkdir(parents=True)
    (ui / "src" / "styles").mkdir(parents=True)
    shutil.copy(_SCRIPT, ui / "scripts" / "check-css.mjs")
    (ui / "src" / "styles" / "page.css").write_text(
        _CSS.read_text(encoding="utf-8") + "\n" + extra_rule + "\n", encoding="utf-8"
    )
    return subprocess.run(["node", str(ui / "scripts" / "check-css.mjs")], capture_output=True).returncode


def test_the_stylesheet_as_it_stands_passes() -> None:
    """The baseline, so a failure below is about the appended rule."""
    assert subprocess.run(["node", str(_SCRIPT)], capture_output=True).returncode == 0


@pytest.mark.parametrize(("name", "rule", "allowed"), _CASES, ids=[c[0] for c in _CASES])
def test_the_gate_judges_a_rule_by_what_it_says(tmp_path: Path, name: str, rule: str, allowed: bool) -> None:
    code = _run_gate_over(tmp_path, rule)
    if allowed:
        assert code == 0, f"the gate rejected a legitimate rule: {name}"
    else:
        assert code != 0, f"the gate accepted a color literal it exists to reject: {name}"


@pytest.mark.parametrize(("name", "rule", "allowed"), _CASES, ids=[c[0] for c in _CASES])
def test_the_verdict_does_not_depend_on_where_the_rule_sits(
    tmp_path: Path, name: str, rule: str, allowed: bool
) -> None:
    """The same rule, prepended instead of appended, gets the same verdict.

    The gate once exempted everything above the first ordinary rule, which
    made it answer for position rather than for content.
    """
    ui = tmp_path / "ui"
    (ui / "scripts").mkdir(parents=True)
    (ui / "src" / "styles").mkdir(parents=True)
    shutil.copy(_SCRIPT, ui / "scripts" / "check-css.mjs")
    (ui / "src" / "styles" / "page.css").write_text(rule + "\n" + _CSS.read_text(encoding="utf-8"), encoding="utf-8")
    code = subprocess.run(["node", str(ui / "scripts" / "check-css.mjs")], capture_output=True).returncode
    assert (code == 0) is allowed, f"verdict changed with position: {name}"


@pytest.mark.parametrize(("name", "rule", "allowed"), _CASES, ids=[c[0] for c in _CASES])
def test_the_verdict_does_not_depend_on_the_declaration_before_it(
    tmp_path: Path, name: str, rule: str, allowed: bool
) -> None:
    """The same rule keeps its verdict with an exempt declaration ahead of it.

    The position pass above varies where the rule sits in the file. This one
    varies what precedes it, which is the axis a reader carrying state across
    declarations fails on -- and the one the suite was blind to while the gate
    skipped nearly half the file.
    """
    ahead = ":root { --probe-ahead: #ffffff; }\n.probe-ahead { mask-image: linear-gradient(#000, transparent); }\n"
    code = _run_gate_over(tmp_path, ahead + rule)
    assert (code == 0) is allowed, f"verdict changed with an exempt declaration ahead of it: {name}"
