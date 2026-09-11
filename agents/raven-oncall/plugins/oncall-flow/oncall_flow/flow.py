"""The turn-frame hook: the fork's loop-side on-call glue on the trunk's six phases.

The fork hard-wired four behaviours into its AgentLoop; each is one axis here,
an ``AgentHook`` on the v3 surface (raven/contracts/loop_hooks.py), and the
plugin contributes them as ONE hook running them in a fixed order:

- **turn context** (fork ``_set_tool_context`` + ``_capture_owner_instruction``,
  loop/main.py): window identity and the task fingerprint reach the tool faces
  through ``tools.base`` -- the same instances the loop registered, met through
  the roster -- and what the owner types lands in the watched campaign's notes,
  pulling the pending wake to now when a question was waiting on it.
- **turn accounting** (fork ``TurnOutcome.usage_totals``): the turn's summed
  LLM usage, counted per iteration and stamped into the persisted record
  through ``metadata["observers"]`` at the send fire.
- **the work-to-watch judgement** (fork ``_note_watched_path`` + ops/watched.py):
  one sidecar model call per turn that touches a subject, the verdict cached on
  the turn's own metadata dict, and the provenance line delivered where a
  failure would be -- on the tool result, via the ``append_note`` grant, the
  only channel measured to change behaviour.
- **the turn close** (fork ``Tool.ends_turn`` on ops_check_later): a successful
  ops_check_later ends the turn; the fork's primitive does not exist on the
  trunk, so the close is ``after_iteration``'s ``short_circuit_result``, keyed
  on the call's name plus its result's own first words ("Scheduled a wake" --
  every refusal branch deliberately starts otherwise).

Per-turn state rides the turn's ONE metadata dict (seeded at the inbound fire,
same dict through the iterations and the send -- the trunk pins this), under
the freight keys below; ``after_send`` pops every key, so nothing can leak
into a later turn even on a host that skips a phase.

The axes are composed here rather than as four manifest rows because the
registry serves hook names sorted, and the axis order is part of this flow's
contract (context before anything that reads it; accounting before the close
so the closing call is counted). The import discipline (stdlib + contracts +
own package) keeps the kernel's CompositeHook out of reach, so ``_run_phase``
mirrors its documented semantics: first halting state wins and carries the
trail, notes chain in order joined by a blank line, a raising axis is logged
and treated as a no-op.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from loguru import logger

from oncall_flow import wakes, watched
from oncall_flow.escalation import append_note, unanswered_question
from oncall_flow.instrument import is_concluded, log_event
from oncall_flow.tools import base as tools_base
from oncall_flow.window import campaign_for_window, task_fingerprint
from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider
    from raven.plugins.context import PluginContext

# ``ctx.metadata`` is ONE dict per turn: the loop seeds it at the inbound fire
# and hands the same dict to the iterations and the send. Everything the axes
# pass between phases rides it under these keys, and the accounting axis pops
# all three at ``after_send`` -- ``before_user_inbound`` is skipped for some
# turn origins, and a value that survived by accident would stamp one turn's
# judgement on the next (the research-flow freight-key discipline).

# The window's identity as the inbound fire saw it: channel, chat_id, task
# fingerprint. Its absence at iteration 1 is how the context axis knows the
# inbound fire never ran and the turn is a cold start.
_CONTEXT_KEY = "oncall_ctx"

# The one work-to-watch verdict this turn bought (a watched.Verdict). Cached
# whatever it says: "not watched" is as much an answer as "watched", and the
# fork paid for exactly one judgement per turn.
_VERDICT_KEY = "oncall_watched"

# The turn's counters, accumulated by every axis, stamped into
# ``metadata["observers"]`` at the send fire and filed onto the turn's record
# at persist time (hook surface v3).
_ACCOUNT_KEY = "oncall_turn"

# The ACP layer's own observer stash and the one key of it that layer acts on
# (raven/contracts/loop_hooks.py, ``metadata``): a turn that files this ended
# with a wake armed, so the ``session/prompt`` that carried it is kept open --
# the on-call agent sleeps, the prompt does not end -- until a later turn on
# the session ends with no wake pending. Same literal the code-flow plugin
# carries for the stash; nothing in raven/ knows this plugin exists.
_ACP_META_OBSERVER = "acp_meta"
_HOLD_TURN_META = "raven.holdTurn"


def _account(ctx: AgentHookContext) -> dict[str, Any]:
    return ctx.metadata.setdefault(_ACCOUNT_KEY, {})


def _hold_after_turn(session_key: str) -> dict[str, Any] | None:
    """Whether this window's campaign has a wake pending once this turn ends.

    The fork ended the turn on ``ops_check_later`` and let the wake run a later
    turn on its own; over ACP that read as "finished" to the caller, who judged
    the wait a failure (a DAG node spent every continuation on "still watching",
    2026-09-08). The answer here is what the ACP layer holds the prompt open
    on: the campaign this window claimed, not concluded, with one pending wake
    -- read the same way the owner's answer pulls that wake forward. None when
    there is nothing to wait for, or nothing can be read.
    """
    if not session_key:
        return None
    try:
        home = tools_base.ops_home()
        campaign = campaign_for_window(home, session_key)
        if not campaign or is_concluded(home / campaign):
            return None
        job = wakes.pending_look(tools_base.granted_scheduler(), campaign)
        if job is None:
            return None
        state = getattr(job, "state", None)
        until = getattr(state, "next_run_at_ms", None) or getattr(getattr(job, "schedule", None), "at_ms", None)
        return {"untilMs": int(until) if until else None, "why": f"waiting for campaign {campaign}'s next look"}
    except Exception:  # noqa: BLE001 -- a wait that cannot be read is not thereby over; the caller sees an ended turn
        logger.debug("oncall-flow: could not read the pending wake for {}", session_key, exc_info=True)
        return None


def _tool_payload(content: Any) -> str:
    """A tool result's own text, out of the loop's untrusted-content fence.

    The loop fences every tool result (a nonce-tagged BEGIN/END pair) before
    it reaches ``ctx.messages``. This reads the body back out without
    verifying the nonce: the caller has already selected the message by its
    loop-authored ``name`` field, so the body is our own tool's prose, not
    attacker-chosen text.
    """
    text = content if isinstance(content, str) else str(content or "")
    if not text.startswith("[BEGIN UNTRUSTED ") or "\n" not in text:
        return text
    body = text.split("\n", 1)[1]
    inner, _, tail = body.rpartition("\n")
    if tail.startswith("[END UNTRUSTED "):
        return inner
    return body


def _last_tool_payload(messages: list[dict[str, Any]], name: str) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "tool" and message.get("name") == name:
            return _tool_payload(message.get("content"))
    return ""


# ── Axis 1: turn context ────────────────────────────────────────────


def _capture_owner_instruction(session_key: str, content: str, origin: str) -> dict[str, Any]:
    """Put what the owner typed into the campaign this window is watching.

    The fork's function of the same name (loop/main.py:246-295), on the
    plugin's own state: recording every message rather than the ones that
    look like instructions, because deciding which sentence is an instruction
    is the same judgement that was being missed (measured: four campaigns
    asked the owner a question, zero recorded the answer). Only a window that
    has claimed a campaign, and only messages from a person -- a wake turn's
    prompt is not the owner speaking.

    Returns the counters the accounting axis stamps, {} when nothing landed.
    """
    if origin != "user" or not session_key or session_key.startswith("cron:"):
        return {}
    text = (content or "").strip()
    if not text or text.startswith("/"):
        return {}
    try:
        home = tools_base.ops_home()
        campaign = campaign_for_window(home, session_key)
        if not campaign:
            return {}
        cdir = home / campaign
        if (cdir / "concluded.json").exists():
            return {}
        # Read BEFORE the note is written: any note at or after the ask closes
        # the question (escalation.unanswered_question), and the capture's own
        # note is exactly such a note -- writing first would answer the
        # question with itself and the wake would never be pulled forward.
        question_open = bool(unanswered_question(cdir))
        append_note(cdir, text[:2000], source="owner")
        out: dict[str, Any] = {"owner_note": campaign}
        if question_open:
            # The owner is right here; the wake the loop scheduled while it
            # waited is pulled to now, so the answer is read within seconds
            # rather than after the rest of a twenty-minute timer (fork
            # ``_advance_campaign_wake``: the event logs whether a wake was
            # there to pull).
            scheduler = tools_base.granted_scheduler()
            if scheduler is not None:
                woke = wakes.pending_look(scheduler, campaign) is not None
                log_event(cdir, "owner_answered", woke=woke)
                if woke:
                    wakes.advance_look(scheduler, campaign)
                out["owner_woke"] = woke
        return out
    except Exception:  # noqa: BLE001 -- a note that cannot be written must not drop the message
        return {}


class TurnContextHook(AgentHook):
    """Window identity to the faces; the owner's words onto the record.

    ``before_user_inbound`` reads the turn's window off ``turn_request``
    (channel/chat_id from the Source, the session key from the context),
    fingerprints the task from this turn's own message (the fork's
    ``_task_statement``: not the session's first -- one window does task A,
    then chat, then task B), pushes all of it onto every adopted tool face,
    and captures the owner's message into the bound campaign. The iteration
    fallback covers turn origins whose inbound fire is skipped: an empty
    channel resolves exactly like the fork's empty context -- a cold start,
    routed by the campaign's own declaration.
    """

    @property
    def name(self) -> str:
        return "OncallTurnContext"

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        request = ctx.turn_request
        source = getattr(request, "source", None)
        channel = str(getattr(source, "channel", "") or "")
        chat_id = str(getattr(source, "chat_id", "") or "")
        text = ctx.inbound_content if ctx.inbound_content is not None else str(getattr(request, "text", "") or "")
        task = task_fingerprint(text)
        ctx.metadata[_CONTEXT_KEY] = {"channel": channel, "chat_id": chat_id, "task": task}
        faces = tools_base.set_turn_context(channel, chat_id, ctx.session_key or "", task)
        acct = _account(ctx)
        acct["faces"] = faces
        # Compared as the string the StrEnum is, so the axis needs no import
        # from the spine: raven.spine.turn.Origin.USER == "user".
        origin = str(getattr(request, "origin", "") or "")
        acct.update(_capture_owner_instruction(ctx.session_key or "", text, origin))
        return HookDecision()

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if ctx.iteration != 1 or _CONTEXT_KEY in ctx.metadata:
            return HookDecision()
        task = task_fingerprint(ctx.turn_question or "")
        ctx.metadata[_CONTEXT_KEY] = {"channel": "", "chat_id": "", "task": task}
        acct = _account(ctx)
        acct["faces"] = tools_base.set_turn_context("", "", ctx.session_key or "", task)
        acct["cold_context"] = True
        return HookDecision()


# ── Axis 2: turn accounting ─────────────────────────────────────────

_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


class TurnAccountingHook(AgentHook):
    """The turn's summed usage, and the stamp that files every axis's counters.

    The fork summed usage at the provider boundary into
    ``TurnOutcome.usage_totals`` (calls counted beside the tokens, a call
    without usage counted separately rather than passed over -- a provider
    that stops reporting must not look like a cheap turn); the trunk has no
    such column, so the sum is kept here, one increment per iteration, and
    stamped through ``metadata["observers"]`` -- filed onto the turn's last
    substantive assistant message at persist time (hook surface v3).

    ``after_send`` is also where every freight key comes off the metadata
    dict: the counters are read, the context and the verdict are spent, and
    nothing of this turn survives into the next one's dict.
    """

    @property
    def name(self) -> str:
        return "OncallTurnAccounting"

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        acct = _account(ctx)
        acct["calls"] = int(acct.get("calls") or 0) + 1
        usage = getattr(ctx.response, "usage", None)
        if isinstance(ctx.response, dict):
            usage = ctx.response.get("usage")
        if usage:
            for key in _USAGE_KEYS:
                acct[key] = int(acct.get(key) or 0) + int(usage.get(key, 0) or 0)
        else:
            acct["calls_without_usage"] = int(acct.get("calls_without_usage") or 0) + 1
        return HookDecision()

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        ctx.metadata.pop(_CONTEXT_KEY, None)
        ctx.metadata.pop(_VERDICT_KEY, None)
        acct = ctx.metadata.pop(_ACCOUNT_KEY, None)
        if acct:
            ctx.metadata.setdefault("observers", {})["oncall_flow"] = dict(acct)
        hold = _hold_after_turn(ctx.session_key or "")
        if hold is not None:
            observers = ctx.metadata.setdefault("observers", {})
            observers.setdefault(_ACP_META_OBSERVER, {})[_HOLD_TURN_META] = hold
            if "oncall_flow" in observers:
                observers["oncall_flow"]["held"] = True
        return HookDecision()


# ── Axis 3: the work-to-watch judgement ─────────────────────────────

# The looking tools and the argument that names what they looked at (the
# fork's table, loop/main.py). The machine face rides trunk exec's own
# ``machine`` parameter, so ``exec`` covers a look whichever computer it
# lands on.
_WATCHED_TOOLS = {
    "list_dir": "path",
    "read_file": "path",
    "grep": "path",
    "find": "path",
    "exec": "command",
    "web_fetch": "url",
}


class WatchedPathHook(AgentHook):
    """One judgement per turn; one line where a failure would have arrived.

    Skipped for a window that already has a campaign (the question this line
    asks has been answered for that window, and asking again would tell a
    loop that is driving a campaign to go and declare one), and without a
    provider (the judge is a sidecar call on the lent model, never the acting
    turn's own budget). A judgement that cannot be made leaves the look
    exactly as it would have been -- the fork's asymmetry: a missing line
    costs nothing today, a wrong line is one sentence that does not apply.
    """

    def __init__(self, provider: "LLMProvider | None" = None, model: str | None = None) -> None:
        self._provider = provider
        self._model = model

    @property
    def name(self) -> str:
        return "OncallWatchedPath"

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        subjects: list[tuple[str, str]] = []
        for call in getattr(ctx.response, "tool_calls", None) or []:
            key = _WATCHED_TOOLS.get(getattr(call, "name", ""))
            if not key:
                continue
            subject = str((getattr(call, "arguments", None) or {}).get(key) or "")
            if subject:
                subjects.append((key, subject))
        if not subjects or self._provider is None:
            return HookDecision()
        try:
            if campaign_for_window(tools_base.ops_home(), ctx.session_key or ""):
                return HookDecision()
        except Exception:  # noqa: BLE001 -- an unreadable binding is not a reason to go quiet
            pass
        try:
            verdict = ctx.metadata.get(_VERDICT_KEY)
            if verdict is None:
                reply = await self._provider.chat_with_retry(
                    messages=watched.build_prompt(ctx.turn_question or ""),
                    model=self._model,
                )
                verdict = watched.read_verdict(getattr(reply, "content", None))
                ctx.metadata[_VERDICT_KEY] = verdict
                _account(ctx)["watched"] = {
                    "judged": True,
                    "watched": verdict.watched,
                    "subjects": len(verdict.subjects),
                }
            if not verdict.watched:
                return HookDecision()
            for key, subject in subjects:
                if key == "command":
                    # The paths inside a command, plus the command whole: the
                    # subject the owner named rarely reappears verbatim, and
                    # the whole-command hit is what lets anchor matching see
                    # a percent-encoded slug (fork, measured 2026-08-28).
                    hits = re.findall(r"(/[^\s'\"|;&>]+)", subject)
                    hits += re.findall(r"(https?://[^\s'\"|;&>]+)", subject)
                    hits.append(subject)
                else:
                    hits = [subject]
                if any(verdict.claims(h) for h in hits):
                    counters = _account(ctx).setdefault("watched", {})
                    counters["nudges"] = int(counters.get("nudges") or 0) + 1
                    return HookDecision(append_note=watched.provenance_line().strip())
        except Exception:  # noqa: BLE001 -- a look must not fail over a judgement
            logger.debug("watched-path judgement skipped", exc_info=True)
        return HookDecision()


# ── Axis 4: work killed at the local exec cap ───────────────────────

# The sandbox executor's kill report, rendered by ExecResult.as_text: the only
# shape a cap kill produces on the local shell. The machine channel's cap writes
# its own sentence and needs no note here.
_EXEC_CAP_KILL = re.compile(r"STDERR:\nTimed out after (\d+)(?:\.\d+)?s\b")

# A command whose long half is moving bytes, not computing: its client runs
# locally, so the background lane is its door, never ops_submit.
_TRANSFER_SHAPE = re.compile(r"\b(scp|rsync|sftp|curl|wget)\b")


class ExecCapKillHook(AgentHook):
    """A local exec kill is a routing signal, not a transient failure.

    A bare "Timed out after 600s" reads as a fault to retry: measured
    2026-09-02 on the fork, a nine-minute training run was killed at the cap,
    the result it had computed died with it, and the loop re-ran the same
    command into the same wall. The work was never going to fit a synchronous
    shell, and the door that fits it is contributed by this very plugin -- an
    active flow always has ops_submit on the roster, so the note points there
    unconditionally (the fork gated the same sentence on its ops surface
    being present).

    Said as a note under the result rather than a rewrite of it: the fork
    appended to the tool result text it rendered itself; here the result
    belongs to the trunk's exec tool, and ``append_note`` is the loop's grant
    for exactly this line.
    """

    @property
    def name(self) -> str:
        return "OncallExecCapKill"

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        calls = getattr(ctx.response, "tool_calls", None) or []
        by_id = {
            getattr(call, "id", None): str((getattr(call, "arguments", None) or {}).get("command") or "")
            for call in calls
            if getattr(call, "name", "") == "exec"
        }
        by_id.pop(None, None)
        if not by_id:
            return HookDecision()
        for message in reversed(ctx.messages or []):
            if message.get("role") != "tool" or message.get("tool_call_id") not in by_id:
                continue
            payload = _tool_payload(message.get("content"))
            hit = _EXEC_CAP_KILL.search(payload)
            if hit is None or "Exit code: -1" not in payload:
                continue
            cap = int(hit.group(1))
            command = by_id[message.get("tool_call_id")]
            _account(ctx)["exec_cap_kill"] = {"cap_s": cap}
            # Two doors for two shapes. A transfer's client runs locally and
            # spends no budget, so ops_submit is the wrong pointer for it --
            # measured 2026-09-03 on this note's first real firing, a killed
            # whole-tree scp was pointed at a campaign it should never become.
            if _TRANSFER_SHAPE.search(command):
                return HookDecision(
                    append_note=(
                        f"Killed at the {cap}s exec cap; the copy so far is incomplete. "
                        "Re-running it the same way dies at the same cap. A transfer belongs "
                        "in the background lane: run it again with exec's "
                        "run_in_background=true (it logs to a managed file this turn does "
                        "not wait on), and cut what you copy -- a .venv does not travel."
                    )
                )
            return HookDecision(
                append_note=(
                    f"Killed at the {cap}s exec cap; whatever it computed is gone with it. "
                    "Re-running it here dies at the same cap. Work that outlives the cap "
                    "belongs to ops_submit: the job runs detached, logs to disk, and writes "
                    "result.json to the ledger, so nothing is lost when it finishes after "
                    "this turn."
                )
            )
        return HookDecision()


# ── Axis 5: the turn close ──────────────────────────────────────────

# Every refusal branch of ops_check_later deliberately starts with REFUSED or
# the budget prose; only a scheduled wake starts with this (part 2b keyed the
# faces so this axis could judge by shape).
_WAKE_NOTE_PREFIX = "Scheduled a wake"


class TurnCloseHook(AgentHook):
    """The fork's ``ends_turn``, said as ``after_iteration``'s short circuit.

    A successful ops_check_later means the next decision belongs to the wake
    it scheduled; the fork took the tools away and let the model write one
    closing reply, the rebuilt shape ends the turn on the wake note itself --
    the verdict judged the two behaviourally equivalent, and the note already
    says everything the turn decided (when it comes back, and why).
    """

    @property
    def name(self) -> str:
        return "OncallTurnClose"

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        calls = getattr(ctx.response, "tool_calls", None) or []
        if not any(getattr(call, "name", "") == "ops_check_later" for call in calls):
            return HookDecision()
        note = _last_tool_payload(ctx.messages or [], "ops_check_later")
        if not note.startswith(_WAKE_NOTE_PREFIX):
            return HookDecision()
        _account(ctx)["closed_by"] = "ops_check_later"
        return HookDecision(short_circuit_result=note)


# ── The contributed hook ────────────────────────────────────────────

_CHAIN_MODIFIED = frozenset({"after_send", "before_user_inbound"})
_CHAIN_TOOLS = frozenset({"before_iteration"})
_CHAIN_NOTES = frozenset({"before_iteration", "before_execute_tools", "after_iteration"})


class OncallFlowHook(AgentHook):
    """The one manifest hook: five axes, fixed order, composite semantics.

    Order is the contract: context first (everything downstream reads what it
    set), accounting before the close (the closing call is still counted),
    the judgement and the cap-kill note before the close (the fork noted
    results before it read the ends-turn flag). ``_run_phase`` mirrors the
    kernel CompositeHook's
    documented behaviour -- first halting state halts and carries the trail,
    notes chain joined by a blank line, content and tool modifications chain
    through the context, a raising axis is a logged no-op -- because the
    plugin's import discipline stops at the contracts layer.
    """

    def __init__(self, provider: "LLMProvider | None" = None, judge_model: str | None = None) -> None:
        self._axes: tuple[AgentHook, ...] = (
            TurnContextHook(),
            TurnAccountingHook(),
            WatchedPathHook(provider, judge_model),
            ExecCapKillHook(),
            TurnCloseHook(),
        )

    @property
    def name(self) -> str:
        return "OncallFlowHook"

    @property
    def axes(self) -> tuple[AgentHook, ...]:
        return self._axes

    async def _run_phase(self, phase: str, ctx: AgentHookContext) -> HookDecision:
        last_modified: str | None = None
        last_tools: list[dict[str, Any]] | None = None
        notes: list[str] = []
        trail: list[str] = []
        for axis in self._axes:
            try:
                decision = await getattr(axis, phase)(ctx)
            except Exception:  # noqa: BLE001 -- one flaky axis must not take down the turn
                logger.exception("oncall-flow axis {} raised in {}; treated as a no-op", axis.name, phase)
                continue
            if decision.short_circuit_result is not None or decision.rollback:
                return replace(decision, notes=[*trail, *decision.notes])
            if phase in _CHAIN_MODIFIED and decision.modified_content is not None:
                if phase == "before_user_inbound":
                    ctx.inbound_content = decision.modified_content
                else:
                    ctx.outbound_content = decision.modified_content
                last_modified = decision.modified_content
            if phase in _CHAIN_TOOLS and decision.modified_tools is not None:
                ctx.tools = decision.modified_tools
                last_tools = decision.modified_tools
            if phase in _CHAIN_NOTES and decision.append_note:
                notes.append(decision.append_note)
            trail.extend(decision.notes)
        return HookDecision(
            modified_content=last_modified,
            modified_tools=last_tools,
            append_note="\n\n".join(notes) or None,
            notes=trail,
        )

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        return await self._run_phase("before_user_inbound", ctx)

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        return await self._run_phase("before_iteration", ctx)

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        return await self._run_phase("before_execute_tools", ctx)

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        return await self._run_phase("after_iteration", ctx)

    async def terminal_answerless(self, ctx: AgentHookContext) -> HookDecision:
        return await self._run_phase("terminal_answerless", ctx)

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        return await self._run_phase("after_send", ctx)


def make_flow_hook(ctx: "PluginContext") -> OncallFlowHook | None:
    """Factory for the manifest's one hook row.

    Declines (returns None) when the slice leaves the flow off -- the same D6
    gate every tool factory applies, so a disabled product steers nothing.
    Wires the shared home exactly as the tool factories do: whichever lane
    the registry builds first, the roster and the campaign root agree.
    """
    from oncall_flow.config import FlowConfig, state_root

    raw = dict(ctx.config or {})
    if not FlowConfig.from_slice(raw).enabled:
        return None
    tools_base.set_home(state_root(raw, ctx.services.workspace))
    return OncallFlowHook(provider=ctx.services.provider)


__all__ = [
    "OncallFlowHook",
    "TurnAccountingHook",
    "TurnCloseHook",
    "TurnContextHook",
    "WatchedPathHook",
    "make_flow_hook",
]
