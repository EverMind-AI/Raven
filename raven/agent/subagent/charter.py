"""A worker's charter: what the turn it is about to run may see and do.

The other half of ``delegate``. The host writes a charter per worker and ships
it with the dispatch; this is what the worker's own process reads it back into,
and the scope the turn then runs under.

Two things make this its own module rather than part of ``delegate``:

- **Direction.** ``delegate`` is what a dispatching turn offers; a charter is
  what a dispatched turn is held to. One is read by ``spawn``, the other by the
  assembler and the tool registry, and a module serving both would be imported
  by everything.
- **Trust.** A charter arrives from another process. It is narrowed against what
  this install already allows rather than applied as given, and the narrowing
  belongs next to the type rather than at each reader.

Narrowing, never widening. The host may say "only these tools for this job";
it may not say "and also this one you have switched off". Every field is
therefore an intersection with what this process would have done anyway, which
is the same rule ``_narrow_build`` states for a builtin row's allow-lists and
the same one the tool registry's off switch follows.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from loguru import logger

from raven.contracts.participant import AgentParticipant, StepView

MAX_PROMPT_CHARS = 4000
"""A charter's prompt is a brief, not a second system prompt. Bounded so a
malformed or hostile payload cannot push the turn's own identity out of the
window."""

MAX_TOOLS = 64
MAX_CODE_CHARS = 8000


def _parameter_enabled(module: str, name: str) -> bool:
    from raven.agent.harness_capabilities import parameter_enabled

    return parameter_enabled(module, name)


def _function_enabled(module: str, kind: str, name: str) -> bool:
    from raven.agent.harness_capabilities import function_enabled

    return function_enabled(module, kind, name)


@dataclass(frozen=True)
class Charter:
    """One dispatch's brief, already narrowed."""

    prompt: str = ""
    """The dispatch brief, separate from generated instructions so either can
    be disabled without erasing or impersonating the other."""

    instruction_addendum: str = ""
    """Task-specific instructions appended after the worker's existing identity,
    never in place of the runtime facts and safety rules that identity carries."""

    tools: tuple[str, ...] | None = None
    """``None`` is "whatever this agent already offers". A tuple narrows to
    those names. An empty tuple is a real answer -- "this job needs no tools" --
    and is preserved rather than folded into ``None``, which would turn it into
    its own opposite."""

    functions: tuple[tuple[str, str], ...] = ()
    """Enabled generated participant sources, stored as immutable name-source
    pairs. ``judge`` is one of them: it was once a ``code`` field beside a
    declarative rule table, and the table could say nothing ``judge`` cannot.
    A table migrated from that era rides in the ``judge`` source as
    ``rules_judge``; see ``charter_code.COMPANIONS``. Compiled behind the gate in ``charter_code``, which admits an allow-listed
    subset and nothing else; a source that does not pass never runs, and one
    that raises at run time has said nothing rather than refused everything."""

    timeout_s: int | None = None
    """A deadline for this dispatch, if the brief carried one. Tightening only:
    a worker configured with a limit keeps the smaller of the two, and one
    configured without a limit is not the same as one told to take forever --
    see ``narrowed_timeout``."""

    stop_when: str = ""

    @property
    def task_brief(self) -> str:
        """The brief and enabled instruction delta rendered for Memory."""
        return "\n\n".join(part for part in (self.prompt, self.instruction_addendum) if part)

    def __bool__(self) -> bool:
        return bool(
            self.prompt
            or self.instruction_addendum
            or self.tools is not None
            or self.functions
            or self.timeout_s
            or self.stop_when
        )


