"""The sections a ``mode: stint`` playbook carries beyond the common ones.

One round is one pass of every role over the project; the playbook says who the
roles are, what carries between rounds, what counts as passing, and when to
stop. It does not say what happens in a round -- that is the roles' own
business, and a file that tried to say it would be a program.

``roles[]`` is a ``delegate[]`` row with the round's fields on it, deliberately:
a worker is a worker whether one turn dispatches it or thirty rounds do, and
one description of a worker is what keeps the two from drifting. The extra
fields are on the subclass rather than on :class:`DelegateEntry` itself,
because a delegate table is *generated* every turn when that harness is on, and
a field on the parent is a field a model could emit -- which is how an
operator's own settings get overruled by a file the operator never wrote.

What is *not* here, and where it went instead:

* **model** -- on the roster row ``name`` points at. A playbook is a file that
  travels, and letting a travelling file pick the model overrules the person
  running it. Three roles on three models is three roster rows.
* **workspace** -- not per role. A whole run works one tree, and the tree is
  the stint's own worktree so the session that started it keeps its checkout.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from raven.playbook.agent_spec import DelegateEntry
from raven.playbook.base import CamelBase
from raven.stint.journal import JOURNAL_ROUNDS_IN_PROMPT, MAX_JOURNAL_CHARS
from raven.stint.verify import DEFAULT_TIMEOUT_SEC

__all__ = [
    "DEFAULT_MAX_HANDBACKS",
    "DEFAULT_MAX_ROUNDS",
    "MAX_ROUNDS",
    "EnforceSpec",
    "MemoryEntry",
    "RoleEntry",
    "StopSpec",
    "VerifyEntry",
]

DEFAULT_MAX_ROUNDS = 10
"""Rounds a stint runs when it says nothing. Enough to be worth starting and
small enough that a misjudged one is cheap; a stint that wants thirty says so,
and says it at the one moment somebody approves the whole run."""

MAX_ROUNDS = 99
"""The ceiling a stint may not raise, matching what the H* launcher enforces."""

DEFAULT_MAX_HANDBACKS = 2
"""Times a role is handed its own failing checks back before the round moves on.

Matches the graph runner's own continuation budget. Past it the round does not
stall and the role is not failed -- the failure is written down where the next
round reads it, because a round that ends with a known defect recorded is worth
more than a round that ends with nothing."""

_NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"


class EnforceSpec(CamelBase):
    """Whether a boundary is checked by the program or only stated in the prompt.

    Asymmetric on purpose. Writing is ``hard`` because a stray write is undoable
    and the undo is what makes the boundary real. Reading is ``soft`` because
    denying a read mostly denies a role the context it needs to do the work,
    and reading has no effect to undo.
    """

    read: str = Field(default="soft", pattern=r"^(soft|hard)$")
    write: str = Field(default="hard", pattern=r"^(soft|hard)$")


class RoleEntry(DelegateEntry):
    """One role in every round: who plays it, what it owns, what it must pass."""

    depends_on: list[str] = Field(default_factory=list)
    """Labels of roles that go first. The round runs them in that order."""

    node_summary: str = ""
    """A short title for this role's step, for somebody watching the round."""

    prompt_template: str = ""
    """What the role is told. Long standing orders belong in a file this
    references (``{{ref:...}}``), not inline: a graph carries references, and a
    round that pasted every rule into every prompt hit the reply ceiling."""

    owns: list[str] = Field(default_factory=list)
    """Path globs only this role may write. ``{NN}`` expands to the round."""

    appends: list[str] = Field(default_factory=list)
    """Path globs this role may add to but not cut from."""

    artifacts: list[str] = Field(default_factory=list)
    """Paths with no author -- a build writes them. Declared per role and
    exempt for every role, because whoever ran the build is not their author."""

    reads: list[str] = Field(default_factory=list)
    """What this role is pointed at, for the prompt. Advisory unless
    ``enforce.read`` is ``hard``."""

    enforce: EnforceSpec = Field(default_factory=EnforceSpec)
    mcps: list[str] | None = None
    """MCP servers for this role's step. Three-valued, as on a graph node:
    omitted keeps the roster row's own, a list replaces it, empty attaches none."""

    skills: list[str] | None = None
    """Skills for this role's step, three-valued on the same terms."""

    journal_section: str = ""
    """The journal subsection this role writes, e.g. ``Dev``. A role with none
    is not asked to leave anything for the rounds after it."""

    verify_after: list[str] = Field(default_factory=list)
    """Names from ``verify[]`` that run when this role stops."""

    max_handbacks: int = Field(default=DEFAULT_MAX_HANDBACKS, ge=0, le=5)


class MemoryEntry(CamelBase):
    """One file that carries between rounds."""

    path: str = Field(min_length=1)
    append: bool = False
    """Only additions are this round's to make; a removal is put back."""

    recent_rounds: int = Field(default=JOURNAL_ROUNDS_IN_PROMPT, ge=1)
    """How many of the file's round sections reach the next prompt. The rest
    are in the commit log, which is where forty rounds of history belong."""

    max_chars: int = Field(default=MAX_JOURNAL_CHARS, ge=1)
    """The second gate, for a file whose sections are individually huge."""


class VerifyEntry(CamelBase):
    """A real command whose verdict no model wrote."""

    name: str = Field(pattern=_NAME_RE)
    run: str = Field(min_length=1)
    timeout_sec: float = Field(default=DEFAULT_TIMEOUT_SEC, gt=0)

    needs_display: bool = False
    """This check renders, so it needs a screen to render onto.

    Declared because the alternative is failing it on every machine without one.
    A check that needs a display and finds none is recorded as *skipped* -- which
    is the truth, and reads differently in the round's table from a check that
    ran and failed."""


class StopSpec(CamelBase):
    """When the stint stops opening rounds, and what it says on the way.

    ``report`` is here rather than in a section of its own because it is the
    same question seen from the other end: a stint that says nothing until it
    stops is a stint a person cannot tell from a hung one, and for thirty rounds
    that is hours of silence. It costs a main-agent turn a round -- an announced
    message is the only thing that starts one -- so a long unattended stint can
    ask for ``end`` and get the old behaviour back.
    """

    report: Literal["round", "end"] = "round"
    """``round``: a line when each round finishes. ``end``: only the summary."""

    max_rounds: int = Field(default=DEFAULT_MAX_ROUNDS, ge=1, le=MAX_ROUNDS)
    until: str = ""
    """A marker a role writes when there is nothing left worth another round.
    Empty means only ``maxRounds`` stops it."""

    @model_validator(mode="after")
    def _until_is_a_marker_not_a_condition(self) -> "StopSpec":
        # A condition would need an evaluator, and an evaluator is a program the
        # approver cannot read at the one moment they are asked to approve it.
        if any(ch in self.until for ch in "{}()<>=!&|"):
            raise ValueError(f"stop.until is a marker a role writes, not an expression; {self.until!r} reads as one")
        return self
