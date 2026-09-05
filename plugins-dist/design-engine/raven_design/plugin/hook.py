"""The design turn frame: domain selection in, task-state projection through.

The fork ran the Visual Domain Selector inside context assembly -- a fork-only
parameter on its SkillsSegmentBuilder, gathered in parallel with general skill
routing -- and projected the resident Task State onto a transient copy of the
transcript before every model call (fork ``agent/loop/main.py:1184-1230``).
Here both ride the loop's own hook phases, per the amended verdict:

* ``before_user_inbound`` -- run one selection over the fixed domain catalog
  and append the card block below a separator to the model's view of the
  inbound text. The session record keeps the user's own words on every turn
  outcome: the loop persists ``inbound_original`` on the healthy save and --
  sealed at the engine wave's kernel repair -- on the cancelled/failed save too
  (``_save_broken_turn`` used to drop it, which let a hook's rewrite land in
  history on exactly the outcomes users hit mid-task). So the block reaches
  neither the transcript on disk nor the memory extractor -- the D1
  structural guarantee, pinned by the plugin family's parameterized history
  tests, one per exit door. The four D1 rebuild clauses
  are landed here as code: (1) the rewritten inbound reaches every downstream
  consumer (personalizer, model router, general skill routing, the assembly /
  scent / memory-recall query, ``turn_question``), so the block stays cards-
  only, never bodies, below a ``---`` separator; (2) a command-shaped or
  blank inbound is never rewritten -- hooks fire before slash dispatch;
  (3) the seat assumes pull discovery: the shipped product config never sets
  ``skillForge.discovery``, and switching the host to push would inject the
  general lane's full bodies beside these cards (the double-injection trap
  the verdict forbids); (4) the selection call rides ``active_binding()``
  first -- the fork's own binding order -- so a session ``/model`` switch
  reaches it.

  One selection is one LLM call embedding all fifteen full SKILL.md bodies:
  2,721 lines / 246,422 bytes measured, roughly 60-90k input tokens at CJK
  tokenizer rates (C7). The catalog is constant -- a caching provider pays
  it once per prefix -- and ``visualDomainSelector.enabled`` is the off
  switch. Selection failure degrades to the full description catalog for the
  turn (the fork's shape), never to a broken turn.

* ``before_iteration`` -- project the current Task State as an
  ``append_note`` before every model call (the C2/HIGH-1 seat). State is
  keyed by the bound working directory -- the only session identity the tool
  seat shares with this hook -- so two sessions pointed at one directory
  share one resident list (the fork keyed per session id; ledgered as D5's
  fourth loss line, self-healing because ``initialize`` replaces the whole
  state). The fork
  stripped stale projections from a transient copy; a note appended here
  stays where it landed in the transcript, so an older projection can also
  be eaten by a mid-turn compaction -- both are the ledgered D5 loss, and
  both self-heal because every iteration appends the fresh snapshot.

* ``after_send`` -- two reply tails, in the fork's own order: the completion
  notice while unfinished items remain (fork ``main.py:2928-2931``), then the
  caller-workspace git summary the fork launcher appended after every answer
  (fork ``run.py:357-378``, the amended D3 rebuild) -- names and counts, not
  a patch, and only when the bound working directory is a git tree; a
  workspace that is not a checkout passes untouched. The rewritten reply is
  what ``context_engine.after_turn`` receives, so Curator bookkeeping sees
  the appended tails (bounded, replaced each turn; the ppt after_send rides
  the same channel) -- the session record and the memory store do not.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.agent import workdir
from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision
from raven.memory_engine.skill_local.registry import SkillRegistry
from raven_design.selector import VisualDomainSkillSelector

if TYPE_CHECKING:
    from raven_design.plugin.config import EngineConfig
    from raven_design.task_state.manager import TaskStateManager

logger = logging.getLogger(__name__)

#: The always-read foundation skill the card block names ahead of the
#: preferred cards (the fork's ``builtin/visual-artifact-design``, respelled
#: to the localDirs namespace).
FOUNDATION_SKILL_ID = "local/visual-artifact-design"

#: Never a real registry workspace: the packaged corpus is mounted as an
#: extra dir, and the registry's workspace mkdir attempt fails silently on
#: this path by design (its own OSError guard).
_PROTOTYPE_WORKSPACE = Path("/nonexistent/design-engine-prototype")

_MALFORMED_SLICE_ERROR = 'the design-engine config slice is malformed; fix plugins.config["design-engine"]'


def packaged_skills_dir() -> Path:
    """The corpus shipped inside this wheel."""
    return Path(__file__).resolve().parent.parent / "skills"


def build_selector(cfg: "EngineConfig") -> VisualDomainSkillSelector:
    """Construct the selector over the packaged corpus, fail-closed.

    The registry mounts the wheel's own skills directory the same way the
    launcher mounts it for the host (``skillForge.localDirs``, rendered
    per-entry and keyed by path), so the ids the cards carry
    (``local/<name>``) are the ids the host's ``read_skill`` resolves. A
    corpus with a missing or empty domain skill raises here, at activation --
    the factory catches it and casts the sentinel rather than serving a
    selector that lies about its catalog.
    """
    registry = SkillRegistry(
        _PROTOTYPE_WORKSPACE,
        builtin_skills_dir=_PROTOTYPE_WORKSPACE / "builtin",
        extra_dirs=[(packaged_skills_dir(), "design-engine", True)],
    )
    return VisualDomainSkillSelector.from_registry(
        provider=None,
        registry=registry,
        preferred_max=cfg.selector.preferred_max,
        alternatives_max=cfg.selector.alternatives_max,
        temperature=cfg.selector.temperature,
        max_tokens=cfg.selector.max_tokens,
    )


def render_selection_block(selection: Any) -> str:
    """The fork's Skills-segment block (context_engine/segments/render.py),
    re-said for the message seat: cards only, bodies read on demand."""
    preferred = list(getattr(selection, "preferred", ()) or ())
    alternatives = list(getattr(selection, "alternatives", ()) or ())
    if not preferred and not alternatives:
        return ""
    lines = [
        "The Visual Domain Selector compared the complete packaged SKILL.md bodies for all domain candidates.",
        "The required Skill bodies are not inlined. For a visual task, before planning or taking action, call "
        f"`read_skill` for `{FOUNDATION_SKILL_ID}` and every Skill under Preferred Skills below unless "
        "its complete body is already present in the current context. Preferred Skills are the strongest matches; "
        "Alternative Skills are optional and should be read only when their procedures would help.",
    ]
    if getattr(selection, "degraded", False):
        lines.append(
            "The selector call failed, so the full description catalog is shown as alternatives for this turn."
        )
    for title, cards in (("Preferred Skills", preferred), ("Alternative Skills", alternatives)):
        if not cards:
            continue
        lines.extend(["", f"## {title}"])
        for card in cards:
            lines.append(f"- `{card.qualified_id}`: {card.description}")
    return "\n".join(lines)


def completion_notice(manager: "TaskStateManager", session_key: str) -> str | None:
    """The fork's turn-end nudge (main.py:1208-1230), verbatim semantics."""
    state = manager.get(session_key)
    if state is None:
        return None
    unfinished = [
        (number, item["status"]) for number, item in enumerate(state["items"], start=1) if item["status"] != "completed"
    ]
    if not unfinished:
        return None
    pending = [str(number) for number, status in unfinished if status in {"pending", "in_progress"}]
    waiting = [str(number) for number, status in unfinished if status in {"waiting", "blocked"}]
    parts = []
    if pending:
        parts.append(f"unfinished item_number values: {', '.join(pending)}")
    if waiting:
        parts.append(f"waiting or blocked item_number values: {', '.join(waiting)}")
    return "[Task State] This task is not complete; " + "; ".join(parts) + "."