def parse(payload: Any) -> Charter | None:
    """Read a charter off the wire, dropping anything unrecognised.

    Unrecognised keys are dropped rather than refused, on the same reasoning the
    usage-owner payload beside it uses: the sender may be a newer host, and a
    field this build does not know is not a reason to fail the dispatch. What is
    *known* is bounded -- an over-long prompt or an over-long list is truncated
    and logged, so a malformed payload costs a log line rather than the turn.
    """
    if not isinstance(payload, dict):
        return None
    legacy_prompt = payload.get("prompt")
    raw_brief = payload.get("brief") if "brief" in payload else legacy_prompt
    prompt = str(raw_brief or "")[:MAX_PROMPT_CHARS]
    instruction_addendum = (
        str(payload.get("instructionAddendum") or "")[:MAX_PROMPT_CHARS]
        if _parameter_enabled("memory", "systemPrompt")
        else ""
    )
    stop_when = (
        str(payload.get("stopWhen") or payload.get("stop_when") or "")[:MAX_PROMPT_CHARS]
        if _parameter_enabled("memory", "stopWhen")
        else ""
    )
    tools: tuple[str, ...] | None = None
    raw_tools = payload.get("tools") if _parameter_enabled("capability", "tools") else None
    if isinstance(raw_tools, list):
        tools = tuple(str(name) for name in raw_tools[:MAX_TOOLS] if isinstance(name, str) and name)
    function_modules = {
        "intake": "memory",
        "system_addendum": "memory",
        "archive": "memory",
        "select_tools": "capability",
        "advise": "planning",
        "judge": "action",
        "review": "action",
        "salvage": "action",
    }
    raw_functions = payload.get("functions")
    functions: list[tuple[str, str]] = []
    if isinstance(raw_functions, dict):
        for name, source in raw_functions.items():
            module = function_modules.get(name)
            if not (module and isinstance(source, str) and source.strip()):
                continue
            if not _function_enabled(module, "participant", name):
                continue
            # Dropped whole rather than cut to fit. Half of a function is not a
            # shorter function: the cut lands mid-token, the gate reports a
            # syntax error, and the verb answers silence -- which for ``judge``
            # turns every refusal it carried into permission. A source over the
            # ceiling is a brief this process will not run, and saying so is
            # the only honest answer.
            if len(source) > MAX_CODE_CHARS:
                logger.warning(
                    "charter: its {} function is {} characters, over the {} ceiling; "
                    "dropping it rather than cutting executable source",
                    name,
                    len(source),
                    MAX_CODE_CHARS,
                )
                continue
            functions.append((name, source))
    raw_timeout = payload.get("timeoutSeconds") or payload.get("timeout_s")
    timeout_s: int | None = None
    if isinstance(raw_timeout, (int, float)) and raw_timeout > 0:
        timeout_s = int(raw_timeout)
    charter = Charter(
        prompt=prompt,
        instruction_addendum=instruction_addendum,
        tools=tools,
        functions=tuple(functions),
        timeout_s=timeout_s,
        stop_when=stop_when,
    )
    return charter or None


_CHARTER: ContextVar[Charter | None] = ContextVar("subagent_charter", default=None)
_CALLS: ContextVar[list[tuple[str, Mapping[str, Any]]] | None] = ContextVar("subagent_charter_calls", default=None)
"""What this turn has already run, for the rules that are about the last call
rather than this one.

Kept beside the charter rather than on ``PermissionTurn``, which is where it
first sat: ``current_turn()`` answers an *unbound* task with a fresh object
every time, so a log appended there is discarded on the next read. That is
correct for a refusal memory -- an unattended task has no approvals to remember
-- and wrong for this, which has to survive the whole turn or say nothing at
all."""


@contextmanager
def charter_scope(charter: Charter | None) -> Iterator[None]:
    """Hold ``charter`` for the current task, and only there.

    ``None`` holds nothing, which is what every reader answers to as "no charter
    this turn" -- so a dispatch that carried none takes the path it took before
    charters existed.
    """
    token = _CHARTER.set(charter or None)
    calls_token = _CALLS.set([] if charter else None)
    try:
        yield
    finally:
        _CALLS.reset(calls_token)
        _CHARTER.reset(token)


def current_charter() -> Charter | None:
    """This turn's charter, or ``None`` outside any scope."""
    return _CHARTER.get()


def bind_charter_for_turn(charter: Charter | None) -> None:
    """Replace the charter inside the caller's existing turn scope.

    The counterpart of ``bind_delegate_for_turn``: ``load_playbook`` runs after
    the outer scope has opened, so a stored Persona's coordinator seat reaches
    the rest of that turn the same way its workers do, and the outer scope's
    token still restores the previous value.

    The call log is opened with it. ``charter_scope`` opens one only when it was
    given a charter, so a turn that started without one has ``None`` there, and
    a coordinator bound here would otherwise have its ``judge`` asked with an
    empty history on every call.
    """
    _CHARTER.set(charter or None)
    if charter and _CALLS.get() is None:
        _CALLS.set([])


