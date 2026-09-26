"""Coordinate source queries, planning, implementation and bounded validation repair."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from math import isfinite

from raven.agent.tools.registry import call_failed
from raven.contracts.llm_provider import LLMProvider
from raven.contracts.tool import RAW_ARGUMENTS_KEY, SKIPPED_AFTER_BLOCKED_CALL, Continuation
from raven.permissions.turn import set_current_tool_call_id
from raven.providers.prompt_cache import accepts_cache_control, cache_control, claim_marks
from raven.security.trust import wrap_untrusted

from ..harness import Candidate, Plan, Validation
from ..raven_adapter.observe import plain
from .context import query, render
from .context.collect import Context
from .stages import design, implement, repair, select, understand
from .state import GenerationState


@dataclass(frozen=True)
class Limits:
    """`max_output` caps one model call's output tokens (thinking included); None leaves the host's per-call cap,
    which also bounds any larger value."""

    max_calls: int = 12
    max_queries: int = 8
    max_repairs: int = 3
    call_timeout: float = 180
    max_checks: int = 3
    max_output: int | None = None

    def __post_init__(self):
        if (
            not all(
                isinstance(value, int)
                for value in (self.max_calls, self.max_queries, self.max_repairs, self.max_checks)
            )
            or self.max_calls < 1
            or self.max_queries < 0
            or self.max_repairs < 0
            or self.max_checks < 0
            or not isfinite(self.call_timeout)
            or self.call_timeout <= 0
            or (self.max_output is not None and (not isinstance(self.max_output, int) or self.max_output < 1))
        ):
            raise ValueError("generation limits must be finite positive bounds (query/repair/check counts may be zero)")


@dataclass(frozen=True)
class Generated:
    candidate: Candidate
    validation: Validation
    trace: tuple[dict, ...]


class GenerationError(RuntimeError):
    def __init__(self, message, trace=()):
        super().__init__(message)
        self.trace = tuple(trace)


class GenerationInterruptedError(GenerationError):
    """An incomplete generation whose investigation remains valid for an explicit continuation."""

    def __init__(self, reason, state):
        super().__init__(reason, state.trace)
        self.state = state.model_copy(deep=True)


class GenerationPausedError(GenerationInterruptedError):
    def __init__(self, state):
        super().__init__("Curator call budget exhausted; resume with a higher total max_calls", state)


def _argument_errors(response):
    invalid_calls = []
    for call in response.tool_calls:
        meta = call.run_meta
        if response.truncated or response.finish_reason == "length" or (meta and meta.truncation):
            error = (
                "The response or tool arguments were truncated at the per-call output limit, which counts your "
                "reasoning too. Resubmit a complete call that fits: split a large module into several smaller files "
                "staged one per call, and keep drafts out of your reasoning."
            )
        elif RAW_ARGUMENTS_KEY in call.arguments or (meta and meta.arguments_repaired):
            error = "Tool arguments were not a valid JSON object. Resubmit with correctly encoded arguments."
            raw = call.arguments.get(RAW_ARGUMENTS_KEY)
            if isinstance(raw, str):
                try:
                    json.loads(raw)
                except json.JSONDecodeError as exc:
                    error += f" Parser: {exc}"
        else:
            continue
        invalid_calls.append({"tool_call_id": call.id, "tool": call.name, "error": error})
    return invalid_calls


CHECK = "check_candidate"
THINKING = (None, "low", "none")


def _marked(message: dict) -> dict | None:
    content = message.get("content")
    if isinstance(content, str) and content:
        return {**message, "content": [{"type": "text", "text": content, "cache_control": cache_control()}]}
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        return {**message, "content": [*content[:-1], {**content[-1], "cache_control": cache_control()}]}
    return None


def cached(messages: list[dict], tools: list[dict] | None, model: str) -> tuple[list[dict], list[dict] | None]:
    """Copies of a stage request with cache breakpoints, for a model that reads them only when marked.

    The tools, the shared rules and the curation's materials (the first two messages) stay the same across the
    stages and scopes of one curation, and a stage's messages only grow while the trailing budget changes every
    call. So the marks go on the tools, those two messages and the last message before the budget: each call reads
    what the previous call or stage already sent and pays fresh only for what is new.
    """
    if not accepts_cache_control(model):
        return messages, tools
    marked = list(messages)
    tail = next((index for index in range(len(marked) - 2, 1, -1) if _marked(marked[index])), None)
    for index in dict.fromkeys(index for index in (0, 1, tail) if index is not None and index < len(marked)):
        marked[index] = _marked(marked[index]) or marked[index]
    if tools:
        tools = [*tools[:-1], {**tools[-1], "cache_control": cache_control()}]
    return claim_marks(marked), tools


def _shared_tools(context, tool_registry) -> list[dict]:
    """Every action any stage can use, with stage-independent schemas: one list for the whole curation.

    A provider's prompt cache keys on the tool list as well as the messages, so a list that changed by stage would
    make every stage pay for the whole request again; each stage's narrowed submission schemas travel in its data.
    """
    declaration = context.declaration
    check = implement.output()
    check["function"]["name"] = CHECK
    check["function"]["description"] = (
        "Check a draft using the current plan and host validator without submitting or installing it. "
        "Uses a separate preflight budget; the result reports only the checks actually performed."
    )
    tools = [
        *query.tools(context),
        *(tool_registry.get_definitions() if tool_registry is not None else []),
        select.output(declaration),
        select.output(declaration, revise=True),
        design.output(declaration),
        implement.output(),
        repair.reopen(),
        check,
        implement.stage_tool(),
        understand.gap_tool(),
    ]
    names = [item["function"]["name"] for item in tools]
    if len(names) != len(set(names)):
        raise ValueError("Curator tool names must be unique across native and stage operations")
    return tools


STAGED_ACTIONS = frozenset(
    {select.NAME, select.REVISE, design.NAME, implement.NAME, repair.NAME, CHECK, implement.STAGE_FILE}
)


async def _ask(
    stage,
    stages,
    stage_materials,
    schemas,
    parsers,
    check_parser=None,
    *,
    context,
    shared_materials,
    provider,
    state,
    limits,
    validate,
    model,
    tool_registry,
    stage_candidate,
    stage_files=False,
    progress=None,
):
    """One stage's tool loop. `schemas` gives the narrowed argument schema of each submission this stage accepts."""
    trace = state.trace
    tools = _shared_tools(context, tool_registry)
    offered = {
        *parsers,
        understand.GAP,
        *((CHECK,) if check_parser is not None else ()),
        *((implement.STAGE_FILE,) if stage_files else ()),
    }
    available = {name for name in (item["function"]["name"] for item in tools) if name not in STAGED_ACTIONS} | offered
    if not state.messages:
        state.messages = render.messages(
            stages,
            shared_materials,
            {
                **stage_materials,
                "submission_schemas": schemas,
                "history": [event for event in trace if event["event"] != "model.call"],
            },
            tools=tools,
            available=available,
        )
    messages = state.messages
    parsers = {**parsers, understand.GAP: understand.parse_gap}
    # A call that spends its whole output on thinking returns nothing, and some providers still report `stop`: each such
    # call moves the rest of this stage one step down THINKING (the configured effort, then low, then none) so a later
    # call can still submit.
    thinking = 0
    while state.calls < limits.max_calls:
        if progress:
            progress(state)
        request_messages = render.trailing(
            messages,
            render.budget(
                calls=limits.max_calls - state.calls,
                queries=limits.max_queries - state.queries,
                checks=limits.max_checks - state.checks,
                repairs=limits.max_repairs - state.repairs,
            ),
        )
        state.calls += 1
        trace.append({"stage": stage, "event": "model.call", "call": state.calls})
        default = getattr(provider, "get_default_model", None)
        marked, marked_tools = cached(request_messages, tools, model or (default() if default else ""))
        try:
            async with asyncio.timeout(limits.call_timeout):
                response = await provider.chat_with_retry(
                    messages=marked,
                    tools=marked_tools,
                    model=model,
                    tool_choice="auto",
                    **({"max_tokens": limits.max_output} if limits.max_output else {}),
                    **({"reasoning_effort": THINKING[thinking]} if thinking else {}),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise GenerationInterruptedError(f"{stage}: model call failed: {exc}", state) from exc
        if response.finish_reason == "error":
            raise GenerationInterruptedError(f"{stage}: provider error: {response.content}", state)
        if response.content:
            trace.append({"stage": stage, "event": "model.note", "content": response.content})
        assistant = render.assistant_message(response)
        messages.append(assistant)
        invalid_calls = _argument_errors(response)
        if invalid_calls:
            result = {
                "error": "No tools in this response were executed. Correct and resubmit.",
                "details": invalid_calls,
            }
            for call in response.tool_calls:
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
            trace.append(
                {
                    "stage": stage,
                    "event": "output.rejected",
                    "calls": assistant["tool_calls"],
                    **result,
                }
            )
            continue
        submitted = [call for call in response.tool_calls if call.name in parsers]
        if submitted:
            if len(response.tool_calls) != 1:
                error = "Submit exactly once, separately from exploration or preflight calls."
                for call in response.tool_calls:
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps({"error": error})})
                trace.append({"stage": stage, "event": "output.rejected", "error": error})
                continue
            call = submitted[0]
            try:
                value = parsers[call.name](call.arguments)
            except (ValueError, TypeError, KeyError) as exc:
                error = str(exc)[:6000]
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps({"error": error})})
                trace.append(
                    {
                        "stage": stage,
                        "event": "output.rejected",
                        "tool": call.name,
                        "arguments": call.arguments,
                        "error": error,
                    }
                )
                continue
            trace.append({"stage": stage, "event": call.name, "output": plain(value)})
            if call.name == understand.GAP:
                raise GenerationError(f"{stage}: {value}", trace)
            return call.name, value
        overthought = not response.tool_calls and (
            response.finish_reason == "length" or not (response.content or "").strip()
        )
        if overthought:
            thinking = min(thinking + 1, len(THINKING) - 1)
        if not response.tool_calls:
            messages.append(
                {
                    "role": "user",
                    "content": "The last answer ran out of output before any tool call; submit now with the supplied "
                    "tool, keeping the reasoning short."
                    if overthought
                    else "Use the supplied submission tool, or query a concrete information gap.",
                }
            )
            trace.append(
                {
                    "stage": stage,
                    "event": "output.missing",
                    "finish_reason": response.finish_reason,
                    **({"next_effort": THINKING[thinking]} if overthought else {}),
                }
            )
            continue
        for index, call in enumerate(response.tool_calls):
            if call.name in STAGED_ACTIONS and call.name not in offered:
                result = {
                    "error": f"`{call.name}` is not available in the {stage} stage; use one of "
                    + ", ".join(sorted(offered & STAGED_ACTIONS))
                }
                trace.append({"stage": stage, "event": "action.unavailable", "tool": call.name, "result": result})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
                continue
            if stage_files and call.name == implement.STAGE_FILE:
                try:
                    staged = implement.parse_staged(call.arguments)
                    state.staged[staged.path] = staged.content
                    result = {"staged": staged.path, "chars": len(staged.content), "staged_files": sorted(state.staged)}
                except (ValueError, TypeError) as exc:
                    result = {"error": str(exc)[:2000]}
                trace.append(
                    {"stage": stage, "event": "file.staged", "path": call.arguments.get("path"), "result": result}
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)}
                )
                continue
            if call.name == CHECK and check_parser is not None:
                if state.checks >= limits.max_checks:
                    result = {"error": "preflight budget exhausted; submit the candidate or report the gap"}
                else:
                    state.checks += 1
                    try:
                        candidate = check_parser(call.arguments)
                        location = stage_candidate(candidate) if stage_candidate else None
                        validation = await validate(candidate)
                        if not isinstance(validation, Validation):
                            raise TypeError("the host validator must return Validation")
                        result = {
                            "passed": validation.passed,
                            "errors": validation.errors,
                            "observations": render.observations(validation.observations),
                            "candidate": location,
                        }
                    except (ValueError, TypeError, KeyError) as exc:
                        result = {"error": str(exc)}
                trace.append({"stage": stage, "event": "preflight", "arguments": call.arguments, "result": result})
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)}
                )
                continue
            if state.queries >= limits.max_queries:
                result = {
                    "error": "exploration query budget exhausted; use retained evidence to submit or report a gap"
                }
                trace.append(
                    {
                        "stage": stage,
                        "event": "query.rejected",
                        "tool": call.name,
                        "arguments": call.arguments,
                        "result": result,
                    }
                )
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
                continue
            state.queries += 1
            if tool_registry is not None and tool_registry.get(call.name) is not None:
                set_current_tool_call_id(call.id)
                output = await tool_registry.execute(call.name, call.arguments, run_meta=call.run_meta)
                result = {
                    "text": wrap_untrusted(str(output), source=call.name),
                    "failed": call_failed(output),
                    "retryable": getattr(output, "retryable", True),
                    "blocks_call": getattr(output, "blocks_call", False),
                    "continuation": getattr(output, "continuation", Continuation.CONTINUE),
                }
                trace.append(
                    {
                        "stage": stage,
                        "event": "query",
                        "tool": call.name,
                        "arguments": call.arguments,
                        "result": result,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": result["text"],
                    }
                )
                if result["blocks_call"]:
                    for skipped in response.tool_calls[index + 1 :]:
                        messages.append(
                            {"role": "tool", "tool_call_id": skipped.id, "content": SKIPPED_AFTER_BLOCKED_CALL}
                        )
                        trace.append(
                            {
                                "stage": stage,
                                "event": "tool.skipped",
                                "tool": skipped.name,
                                "tool_call_id": skipped.id,
                            }
                        )
                    if result["continuation"] == Continuation.ABORT_TURN:
                        raise GenerationError("Curator execution stopped by native tool decision", trace)
                    break
                continue
            try:
                result = query.execute(context, call.name, call.arguments)
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                result = {"error": str(exc)[:4000]}
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)}
            )
            trace.append(
                {"stage": stage, "event": "query", "tool": call.name, "arguments": call.arguments, "result": result}
            )
    raise GenerationPausedError(state)


