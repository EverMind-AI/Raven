"""The turn execution path: dispatch, the agent loop, streaming, recovery,
persistence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from raven.agent.loop import compaction
from raven.agent.loop._shared import (
    _ABORTED_ACTION_REPLY,
    _ATTACHED_IMAGE_KEY,
    _DELEGATED_KEY,
    _HOOK_INJECTED_KEY,
    _IMAGE_SOURCES_KEY,
    _MAX_ITER_STATIC_FALLBACK,
    _MAX_ITER_SYNTHESIS_PROMPT,
    _NOTICE_KEY,
    _ORIGIN_KEY,
    _REASONING_MS_KEY,
    _SKIP_AFTER_SEND_ORIGINS,
    _SKIP_USER_INBOUND_ORIGINS,
    _TOOL_DURATION_MS_KEY,
    _TOOL_METADATA_KEY,
    _TOOL_PREVIEW_MAX_CHARS,
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
    Origin,
    RecoveryAction,
    Session,
    _appended_by_hook,
    _display_label,
    _file_change_payload,
    _first_line,
    _image_sources,
    _inline_image_bytes,
    _runtime_origin,
    _stamp_reasoning_ms,
    _strip_inline_images,
    _withdrawn_image_note,
    append_hook_note,
    asyncio,
    autofill_resolver,
    classify_empty_response,
    current_autofill,
    estimate_prompt_tokens,
    failure_class,
    filed_image_note,
    image_placeholder_text,
    is_hard_tool_failure,
    is_image_part,
    is_only_think_debris,
    json,
    logger,
    loop_break_nudge,
    replace,
    resolve_context_window,
    resolve_max_output_tokens,
    semconv,
    session_of,
    stream_llm_call,
    strip_think_blocks,
    time,
    trace,
    turn_question,
    uuid4,
    workdir,
)
from raven.agent.loop.recovery import ContinuationGate, cut_reasoning_head
from raven.agent.tools.registry import call_failed
from raven.permissions.turn import set_current_tool_call_id
from raven.providers.tool_calls import openai_tool_call

if TYPE_CHECKING:
    from raven.agent.loop.checkpoint import CheckpointService
    from raven.contracts.token_strategy import UsageSnapshot
    from raven.spine.events import NoticeKind
    from raven.spine.runner import Drain, Emit
    from raven.spine.turn import TurnRequest


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

    @classmethod
    def _emergency_shrink(cls, messages: list[dict]) -> tuple[list[dict], int]:
        """Elide the bodies of older tool-result messages to fit a tighter window.

        Mid-turn context overflow is almost always accumulated tool output, so
        replacing the content of all but the most recent few ``role="tool"``
        messages with a short placeholder frees the most tokens while keeping
        system / user / assistant reasoning intact. Deterministic, no extra LLM
        call. Returns ``(new_messages, num_elided)``; ``num_elided == 0`` means
        there was nothing worth eliding (caller should not bother retrying).

        Three passes, cheapest loss first: the pictures tools showed, then older
        tool bodies, then -- only when those two freed nothing -- the pictures
        the user sent, all but the newest.
        """
        messages, elided = cls._elide_older_images(messages)

        placeholder = "[earlier tool output elided to fit the context window]"
        tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        shrunk = messages
        if len(tool_idxs) > cls._SHRINK_KEEP_RECENT_TOOL_RESULTS:
            elide = set(tool_idxs[: -cls._SHRINK_KEEP_RECENT_TOOL_RESULTS])
            shrunk = []
            for i, m in enumerate(messages):
                if i in elide and m.get("content") and m.get("content") != placeholder:
                    clean = dict(m)
                    clean["content"] = placeholder
                    shrunk.append(clean)
                    elided += 1
                else:
                    shrunk.append(m)
        if elided:
            return shrunk, elided
        # Nothing a result picture or a tool body could give back. The pictures the
        # user sent go last, newest kept: a turn that is nothing but pasted
        # screenshots overflows on them alone, and this path could reach them
        # before the standing window narrowed the first pass to results.
        out = list(shrunk)
        changed, _ = cls._window_images(out, cls._SHRINK_KEEP_RECENT_IMAGES, reason="context", any_role=True)
        return (out, changed) if changed else (shrunk, 0)

    @staticmethod
    def _demote_tool_images(messages: list[dict]) -> tuple[list[dict], int]:
        """Move images out of tool results into a following user message.

        The recovery for an endpoint that refuses a picture in ``role="tool"``.
        Produces exactly the message list a ``False`` capability verdict would
        have built in the first place, so the retry lands on the already-tested
        placeholder path rather than inventing a third shape.

        A run of consecutive tool messages is one batch answering one assistant
        message, so the pictures pulled out of it are attached once after the last
        of them -- putting one between two tool results leaves a tool_call
        unanswered where the API checks the sequence (measured, see the batching
        comment in the tool loop).

        Returns ``(new_messages, num_demoted)``; ``0`` means no tool result
        carried an image, so the refusal was about something else and the caller
        should not retry.
        """
        out: list[dict] = []
        pending: list[dict] = []
        pending_sources: list[dict] = []
        demoted = 0

        def flush() -> None:
            if pending:
                out.append(
                    {
                        "role": "user",
                        "content": list(pending),
                        _ATTACHED_IMAGE_KEY: True,
                        _IMAGE_SOURCES_KEY: list(pending_sources),
                    }
                )
                pending.clear()
                pending_sources.clear()

        for m in messages:
            content = m.get("content")
            if m.get("role") != "tool":
                flush()
                out.append(m)
                continue
            if not isinstance(content, list):
                out.append(m)
                continue
            images = [p for p in content if is_image_part(p)]
            if not images:
                out.append(m)
                continue
            clean = dict(m)
            clean["content"] = image_placeholder_text(content)
            sources = clean.pop(_IMAGE_SOURCES_KEY, None) or [{"tool": m.get("name")} for _ in images]
            out.append(clean)
            pending.extend(images)
            pending_sources.extend(sources)
            demoted += len(images)
        flush()
        return out, demoted

    @classmethod
    def _window_images(
        cls,
        messages: list[dict],
        keep: int,
        *,
        budget: int | None = None,
        reason: str = "superseded",
        any_role: bool = False,
    ) -> tuple[int, int]:
        """Withdraw the pictures the request should no longer carry, in place.

        Two modes. Without ``budget``, every image-bearing message but the newest
        ``keep`` loses its pictures: the shape the overflow path and the refusal
        ladder want. With ``budget`` (decoded bytes), nothing happens while the
        pictures still live in the transcript fit under it, and the moment they
        do not, every message but the newest ``keep`` loses its pictures at once.
        A collapse rather than a slide because each withdrawal is a break in the
        prefix an upstream cache can match: sliding one message out per new batch
        broke the prefix on 16 of 60 calls in one measured run (19 of 41 in the
        other) and the hit rate went from 84.5% to 64.7%; collapsing when the
        budget is hit breaks it once or a handful of times per deck, and the
        pictures stay in view for longer in between.

        In place: each withdrawn message is replaced in ``messages`` by a copy whose
        image parts have become notes (:func:`_withdrawn_image_note`), so what the
        model has been told about a picture it can no longer see is part of the
        transcript from then on, not something recomputed per request.

        Prefix-stable by construction. A message is touched only while it still
        carries a picture, so one withdrawn on an earlier iteration is byte for
        byte what it was; between two iterations either nothing changes or one
        collapse does. That is the property a cached prefix needs, and the reason
        this is not a pure function returning a fresh list.

        The pictures tools showed, on either transport: a ``tool`` message where
        the endpoint carries them there, the following ``user`` message the loop
        attached where it does not. A picture the user sent is the subject of the
        turn rather than a result, and stays -- unless ``any_role``, which is for
        the request the endpoint has already refused, when there is nothing else
        left to take out. Remote references count toward ``keep`` but weigh
        nothing in the budget: their size is unknowable without fetching them.

        Returns ``(messages_changed, pictures_withdrawn)``; ``(0, 0)`` means
        nothing had to go.
        """
        bearing = [
            i
            for i, m in enumerate(messages)
            if (any_role or m.get("role") == "tool" or m.get(_ATTACHED_IMAGE_KEY))
            and isinstance(m.get("content"), list)
            and any(is_image_part(p) for p in m["content"])
        ]
        if budget is not None:
            live = sum(_inline_image_bytes(p) for i in bearing for p in messages[i]["content"])
            if live <= budget:
                return 0, 0
        stale = bearing[:-keep] if keep else bearing
        pictures = 0
        for i in stale:
            m = messages[i]
            sources = m.get(_IMAGE_SOURCES_KEY) or []
            total = sum(1 for p in m["content"] if is_image_part(p))
            parts: list[Any] = []
            seen = 0
            for p in m["content"]:
                if not is_image_part(p):
                    parts.append(p)
                    continue
                source = sources[seen] if seen < len(sources) else {}
                seen += 1
                note = _withdrawn_image_note(source, index=seen, total=total, reason=reason, keep=keep)
                parts.append({"type": "text", "text": note})
            clean = dict(m)
            clean["content"] = parts
            messages[i] = clean
            pictures += total
        return len(stale), pictures

    @classmethod
    def _elide_older_images(cls, messages: list[dict]) -> tuple[list[dict], int]:
        """Drop pictures from all but the most recent image-bearing message, for the
        overflow path.

        Run before the tool-text pass because an image is by far the densest
        thing in the window -- one costs up to 1568 tokens, which is more than
        most tool outputs -- so dropping a stale picture buys more room than
        eliding several text results, and costs less of what the model still
        needs. The standing window (``_IMAGE_WINDOW_RECENT_MESSAGES``) has
        usually already done this; the tighter count here is for the turn whose
        window was not enough.

        A new list, like the rest of the overflow path: the caller rebinds.
        """
        out = list(messages)
        changed, _ = cls._window_images(out, cls._SHRINK_KEEP_RECENT_IMAGES, reason="context")
        return (out, changed) if changed else (messages, 0)

    async def _summarize_head(
        self, messages: list[dict], model: str | None, reasoning_effort: str | None = None
    ) -> tuple[list[dict], str]:
        """Replace the transcript head with one LLM-written handoff brief.

        The system prefix and the first user message never enter the summary,
        and a recent tail (``preserve_recent_tokens`` budget) stays verbatim so
        the model keeps its most recent working state. The summary runs on the
        turn's own provider and model: a pinned summary model would outlive a
        model switch and then route every summary to a retired endpoint. It
        also runs at the turn's own reasoning effort when the session policy
        names one -- a model call of the turn like any other; ``None`` passes
        nothing, so the provider's configured default stands.

        Returns ``(messages, verdict)`` with verdict one of ``"changed"``
        (head replaced), ``"failed"`` (a summary call was paid for and freed
        nothing -- the caller must count it against the shared retry budget or
        a failing endpoint would be paid once per iteration) or ``"skipped"``
        (no call was made: the head is too small to be worth one). Anything
        but ``"changed"`` returns the input untouched, so the caller degrades
        to pruning plus the existing overflow path and is never worse off
        than today.
        """
        cfg = self._compaction
        limit = self.context_window_tokens
        reserved = compaction.reserved_tokens(
            cfg.reserved_tokens,
            resolve_max_output_tokens(model or self.model, allow_fetch=False),
        )
        budget = compaction.tail_budget(cfg.preserve_recent_tokens, limit, reserved)
        split = compaction.select_split(messages, budget, estimate_prompt_tokens)
        protect_end = compaction.protected_prefix_end(messages)
        if split is None or protect_end is None:
            return messages, "skipped"
        transcript = compaction.render_transcript(messages[protect_end:split])
        try:
            response = await self.provider.chat(
                messages=[
                    {"role": "system", "content": compaction.SUMMARY_INSTRUCTIONS},
                    {"role": "user", "content": transcript},
                ],
                tools=None,
                model=model or self.model,
                max_tokens=compaction.SUMMARY_MAX_TOKENS,
                **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
            )
        except Exception as exc:
            logger.warning("Transcript head summary call raised: {}", exc)
            return messages, "failed"
        summary = (response.content or "").strip()
        if response.finish_reason == "error" or not summary:
            # The reason decides the fix (transcript too long vs endpoint
            # refusal vs empty completion), so record it.
            logger.warning(
                "Transcript head summary failed ({} head message(s), {} chars): {}",
                split - protect_end,
                len(transcript),
                str(response.content or "empty summary")[:300],
            )
            return messages, "failed"
        return compaction.build_compacted(messages, split, summary), "changed"

    async def _synthesize_final_on_exhaustion(
        self,
        messages: list[dict],
        model: str | None,
        fallback_models: list[str] | None,
        on_token_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """One tools-disabled LLM call to wrap up after the iteration budget runs out.

        Instead of returning a canned apology, ask the model to summarize what
        it accomplished and deliver its best partial answer. Tools are withheld
        (``tools=None``) so it cannot start another tool call — or an
        ``ask_user`` — at the cliff edge. Falls back to a static message if the
        call errors or comes back empty, so the turn is never left silent.

        When the turn caller wired streaming callbacks, this synthesized reply
        must stream too — otherwise it never reaches a streaming outlet: the
        run_turn boundary only emits a closing ``Text`` when nothing streamed,
        so a non-streamed wrap-up after an already-streamed turn gets dropped.
        """
        synth_messages = messages + [{"role": "user", "content": _MAX_ITER_SYNTHESIS_PROMPT}]
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
                "Max-iter synthesis returned no usable content (finish_reason={})",
                response.finish_reason,
            )
        except Exception as exc:
            logger.warning("Max-iter synthesis call failed: {}", exc)
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

    async def _run_agent_loop(
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
        """
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        effective_model = model or self.model

        # Track whether the turn was a normal exit or a
        # max-iter interruption. ``status`` is the only piece read downstream
        # (used to label the shadow-git commit and stamp the ``LoopOutcome``).
        status = "completed"

        # Context-overflow recovery: bound the number of emergency shrinks so a
        # turn that overflows even after eliding can't loop forever.
        compress_retries = 0
        # In-turn transcript compaction (config-gated, factory-off). The
        # proactive trigger arms on the billed context size of the last
        # successful call: 0 until the first usage report and after any
        # compaction, so it only ever fires on fresh data.
        last_context_used = 0
        # The reactive summary is a last resort, once per turn: an overflow
        # retry that finds nothing left to elide may summarize the head
        # instead of surfacing a fatal error.
        reactive_summary_tried = False
        # Image-demotion recovery: bound per turn, same reason.
        image_demote_retries = 0
        # Image-size refusals: the window below closes a notch per refusal; bounded too.
        image_strip_retries = 0
        # The image window: pictures stay while they fit the budget, and collapse
        # to the newest ``image_window`` messages when they do not. Applied to the
        # live list every iteration, so a picture withdrawn once stays withdrawn;
        # a size refusal closes both for the rest of the turn. A budget of 0 in the
        # settings means no standing pass at all: ``None`` here, and the window
        # only starts to act once a refusal has closed it a notch.
        image_window = self._IMAGE_WINDOW_RECENT_MESSAGES
        image_budget: int | None = self._recovery_limits.image_window_budget_bytes or None
        # Retryable model errors that outlasted the provider's own ladder: how many
        # of the loop's longer waits this turn has spent.
        error_waits = 0
        # Tool-failure-loop break: track consecutive hard failures of the
        # same tool *with the same kind of error* across iterations; nudge once
        # per fresh streak, bounded/turn.
        loop_fail_key: tuple[str, str] | None = None
        loop_fail_streak = 0
        loop_nudges = 0
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
                await on_episode_start(iteration - 1)

            # Merge any INJECT-ed user messages (BusyPolicy.INJECT) before this
            # iteration's LLM call. Media-carrying injects keep their file
            # paths in the text so nothing is silently dropped.
            if drain is not None:
                for inj in drain():
                    inj_text = inj.text or ""
                    inj_paths = [m.path for m in inj.media]
                    if inj_paths:
                        prefix = inj_text + "\n" if inj_text else ""
                        inj_text = f"{prefix}[injected message; attached files: {', '.join(inj_paths)}]"
                    if inj_text:
                        messages.append({"role": "user", "content": inj_text})
                        logger.info("inject: merged a mid-turn user message")

            # Proactive compaction layer (config-gated, factory-off): the same
            # usage reading the overflow recovery consults, acted on before the
            # next call so recovery does not have to wait for the window to
            # blow. Deterministic pruning runs first; the LLM head summary runs
            # only when pruning is not enough, and shares the overflow-retry
            # budget so summary calls stay bounded per turn. Placed above the
            # autofill publish: compaction rebinds ``messages``, and a snapshot
            # published before the rebind would go quietly stale.
            if self._compaction.enabled and last_context_used:
                limit = self.context_window_tokens
                reserved = compaction.reserved_tokens(
                    self._compaction.reserved_tokens,
                    resolve_max_output_tokens(effective_model, allow_fetch=False),
                )
                if compaction.should_compact(last_context_used, limit, reserved, self._compaction.trigger_ratio):
                    projected = last_context_used
                    if self._compaction.prune:
                        pruned, elided = self._emergency_shrink(messages)
                        if elided > 0:
                            # No server reading exists for the pruned list until
                            # the next response, so judge the summary tier by
                            # projecting the estimated savings onto the observed
                            # size (local estimates do not know the server's
                            # tokenizer; the delta is safer than the absolute).
                            saved = max(0, estimate_prompt_tokens(messages) - estimate_prompt_tokens(pruned))
                            messages = pruned
                            projected = max(0, last_context_used - saved)
                            last_context_used = 0
                            logger.warning(
                                "Context near window; elided {} older transcript item(s) before the next call",
                                elided,
                            )
                    if (
                        compaction.should_compact(projected, limit, reserved, self._compaction.trigger_ratio)
                        and compress_retries < self._MAX_COMPRESS_RETRIES
                    ):
                        summarized, verdict = await self._summarize_head(
                            messages, effective_model, reasoning_effort=policy.reasoning_effort
                        )
                        if verdict != "skipped":
                            compress_retries += 1
                        if verdict == "changed":
                            messages = summarized
                            last_context_used = 0
                            logger.warning(
                                "Context near window; summarized the transcript head before the next call ({}/{})",
                                compress_retries,
                                self._MAX_COMPRESS_RETRIES,
                            )

            # The standing image window. Before the snapshot and the hooks below
            # so every reader of ``messages`` this iteration sees the same list
            # the model will; in place so the notes it writes are the transcript
            # from here on rather than a per-request rewrite (see _window_images).
            windowed = withdrawn = 0
            if image_budget is not None or image_window < self._IMAGE_WINDOW_RECENT_MESSAGES:
                windowed, withdrawn = self._window_images(
                    messages, image_window, budget=image_budget, reason="budget" if image_budget else "superseded"
                )
            if windowed:
                logger.info(
                    "Image window: withdrew {} picture(s) from {} older message(s); the newest {} keep theirs "
                    "(budget {} bytes)",
                    withdrawn,
                    windowed,
                    image_window,
                    image_budget,
                )

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

            tool_defs = self.tools.get_definitions()
            iter_msg_base = len(messages)

            if hook_ctx is not None:
                hook_ctx.iteration = iteration
                hook_ctx.messages = messages
                hook_ctx.tools = tool_defs
                hook_ctx.response = None
                decision = await self.hooks.before_iteration(hook_ctx)
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
                **({"reasoning_effort": policy.reasoning_effort} if policy.reasoning_effort else {}),
                **(pending_gen_overrides or {}),
            }
            pending_gen_overrides = None
            call_messages, call_tools, call_model = await self.strategies.before_llm_call(
                messages,
                tool_defs,
                effective_model,
            )
            gate = ContinuationGate(on_token_delta) if cut_continuation and on_token_delta is not None else None
            if on_token_delta is not None or on_reasoning_delta is not None:
                response = await self._llm_call_stream(
                    messages=call_messages,
                    tools=call_tools,
                    model=call_model,
                    on_token_delta=gate if gate is not None else on_token_delta,
                    on_reasoning_delta=on_reasoning_delta,
                    **gen_overrides,
                )
            else:
                response = await self.provider.chat_with_retry(
                    messages=call_messages,
                    tools=call_tools,
                    model=call_model,
                    fallback_models=fallback_models,
                    **gen_overrides,
                )
            if cut_continuation:
                cut_continuation = False
                if gate is not None:
                    await gate.finish()
                response.content = cut_reasoning_head(response.content)
            # TokenWise after-hook: strategies observe the response for
            # usage tracking, budget enforcement, etc. Errors are swallowed.
            usage_snapshot = self._build_usage_snapshot(response, call_model, session_key or "")
            await self.strategies.after_llm_call(
                {
                    "content": response.content,
                    "finish_reason": response.finish_reason,
                    "usage": response.usage,
                },
                usage_snapshot,
            )
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
                # they measure.
                if prompt_tokens + completion_tokens > 0:
                    last_context_used = prompt_tokens + completion_tokens
            if usage_sink is not None and response.usage:
                # An explicitly configured window always wins over the live
                # table -- that is what setting it means. Otherwise the live
                # window from the model's provider table (e.g. OpenRouter,
                # when LiteLLM lags) answers instead; unknown to that table
                # too, 0 tells the UI to show its empty state rather than a
                # number that isn't this model's.
                if self._configured_window:
                    context_max = self._configured_window
                else:
                    # Off the event loop: allow_fetch=True here can hit the
                    # network for up to 10s on an OpenRouter model with both
                    # caches expired. See rates._fetch_openrouter_models.
                    context_max = await asyncio.to_thread(resolve_context_window, call_model) or 0
                context_used = prompt_tokens + completion_tokens
                usage_sink.clear()
                usage_sink["prompt_tokens"] = prompt_tokens
                usage_sink["completion_tokens"] = completion_tokens
                usage_sink["total_tokens"] = int(response.usage.get("total_tokens", 0) or 0)
                usage_sink["cost_usd"] = usage_snapshot.cost_usd
                usage_sink["cost_missing_calls"] = int(usage_snapshot.cost_usd is None)
                usage_sink["context_max"] = context_max
                usage_sink["context_used"] = context_used
                usage_sink["context_percent"] = round(100 * context_used / context_max) if context_max else 0

            # Context-window overflow recovery: the structured classifier flags
            # should_compress (a smaller window won't help, but eliding the bulk
            # of accumulated tool output will). Shrink in place and retry this
            # iteration instead of surfacing it as a fatal error. Bounded.
            cls_ = response.error_classification
            if (
                response.finish_reason == "error"
                and cls_ is not None
                and cls_.should_compress
                and compress_retries < self._MAX_COMPRESS_RETRIES
            ):
                shrunk, elided = self._emergency_shrink(messages)
                if elided > 0:
                    messages = shrunk
                    compress_retries += 1
                    iteration -= 1  # the overflowed call did no work; don't bill it
                    last_context_used = 0
                    logger.warning(
                        "Context overflow; elided {} old tool result(s), retrying ({}/{})",
                        elided,
                        compress_retries,
                        self._MAX_COMPRESS_RETRIES,
                    )
                    continue
                if self._compaction.enabled and not reactive_summary_tried:
                    reactive_summary_tried = True
                    summarized, verdict = await self._summarize_head(
                        messages, call_model or effective_model, reasoning_effort=policy.reasoning_effort
                    )
                    if verdict == "changed":
                        messages = summarized
                        compress_retries += 1
                        iteration -= 1  # same discipline: the overflowed call did no work
                        last_context_used = 0
                        logger.warning(
                            "Context overflow with nothing left to elide; summarized the "
                            "transcript head, retrying ({}/{})",
                            compress_retries,
                            self._MAX_COMPRESS_RETRIES,
                        )
                        continue

            # Image-in-tool-result refused: this endpoint takes a picture only in
            # a user message. Rebuild onto the placeholder path -- the shape a
            # False capability verdict would have produced -- and retry this
            # iteration. Also cache the verdict so the rest of the process stops
            # paying for the attempt: the static table in `capabilities` guessed
            # wrong, and this is how it self-corrects.
            if (
                response.finish_reason == "error"
                and cls_ is not None
                and cls_.should_drop_tool_images
                and image_demote_retries < self._MAX_IMAGE_DEMOTE_RETRIES
            ):
                demoted_messages, demoted = self._demote_tool_images(messages)
                if demoted > 0:
                    messages = demoted_messages
                    self._image_tool_result_ok[call_model or effective_model] = False
                    image_demote_retries += 1
                    iteration -= 1  # the refused call did no work; don't bill it
                    logger.warning(
                        "Endpoint refused {} image(s) in a tool result; moved them "
                        "to a user message and retrying ({}/{})",
                        demoted,
                        image_demote_retries,
                        self._MAX_IMAGE_DEMOTE_RETRIES,
                    )
                    continue

            # Pictures refused for their size. Moving them keeps the bytes and the
            # refusal, waiting does not shrink them, and `unknown` would have spent
            # the whole error ladder on them -- so the window closes a notch and the
            # same ask goes again. A notch, not a one-off strip: the strip left the
            # history as it was, and both measured runs refused again a few calls
            # later once the pictures had built back up (amber 09:59 and 10:05, red
            # 11:14 and 11:39, 2026-09-05). First notch: the budget goes and only the
            # newest message keeps its pictures, since that is the one the model has
            # not read yet; when it alone is over the cap the second notch takes it
            # too. The closed window then stands for the rest of the turn, so the
            # refusal cannot recur.
            #
            # At zero the model sees no picture for the rest of the turn, and the
            # notes say so. Accepted rather than papered over with a per-batch byte
            # budget: reaching zero takes a single batch over the cap on its own,
            # which at the measured render sizes means a build of thirty-odd pages
            # returned in one call, and the four measured refusals were all
            # accumulation (75-80 pictures over 14-19 messages; the window's replay
            # peak on those same runs is 6.87 MB against a cap measured at ~26.3 MB
            # decoded). Add the budget when a run actually gets here.
            if (
                response.finish_reason == "error"
                and cls_ is not None
                and cls_.strip_images
                and image_strip_retries < self._MAX_IMAGE_STRIP_RETRIES
            ):
                withdrawn = 0
                while not withdrawn and (image_budget is not None or image_window > 0):
                    if image_budget is not None or image_window > 1:
                        image_budget = None
                        image_window = min(image_window, 1)
                    else:
                        image_window = 0
                    _, withdrawn = self._window_images(
                        messages, image_window, reason="refused", any_role=image_window == 0
                    )
                if withdrawn > 0:
                    image_strip_retries += 1
                    iteration -= 1  # the refused call did no work; don't bill it
                    logger.warning(
                        "Endpoint refused the request's pictures as too large; withdrew {} and closed the "
                        "image window to {} for the rest of the turn, retrying ({}/{})",
                        withdrawn,
                        image_window,
                        image_strip_retries,
                        self._MAX_IMAGE_STRIP_RETRIES,
                    )
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
                    tracker = self.strategies.get("usage_tracker")
                    if tracker is not None:
                        await tracker.record_tool_call(tool_call.name, tool_call.id)
                    preempted = ""
                    if tool_call.name == "ask_user":
                        from raven.agent.subagent import watch_work as _ww

                        preempted = _ww.preempt_owner_ask(watch_state, tool_call.arguments)
                    if preempted:
                        # The owner registered this answer so they would not be
                        # asked for it; the question never reaches them, and the
                        # reply arrives where the model expected the owner's.
                        result = "This question was not sent to the owner."
                        watch_note = preempted
                        duration_ms = int((time.monotonic() - tool_t0) * 1000)
                    else:
                        result = await self.tools.execute(
                            tool_call.name, tool_call.arguments, run_meta=tool_call.run_meta
                        )
                        duration_ms = int((time.monotonic() - tool_t0) * 1000)
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
                    sources = _image_sources(tool_call.name, result_blocks or [], iteration) if result_blocks else []
                    if blocks:
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, model_text, blocks, trusted_note=watch_note
                        )
                        if sources:
                            messages[-1][_IMAGE_SOURCES_KEY] = sources
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
                        await on_notice(_NoticeKind.ACTION_BLOCKED, abort_reason)
                    elif on_token_delta is not None:
                        # A channel with no notice outlet still has to say
                        # something, and silence is the worse failure.
                        await on_token_delta(_ABORTED_ACTION_REPLY)
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
                            _ATTACHED_IMAGE_KEY: True,
                            _IMAGE_SOURCES_KEY: pending_sources,
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
                        iteration -= 1  # the failed call did no work; don't bill it
                        await asyncio.sleep(delay)
                        continue
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    status = "error"
                    break

                # Empty-response recovery: an empty assistant turn would
                # otherwise break out here and surface a "no response to give"
                # dud. Try to recover before giving up. Synthetic scaffolding is
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
                    empty_retries += 1
                    logger.warning(
                        "empty-recovery: plain empty retry {}/{}",
                        empty_retries,
                        self._recovery_limits.empty_content_max_retries,
                    )
                    prev_had_tool_calls = False
                    continue

                # Before the text is persisted, so a short-circuit replaces it
                # without leaving the replaced draft in history and a rollback
                # discards-and-re-samples it.
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

        if final_content is None and iteration >= iteration_cap:
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
        if hook_ctx is not None and answerless:
            hook_ctx.messages = messages
            hook_ctx.response = None
            hook_ctx.metadata["turn_end"] = {"status": status, "iterations": iteration}
            decision = await self.hooks.terminal_answerless(hook_ctx)
            if decision.short_circuit_result is not None:
                final_content = str(decision.short_circuit_result)
                messages = self.context.add_assistant_message(messages, final_content)
                hook_ctx.metadata["turn_end"]["salvaged"] = True

        _transient = ("_recovery_synthetic", _ATTACHED_IMAGE_KEY)
        if any(any(m.get(k) for k in _transient) for m in messages):
            messages = [m for m in messages if not any(m.get(k) for k in _transient)]

        # Extraction belongs to the caller's after-turn pipeline
        # (``context_engine.after_turn`` + ``backend.store`` + ``backend.feedback``
        # run from ``_process_message``); ``outcome.status`` is surfaced so that
        # pipeline can gate on completion.

        outcome = LoopOutcome(status=status)
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
    async def _process_message(
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
        skip_user_inbound = origin in _SKIP_USER_INBOUND_ORIGINS
        # The turn's one hook-metadata dict, and the words the user actually
        # sent: the first crosses every phase group with the turn, the second
        # is what history keeps no matter how hooks rewrite the model's view.
        turn_hook_meta: dict[str, Any] = {}
        inbound_original = content
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
                return _decision.short_circuit_result
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
        if not self.context_engine.owns_compaction:
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        # ── Personalization flow (global switch: self.enable_personalization) ──
        # Skip for a subagent result re-injection: its content is a system-generated
        # announce, not user input — personalizing it would pollute the profile or
        # fire a clarification on the announce. Only SUBAGENT skips here (not the
        # wider after-send / user-inbound sets): a Sentinel notice and cron/heartbeat
        # reach this flow today and keep it.
        if self.enable_personalization and origin is not Origin.SUBAGENT:
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
        # The stream buffers exist so a turn that dies mid-answer still has the
        # text that was already on the reader's screen: the loop only appends an
        # assistant message once the provider call returns, so a cancel in the
        # middle of one would otherwise lose exactly what streamed.
        streamed: dict[str, str] = {"text": "", "thought": ""}

        async def _tap_token(delta: str) -> None:
            streamed["text"] += delta
            if on_token_delta is not None:
                await on_token_delta(delta)

        async def _tap_reasoning(delta: str) -> None:
            streamed["thought"] += delta
            if on_reasoning_delta is not None:
                await on_reasoning_delta(delta)

        async def _tap_episode(index: int) -> None:
            # A new episode is a new stream: without the reset, a buffer that
            # spans two assistant messages matches neither and would be saved
            # as a duplicate of text the loop already committed.
            streamed["text"] = ""
            streamed["thought"] = ""
            if on_episode_start is not None:
                await on_episode_start(index)

        from raven.agent.subagent.mode_tiers import turn_tier

        try:
            # The tier this turn dispatches sub-agents at, frozen here for the
            # same reason the iteration cap is read once: a switch arriving mid-turn
            # lands on the next turn, not on a sub-agent this one has yet to call.
            with turn_tier(self.session_tier(key)):
                final_content, _, all_msgs, outcome = await self._run_agent_loop(
                    initial_messages,
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
                    session_history=session.messages,
                    origin=req.origin,
                )
        except asyncio.CancelledError:
            # A stop is not a failure, but it is also not amnesia: what already
            # streamed is work the reader saw, so it lands in the session with a
            # note saying a person ended the turn. Then the cancel proceeds.
            self._save_broken_turn(
                session,
                initial_messages,
                turn_start_idx,
                turn_received_at,
                streamed,
                status="cancelled",
                inbound_original=inbound_original,
            )
            raise
        except Exception as exc:
            self._save_broken_turn(
                session,
                initial_messages,
                turn_start_idx,
                turn_received_at,
                streamed,
                status="failed",
                reason=str(exc),
                inbound_original=inbound_original,
            )
            raise
        self._stash_recovery(key, outcome)

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

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
                session_history=session.messages,
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

        prev_len = len(session.messages)
        self._save_turn(
            session, all_msgs, turn_start_idx, received_at=turn_received_at, inbound_original=inbound_original
        )
        self.sessions.save(session)
        await self.context_engine.after_turn(
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
        if not self.context_engine.owns_compaction:
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        # ── Step 4: post-action learning (background, non-blocking) ─────────────
        # Skip for a subagent result re-injection (see the pre-turn flow above):
        # its content is a system-generated announce, not user input to learn from.
        if self.enable_personalization and origin is not Origin.SUBAGENT:
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
          transcript stops there, and readable text so the model sees the same.

        ``inbound_original`` rides through to :meth:`_save_turn` exactly as it
        does on the healthy path: a hook's ``modified_content`` rewrite shapes
        only what the model saw this turn, and a turn the user cancelled (or
        one that died) must not be the one door through which the rewritten
        envelope enters the persisted history -- replayed as the user's own
        words every later turn and eligible for consolidation into memory.
        Broken turns used to drop it, which is how a product hook's injected
        block (the design selector cards, ppt's staged-material block) leaked
        into the record on exactly the outcomes users hit mid-task.

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
            word = "cancelled by the user" if status == "cancelled" else f"failed: {reason or 'unknown error'}"
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
        arrived. This save runs after the turn completes, so stamping every
        entry "now" would give the user message and the final answer the same
        timestamp -- and a restored transcript reads the gap between those two
        as the turn's duration.
        """
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
            if entry.get(_ATTACHED_IMAGE_KEY):
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
            if tool_metadata := entry.pop(_TOOL_METADATA_KEY, None):
                entry["metadata"] = tool_metadata
            # Provenance of pictures that lived for this turn only; nothing to file.
            entry.pop(_IMAGE_SOURCES_KEY, None)
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
        inline_tool_stream: bool = False,
        usage_sink: dict[str, Any] | None = None,
        text_sink: dict[str, Any] | None = None,
    ) -> LoopOutcome:
        """Spine-native turn entry: consume a TurnRequest, fan the agent's output
        onto the single ``emit``, return a LoopOutcome. Collapses the legacy
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
        # to what a no-model delivery needs: backend.store indexes the report;
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

        # deep_research (streaming surfaces only): stream its progress live and
        # deliver its finished answer inline, so the tool returns a compact
        # receipt and the model relays instead of re-emitting/rewriting. Progress
        # rides Reasoning (TUI thinking.delta / CLI progress line); the answer
        # follows the same stream switch as the main reply.
        if inline_tool_stream:
            dr_tool = self.tools.get("deep_research")
            if dr_tool is not None and hasattr(dr_tool, "set_stream_callback"):

                async def _route_deep_research(kind: str, text: str) -> None:
                    if not text:
                        return
                    if kind == "progress":
                        await emit(Reasoning(content=text))
                    elif stream:
                        await on_token(text)
                    else:
                        await emit(Text(content=text))

                dr_tool.set_stream_callback(_route_deep_research)

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
