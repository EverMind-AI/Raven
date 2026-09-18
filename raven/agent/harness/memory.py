"""The default Memory strategy: the window this turn's model calls see.

The context engine stays the thing that assembles -- it holds the Curator's
working state and each segment builder's provider binding, and the builders
were sized against this generation's context window, so a generation's window
*is* its engine and replacing it is a new generation. What this module adds is
the seat: the loop asks the Memory role rather than reaching past it, and the
turn-side decisions that were never the shell's live here with it.

``candidate_messages`` turns on ``owns_compaction``, which is this role's own
property: an engine that archives for itself wants the whole append-only log
so it can decide what to evict, one that does not wants the post-consolidation
slice so the view matches what the consolidator left behind.

``token_budget`` sizes the prompt against the model's own ceiling, which is the
window's arithmetic by definition -- and the five things it reads (provider,
model, window, tool definitions, identity builder) are the same five the engine
itself was constructed with, so this is the logic returning to the role that
owns it rather than new coupling. All five are read through callables because
all five move under a live ``/model`` switch or a hot config apply, and a value
captured at assembly would answer for the retired one.

Still with the shell on purpose: the assembly orchestration around ``assemble``
(building the TurnContext, save for the brief this role fills in; the
degraded-segment stash; the injected-skill bookkeeping the loop reads back
afterwards). Half of that is the window's and half is the shell's, so it is
not a move this role can make honestly.

``shrink`` is the window's mid-turn seat: the five ways a transcript is made to
fit again (see ``raven.agent.window.shrink``), asked for by the loop under a
``WindowPressure`` and answered against a ``WindowState`` the loop carries. The
policy -- what to give up, when a summary is worth paying for -- lives here;
the retry itself (re-entering the iteration) stays with the loop, so the hooks
see a retried call exactly as they saw the first.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.harness.conducts import compose_addendum, compose_intake, compose_record
from raven.agent.window import compaction, shrink
from raven.contracts.agent_conduct import AgentConduct, Intake, StepView
from raven.contracts.assembled import TokenBudget
from raven.contracts.harness import MemoryModule, ShrinkResult, WindowPressure, WindowState
from raven.providers.base import send_max_tokens
from raven.utils.tokens import estimate_prompt_tokens

if TYPE_CHECKING:
    from raven.contracts.assembled import AssembledContext
    from raven.contracts.context import ContextEngine, TurnContext


class DefaultMemory:
    """This generation's context engine, plus the window's own turn decisions."""

    def __init__(
        self,
        engine: "ContextEngine",
        *,
        provider: Callable[[], Any],
        model: Callable[[], str],
        context_window_tokens: Callable[[], int],
        tool_definitions: Callable[[], list[dict[str, Any]]],
        system_prompt: Callable[[list[Any] | None], str],
        compaction: Callable[[], Any],
        output_ceiling: Callable[[str | None], int],
    ) -> None:
        self._engine = engine
        # The two the window's mid-turn moves read: the compaction settings
        # (config-gated, factory-off) and the output ceiling a request for a
        # model will carry, which sizes what the window must keep in reserve.
        self._compaction = compaction
        self._output_ceiling = output_ceiling
        self._provider = provider
        self._model = model
        self._window = context_window_tokens
        self._tool_definitions = tool_definitions
        self._system_prompt = system_prompt

    @property
    def owns_compaction(self) -> bool:
        return self._engine.owns_compaction

    def candidate_messages(self, session: Any) -> list[dict[str, Any]]:
        if self._engine.owns_compaction:
            return list(session.messages)
        return session.get_history(max_messages=0)

    def token_budget(self, selected_skills: list[Any] | None = None) -> TokenBudget:
        provider = self._provider()
        window = self._window()
        # allow_fetch=False for the reason construction passes it: this runs per
        # turn on the loop's own thread and only needs a number to reserve, not
        # the one a request will carry. The fallback under-reserves at worst.
        ceiling = send_max_tokens(
            getattr(provider, "generation", None),
            # The id a request goes out under, so the reservation matches the
            # ceiling that request will carry rather than the stored name's.
            getattr(provider, "wire_model_id", lambda m: m)(self._model()),
            allow_fetch=False,
        )
        # The whole ceiling, not a share of it. Requests no longer name a
        # ceiling, so the one that applies is the model's own. Reserving less
        # hands out a prompt the reply cannot coexist with: measured on this
        # repo's default model, a share leaves the prompt 150000 of a 200000
        # window against a reply allowed 64000, and the sum is refused at
        # request time -- and the emergency shrink only elides tool bodies, so
        # a history grown on conversation gets no retry from that refusal.
        reserved_output = min(ceiling, window)
        tool_tokens = estimate_prompt_tokens([], self._tool_definitions())
        system_tokens = estimate_prompt_tokens([{"role": "system", "content": self._system_prompt(selected_skills)}])
        return TokenBudget(
            context_length=window,
            reserved_output=reserved_output,
            reserved_tools=tool_tokens,
            reserved_system=system_tokens,
            available_history=max(0, window - reserved_output - tool_tokens - system_tokens),
        )

    async def assemble(
        self,
        session_key: str,
        session_messages: list[dict[str, Any]],
        budget: TokenBudget,
        *,
        turn: "TurnContext",
    ) -> "AssembledContext":
        return await self._engine.assemble(session_key, session_messages, budget, turn=self._briefed(turn))

    @staticmethod
    def _briefed(turn: "TurnContext") -> "TurnContext":
        """The turn with what this dispatch's Charter asked of it, if anything.

        Read here rather than where the text is rendered: what a turn shows its
        model is this role's question, so a later field ("carry the memory
        segment this time", "recall five rather than three") has one obvious
        home.

        The rendering stays with the segment that owns the identity's shape.
        This hands over the two strings and nothing about how they look.

        Imported in the call for the reason ``DefaultAction.judge`` states:
        naming the charter at the top makes importing the harness import the
        whole sub-agent package. Nothing is caught around it -- a catch would
        trade a loud failure for a turn that silently ran unbriefed.
        """
        from raven.agent.subagent.charter import current_charter

        charter = current_charter()
        if charter is None or not (charter.prompt or charter.stop_when):
            return turn
        return replace(turn, task_brief=charter.prompt, task_done_when=charter.stop_when)

    async def shrink(
        self,
        messages: list[dict[str, Any]],
        *,
        pressure: WindowPressure,
        state: WindowState,
        model: str | None,
    ) -> ShrinkResult:
        if pressure == WindowPressure.PROACTIVE:
            return await self._compact_ahead(messages, state, model)
        if pressure == WindowPressure.STANDING:
            return self._standing_window(messages, state)
        if pressure == WindowPressure.OVERFLOW:
            return await self._on_overflow(messages, state, model)
        if pressure == WindowPressure.TOOL_IMAGES_REFUSED:
            return self._on_tool_images_refused(messages, state)
        if pressure == WindowPressure.IMAGES_TOO_LARGE:
            return self._on_images_too_large(messages, state)
        # ``==`` rather than ``is`` above: ``WindowPressure`` is a ``str`` Enum,
        # so the documented string a config, an event payload or a replacement
        # shell hands over compares equal to the member but is not it.
        raise ValueError(f"unknown window pressure {pressure!r}")

    async def _summarize_head(
        self, messages: list[dict[str, Any]], model: str | None, cfg: Any
    ) -> tuple[list[dict[str, Any]], str]:
        chosen = model or self._model()
        return await shrink.summarize_head(
            messages,
            provider=self._provider(),
            model=chosen,
            window=self._window(),
            ceiling=self._output_ceiling(chosen),
            cfg=cfg,
        )

    async def _compact_ahead(
        self, messages: list[dict[str, Any]], state: WindowState, model: str | None
    ) -> ShrinkResult:
        """The proactive layer: act on the last billed reading before the next
        call, so recovery does not have to wait for the window to blow.

        Deterministic pruning runs first; the LLM head summary runs only when
        pruning is not enough, and shares the overflow-retry budget so summary
        calls stay bounded per turn.
        """
        cfg = self._compaction()
        if not (cfg.enabled and state.last_context_used):
            return ShrinkResult(messages, False)
        limit = self._window()
        reserved = compaction.reserved_tokens(cfg.reserved_tokens, self._output_ceiling(model))
        if not compaction.should_compact(state.last_context_used, limit, reserved, cfg.trigger_ratio):
            return ShrinkResult(messages, False)
        changed = False
        projected = state.last_context_used
        if cfg.prune:
            pruned, elided = shrink.emergency_shrink(messages)
            if elided > 0:
                # No server reading exists for the pruned list until the next
                # response, so judge the summary tier by projecting the
                # estimated savings onto the observed size (local estimates do
                # not know the server's tokenizer; the delta is safer than the
                # absolute).
                saved = max(0, estimate_prompt_tokens(messages) - estimate_prompt_tokens(pruned))
                messages = pruned
                projected = max(0, state.last_context_used - saved)
                state.last_context_used = 0
                changed = True
                logger.warning(
                    "Context near window; elided {} older transcript item(s) before the next call{}",
                    elided,
                    (
                        " (the head summary failed earlier this turn, so eliding is all that is left)"
                        if state.head_summary_failures
                        else ""
                    ),
                )
        if (
            compaction.should_compact(projected, limit, reserved, cfg.trigger_ratio)
            and state.compress_retries < shrink.MAX_COMPRESS_RETRIES
        ):
            summarized, verdict = await self._summarize_head(messages, model, cfg)
            if verdict != "skipped":
                state.compress_retries += 1
            if verdict == "failed":
                state.head_summary_failures += 1
            if verdict == "changed":
                messages = summarized
                state.last_context_used = 0
                changed = True
                logger.warning(
                    "Context near window; summarized the transcript head before the next call ({}/{})",
                    state.compress_retries,
                    shrink.MAX_COMPRESS_RETRIES,
                )
        return ShrinkResult(messages, changed)

    @staticmethod
    def _standing_window(messages: list[dict[str, Any]], state: WindowState) -> ShrinkResult:
        """The standing image window, in place: pictures stay while they fit the
        budget and collapse to the newest ``image_window`` messages when they do
        not; a picture withdrawn once stays withdrawn (see ``shrink.window_images``)."""
        if state.image_budget is None and state.image_window >= shrink.IMAGE_WINDOW_RECENT_MESSAGES:
            return ShrinkResult(messages, False)
        windowed, withdrawn = shrink.window_images(
            messages,
            state.image_window,
            budget=state.image_budget,
            reason="budget" if state.image_budget is not None else "superseded",
        )
        if windowed:
            logger.info(
                "Image window: withdrew {} picture(s) from {} older message(s); the newest {} keep theirs "
                "(budget {} bytes)",
                withdrawn,
                windowed,
                state.image_window,
                state.image_budget,
            )
        return ShrinkResult(messages, bool(windowed))

    async def _on_overflow(self, messages: list[dict[str, Any]], state: WindowState, model: str | None) -> ShrinkResult:
        """The endpoint refused the request as too long: elide the bulk of
        accumulated tool output (a smaller window would not help), and when
        nothing is left to elide, summarize the head once per turn. Bounded by
        the shared retry budget."""
        if state.compress_retries >= shrink.MAX_COMPRESS_RETRIES:
            return ShrinkResult(messages, False)
        shrunk, elided = shrink.emergency_shrink(messages)
        if elided > 0:
            state.compress_retries += 1
            state.last_context_used = 0
            logger.warning(
                "Context overflow; elided {} old tool result(s), retrying ({}/{})",
                elided,
                state.compress_retries,
                shrink.MAX_COMPRESS_RETRIES,
            )
            return ShrinkResult(shrunk, True)
        cfg = self._compaction()
        if cfg.enabled and not state.reactive_summary_tried:
            state.reactive_summary_tried = True
            summarized, verdict = await self._summarize_head(messages, model, cfg)
            if verdict == "failed":
                state.head_summary_failures += 1
            if verdict == "changed":
                state.compress_retries += 1
                state.last_context_used = 0
                logger.warning(
                    "Context overflow with nothing left to elide; summarized the transcript head, retrying ({}/{})",
                    state.compress_retries,
                    shrink.MAX_COMPRESS_RETRIES,
                )
                return ShrinkResult(summarized, True)
        return ShrinkResult(messages, False)

    @staticmethod
    def _on_tool_images_refused(messages: list[dict[str, Any]], state: WindowState) -> ShrinkResult:
        """This endpoint takes a picture only in a user message: rebuild onto
        the placeholder path -- the shape a False capability verdict would have
        produced -- so the retry lands on the already-tested shape rather than
        a third one. Once per turn: a refusal is deterministic for the model,
        and the shell caches the verdict on the first retry."""
        if state.image_demote_retries >= shrink.MAX_IMAGE_DEMOTE_RETRIES:
            return ShrinkResult(messages, False)
        demoted_messages, demoted = shrink.demote_tool_images(messages)
        if demoted == 0:
            return ShrinkResult(messages, False)
        state.image_demote_retries += 1
        logger.warning(
            "Endpoint refused {} image(s) in a tool result; moved them to a user message and retrying ({}/{})",
            demoted,
            state.image_demote_retries,
            shrink.MAX_IMAGE_DEMOTE_RETRIES,
        )
        return ShrinkResult(demoted_messages, True)

    @staticmethod
    def _on_images_too_large(messages: list[dict[str, Any]], state: WindowState) -> ShrinkResult:
        """Pictures refused for their size: the window closes a notch and the same
        ask goes again. A notch, not a one-off strip: the strip left the history
        as it was, and both measured runs refused again a few calls later once the
        pictures had built back up (amber 09:59 and 10:05, red 11:14 and 11:39,
        2026-09-05). First notch: the budget goes and only the newest message
        keeps its pictures, since that is the one the model has not read yet; when
        it alone is over the cap the second notch takes it too. The closed window
        then stands for the rest of the turn, so the refusal cannot recur.

        At zero the model sees no picture for the rest of the turn, and the notes
        say so. Accepted rather than papered over with a per-batch byte budget:
        reaching zero takes a single batch over the cap on its own, which at the
        measured render sizes means a build of thirty-odd pages returned in one
        call, and the four measured refusals were all accumulation (75-80 pictures
        over 14-19 messages; the window's replay peak on those same runs is 6.87
        MB against a cap measured at ~26.3 MB decoded). Add the budget when a run
        actually gets here.
        """
        if state.image_strip_retries >= shrink.MAX_IMAGE_STRIP_RETRIES:
            return ShrinkResult(messages, False)
        withdrawn = 0
        while not withdrawn and (state.image_budget is not None or state.image_window > 0):
            if state.image_budget is not None or state.image_window > 1:
                state.image_budget = None
                state.image_window = min(state.image_window, 1)
            else:
                state.image_window = 0
            _, withdrawn = shrink.window_images(
                messages, state.image_window, reason="refused", any_role=state.image_window == 0
            )
        if withdrawn == 0:
            return ShrinkResult(messages, False)
        state.image_strip_retries += 1
        logger.warning(
            "Endpoint refused the request's pictures as too large; withdrew {} and closed the "
            "image window to {} for the rest of the turn, retrying ({}/{})",
            withdrawn,
            state.image_window,
            state.image_strip_retries,
            shrink.MAX_IMAGE_STRIP_RETRIES,
        )
        return ShrinkResult(messages, True)

    async def after_turn(self, session_key: str, outcome: dict[str, Any]) -> None:
        await self._engine.after_turn(session_key, outcome)

    async def read_inbound(self, text: str, step: StepView, conducts: Sequence[AgentConduct]) -> Intake | None:
        return await compose_intake(text, step, conducts)

    async def compose_addendum(self, step: StepView, conducts: Sequence[AgentConduct]) -> Intake | None:
        return await compose_addendum(step, conducts)

    async def file_record(
        self, step: StepView, reply: str | None, conducts: Sequence[AgentConduct]
    ) -> dict[str, dict[str, Any]] | None:
        return await compose_record(step, reply, conducts)


def bind(memory: DefaultMemory) -> MemoryModule:
    """Admit a built Memory role, naming a missing member at assembly rather
    than as an AttributeError inside somebody's turn."""
    if not isinstance(memory, MemoryModule):
        raise TypeError(
            f"{type(memory).__name__} cannot serve as the Memory role: it must provide "
            "owns_compaction, candidate_messages, token_budget, assemble, shrink, read_inbound and after_turn"
        )
    return memory


__all__ = ["DefaultMemory", "bind"]