async def generate(
    context: Context,
    provider: LLMProvider,
    *,
    validate: Callable[[Candidate], Awaitable[Validation]],
    model: str | None = None,
    limits: Limits = Limits(),
    tool_registry=None,
    stage_candidate=None,
    resume: GenerationState | None = None,
    progress=None,
) -> Generated:
    """Run three bounded stages; stage_candidate(None) clears a superseded draft.

    `progress(state)` is called before every model call and after every validation, so a reader can follow a
    generation while it runs."""
    identity = GenerationState.identity(context)
    state = resume.model_copy(deep=True) if resume is not None else GenerationState(input_id=identity)
    state.check(context, limits)
    trace = state.trace

    shared = understand.materials(context)
    shared_materials = {
        "task": shared.pop("task"),
        "orientation": shared.pop("orientation"),
        "available_targets": select.materials(context.declaration),
        **shared,
    }
    ask = partial(
        _ask,
        context=context,
        shared_materials=shared_materials,
        provider=provider,
        state=state,
        limits=limits,
        validate=validate,
        model=model,
        tool_registry=tool_registry,
        stage_candidate=stage_candidate,
        progress=progress,
    )

    async def checked(candidate):
        if stage_candidate:
            stage_candidate(candidate)
        validation = await validate(candidate)
        if not isinstance(validation, Validation):
            raise TypeError("the host validator must return Validation")
        trace.append(
            {
                "stage": "validate",
                "event": "validation",
                "errors": validation.errors,
                "observations": validation.observations,
            }
        )
        if progress:
            progress(state)
        if validation.passed:
            return Generated(candidate, validation, tuple(trace))
        if state.repairs >= limits.max_repairs:
            raise GenerationError("repair budget exhausted: " + "; ".join(validation.errors), trace)
        state.repairs += 1
        state.plan = candidate.plan
        state.candidate, state.validation = candidate, validation
        state.advance("repair")
        return None

    while True:
        if state.stage == "select":
            _, state.selection = await ask(
                "select",
                ("understand", "select"),
                {},
                {},
                {select.NAME: context.declaration.parse_selection},
            )
            state.advance("design")
        selection = state.selection
        if selection is None:
            raise ValueError("generation state is missing its selection")
        if state.stage == "design" and not selection.targets and context.facts.get("scope", {}).get("kind") != "root":
            candidate = context.declaration.accept(Plan(understanding=selection.understanding), {"values": {}})
            if result := await checked(candidate):
                return result
            continue
        if state.stage == "design":
            name, proposal = await ask(
                "design",
                ("design",),
                design.materials(context, selection),
                {design.NAME: design.schema(context.declaration, selection)},
                {
                    design.NAME: lambda value: design.parse(context.declaration, selection, value),
                    select.REVISE: context.declaration.parse_selection,
                },
            )
            if name == select.REVISE:
                state.selection = proposal
                state.advance("design")
                continue
            state.plan = proposal
            state.advance("implement")
        plan = state.plan
        authored = set((context.facts.get("authored") or {}).get("files") or {})
        if plan is None:
            raise ValueError("generation state is missing its plan")
        materials = implement.materials(context, selection, plan)
        if state.stage == "repair":
            if state.candidate is None or state.validation is None:
                raise ValueError("repair state is missing its candidate or validation")
            materials = repair.materials(context, selection, state.candidate, state.validation)
        artifact_schema = context.declaration.artifact_schema(plan)
        name, proposal = await ask(
            state.stage,
            ("implement",) if state.stage == "implement" else ("implement", "repair"),
            materials,
            {implement.NAME: artifact_schema, CHECK: artifact_schema},
            {
                implement.NAME: lambda value: implement.parse(context.declaration, plan, value, state.staged, authored),
                repair.NAME: repair.Revision.model_validate,
                select.REVISE: context.declaration.parse_selection,
            },
            check_parser=lambda value: implement.parse(context.declaration, plan, value, state.staged, authored),
            stage_files=True,
        )
        if name in (repair.NAME, select.REVISE):
            state.staged = {}
            if stage_candidate:
                stage_candidate(None)
            if name == select.REVISE:
                state.selection = proposal
            state.plan = state.candidate = state.validation = None
            state.advance("design")
            continue
        if result := await checked(proposal):
            return result
