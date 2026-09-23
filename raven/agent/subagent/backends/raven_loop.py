"""The default subagent backend: a bounded in-process Raven agent loop.

This is the logic that used to live inline in ``SubagentManager._run_subagent_inner``
and ``_build_subagent_prompt``, extracted verbatim so a spawned sub-agent's
*executor* becomes pluggable without changing default behavior.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.subagent import activity
from raven.agent.subagent import charter as charter_mod
from raven.agent.subagent.attachments import with_attachment_note
from raven.agent.subagent.backends.base import IN_SUBAGENT_RUN
from raven.agent.subagent.delegate import outbound_charter
from raven.agent.subagent.mcp_grant import (
    McpGrant,
    McpSource,
    annotate_mcp_failure,
    raven_loop_target,
    resolve_grant,
)
from raven.agent.subagent.tool_vocabulary import RAVEN_NAME
from raven.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from raven.agent.tools.registry import ToolRegistry, call_failed
from raven.agent.tools.removals import RemovalWatch
from raven.agent.tools.shell import ExecTool
from raven.agent.tools.web import ImageSearchTool, WebFetchTool, WebSearchTool, image_search_vendor, resolve_vendor_key
from raven.config.live import LiveConfig, exec_extra_deny_patterns, live_vendor_key
from raven.config.schema import LLM_ERROR_RETRY_DELAYS_DEFAULT, ExecToolConfig
from raven.contracts.llm_provider import LLMProvider
from raven.contracts.participant import StepView
from raven.contracts.subagent_backend import SubagentActionAbortedError, SubagentNoAnswerError
from raven.contracts.tool import SKIPPED_AFTER_BLOCKED_CALL, Continuation
from raven.memory_engine import filter_by_required_tools
from raven.providers.streaming import generation_kwargs, stream_llm_call
from raven.providers.tool_calls import openai_tool_call
from raven.security.trust import wrap_untrusted
from raven.spine.message import Media
from raven.utils.messages import build_assistant_message

if TYPE_CHECKING:
    from raven.providers.binding import ModelBinding

_LIVE_CONFIG = LiveConfig()
#: The ladder a spawn waits out when its caller passed none. Only a rig that
#: builds a backend by hand lands here; every product path carries the
#: deployment's own ``agents.defaults.llmErrorRetryDelays``.
_STREAM_RETRY_DELAYS = LLM_ERROR_RETRY_DELAYS_DEFAULT


def _live_exec_extra_deny() -> list[str] | None:
    """The operator's live exec deny extras, shared by every sub-agent run.

    Module-level so the byte-compare cache is warm across runs; the answer is
    identical to the main loop's reader, which is the point -- a tightened
    permission gates a delegated shell the same call it gates a direct one.
    """
    return exec_extra_deny_patterns(_LIVE_CONFIG)


def _append_participant_note(messages: list[dict[str, Any]], note: str) -> None:
    """Append generated advice without importing the main loop back into this backend."""
    if not messages or not note:
        return
    body = messages[-1].get("content")
    if isinstance(body, str):
        messages[-1]["content"] = f"{body}\n\n{note}" if body else note
    elif isinstance(body, list):
        messages[-1]["content"] = [*body, {"type": "text", "text": note}]
    elif body is None:
        messages[-1]["content"] = note


def _withheld_here(tools: ToolRegistry, mcp_source: "McpSource | None") -> frozenset[str]:
    """Which of this run's tools are not on offer right now.

    Two sources, asked per assembly the way ``AgentLoop._withheld_tool_names``
    asks them: the operator's MCP blacklist, and every registered tool that
    says it is unconfigured. This lane builds its own registry, so without the
    second source a source install without the browser extra advertised eight
    ``browser_*`` tools to a delegated run that could only watch them fail --
    the main loop withholds exactly those, and a delegated run is not a
    different deployment.
    """
    withheld: set[str] = set(mcp_source.disabled_tools()) if mcp_source is not None else set()
    for name in tools.names():
        spec = tools.spec_of(name)
        if spec is None or spec.configured is None:
            continue
        try:
            offered = bool(spec.configured())
        except Exception as exc:
            logger.warning("tool {} could not say whether it is configured: {}", name, exc)
            continue
        if not offered:
            withheld.add(name)
    return frozenset(withheld)


def _file_change_counts(file_change: Any, diff: str | None) -> tuple[int, int]:
    """Added/removed line counts for one file change, preferring the tool's own diff.

    A unified diff, when the tool produced one, is counted directly. A rewrite
    the tool dropped for being too large to render (or a write with no prior
    content to diff against) has no ``diff``, so the two contents are compared
    directly: a new file (``before is None``) counts every line of ``after`` as
    added, and an existing file is compared line-by-line with ``difflib``.
    """
    if diff:
        lines = diff.splitlines()
        add = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        delete = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        return add, delete
    after_lines = file_change.after.splitlines()
    if file_change.before is None:
        return len(after_lines), 0
    before_lines = file_change.before.splitlines()
    add = delete = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before_lines, after_lines).get_opcodes():
        if tag in ("insert", "replace"):
            add += j2 - j1
        if tag in ("delete", "replace"):
            delete += i2 - i1
    return add, delete


def _workspace_relative(path: str, workspace: Path) -> str:
    """The path a file record carries: relative to the run's workspace when the
    file is under it (the file endpoint anchors relative paths there, and the
    panel reads ``work/notes.md`` where an absolute path says nothing), absolute
    otherwise."""
    try:
        return str(Path(path).resolve().relative_to(Path(workspace).resolve()))
    except (ValueError, OSError):
        return path


def build_subagent_prompt(
    agent_home: Path,
    work_dir: Path,
    tool_names: Collection[str] = (),
    skills_allow: Collection[str] | None = None,
) -> str:
    """Build a focused system prompt for an in-process raven subagent.

    ``agent_home`` is where the agent's memory and skills live; ``work_dir`` is
    the session directory this sub-agent reads and writes files in. They are
    different directories and only ``agent_home`` may reach the skill catalog
    or the memory store -- pointing those at ``work_dir`` would build a private
    raven tree inside the user's own directory and hide every skill the agent
    actually has.

    ``tool_names`` is what this sub-agent's registry actually holds; skills
    declaring a ``requires.tools`` outside it are withheld, so the prompt can
    never hand the sub-agent a procedure it has no tool to follow. That covers
    the orchestration guide (it needs ``run_subagent_dag``, which only the main
    agent registers) without naming it, and any future tool-gated skill for
    free.

    The default is an *empty* set rather than "unknown": a sub-agent prompt is
    always built next to its own registry, so no names means no tools -- unlike
    the main agent's segment builder, where a callable that fails to answer has
    to degrade to showing everything.

    ``skills_allow`` narrows the skills menu on top of the tool filter:
    ``None`` keeps the current full-catalog behaviour, ``[]`` hides the menu
    entirely, and a list shows only the named skills. It stacks with the tool
    filter rather than replacing it, so a whitelisted skill whose required
    tools this sub-agent lacks is still withheld.
    """
    from raven.agent.context import ContextBuilder
    from raven.memory_engine import LocalSkillCatalog

    # Transient ContextBuilder just for the runtime-context builder; the
    # subagent has no ContextBuilder of its own (and must not start a watcher).
    time_ctx = ContextBuilder(agent_home, start_watcher=False)._build_runtime_context(None, None)
    parts = [
        f"""# Subagent

