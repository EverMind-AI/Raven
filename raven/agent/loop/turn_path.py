"""The turn execution path: dispatch, the agent loop, streaming, recovery,
persistence.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from raven.agent.loop._shared import (
    _ABORTED_ACTION_REPLY,
    _DELEGATED_KEY,
    _HOOK_INJECTED_KEY,
    _MAX_ITER_STATIC_FALLBACK,
    _MAX_ITER_SYNTHESIS_PROMPT,
    _MID_TURN_USER_KEY,
    _NOTICE_KEY,
    _ORIGIN_KEY,
    _REASONING_MS_KEY,
    _SKIP_AFTER_SEND_ORIGINS,
    _SKIP_USER_INBOUND_ORIGINS,
    _STALLED_STATIC_FALLBACK,
    _STALLED_SYNTHESIS_PROMPT,
    _TOOL_DURATION_MS_KEY,
    _TOOL_METADATA_KEY,
    _TOOL_PREVIEW_MAX_CHARS,
    OUTPUT_LIMIT_NUDGE,
    POST_TOOL_NUDGE,
    SKIPPED_AFTER_BLOCKED_CALL,
    Any,
    AskUserTool,
    Awaitable,
    Callable,
    ContextBuilder,
    Continuation,
    LLMResponse,
    LoopOutcome,
    MemoryStore,
    MessageTool,
    NoProgressAction,
    NoProgressGuard,
    Origin,
    RecoveryAction,
    Session,
    _appended_by_hook,
    _display_label,
    _file_change_payload,
    _file_removed_payload,
    _file_written_payload,
    _first_line,
    _listing_removals,
    _runtime_origin,
    _stamp_reasoning_ms,
    _strip_inline_images,
    append_hook_note,
    asyncio,
    autofill_resolver,
    classify_empty_response,
    current_autofill,
    failure_class,
    is_hard_tool_failure,
    is_only_think_debris,
    json,
    logger,
    loop_break_nudge,
    merge_mid_turn,
    monotonic,
    replace,
    resolve_context_window,
    semconv,
    session_of,
    stream_llm_call,
    strip_think_blocks,
    time,
    trace,
    turn_ask_kind,
    turn_budgets,
    turn_question,
    uuid4,
    workdir,
)
from raven.agent.loop.dead_end import NO_RESPONSE_FALLBACK, dead_reasons
from raven.agent.loop.first_call import FirstCallGuard
from raven.agent.loop.recovery import ContinuationGate, DraftGate, cut_reasoning_head, lower_reasoning_effort
from raven.agent.tools import snapshot as workdir_snapshot
from raven.agent.tools.registry import call_failed
from raven.agent.tools.removals import RemovalWatch
from raven.agent.window import shrink
from raven.agent.window.images import ATTACHED_IMAGE_KEY, IMAGE_SOURCES_KEY, filed_image_note, image_sources
from raven.contracts.harness import ActionRequest, CapabilityRequest, PlanningRequest, WindowPressure, WindowState
from raven.contracts.loop_hooks import HookDecision
from raven.permissions.turn import set_current_tool_call_id
from raven.providers import usage_record
from raven.providers.base import bound_llm_detail, canonical_llm_error, llm_error_summary, parse_llm_error
from raven.providers.first_byte import first_byte_budget
from raven.providers.tool_calls import openai_tool_call
from raven.spine.events import bound_failure_text
from raven.spine.turn import AnswerlessTurnError
from raven.token_wise import usage_context
from raven.token_wise.turn_spend import TurnSpend

if TYPE_CHECKING:
    from raven.agent.loop.checkpoint import CheckpointService
    from raven.contracts.token_strategy import UsageSnapshot
    from raven.providers.base import ErrorClassification
    from raven.spine.events import NoticeKind
    from raven.spine.runner import Drain, Emit, TurnOutcome
    from raven.spine.turn import TurnRequest


def _llm_failure_detail(content: str | None, verdict: ErrorClassification | None) -> str:
    """The one line a model call the loop gave up on is reported by.

    The error response's own text, which is the provider's account of the
    failure and nothing else -- no setting decides which of two meanings the
    content carries. A response with no text at all still needs a sentence the
    readers of that format can parse.

    A canonical sentence arrives bounded from the constructor that built it; a
    provider that words a failure its own way (the upstream-transport-failure
    account) is bounded here, so every route out of a failed call carries the
    same ceiling.
    """
    text = (content or "").strip()
    if text:
        return text if parse_llm_error(text) is not None else bound_llm_detail(text)
    category = verdict.category if verdict is not None else "unknown"
    return canonical_llm_error(category, None, "the provider gave no detail")


def _marker_failure(reason: str | None) -> str:
    """How a broken turn's marker tells the MODEL the turn failed.

    Separated from ``turn_ended.reason``, which keeps the provider's own
    account for whoever is diagnosing the failure, the way ``notice`` and
    ``origin`` are separated from what the model reads. A vendor body is not
    written for a model: it spends context on a masked key and an account URL,
    and reads as something to answer rather than as the end of the turn.
    """
    parsed = parse_llm_error(reason or "")
    if parsed is not None:
        category, provider, _detail = parsed
        return llm_error_summary(category, provider)
    return reason or "unknown error"


def _stamp_turn_observers(messages: list[dict[str, Any]], metadata: dict[str, Any] | None, turn_base: int) -> None:
    """File the turn's observer record onto its last substantive assistant message.

    The loop_hooks paper files ``metadata["observers"]`` on THE TURN'S message
    at persist time, so the search never crosses ``turn_base``: an answerless
    turn has no seat, and the assembled window can share dict objects with the
    session record (the curator candidate view), so stamping an earlier
    message would rewrite filed history in place.
    """
    if not metadata:
        return
    observers = metadata.get("observers")
    if not isinstance(observers, dict) or not observers:
        return
    for message in reversed(messages[turn_base:]):
        if message.get("role") == "assistant" and (message.get("content") or message.get("tool_calls")):
            message["observers"] = dict(observers)
            break


# What one log line of model output may carry. The whole reply is already in the
# llm.output artifact; this is the reading copy, so the bound is a paragraph or
# two -- enough to see the intent, short of turning the log into a transcript.
_SAID_LOG_MAX_CHARS = 2000


# The scaffolding a turn raises and does not keep: empty-response recovery marks
# its nudges and prefills, and an attached image rides its own key. They are
# dropped before persistence, so a reader asking what the turn ended on has to
# drop them too, or it answers about a message nobody is going to keep.
_TURN_TRANSIENT_KEYS = ("_recovery_synthetic", ATTACHED_IMAGE_KEY)


# How a turn reports the model saying nothing with no recovery behind it. The
# exhausted-recovery exit words its own sentence from the budgets it spent;
# this one has no counts to give, and says why there are none.
_NO_CONTENT_UNRECOVERED = "The model returned no content, and this turn's empty-response recovery is switched off."


def _log_what_the_model_said(response: Any) -> None:
    """Record the assistant's own words and how much it thought.

    A log that lists what a model did and never what it said cannot be read
    back into an intent: a run of 300 identical tool calls looks the same
    whether the model was converging or stuck. Both fields are truncated and
    report their full length, because either can be tens of thousands of chars.
    """

    def _line(label: str, text: str) -> None:
        # One line, so the log stays greppable by line as the tool lines are.
        flat = " ".join(text.split())
        head = flat[:_SAID_LOG_MAX_CHARS]
        logger.info("{} ({} chars): {}{}", label, len(text), head, " ..." if len(flat) > len(head) else "")

    said = response.content if isinstance(getattr(response, "content", None), str) else ""
    if said.strip():
        _line("Assistant", said)
    thought = getattr(response, "reasoning_content", None)
    if isinstance(thought, str) and thought.strip():
        _line("Assistant reasoning", thought)
    spent = (getattr(response, "usage", None) or {}).get("reasoning_tokens")
    if not (isinstance(thought, str) and thought.strip()) and spent:
        logger.info("Assistant thought for {} tokens the provider did not return", spent)


def _reasoning_wire_keys(provider: Any, model: str | None) -> Any:
    """The provider's "what would this effort send" answer, or None.

    None when the provider cannot say -- a test double, or an adapter from
    before the method existed -- and the retry descent then reads the labels on
    their own, which is what it did before. Never allowed to be the thing that
    fails: this is asked while recovering a turn that has already produced
    nothing, and an exception here would replace an empty answer with an error.
    """
    ask = getattr(provider, "reasoning_wire_keys", None)
    if not callable(ask):
        return None

    def shape(effort: str | None) -> object:
        try:
            return json.dumps(ask(model, effort), sort_keys=True, default=str)
        except Exception:  # noqa: BLE001 - an unanswerable question is not a different request
            return effort

    return shape


class TurnPathMixin:
    """The turn execution path: dispatch, the agent loop, streaming, recovery,
    persistence."""

    @staticmethod
    def _checkpoint_active(policy: str, interactive: bool) -> bool:
        """Resolve ``runtime.checkpoint.policy`` against the call-site's
        ``interactive`` signal. ``"interactive"`` (the default) skips the
        snapshot for one-shot ``-m`` invocations — those have no "next turn"
        to inject recovery into, so paying the snapshot cost there is just
        deadweight. ``"always"`` opts in regardless; ``"never"`` opts out
        regardless."""
        if policy == "never":
            return False
        if policy == "always":
            return True
        return interactive  # policy == "interactive"

    def _turn_checkpoint(self) -> "CheckpointService | None":
        """The shadow-git service for the directory the running turn works in.

        Keyed on the bound directory rather than on a session key: ``run_turn``
        already resolved and validated it once for the whole turn, and
        re-resolving here would both repeat the sandbox-mount check and race a
        workdir override changed mid-turn (the web UI can rewrite it).

        Nothing bound means no turn is running, so there is nothing to
        snapshot and the answer is ``None``. Deriving a directory here instead
        would reintroduce both hazards the binding exists to avoid -- a
        mount-check refusal raised at end of turn, and an empty session key
        materializing the ``ws/_`` fallback directory.

        One service per directory: CheckpointService puts its git dir inside
        the directory it protects, so sharing one across working directories
        would cross-contaminate their edited-file sets.
        """
        target = workdir.current()
        if target is None or not self._checkpoint_enabled:
            return None
        if target not in self._checkpoints:
            from raven.agent.loop.checkpoint import CheckpointService

            try:
                self._checkpoints[target] = CheckpointService(
                    target,
                    shadow_dir=self.runtime_config.checkpoint.shadow_dir,
                )
            except ValueError as exc:
                logger.warning("runtime.checkpoint disabled for {} -- {}", target, exc)
                self._checkpoints[target] = None
        return self._checkpoints[target]

    def _stash_recovery(self, session_key: str, outcome: "LoopOutcome") -> None:
        """Remember an interrupted turn's snapshot so the next turn in this
        session gets a recovery prompt. No-op unless checkpoint is enabled
        and the turn was actually interrupted with something to recover.

        Status filter is intentional: only ``"interrupted"`` triggers a
        recovery prompt. ``"error"`` turns still get a per-turn shadow
        commit (useful for audit), but they don't usually have a partial-
        edits trajectory to resume (provider 400 etc.) and surfacing
        "Files modified last turn" for them would be misleading.
        """
        if self._turn_checkpoint() is None or outcome.status != "interrupted":
            return
        if outcome.edited_files or outcome.checkpoint_id:
            self._pending_recovery[session_key] = {
                "checkpoint_id": outcome.checkpoint_id,
                "files": outcome.edited_files,
            }

    def _inject_recovery_block(self, session_key: str, messages: list[dict]) -> None:
        """Prepend a recovery notice to the current user message when the
        previous turn for this session was interrupted. Consumed once on
        successful injection; if the current message's content has an
        unexpected shape (None / dict / etc.) the pending entry is kept so
        a later assembly with a normal content can still inject it."""
        recovery = self._pending_recovery.get(session_key)
        if not recovery or not messages:
            return
        last = messages[-1]
        if last.get("role") != "user":
            # Last message isn't the user turn — keep the recovery pending so
            # the next assembly (which does end with the user message) injects it.
            return
        content = last.get("content")
        files = recovery.get("files") or []
        cid = recovery.get("checkpoint_id")
        lines = ["[Recovery — the previous turn was interrupted before finishing]"]
        if files:
            lines.append("Files modified last turn: " + ", ".join(files))
        if cid:
            lines.append(f"Checkpoint: {cid}")
        lines.append("Verify the current state of these files before continuing.")
        block = "\n".join(lines)
        # Mutate first, pop second — atomic from the caller's perspective. If
        # we can't safely write to ``content`` (unknown shape) the recovery
        # stays pending instead of being silently dropped on the floor.
        if isinstance(content, str):
            last["content"] = f"{block}\n\n{content}"
        elif isinstance(content, list):
            last["content"] = [{"type": "text", "text": block}] + content
        else:
            return  # unexpected content shape → keep pending
        self._pending_recovery.pop(session_key, None)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content.

        Paired blocks are removed. What is left is then checked for being
        nothing but tag debris: when a backend inlines its reasoning and the
        turn is cut off inside it, content arrives as a lone closing tag with
        no opener to pair against, so the substitution above finds nothing and
        an eleven-character string reads as a real answer. Recovery is skipped
        and the tag is what the user sees.

        The check is on residue, not on vendor spellings -- it does not matter
        which prefix a backend picked. Text that merely mentions a tag keeps
        its other words and is returned untouched.
        """
        if not text:
            return None
        cleaned = strip_think_blocks(text)
        if is_only_think_debris(cleaned):
            return None
        return cleaned or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""

        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'

        return ", ".join(_fmt(tc) for tc in tool_calls)

    @staticmethod
    def _build_usage_snapshot(response, model: str, session_key: str) -> "UsageSnapshot":
        """Build the same reported usage that tracing consumes."""
        from raven.contracts.token_strategy import UsageSnapshot
        from raven.providers.usage import normalize_usage

        usage = normalize_usage(response.usage)
        usage.pop("total_tokens")
        return UsageSnapshot(model=model, session_key=session_key or None, **usage)

    @trace.instrument("llm.call", extract=semconv.llm_call_stream)
    async def _llm_call_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        model: str | None,
        on_token_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
        **generation: Any,
    ) -> LLMResponse:
        """Stream LLM response via ``provider.chat_stream`` + accumulate to LLMResponse.

        When a turn caller wires ``on_token_delta``, AgentLoop diverts here
        instead of to ``chat_with_retry``. The work itself lives in
        ``raven.providers.streaming`` — a sub-agent backend answering a direct
        chat drives the same call and must not drift from it. This method stays
        as the loop's own entry point (its span, its reconnect budget).

        Generation parameters travel only as the turn's ``gen_overrides``: the
        session's pinned reasoning effort under a hook's rollback override.
        With none, ``chat_stream``'s own signature defaults stand, as they
        always have. See ``generation_kwargs`` for the callers that build their
        own.
        """
        limits = self._recovery_limits
        return await stream_llm_call(
            self.provider,
            messages=messages,
            tools=tools,
            model=model,
            on_token_delta=on_token_delta,
            on_reasoning_delta=on_reasoning_delta,
            max_reconnects=self._MAX_STREAM_RECONNECTS,
            retry_delays=tuple(limits.llm_error_retry_delays),
            retry_after_output=bool(limits.llm_retry_after_output),
            **generation,
        )

    async def _synthesize_final_on_exhaustion(
        self,
        messages: list[dict],
        model: str | None,
        fallback_models: list[str] | None,
        on_token_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
        reasoning_effort: str | None = None,
        prompt: str = _MAX_ITER_SYNTHESIS_PROMPT,
        fallback: str | None = None,
    ) -> str:
        """One tools-disabled LLM call to wrap up a turn that has to stop early.

        Instead of returning a canned apology, ask the model to summarize what
        it accomplished and deliver its best partial answer. Tools are withheld
        (``tools=None``) so it cannot start another tool call — or an
        ``ask_user`` — at the cliff edge. Falls back to a static message if the
        call errors or comes back empty, so the turn is never left silent.

        When the turn caller wired streaming callbacks, this synthesized reply
        must stream too — otherwise it never reaches a streaming outlet: the
        run_turn boundary only emits a closing ``Text`` when nothing streamed,
        so a non-streamed wrap-up after an already-streamed turn gets dropped.

        ``prompt`` and ``fallback`` are the caller's because the reason differs
        and the reason is model-visible: a turn stopped on a repeating call has
        not used up its budget, and told that it had, it summarizes the wrong
        thing.
        """
        synth_messages = messages + [{"role": "user", "content": prompt}]
        # The wrap-up is a model call of the same turn, so it pays the turn's
        # effort; absent, the provider's configured default stands.
        effort_kwargs: dict[str, str] = {} if reasoning_effort is None else {"reasoning_effort": reasoning_effort}
        try:
            if on_token_delta is not None or on_reasoning_delta is not None:
                response = await self._llm_call_stream(
                    messages=synth_messages,
                    tools=None,
                    model=model,
                    on_token_delta=on_token_delta,
                    on_reasoning_delta=on_reasoning_delta,
                    **effort_kwargs,
                )
            else:
                response = await self.provider.chat_with_retry(
                    messages=synth_messages,
                    tools=None,
                    model=model,
                    fallback_models=fallback_models,
                    **effort_kwargs,
                )
            text = self._strip_think(response.content)
            if response.finish_reason != "error" and text:
                return text
            logger.warning(
                "Early-exit synthesis returned no usable content (finish_reason={})",
                response.finish_reason,
            )
        except Exception as exc:
            logger.warning("Early-exit synthesis call failed: {}", exc)
        if fallback is None:
            fallback = _MAX_ITER_STATIC_FALLBACK.format(n=self.max_iterations)
        # The streamed-success path already delivered its text through
        # ``on_token_delta``; this fallback did not. Push it through the stream
        # too, or the run_turn boundary — which suppresses the closing ``Text``
        # once anything has streamed — would drop it on a streaming outlet.
        if on_token_delta is not None:
            await on_token_delta(fallback)
        return fallback

    def _flush_autofill(self, messages: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
        """Write what raven answered for the user into the turn, as a tool call.

        A live row only reaches the screen; a reloaded transcript is rebuilt
        from the persisted ``tool_calls`` and their results, so without this the
        account of what raven decided is gone by the next session.

        Not written where the decision was made. A sub-agent's question arrives
        while its spawn tool is still executing -- after the assistant message
        carrying ``tool_calls`` is in the list and before its ``tool`` results
        are -- and a message spliced into that window breaks the pairing
        documented at the ``add_assistant_message`` call site below, which Chat
        Completions rejects with a 400. Here the loop owns the list on its own
        task with every result already in, which is why ``drain`` merges its
        injected messages at the same point.

        Only host-minted fields go into the arguments. The sub-agent's own
        wording would arrive here unfenced -- ``add_tool_result`` wraps the
        summary below, nothing wraps an assistant message's ``tool_calls`` --
        and that message is read by the next model call and by the next
        autofill's snapshot. The summary names every question regardless.
        """
        for row in rows:
            call_id = f"autofill-{uuid4().hex[:8]}"
            self.context.add_assistant_message(
                messages,
                None,
                [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": autofill_resolver.TOOL_NAME,
                            "arguments": json.dumps(
                                {"agent": row.get("agent", ""), "instance": row.get("instance", "")},
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            )
            self.context.add_tool_result(messages, call_id, autofill_resolver.TOOL_NAME, row.get("summary", ""))

    async def _run_agent_loop(  # noqa: C901 (cc 100: pre-existing, above the ceiling)
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        session_key: str | None = None,
        model: str | None = None,
        fallback_models: list[str] | None = None,
        injected_skill_ids: list[str] | None = None,
        on_token_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_event: Callable[[str, dict], Awaitable[None]] | None = None,
        on_episode_start: Callable[[int], Awaitable[None]] | None = None,
        on_notice: Callable[[NoticeKind, str], Awaitable[None]] | None = None,
        usage_sink: dict[str, Any] | None = None,
        drain: Drain | None = None,
        hook_metadata: dict[str, Any] | None = None,
        session_history: list[dict[str, Any]] | None = None,
        origin: "Origin | None" = None,
        turn_started_at: float | None = None,
        attempt: int = 1,
        rerun_pending: "Callable[[str | None, list[dict], str], bool] | None" = None,
        spend: TurnSpend | None = None,
    ) -> tuple[str | None, list[str], list[dict], LoopOutcome]:
        """Run the agent iteration loop.

        ``origin`` is the turn's: the watch-work judgement reads the owner's
        request off the messages, and a turn the runtime re-injected has none
        to read (``watch_work.asked_for``).

        ``drain``, when wired, is called at the top of each iteration to pull
        any user messages injected mid-turn (BusyPolicy.INJECT) and merge them
        as user turns before the next LLM call.

        ``session_key`` is the turn's real session key. It labels the
        checkpoint commit only; usage attribution deliberately carries an
        empty session key, since widening it would start filling per-session
        cost buckets that have always been empty.

        ``session_history`` is the filed record of the session this turn
        persists into -- still ending with the previous turn -- stamped once
        onto the iteration hook context. The caller owns which record that is.

        ``turn_started_at`` is when the caller's turn began, on the clock
        :func:`monotonic` reads. A caller that may run this loop more than once
        for one user turn passes it, so that the wall-clock budget and the
        elapsed time recorded at the end both measure the turn rather than the
        attempt; omitted, the attempt is the turn and the clock starts here.

        ``attempt`` is which run of the loop this is, counting from 1. It rides
        the turn's end record so a reader can tell the scope of the count beside
        it -- the iterations there are this attempt's, while the elapsed time is
        the turn's. Nothing in the loop branches on it.

        ``rerun_pending`` lets a caller that budgets reruns say this attempt is
        about to be run again, given ``(final_content, messages, status)``. It is
        asked only when the turn ended with nothing to show, and a yes means the
        terminal seam is skipped: the answer a gate would manufacture there is
        one the rerun discards, and the minutes spent making it come out of the
        turn's own clock. Omitted, the seam fires for every answerless turn,
        which is what every agent that budgets no rerun sees.

        ``spend`` is the turn's cost, which outlives this loop the way the
        clock above does: a rerun is a second attempt at one turn and bills
        into the same scope. A caller that opened no scope gets one for this
        loop alone, which bills its own calls and takes no delegated ones.
        """
        spend = spend if spend is not None else TurnSpend(session_key)
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        effective_model = model or self.model

        # Track whether the turn was a normal exit or a max-iter interruption.
        # ``status`` labels the shadow-git commit and, with ``error_detail`` --
        # the loop's own words for a model call it gave up on -- stamps the
        # ``LoopOutcome`` the caller fails the turn on. Both the provider-error
        # exit and the exhausted empty-response exit set the pair: a turn that
        # ran out of ways to get a word back has no answer either, and it is the
        # caller that knows whether a tool has already delivered one.
        status = "completed"
        error_detail: str | None = None

        # The turn's window bookkeeping: the last billed context size, the retry
        # budgets every shrink draws on, and the picture window. Held here and
        # handed to the Memory role each time it is asked to make the window
        # smaller, because that role outlives the turn. A budget of 0 in the
        # settings means no standing image pass: ``None`` here, and the window
        # only starts to act once a refusal has closed it a notch.
        window = WindowState(
            image_window=shrink.IMAGE_WINDOW_RECENT_MESSAGES,
            image_budget=self._recovery_limits.image_window_budget_bytes or None,
        )
        # Retryable model errors that outlasted the provider's own ladder: how many
        # of the loop's longer waits this turn has spent.
        error_waits = 0
        # The loop's own pre-request awaits, bounded: a provider timeout cannot
        # fire before the call is entered, and 2026-09-10 stalled before that.
        first_call = FirstCallGuard(
            first_byte_budget(getattr(self.provider, "generation", None)),
            model=effective_model,
        )
        # Tool-failure-loop break: track consecutive hard failures of the
        # same tool *with the same kind of error* across iterations; nudge once
        # per fresh streak, bounded/turn.
        loop_fail_key: tuple[str, str] | None = None
        loop_fail_streak = 0
        loop_nudges = 0
        # No-progress ladder: nudge, then refuse the call, then end the turn.
        # Owns its own per-turn counters; see ``no_progress.NoProgressGuard``.
        no_progress = NoProgressGuard(
            nudge_at=self._NO_PROGRESS_THRESHOLD,
            refuse_at=self._NO_PROGRESS_REFUSE,
            max_nudges=self._NO_PROGRESS_MAX,
            max_refusals=self._NO_PROGRESS_REFUSALS_MAX,
        )
        # Set to the tool's name when the ladder's last step fires, which ends
        # the turn the way an exhausted iteration budget does.
        stalled_tool: str | None = None
        # What this turn has written, so a later call that removes one of those
        # files is seen. Per turn for the reason the counters above are: the loop
        # is a singleton and another session's turn is running beside this one.
        removal_watch = RemovalWatch()
        # Empty-response recovery state, local to the turn — the AgentLoop is a
        # long-lived singleton shared across sessions, so per-instance counters
        # would leak across turns; resetting here gives clean per-turn budgets.
        prev_had_tool_calls = False
        post_tool_nudges = 0
        prefill_retries = 0
        # Set when a prefill re-feeds reasoning the ceiling cut: the next call
        # continues mid-thought, and its opening fragment is not content.
        cut_continuation = False
        empty_retries = 0
        # Sticky across a turn's empty retries: once the descent reaches a rung,
        # a later retry must not read the turn's own effort again and hand back
        # the value that already came up empty.
        empty_retry_effort: str | None = None
        # Said once per turn, not once per retry: three identical copies
        # of it in one transcript are noise, and the escalation for advice
        # that did not work is `loop_break_nudge`, not repetition.
        output_limit_told = False
        # The watch-work judgement's state, owned by this turn: the loop is a
        # singleton and turns from other sessions run concurrently, so anything
        # on `self` here would let one session's dispatch silence another's
        # nudge and one session's verdict answer for another's request.
        from raven.agent.subagent import watch_work as _watch_work

        watch_state = _watch_work.TurnWatch()
        watch_request = _watch_work.asked_for(initial_messages, origin=origin)

        # Read once, here: a mode switched mid-turn lands on the next turn.
        policy = self.session_policy(session_key or "")
        iteration_cap = policy.max_iterations or self.max_iterations
        # The other bound on this turn, and the only one a product supplies rather
        # than the loop: whatever a hook left under ``TURN_BUDGETS_KEY``. Read here
        # for the same reason the cap is, and defaulting to unbounded, so an agent
        # that writes nothing runs exactly as it did before this existed.
        budgets = turn_budgets(hook_metadata)
        # The caller's clock when it has one: a turn that may be re-run hands the
        # same start to every attempt, so one budget covers the turn instead of
        # each attempt receiving a fresh copy of it.
        turn_t0 = turn_started_at if turn_started_at is not None else monotonic()
        stopped_by: str | None = None
        # The session's effort rides every model call of the turn as an explicit
        # argument. Omitted, not None, when the policy names none: an explicit
        # None would override the provider's sentinel and switch its configured
        # default off.

        # The iteration hook chain's context for this whole turn; None when no
        # hook is registered, so a default install pays nothing here.
        from raven.agent.hook import AgentHookContext

        # One metadata dict serves the whole turn: the entrance seeds it at the
        # inbound phase and reads it after the send, so what a hook stashes in
        # one phase group is still there in the next. The mode keys are the
        # loop's own and overwrite whatever a caller seeded under those names.
        turn_meta = hook_metadata if hook_metadata is not None else {}
        turn_meta["mode"] = policy.mode
        turn_meta["mode_overlay"] = dict(policy.mode_overlay)
        hook_ctx = (
            AgentHookContext(
                session_key=session_key or "",
                turn_question=turn_question(initial_messages),
                turn_base=max(0, len(initial_messages) - 1),
                session_history=session_history,
                max_iterations=iteration_cap,
                context_window_tokens=self.context_window_tokens,
                metadata=turn_meta,
            )
            if len(self.hooks) > 0
            else None
        )
        hook_rollbacks = 0
        # Whether anything installed can send a response back. Only then are the
        # deltas worth holding: holding coalesces the reply into one delta, which
        # costs the reader the answer typing out, and a hook that never rolls
        # back would pay that for nothing. Read off the hook's own declaration
        # rather than from the override: overriding the phase only says a hook
        # watches it, and eval_engine's judge overrides it, awaits an LLM call
        # inside it, and promises never to interrupt the reply.
        holds_drafts = hook_ctx is not None and any(
            getattr(hook, "rolls_back_iterations", False) for hook in self.hooks
        )
        iter_msg_base = 0
        pending_gen_overrides: dict[str, Any] | None = None

        def _land_hook_note(decision) -> None:
            """Honor a hook's ``append_note``: the note joins the last message
            the model is about to read. A body shape the loop does not know
            drops the note with a warning rather than guessing it into place."""
            if decision.append_note and not append_hook_note(messages, decision.append_note):
                logger.warning("Hook note dropped: no transcript message to land it on")

        def _hook_rollback(decision) -> bool:
            """Honor a hook's rollback: pop everything this iteration appended
            and re-sample without consuming an iteration. Message helpers
            mutate-and-return the same list, so truncating at the iteration
            watermark removes exactly this iteration's products."""
            nonlocal iteration, hook_rollbacks, pending_gen_overrides
            if not decision.rollback:
                return False
            if hook_rollbacks >= self._MAX_HOOK_ROLLBACKS:
                if hook_ctx is not None:
                    hook_ctx.metadata["rollbacks_refused"] = hook_ctx.metadata.get("rollbacks_refused", 0) + 1
                logger.warning("Hook rollback cap ({}) reached; proceeding without rollback", self._MAX_HOOK_ROLLBACKS)
                return False
            requested = decision.rollback_overrides or {}
            overrides = {k: v for k, v in requested.items() if k in self._ROLLBACK_OVERRIDE_KEYS}
            if len(overrides) != len(requested):
                logger.warning(
                    "Hook rollback overrides dropped (not in allowlist): {}", sorted(set(requested) - set(overrides))
                )
            hook_rollbacks += 1
            if hook_ctx is not None:
                # The honoured count beside the refused one: a gate scoped to the
                # turn boundary (ask_user) needs to know the iteration number it
                # sees is a re-sample, and only the loop knows that.
                hook_ctx.metadata["hook_rollbacks"] = hook_rollbacks
            del messages[iter_msg_base:]
            for m in decision.rollback_inject or ():
                entry = dict(m)
                entry.setdefault("timestamp", self._now_fn().isoformat())
                entry[_HOOK_INJECTED_KEY] = True
                messages.append(entry)
            iteration -= 1
            pending_gen_overrides = overrides or None
            logger.info(
                "Hook rollback {}/{}: popped iteration messages, re-sampling (overrides={})",
                hook_rollbacks,
                self._MAX_HOOK_ROLLBACKS,
                sorted(overrides) or None,
            )
            return True

        while iteration < iteration_cap:
            # Checked here and not mid-generation: cancelling a call in flight
            # discards a finished generation and leaves no answer, and a deadline
            # landing between a tool result and the model reading it produces a
            # trajectory nothing can interpret. The cost is an overrun of at most
            # one iteration, which the budget's own docstring states.
            if budgets.wall_clock_seconds is not None and (monotonic() - turn_t0) >= budgets.wall_clock_seconds:
                stopped_by = "wall_clock"
                logger.warning(
                    "Wall-clock budget {}s reached after {} iterations; wrapping up",
                    budgets.wall_clock_seconds,
                    iteration,
                )
                break
            iteration += 1
            logger.info(
                "Iteration {}/{} model={}",
                iteration,
                iteration_cap,
                effective_model,
            )

            # Mark the episode boundary (one per model call) so an outlet can
            # group this call's reasoning + text + tools into a single step.
            if on_episode_start is not None:
                # A bounded per-outlet queue with a wedged worker behind it parks
                # here forever; the boundary is a grouping hint, so losing one is
                # a step drawn wrong, not a turn lost.
                await first_call.stage(
                    "the episode-boundary emit",
                    on_episode_start(iteration - 1),
                    iteration=iteration,
                    fallback=None,
                )

            # Take any INJECT-ed user messages (BusyPolicy.INJECT) into this
            # iteration before its LLM call. Media-carrying injects keep their
            # file paths in the text so nothing is silently dropped. They are
            # appended one per message here and labelled for the provider at the
            # call seam below, so every reader of ``messages`` sees the shape
            # they arrived in.
            if drain is not None:
                for inj in drain():
                    inj_text = inj.text or ""
                    # Only the paths the message does not already name. A send
                    # from the page bakes its own attachment note into the text
                    # and derives ``media`` from it, so naming them again put a
                    # second, raw copy of the path into the reader's own bubble
                    # once the stored entry was drawn.
                    inj_paths = [m.path for m in inj.media if m.path not in inj_text]
                    if inj_paths:
                        prefix = inj_text + "\n" if inj_text else ""
                        inj_text = f"{prefix}[injected message; attached files: {', '.join(inj_paths)}]"
                    if inj_text:
                        # Marked because the queue has now given this up: it was
                        # delivered once, to a turn that may yet be thrown away and
                        # run again. The mark is what lets the rerun carry it.
                        messages.append(
                            {
                                "role": "user",
                                "content": inj_text,
                                # When it arrived, not when the turn happened to
                                # reach a gap: the wait is the whole point of an
                                # inject, and a long turn is where they are sent.
                                "timestamp": inj.received_at or self._now_fn().isoformat(),
                                _MID_TURN_USER_KEY: True,
                            }
                        )
                        logger.info("inject: merged a mid-turn user message")

            # The two window passes, before the snapshot and the hooks below so
            # every reader of ``messages`` this iteration sees the list the model
            # will. The Memory role decides whether the transcript is near its
            # line and what to give up; this loop rebinds what it hands back.
            # Compaction rebinds ``messages``, which is why it sits above the
            # autofill publish: a snapshot published before the rebind would go
            # quietly stale.
            ahead = await self.harness.memory.shrink(
                messages, pressure=WindowPressure.PROACTIVE, state=window, model=effective_model
            )
            messages = ahead.messages
            # The standing image window: the notes it writes are the transcript
            # from here on rather than a per-request rewrite.
            standing = await self.harness.memory.shrink(
                messages, pressure=WindowPressure.STANDING, state=window, model=effective_model
            )
            messages = standing.messages

            # Same seam and the same reason as the drain above: only the loop's
            # own task may touch ``messages``, and only here is every tool
            # result already in. Reading the turn's autofill is safe at this
            # point -- unlike at question time, where the ACP connection pool
            # carries the wrong turn's ContextVars -- because ``_run_agent_loop``
            # runs below the ``start_ask_turn`` binding in ``RpcTurnRunner.run``.
            auto = current_autofill()
            if auto is not None:
                # Flush before publishing, so a sub-agent asking a second time
                # this turn sees what raven already answered for the first.
                self._flush_autofill(messages, auto.pending_rows())
                # Every iteration, not once: the overflow and image-demotion
                # recoveries below rebind ``messages`` to a fresh list, so the
                # object published last time can stop being the one the turn is
                # building, and a snapshot of it would go quietly stale.
                auto.set_snapshot(messages)

            capability = await self.harness.capability.select(CapabilityRequest(messages=messages, iteration=iteration))
            tool_defs = capability.tools
            iter_msg_base = len(messages)

            if hook_ctx is not None:
                hook_ctx.iteration = iteration
                hook_ctx.messages = messages
                hook_ctx.tools = tool_defs
                hook_ctx.response = None
                # CompositeHook survives a hook that raises and has nothing to
                # say about one that never returns. A neutral decision is what
                # "no hook ran" already means everywhere else here.
                decision = await first_call.stage(
                    "the before_iteration hooks",
                    self.hooks.before_iteration(hook_ctx),
                    iteration=iteration,
                    fallback=HookDecision(),
                )
                if decision.short_circuit_result is not None:
                    final_content = str(decision.short_circuit_result)
                    messages = self.context.add_assistant_message(messages, final_content)
                    break
                _land_hook_note(decision)
                # Taken from the decision, not from ``hook_ctx.tools``: the two
                # happen to be the same object today, and relying on that would
                # make the rule stop firing silently the day get_definitions
                # returns a cached list. The registry itself is never touched.
                if decision.modified_tools is not None:
                    tool_defs = decision.modified_tools

            # Nothing to switch off here any more. When CacheOptimizer runs it
            # stamps the request it marked, and the provider reads that stamp on
            # the way out, so the two cannot both place breakpoints on the same
            # request -- including on a pool-built provider a session switched
            # onto mid-conversation, which is what this block was added for.
            #
            # The switch it used to set lived on the provider *object* and was
            # never unset, so every later consumer of that object -- the Curator,
            # a subagent, Sentinel, the session titler -- kept sending with no
            # breakpoints at all. That was named as known residue when this block
            # landed; asking the request instead is what retires it.

            # TokenWise before-hook: strategies may rewrite messages, tools,
            # or model (e.g. CacheOptimizer marks cache_control blocks).
            # The session's pinned effort first, a hook's rollback override on
            # top: a mode that asks for more thinking sets the turn's default,
            # and a gate re-sampling one call may still move that one call.
            gen_overrides = {
                # The configured default underneath, the session's pin over it,
                # a hook's rollback override on top. The default is here rather
                # than left to the provider because the provider's copy is
                # frozen at construction (see ``default_reasoning_effort``).
                **({"reasoning_effort": self.default_reasoning_effort} if self.default_reasoning_effort else {}),
                **({"reasoning_effort": policy.reasoning_effort} if policy.reasoning_effort else {}),
                **(pending_gen_overrides or {}),
            }
            pending_gen_overrides = None
            call_reasoning_effort = gen_overrides.get("reasoning_effort")
            call_messages, call_tools, call_model = await first_call.stage(
                "the before_llm_call strategies",
                self.strategies.before_llm_call(
                    messages,
                    tool_defs,
                    effective_model,
                ),
                iteration=iteration,
                fallback=(messages, tool_defs, effective_model),
            )
            # Last, and on the payload only: the strategies above decide against
            # the shape the messages arrived in, and the prompt-cache prefix
            # stays stable because the same arrivals always fold the same way.
            call_messages = merge_mid_turn(call_messages)
            # A hook can send this whole response back, and a rollback pops the
            # history the stream has already left -- so where hooks are installed
            # the deltas are held until something keeps the response. The draft
            # gate owns the continuation head cut in that case: holding the whole
            # text cuts it exactly, so the two gates never stack.
            draft = (
                DraftGate(on_token_delta, cut_head=cut_continuation)
                if on_token_delta is not None and holds_drafts
                else None
            )
            gate = (
                ContinuationGate(on_token_delta)
                if draft is None and cut_continuation and on_token_delta is not None
                else None
            )
            # Every provider call ``decide`` makes reaches the provider seam, and
            # an Action may make more than one (best-of-n, a critic pass: see
            # ActionRequest), so none of them is claimed here. The session and
            # the sink are both bound so the seam bills each call to this turn's
            # conversation and to this loop's own registry -- a caller entering
            # below ``run_turn`` has bound neither for it, and the sink is bound
            # per turn because a candidate generation is built while the one it
            # replaces may still be serving.
            with (
                usage_record.bind(self.strategies),
                usage_context.bind(session_key) if session_key else contextlib.nullcontext(),
            ):
                response = await self.harness.action.decide(
                    ActionRequest(
                        provider=self.provider,
                        messages=call_messages,
                        tools=call_tools,
                        model=call_model,
                        fallback_models=fallback_models,
                        stream_call=self._llm_call_stream,
                        # Spliced here rather than inside the module: both gates are
                        # the shell's own, and which sink a delta reaches is not a
                        # strategy decision. The module reads the field it is handed,
                        # so the stream/retry branch stays exactly the one the loop
                        # took -- neither gate is built unless ``on_token_delta``
                        # already is, so the spliced value is None on exactly the
                        # turns the raw sink was, and a reasoning sink alone still
                        # streams.
                        on_token_delta=draft or gate or on_token_delta,
                        on_reasoning_delta=on_reasoning_delta,
                        generation_overrides=gen_overrides,
                    )
                )
                # Read inside the bind: the count belongs to it and ends with it.
                recorded_at_seam = usage_record.recorded_inbound()
            # Assigned, not latched: the fact this carries is that the turn's
            # own last word was cut, so a call that recovers clears it. Latching
            # would report a cut to a reader whose question is what the turn
            # delivered, and an earlier iteration that was cut and then answered
            # in full delivered it. Read off `response.truncated` rather than
            # `finish_reason`, because an upstream can answer a ceiling hit with
            # a success claim instead.
            if hook_metadata is not None:
                hook_metadata["output_limited"] = bool(getattr(response, "truncated", False))
            if cut_continuation:
                cut_continuation = False
                if gate is not None:
                    await gate.finish()
                response.content = cut_reasoning_head(response.content)
            if draft is not None and response.tool_calls:
                # A preamble beside tool calls is not the answer a hook holds back
                # as final, and holding it would park it for the whole tool run.
                await draft.release()
            _log_what_the_model_said(response)
            # TokenWise after-hook: strategies observe the response for
            # usage tracking, budget enforcement, etc. Errors are swallowed.
            usage_snapshot = self._build_usage_snapshot(response, call_model, session_key or "")
            # A call behind this response was recorded at the seam and this row
            # would be a second copy of one of them; with no sink bound (a test,
            # an embedder) nothing was recorded and this row is the only one.
            if not recorded_at_seam:
                await self.strategies.after_llm_call(
                    {
                        "content": response.content,
                        "finish_reason": response.finish_reason,
                        "usage": response.usage,
                    },
                    usage_snapshot,
                )
            spend.note(usage_snapshot.cost_usd)
            # The stream caller (turn.* handler) may want the
            # final-iteration usage to populate `message.complete.payload.usage`
            # on the wire. Use the wire-contract TurnUsage
            # fields (prompt_tokens / completion_tokens / total_tokens) — not
            # the agent-internal snapshot with model / cache / cost fields.
            if response.usage:
                prompt_tokens = int(response.usage.get("prompt_tokens", 0) or 0)
                completion_tokens = int(response.usage.get("completion_tokens", 0) or 0)
                # One reading for both consumers: the sink gauge below and the
                # proactive compaction trigger at the top of the next
                # iteration. They differ in their thresholds, not in what
                # they measure -- and what they measure is how full the window
                # is, which is not what the prompt was billed as. A provider
                # that reports cache reads apart from ``prompt_tokens``
                # (``prompt_tokens_include_cache`` false, the Anthropic
                # Messages shape) bills a fraction of a warm prompt while all
                # of it still occupies the window, so the cached counts are
                # added back here. Same add-back, for the same reason, as
                # ``transport_failure.never_processed``.
                context_used = prompt_tokens + completion_tokens
                if not response.usage.get("prompt_tokens_include_cache", True):
                    context_used += int(response.usage.get("cache_read_input_tokens", 0) or 0) + int(
                        response.usage.get("cache_creation_input_tokens", 0) or 0
                    )
                if context_used > 0:
                    window.last_context_used = context_used
            if usage_sink is not None and response.usage:
                # An explicitly configured window always wins over the live
                # table -- that is what setting it means. Otherwise the live
                # window from the model's provider table (e.g. OpenRouter,
                # when LiteLLM lags) answers instead; unknown to that table
                # too, 0 tells the UI to show its empty state rather than a
                # number that isn't this model's.
                from raven.providers.binding import active_binding

                configured = (active_binding() or self._default_binding).configured_window
                if configured:
                    context_max = configured
                else:
                    # Off the event loop: allow_fetch=True here can hit the
                    # network for up to 10s on an OpenRouter model with both
                    # caches expired. See rates._fetch_openrouter_models.
                    context_max = await asyncio.to_thread(resolve_context_window, call_model) or 0
                usage_sink.clear()
                usage_sink["prompt_tokens"] = prompt_tokens
                usage_sink["completion_tokens"] = completion_tokens
                usage_sink["total_tokens"] = int(response.usage.get("total_tokens", 0) or 0)
                # The counts above are this call's: what they feed is a gauge --
                # how full the window is now. The cost is the turn's: it is a
                # flow, so it sums every iteration this turn ran and every call
                # its delegations made while it ran. Billing the final call
                # alone reported a fifth of what a delegating turn spent.
                usage_sink["cost_usd"] = spend.cost_usd
                usage_sink["cost_missing_calls"] = spend.cost_missing_calls
                usage_sink["context_max"] = context_max
                usage_sink["context_used"] = context_used
                usage_sink["context_percent"] = round(100 * context_used / context_max) if context_max else 0

            # Recoveries that retry this iteration. The classifier on the response
            # says what kind of refusal this was; the Memory role says whether it
            # has a move left for it and makes it. ``continue`` re-enters the
            # iteration at the top, so the hooks and the standing passes see the
            # retry as they saw the attempt, and ``iteration`` is not billed for
            # a call that did no work.
            cls_ = response.error_classification
            refused = response.finish_reason == "error" and cls_ is not None
            retry_model = call_model or effective_model
            if refused and cls_.should_compress:
                shrunk = await self.harness.memory.shrink(
                    messages, pressure=WindowPressure.OVERFLOW, state=window, model=retry_model
                )
                messages = shrunk.messages
                if shrunk.changed:
                    iteration -= 1
                    continue
            if refused and cls_.should_drop_tool_images:
                demoted = await self.harness.memory.shrink(
                    messages, pressure=WindowPressure.TOOL_IMAGES_REFUSED, state=window, model=retry_model
                )
                messages = demoted.messages
                if demoted.changed:
                    # Cached so the rest of the process stops paying for the
                    # attempt: the static table in ``capabilities`` guessed wrong
                    # about this endpoint, and this is how it self-corrects.
                    self._image_tool_result_ok[retry_model] = False
                    iteration -= 1
                    continue
            if refused and cls_.strip_images:
                withdrawn = await self.harness.memory.shrink(
                    messages, pressure=WindowPressure.IMAGES_TOO_LARGE, state=window, model=retry_model
                )
                messages = withdrawn.messages
                if withdrawn.changed:
                    iteration -= 1
                    continue

            if response.has_tool_calls:
                if hook_ctx is not None:
                    hook_ctx.messages = messages
                    hook_ctx.response = response
                    decision = await self.hooks.before_execute_tools(hook_ctx)
                    if _hook_rollback(decision):
                        continue
                    _land_hook_note(decision)
                    if decision.short_circuit_result is not None:
                        # The tool-call response is dropped entirely: persisting
                        # an assistant message whose tool_calls never executed
                        # leaves dangling calls that strict providers reject.
                        final_content = str(decision.short_circuit_result)
                        messages = self.context.add_assistant_message(messages, final_content)
                        break
                # Blocking a call is handled where the call is: the
                # branch below refuses it and cancels the siblings the
                # model wrote beside it. The only answer that has to
                # leave that branch is the one about the turn, and this
                # is it. Every refusal asks for ABORT_TURN today; the
                # point of naming it separately is that the one which
                # should not can say so without touching this loop.
                continuation = Continuation.CONTINUE
                abort_reason = ""
                if on_progress:
                    thought = self._strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

                # Images this transport cannot carry inside a tool result.
                # Collected across the whole batch and attached once *after* the
                # last tool result: a user message sitting between two tool
                # results leaves an assistant tool_call unanswered at the point
                # the API validates the sequence. Measured on gpt-4o, 2026-07-31,
                # two tool calls with the picture from the first:
                #   tool(c1), user, tool(c2) -> 400 "An assistant message with
                #     'tool_calls' must be followed by tool messages responding
                #     to each 'tool_call_id'"
                #   tool(c1), tool(c2), user -> 200, and the model named the
                #     image's colour
                # Anthropic accepts both, so this only bites on Chat Completions
                # -- which is the only transport that takes this path at all.
                pending_images: list[dict[str, Any]] = []
                pending_sources: list[dict[str, Any]] = []
                tool_call_dicts = [openai_tool_call(tc) for tc in response.tool_calls]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                _stamp_reasoning_ms(messages, response)

                for tool_call_index, tool_call in enumerate(response.tool_calls):
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    # Skip the message tool: turn.py emits its tool.complete; a
                    # second emit here would double it.
                    emit_tool_event = on_tool_event is not None and tool_call.name != "message"
                    if emit_tool_event:
                        _tool = self.tools.get(tool_call.name)
                        await on_tool_event(
                            "start",
                            {
                                "tool_call_id": tool_call.id,
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                                "blocking": self.tools.is_blocking(tool_call.name, tool_call.arguments),
                                # Tool-authored call label; None -> UI derives one.
                                "display": _display_label(_tool, tool_call.arguments),
                            },
                        )
                    # A tool whose output also reaches the UI on a side channel
                    # (exec's inline diff, run_subagent_dag's progress events)
                    # needs this call's id to correlate with the row the UI drew.
                    if (setter := getattr(self.tools.get(tool_call.name), "set_tool_call_id", None)) is not None:
                        setter(tool_call.id)
                    set_current_tool_call_id(tool_call.id)
                    tool_t0 = time.monotonic()
                    # The working directory either side of a command, set by the
                    # branch below that actually runs one. Nothing here for every
                    # other call, which is what the empty diff of two Nones means.
                    exec_before: workdir_snapshot.Snapshot | None = None
                    exec_after: workdir_snapshot.Snapshot | None = None
                    tracker = self.strategies.get("usage_tracker")
                    if tracker is not None:
                        await tracker.record_tool_call(tool_call.name, tool_call.id)
                    preempted = ""
                    if tool_call.name == "ask_user":
                        from raven.agent.subagent import watch_work as _ww

                        preempted = _ww.preempt_owner_ask(watch_state, tool_call.arguments)
                    # Not asked of a preempted call: the branch below wins, so
                    # the refusal would be spent on something the model never sees.
                    stalled_verdict, stalled_answer = (
                        (NoProgressAction.RUN, "")
                        if preempted
                        else no_progress.check(tool_call.name, tool_call.arguments)
                    )
                    if preempted:
                        # The owner registered this answer so they would not be
                        # asked for it; the question never reaches them, and the
                        # reply arrives where the model expected the owner's.
                        result = "This question was not sent to the owner."
                        watch_note = preempted
                        duration_ms = int((time.monotonic() - tool_t0) * 1000)
                    elif stalled_verdict is not NoProgressAction.RUN:
                        # The call is established: it has already answered the
                        # same thing often enough that running it again is known
                        # not to change the answer. Answered rather than run, so
                        # what the model reads is an error it has to deal with
                        # instead of the success the nudge was appended to.
                        # A result is still appended for it either way -- an
                        # advertised tool_call id with no result is a 400 from
                        # every strict provider, including on the way out.
                        result = stalled_answer
                        watch_note = ""
                        duration_ms = int((time.monotonic() - tool_t0) * 1000)
                        if stalled_verdict is NoProgressAction.END_TURN:
                            stalled_tool = tool_call.name
                    else:
                        # Only around a command: every other tool reports the
                        # file it touched, and walking the directory twice per
                        # call would cost the turn far more than the one change
                        # it could find. Off the loop, because the walk is tens
                        # of milliseconds of it and every other session on this
                        # process waits behind them. The directory is the one
                        # the command runs in, which the tool itself resolves:
                        # the turn's unless the call names another.
                        exec_root = (
                            workdir_snapshot.root_for(
                                self.tools.get(tool_call.name),
                                tool_call.arguments,
                                workdir.current() or self.workspace,
                            )
                            if tool_call.name == "exec"
                            else None
                        )
                        if exec_root is not None:
                            exec_before = await asyncio.to_thread(workdir_snapshot.take, exec_root)
                        result = await self.tools.execute(
                            tool_call.name, tool_call.arguments, run_meta=tool_call.run_meta
                        )
                        duration_ms = int((time.monotonic() - tool_t0) * 1000)
                        if exec_before is not None:
                            exec_after = await asyncio.to_thread(workdir_snapshot.take, exec_root)
                        # The result itself is left alone: the note is the
                        # system's own line and is placed by add_tool_result AFTER
                        # the untrusted fence closes, so the model reads it as this
                        # system speaking rather than as data it must not obey.
                        watch_note = await self._note_watch_work(
                            watch_state,
                            tool_call.name,
                            tool_call.arguments,
                            str(result),
                            watch_request,
                            reasoning_effort=policy.reasoning_effort,
                        )
                    # The registry already unwrapped any ToolResult: `result` is
                    # the model-facing text, with the optional display string
                    # riding along on it (ToolOutput). The model always gets the
                    # model text; the UI preview prefers the display string.
                    model_text = str(result)
                    display_src = getattr(result, "display_text", None) or model_text
                    # The log stays one line; the UI event keeps newlines so a
                    # tool that reports several items (e.g. ask_user's
                    # question -> answer pairs) renders one row each.
                    #
                    # 200 was a row's worth, and the row is not where this ends
                    # up: the detail card shows the same string, where 200 chars
                    # cut an ordinary error message mid-sentence and left the
                    # reader to guess the rest. The cap is what a card can show
                    # without becoming a file viewer, not what a row can.
                    preview = display_src[:_TOOL_PREVIEW_MAX_CHARS]
                    logger.info(
                        "Tool result: {} duration={}ms result={}",
                        tool_call.name,
                        duration_ms,
                        preview.replace("\n", " ")[:200],
                    )
                    tool_metadata = self.tools.take_metadata(tool_call.name, tool_call.arguments)
                    # What the tool saw go, plus what this turn wrote and can no
                    # longer find. Settled before this call's own write is noted,
                    # so the file it just wrote is not stat'ed to say it exists.
                    tool_removed = removal_watch.settle(getattr(result, "removed", ()))
                    removal_watch.note_write(getattr(result, "file_change", None))
                    # What the listing found beside what the call said itself. The
                    # paths the result named are held out of it: the listing sees
                    # those too, and one write drawn twice is two files to a reader.
                    tool_change = getattr(result, "file_change", None)
                    accounted = [removal.path for removal in tool_removed]
                    if isinstance(getattr(tool_change, "path", None), str):
                        accounted.append(tool_change.path)
                    created, modified, deleted = workdir_snapshot.diff(exec_before, exec_after)
                    # Off the loop for the reason the walks above are: this reads
                    # every created file to number its lines, and one command can
                    # create hundreds. The removals stay here -- they read nothing.
                    tool_written = (
                        await asyncio.to_thread(_file_written_payload, created, modified, exec_after, already=accounted)
                        if created or modified
                        else None
                    )
                    tool_removed.extend(_listing_removals(deleted, already=accounted))
                    if emit_tool_event:
                        await on_tool_event(
                            "complete",
                            {
                                "tool_call_id": tool_call.id,
                                "result_preview": preview,
                                "truncated": len(display_src) > _TOOL_PREVIEW_MAX_CHARS,
                                "metadata": tool_metadata,
                                # The tool's own verdict, with the registry's
                                # failure text kept as the backstop for a result
                                # that carried none. See ToolEvent.ok.
                                "ok": not call_failed(result),
                                # The one hop the diff has to make by hand: the
                                # registry attaches it to the result, and only
                                # this event reaches a UI.
                                "diff": getattr(result, "diff", None),
                                # Alongside it, for a surface that renders the
                                # change itself rather than a unified diff of it.
                                "file_change": _file_change_payload(getattr(result, "file_change", None)),
                                # The deletions, which no tool reports as its
                                # result: a command's own watch plus the turn's.
                                "file_removed": _file_removed_payload(tool_removed),
                                # And what a command wrote, which it reports even
                                # less: read off the directory either side of it.
                                "file_written": tool_written,
                            },
                        )
                    # A skill the model loaded itself never passes through
                    # SkillForge injection, so report it here or the skill panel
                    # misses the whole class (builtin guides in particular).
                    if tool_call.name in ("read_skill", "use_skill") and not model_text.startswith("Error"):
                        await self._report_skill_read(session_key or "", tool_call.name, tool_call.arguments)
                    result_blocks = getattr(result, "blocks", None)
                    model_text, blocks, attach_blocks = self._route_result_images(
                        model_text, result_blocks, call_model or effective_model
                    )
                    # Provenance for the pictures, taken here where the tool, the
                    # round and the captions are all still in one place. The
                    # window reads it back when it withdraws them.
                    sources = image_sources(tool_call.name, result_blocks or [], iteration) if result_blocks else []
                    if blocks:
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, model_text, blocks, trusted_note=watch_note
                        )
                        if sources:
                            messages[-1][IMAGE_SOURCES_KEY] = sources
                    else:
                        # Keep the long-standing 4-arg call for text results so no
                        # existing caller or test double sees a signature change.
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, model_text, trusted_note=watch_note
                        )
                    if messages:
                        # Dispatch to result, on the entry that answers the call.
                        # The live tool event was its only carrier, so a restored
                        # transcript had to either invent a number or say nothing
                        # about a call that took two minutes.
                        messages[-1][_TOOL_DURATION_MS_KEY] = duration_ms
                        if tool_metadata:
                            messages[-1][_TOOL_METADATA_KEY] = tool_metadata
                    if (tool_diff := getattr(result, "diff", None)) and messages:
                        # Underscore-keyed while the turn is live so no provider
                        # payload grows a field mid-turn; `_save_turn` renames it
                        # to `diff` on the stored entry. Without this the diff
                        # exists only on the live tool event, and a reloaded page
                        # can never number a change it no longer has.
                        messages[-1]["_diff"] = tool_diff
                    if tool_removed and messages:
                        # The same underscore-then-rename convention as the diff
                        # above, and line counts rather than bodies: a removed
                        # file's text is what the live event carries, while what
                        # a reloaded page needs is that the file went and how big
                        # the hole is.
                        messages[-1]["_file_removed"] = [
                            {"path": removal.path, "del": len((removal.before or "").splitlines())}
                            for removal in tool_removed
                        ]
                    if tool_written and messages:
                        # Same underscore-then-rename convention, and here the
                        # stored shape is the live one: what it carries is already
                        # a count and a size rather than a body.
                        messages[-1]["_file_written"] = tool_written
                    if attach_blocks:
                        pending_images.extend(attach_blocks)
                        pending_sources.extend(sources)
                    if getattr(result, "blocks_call", False):
                        continuation = getattr(result, "continuation", Continuation.ABORT_TURN)
                        # The blocking tool's own words, kept for the reader: the
                        # canned reply below says an operation stopped but never
                        # which one, so without this the user is told a thing
                        # happened and given no way to find out what.
                        abort_reason = _first_line(model_text)
                        # A single assistant message may contain several parallel
                        # tool calls (for example ``rm`` followed by a Python
                        # fallback). Once policy terminates the action, none of
                        # the siblings may execute. We must nevertheless append
                        # one result for every advertised call id: OpenAI-style
                        # providers reject conversation history containing an
                        # assistant tool call without its matching tool result.
                        for skipped_call in response.tool_calls[tool_call_index + 1 :]:
                            messages = self.context.add_tool_result(
                                messages,
                                skipped_call.id,
                                skipped_call.name,
                                SKIPPED_AFTER_BLOCKED_CALL,
                            )
                        break
                    # Track consecutive same-tool deterministic failures
                    # (transient errors excluded — a retry would clear those).
                    if is_hard_tool_failure(model_text):
                        failure_key = (tool_call.name, failure_class(model_text))
                        if failure_key == loop_fail_key:
                            loop_fail_streak += 1
                        else:
                            loop_fail_key, loop_fail_streak = failure_key, 1
                    else:
                        loop_fail_key, loop_fail_streak = None, 0
                    # Counted for every call, failures included: a call failing
                    # identically is stuck too, and the branch above just has an
                    # earlier threshold for that shape. Not for a call the ladder
                    # answered itself, which ran nothing and so answered nothing.
                    # The routed blocks, not the raw ones: what the model receives is
                    # what decides whether this call answered anything new.
                    if stalled_verdict is NoProgressAction.RUN:
                        no_progress.record(tool_call.name, tool_call.arguments, model_text, blocks or attach_blocks)

                if continuation is Continuation.ABORT_TURN:
                    # A normal tool result starts another model iteration. That
                    # is specifically unsafe here: the next plan can translate
                    # the rejected operation into an equivalent interpreter,
                    # script, or tool call. Finish the turn in runtime code and
                    # expose only the non-destructive continuation question.
                    # The model must read this, so it goes into the history as an
                    # assistant message -- but it goes to the CLIENT as a notice.
                    # Pushed down the token stream instead, it arrived as the
                    # model's own prose: glued to whatever the model had just
                    # narrated (nothing separates two segments in one buffer),
                    # dressed in the answer's copy and branch actions, and always
                    # in English no matter what language the turn was in.
                    from raven.spine.events import NoticeKind as _NoticeKind

                    messages = self.context.add_assistant_message(messages, _ABORTED_ACTION_REPLY)
                    if messages:
                        messages[-1][_NOTICE_KEY] = {
                            "kind": _NoticeKind.ACTION_BLOCKED.value,
                            **({"detail": abort_reason} if abort_reason else {}),
                        }
                    final_content = _ABORTED_ACTION_REPLY
                    if on_notice is not None:
                        # Not staged the way the retry notice is: this notice IS
                        # the turn's answer, so abandoning it on a wedged outlet
                        # queue would end the turn saying nothing at all.
                        await on_notice(_NoticeKind.ACTION_BLOCKED, abort_reason)
                    elif on_token_delta is not None:
                        # A channel with no notice outlet still has to say
                        # something, and silence is the worse failure.
                        await on_token_delta(_ABORTED_ACTION_REPLY)
                    break

                # The no-progress ladder's last step. Placed with the aborted
                # action rather than with the nudges below: no further model
                # call happens, so an appended nudge would be read by nobody and
                # a pending picture would be shown to nobody. The batch this
                # call sat in was allowed to finish first -- every advertised
                # tool_call id therefore has its result, which is what strict
                # providers validate on the way out too.
                if stalled_tool is not None:
                    logger.warning(
                        "`{}` repeated with an identical result past the refusal budget; ending the turn",
                        stalled_tool,
                    )
                    break

                # Failure-loop break: the same tool failed deterministically
                # `threshold` times running → append a change-approach nudge to
                # the last tool result so the model stops repeating a dead call.
                if (
                    loop_fail_streak >= self._LOOP_BREAK_THRESHOLD
                    and loop_nudges < self._LOOP_BREAK_MAX
                    and messages
                    and messages[-1].get("role") == "tool"
                ):
                    loop_nudges += 1
                    messages[-1]["content"] = (
                        str(messages[-1].get("content", ""))
                        + "\n\n"
                        + loop_break_nudge(
                            loop_fail_key[0],
                            loop_fail_streak,
                            loop_fail_key[1],
                            suggest_find_skill=self.tools.offers_by_name("find_skill"),
                        )
                    )
                    loop_fail_streak = 0  # fire once per fresh streak
                # And the other stuck shape: the call works, and keeps saying the
                # same thing. Second, because the failure nudge is the more
                # specific advice and reaches its threshold first. Taken only
                # here, where the tool result it belongs to is still the last
                # message -- the guard holds it armed until then.
                elif messages and messages[-1].get("role") == "tool" and (nudge := no_progress.take_nudge()):
                    messages[-1]["content"] = str(messages[-1].get("content", "")) + "\n\n" + nudge
                # After the nudge above, which needs the last message to still be
                # the tool result it appends to. Also after the blocked-call
                # branch, which ends the turn in runtime code -- there is no
                # further model call to show a picture to, so an aborted action
                # deliberately drops it rather than leaving it dangling.
                if pending_images:
                    messages.append(
                        {
                            "role": "user",
                            "content": pending_images,
                            ATTACHED_IMAGE_KEY: True,
                            IMAGE_SOURCES_KEY: pending_sources,
                        }
                    )
                # Dispatched before prev_had_tool_calls is set: a rollback means
                # this iteration never happened, so the empty-response classifier
                # must see the pre-iteration state on the re-sample.
                if hook_ctx is not None:
                    hook_ctx.messages = messages
                    hook_ctx.response = response
                    decision = await self.hooks.after_iteration(hook_ctx)
                    if _hook_rollback(decision):
                        continue
                    _land_hook_note(decision)
                    if decision.short_circuit_result is not None:
                        final_content = str(decision.short_circuit_result)
                        messages = self.context.add_assistant_message(messages, final_content)
                        break
                prev_had_tool_calls = True
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops.
                if response.finish_reason == "error":
                    # The provider's ladder is seconds long and has already run. A
                    # gateway serving error pages for a few minutes outlasts it, and
                    # ending the turn here threw away an hour of work on a 40-second
                    # outage. So a retryable failure waits out a longer ladder before
                    # the turn is given up -- the messages are untouched (nothing was
                    # appended for the failed call), so asking again is the same ask.
                    verdict = response.error_classification
                    if verdict is None and (classify := getattr(self.provider, "classify_error", None)) is not None:
                        verdict = classify(content=clean or None)
                    ladder = self._recovery_limits.llm_error_retry_delays
                    if verdict is not None and verdict.retryable and error_waits < len(ladder):
                        delay = ladder[error_waits]
                        error_waits += 1
                        logger.warning(
                            "LLM error [{}] outlasted the provider's retries; asking again in {:.0f}s (wait {}/{}): {}",
                            verdict.category,
                            delay,
                            error_waits,
                            len(ladder),
                            (clean or "")[:160],
                        )
                        if on_notice is not None:
                            # Only the category: the vendor's own body can carry
                            # a masked key and an account URL, and this text is
                            # read by everyone watching the turn.
                            from raven.spine.events import NoticeKind as _NoticeKind

                            # Staged like the episode boundary: a bounded outlet
                            # queue with a wedged worker behind it would park the
                            # turn here for the whole ladder, and the notice is a
                            # status hint -- losing one is a silent wait, not a
                            # turn lost.
                            await first_call.stage(
                                "the retry-notice emit",
                                on_notice(_NoticeKind.LLM_RETRY, verdict.category),
                                iteration=iteration,
                                fallback=None,
                            )
                        iteration -= 1  # the failed call did no work; don't bill it
                        await asyncio.sleep(delay)
                        continue
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    status = "error"
                    error_detail = _llm_failure_detail(clean, verdict)
                    break

                # Empty-response recovery: an empty assistant turn would
                # otherwise end here as a turn with no answer, which fails.
                # Try to recover before giving up. Synthetic scaffolding is
                # marked ``_recovery_synthetic`` and stripped before persistence
                # / extraction so it can't poison future context.
                # Asked of the provider for the model this call went to: a
                # prefill-refusing vendor (Anthropic with thinking on) must never
                # be handed a request ending on an assistant message.
                supports_prefill = getattr(self.provider, "supports_assistant_prefill", None)
                action = classify_empty_response(
                    response,
                    clean,
                    prev_had_tool_calls=prev_had_tool_calls,
                    nudges_done=post_tool_nudges,
                    prefill_retries=prefill_retries,
                    empty_retries=empty_retries,
                    limits=self._recovery_limits,
                    prefill_supported=supports_prefill is None or supports_prefill(call_model),
                )
                if action is RecoveryAction.PREFILL:
                    prefill_retries += 1
                    logger.warning(
                        "empty-recovery: thinking-only prefill {}/{}",
                        prefill_retries,
                        self._recovery_limits.thinking_prefill_max_retries,
                    )
                    # Re-feed the model its own reasoning (not stripped) so it
                    # continues into the body. Marked synthetic → dropped before
                    # persistence/extraction; the reasoning fields are stripped
                    # from the wire request by the provider's key allowlist.
                    messages = self.context.add_assistant_message(
                        messages,
                        response.content,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    )
                    messages[-1]["_recovery_synthetic"] = True
                    prev_had_tool_calls = False
                    # Reasoning that ended on the ceiling was cut mid-thought, so
                    # the continuation opens on the rest of that thought.
                    cut_continuation = response.finish_reason == "length"
                    continue
                if action is RecoveryAction.NUDGE:
                    post_tool_nudges += 1
                    logger.warning("empty-recovery: post-tool empty nudge")
                    # The (empty) assistant must sit between the tool result and
                    # the nudge — a bare tool→user sequence is a 400 on most APIs.
                    messages = self.context.add_assistant_message(messages, "(empty)")
                    messages[-1]["_recovery_synthetic"] = True
                    messages.append({"role": "user", "content": POST_TOOL_NUDGE, "_recovery_synthetic": True})
                    prev_had_tool_calls = False
                    continue
                if action is RecoveryAction.RETRY:
                    # Re-sent unchanged, this was the same bytes, so each retry
                    # was a guaranteed repeat: measured, three of them behind
                    # three truncations at exactly the output ceiling. An empty
                    # body with reasoning behind it is a call whose thinking
                    # spent the ceiling before the answer began, so what has to
                    # change is how much of the ceiling thinking may take. The
                    # descent rides the loop's existing per-call override lane
                    # (``pending_gen_overrides``, which a hook rollback uses to
                    # re-sample one call) rather than a second mechanism beside
                    # it, and it is spent on the retry only -- a call that
                    # answers leaves the turn's own effort standing.
                    # Asked of the provider, not of the ladder: two labels can
                    # be one request. ``reasoning_wire_keys`` says what a rung
                    # would actually send, so a rung the wire cannot tell apart
                    # from this one is skipped instead of paid for again.
                    lowered = lower_reasoning_effort(
                        empty_retry_effort or call_reasoning_effort,
                        _reasoning_wire_keys(self.provider, effective_model),
                    )
                    if lowered is None:
                        # The budget is not the only bound: a retry with nothing
                        # left to change is the failure again at full price. Ending
                        # here takes the FAIL exit below rather than falling into
                        # the completion path -- running out of rungs is the same
                        # outcome as running out of budget, and the completion path
                        # is what filed a dead turn as a finished one.
                        logger.warning(
                            "empty-recovery: not retrying at reasoning_effort {} -- no rung left to change",
                            empty_retry_effort or call_reasoning_effort or "unstated",
                        )
                        action = RecoveryAction.FAIL
                    else:
                        empty_retries += 1
                        empty_retry_effort = lowered
                        pending_gen_overrides = {"reasoning_effort": lowered}
                        logger.warning(
                            "empty-recovery: plain empty retry {}/{} (reasoning_effort {} -> {})",
                            empty_retries,
                            self._recovery_limits.empty_content_max_retries,
                            call_reasoning_effort or "unstated",
                            lowered,
                        )
                        if response.truncated and not output_limit_told:
                            # The descent above changes how much thinking the next
                            # call may buy, which is nothing to a model that does no
                            # reasoning and nothing to a write whose payload is the
                            # thing that overran. Neither case leaves a tool call for
                            # `Tool.truncation_hint` to ride, so this is the only
                            # channel that reaches the model at all.
                            # Read off `response.truncated`, not `finish_reason`: the
                            # contract has one field for "stopped at the ceiling" and
                            # says an upstream may claim success instead.
                            # Same assistant-then-user shape as the nudge above, for
                            # the same reason: a bare tool->user pair is a 400 on most
                            # APIs, and the prefill may have left an assistant last.
                            output_limit_told = True
                            messages = self.context.add_assistant_message(messages, "(empty)")
                            messages[-1]["_recovery_synthetic"] = True
                            messages.append(
                                {"role": "user", "content": OUTPUT_LIMIT_NUDGE, "_recovery_synthetic": True}
                            )
                        prev_had_tool_calls = False
                        continue
                if action is RecoveryAction.FAIL:
                    # Same shape as the provider-error exit above, because it is
                    # the same kind of outcome: the turn produced nothing and the
                    # caller has to be able to act on that. What it buys is the
                    # label every surface puts on the turn -- the web page, the
                    # terminal, ``raven agent -p``'s exit code and a channel's
                    # reply -- plus the marker filed in the session and the
                    # failure a cron record and a trajectory reader keep. It is
                    # not about ACP's stop reason: that answers ``end_turn`` with
                    # the failure as text for every failed turn by design.
                    # The counts go into the detail, because the reader who has
                    # to decide whether to ask again is a person or a judging
                    # model, and neither can see this log line.
                    attempts = 1 + prefill_retries + post_tool_nudges + empty_retries
                    logger.error(
                        "empty-recovery: no content after {} attempt(s) "
                        "(prefill {}, nudge {}, retry {}); ending the turn as an error",
                        attempts,
                        prefill_retries,
                        post_tool_nudges,
                        empty_retries,
                    )
                    error_detail = (
                        f"The model returned no content on {attempts} attempt(s) "
                        f"(prefill {prefill_retries}, post-tool nudge {post_tool_nudges}, "
                        f"plain retry {empty_retries})."
                    )
                    # Kept, and read only where a tool has already answered: the
                    # caller fails an unanswered turn on ``error`` above instead
                    # of delivering this.
                    final_content = (
                        f"{error_detail} This turn's empty-response recovery is spent; "
                        "the turn produced no answer of its own."
                    )
                    status = "error"
                    break

                # Before the text is persisted, so a short-circuit replaces it
                # without leaving the replaced draft in history and a rollback
                # discards-and-re-samples it. The stream is held to match: a
                # rollback pops history, and only the draft gate can keep the
                # reader from having seen what history no longer has.
                if hook_ctx is not None:
                    hook_ctx.messages = messages
                    hook_ctx.response = response
                    decision = await self.hooks.after_iteration(hook_ctx)
                    if _hook_rollback(decision):
                        if draft is not None:
                            draft.discard()
                        continue
                    if draft is not None:
                        await draft.release()
                    _land_hook_note(decision)
                    if decision.short_circuit_result is not None:
                        final_content = str(decision.short_circuit_result)
                        messages = self.context.add_assistant_message(messages, final_content)
                        break

                messages = self.context.add_assistant_message(
                    messages,
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                _stamp_reasoning_ms(messages, response)
                final_content = clean
                break

        # A row recorded while a tool ran rides the next iteration's flush,
        # because the model always has to be shown the results. The exits that
        # skip that next pass -- max iterations, a blocked call -- lose the
        # write-back. The row was already rendered live and the answer already
        # went to the sub-agent, so this is worth a line, not a repair.
        leftover = current_autofill()
        if leftover is not None and (lost := leftover.pending_rows()):
            logger.info("question autofill: {} row(s) ended the turn unwritten", len(lost))

        if final_content is None and (
            stalled_tool is not None or iteration >= iteration_cap or stopped_by == "wall_clock"
        ):
            if stalled_tool is not None:
                stopped_by = "stalled_tool"
                logger.warning("Turn stopped on a repeating `{}`; synthesizing final answer", stalled_tool)
            elif stopped_by == "wall_clock":
                logger.warning("Wall-clock budget reached; synthesizing final answer")
            else:
                stopped_by = "iteration_cap"
                logger.warning("Max iterations ({}) reached; synthesizing final answer", iteration_cap)
            # Exhaustion is two orthogonal facts, not an either/or:
            #   1. The turn did NOT complete — tag it ``interrupted`` so the
            #      shadow-git checkpoint commit is labelled and the next turn's
            #      recovery prompt can surface the sha + edited files to resume.
            #   2. The user still deserves a useful reply NOW — so, checkpoint
            #      or not, synthesize a best-effort wrap-up (one tools-disabled
            #      call summarising what was done and what's left) instead of a
            #      canned apology. Synthesis falls back to a static message
            #      internally if the call fails, so the turn is never silent.
            status = "interrupted"
            final_content = await self._synthesize_final_on_exhaustion(
                messages,
                effective_model,
                fallback_models,
                on_token_delta=on_token_delta,
                on_reasoning_delta=on_reasoning_delta,
                reasoning_effort=policy.reasoning_effort,
                prompt=_MAX_ITER_SYNTHESIS_PROMPT if stalled_tool is None else _STALLED_SYNTHESIS_PROMPT,
                fallback=(
                    _MAX_ITER_STATIC_FALLBACK.format(n=self.max_iterations)
                    if stalled_tool is None
                    else _STALLED_STATIC_FALLBACK.format(tool=stalled_tool)
                ),
            )
            # Persist the wrap-up into history like any normal final reply.
            # Persistence downstream reads only the returned ``messages`` list,
            # so without this the synthesized answer reaches the user via the
            # stream yet never enters the conversation — the next turn (notably
            # an interrupted-turn resume) could not see what was summarized. The
            # synthesis prompt itself stays local to the helper, so only the
            # reply lands here.
            if final_content:
                messages = self.context.add_assistant_message(messages, final_content)

        # Drop transient empty-recovery scaffolding before persistence /
        # extraction / return: empty recovery marks synthetic nudge/prefill
        # messages with ``_recovery_synthetic``; strip them so they never
        # persist. The after-turn pipeline (``context_engine.after_turn`` +
        # ``backend.store`` in ``_process_message``) owns extraction.
        # Attached-image messages are dropped here for the same reason and at the
        # same point: the returned list feeds persistence, ``after_turn``
        # extraction and ``backend.store`` alike, so filtering once upstream of
        # all three is the only place that covers them.
        # The terminal seam: a turn that ended with nothing a reader can see --
        # a provider error, or an exhausted budget whose wrap-up came back
        # empty -- gets one last chance to commit an answer.
        answerless = status == "error" or not (final_content or "").strip()
        if hook_ctx is not None:
            # Written for every turn, not only an answerless one. "This turn was cut
            # short" is not recoverable from ``status``: the iteration cap, a stalled
            # tool and a spent wall clock all land on ``interrupted``, and the wrap-up
            # reply they produce reads as an ordinary answer to everything downstream.
            # Ints and None only -- an observer chain that keeps scalars by type drops a
            # float without a word.
            #
            # Two scopes sit in here and ``attempt`` is what tells them apart. A turn a
            # product budgets a rerun for runs this loop more than once, and the count
            # below is of THIS run, because that is what the loop counts; the elapsed
            # time is of the whole turn, because that is what the budget bounds. One
            # iteration beside twelve seconds otherwise reads as a single slow call.
            # ``salvaged`` joins the record below, after the terminal seam returns, so
            # the gate reading this record is never the one that sees it.
            hook_ctx.metadata["turn_end"] = {
                "status": status,
                "attempt": attempt,
                "iterations": iteration,
                "stopped_by": stopped_by,
                "wall_clock_budget_s": int(budgets.wall_clock_seconds) if budgets.wall_clock_seconds else None,
                "turn_elapsed_s": int(monotonic() - turn_t0),
            }
        # An attempt the caller is about to run again is not salvaged first. The gate
        # behind this seam can spend minutes manufacturing an answer, and both ends
        # that answer can meet are wrong: the rerun discards it, having charged the
        # turn's own clock for it, or -- when the turn ended answerless without
        # erroring -- it fills the emptiness the rerun reads and the rerun never
        # happens, so salvage wins by suppressing the thing measured to beat it.
        #
        # Asked for every turn rather than only an answerless one, and asked once:
        # the caller decides the rerun on this same answer, and a question put twice
        # can come back differently the second time -- the checkpoint and the persist
        # between here and there are seconds a clock can cross. A loop nobody
        # budgeted a rerun for asks no one and salvages as it always did.
        rerun_coming = rerun_pending is not None and rerun_pending(final_content, messages, status)
        if hook_ctx is not None and answerless and not rerun_coming:
            hook_ctx.messages = messages
            hook_ctx.response = None
            decision = await self.hooks.terminal_answerless(hook_ctx)
            if decision.short_circuit_result is not None:
                final_content = str(decision.short_circuit_result)
                messages = self.context.add_assistant_message(messages, final_content)
                hook_ctx.metadata["turn_end"]["salvaged"] = True
                error_detail = None

        if any(any(m.get(k) for k in _TURN_TRANSIENT_KEYS) for m in messages):
            messages = [m for m in messages if not any(m.get(k) for k in _TURN_TRANSIENT_KEYS)]

        # Extraction belongs to the caller's after-turn pipeline
        # (``context_engine.after_turn`` + ``backend.store`` + ``backend.feedback``
        # run from ``_process_message``); ``outcome.status`` is surfaced so that
        # pipeline can gate on completion.

        outcome = LoopOutcome(status=status, error=error_detail)
        checkpoint = self._turn_checkpoint()
        if checkpoint is not None:
            # Per-turn snapshot: one commit covering all of this turn's edits,
            # for both normal and interrupted exits (matches Claude Code/Cursor
            # granularity). Best-effort — commit_turn never raises.
            # The label is read in ``git log`` inside the shadow repo by
            # someone recovering a file. The repo already implies the
            # directory, so the session key is what disambiguates -- several
            # sessions share one shadow repo whenever they resolve to the same
            # directory (LAUNCH_DIR, or an explicit workdir override).
            label = f"turn {session_key} [{status}]" if session_key else f"turn [{status}]"
            cid, changed = await checkpoint.commit_turn(label)
            outcome.checkpoint_id = cid
            if status == "interrupted":
                outcome.edited_files = changed

        return final_content, tools_used, messages, outcome

    # root: a turn is the top of its own trace. Without it a turn opened while
    # another span is still active -- a dispatch that reports back, a follow-up
    # driven by a tool's own completion -- nests inside that span, and the two
    # turns share one trace.
    @trace.instrument(
        "session.turn", root=True, seed=semconv.turn_seed, on_open=semconv.turn_open, extract=semconv.turn
    )
    async def _process_message(  # noqa: C901 (cc 47: pre-existing, above the ceiling)
        self,
        req: TurnRequest,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_token_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_event: Callable[[str, dict], Awaitable[None]] | None = None,
        on_episode_start: Callable[[int], Awaitable[None]] | None = None,
        on_notice: Callable[[NoticeKind, str], Awaitable[None]] | None = None,
        usage_sink: dict[str, Any] | None = None,
        origin: Origin | None = None,
        drain: Drain | None = None,
        hook_sink: dict[str, str] | None = None,
    ) -> tuple[str | None, list[str]] | None:
        """Process a single turn request and return its reply.

        Returns ``(reply_content, media_paths)`` for a turn that produced an
        outbound reply, or ``None`` for a silent turn (the message tool already
        sent, or a hook short-circuit chose to return None). ``origin`` is the
        spine TurnRequest's origin. ``hook_sink``, when given, receives under
        ``"appended"`` whatever the ``after_send`` chain added to the end of the
        reply: a streamed reply has already left as deltas by then, so the
        caller has to send that tail itself.
        """
        from raven.agent.hook import AgentHookContext

        # Captured before any pre-turn work (hooks, personalization, context
        # assembly): this is when the user's message arrived, and it becomes the
        # stored user entry's timestamp. Everything saved by _save_turn is
        # stamped at turn END, so without this the whole turn shares one clock
        # read and a restored transcript cannot say how long the turn took.
        turn_received_at = self._now_fn().isoformat()

        channel = req.source.channel
        sender_id = req.source.sender_id
        chat_id = req.source.chat_id
        content = req.text
        metadata = dict(req.source.extras)
        media_paths = [m.path for m in req.media]
        msg_session_key = req.conversation or f"{channel}:{chat_id}"

        # AgentHook ``before_user_inbound`` chain. The chain runs once and:
        #   - lets observer hooks (FeedbackTracker, the on_user_inbound
        #     adapter) record engagement;
        #   - lets short-circuit hooks (DecisionConsumer adapter for
        #     Sentinel /pick replies) halt processing and return their
        #     (content, media) reply directly.
        #
        # Skip the user-inbound hooks for Sentinel / subagent turns (by origin).
        inbound_original = content
        skip_user_inbound = origin in _SKIP_USER_INBOUND_ORIGINS
        if skip_user_inbound:
            from raven.agent.harness.participants import Intake, read_intake
            from raven.agent.subagent.charter import charter_participants
            from raven.contracts.participant import StepView

            participants = charter_participants()
            if participants:
                peeked = self.sessions.peek(msg_session_key)
                step = StepView(
                    session_key=msg_session_key,
                    iteration=0,
                    response=None,
                    transcript=(),
                    history=tuple(peeked.messages if peeked is not None else ()),
                    turn_base=0,
                    question=content,
                    rollbacks=0,
                    mode=None,
                    mode_overlay=None,
                    phase="user_inbound",
                )
                answer = await self.harness.memory.ask_intake(content, step, participants)
                intake = answer if answer is None or isinstance(answer, Intake) else read_intake(answer, text=content)
                if intake is not None:
                    if intake.reply is not None:
                        return str(intake.reply), []
                    content = intake.text

        # The turn's one hook-metadata dict, and the words the user actually
        # sent: the first crosses every phase group with the turn, the second
        # is what history keeps no matter how hooks rewrite the model's view.
        turn_hook_meta: dict[str, Any] = {}
        if len(self.hooks) > 0 and not skip_user_inbound:
            _peeked = self.sessions.peek(msg_session_key)
            _hook_ctx = AgentHookContext(
                session_key=msg_session_key,
                turn_request=req,
                inbound_content=content,
                session_history=_peeked.messages if _peeked is not None else [],
                metadata=turn_hook_meta,
            )
            _decision = await self.hooks.before_user_inbound(_hook_ctx)
            if _decision.short_circuit_result is not None:
                result = _decision.short_circuit_result
                if isinstance(result, tuple) and len(result) == 2:
                    return result
                return str(result), []
            if _decision.modified_content is not None:
                content = _decision.modified_content

        preview = content[:80] + "..." if len(content) > 80 else content
        logger.info("Processing message from {}:{}: {}", channel, sender_id, preview)

        # NOTE: the Sentinel ``decision_consumer`` short-circuit lives in the
        # unified AgentHook ``before_user_inbound`` chain at the
        # top of this method. Reaching this point means no hook claimed
        # the message and we proceed to normal slash-command / agent-loop
        # processing.

        key = session_key or msg_session_key
        session = self.sessions.get_or_create(key)

        # Slash commands
        cmd = content.strip().lower()
        if cmd == "/new":
            try:
                if not await self.memory_consolidator.consolidate_unconsolidated(session):
                    return (
                        "Memory consolidation failed, session not cleared. Please try again.",
                        [],
                    )
            except Exception:
                logger.exception("/new consolidation failed for {}", session.key)
                return (
                    "Memory consolidation failed, session not cleared. Please try again.",
                    [],
                )

            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            return ("New session started.", [])
        if cmd == "/help":
            lines = [
                "🐦‍⬛ Raven commands:",
                "/new — Start a new conversation",
                "/stop — Stop the current task",
                "/restart — Restart the bot",
                "/help — Show available commands",
            ]
            return ("\n".join(lines), [])
        if not self.harness.memory.owns_compaction:
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        # ── Personalization flow (global switch: agents.defaults.enablePersonalization) ──
        # Skip for a subagent result re-injection: its content is a system-generated
        # announce, not user input — personalizing it would pollute the profile or
        # fire a clarification on the announce. Only SUBAGENT skips here (not the
        # wider after-send / user-inbound sets): a Sentinel notice and cron/heartbeat
        # reach this flow today and keep it.
        if self.personalization_enabled and origin is not Origin.SUBAGENT:
            from datetime import datetime as _dt

            from raven.agent.personalizer import Personalizer

            _personalizer = Personalizer(MemoryStore(self.workspace), self.provider, self.model)

            # ── Step 2 completion: user is answering a pending clarification ──
            if session.pending_clarification:
                _pending = session.pending_clarification

                # Determine whether the user is answering the previous question
                # or starting a fresh request. A fresh request typically contains
                # action verbs and is unrelated to the original; re-classify to
                # decide: if clarification is still needed, treat it as new.
                _recent = session.get_history(max_messages=4)
                _recheck = await _personalizer.classify(content, history=_recent)
                _is_new_request = _recheck.get("needs_clarification", False)

                if _is_new_request:
                    # User started a new request; discard the old pending state and re-classify.
                    session.pending_clarification = None
                    self.sessions.save(session)
                    logger.info("Personalization: new request detected, discarding old pending_clarification")

                    _question = await _personalizer.generate_question(
                        content,
                        _recheck.get("domain", ""),
                    )
                    if _question:
                        _ts = _dt.now().isoformat()
                        self._record_inbound(session, content, origin, _ts)
                        session.record({"role": "assistant", "content": _question, "timestamp": _ts})
                        session.pending_clarification = {
                            "original_message": content,
                            "question": _question,
                            "domain": _recheck.get("domain", ""),
                        }
                        self.sessions.save(session)
                        logger.info(
                            "Personalization: asked clarification for new request, session {}",
                            session.key,
                        )
                        return (_question, [])
                    # Clear pending state and proceed normally when question generation fails.
                    session.pending_clarification = None

                else:
                    # User is answering the previous question; extract preference and resume the original task.
                    session.pending_clarification = None

                    # Extract preference into MEMORY.md in the background without blocking the response.
                    async def _extract():
                        await _personalizer.extract_and_store_preference(
                            original_message=_pending["original_message"],
                            question=_pending["question"],
                            answer=content,
                        )

                    _t = asyncio.create_task(_extract())
                    self._consolidation_tasks.add(_t)
                    _t.add_done_callback(self._consolidation_tasks.discard)
                    # Continue normally: LLM understands the task via conversation history.

            else:
                # ── Step 1: classify the request — decide whether clarification is needed ──
                _recent = session.get_history(max_messages=4)
                _classification = await _personalizer.classify(content, history=_recent)

                if _classification.get("needs_clarification"):
                    # ── Step 2: pre-action interaction — generate and return a clarifying question ──
                    _question = await _personalizer.generate_question(
                        content,
                        _classification.get("domain", ""),
                    )

                    if _question:
                        # Write the original request and the clarifying question into history to keep the conversation coherent.
                        _ts = _dt.now().isoformat()
                        self._record_inbound(session, content, origin, _ts)
                        session.record({"role": "assistant", "content": _question, "timestamp": _ts})

                        # Save the pending state so the next message can resume it.
                        session.pending_clarification = {
                            "original_message": content,
                            "question": _question,
                            "domain": _classification.get("domain", ""),
                        }
                        self.sessions.save(session)

                        logger.info("Personalization: asked clarification for session {}", session.key)
                        return (_question, [])
                    # generate_question failed: skip silently and proceed
        # ── End personalization flow ─────────────────────────────────────────

        self._set_tool_context(channel, chat_id, metadata.get("message_id"), session_key=key)
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()
        # ask_user keys by the true conversation_id (== the lane / gate key),
        # which is topic-aware (req.conversation), not just channel:chat_id.
        if (ask_tool := self.tools.get("ask_user")) and isinstance(ask_tool, AskUserTool):
            ask_tool.set_context(key)

        # No playbook interception. A playbook is one of the things the model can
        # reach for this turn (`load_playbook`), not something that decides ahead
        # of it: the funnel that used to sit here judged one message with no
        # history and, on a hit, replaced the whole turn -- so the party with the
        # least context made the most expensive call. All that remains per turn is
        # telling the tool what the turn is about, so its listing can be ranked.
        if self._playbooks is not None:
            self._playbooks.set_context(channel=channel, chat_id=chat_id, session_key=key)
            if (pb_tool := self.tools.get("load_playbook")) is not None and hasattr(pb_tool, "set_turn_message"):
                pb_tool.set_turn_message(content)

        context_messages = self._context_messages_for_session(session)
        # SkillForge: Selector picks top-K. See note in the system-message
        # branch above — empty return falls back to the full directory.
        # Routed via ``_select_skills_for_turn`` so the engine can
        # short-circuit selection here.
        selected_skills = await self._select_skills_for_turn(
            content,
            context_messages,
        )
        # ── Model selection: the session's own pick, else the router ─────────
        # Ahead of assembly, not after it: assembly decides whether an
        # attachment is inlined as a picture or described in text, and that
        # question is about the model the request will actually reach. Routing
        # needs only ``content``, so asking first costs nothing and stops one
        # model's verdict from shaping a message another model receives.
        # A per-session model is an explicit user choice, so it outranks the
        # router's heuristic and suppresses its fallback chain.
        routed_model: str | None = session.metadata.get("model")
        fallback_models: list[str] = []
        if routed_model is not None:
            logger.info("Session model: {} → {}", self.model, routed_model)
        elif self.router is not None:
            routed_model, fallback_models = await self.router.select_model_chain(content)
            if routed_model and routed_model != self.model:
                logger.info("Router: {} → {}", self.model, routed_model)
            if fallback_models:
                logger.info("Router fallback chain: {}", fallback_models)

        initial_messages = await self._assemble_context_messages(
            session=session,
            session_key=key,
            current_message=content,
            media=media_paths if media_paths else None,
            channel=channel,
            chat_id=chat_id,
            surface=req.source.surface,
            selected_skills=selected_skills or None,
            model=routed_model,
        )
        if (origin_mark := _runtime_origin(req.origin)) and initial_messages:
            # Stamped on the envelope the assembler just built, which is the entry
            # ``_save_turn`` persists as this turn's user message. Marked here
            # rather than inside the assembler because the origin is a fact about
            # the request, and the assembler is handed a turn, not a request.
            last = initial_messages[-1]
            if last.get("role") == "user":
                last[_ORIGIN_KEY] = origin_mark
        # Planning's one seat, and deliberately after the stamp above: the
        # origin mark belongs on the envelope the assembler built, so a planner
        # that returns a different list must not be able to move which message
        # gets marked. The default passes the list straight through, which is
        # what Raven has always done -- planning is the model's own, and the
        # position *ahead* of the turn was measured to be the wrong one for a
        # harness to take it (see ``harness/planning.py``).
        planning = await self.harness.planning.prepare(
            PlanningRequest(task=content, session_key=key, messages=initial_messages)
        )
        initial_messages = planning.messages
        # Surface the skills SkillForge injected this turn to the web UI's skill
        # panel (populated into _last_injected_skill_ids by the assemble above).
        await self._emit_injected_skills(key)
        degraded = getattr(self, "_last_degraded_segments", [])
        if degraded and on_notice is not None:
            # The answer was produced without an optional organ; saying so is
            # the degrade-with-notice ruling -- silence here would make a
            # memoryless answer indistinguishable from a remembered one.
            await on_notice(
                NoticeKind.ORGAN_DEGRADED,
                "Some capabilities were unavailable this turn ("
                + ", ".join(degraded)
                + "); the answer was produced without them.",
            )

        turn_start_idx = len(initial_messages) - 1
        # The assembled list ends with THIS turn's user message, which is the
        # entry the mark belongs on.
        if req.delegated and initial_messages:
            initial_messages[-1][_DELEGATED_KEY] = dict(req.delegated)
        # The question reaches disk here rather than with the rest of the turn.
        # A session whose first turn is still running had nothing on disk at
        # all, so it was absent from every listing, and a reader who left the
        # page could not find their way back to the turn still running in it.
        # Read prev_len first: everything below that slices "what this turn
        # added" off the session counts from before this write.
        prev_len = len(session.messages)
        self._save_turn(
            session,
            initial_messages,
            turn_start_idx,
            received_at=turn_received_at,
            inbound_original=inbound_original,
        )
        self.sessions.save(session)
        # Where the three writes that close the turn start from. The question is
        # already filed, and filing it again would both double it and stamp the
        # turn's arrival clock onto the first message injected mid-turn.
        persist_from = turn_start_idx + 1
        # The stream buffers exist so a turn that dies mid-answer still has the
        # text that was already on the reader's screen: the loop only appends an
        # assistant message once the provider call returns, so a cancel in the
        # middle of one would otherwise lose exactly what streamed.
        streamed: dict[str, str] = {"text": "", "thought": ""}
        # The turn's episode counter, kept out here rather than taken from the loop.
        # ``EpisodeStart.index`` is the 0-based step within the TURN, and the loop
        # numbers from its own iteration count, which restarts whenever the turn is
        # run again. The TUI keys episode rows and their fold state by this index, so
        # a second attempt beginning at zero would share both with the first.
        episodes: dict[str, int] = {"next": 0}

        async def _tap_token(delta: str) -> None:
            streamed["text"] += delta
            if on_token_delta is not None:
                await on_token_delta(delta)

        async def _tap_reasoning(delta: str) -> None:
            streamed["thought"] += delta
            if on_reasoning_delta is not None:
                await on_reasoning_delta(delta)

        async def _tap_episode(_index: int) -> None:
            # A new episode is a new stream: without the reset, a buffer that
            # spans two assistant messages matches neither and would be saved
            # as a duplicate of text the loop already committed.
            streamed["text"] = ""
            streamed["thought"] = ""
            index = episodes["next"]
            episodes["next"] = index + 1
            if on_episode_start is not None:
                await on_episode_start(index)

        from raven.agent.subagent.attachments import turn_attachments
        from raven.agent.subagent.mode_tiers import turn_tier

        # The turn's cost, opened around the attempts rather than inside one:
        # every attempt is this turn spending, and so is every sub-agent it
        # dispatches, which bills in from its own session by this key.
        spend = TurnSpend(key)

        async def _attempt(seed: list[dict], attempt: int):
            pending.update(rerun=False, reasons=[])
            # The list this attempt appends to, for the rescue paths below. A rerun
            # runs on a list of its own, and a turn cancelled during one used to be
            # saved from the first attempt's -- so a tool the reader watched run in
            # the rerun, and its result, were not in what the cancel filed.
            live["messages"] = seed
            return await self._run_agent_loop(
                seed,
                on_progress=on_progress,
                session_key=key,
                model=routed_model,
                fallback_models=fallback_models,
                injected_skill_ids=self._collect_injected_skill_ids(selected_skills),
                on_token_delta=_tap_token if on_token_delta is not None else None,
                on_reasoning_delta=_tap_reasoning if on_reasoning_delta is not None else None,
                on_tool_event=on_tool_event,
                on_episode_start=_tap_episode,
                on_notice=on_notice,
                usage_sink=usage_sink,
                drain=drain,
                hook_metadata=turn_hook_meta,
                session_history=session.messages[:prev_len],
                origin=req.origin,
                turn_started_at=turn_t0,
                attempt=attempt,
                rerun_pending=_rerun_pending,
                spend=spend,
            )

        # Taken BEFORE the first attempt, because the loop appends to the list it is
        # handed: rebuilt afterwards it would carry the failed attempt's research, and
        # the whole point is to start again from the question.
        budgets = turn_budgets(turn_hook_meta)
        ask_kind = turn_ask_kind(turn_hook_meta)
        retries_left = budgets.dead_end_retries
        attempt_no = 1
        retry_seed = [dict(m) for m in initial_messages] if retries_left else None
        turn_t0 = monotonic()
        pending: dict[str, Any] = {"rerun": False, "reasons": []}
        live: dict[str, list[dict]] = {"messages": initial_messages}

        def _seed_for_the_rerun(msgs: list[dict]) -> list[dict]:
            """The question again, plus whatever the reader said while the attempt ran.

            The rerun exists to discard an attempt's research, and a correction typed
            mid-turn is not research: the loop took it off the inject queue, which will
            not offer it twice, so a seed rebuilt from the question alone silently
            un-asks it. Read off the whole finished list rather than its tail, because
            compaction may have moved the tail, and rebuilt from the untouched snapshot
            each time, so carrying one forward twice cannot double it.
            """
            return [dict(m) for m in retry_seed] + [dict(m) for m in msgs if m.get(_MID_TURN_USER_KEY)]

        def _dead_reasons(final_content: str | None, msgs: list[dict], status: str) -> list[str]:
            # The scaffolding goes before the reading. This question used to be asked
            # after the attempt returned, on the list it had already been dropped from,
            # and it is asked inside the attempt now; a recovery nudge left sitting at
            # the end would otherwise become what the turn is judged to have ended on.
            turn = [m for m in msgs[turn_start_idx:] if not any(m.get(k) for k in _TURN_TRANSIENT_KEYS)]
            reasons = dead_reasons(messages=turn, final_content=final_content, status=status, ask_kind=ask_kind)
            if budgets.dead_end_reasons:
                reasons = [r for r in reasons if r.startswith(budgets.dead_end_reasons)]
            return reasons

        def _clock_spent() -> bool:
            return bool(budgets.wall_clock_seconds) and (monotonic() - turn_t0) >= budgets.wall_clock_seconds

        def _rerun_pending(final_content: str | None, msgs: list[dict], status: str) -> bool:
            # The turn's one rerun decision, made where the attempt ends. The loop
            # below reads the answer rather than working it out again, so the seam
            # that skipped a salvage on a yes here cannot then meet a no there and
            # leave the turn with neither.
            reasons = _dead_reasons(final_content, msgs, status)
            # Every attempt spends the one turn clock, so a rerun gets what the
            # attempts before it left rather than a fresh copy. A turn whose budget
            # is already gone stops with what it has rather than starting an attempt
            # that would break on its first iteration -- and, saying so here, keeps
            # the salvage it would otherwise have skipped for a rerun it cannot run.
            spent = bool(reasons) and _clock_spent()
            if spent:
                logger.info("Dead end ({}); not re-running: the turn's clock is spent", ", ".join(reasons))
            pending.update(rerun=bool(retries_left) and bool(reasons) and not spent, reasons=reasons)
            return pending["rerun"]

        try:
            # The tier this turn dispatches sub-agents at, frozen here for the
            # same reason the iteration cap is read once: a switch arriving mid-turn
            # lands on the next turn, not on a sub-agent this one has yet to call.
            # And its attachments, for the same reader: a dispatch that names a
            # file the user attached is handing it over, one that names any other
            # .pptx is not, and only the turn knows which is which.
            with turn_tier(self.session_tier(key)), turn_attachments(req.media), spend.collecting():
                final_content, _, all_msgs, outcome = await _attempt(initial_messages, attempt_no)
                # The conditional rerun. A dead turn has no answer to damage -- "empty
                # implies wrong" is a scoring rule, so the count of right answers among
                # dead turns starts at zero and a second attempt can only raise it. It
                # re-runs from the original question rather than salvaging the failed
                # attempt, because salvage was measured to turn a detectable zero into a
                # confident wrong answer and to empty this trigger at the same time.
                # Unreachable for an agent whose hooks leave no budget, which is every
                # agent but the one that asked for it.
                #
                # The second half of the condition is the answer the attempt that just
                # ended gave; the first is the bound, kept here rather than left to a
                # callback, because a loop whose only exit is a value someone else
                # writes has no exit a reader of this loop can check. The two read one
                # counter for two questions: this one asks how many reruns are left to
                # run, the seam asks whether the attempt it is ending has one after it,
                # which is what decides that attempt's salvage.
                while retries_left > 0 and pending["rerun"]:
                    retries_left -= 1
                    attempt_no += 1
                    logger.info("Dead end ({}); re-running the turn", ", ".join(pending["reasons"]))
                    if on_progress is not None:
                        # Neutral about what the turn was doing and about which
                        # attempt this is: the loop serves every agent, and the
                        # budget allows more reruns than the one.
                        await on_progress("That attempt produced no answer; running the turn again.")
                    final_content, _, all_msgs, outcome = await _attempt(_seed_for_the_rerun(all_msgs), attempt_no)
                # Session-level because this turn may persist no assistant row at
                # all -- a turn whose whole budget went to reasoning has no
                # message to hang a record on. Stamped with the index this turn's
                # rows start at, so a reader can tell the fact apart from an
                # earlier turn's.
                #
                # Written OR cleared every turn, which is what actually makes it
                # turn-scoped: the index alone would only be enough if it never
                # went backwards, and `Session.clear()` (what `/new` calls) resets
                # it while `undo_last_turn` rewinds it, neither touching metadata.
                # An old marker could then sit at an index a later turn's own
                # start satisfies, and that turn would be reported as cut -- a
                # false fact, which is worse than the missing one this exists to
                # supply.
                #
                # Ahead of the answerless exit below rather than after it: the
                # turn most likely to have been cut at the ceiling is the one that
                # came back with nothing, and left downstream of the raise that
                # turn both loses its own marker and leaves an earlier turn's
                # standing -- which is exactly the reading ACP's ``max_tokens``
                # refinement makes.
                if turn_hook_meta.get("output_limited"):
                    session.metadata["output_limit_turn_at"] = prev_len
                else:
                    # None rather than dropping the key: a save merges its
                    # metadata over the record on disk, so a key left unsaid is
                    # kept rather than cleared (SessionManager._metadata_to_write).
                    # The reader asks whether this is an int, which None is not.
                    session.metadata["output_limit_turn_at"] = None
                # The loop gave up on the model. Its ladder, its rerun and its
                # salvage have all had their say, so what is left is a turn with
                # no answer: the failure the handler below files and the lane
                # reports, not a reply for the outlets to deliver.
                mt = self.tools.get("message")
                answered = isinstance(mt, MessageTool) and mt.sent_in_turn
                if not answered:
                    if outcome.error:
                        raise AnswerlessTurnError(outcome.error)
                    if final_content is None:
                        # The other way a turn arrives here with nothing: recovery
                        # switched off, so the empty response was taken at its word
                        # and no budget was ever spent. Same fact as the exit above
                        # -- the model said nothing -- so it is reported the same
                        # way rather than dressed as a reply.
                        raise AnswerlessTurnError(_NO_CONTENT_UNRECOVERED)
        except asyncio.CancelledError:
            # A stop is not a failure, but it is also not amnesia: what already
            # streamed is work the reader saw, so it lands in the session with a
            # note saying a person ended the turn. Then the cancel proceeds.
            self._save_broken_turn(
                session,
                live["messages"],
                persist_from,
                None,
                streamed,
                status="cancelled",
            )
            raise
        except AnswerlessTurnError as exc:
            # The attempt's own list rather than `live["messages"]`: a window pass
            # that rebound `messages` mid-turn leaves `live` on the seed, and the
            # marker would then be filed with none of this turn's work between the
            # question and itself. `all_msgs` is what the healthy path persists.
            self._save_broken_turn(session, all_msgs, persist_from, None, streamed, status="failed", reason=str(exc))
            raise
        except Exception as exc:
            self._save_broken_turn(
                session,
                live["messages"],
                persist_from,
                None,
                streamed,
                status="failed",
                # Bounded the way the lane bounds the same crash for its event:
                # an arbitrary exception message is filed in a session a reader
                # and a model both read back.
                reason=bound_failure_text(str(exc)),
            )
            raise
        self._stash_recovery(key, outcome)

        if final_content is None:
            # Only a turn a tool already answered reaches this now: the guard
            # above fails an unanswered one. What follows is the record, the
            # after_send chain and the log line, and each of them wants a string.
            final_content = NO_RESPONSE_FALLBACK

        # AgentHook ``after_send`` chain — typically a Sentinel
        # NudgeInjector / response_modifier modifying the outbound text. Skip it
        # for system-originated turns (Sentinel / subagent) so their reply
        # doesn't get a nudge layered on. A menu pick is USER (its reply IS the
        # user's intent — runs after_send); the Sentinel supersede notice is
        # SENTINEL and a subagent result is SUBAGENT (both skip).
        skip_after_send = origin in _SKIP_AFTER_SEND_ORIGINS
        if len(self.hooks) > 0 and not skip_after_send:
            from raven.agent.hook import AgentHookContext

            _send_ctx = AgentHookContext(
                session_key=key,
                outbound_content=final_content,
                session_history=session.messages[:prev_len],
                metadata=turn_hook_meta,
            )
            _send_decision = await self.hooks.after_send(_send_ctx)
            if _send_decision.modified_content is not None:
                if hook_sink is not None:
                    hook_sink["appended"] = _appended_by_hook(final_content, _send_decision.modified_content)
                final_content = _send_decision.modified_content

        # A hook chain's per-turn observer record files onto the turn's last
        # substantive assistant message at persist time -- after the send fire,
        # so a stash from any phase, ``after_send`` included, reaches the filed
        # record. Per turn, on the turn -- not a session-wide last write.
        if len(self.hooks) > 0:
            _stamp_turn_observers(all_msgs, turn_hook_meta, turn_start_idx)

        self._save_turn(session, all_msgs, persist_from)
        self.sessions.save(session)
        await self.harness.memory.after_turn(
            key,
            {
                "final_content": final_content,
                "messages": all_msgs[turn_start_idx:],
            },
        )
        # Plugin-side indexing (third peer step in the after-turn pipeline).
        self._dispatch_backend_store(key, session.messages[prev_len:])
        # Forward source-qualified skill-usage feedback. Only
        # ``everos/`` prefix is forwarded to the plugin; static-library
        # sources (``local`` / ``mass``) have no feedback channel.
        await self._dispatch_backend_feedback(
            key,
            self._collect_injected_skill_ids(selected_skills),
        )
        if not self.harness.memory.owns_compaction:
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        # ── Step 4: post-action learning (background, non-blocking) ─────────────
        # Skip for a subagent result re-injection (see the pre-turn flow above):
        # its content is a system-generated announce, not user input to learn from.
        if self.personalization_enabled and origin is not Origin.SUBAGENT:
            from raven.agent.personalizer import Personalizer

            _p4 = Personalizer(MemoryStore(self.workspace), self.provider, self.model)

            async def _post_learn():
                await _p4.post_learn(content, final_content)

            _t4 = asyncio.create_task(_post_learn())
            self._consolidation_tasks.add(_t4)
            _t4.add_done_callback(self._consolidation_tasks.discard)
        # ── End Step 4 ──────────────────────────────────────────────────────

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt.sent_in_turn:
            # Defensive fingerprint. A silent return None would leave no
            # trace when the agent replied via the message tool, making
            # stochastic dud-turn bugs invisible to grep. Log
            # the would-be response so future investigations have a trail
            # parallel to "Response to ..." below.
            if final_content:
                preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
                logger.info(
                    "MessageTool sent in turn for {}:{}: {}",
                    channel,
                    sender_id,
                    preview,
                )
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", channel, sender_id, preview)
        return (final_content, [])

    def _save_broken_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        received_at: str | None,
        streamed: dict[str, str],
        *,
        status: str,
        reason: str | None = None,
        inbound_original: str | None = None,
    ) -> None:
        """Persist what a cancelled or failed turn got as far as producing.

        The tail of the turn plus two kinds of repair, then one closing marker:

        - an assistant message whose tool calls never got results gains a
          synthetic ``[interrupted]`` result per open call, because a stored
          history with an unanswered tool call is one strict providers reject
          on the next turn;
        - the text that streamed after the last committed message is saved as
          its own assistant message -- it was on the reader's screen, and the
          loop only commits a message once the provider call returns;
        - the marker entry carries ``turn_ended`` so a client can say WHY the
          transcript stops there, and readable text so the model knows the same.
          The two are worded for their own reader: ``turn_ended.reason`` keeps
          the provider's account for the person diagnosing it, while the text
          the model reads back names the category only (see
          ``_marker_failure``).

        ``received_at`` and ``inbound_original`` describe the turn's question,
        which ``_process_message`` files before the attempt starts and hands
        this one a ``skip`` that begins after it: both arrive as None from
        there. They stay on the signature because what this rescues is the tail
        of an arbitrary message list, and a caller whose list still opens on an
        unfiled inbound needs them the way the healthy path does -- a hook's
        ``modified_content`` rewrite shapes only what the model saw, and a
        cancelled turn must not be the door through which the rewritten
        envelope enters the persisted history.

        Never raises: this runs on the way out of a dying turn, and a rescue
        that throws replaces one loss with another.
        """
        try:
            tail: list[dict] = [dict(m) for m in messages[skip:]]
            open_calls: dict[str, str] = {}
            for m in tail:
                if m.get("role") == "assistant":
                    for tc in m.get("tool_calls") or []:
                        cid = str(getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else "") or "")
                        if cid:
                            name = getattr(getattr(tc, "function", None), "name", None) or (
                                (tc.get("function") or {}).get("name") if isinstance(tc, dict) else None
                            )
                            open_calls[cid] = str(name or "tool")
                elif m.get("role") == "tool":
                    open_calls.pop(str(m.get("tool_call_id") or ""), None)
            for cid, name in open_calls.items():
                tail.append(
                    {
                        "role": "tool",
                        "tool_call_id": cid,
                        "name": name,
                        "content": "[interrupted] this call never returned",
                    }
                )
            text = (streamed.get("text") or "").strip()
            if text and not any(
                m.get("role") == "assistant" and str(m.get("content") or "").strip() == text for m in tail
            ):
                partial: dict[str, Any] = {"role": "assistant", "content": streamed["text"]}
                if (thought := (streamed.get("thought") or "").strip()) and not any(
                    str(m.get("reasoning_content") or "").strip() == thought for m in tail
                ):
                    partial["reasoning_content"] = streamed["thought"]
                tail.append(partial)
            word = "cancelled by the user" if status == "cancelled" else f"failed: {_marker_failure(reason)}"
            marker: dict[str, Any] = {
                "role": "assistant",
                "content": f"(turn {word})",
                "turn_ended": {"status": status, **({"reason": reason} if reason else {})},
            }
            tail.append(marker)
            self._save_turn(session, tail, 0, received_at=received_at, inbound_original=inbound_original)
            self.sessions.save(session)
        except Exception:  # noqa: BLE001 - see docstring
            logger.opt(exception=True).warning("could not persist the broken turn for {}", session.key)

    def _record_inbound(self, session: Session, content: str, origin: "Origin | None", timestamp: str) -> None:
        """Persist the inbound entry of a turn that answers before ``_save_turn``.

        The personalization flow can reply with a clarifying question and return,
        so the envelope the assembler would have marked is never built and the
        turn's user entry reaches disk from here instead. Both paths that do it
        go through this one, because two copies of the same write is how one of
        them came to be marked and the other not.

        Writes ``origin`` rather than ``_origin``: the underscore exists so a
        mark stays out of the live provider payload until ``_save_turn`` renames
        it, and nothing here is going to a provider.
        """
        entry: dict[str, Any] = {"role": "user", "content": content, "timestamp": timestamp}
        if mark := _runtime_origin(origin):
            entry["origin"] = mark
        session.record(entry)

    def _file_unanswered(self, messages: list[dict]) -> None:
        """Append one notice for questions this turn could not put to anyone."""
        from raven.permissions.turn import UNANSWERED_KIND, unanswered_filing

        filing = unanswered_filing()
        if filing is None:
            return
        for message in messages:
            notice = message.get(_NOTICE_KEY) or message.get("notice")
            if isinstance(notice, dict) and notice.get("kind") == UNANSWERED_KIND:
                return
        text, notice = filing
        messages.append({"role": "assistant", "content": text, _NOTICE_KEY: notice})

    def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        *,
        inbound_original: str | None = None,
        received_at: str | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results.

        ``received_at`` is the wall clock at which the turn's inbound message
        arrived, and it is the opening write that passes one: the turn's other
        writes run after work that took time, so stamping every entry "now"
        would give the user message and the final answer the same timestamp --
        and a restored transcript reads the gap between those two as the turn's
        duration.
        """
        # Both exits that persist a turn come through here, so a question noted
        # on the turn is filed once whichever exit runs. The early inbound write
        # also comes through, before any question, and files nothing.
        self._file_unanswered(messages)
        first_user_pending = received_at is not None
        # The turn's first user entry is the inbound message; hooks may have
        # rewritten what the model saw (a memo prepended, a reminder appended),
        # and persisting that view would compound it into every later window.
        # Same rule as the runtime-context strip below: history keeps the words
        # the user sent. Block-shaped inbounds pass through as built.
        inbound_rewrite_pending = inbound_original is not None
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if first_user_pending and role == "user":
                entry.setdefault("timestamp", received_at)
                first_user_pending = False
            if entry.get("_recovery_synthetic"):
                continue  # #1a synthetic recovery nudge — never persist scaffolding
            if entry.get(ATTACHED_IMAGE_KEY):
                # Already filtered upstream; kept because this is the last gate
                # before a write that cannot be undone, unlike the code above it.
                continue
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if turn_origin := entry.pop(_ORIGIN_KEY, None):
                entry["origin"] = turn_origin
            if delegated := entry.pop(_DELEGATED_KEY, None):
                # Same rename as the origin below, for the same reason: without
                # it a reload draws a delegated result as a question the user
                # asked, fence and all. The origin says WHO opened the turn;
                # the delegated identity says WHICH run came back, so the two
                # coexist.
                entry["delegated"] = delegated
            if entry.pop(_MID_TURN_USER_KEY, None):
                # Same rename as the origin above: the underscore kept it out of
                # the provider payload, the plain name is what session.resume
                # puts on the wire so a reload draws this message inside the
                # turn it was merged into rather than as one of its own.
                entry["mid_turn"] = True
            if notice := entry.pop(_NOTICE_KEY, None):
                # Same rename as the diff below, for the same reason. Without
                # it a reload draws this runtime prose as the model's answer,
                # so the live view and the restored one disagree about who
                # spoke.
                entry["notice"] = notice
            if tool_diff := entry.pop("_diff", None):
                # Renamed for storage: the private spelling kept it out of the
                # live provider payload, the plain one is what session.resume
                # maps onto the wire so a reloaded page can renumber the change.
                entry["diff"] = tool_diff
            if tool_removed := entry.pop("_file_removed", None):
                # Renamed for storage for the same reason as the diff above: the
                # plain name is what session.resume puts on the wire, so a
                # reloaded page draws the deletion the live view drew.
                entry["file_removed"] = tool_removed
            if tool_written := entry.pop("_file_written", None):
                # Renamed for storage for the reason the removals above are: what
                # a command wrote has no other record, so without this a reloaded
                # page shows a turn whose commands changed nothing.
                entry["file_written"] = tool_written
            if tool_metadata := entry.pop(_TOOL_METADATA_KEY, None):
                entry["metadata"] = tool_metadata
            # Provenance of pictures that lived for this turn only; nothing to file.
            entry.pop(IMAGE_SOURCES_KEY, None)
            # A withdrawn picture's note is filed without this turn's reasons: on
            # the tool-result transport the message outlives the turn, and a
            # resumed session would otherwise replay "this turn's pictures outgrew
            # their budget" as current.
            if isinstance(content, list):
                content = entry["content"] = [
                    {**part, "text": filed_image_note(part["text"])}
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                    else part
                    for part in content
                ]
            for private_key, stored_key in (
                (_REASONING_MS_KEY, "reasoning_ms"),
                (_TOOL_DURATION_MS_KEY, "duration_ms"),
            ):
                # Renamed for the same reason as the diff above. A duration is a
                # property of the part it measures, so it is written down with
                # it: a clock started when a page loads can only ever guess.
                if (took := entry.pop(private_key, None)) is not None:
                    entry[stored_key] = took
            if role == "tool" and isinstance(content, list):
                # A multimodal tool result. Images must never reach the JSONL:
                # a single one adds megabytes that are then replayed on every
                # resume and re-fed to the model, and unlike a code bug that is
                # not revertible once written. The char cap below cannot catch
                # it either — it guards `str` content only.
                content = _strip_inline_images(content)
                entry["content"] = content
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[: self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if inbound_rewrite_pending:
                    inbound_rewrite_pending = False
                    if isinstance(entry.get("content"), str) and entry["content"] != inbound_original:
                        entry["content"] = inbound_original
                if isinstance(content, list):
                    filtered = [
                        c
                        for c in _strip_inline_images(content)
                        if not (
                            isinstance(c, dict)
                            and c.get("type") == "text"
                            and isinstance(c.get("text"), str)
                            and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
                        )
                    ]
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", self._now_fn().isoformat())
            session.record(entry)
        session.updated_at = self._now_fn()

    async def _run_turn(
        self,
        req: TurnRequest,
        emit: Emit,
        drain: Drain,
        *,
        stream: bool = True,
        usage_sink: dict[str, Any] | None = None,
        text_sink: dict[str, Any] | None = None,
    ) -> "TurnOutcome":
        """Spine-native turn entry: consume a TurnRequest, fan the agent's output
        onto the single ``emit``, return a TurnOutcome. Collapses the legacy
        output paths (a str return + the five callbacks) onto one boundary.

        Named ``run_turn`` rather than ``run``: ``run`` is the runtime keep-alive
        (executor / debug server / MCP up, then idle). A spine runner calls the
        public ``run_turn`` to satisfy the TurnRunner protocol.

        ``stream`` is the reply-assembly switch: a streaming outlet (TUI) wires
        it True so the reply goes out as StreamDelta and dissolves with no
        trailing Text; a non-streaming outlet (REPL) wires it False so the reply
        is one Text. It gates both LLM callbacks (the loop streams when either is
        wired) and the message-tool routing, so the whole reply travels one way.

        Exceptions propagate so the lane turns them into TurnFailed — run_turn
        does not catch sandbox-init to return an error string (the legacy direct
        path did; the spine surfaces it as a TurnFailed event instead).

        ``usage_sink`` lets a caller observe the turn's full token accounting
        (cost / context, richer than the three-field LoopOutcome.usage): pass a
        dict and it is filled. The TUI passes one to attach the rich usage to
        message.complete; the REPL omits it and uses LoopOutcome.usage.

        ``text_sink`` is its sibling for the reply text: pass a dict and the
        final reply lands in text_sink["text"] (the reply still goes out via
        emit — this is an observation copy, not a second delivery). cron passes
        one so its system event can tell the heartbeat what the run produced.
        Both sinks are transitional, to retire together when taps lands (a
        read-only observer of the turn's output).

        ``drain`` pulls user messages injected mid-turn (BusyPolicy.INJECT); it
        is threaded into the agent loop and consumed at the top of each iteration.
        """
        from raven.agent.subagent.direct_chat import DirectChatError
        from raven.proactive_engine.schedulers.cron.tool import CronTool
        from raven.spine.events import (
            EpisodeStart,
            MediaOut,
            Notice,
            NoticeKind,
            Reasoning,
            StreamDelta,
            Text,
            ToolEvent,
            ToolPhase,
            Usage,
        )
        from raven.spine.message import Media
        from raven.spine.runner import TurnOutcome

        cid = req.conversation or f"{req.source.channel}:{req.source.chat_id}"

        # Direct sub-agent turn (direct_target -- see TurnRequest). Deliberately
        # asymmetric with the deliver_text branch below: that one persists to the
        # session before emitting, this one persists nothing. The whole purpose
        # of a direct chat is that the main agent's transcript does not carry it;
        # the turn's evidence is its record directory, and what the main agent
        # eventually learns is the handoff block, not these messages.
        if req.direct_target is not None:
            agent, handle = req.direct_target
            # The lane is this instance's, so that a direct chat runs concurrently
            # with the main agent's turn and with every other instance's. Records,
            # the instance registry and the handoff are the *session's* though --
            # keyed by the lane they would scatter one instance's history into a
            # directory of its own and land the handoff on a conversation nobody
            # reads. See ``raven.spine.turn.session_of``.
            session_key = session_of(cid)
            streamed_direct = False

            async def on_direct_delta(text: str) -> None:
                # Text only, never Reasoning: the wire tags an instance on four
                # event types and thinking.delta is not one of them, so a direct
                # chat's reasoning would be rendered into the main transcript.
                nonlocal streamed_direct
                if not text:
                    return
                streamed_direct = True
                await emit(StreamDelta(delta=text))

            try:
                reply, meta = await self.subagents.chat(
                    session_key=session_key,
                    agent=agent,
                    handle=handle,
                    text=req.text,
                    media=req.media,
                    # The session's working directory, the same one this turn's
                    # own tools would get and the same one `spawn` captures. The
                    # binding is not set here -- `workdir.bind` wraps the main
                    # turn body further down, which this branch returns before
                    # reaching -- so it is resolved rather than read. Omitting it
                    # fell back to agent home (`~/.raven/workspace`), which is
                    # raven's memory and skills rather than the work: a direct
                    # chat about the checkout the user is sitting in ran `git
                    # status` against raven's own home and answered about that.
                    workspace=self.session_workdir(session_key),
                    # A non-streaming outlet (the REPL) gets no deltas to
                    # assemble, exactly as it gets none from a normal turn.
                    on_delta=on_direct_delta if stream else None,
                )
            except DirectChatError as exc:
                self._direct_handoff.record(session_key, exc.meta)
                raise
            self._direct_handoff.record(session_key, meta)
            # Same rule as the main path below: what streamed is
            # already on screen, so a closing Text would render the reply twice.
            # An instance whose transport cannot stream never sets the flag and
            # is delivered whole, which is what every direct chat did before.
            if not streamed_direct:
                await emit(Text(content=reply))
            return TurnOutcome(
                usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                explicit_reply=True,
            )

        # Verbatim delivery (deliver_text — see TurnRequest). Persist before
        # emit, mirroring the normal turn's save-then-reply order, so a save
        # failure never leaves the user a delivered message no turn recorded.
        # The after-turn work a normal turn does in _process_message is reduced
        # to what a no-model delivery needs: backend.store indexes the message;
        # after_turn is a no-op without a turn_id; consolidation is the curator's
        # job on its next assemble.
        if req.deliver_text is not None:
            session = self.sessions.get_or_create(cid)
            msg = {"role": "assistant", "content": req.deliver_text}
            prev_len = len(session.messages)
            self._save_turn(session, [msg], 0)
            self.sessions.save(session)
            self._dispatch_backend_store(cid, session.messages[prev_len:])
            await emit(Text(content=req.deliver_text))
            return TurnOutcome(
                usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                explicit_reply=True,
            )

        # A direct chat is invisible to the main agent by design, so this is
        # where it finds out one happened: pointers to what was asked and
        # answered, never the text. Take-and-clear, so a segment is reported
        # once. Nothing pending returns immediately -- this runs on every turn.
        handoff = self._direct_handoff.take(cid)
        if handoff is not None:
            req = replace(req, text=f"{handoff}\n\n{req.text}")

        streamed = False
        hook_sink: dict[str, str] = {}

        async def on_token(text: str) -> None:
            nonlocal streamed
            if not text:
                return
            streamed = True
            await emit(StreamDelta(delta=text))

        async def on_reasoning(text: str) -> None:
            if text:
                await emit(Reasoning(content=text))

        async def on_episode(index: int) -> None:
            await emit(EpisodeStart(index=index))

        async def on_tool(phase: str, info: dict[str, Any]) -> None:
            if phase == "start":
                await emit(
                    ToolEvent(
                        phase=ToolPhase.START,
                        tool_call_id=info["tool_call_id"],
                        name=info["name"],
                        arguments=info["arguments"],
                        blocking=bool(info.get("blocking")),
                        display=info.get("display"),
                    )
                )
            else:
                await emit(
                    ToolEvent(
                        phase=ToolPhase.COMPLETE,
                        tool_call_id=info["tool_call_id"],
                        result_preview=info["result_preview"],
                        truncated=info["truncated"],
                        ok=bool(info.get("ok", True)),
                        metadata=info.get("metadata"),
                        diff=info.get("diff"),
                        file_change=info.get("file_change"),
                        file_removed=info.get("file_removed"),
                        file_written=info.get("file_written"),
                    )
                )

        async def on_progress(text: str, tool_hint: bool = False) -> None:
            # Keep the progress/tool-hint distinction so an outlet can gate each on
            # its own config flag (send_progress vs send_tool_hints), as the bus
            # path did — tool-hint text rides NoticeKind.TOOL_HINT, progress rides
            # PROGRESS. Outlets that don't render either eat both kinds anyway.
            if text:
                await emit(
                    Notice(
                        kind=NoticeKind.TOOL_HINT if tool_hint else NoticeKind.PROGRESS,
                        detail=text,
                    )
                )

        async def on_notice(kind: NoticeKind, detail: str) -> None:
            await emit(Notice(kind=kind, detail=detail or None))

        async def _emit_media(paths: list[str]) -> None:
            await emit(
                MediaOut(media=tuple(Media(path=p, mime="application/octet-stream", kind="file") for p in paths))
            )

        # Route the message tool's reply through the token stream so a
        # tool-driven reply streams like the main response; _process_message
        # then returns None, so the boundary below emits nothing for it. The
        # callback is turn-local (a ContextVar in MessageTool), so a concurrent
        # turn cannot clobber this turn's routing — no save/restore needed.
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):

            async def _route_to_stream(content: str, media: list[str]) -> None:
                # A message-tool reply can attach media; emit it independently so
                # it is not dropped (_process_message returns None for a tool reply
                # so the boundary below never sees it). The content follows the same
                # stream switch as the main reply: StreamDelta when streaming, one
                # Text otherwise — else a non-streaming outlet would eat the delta.
                if media:
                    await _emit_media(media)
                if text_sink is not None and content:
                    text_sink["text"] = content
                if stream:
                    await on_token(content)
                elif content:
                    await emit(Text(content=content))

            message_tool.set_send_callback(_route_to_stream)

        # A CRON turn must not let the agent schedule new cron jobs mid-run. The
        # CronTool guards via a ContextVar; set it here, in the lane task that runs
        # the turn, so it propagates to the tool — the cron callback sets it in a
        # different task that never reaches this one.
        cron_tool = self.tools.get("cron")
        cron_token = None
        if req.origin is Origin.CRON and isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)

        if usage_sink is None:
            usage_sink = {}
        try:
            # Resolve inside the try so a bad persisted override (deleted
            # directory, permission change) still releases the cron token
            # below via the outer finally, instead of stranding it.
            try:
                turn_workdir = self.session_workdir(cid)
            except Exception as exc:
                raise RuntimeError(
                    "Session working directory is invalid; clear this session's working "
                    "directory override from the web UI to recover."
                ) from exc
            # Bind the session's working directory around the whole turn body (not
            # just the _set_tool_context call inside _process_message) so every
            # path-aware tool sees it, including on the exception and cancellation
            # paths below -- workdir.bind's finally always resets the ContextVar.
            from raven.token_wise import usage_context

            with (
                workdir.bind(turn_workdir),
                usage_context.bind(cid, self.sessions.get_or_create(cid).metadata.get("usage_owner", {})),
            ):
                try:
                    await self._start_executor()
                    # Fire and forget: a turn must not wait on a handshake, and
                    # every host that serves turns without run() reaches MCP
                    # through here (the TUI has no other path at all, and this is
                    # also the reconnect after close_mcp and the retry after a
                    # prewarm that failed). Idempotent, so an in-flight connect is
                    # not restarted. Once connected, the reconcile beside it picks
                    # up an out-of-band edit of tools.mcpServers the same way --
                    # the turn boundary is the one place every host passes.
                    self.prewarm_mcp()
                    self.reconcile_mcp_from_live()
                    out = await self._process_message(
                        req,
                        session_key=cid,
                        on_progress=on_progress,
                        on_token_delta=on_token if stream else None,
                        on_reasoning_delta=on_reasoning if stream else None,
                        on_tool_event=on_tool,
                        on_episode_start=on_episode if stream else None,
                        on_notice=on_notice,
                        usage_sink=usage_sink,
                        origin=req.origin,
                        drain=drain,
                        hook_sink=hook_sink,
                    )
                except AnswerlessTurnError:
                    # A turn the loop gave up on is not a crash: the executor and
                    # the servers it holds are fine, and the next turn needs them.
                    raise
                except Exception:
                    # Before the executor goes: the prewarm this turn started is
                    # still running, and a stdio handshake inside it is spawned
                    # into that executor. Closing under it lands the server in
                    # `error`, which no reload retries.
                    await self.reap_mcp_prewarm()
                    await self.close_executor()
                    raise
        finally:
            if cron_token is not None and isinstance(cron_tool, CronTool):
                cron_tool.reset_cron_context(cron_token)

        # Single return->emit boundary. MediaOut is independent of the
        # stream and precedes Text (the current order is media-first).
        if out is not None:
            reply_content, reply_media = out
            if reply_media:
                await _emit_media(reply_media)
            if not streamed and reply_content:
                await emit(Text(content=reply_content))
            elif streamed and hook_sink.get("appended"):
                # The reply left as deltas before the after_send chain ran, so what a
                # hook appended -- a deck engine's "Deck: / Preview: / MEDIA:" lines,
                # a nudge -- reached no streaming surface: not the web page, not the
                # TUI, not an ACP client. Sent as one more delta, ahead of
                # message.complete, so the reply the client holds is the reply.
                await emit(StreamDelta(delta=hook_sink["appended"]))
            if text_sink is not None and reply_content:
                text_sink["text"] = reply_content

        usage = Usage(
            prompt_tokens=int(usage_sink.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage_sink.get("completion_tokens", 0) or 0),
            total_tokens=int(usage_sink.get("total_tokens", 0) or 0),
        )
        # A message-tool reply returns None from _process_message but did reply,
        # so it counts as an explicit reply too.
        replied_via_tool = isinstance(message_tool, MessageTool) and message_tool.sent_in_turn
        return TurnOutcome(usage=usage, explicit_reply=out is not None or replied_via_tool)