def describe_changes(repo: Path) -> str:
    """Summarise the working-tree footprint of a run, when the workspace is a
    checkout -- the fork launcher's reply tail, verbatim semantics.

    A name-and-count summary rather than a patch: the agent edits the
    caller's real files, so what the caller needs from the reply is where to
    look. Clean trees still answer ("no files were changed"), the fork's own
    behaviour; a missing git or a failing status answers with silence.
    """
    git = shutil.which("git")
    if git is None:
        return ""

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([git, *args], cwd=str(repo), capture_output=True, text=True)

    status = run("status", "--porcelain")
    if status.returncode != 0:
        return ""
    lines = [line for line in status.stdout.splitlines() if line.strip()]
    if not lines:
        return "no files were changed in the working tree"
    stat = run("diff", "--stat")
    body = "\n".join(lines[:40])
    if len(lines) > 40:
        body += f"\n... and {len(lines) - 40} more"
    tail = stat.stdout.strip().splitlines()[-1:] if stat.returncode == 0 else []
    summary = f"working tree of {repo} after this run:\n{body}"
    if tail:
        summary += f"\n{tail[0].strip()}"
    return summary


class MisconfiguredEngineHook(AgentHook):
    """Fail-closed sentinel cast when the config slice cannot be parsed.

    A raising factory is logged and SKIPPED by the lenient stack builder, so
    letting the parse error escape would boot a design product with no
    selector, no render tools and no task state under a config that says the
    engine is on -- the silent degradation the MisconfiguredGate doctrine
    (w101) exists to prevent. This sentinel takes the hook's seat instead:
    every turn is answered with the config fix named, and the deploy stays
    loud until someone repairs the slice. Deliberately cast even when
    ``enabled`` was meant to be false: an unparseable slice proves nothing
    about intent, and closed-and-loud beats open-and-quiet.
    """

    def __init__(self, error: str) -> None:
        self._error = error

    @property
    def name(self) -> str:
        return "design_engine"

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        return HookDecision(short_circuit_result=(f"{_MALFORMED_SLICE_ERROR}: {self._error}", []))


