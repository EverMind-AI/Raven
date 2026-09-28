"""Run a generated planning strategy with per-session checkpoints and native interaction paths."""

import asyncio
import json
from copy import deepcopy
from inspect import getdoc, iscoroutinefunction, signature
from pathlib import Path
from typing import Any, TypeVar, get_type_hints

from pydantic import ConfigDict, create_model

from raven.agent.hook.participant import ParticipantHook
from raven.contracts.participant import AgentParticipant, Intake
from raven.contracts.tool import Tool

from ...harness.declaration import parse_as, schema_for, typed
from ...harness.strategies import PlanningStrategy
from ..calls import translator
from ..inference import strategy_factory
from ..observe import plain
from ..strategy import Scopes
from .contracts import TOOL_NAME, PlanningBinding, PlanningObservation


class BoundPlanning:
    """One plan per session, shared by that session's tool calls, model context and observed iterations.

    A session's plan is initialized from the task on its first use, so each conversation starts its own plan.
    """

    def __init__(self, config: PlanningBinding, task, path: Path, package: Path, recorder, *, infer=None):
        self.config, self.task, self.path, self.recorder = config, task, path, recorder
        self.lock = asyncio.Lock()
        self.factory = lambda state: strategy_factory(
            config.factory, package, state, protocol=PlanningStrategy, infer=infer
        )
        self.scopes = Scopes("planning", task, path, self.factory, per_session=True)
        for name in ("initialize", "view", "revise"):
            method = getattr(self.strategy, name, None)
            if not iscoroutinefunction(method) or getattr(type(self.strategy), name, None) is getattr(
                PlanningStrategy, name
            ):
                raise TypeError(f"planning strategy must implement async {name}")
            count = len(signature(getattr(PlanningStrategy, name)).parameters) - 1
            signature(method).bind(*[object() for _ in range(count)])
        annotations = {name: get_type_hints(getattr(self.strategy, name)) for name in ("initialize", "view", "revise")}
        self.view_type = annotations["view"]["return"]
        self.change_type = annotations["revise"][next(iter(signature(self.strategy.revise).parameters))]
        if any(annotations[name]["return"] != self.view_type for name in ("initialize", "revise")):
            raise TypeError("planning initialize, view and revise must return the same concrete view type")
        for annotation in (self.view_type, self.change_type):
            if annotation is Any or isinstance(annotation, TypeVar) or not schema_for(annotation):
                raise TypeError("planning method types must be concrete")
        self.to_change = translator(config.tool, package, 1)
        self.render = translator(config.context, package, 1)
        self.observe = translator(config.observe, package, 2)
        self.request_type = None
        if self.to_change:
            parameter = next(iter(signature(self.to_change).parameters))
            request_type = get_type_hints(self.to_change)[parameter]
            if request_type is Any or isinstance(request_type, TypeVar) or not schema_for(request_type):
                raise TypeError("planning tool requires a concrete command annotation")
            self.request_type = create_model(
                "PlanningToolRequest", __config__=ConfigDict(extra="forbid"), request=(request_type, ...)
            )
        self.current_view = None
        self.views = {}

    @property
    def state(self) -> dict:
        return self.scopes.current().state

    @property
    def strategy(self):
        return self.scopes.current().owner

    def _restore(self, state):
        scope = self.scopes.current()
        scope.state.clear()
        scope.state.update(state)
        scope.owner = self.factory(scope.state)

    async def _enter(self):
        scope = self.scopes.current()
        if not scope.resuming:
            await self._call("initialize", self.task.text, source="task")
            scope.resuming = True

    def _translate(self, operation, function, *args, output):
        before = deepcopy(self.state)
        try:
            result = function(*(deepcopy(value) for value in args))
            if self.state != before:
                raise ValueError("planning translations must not mutate state")
            return typed(output, result)
        except BaseException as exc:
            self._restore(before)
            self.recorder.add(
                "planning.error",
                operation=operation,
                arguments=args,
                state=before,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

    async def _call(self, operation, *args, source):
        before = deepcopy(self.state)
        self.recorder.add("planning.call", operation=operation, source=source, arguments=args)
        try:
            value = typed(self.view_type, await getattr(self.strategy, operation)(*args))
            if value is None:
                raise ValueError("a planning view cannot be None")
            resuming = self.scopes.current().resuming
            if (operation == "view" or (operation == "initialize" and resuming)) and self.state != before:
                raise ValueError(f"planning {operation} changed existing state")
            updated = deepcopy(self.state)
            if operation != "view":
                actual = typed(self.view_type, await self.strategy.view())
                if self.state != updated or plain(actual) != plain(value):
                    raise ValueError("planning result and its read-only current view disagree")
            self.scopes.save()
            self.current_view = plain(value)
            self.views[self.scopes.key()] = self.current_view
        except BaseException as exc:
            self._restore(before)
            self.recorder.add(
                "planning.error",
                operation=operation,
                source=source,
                arguments=args,
                state=before,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        self.recorder.add("planning.result", operation=operation, source=source, result=value)
        return value

    def read(self):
        """The PlanReader the host supplies to other components: this conversation's last view, detached."""
        return deepcopy(self.views.get(self.scopes.key()))

    async def prepare(self):
        async with self.lock:
            scope = self.scopes.current()
            await self._call("initialize", self.task.text, source="task")
            scope.resuming = True

    async def tool_call(self, arguments):
        async with self.lock:
            await self._enter()
            try:
                request = parse_as(self.request_type, arguments, strict=True)
                change = self._translate("tool", self.to_change, request.request, output=self.change_type | None)
                if change is None:
                    return await self._call("view", source="tool")
                return await self._call("revise", change, source="tool")
            except (ValueError, TypeError) as exc:
                self.recorder.add("planning.error", operation="tool", source="tool", error=str(exc))
                raise

    async def addendum(self):
        if self.render is None:
            return None
        async with self.lock:
            await self._enter()
            view = await self._call("view", source="context")
            content = self._translate("context", self.render, view, output=str | None)
            self.recorder.add("planning.context", content=content)
            return content

    async def after_iteration(self, observation):
        if self.observe is None:
            return
        async with self.lock:
            await self._enter()
            view = await self._call("view", source="observation")
            self.recorder.add("planning.observation", observation=observation)
            change = self._translate("observe", self.observe, view, observation, output=self.change_type | None)
            if change is not None:
                await self._call("revise", change, source="observation")

    def facts(self):
        return {
            "task_id": self.task.id,
            "view": deepcopy(self.current_view),
            "state": deepcopy(self.state),
            "sessions": self.scopes.sessions(),
            "view_schema": schema_for(self.view_type),
            "change_schema": schema_for(self.change_type),
            "tool_schema": schema_for(self.request_type) if self.request_type else None,
            "binding": self.config.model_dump(mode="json"),
        }

    def hook(self):
        owner = self

        class PlanningParticipant(AgentParticipant):
            async def system_addendum(self, step):
                try:
                    content = await owner.addendum()
                    return Intake(content) if content is not None else None
                except Exception as exc:
                    owner.recorder.add("planning.error", operation="context", error=f"{type(exc).__name__}: {exc}")
                    raise

            async def advise(self, step):
                if step.phase == "after_iteration" and owner.observe is not None:
                    try:
                        await owner.after_iteration(
                            PlanningObservation(
                                iteration=step.iteration,
                                messages=plain(step.transcript[step.turn_base :]),
                                response=plain(step.response),
                            )
                        )
                    except Exception as exc:
                        owner.recorder.add("planning.error", operation="observe", error=f"{type(exc).__name__}: {exc}")
                        raise
                return None

        return ParticipantHook("curator-planning", PlanningParticipant)

    def tool(self):
        owner = self

        class PlanningTool(Tool):
            name = TOOL_NAME
            description = getdoc(owner.to_change) or "Read or revise this conversation's plan."
            parameters = schema_for(owner.request_type)

            async def execute(self, **kwargs):
                return json.dumps(plain(await owner.tool_call(kwargs)), ensure_ascii=False)

        return PlanningTool()
