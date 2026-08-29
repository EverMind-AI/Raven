"""Optional-organ glue: context assembly, memory store pipeline, skill
injection, vision routing.
"""

from __future__ import annotations

from raven.agent.loop._shared import (
    _STORE_DRAIN_BUDGET_S,
    Any,
    Session,
    TokenBudget,
    _filter_qualified_ids,
    estimate_prompt_tokens,
    image_placeholder_text,
    logger,
    semconv,
    send_max_tokens,
    supports_image_tool_result,
    trace,
    vision_verdict,
)


class OrganGlueMixin:
    """Optional-organ glue: context assembly, memory store pipeline, skill
    injection, vision routing."""

    def _supports_image_tool_result(self, model: str | None = None) -> bool:
        """Cached per model: resolving the LiteLLM target parses the model string,
        and this is asked once per tool call that returns an image.

        A ``False`` learned from a refused request (see ``should_drop_tool_images``)
        is written into the same cache, so a static table that guessed wrong stops
        costing a wasted call after the first one.
        """
        key = model or self.model
        if key not in self._image_tool_result_ok:
            spec = None
            try:
                from raven.providers.registry import find_by_model

                spec = find_by_model(key)
            except Exception:
                pass
            self._image_tool_result_ok[key] = supports_image_tool_result(self.provider, key, spec)
            logger.debug(
                "image-in-tool-result support for {}: {}",
                key,
                self._image_tool_result_ok[key],
            )
        return self._image_tool_result_ok[key]

    def _describe_tool_name(self) -> str | None:
        """The description tool's name if it is registered, else ``None``.

        Checked rather than assumed: the note that replaces a picture points at
        this tool, and pointing at one the model was never given is an
        instruction it cannot follow.
        """
        return self._DESCRIBE_TOOL if self.tools.get(self._DESCRIBE_TOOL) else None

    def _route_result_images(
        self,
        model_text: str,
        blocks: list[dict[str, Any]] | None,
        model: str,
    ) -> tuple[str, list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
        """Decide how a tool result's pictures reach ``model``.

        Returns the text the tool result carries, the blocks to put *in* it, and
        the blocks to attach to a following user message. Exactly one of the last
        two is ever populated.

        Three outcomes, and the wording differs because the model's next move
        differs. No vision at all: nothing follows, so the note must not promise
        an attachment, and it names the description tool when one is registered.
        Vision and a transport that carries images in a ``role="tool"`` message:
        the blocks ride along untouched. Vision but a transport that cannot (every
        OpenAI-style Chat Completions endpoint -- image is excluded from the tool
        role at the schema level): the result carries text and the picture follows
        in a user message, the shape OpenClaw uses.

        A method rather than a branch inside the loop so it can be tested at all:
        the loop reaches this point only through a live provider and a real tool
        call, and the wrong choice here is silent -- the model answers about a
        picture it never received.
        """
        if not blocks:
            return model_text, blocks, None
        if not self._supports_vision(model):
            return image_placeholder_text(blocks, blind=True, describe_tool=self._describe_tool_name()), None, None
        if self._supports_image_tool_result(model):
            return model_text, blocks, None
        attach = [b for b in blocks if b.get("type") == "image_url"]
        return image_placeholder_text(blocks), None, attach

    def _supports_vision(self, model: str | None = None) -> bool:
        """Cached per model: whether this model can see a picture at all.

        Asked once per turn and once per tool result that returns an image, and
        the lookup joins the model string against the gateway catalogue -- same
        reason the sibling probe above is cached.

        Only a real verdict is cached. ``vision_verdict`` returns ``None`` while
        the catalog has no answer -- a cold install, before the background warm
        lands -- and that is optimism rather than knowledge: caching it would
        freeze the guess for the life of this loop, which is the life of the
        process, and the warm would then fill a table nothing re-reads. An
        unknown model is re-asked each turn, which costs a dict lookup.

        The verdict is the routed primary's. A fallback further down the chain is
        sent the same message list, so a vision-capable primary with a blind
        fallback hands the blind endpoint an image block it will refuse; that
        refusal classifies as fatal and stops the chain rather than answering
        blind. Pre-existing in the sibling probe too, and it needs the fallback
        chain to be assembled per candidate to fix properly.
        """
        key = model or self.model
        cached = self._vision_ok.get(key)
        if cached is not None:
            return cached

        spec = None
        try:
            from raven.providers.registry import find_by_model

            spec = find_by_model(key)
        except Exception:
            pass
        verdict = vision_verdict(key, spec, self.provider)
        logger.debug("vision support for {}: {}", key, verdict)
        if verdict is None:
            return True
        self._vision_ok[key] = verdict
        return verdict

    # ── Context engine helpers ──────────────────────────────────────────

    def _context_messages_for_session(self, session: Session) -> list[dict[str, Any]]:
        """Return the candidate message view owned by the active context engine.

        Curator (``owns_compaction=True``) wants the full append-only log so
        it can decide what to archive itself; Legacy wants the post-consolidation
        slice to match the pre-Curator behavior exactly.
        """
        if self.context_engine.owns_compaction:
            return list(session.messages)
        return session.get_history(max_messages=0)

    def _make_token_budget(self, selected_skills: list[Any] | None = None) -> TokenBudget:
        """Compute a conservative per-turn prompt budget for the active engine."""
        # allow_fetch=False for the same reason construction passes it (see
        # __init__): this runs per turn on the loop's own thread, and it only
        # needs a number to reserve -- not the one a request will carry. The
        # fallback under-reserves at worst; the importing tier costs seconds.
        ceiling = send_max_tokens(
            getattr(self.provider, "generation", None),
            # The id a request will go out under, so the reservation matches the
            # ceiling that request will carry rather than the stored name's.
            getattr(self.provider, "wire_model_id", lambda m: m)(self.model),
            allow_fetch=False,
        )
        # The whole ceiling, not a share of it. Requests no longer name a
        # ceiling, so the one that applies is the model's own -- whatever the
        # vendor or LiteLLM's transformation fills in. Reserving less than that
        # hands out a prompt the reply cannot coexist with: measured on this
        # repo's default model, a share leaves the prompt 150000 of a 200000
        # window against a reply allowed 64000, and the sum is refused at
        # request time. `_emergency_shrink` only elides tool bodies, so a
        # history grown on conversation gets no retry from that refusal.
        #
        # A share would be right again only if the request carried one, which
        # is the trade the previous shape made and this one does not.
        reserved_output = min(ceiling, self.context_window_tokens)
        tool_tokens = estimate_prompt_tokens([], self.tools.get_definitions())
        system_prompt = self.context.build_system_prompt(selected_skills)
        system_tokens = estimate_prompt_tokens([{"role": "system", "content": system_prompt}])
        available_history = max(
            0,
            self.context_window_tokens - reserved_output - tool_tokens - system_tokens,
        )
        return TokenBudget(
            context_length=self.context_window_tokens,
            reserved_output=reserved_output,
            reserved_tools=tool_tokens,
            reserved_system=system_tokens,
            available_history=available_history,
        )

    def _uses_default_engine(self) -> bool:
        """Whether the active engine owns skill selection via SkillForgeRouter.

        Always ``True`` now — there is a single
        :class:`ContextAssembler` whose SkillsSegmentBuilder handles
        selection and populates ``injected_skill_ids`` in the assembled
        metadata. Kept as a method (rather than inlined) because several
        callsites still gate on it; it no longer branches on engine name.
        """
        return True

    async def _select_skills_for_turn(
        self,
        current_message: str,
        history: list[dict],
    ) -> list[Any] | None:
        """No host-side pre-selection — the engine's SkillForgeRouter owns it.

        The unified engine selects + renders skills internally and
        surfaces ``injected_skill_ids`` via ``AssembledContext.metadata``,
        which AgentLoop reads out of ``_last_injected_skill_ids`` after
        assemble. No SkillMeta list flows through this path.
        """
        return None

    async def _assemble_context_messages(
        self,
        *,
        session: Session,
        session_key: str,
        current_message: str,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        surface: str | None = None,
        selected_skills: list[Any] | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Ask the active context engine for the main-agent message window.

        ``model`` is the id the request will actually reach (the router's pick,
        when there is one). It decides whether an attachment is inlined as a
        picture, so defaulting it to ``self.model`` would let the configured
        model answer for a routed one.
        """
        from raven.context_engine import TurnContext  # deferred — see module note

        # Phase A / Phase C tidy: reset the metadata stash BEFORE calling
        # the engine. If ``engine.assemble`` raises partway, the next
        # caller falls back to the legacy ``_collect_injected_skill_ids``
        # path rather than accidentally consuming a previous turn's
        # injected ids. Only successful assemble repopulates the stash.
        self._last_injected_skill_ids = None
        self._last_injected_skill_sources = {}
        self._last_degraded_segments: list[str] = []
        session_messages = self._context_messages_for_session(session)
        assembled = await self.context_engine.assemble(
            session_key,
            session_messages,
            self._make_token_budget(selected_skills),
            turn=TurnContext(
                current_message=current_message,
                media=media,
                can_see_images=self._supports_vision(model),
                describe_tool=self._describe_tool_name(),
                channel=channel,
                chat_id=chat_id,
                surface=surface,
                selected_skills=selected_skills,
            ),
        )
        # Stash the engine's injected_skill_ids so the after-turn
        # feedback dispatcher can read the source-qualified ids the
        # unified engine populates via SkillForgeRouter. If the key is absent
        # the stash stays None and _collect_injected_skill_ids falls back
        # to the SkillMeta-based path.
        meta_ids = assembled.metadata.get("injected_skill_ids") if assembled.metadata else None
        self._last_injected_skill_ids = list(meta_ids) if meta_ids else None
        meta_sources = assembled.metadata.get("injected_skill_sources") if assembled.metadata else None
        self._last_injected_skill_sources = dict(meta_sources) if meta_sources else {}
        meta_degraded = assembled.metadata.get("degraded_segments") if assembled.metadata else None
        self._last_degraded_segments = list(meta_degraded) if meta_degraded else []
        messages = assembled.messages
        self._inject_recovery_block(session_key, messages)
        return messages

    @trace.instrument("memory.feedback", extract=semconv.memory_feedback)
    async def _dispatch_backend_feedback(
        self,
        session_key: str,
        injected_skill_ids: list[str] | None,
        used_skill_ids: list[str] | None = None,
    ) -> None:
        """FB-1: forward source-qualified skill-usage signals to
        :meth:`MemoryBackend.feedback`.

        Skill IDs surface with a ``<source>/<native_id>`` prefix
        (``local/git-resolver`` / ``mass/abc`` / ``everos/xyz``).
        Only the ``everos/`` prefix is forwarded — static libraries
        (``local`` / ``mass``) have no feedback channel; the dispatcher
        is silent for them (no warning, just skipped). Unprefixed legacy
        ids (e.g. raw skill names emitted by the pre-SkillForgeRouter
        ``SkillService.select`` path) are also skipped — they predate
        the qualified-id convention and there's no safe routing target.

        No-ops when:
        - ``self.backend is None`` (no plugin wired)
        - No qualified-id matches the ``everos/`` prefix
        - The injected + used lists are both empty / None

        Exceptions from :meth:`backend.feedback` are caught + logged.
        The host MUST NOT abort the after-turn pipeline because a
        plugin's feedback handler raised — feedback is best-effort
        telemetry, not load-bearing state.
        """
        if self.backend is None:
            return
        injected_native = _filter_qualified_ids(injected_skill_ids, "everos")
        used_native = _filter_qualified_ids(used_skill_ids, "everos")
        if not injected_native and not used_native:
            return
        signals = {
            "kind": "skill_usage",
            "session_id": session_key,
            "injected": injected_native,
            "used": used_native,
        }
        try:
            await self.backend.feedback(signals)
        except Exception:
            logger.exception(
                "backend.feedback failed for session {}; signals dropped",
                session_key,
            )

    @trace.instrument("memory.enqueue")
    def _dispatch_backend_store(
        self,
        session_key: str,
        messages_slice: list[dict],
    ) -> None:
        """AG-1: hand a turn's messages to the plugin :class:`MemoryBackend`.

        Third peer step in the after-turn pipeline alongside
        ``context_engine.after_turn`` (engine-side bookkeeping) and
        ``memory.maybe_consolidate`` (raven-core compaction). When no backend
        was wired, this is a no-op.

        Synchronous by contract: the turn hands the slice over and returns.
        What happens to it after that is :class:`StorePipeline`'s.
        """
        self._store_pipeline.enqueue(session_key, messages_slice)

    async def drain_backend_stores(self, timeout: float = _STORE_DRAIN_BUDGET_S) -> None:
        """Let queued writes finish before the process goes away, then say what
        was lost. The counting is the pipeline's; telling the user is the
        host's, and this is the last moment it is still actionable."""
        dropped = await self._store_pipeline.drain(timeout)
        if dropped:
            logger.warning(
                "{} turn(s) were not indexed: the memory service never caught up",
                dropped,
            )
            from rich.console import Console

            Console(stderr=True).print(
                f"[yellow]{dropped} turn(s) were not written to long-term memory "
                "because the memory service was unavailable.[/yellow]"
            )

    def _note_memory_ok(self) -> None:
        """A successful store clears a standing fault, and says so once."""
        if self._memory_fail_streak >= self._MEMORY_FAILURES_BEFORE_ALARM:
            logger.info("backend.store recovered; long-term memory is being written again")
            self._emit_mcp_event("memory.health", {"ok": True, "error": None})
        self._memory_fail_streak = 0

    def _note_memory_failure(self, detail: str) -> None:
        """Escalate a run of failures from the log to the client.

        The swallow above is right -- a failed index must not cost the user
        their reply -- but on its own it made a permanently broken backend
        indistinguishable from a working one: writes and recalls can fail every
        turn for days with the only trace in a log nobody is reading.
        """
        self._memory_fail_streak += 1
        if self._memory_fail_streak == self._MEMORY_FAILURES_BEFORE_ALARM:
            logger.error(
                "backend.store has failed {} times in a row; long-term memory is not being written",
                self._memory_fail_streak,
            )
            self._emit_mcp_event("memory.health", {"ok": False, "error": detail[:400]})

    def _collect_injected_skill_ids(
        self,
        selected: list[Any] | None,
    ) -> list[str]:
        """Combine selector top-K + always-skills into a deduplicated id list.

        ``selected`` is the :class:`SkillMeta` list returned by the
        retrieval selector for this turn (or ``None`` when the selector
        is disabled / returned empty). always-skills are pulled from
        :class:`LocalSkillCatalog` since they are unconditionally rendered
        regardless of the selector's output.

        Returns ids canonicalized to ``{source}/{stable_key}`` form.
        ``stable_key`` is whatever the source uses for unambiguous
        addressing — the sqlite ``skills.id`` for ``everos`` (which
        allows duplicate names) and the directory / display name
        elsewhere. Different SkillMeta producers populate ``meta.id``
        inconsistently (file registry: bare key or ``{source}/{key}``;
        sqlite store: ``{source}/{key}``); this function normalizes them
        to a single shape so the after-turn ``backend.feedback`` signal
        can route them uniformly.
        """
        skills_svc = getattr(self.context, "skills", None)
        if skills_svc is None:
            return []

        seen: set[str] = set()
        ids: list[str] = []

        def _add(meta: Any) -> None:
            src = getattr(meta, "source", None)
            mid = getattr(meta, "id", None)
            if not src or not mid:
                return
            canonical = mid if "/" in mid else f"{src}/{mid}"
            if canonical not in seen:
                seen.add(canonical)
                ids.append(canonical)

        def _add_raw_id(qid: str) -> None:
            if qid and qid not in seen:
                seen.add(qid)
                ids.append(qid)

        # Prefer the AssembledContext metadata the unified engine
        # populated from its SkillForgeRouter. Those ids are already
        # source-qualified (``local/x`` / ``mass/y`` / ``everos/z``) so
        # they bypass the SkillMeta-canonicalization path. Always-skills
        # get folded in afterwards because they live outside
        # SkillForgeRouter's selection.
        if self._last_injected_skill_ids is not None:
            for qid in self._last_injected_skill_ids:
                _add_raw_id(qid)
        else:
            for meta in selected or []:
                _add(meta)
        try:
            always = skills_svc.get_always_skills()
        except Exception:
            always = []
        for meta in always:
            _add(meta)
        return ids

    def set_skills_sink(self, sink) -> None:
        """Late-bind the sink that reports SkillForge-injected skills to the web
        UI (the host wires it to the page's emitter). ``sink`` is an async
        callable ``(conversation, name, payload)`` matching the DAG sink."""
        self._skills_sink = sink

    async def _emit_injected_skills(self, session_key: str) -> None:
        """Push this turn's SkillForge-injected skills to the web UI's skill
        panel as a ``skills_injected`` custom event.

        The id's prefix is the *addressing* namespace, which is ``local`` for
        every on-disk skill, so origin comes from
        ``_last_injected_skill_sources`` (the registry source the engine
        reported) and falls back to the prefix only when that is absent. No-op
        when there is no sink (CLI/IM) or nothing was injected this turn."""
        if self._skills_sink is None:
            return
        ids = self._last_injected_skill_ids or []
        if not ids:
            return
        skills = []
        for qid in ids:
            prefix, _, name = str(qid).partition("/")
            skills.append(
                {
                    "id": str(qid),
                    "source": self._last_injected_skill_sources.get(str(qid)) or prefix,
                    "name": name or str(qid),
                    "kind": "injected",
                }
            )
        await self._emit_skills(session_key, skills)

    async def _emit_skills(self, session_key: str, skills: list[dict[str, Any]]) -> None:
        """Send one ``skills_injected`` payload, swallowing any failure."""
        try:
            await self._skills_sink(session_key, "skills_injected", {"skills": skills})
        except Exception:  # never break a turn on a telemetry/UI push
            logger.exception("skills_injected emit failed")

    async def _report_skill_read(self, session_key: str, tool_name: str, args: dict[str, Any]) -> None:
        """Report a skill the model loaded itself to the web UI's skill panel.

        ``read_skill`` / ``use_skill`` are how a skill advertised by description
        alone actually gets loaded -- notably the builtin orchestration guide,
        whose id the DAG tool names in its own description. Those never pass
        through SkillForge injection, so without this the panel shows nothing
        for a turn that demonstrably used a skill.

        The model passes the addressing id (``local/<name>``); the registry is
        what knows the real source. An unresolvable id is still reported, with
        the addressed namespace as source, rather than dropped -- the call
        happened."""
        if self._skills_sink is None:
            return
        qid = str(args.get("skill_id") or "").strip()
        if not qid:
            return
        namespace, native = qid.partition("/")[0], qid.partition("/")[2]
        registry = getattr(getattr(self.context, "skills", None), "registry", None)
        try:
            # The same split and resolver ``read_skill`` itself uses, so the id
            # grammar -- including "a bare id addresses the Hub" -- and "local
            # spans every on-disk source" stay defined in exactly one place.
            from raven.agent.tools.skill_hub import lookup_on_disk, split_qualified_id

            namespace, native = split_qualified_id(qid)
            meta = lookup_on_disk(registry, namespace, native)
            source = str(meta.source) if meta is not None and getattr(meta, "source", None) else namespace
        except Exception:  # noqa: BLE001 - a registry hiccup must not lose the report
            logger.debug("skill source lookup failed for %s", qid)
            source = namespace
        name = native or qid
        await self._emit_skills(
            session_key,
            [{"id": qid, "source": source, "name": name, "kind": tool_name}],
        )

    async def _note_watch_work(self, state: Any, name: str, args: dict[str, Any], result: str, message: str) -> str:
        """Add one line when a look landed on a path the owner asked about.

        Ported from the on-call agent's ``_note_watched_path``, one step earlier
        in the chain: there the line steers a loop that already is the on-call
        agent toward ``ops_declare``; here it steers the main agent toward
        spawning that agent. Lazily, and once per turn: a request that never
        reaches for a path never pays for the judgement, and a turn that reaches
        for twenty pays once.

        Placed on the result rather than before the call because the result is
        the only channel measured to change the next move: 2026-08-19 (and again
        2026-08-27 on this loop, with the roster description carrying the same
        fact), the same fact at the top of the turn was read, written into the
        reasoning, and ignored, while a fact arriving in a tool result was acted
        on.
        """
        # Only a real hand-off answers the question this line asks for the rest
        # of the turn. The first cut treated every spawn as one and silenced the
        # nudges on refusal too -- at exactly the moment the model chose to run
        # trials by hand -- and an unrelated spawn of another agent silenced
        # them just as well. A machineless refusal instead arms the sharper
        # line; anything unrecognised keeps the nudges alive.
        if name in ("spawn", "run_subagent_dag"):
            from raven.agent.subagent import watch_work

            try:
                spawn_tool = self.tools.get("spawn")
                agents = getattr(spawn_tool, "_agents", lambda: [])()
                agent = await watch_work.oncall_agent([a.name for a in agents]) if agents else None
            except Exception:  # noqa: BLE001 -- an unreadable roster attributes nothing
                agent = None
            if agent is None:
                return result
            try:
                if "Nothing was dispatched" in str(result):
                    state.machineless = True
                elif watch_work.handed_over(name, args, str(result), agent):
                    state.dispatched = True
            except Exception:  # noqa: BLE001 -- a dispatch must not fail over a judgement
                logger.debug("watch-work hand-off judgement skipped", exc_info=True)
            return result
        if state.dispatched:
            return result
        key = self._WATCHED_TOOLS.get(name)
        if not key:
            return result
        subject = str(args.get(key) or "")
        if not subject or not message:
            return result
        try:
            # No agent on the roster keeps a ledger: the line would point at a
            # spawn that cannot land, and per the prober's contract an answer
            # that could not be established must change nothing.
            spawn_tool = self.tools.get("spawn")
            agents = getattr(spawn_tool, "_agents", lambda: [])()
            if not agents:
                return result
            from raven.agent.subagent import watch_work

            agent = await watch_work.oncall_agent([a.name for a in agents])
            if not agent:
                return result
            verdict = state.verdict
            if verdict is None:
                verdict = state.verdict = watch_work.read_verdict(
                    (await self._llm_call_stream(watch_work.build_prompt(message), None, self.model)).content
                )
            if not verdict.watched:
                return result
            import re as _re

            # A command is searched for paths and URLs, and offered whole: the
            # subject the owner named rarely reappears verbatim -- a pipeline
            # named by web URL is looked at through a CLI call carrying the
            # project slug percent-encoded -- and the whole-command hit is what
            # lets anchor matching see it (a path-typed subject never matches a
            # whole command, so paths lose nothing).
            if key == "command":
                hits = _re.findall(r"(/[^\s'\"|;&>]+)", subject)
                hits += _re.findall(r"(https?://[^\s'\"|;&>]+)", subject)
                hits.append(subject)
            else:
                hits = [subject]
            if any(verdict.claims(h) for h in hits):
                if state.machineless:
                    return result + watch_work.machineless_nudge(agent)
                return result + watch_work.nudge(agent)
        except Exception:  # noqa: BLE001 -- a look must not fail over a judgement
            logger.debug("watch-work judgement skipped", exc_info=True)
        return result