{time_ctx}

You are a subagent spawned by the main agent to complete a specific task.
Stay focused on the assigned task. Your final response will be reported back to the main agent.

## Directories
- Working directory: {work_dir} — files you produce go here; relative paths resolve here.
- Agent home: {agent_home} — the agent's memory and skills."""
    ]

    catalog = LocalSkillCatalog(agent_home, start_watcher=False)
    visible = filter_by_required_tools(catalog.registry.list_all(), tool_names)
    if skills_allow is not None:
        allowed = set(skills_allow)
        visible = [s for s in visible if s.name in allowed]
    skills_summary = catalog.build_skills_summary(only=visible) if visible else ""
    if skills_summary:
        parts.append(f"## Skills\n\nRead SKILL.md with read_file to use a skill.\n\n{skills_summary}")

    return "\n\n".join(parts)


class RavenLoopBackend:
    """Runs the task as a bounded in-process Raven agent loop (the default).

    Streaming forwards *every* model call's text, including the preamble a call
    writes before reaching for a tool. Only the final answer is returned, so a
    live direct chat shows more than the record replays afterwards: the preamble
    is progress, and a run's evidence is the answer it arrived at. Withholding it
    is not an option -- whether a call is the last one is knowable only once it
    has finished, which is after the whole reply would have been buffered.
    """

    kind = "raven-loop"
    streams = True
    _MAX_ITERATIONS = 15

    def __init__(
        self,
        *,
        provider: LLMProvider,
        model: str,
        agent_home: Path,
        restrict_to_workspace: bool = False,
        exec_config: "ExecToolConfig | None" = None,
        search_api_key: str | None = None,
        jina_api_key: str | None = None,
        web_proxy: str | None = None,
        web_search_provider: str = "serper",
        web_fetch_provider: str = "jina",
        web_provider_keys: dict[str, str] | None = None,
        image_search: bool = False,
        tools_allow: Collection[str] | None = None,
        skills_allow: Collection[str] | None = None,
        mcp_allow: Collection[str] | None = None,
        retry_delays: "Sequence[float] | None" = None,
        retry_after_output: bool = False,
        pin: Callable[[], ModelBinding | None] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        # The row's own model paired with its own credential, resolved per run
        # (`SubagentManager.build_builtin_backend`); None means this agent runs
        # on whatever pair the dispatch brings.
        self._pin = pin
        # Global, unlike the per-run ``workspace``: memory and skills are the
        # agent's identity and stay in one place whatever directory a session
        # works in.
        self.agent_home = Path(agent_home)
        self.restrict_to_workspace = restrict_to_workspace
        self.exec_config = exec_config or ExecToolConfig()
        self.search_api_key = search_api_key
        self.jina_api_key = jina_api_key
        self.web_proxy = web_proxy
        self.web_search_provider = web_search_provider
        self.web_fetch_provider = web_fetch_provider
        self.web_provider_keys = web_provider_keys
        self.image_search = image_search
        # Per-role capability whitelists (playbook roles build one backend per
        # role). None = current full set; [] = none; a list = only those.
        # skills_allow additionally stacks with the tool-based skill filter.
        self.tools_allow = set(tools_allow) if tools_allow is not None else None
        self.skills_allow = skills_allow
        self.mcp_allow = list(mcp_allow) if mcp_allow is not None else None
        # The loop's own ladder, so a spawn waits what the deployment configured
        # rather than a constant -- an empty list there means no waits, and reading
        # it as "unset" put 105 seconds back on a deployment that turned them off.
        self.retry_delays = _STREAM_RETRY_DELAYS if retry_delays is None else tuple(retry_delays)
        self.retry_after_output = retry_after_output
        self.mcp_source: McpSource | None = None

    def _web_key(self, vendor: str) -> str | None:
        return resolve_vendor_key(vendor, self.web_provider_keys, self.search_api_key, self.jina_api_key)

    def set_mcp_source(self, source: McpSource | None) -> None:
        """Late-bind the host MCP view without rebuilding this cached backend."""
        self.mcp_source = source

    def resolve_mcp_grant(self, mcps: list[str] | None = None) -> McpGrant:
        """Resolve this dispatch's override or the agent row's default."""
        effective = self.mcp_allow if mcps is None else mcps
        return resolve_grant(effective, self.mcp_source, raven_loop_target())

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
        provider: LLMProvider | None = None,
        model: str | None = None,
        mcps: list[str] | None = None,
        mcp_grant: McpGrant | None = None,
        mode: str | None = None,
        authored_task: str | None = None,
        history: list[dict[str, Any]] | None = None,
        on_messages: Callable[[list[dict[str, Any]]], None] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        media: Sequence[Media] = (),
    ) -> str:
        token = IN_SUBAGENT_RUN.set(True)
        grant = self.resolve_mcp_grant(mcps)
        # The in-process half of what ``_meta`` carries over ACP. A forked
        # worker reads its charter off the wire and opens the scope in its own
        # turn; a sub-agent that runs here never crosses a process boundary, so
        # the dispatch's charter would otherwise be set by ``spawn`` and read by
        # nobody -- the brief would ride along in the task text while the tools,
        # the checks and the deadline it named were silently dropped.
        #
        # Read here rather than deeper for the reason ``IN_SUBAGENT_RUN`` is set
        # here: this call is the whole of the dispatched run, and a scope that
        # did not span it would leave part of the run unbriefed.
        charter = charter_mod.parse(outbound_charter())
        if charter is not None:
            # The same line the ACP lane logs on arrival. Without it a briefed
            # in-process run and an unbriefed one read identically in the log,
            # and the difference between them is the whole of this feature.
            logger.info(
                "agent playbook: charter applied to this run ({} tool(s), {} check(s){})",
                "all" if charter.tools is None else len(charter.tools),
                len(charter.checks),
                ", judge" if charter.code else "",
            )
        try:
            with annotate_mcp_failure(grant), charter_mod.charter_scope(charter):
                return await self._run(
                    task,
                    task_id=task_id,
                    workspace=workspace,
                    executor=executor,
                    session_key=session_key,
                    instance=instance,
                    provider=provider,
                    model=model,
                    grant=grant,
                    history=history,
                    on_messages=on_messages,
                    on_delta=on_delta,
                    media=media,
                )
        finally:
            IN_SUBAGENT_RUN.reset(token)

    async def _run(  # noqa: C901 -- this is the bounded in-process worker loop
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
        provider: LLMProvider | None = None,
        model: str | None = None,
        grant: McpGrant,
        history: list[dict[str, Any]] | None = None,
        on_messages: Callable[[list[dict[str, Any]]], None] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        media: Sequence[Media] = (),
    ) -> str:
        # The spawn's snapshot wins over the pair this backend was built with;
        # see ``SubagentBackend.run``. The constructor pair remains the fallback
        # for callers that drive a backend directly.
        provider = provider or self.provider
        model = model or self.model
        # The row's own model, when it has one, over the pair the dispatch
        # brought: a per-agent model is a fact about this agent, and the turn's
        # binding is the fallback it was always meant to be. Read per run
        # because this backend is cached across bindings; a pin that resolves
        # to nothing usable leaves the pair alone (see `live_pin_resolver`).
        pinned = self._pin() if self._pin is not None else None
        if pinned is not None:
            provider, model = pinned.provider, pinned.model
        # Build subagent tools (no message tool, no spawn tool). The gate is
        # unattended by construction: a spawned task inherits the parent turn's
        # context -- responder included -- and a sub-agent must never pop an
        # approval prompt of its own. Its ask-tier calls are refused with the
        # reason in the tool error; the parent can rerun the step in the user's
        # own turn, where approval is interactive.
        from raven.config.live import LiveConfig, permissions_config
        from raven.permissions import BuiltinRulings, PermissionGate
        from raven.permissions.turn import set_current_tool_call_id

        live_config = LiveConfig()

        def _subagent_permissions():
            # The parent turn's mode, not a pin: the user authorized the spawn,
            # not every command the sub-agent composes afterwards. Its ask tier
            # reaches the same responder the parent turn bound -- the ContextVar
            # is inherited into this task -- so a delegated push asks the human
            # exactly as a direct one would, and an unattended parent (cron,
            # one-shot) leaves it unattended too.
            return permissions_config(live_config)

        gate = PermissionGate(
            config_source=_subagent_permissions,
            builtin=BuiltinRulings(
                extra_deny_patterns=self.exec_config.extra_deny_patterns,
                # The same live deny source the main loop's gate reads: a
                # tightened permission must gate a delegated shell the same
                # call it gates a direct one.
                extra_deny_source=_live_exec_extra_deny,
            ),
            allow_ask=True,
        )
        # This lane runs its own loop rather than an ``AgentLoop``, so it has
        # no harness to borrow the Action role from -- it builds the default
        # one for the single thing the registry asks of it: the judgement this
        # dispatch's Charter carries -- wired the way ``AgentLoop`` wires its
        # own, so a charter's ``checks`` hold on this lane as they do on the
        # forked one.
        # Imported here, not at module scope: ``raven.agent.harness`` reaches
        # this module through ``raven.agent.subagent``'s own package import, so
        # naming it at the top closes a cycle that only shows at first import.
        from raven.agent.harness import DefaultAction

        _verifier = DefaultAction()
        tools = ToolRegistry(permission_gate=gate, verifier_provider=lambda: _verifier)
        tools.set_withheld_source(lambda: _withheld_here(tools, self.mcp_source))
        for wrapper, origin in grant.for_registry():
            tools.register(wrapper, origin=origin)

        def allowed(name: str) -> bool:
            return self.tools_allow is None or name in self.tools_allow

        # Two roots, matching the main loop: the session directory the run works
        # in, and agent home, whose absolute paths this prompt hands out.
        allowed_dirs = (workspace, self.agent_home) if self.restrict_to_workspace else ()
        # follow_binding=False: this run is a background asyncio task that can
        # outlive the turn that spawned it, since SubagentManager.spawn captures
        # the workspace at spawn time, so its tools must fence on the directory
        # captured for this run, not on whatever the ambient workdir binding
        # holds by the time they actually execute.
        if allowed("read_file"):
            tools.register(ReadFileTool(workspace=workspace, allowed_dirs=allowed_dirs, follow_binding=False))
        if allowed("write_file"):
            tools.register(WriteFileTool(workspace=workspace, allowed_dirs=allowed_dirs, follow_binding=False))
        if allowed("edit_file"):
            tools.register(EditFileTool(workspace=workspace, allowed_dirs=allowed_dirs, follow_binding=False))
        if allowed("list_dir"):
            tools.register(ListDirTool(workspace=workspace, allowed_dirs=allowed_dirs, follow_binding=False))
        if allowed("exec"):
            tools.register(
                ExecTool(
                    working_dir=str(workspace),
                    timeout=self.exec_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    path_append=self.exec_config.path_append,
                    executor=executor,
                    extra_allowed_dirs=allowed_dirs,
                    follow_binding=False,
                )
            )

        def live_key(vendor: str) -> Callable[[], str]:
            # Read on every call, as the main loop's are: a refused key pauses
            # the tool and points the user at the config slot, so the slot has
            # to be what the next call reads -- for all three tools, since all
            # three carry that advice.
            return lambda: live_vendor_key(_LIVE_CONFIG, vendor, boot=self._web_key(vendor))

        # Withheld without a key, same as the main loop: a sub-agent that reaches
        # for a search it cannot run reports the failure to its caller, and that
        # text ends up in the parent turn. The whitelist stacks on top: a
        # whitelisted web_search without a key is still withheld.
        if allowed("web_search"):
            search_provider = self.web_search_provider
            web_search = WebSearchTool(
                api_key=live_key(search_provider), proxy=self.web_proxy, provider=search_provider
            )
            if web_search.api_key:
                tools.register(web_search)
        if self.image_search and allowed("image_search"):
            picture_vendor = image_search_vendor(self.web_search_provider, self._web_key)
            image_search = ImageSearchTool(
                api_key=live_key(picture_vendor), proxy=self.web_proxy, provider=picture_vendor
            )
            if image_search.api_key:
                tools.register(image_search)
        if allowed("web_fetch"):
            fetch_provider = WebFetchTool.effective_provider(
                self.web_fetch_provider, self._web_key(self.web_fetch_provider)
            )
            tools.register(
                WebFetchTool(api_key=live_key(fetch_provider), proxy=self.web_proxy, provider=fetch_provider)
            )
        # The same browser the parent drives, in a tab of this run's own: the
        # tools name the run in flight as their owner, so two sub-agents
        # browsing at once are two tabs, never one page typed into twice.
        from raven.agent.tools.browser import browser_tools

        for tool in browser_tools():
            if allowed(tool.name):
                tools.register(tool)

        # A resumed instance brings its own history, system prompt included;
        # rebuilding the prompt here would append a second system turn. A
        # fresh run's prompt carries the role's skill whitelist.
        messages: list[dict[str, Any]] = (
            list(history)
            if history
            else [
                {
                    "role": "system",
                    "content": build_subagent_prompt(self.agent_home, workspace, tools.tool_names, self.skills_allow),
                }
            ]
        )
        iteration = 0
        participants = charter_mod.charter_participants()
        original_task = task

        async def participant_answer(verb: str, *args: Any) -> Any:
            if not participants:
                return None
            try:
                return await getattr(participants[0], verb)(*args)
            except Exception:
                logger.exception("generated charter participant %s raised; treating it as silence", verb)
                return None

        def participant_step(phase: str, *, response: Any = None) -> StepView:
            return StepView(
                session_key=session_key or task_id,
                iteration=iteration,
                response=response,
                transcript=tuple(messages),
                history=tuple(history or ()),
                turn_base=len(history or ()),
                question=original_task,
                rollbacks=0,
                mode=None,
                mode_overlay=None,
                phase=phase,
                tools=tuple(tools.get_definitions()),
                max_iterations=self._MAX_ITERATIONS,
            )

        if participants:
            intake = await participant_answer("intake", task, participant_step("user_inbound"))
            if isinstance(intake, Mapping):
                if intake.get("reply") is not None:
                    return str(intake["reply"])
                if isinstance(intake.get("text"), str):
                    task = intake["text"]
        messages.append({"role": "user", "content": with_attachment_note(task, media)})
        # Where this run's own turns begin. Taken here rather than assumed to be
        # index 2, because a resumed instance arrives with its whole history in
        # front of the task -- slicing from a constant would replay every earlier
        # node's work as this node's.
        own_turns_from = len(messages)

        final_result: str | None = None
        # Whether the LAST model response of this run was cut at the output
        # ceiling, not whether any was: a round that was cut and then answered
        # in full delivered its answer, and the verdict reading this is asking
        # what the run has to show for itself.
        cut_at_ceiling = False
        # What this run has written, so a command of its own that removes one of
        # those files reaches the record. Per run, like everything else here: the
        # backend object is shared and other runs write beside this one.
        removal_watch = RemovalWatch()
        while iteration < self._MAX_ITERATIONS:
            iteration += 1
            if participants:
                advice = await participant_answer("advise", participant_step("iteration"))
                if isinstance(advice, str) and advice:
                    _append_participant_note(messages, advice)
            if on_delta is None:
                response = await provider.chat_with_retry(
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=model,
                )
                if response.finish_reason == "error" and not response.has_tool_calls:
                    # The ladder was chat_with_retry's own, so an error here is
                    # one it already gave up on. Raised rather than returned: the
                    # error text would otherwise be the run's answer and the
                    # record would read completed (see SubagentNoAnswerError).
                    activity.note_usage(response.usage)
                    verdict = response.error_classification
                    raise SubagentNoAnswerError(
                        "sub-agent's model call failed"
                        + (f" ({verdict.category})" if verdict is not None else "")
                        + ": "
                        + (response.content or "")[:200]
                    )
            else:
                # A spawned run keeps the retry ladder; only a caller that asked
                # to watch the reply form gives it up (a stream that already
                # rendered deltas cannot be retried without duplicating them).
                # The ladder has to be handed over for that to be true: left to
                # the signature's defaults this call reconnected once and waited
                # never, so one dropped stream ended a run with an hour behind it.
                # The generation settings are passed on so this run answers under
                # the same budget as the same instance's spawns -- chat_stream's
                # signature would otherwise cap it at its own literal 4096.
                response = await stream_llm_call(
                    provider,
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=model,
                    on_token_delta=on_delta,
                    retry_delays=self.retry_delays,
                    retry_after_output=self.retry_after_output,
                    **generation_kwargs(provider),
                )
                if response.finish_reason == "error" and not response.has_tool_calls:
                    # An error reply has two sources: a stream that died before
                    # anything deliverable arrived (still retryable), or one that
                    # died after rendering, whose retry ``stream_llm_call`` has
                    # already spent so the same words are not drawn twice. Either
                    # way its text is a diagnostic, not the answer. The failed
                    # call's tokens were still spent -- a cut mid-thought is 11-15k
                    # reasoning tokens -- so they are billed before the reply is
                    # replaced.
                    activity.note_usage(response.usage)
                    verdict = response.error_classification
                    if verdict is None and (classify := getattr(provider, "classify_error", None)) is not None:
                        verdict = classify(content=response.content or None)
                    if verdict is None or not verdict.retryable:
                        # A refusal the repo has already decided not to retry -- an
                        # oversized or unsupported image -- is refused the same way
                        # a minute later, so asking the same bytes again is waste and
                        # calling it transport is wrong. The run fails on it.
                        raise SubagentNoAnswerError(
                            "sub-agent's model call failed"
                            + (f" ({verdict.category})" if verdict is not None else "")
                            + ": "
                            + (response.content or "")[:200]
                        )
                    # Retryable, and nothing of it was rendered: asking again through
                    # the waited-for call repeats nothing and gets the retry ladder a
                    # watched reply gave up.
                    logger.warning(
                        "Subagent [{}] streamed reply ended in transport ({}); asking again without the stream",
                        task_id,
                        (response.content or "")[:160],
                    )
                    response = await provider.chat_with_retry(
                        messages=messages,
                        tools=tools.get_definitions(),
                        model=model,
                    )
                    if response.finish_reason == "error" and not response.has_tool_calls:
                        activity.note_usage(response.usage)
                        raise SubagentNoAnswerError(
                            "sub-agent's model call failed in transport twice: " + (response.content or "")[:200]
                        )
            # Per iteration, because that is how the cost accrues: this loop calls
            # the model once per round and the run's cost is their sum, unlike an
            # ACP agent's one cumulative report for the whole turn. Both arms
            # land here -- a streamed reply costs the same as a waited-for one.
            activity.note_usage(response.usage)
            cut_at_ceiling = response.truncated
            if response.has_tool_calls:
                tool_call_dicts = [openai_tool_call(tc) for tc in response.tool_calls]
                messages.append(
                    build_assistant_message(
                        response.content or "",
                        tool_calls=tool_call_dicts,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    )
                )
                for call_index, tool_call in enumerate(response.tool_calls):
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.debug("Subagent [{}] executing: {} with arguments: {}", task_id, tool_call.name, args_str)
                    # Before the call, not after: a tool that raises is still
                    # something the sub-agent did, and it is the one a reader
                    # asking "what happened" most needs to see.
                    activity.note_tool_call(tool_call.name)
                    set_current_tool_call_id(tool_call.id)
                    result = await tools.execute(tool_call.name, tool_call.arguments, run_meta=tool_call.run_meta)
                    # Settled before this call's own write is noted, so the file
                    # it just wrote is not stat'ed to say it exists.
                    for removal in removal_watch.settle(getattr(result, "removed", ())):
                        # No add, and no size: the file is gone, and a zero there
                        # would read as a file that is present and empty.
                        activity.note_file_change(
                            _workspace_relative(removal.path, workspace),
                            "delete",
                            0,
                            len((removal.before or "").splitlines()),
                            None,
                        )
                    removal_watch.note_write(getattr(result, "file_change", None))
                    if (file_change := getattr(result, "file_change", None)) is not None:
                        # The op is the tool's, except that a write onto nothing
                        # is a creation: `before is None` is the only record that
                        # the file did not exist, and a reader draws an added file
                        # differently from a rewritten one.
                        add, delete = _file_change_counts(file_change, getattr(result, "diff", None))
                        if "edit" in RAVEN_NAME.get(tool_call.name, tool_call.name):
                            op = "edit"
                        else:
                            op = "add" if file_change.before is None else "write"
                        activity.note_file_change(
                            _workspace_relative(file_change.path, workspace),
                            op,
                            add,
                            delete,
                            len(file_change.after.encode("utf-8")),
                        )
                    # Recorded beside the call, so the run's account says how
                    # its calls went and not only that it made them. Through the
                    # registry's own predicate: a call refused before dispatch
                    # comes back as a bare string with no `ok` to read.
                    if call_failed(result):
                        activity.note_tool_failure(tool_call.name)
                    # The subagent's loop is an untrusted-data path too — fence its
                    # tool output like the main loop does in add_tool_result.
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": wrap_untrusted(result, source=tool_call.name),
                        }
                    )
                    # In flight, not at the end: the collector is how a panel
                    # watches a running node, and an account that only exists
                    # once the answer does is not a live view of anything. Same
                    # reason the acp lane republishes on every update.
                    blocks_call = getattr(result, "blocks_call", False)
                    if blocks_call:
                        # The siblings in this same response are refused with it:
                        # a refused operation must not be reachable through a
                        # call the model wrote before it knew the answer. They
                        # are answered rather than merely skipped, because the
                        # assistant message above advertises every call id and a
                        # provider that finds one without a matching result
                        # rejects the whole history -- which the next round would
                        # hit on the path where the turn continues.
                        for skipped in response.tool_calls[call_index + 1 :]:
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": skipped.id,
                                    "name": skipped.name,
                                    "content": SKIPPED_AFTER_BLOCKED_CALL,
                                }
                            )
                    activity.note_transcript(messages[own_turns_from:])
                    if blocks_call:
                        # Two decisions: the call is refused either way, and the
                        # continuation says whether the turn survives it.
                        if getattr(result, "continuation", None) is Continuation.ABORT_TURN:
                            raise SubagentActionAbortedError
                        break
                if participants:
                    advice = await participant_answer("advise", participant_step("after_iteration", response=response))
                    if isinstance(advice, str) and advice:
                        _append_participant_note(messages, advice)
            else:
                final_result = response.content
                break

        if final_result is None:
            # The rounds ran out while the model was still calling tools. What it
            # gathered is all in ``messages``, so ask once more with no tools at
            # all: unable to call another, it answers from what it has. Observed
            # need -- a research node spent all fifteen rounds on web_fetch and
            # wrote nothing, and the run's whole output was the placeholder that
            # used to sit here.
            logger.warning(
                "Subagent [{}] used all {} rounds without answering; asking once with no tools",
                task_id,
                self._MAX_ITERATIONS,
            )
            wrap_up = await provider.chat_with_retry(
                messages=[
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "You have used the whole tool budget for this task. Answer now from what "
                            "you already gathered above -- no more tool calls are available. If it is "
                            "incomplete, say what you have and name what is missing."
                        ),
                    },
                ],
                model=model,
            )
            if wrap_up.finish_reason == "error":
                activity.note_usage(wrap_up.usage)
                raise SubagentNoAnswerError("sub-agent's wrap-up model call failed: " + (wrap_up.content or "")[:200])
            final_result = (wrap_up.content or "").strip() or None
            activity.note_usage(wrap_up.usage)
            cut_at_ceiling = wrap_up.truncated
        # Reported before the raise below, so a run that ends with no answer at
        # all carries the reason as well -- that is the shape this exists for.
        if cut_at_ceiling:
            activity.note_output_limit()
        if final_result is None and participants:
            salvaged = await participant_answer("salvage", participant_step("answerless"))
            final_result = salvaged if isinstance(salvaged, str) and salvaged else None
        if final_result is None:
            # Nothing to hand back. Raised rather than returned, so the node
            # fails instead of completing with a sentence the next step would
            # merge as if it were the work.
            raise SubagentNoAnswerError(f"sub-agent used all {self._MAX_ITERATIONS} rounds and produced no answer")
        logger.info("Subagent [{}] completed successfully", task_id)
        # The final state of the account, for whoever opens the node later. The
        # loop above already republished it after every tool result, so this call
        # only matters for a run that answered without calling anything -- and
        # for keeping the last write the complete one. The reader supplies the
        # prompt and the answer itself, so only the middle goes here.
        activity.note_transcript(messages[own_turns_from:])
        if note := grant.note_text():
            notice = f"\n\n[raven] {note}."
            if on_delta is not None:
                await on_delta(notice)
            final_result += notice
        if on_messages is not None:
            messages.append({"role": "assistant", "content": final_result})
            on_messages(messages)
        return final_result