def record_call(name: str, params: Mapping[str, Any]) -> None:
    """Remember a call that ran and did not fail.

    A no-op outside a charter scope: with no brief to be held to, nothing is
    asking what ran before.
    """
    log = _CALLS.get()
    if log is not None:
        log.append((name, dict(params)))


def prior_calls() -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """What this turn has run so far, oldest first."""
    return tuple(_CALLS.get() or ())


def narrowed_tools(offered: Sequence[str]) -> frozenset[str]:
    """The names this turn's charter withholds from ``offered``.

    An intersection expressed as a withholding, because that is the shape the
    registry's off switch already takes and the one place a tool name can
    actually be refused. Names the charter asks for that this install does not
    offer are ignored: the host may be describing a roster this process does not
    have, and inventing them here would be the widening the charter may not do.
    """
    charter = current_charter()
    if charter is None or charter.tools is None:
        return frozenset()
    keep = set(charter.tools)
    withheld = frozenset(name for name in offered if name not in keep)
    if withheld:
        logger.debug("charter: withholding {} tool(s) for this turn", len(withheld))
    return withheld


def _pure_data(value: Any) -> Any:
    """Detach the JSON-like part of a StepView from host runtime objects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _pure_data(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_pure_data(item) for item in value]
    return str(value)


def _step_payload(step: Any) -> dict[str, Any]:
    response = getattr(step, "response", None)
    response_data = None
    if response is not None:
        response_data = {
            name: _pure_data(getattr(response, name, None))
            for name in ("content", "reasoning", "finish_reason", "tool_calls", "usage")
            if getattr(response, name, None) is not None
        }
    return {
        "session_key": getattr(step, "session_key", ""),
        "iteration": getattr(step, "iteration", 0),
        "response": response_data,
        "transcript": _pure_data(getattr(step, "transcript", ())),
        "history": _pure_data(getattr(step, "history", ())),
        "turn_base": getattr(step, "turn_base", 0),
        "question": getattr(step, "question", ""),
        "rollbacks": getattr(step, "rollbacks", 0),
        "mode": getattr(step, "mode", None),
        "mode_overlay": _pure_data(getattr(step, "mode_overlay", None)),
        "phase": getattr(step, "phase", ""),
        "tools": _pure_data(getattr(step, "tools", ())),
        "window": getattr(step, "window", None),
        "max_iterations": getattr(step, "max_iterations", None),
        "tools_ran": bool(getattr(step, "tools_ran", False)),
    }


class CharterParticipant(AgentParticipant):
    """This dispatch's own judgements, as a participant.

    The thin shell the design calls for: a Charter is data the model sent, and
    this is what lets it answer the same verb a plugin answers rather than
    being read by the role itself. Stateless, so one instance serves every call
    of a turn; what it judges comes from the scope, not from this object.

    Generated pure-data functions implement only their declared verbs; every
    other verb keeps the base participant's no-op answer.
    """

    __slots__ = ()

    async def intake(self, text: str, step: StepView) -> Any:
        return _generated_answer("intake", text, _step_payload(step))

    async def advise(self, step: StepView) -> Any:
        return _generated_answer("advise", _step_payload(step))

    async def select_tools(self, offered: list[dict[str, Any]], step: StepView) -> Any:
        return _generated_answer("select_tools", _pure_data(offered), _step_payload(step))

    async def system_addendum(self, step: StepView) -> Any:
        return _generated_answer("system_addendum", _step_payload(step))

    async def review(self, step: StepView) -> Any:
        return _generated_answer("review", _step_payload(step))

    async def salvage(self, step: StepView) -> Any:
        return _generated_answer("salvage", _step_payload(step))

    async def archive(self, step: StepView, reply: str | None) -> Any:
        return _generated_answer("archive", _step_payload(step), reply)

    def judge(
        self,
        name: str,
        params: Mapping[str, Any],
        prior: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> list[str]:
        return judge(name, params, prior)


def charter_participants() -> tuple[CharterParticipant, ...]:
    """The participants this dispatch brings, or none when it brought no
    judgements. Asked after the plugins' own, so a product's rules speak first."""
    charter = current_charter()
    if charter is None or not charter.functions:
        return ()
    return (CharterParticipant(),)


def judge(
    name: str,
    params: Mapping[str, Any],
    prior: Sequence[tuple[str, Mapping[str, Any]]],
) -> list[str]:
    """Why this turn's charter refuses this call, or an empty list.

    A refusal is a sentence, not an exception: it reaches the model as the
    call's result, so the next attempt can be right. One source rather than
    two -- a declarative rule table used to run ahead of this, and everything
    it could say ``judge`` can say. A table migrated from that era is carried
    in the same source as ``rules_judge`` and still asked first, so what a
    seat refuses is read from one place.

    Cheap by contract: this runs before every dispatch and ahead of the
    permission gate. The gate in ``charter_code`` is what keeps it so -- no
    imports, no loops, and a call list that cannot reach the network or build
    an unbounded sequence.
    """
    # Asked apart and joined here, the order the pre-merge runtime used: a
    # migrated table and the hand-written judge beside it each run on their
    # own, so a raise in one leaves the other's refusals standing.
    params, prior = _pure_data(params), _pure_data(prior)
    return _refusals(_generated_answer("rules_judge", name, params, prior)) + _refusals(
        _generated_answer("judge", name, params, prior)
    )


def _refusals(answer: Any) -> list[str]:
    # One sentence is as good as a list: a judge with a single thing to say
    # writes the string, and reading that as silence would turn its refusal
    # into permission. Carried over from the judge this verb replaced.
    if isinstance(answer, str):
        return [answer] if answer.strip() else []
    if not isinstance(answer, (list, tuple)):
        return []
    return [str(line) for line in answer if str(line).strip()]


_COMPILED: dict[Any, Any] = {}
MAX_COMPILED = 64
"""How many distinct generated functions one process keeps compiled.

