"""Translate generated action judgments without replacing Raven's execution loop."""

from raven.agent.hook.participant import ParticipantHook
from raven.contracts.participant import AgentParticipant, Intake

from ...harness.strategies import ActionStrategy
from ..calls import translator
from ..strategy import BoundStrategy
from ..targets.action import ReviewResult


class BoundAction(BoundStrategy):
    def __init__(self, config, task, path, package, recorder, *, infer=None, plan=None):
        super().__init__(
            "action",
            ActionStrategy,
            config,
            task,
            path,
            package,
            recorder,
            optional=("guide",) if config.guidance else (),
            infer=infer,
            plan=plan,
        )
        self.guidance = translator(config.guidance, package, 1)
        if self.guidance and (
            self.types["guide"][0] != self.types["assess"][0] or self.types["guide"][1] not in (str, str | None)
        ):
            raise TypeError("guide must use the assessment input type and return str | None")
        if self.types["assess"][1] != self.types["recover"][1]:
            raise TypeError("action assess and recover must share their concrete decision type")
        self.proposal = translator(config.proposal, package, 1)
        self.decision = translator(config.decision, package, 1)
        self.failure = translator(config.failure, package, 1)
        self.reply = translator(config.reply, package, 1)

    async def assess(self, proposal):
        return await self.call("assess", proposal, source="review")

    async def recover(self, failure):
        return await self.call("recover", failure, source="recovery")

    def hook(self):
        owner = self

        class ActionParticipant(AgentParticipant):
            @owner.callback
            async def system_addendum(self, step):
                if owner.guidance is None:
                    return None
                situation = owner.translate("guidance", owner.guidance, step, output=owner.types["guide"][0][0] | None)
                if situation is None:
                    return None
                text = await owner.call("guide", situation, source="guidance")
                return Intake(text) if text is not None else None

            @owner.callback
            async def review(self, step):
                if owner.proposal is None:
                    return None
                proposal = owner.translate("proposal", owner.proposal, step, output=owner.types["assess"][0][0] | None)
                if proposal is None:
                    return None
                result = await owner.assess(proposal)
                verdict = owner.translate("decision", owner.decision, result, output=ReviewResult | None)
                return verdict.model_dump() if verdict is not None else None

            @owner.callback
            async def salvage(self, step):
                if owner.failure is None:
                    return None
                failure = owner.translate("failure", owner.failure, step, output=owner.types["recover"][0][0] | None)
                if failure is None:
                    return None
                result = await owner.recover(failure)
                return owner.translate("reply", owner.reply, result, output=str | None)

        return ParticipantHook("curator-action", ActionParticipant, rolls_back=self.proposal is not None)
