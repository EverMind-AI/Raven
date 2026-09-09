"""Whether an inline submission may be written over the author's program.

Two guards, learned in the same run and in that order.

First, `script="."` went straight through. It is not blank and it is not a
program either: it replaced a working forty-kilobyte deck program, every later
call failed on the wreckage, and the author stopped using the tool -- it ran its
own script with exec and copied the result to the delivery path, where no gate
ever saw it.

Then the fix for that was refused too eagerly. The same author, wanting the
build.py it had spent six calls writing, tried `" "`, then `"\\n"`, then
`"# use existing build.py"`. All three were refused. The file was protected,
which was the point, but there was nowhere left to go -- and it left the tool
again, the same way. Omitting the field is what the description asks for, and
this author fills in every field it is shown.

So: a submission with no program in it means "run what is there", and a
submission that cannot be a program is refused by name.
"""

from __future__ import annotations

import ast
from pathlib import Path

from raven_ppt.backends.script.workspace import SCRIPT_ENCODING

# A replacement this much smaller than what it replaces is usually a fragment
# sent by mistake rather than a rewrite.
_FRAGMENT_RATIO = 5


def carries_a_program(script: str) -> bool:
    """Whether a submission has any program in it at all.

    Unparsable text is not empty, only wrong, and goes on to be refused by name.
    Silently ignoring it would hide a real mistake.
    """
    if not script.strip():
        return False
    try:
        return bool(ast.parse(script).body)
    except SyntaxError:
        return True


def submission_refusal(script: str, source: Path) -> str | None:
    """Why this submission must not be written over `source`, or None."""
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        return (
            f"the submitted script is not valid python ({exc.msg} on line {exc.lineno}), so it was not "
            f"written to {source.name}. Fix it and resubmit, or omit script to run the file that is there"
        )
    if _delegates_elsewhere(tree):
        return (
            f"the submitted script hands the work to another file rather than drawing the deck, so it was "
            f"not written to {source.name}. Write the pages here: shared helpers first, then one block per "
            "page, each opening with a `# SLIDE <n>` banner --\n"
            "    # SLIDE 1\n"
            "    sl = new_slide()\n"
            "    title(sl, 'Unified video segmentation')\n"
            "    ...\n"
            "    # SLIDE 2\n"
            "The build reads those blocks to match a render back to the code that drew it"
        )
    existing = source.read_text(encoding=SCRIPT_ENCODING) if source.is_file() else ""
    if existing.strip() and len(script.strip()) * _FRAGMENT_RATIO < len(existing.strip()):
        return (
            f"the submission is {len(script.strip())} characters against the {len(existing.strip())} "
            f"already in {source.name}, so it was not written -- a replacement that small is usually a "
            "fragment sent by mistake. Use edit_file to change part of the program, or omit script to run "
            "what is there"
        )
    return None


def _delegates_elsewhere(tree: ast.Module) -> bool:
    """Whether a submission's whole job is to run some other file.

    A shim is worth refusing on its own terms: the build matches each page's
    render back to the code that drew it, and a program whose body is one
    `run_path` call has one block for the whole deck.
    """
    names = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    return bool(names & {"run_path", "run_module"}) or (
        "exec" in names
        and any(isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open" for node in ast.walk(tree))
    )