class DesignEngineHook(AgentHook):
    """Domain selection on the way in; task-state projection and nudge through."""

    def __init__(
        self,
        cfg: "EngineConfig",
        selector: VisualDomainSkillSelector | None,
        manager: "TaskStateManager | None",
    ) -> None:
        self._cfg = cfg
        self._selector = selector
        self._manager = manager

    @property
    def name(self) -> str:
        return "design_engine"

    @staticmethod
    def _session_key() -> str | None:
        bound = workdir.current()
        return str(bound) if bound is not None else None

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        if self._selector is None:
            return HookDecision()
        text = ctx.inbound_content
        # D1 clause 2: hooks fire before slash dispatch, so a command-shaped
        # inbound must pass through untouched or "/new" stops working; a blank
        # one has nothing to classify.
        if not text or not text.strip() or text.lstrip().startswith("/"):
            return HookDecision()
        try:
            selection = await self._selector.select(text)
        except Exception as exc:  # the selector already degrades; this is belt
            logger.warning("design-engine: selection failed outside the selector's own guard: %s", exc)
            return HookDecision()
        block = render_selection_block(selection)
        if not block:
            return HookDecision()
        # D1 clauses 1 and 3: cards only, below a separator, on the pull lane.
        return HookDecision(modified_content=f"{text}\n\n---\n{block}")

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if self._manager is None:
            return HookDecision()
        key = self._session_key()
        if key is None:
            return HookDecision()
        try:
            block = self._manager.render(key)
        except Exception as exc:
            logger.warning("design-engine: task-state projection failed: %s", exc)
            return HookDecision()
        if not block:
            return HookDecision()
        return HookDecision(append_note=block)

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        bound = workdir.current()
        reply = ctx.outbound_content or ""
        if bound is None or not reply.strip():
            return HookDecision()
        suffixes: list[str] = []
        if self._manager is not None:
            notice = completion_notice(self._manager, str(bound))
            if notice is not None:
                suffixes.append(notice)
        # The fork launcher's order kept: the loop appended the notice to the
        # final answer, the wrapper appended the git summary after that.
        if (Path(bound) / ".git").exists():
            changes = describe_changes(Path(bound))
            if changes:
                suffixes.append(f"--- {changes}")
        if not suffixes:
            return HookDecision()
        return HookDecision(modified_content="\n\n".join([reply, *suffixes]))


__all__ = [
    "FOUNDATION_SKILL_ID",
    "DesignEngineHook",
    "MisconfiguredEngineHook",
    "build_selector",
    "completion_notice",
    "packaged_skills_dir",
    "render_selection_block",
]
