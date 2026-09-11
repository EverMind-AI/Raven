"""The agent-playbook contract: one file, one turn's worth of workers.

A third ``mode`` beside the graph-shaped two. ``dag`` and ``prompt`` answer
"how is this piece of work done"; ``agent`` answers "who is doing it, and with
what brief". There is deliberately no graph: which worker runs when stays with
the main agent at run time, which is the one party that has read the files and
run the searches by the time it matters.

Shape, and why it is this small:

- ``delegate`` is the only field with behaviour. Each entry is a label, the
  roster agent behind it, and a sub-playbook that becomes that label's charter.
- ``version`` / ``mode`` / ``name`` / ``description`` carry nothing this
  version reads. They are the file's identity once a playbook is stored, and a
  schema that gains them later has to answer for every row written without
  them, so they are written from the start.
- No ``mode`` / ``model`` on a delegate row. Both would let a generated file
  overrule the operator: ``SubagentManager.resolve_mode`` states that a
  sub-agent's effort is the operator's setting and that the model composing a
  dispatch is the one party that cannot know what was chosen. A model-authored
  playbook is exactly that party.

The sub-playbook mirrors the four module roles, but this version renders it to
text rather than binding it: the charter reaches the worker as a preamble on
its task. So ``tools`` here reads as "these are the tools for this job", not as
a permission -- narrowing what a model is *shown* has never been a permission
in Raven, and the enforcement point is ``ToolRegistry.execute``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from raven.playbook.types import CamelBase

AGENT_SPEC_VERSION = 1

NAME_RE = r"^[a-z0-9][a-z0-9-]*$"
LABEL_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
"""A label may carry a roster name unchanged (``Raven-PPT``) or be a local
alias (``research-a``), so it admits the character set of both."""


class CheckRule(CamelBase):
    """One declarative judgement about a call the worker is about to make.

    Declarative on purpose, and the same set of operators the worker's own
    evaluator reads -- the two are one vocabulary, so a rule written here
    cannot mean something the other side has no way to apply.
    """

    tool: str
    when: dict[str, str] = Field(default_factory=dict)
    """Narrows the rule to calls whose arguments match: a rule about spawning
    the deck agent should not fire on every spawn."""

    path_prefix: str = ""
    """The path argument must start with this."""

    forbid: str = ""
    """A pattern no argument may contain."""

    requires_prior: str = ""
    """A tool that must already have run, successfully, this turn."""

    match_param: str = ""
    """With ``requiresPrior``: the argument the earlier call must have matched
    on, so "read the file you are about to write" is expressible rather than
    only "read something"."""

    message: str = ""
    """What the worker is told when the rule refuses. The refusal reaches its
    model as the call's result, so this is the sentence that has to make the
    next attempt right."""


class Checks(CamelBase):
    """The judgement seat, and the data the shipped judge reads.

    ``impl`` is a registry id, never a path: naming a file would put the choice
    of what to load on the writer of a generated document, and a registry id
    can only select something this install already ships.
    """

    impl: str = "default"
    rules: list[CheckRule] = Field(default_factory=list)

    code: str = ""
    """A judgement the declarative rules cannot say, as Python.

    Some questions about a call are not a prefix or a pattern -- parse this
    argument, compare it with an earlier one, count what has already run -- and
    a rule language grown until it could say them would be a language. This is
    the escape hatch, and it is narrow on purpose:

    - it runs in the **worker's** process, on the worker's own calls, so it
      holds no authority the dispatched agent did not already have, and a judge
      that misbehaves costs one dispatch rather than the conversation;
    - it passes an allow-listed gate on the parse tree before anything is
      compiled (:mod:`raven.agent.subagent.charter_code`) -- no imports, no
      loops, no dunder access, no calls outside a named handful;
    - a source the gate refuses is dropped with a log line rather than turned
      into a refusal, because a judge that did not load has said nothing about
      this call, and refusing everything would be a far larger claim than its
      author made.

    It defines ``judge(name, params, prior)`` and answers with the refusal
    sentences for this call."""


class SubMemory(CamelBase):
    system_prompt: str = ""


class SubCapability(CamelBase):
    tools: list[str] | None = None
    """Three-valued: unset is "whatever this worker already offers", ``[]`` is
    an explicit none, a list is those. Folding ``[]`` into unset would turn
    "this worker needs no tools" into "it gets all of them"."""


class SubAction(CamelBase):
    checks: Checks | None = None


class SubPlaybook(CamelBase):
    """One worker's charter. The four module roles, one level deep."""

    role: Literal["subagent"] = "subagent"
    memory: SubMemory = Field(default_factory=SubMemory)
    capability: SubCapability = Field(default_factory=SubCapability)
    action: SubAction = Field(default_factory=SubAction)
    stop_when: str = ""

    timeout_seconds: int | None = None
    """A deadline for this worker's dispatch, if this job has one.

    Tightening only. Every shipped worker is configured without a limit, and
    deliberately -- a long job is a long job, and a fixed ceiling would cut the
    ones the roster exists to run. A playbook, unlike a config, knows what
    *this* job is, so it may name a deadline the config could not have known to
    set; it may not lengthen one an operator set."""
    """One line naming what, once obtained, means this worker is done."""

    @model_validator(mode="after")
    def _no_third_level(self) -> "SubPlaybook":
        """A worker may not bring workers of its own.

        Depth is a property of what a charter may say, not a counter the
        runtime carries: a worker that cannot name a roster has no route to a
        third level, and the rule is checkable on the file alone.
        """
        extra = getattr(self, "delegate", None)
        if extra:
            raise ValueError("a worker's playbook must not carry 'delegate': nesting is two levels")
        return self


class DelegateEntry(CamelBase):
    """One label this turn may dispatch to."""

    as_: str = Field(default="", alias="as")
    """The label the dispatching model names. Empty means "same as ``name``",
    which is the common case of one charter per agent."""

    name: str
    """The roster agent behind the label. Validated against the live roster by
    the generator's caller, not here: the schema cannot know the install."""

    playbook: SubPlaybook | None = None

    @property
    def label(self) -> str:
        return self.as_ or self.name


class AgentPlaybookSpec(CamelBase):
    """One whole agent playbook."""

    version: int = AGENT_SPEC_VERSION
    mode: Literal["agent"] = "agent"
    name: str = Field(default="turn-plan", pattern=NAME_RE)
    description: str = Field(default="", max_length=200)
    delegate: list[DelegateEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _labels_are_unique(self) -> "AgentPlaybookSpec":
        seen: set[str] = set()
        for entry in self.delegate:
            if entry.label in seen:
                raise ValueError(f"duplicate worker label {entry.label!r}: a label names exactly one charter")
            seen.add(entry.label)
        return self


__all__ = [
    "AGENT_SPEC_VERSION",
    "AgentPlaybookSpec",
    "CheckRule",
    "Checks",
    "DelegateEntry",
    "SubAction",
    "SubCapability",
    "SubMemory",
    "SubPlaybook",
]
