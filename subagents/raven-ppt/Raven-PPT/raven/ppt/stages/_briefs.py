"""What the intake call is told.

There were three briefs here. Two of them -- a page brief and a deck brief -- were
what the design pass was given, and they went with it; every rule they carried that
the author needs is in the `ppt-script-authoring` skill, which is where an author
reads. What is left is the one brief for a call that has no skill to read: the
model that reads the deck task before any project state exists.
"""

from __future__ import annotations

INTAKE_BRIEF = """Read a deck task and say what it takes to do it. You get the
task, what this deck already has, and a workspace listing; the bundled templates
when none is bound yet, and the first of the ingested materials when there are any.

Separate stated facts from guesses. The language, audience and page budget bind
only when stated; missing values become user questions elsewhere. `errands` are
specific material to retrieve, not work to perform. `task_is_material` is true
when the request itself contains evidence or substantive notes.

`forbidden` is what the request ruled out and nothing else: no icons, no
comparison tables, no competitor names, no dark pages. Record only what the task
actually forbids -- it is quoted back on every build as a rule, so a dislike you
inferred becomes a constraint nobody set.

Reply as JSON and nothing else:

{"topic": "<one line>",
 "stated": {"language": <string or null>, "audience": <string or null>,
            "pages_low": <int or null>, "pages_high": <int or null>,
            "forbidden": ["what the request rules out, one thing each"]},
 "materials_dir": "<relative path or empty>",
 "task_is_material": <bool>,
 "template": "<user .pptx path, bundled template filename, or empty>",
 "questions": [{"question": "...", "why": "...", "options": ["...", "..."]}],
 "errands": [{"what": "...", "why": "...", "how": "..."}],
 "notes": ["a binding instruction not carried elsewhere"]}

Keep questions few. Do not ask duplicate language, audience or page-count
questions. Notes carry user instructions, not observations."""


def intake_brief() -> str:
    return INTAKE_BRIEF