Generated participants are keyed by verb and source; judges by source. A worker process outlives any one dispatch, so without a
ceiling this grows with every brief the process is ever sent. Cleared rather
than evicted one at a time: the cost of a miss is one parse-tree walk, and a
policy that has to decide *which* entry to drop is more machinery than the
thing it manages."""


def _generated_answer(name: str, *args: Any) -> Any:
    from raven.agent.subagent.charter_code import COMPANIONS

    charter = current_charter()
    verb = COMPANIONS.get(name, name)
    source = dict(charter.functions).get(verb) if charter is not None else None
    if not source:
        return None
    key = (name, source)
    compiled = _COMPILED.get(key)
    if compiled is None:
        try:
            from raven.agent.subagent.charter_code import compile_function

            compiled = compile_function(source, name)
        except Exception as exc:  # noqa: BLE001 - refused generated code is silence
            # A companion shares its verb's source: a refused source was
            # already reported under the verb, and most define no companion.
            if name == verb:
                logger.warning("charter: its {} function was refused ({}); ignoring it", name, exc)
            compiled = False
        if len(_COMPILED) >= MAX_COMPILED:
            _COMPILED.clear()
        _COMPILED[key] = compiled
    if compiled is False:
        return None
    from raven.agent.subagent.charter_code import run_function

    return run_function(compiled, *args)


def narrowed_timeout(configured: int | None) -> int | None:
    """The deadline this dispatch runs under: the tighter of the two.

    ``None`` on either side means "no limit from here", so a configured ``None``
    takes the charter's number and a charter that named none keeps whatever was
    configured. Tightening only, like every other field: a brief may say this
    job should not take an hour, and may not say a job configured to finish in a
    minute should be allowed one.
    """
    charter = current_charter()
    asked = charter.timeout_s if charter is not None else None
    if asked is None:
        return configured
    if configured is None:
        return asked
    return min(configured, asked)


__all__ = [
    "Charter",
    "bind_charter_for_turn",
    "charter_scope",
    "current_charter",
    "judge",
    "narrowed_timeout",
    "narrowed_tools",
    "parse",
    "prior_calls",
    "record_call",
]
