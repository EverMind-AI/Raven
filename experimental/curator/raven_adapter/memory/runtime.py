"""Expose semantic memory retrieval and retention at native observation points."""

from raven.agent.hook.participant import ParticipantHook
from raven.contracts.participant import AgentParticipant, Intake

from ...harness.strategies import MemoryStrategy
from ...harness.strategies.memory import ContextRequest, ContextView
from ..calls import translator
from ..strategy import BoundStrategy


class BoundMemory(BoundStrategy):
    def __init__(self, config, task, path, package, recorder, *, infer=None, plan=None):
        super().__init__(
            "memory",
            MemoryStrategy,
            config,
            task,
            path,
            package,
            recorder,
            optional=("compose", "compact") if config.composition else (),
            infer=infer,
            plan=plan,
            per_session=False,
        )
        if config.composition:
            for operation in ("compose", "compact"):
                if self.types[operation] != ([ContextRequest], ContextView):
                    raise TypeError(f"memory {operation} must accept ContextRequest and return ContextView")
        self.query = translator(config.query, package, 1)
        self.render = translator(config.context, package, 1)
        self.record = translator(config.retain, package, 1)

    async def recall(self, query):
        return await self.call("recall", query, source="context", readonly=True)

    async def retain(self, record):
        return await self.call("retain", record, source="observation")

    def hook(self):
        owner = self

        class MemoryParticipant(AgentParticipant):
            @owner.callback
            async def system_addendum(self, step):
                if owner.query is None:
                    return None
                query = owner.translate("query", owner.query, step, output=owner.types["recall"][0][0] | None)
                if query is None:
                    return None
                result = await owner.recall(query)
                content = owner.translate("context", owner.render, result, output=str | None)
                return Intake(content) if content is not None else None

            @owner.callback
            async def advise(self, step):
                if step.phase == "after_iteration" and owner.record is not None:
                    record = owner.translate("retain", owner.record, step, output=owner.types["retain"][0][0] | None)
                    if record is not None:
                        await owner.retain(record)
                return None

        return ParticipantHook("curator-memory", MemoryParticipant, rolls_back=False)
