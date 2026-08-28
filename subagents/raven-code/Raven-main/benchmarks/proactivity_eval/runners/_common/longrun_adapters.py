"""Agent adapter protocol for longrun multi-agent evaluation.

Each adapter exposes the minimal surface the calendar-driven loop needs:
- async start()
- async send_user_message(content, session_key, fake_now) → reply
- async tick_to(target_fake_now, on_agent_event) — advance fake_clock;
  may emit agent-initiated events during the advance
- async stop()
- cleanup()

Three implementations:
- RavenAdapter: in-process AgentLoop + Sentinel (existing)
- HermesAdapter: subprocess per turn into isolated HERMES_HOME
- OpenClawAdapter: docker run per turn into isolated OPENCLAW_HOME

Adapters emit events via the on_agent_event callback — the driver logs
them as trajectory lines (kind="sentinel_tick" or "hermes_cron_fire" etc.).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from loguru import logger

EventEmitter = Callable[[dict[str, Any]], None]


_LONGRUN_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "longrun"


# App-lifespan providers log to stdout during an ``raven agent -m`` boot
# even under ``--no-logs`` (the flag mutes Raven's own logger, not the
# provider stack that starts before it). Those structlog lines corrupt the
# captured reply, so drop them here. Fixing the CLI leak is a separate task;
# this only sanitizes the stdout the eval reads back.
_RUNTIME_LOG_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[\s*(?:debug|info|warning|error|critical)\s*\]")


def _strip_runtime_logs(text: str) -> str:
    kept = [ln for ln in text.splitlines() if not _RUNTIME_LOG_RE.match(ln)]
    return "\n".join(kept).strip()


def _raven_soft_dnd_only() -> bool:
    """Contrast-run toggle (#2). When ``LONGRUN_RAVEN_SOFT_DND`` is set, the
    harness injects NO hard DND enforcement into raven — neither the
    sentinel ``nudge_policy.do_not_disturb_windows`` config windows nor the
    ``attention.md ## User overrides`` DSL that NudgePolicy hard-gates on.
    Raven then relies only on the quiet-hours text in MEMORY.md, i.e. the
    same soft, LLM-judged compliance regime hermes/OpenClaw run under. Both
    hard paths derive the same windows, so gating only one is a near-no-op;
    the toggle exists to measure raven's restraint without the disclosed
    config-hardening asymmetry. Default (unset) keeps hard enforcement."""
    return os.environ.get("LONGRUN_RAVEN_SOFT_DND", "").strip().lower() in ("1", "true", "yes", "on")


def _load_scorer_quiet_windows(persona_id: str) -> list[dict[str, Any]]:
    """Derive DND windows from the persona's *unconditional* Type-C
    ``nudge_count_in_window == 0`` outcomes so the policy enforces the
    scorer's hard quiet zones.

    Only unconditional constraints are injected. A conditional restraint
    (``nudge_count_in_window == 0 when topic=work``) bans nudges for one
    topic, not the whole window — turning it into a blanket hard-DND would
    both suppress rubric-allowed nudges and enforce a quiet zone that lives
    only in the rubric (not in the persona's seeded memory), which would
    hand raven a scoring answer-key the other agents cannot see.

    Both the scorer's ``_in_daily_window`` and ``DndWindow.matches`` use an
    exclusive end (``start <= t < end``), so each derived window maps 1:1
    to the rubric window with no boundary bump.
    """
    path = _LONGRUN_DATA_DIR / f"persona-{persona_id}-outcomes.yaml"
    if not path.exists():
        return []
    try:
        import yaml as _yaml

        data = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    windows: list[dict[str, Any]] = []
    for o in data.get("type_c_restraint") or []:
        constraint = (o.get("constraint") or "").strip()
        base, _, condition = constraint.partition(" when ")
        if base.strip() != "nudge_count_in_window == 0" or condition:
            continue
        wd = o.get("window_daily") or []
        if len(wd) != 2:
            continue
        try:
            sh_s, sm_s = wd[0].split(":")
            eh_s, em_s = wd[1].split(":")
            sh, sm = int(sh_s), int(sm_s)
            eh, em = int(eh_s), int(em_s)
        except (ValueError, AttributeError):
            continue
        windows.append(
            {
                "start_hour": sh,
                "start_minute": sm,
                "end_hour": eh,
                "end_minute": em,
                "why": f"scorer_window:{o.get('id', '')}",
            }
        )
    return windows


class AgentAdapter(ABC):
    """Minimal surface a longrun-compatible agent adapter must expose."""

    agent_name: str = "abstract"

    @abstractmethod
    async def start(self) -> None:
        """Initialize agent stack (e.g. start sentinel loop)."""

    @abstractmethod
    async def send_user_message(
        self,
        content: str,
        *,
        session_key: str,
        fake_now: datetime,
    ) -> str:
        """Deliver a user turn, return agent's reply text."""

    @abstractmethod
    async def tick_to(
        self,
        target_fake_now: datetime,
        *,
        current_fake_now: datetime,
        emit: EventEmitter,
    ) -> datetime:
        """Advance fake_now to target. Emit agent_initiated events along
        the way. Return the new fake_now (may equal target)."""

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def cleanup(self) -> None: ...

    # Optional — default to empty set (adapters that don't support memory)
    def final_memory_md(self) -> str | None:
        """Return current MEMORY.md content for scorecard (or None)."""
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Raven — subprocess via ``proactivity_eval.RavenDriver``.
#
# Phase 4 replaced the original in-process ``IsolatedWorkspace`` wrapper
# with this subprocess shell. Every send_user_message / sentinel-tick is
# a fresh ``python -m raven …`` invocation; persistent state lives
# under the per-persona tempdir + ``~/.raven/sentinel/state.json``.


def _cron_registered_event(
    cron: dict,
    *,
    fake_now: datetime,
    trigger: str,
    trigger_content: str,
) -> dict:
    """Provenance record for a newly observed cron/reminder registration.

    The scorecard's Type A decision-attribution reads ``trigger``:
    ``user_turn`` (+ the turn's user text in ``trigger_content``) marks a
    standing order the user asked for; ``cron_fire`` / ``between_turns``
    mark the agent's own initiative.
    """
    return {
        "kind": "cron_registered",
        "fake_now": fake_now.isoformat(),
        "cron_id": str(cron.get("id") or ""),
        "cron_name": str(cron.get("name") or cron.get("message") or "")[:160],
        "cron_prompt": str(cron.get("prompt") or cron.get("message") or "")[:200],
        "trigger": trigger,
        "trigger_content": (trigger_content or "")[:300],
    }


def _resolve_raven_repo() -> Path:
    """Find the raven checkout (in-repo eval lives inside it).

    Layout: longrun_adapters.py → _common → runners → proactivity_eval
    → benchmarks → <repo root>. RAVEN_REPO env var overrides.
    """
    env = os.environ.get("RAVEN_REPO")
    if env:
        p = Path(env).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"RAVEN_REPO does not exist: {p}")
        return p
    candidate = Path(__file__).resolve().parents[4]
    if (candidate / "raven" / "__main__.py").exists():
        return candidate
    raise FileNotFoundError(
        f"Could not locate the raven checkout at {candidate}. "
        "Set RAVEN_REPO=<path> to the dir containing raven/__main__.py."
    )


def _seed_raven_home(
    home_dir: Path,
    workspace: Path,
    persona: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> Path:
    """Create a per-persona raven config dir.

    Copies ``~/.raven/config.json`` to ``<home_dir>/config.json``,
    repointing ``agents.defaults.workspace`` at the per-persona
    workspace. The returned path is what callers pass as ``--config``
    to the raven CLI; downstream ``get_data_dir() / get_sentinel_dir()
    / get_cron_dir()`` then all resolve under ``<home_dir>/`` instead of
    the shared ``~/.raven/`` — that's the isolation Phase D buys.

    When ``persona`` carries ``policy_overrides.do_not_disturb_windows``,
    those windows are merged into ``sentinel.nudge_policy.do_not_disturb_windows``
    so per-persona quiet bands (lunch / pickup / kid-bedtime / weekend
    sleep-in) reach the Sentinel — mirrors what the pre-subprocess
    in-process ``build_isolated`` did. Without this, persona-yaml DND
    windows are silently dropped.

    When ``overrides`` carries ``planner_model``, it's written into
    ``sentinel.evaluator_model`` so the per-persona subprocess Planner
    (and the DailyAnalysis / DailyPlan / Behaviors producers that fall
    through to ``evaluator_model`` when their own ``.model`` is unset)
    uses the requested LLM. Agent-side ``agents.defaults.model`` stays
    untouched — agent reply LLM and Planner LLM are independent knobs.
    """
    import json as _json

    home_dir.mkdir(parents=True, exist_ok=True)
    src = Path.home() / ".raven" / "config.json"
    if not src.exists():
        raise FileNotFoundError(
            f"Per-persona isolation requires ~/.raven/config.json to exist "
            f"(it's the template the copy is derived from). Got: {src}"
        )
    cfg = _json.loads(src.read_text(encoding="utf-8"))
    # Repoint workspace; preserve everything else (provider, model, etc).
    cfg.setdefault("agents", {}).setdefault("defaults", {})["workspace"] = str(workspace)
    # Pin the Sentinel eval config instead of inheriting whatever the live
    # ~/.raven/config.json happens to carry — an empty/disabled sentinel
    # block silently turns the anticipatory channel off (zero ticks, zero
    # warnings; caught in the 2026-07-22 pre-flight smoke). The nudge_policy
    # values below equal the raven factory defaults (isolating the run from
    # live-config drift only). ``enabled`` and ``task_discovery_enabled`` are
    # the exception: both ship False by default and are explicitly opened here
    # to exercise the anticipatory channel — that is NOT as-shipped, and the
    # README discloses both opt-ins.
    sent = cfg.setdefault("sentinel", {})
    sent["enabled"] = True
    # tick_interval matches the adapter's actual 1800s batch grid so the
    # config never claims a denser cadence than what fires.
    sent["tick_interval_seconds"] = 1800
    sent["task_discovery_enabled"] = True
    default_model = (cfg.get("agents", {}).get("defaults", {}) or {}).get("model")
    if default_model:
        sent.setdefault("evaluator_model", default_model)
    np_cfg = sent.setdefault("nudge_policy", {})
    # PRODUCTION quotas, pinned explicitly for determinism (the live
    # ~/.raven/config.json must not leak into eval runs). Raven runs
    # as-shipped like the competitors; note the 2026-06 historical
    # baseline ran with opened quotas (200/h) — v2 numbers are therefore
    # not comparable to that baseline on delivery volume.
    np_cfg["max_nudges_per_hour"] = 3
    np_cfg["max_nudges_per_day"] = 10
    np_cfg["hour_quota_multiplier"] = 1.0
    # Factory default is 0.5 (L6 weekend tightener). A stale 1.0 leftover from
    # the opened-quota iteration used to disable it — which quietly doubled
    # raven's weekend nudge cap and contradicted the "as-shipped" claim.
    np_cfg["weekend_quota_multiplier"] = 0.5
    if persona is not None and not _raven_soft_dnd_only():
        dnd_raw = list((persona.get("policy_overrides") or {}).get("do_not_disturb_windows") or [])
        # Append scorer-derived quiet windows (type_c_restraint
        # ``nudge_count_in_window == 0`` outcomes). End-minute is bumped
        # +1 to match the scorer's inclusive-end semantics.
        pid = persona.get("id")
        if pid:
            dnd_raw.extend(_load_scorer_quiet_windows(pid))
        if dnd_raw:
            cfg.setdefault("sentinel", {}).setdefault("nudge_policy", {})["do_not_disturb_windows"] = dnd_raw
    # No config-level soft-DND stamp: raven's SentinelConfig is extra="forbid",
    # so an unknown key would make the CLI reject the config. The durable
    # markers are the driver's ``run_meta`` trajectory event + scorecard
    # ``soft_dnd`` field; the absent ``do_not_disturb_windows`` corroborates.
    if overrides and overrides.get("planner_model"):
        cfg.setdefault("sentinel", {})["evaluator_model"] = overrides["planner_model"]
    # Host-safety, eval-scoped (NOT a product default): this harness runs the
    # raven agent UN-SANDBOXED on the operator's machine, so block host GUI
    # automation (osascript / ``open -a|-b``) that would otherwise wake
    # Music/Messages/Notes. Injected via config so raven's shipped default is
    # untouched — the block lives here, in the eval, not in raven/agent/tools.
    cfg.setdefault("tools", {}).setdefault("exec", {})["extra_deny_patterns"] = [
        r"\bosascript\b",
        r"\bopen\s+-[ab]\b",
    ]
    dst = home_dir / "config.json"
    dst.write_text(_json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return dst


_QUIET_INLINE_RE = re.compile(
    r"quiet[_\s]?hours?[:\s]+(\d{1,2})[:：](\d{2})\s*[-—~]\s*(\d{1,2})[:：](\d{2})",
    re.IGNORECASE,
)
# 翻译时段（11:00-15:00）...  X 时段 HH:MM-HH:MM ... 不希望被打断
_QUIET_PROSE_RE = re.compile(
    r"(\d{1,2})[:：](\d{2})\s*[-—~]\s*(\d{1,2})[:：](\d{2}).{0,40}(?:不希望|静默|别打扰|勿扰|专注)",
)
# 周末 X 点前勿扰 / 周末早上 9:00 前
_WEEKEND_RE = __import__("re").compile(
    r"周末.{0,8}?(\d{1,2})[:：]?(\d{2})?.{0,8}?(?:前|起).{0,8}?(?:勿扰|静默|别打扰)",
)


def _derive_dnd_lines_from_persona(persona: dict[str, Any]) -> list[str]:
    """Scan persona ``initial_memory_md`` for free-text quiet windows and
    emit DSL lines compatible with ``derive_dnd.parse_user_overrides_dnd``.

    Persona-yaml is the ground truth for "what the user told the system
    before this run started". The structured ``policy_overrides`` field
    is one expression; many personas only have free-text in
    ``initial_memory_md``. Surface both into ``attention.md ## User
    overrides`` so NudgePolicy enforces them uniformly.
    """
    text = persona.get("initial_memory_md") or ""
    lines: list[str] = []
    seen: set[tuple] = set()

    for m in _QUIET_INLINE_RE.finditer(text):
        sh, sm, eh, em = m.groups()
        key = (sh, sm, eh, em, "quiet")
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- dnd: {int(sh):02d}:{sm}-{int(eh):02d}:{em} reason=quiet_hours")

    for m in _QUIET_PROSE_RE.finditer(text):
        sh, sm, eh, em = m.groups()
        key = (sh, sm, eh, em, "prose")
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- dnd: {int(sh):02d}:{sm}-{int(eh):02d}:{em} weekdays=Mon-Fri reason=focus_block")

    for m in _WEEKEND_RE.finditer(text):
        sh, sm = m.groups()
        sm = sm or "00"
        key = ("weekend", sh, sm)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- dnd: 00:00-{int(sh):02d}:{sm} weekdays=Sat-Sun reason=weekend_morning_quiet")

    # Also import the structured ``policy_overrides`` if present — keeps
    # the unified pipeline working for personas that DO have it (dev,
    # parent, student).
    dnd_raw = (persona.get("policy_overrides") or {}).get("do_not_disturb_windows") or []
    for w in dnd_raw:
        sh = int(w.get("start_hour", 0))
        sm = int(w.get("start_minute", 0))
        eh = int(w.get("end_hour", 0))
        em = int(w.get("end_minute", 0))
        why = w.get("why", "").split()[0] if w.get("why") else "policy_override"
        weekdays = w.get("weekdays")
        line = f"- dnd: {sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
        if weekdays:
            day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            spec = ",".join(day_names[d] for d in weekdays)
            line += f" weekdays={spec}"
        line += f" reason={why or 'policy_override'}"
        key = (sh, sm, eh, em, tuple(weekdays or ()), "struct")
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)

    return lines


def _seed_attention_user_overrides(workspace: Path, persona: dict[str, Any]) -> None:
    """Write the derived DND DSL into ``<ws>/user_memory/attention.md``
    under the ``## User overrides`` H2 so NudgePolicy picks it up on the
    first Sentinel tick (via ``parse_user_overrides_dnd`` →
    ``policy.set_user_override_dnd``). Eliminates the
    persona-data-divergence bug where personas with free-text quiet
    hours had their preferences silently dropped."""
    if _raven_soft_dnd_only():
        # #2 contrast run: no hard DND enforcement — raven relies on the
        # MEMORY.md quiet-hours text alone, matching the competitors' regime.
        return
    lines = _derive_dnd_lines_from_persona(persona)
    if not lines:
        return
    user_memory = workspace / "user_memory"
    user_memory.mkdir(parents=True, exist_ok=True)
    body = "\n".join(lines)
    attention_md = user_memory / "attention.md"
    # If file exists (defensive: it shouldn't on cold-start), splice the
    # section; otherwise scaffold a minimal file.
    if attention_md.exists():
        from raven.memory_engine.consolidate.attention import upsert_section

        new_text = upsert_section(
            attention_md.read_text(encoding="utf-8"),
            "## User overrides",
            body,
        )
    else:
        new_text = f"## User overrides\n{body}\n"
    attention_md.write_text(new_text, encoding="utf-8")


def _seed_workspace(workspace: Path, persona: dict[str, Any]) -> None:
    """Mirror the seeding that the old ``build_isolated()`` did:

    - ``<ws>/memory/MEMORY.md`` from ``persona.initial_memory_md`` (or
      a header-only placeholder if absent)
    - ``<ws>/memory/HISTORY.md`` empty
    - ``<ws>/sessions/`` empty dir
    - ``<ws>/user_memory/attention.md`` ``## User overrides`` derived
      from persona free-text quiet_hours + structured policy_overrides

    The new raven still reads MEMORY.md from ``<workspace>/memory/`` per
    ``MemoryStore.__init__`` (verified in the refactor branch).
    """
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    (workspace / "sessions").mkdir(parents=True, exist_ok=True)
    init_memory = (persona.get("initial_memory_md") or "").strip()
    mem_md = workspace / "memory" / "MEMORY.md"
    mem_md.write_text(
        (init_memory + "\n") if init_memory else "# Long-term Memory\n",
        encoding="utf-8",
    )
    (workspace / "memory" / "HISTORY.md").write_text("", encoding="utf-8")
    _seed_attention_user_overrides(workspace, persona)


class RavenAdapter(AgentAdapter):
    agent_name = "raven"

    def __init__(
        self,
        *,
        driver,  # proactivity_eval.RavenDriver
        workspace: Path,
        persona: dict[str, Any],
        owns_workspace: bool,
    ) -> None:
        # ``driver`` is intentionally not typed at module level to avoid
        # circular imports between ``proactivity_eval.driver`` (which
        # depends on no internal packages) and this module.
        self.driver = driver
        self.workspace = workspace
        self.persona = persona
        self._owns_workspace = owns_workspace
        self._last_sentinel_tick: datetime | None = None
        self._pending_cron_events: list[dict] = []
        self._known_cron_ids: set[str] = {str(j["id"]) for j in self._cron_jobs_snapshot()}

    def _cron_jobs_snapshot(self) -> list[dict]:
        path = self.workspace / "ec-home" / "cron" / "jobs.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        jobs = data.get("jobs") if isinstance(data, dict) else data
        return [j for j in jobs or [] if isinstance(j, dict) and j.get("id")]

    def _scan_new_crons(self, *, fake_now: datetime, trigger: str, trigger_content: str) -> None:
        for j in self._cron_jobs_snapshot():
            jid = str(j["id"])
            if jid in self._known_cron_ids:
                continue
            self._known_cron_ids.add(jid)
            self._pending_cron_events.append(
                _cron_registered_event(j, fake_now=fake_now, trigger=trigger, trigger_content=trigger_content)
            )

    def _flush_cron_events(self, emit: EventEmitter) -> None:
        for ev in self._pending_cron_events:
            emit(ev)
        self._pending_cron_events.clear()

    # ------------------------------------------------------------------
    # PendingDecisionStore observation
    #
    # The daily TaskDiscoverer batch fires inside a ``sentinel ticks``
    # subprocess as a side-channel of ``_maybe_run_task_discovery`` — it
    # writes a PendingDecision to pending_decisions.json but does NOT
    # show up in the per-tick JSON the CLI prints (tick action is the
    # Planner.decide outcome, usually "skip" on the tick that fired
    # discovery). To make ``kind=discovery_menu`` events land in the
    # trajectory, we snapshot pending_decisions.json before each
    # ``driver.sentinel_ticks(...)`` batch and diff after.

    def _load_pending_decisions(self) -> list[dict[str, Any]]:
        """Read the per-persona ``pending_decisions.json``. Returns ``[]``
        when the file is missing, the driver config isn't set, or the
        payload is corrupt."""
        if self.driver.config is None:
            return []
        path = self.driver.config.parent / "sentinel" / "pending_decisions.json"
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("decisions", [])
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "pending_decisions.json unreadable at {}: {}: {}",
                path,
                type(exc).__name__,
                exc,
            )
            return []

    @classmethod
    async def build(
        cls,
        persona: dict[str, Any],
        *,
        resume_root: Path | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> "RavenAdapter":
        from .raven_driver import RavenDriver

        # Per-persona sandbox: one tempdir holds BOTH the workspace
        # (memory/sessions/...) AND a copy of ~/.raven/ pointed at it.
        # Passing the per-persona config path as ``--config`` to every
        # raven subprocess redirects sentinel/state.json + cron/jobs.json
        # under this tempdir, so parallel personas can't contaminate each
        # other's NudgePolicy dedup state.
        root = Path(tempfile.mkdtemp(prefix=f"longrun-{persona['id']}-"))
        workspace = root / "workspace"
        ec_home = root / "ec-home"

        if resume_root is not None:
            # Resume copies the checkpointed root contents in. The
            # checkpoint preserved the same workspace/ + ec-home/ layout.
            for child in resume_root.iterdir():
                dest = root / child.name
                if child.is_dir():
                    shutil.copytree(child, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(child, dest)
        else:
            workspace.mkdir()
            _seed_workspace(workspace, persona)
        config_path = _seed_raven_home(ec_home, workspace, persona, overrides)

        driver = RavenDriver(
            raven_repo=_resolve_raven_repo(),
            workspace=workspace,
            config=config_path,
            # Each subprocess gets a generous cap — agent --message can
            # do multi-step tool loops; tighten via env if needed.
            timeout_seconds=float(os.environ.get("EVAL_AGENT_TIMEOUT_SEC", "180")),
        )
        return cls(
            driver=driver,
            workspace=root,
            persona=persona,
            owns_workspace=True,
        )

    async def start(self) -> None:
        # Subprocess starts fresh each call — nothing persistent to start.
        self._last_sentinel_tick = None

    async def send_user_message(
        self,
        content: str,
        *,
        session_key: str,
        fake_now: datetime,
    ) -> str:
        # ``--fake-now`` propagates end-to-end via AgentLoop's ``now_fn``
        # (B1) → ContextBuilder.``_build_runtime_context`` (B2) injects
        # ``Current Time: <fake_now>`` into the system prompt, and
        # CronService (B3) uses the same callable so all internal
        # timestamps align. No ``[sim_context]`` preamble needed — that
        # was an interim eval-side workaround removed once now_fn was
        # threaded through.
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self.driver.send_message(
                content,
                fake_now=fake_now.isoformat(),
                session_id=session_key,
            ),
        )
        self._scan_new_crons(fake_now=fake_now, trigger="user_turn", trigger_content=content)
        if not response.ok:
            logger.warning(
                "agent send_message returned rc={}: {}",
                response.returncode,
                response.stderr[:400],
            )
            return ""
        # The agent CLI prints rendered output to stdout; strip the
        # leaked app-lifespan log lines so only the reply remains.
        return _strip_runtime_logs(response.stdout)

    async def tick_to(
        self,
        target_fake_now: datetime,
        *,
        current_fake_now: datetime,
        emit: EventEmitter,
    ) -> datetime:
        """Fire sentinel ticks every 30 min between (current, target].

        Packs the whole window into a single ``raven sentinel ticks``
        subprocess — the Sentinel stack (ContextAssembler / NudgePolicy /
        MemoryStore) is built ONCE for the batch and reused across every
        tick, rather than re-importing + re-building per tick. On the
        ~80% of ticks that hit fast_path_skip (quiet hours / dedup),
        this is a >20x speedup; on Planner-LLM ticks the LLM still
        dominates wall clock.

        Ticks fire at ``_last_sentinel_tick + N*30min`` boundaries; ticks
        not aligned to that grid are skipped (same semantics as the
        previous per-tick implementation).

        F-J-medium: before sentinel ticks, also poll
        ``ec-home/cron/jobs.json`` for user-scheduled crons that fall in
        (current, target] and emit ``kind=cron_fire`` events. Without
        this, EC's cron service (an in-process asyncio task) never gets
        to tick because each ``raven agent --message`` subprocess
        exits before the cron timer fires. The result: jobs.json
        accumulates user-asked reminders that the eval harness never
        observes — Scheduled execution counts as 0 and Type C scoring
        misses all the "fires at user-safe times" signal. Hermes /
        OpenClaw adapters mirror this same dance via their own cron
        store polling; F-J-medium gives EC parity.

        F-J-medium ALSO writes each cron fire into the Sentinel ledger
        (state.json's ``topic_fired_at`` + sentinel/feedback.jsonl)
        directly — mirroring what F-G's ``_record_cron_dispatch_to_ledger``
        does in production. The next sentinel ticks subprocess loads
        state.json fresh and the topic_quota gate sees the cron fires,
        so Sentinel skips redundant proactive nudges on the same topic.
        """
        # Registrations observed since the last turn: sentinel-tick
        # subprocesses can also create crons — anything new here that
        # wasn't buffered by send_user_message is the agent's own doing.
        self._scan_new_crons(fake_now=current_fake_now, trigger="between_turns", trigger_content="")
        self._flush_cron_events(emit)

        # ── F-J-medium: cron polling pass ────────────────────────────
        self._fire_due_crons(current_fake_now, target_fake_now, emit)

        interval = timedelta(seconds=1800)
        if self._last_sentinel_tick is None:
            self._last_sentinel_tick = current_fake_now

        # First aligned tick strictly after the last one we fired.
        first_tick = self._last_sentinel_tick + interval
        if first_tick > target_fake_now:
            # No tick fires in this window — caller advances time but no
            # 30-min boundary was crossed.
            return target_fake_now

        # Last aligned tick at or before target.
        steps = int((target_fake_now - first_tick).total_seconds() // interval.total_seconds())
        last_tick = first_tick + steps * interval
        n_ticks = steps + 1

        # Generous timeout: budget ~120s per active-hour tick (LLM) + a
        # floor for batch import overhead. Most ticks will fast_path
        # skip, so this is far above the realistic ceiling.
        timeout = max(60.0, 120.0 * n_ticks)

        # Snapshot pending decisions BEFORE the batch so any new entry
        # the daily TaskDiscoverer writes mid-batch shows up in the diff.
        pre_decision_ids = {d.get("decision_id") for d in self._load_pending_decisions()}

        loop = asyncio.get_event_loop()
        try:
            results = await loop.run_in_executor(
                None,
                lambda: self.driver.sentinel_ticks(
                    from_iso=first_tick.isoformat(),
                    to_iso=last_tick.isoformat(),
                    interval_seconds=int(interval.total_seconds()),
                    live=True,
                    timeout_seconds=timeout,
                ),
            )
        except Exception as exc:
            logger.warning(
                "sentinel ticks batch failed ({} ticks {}→{}): {}: {}",
                n_ticks,
                first_tick.isoformat(),
                last_tick.isoformat(),
                type(exc).__name__,
                exc,
            )
            self._last_sentinel_tick = last_tick
            self._scan_new_crons(fake_now=last_tick, trigger="between_turns", trigger_content="")
            self._flush_cron_events(emit)
            return target_fake_now

        for r in results:
            tick_iso = r.fake_now or ""
            if not r.ok and r.action is None:
                logger.warning(
                    "sentinel tick at {} failed: rc={} stderr={}",
                    tick_iso,
                    r.returncode,
                    (r.raw_stderr or "")[:200],
                )
                continue
            emit(
                {
                    "kind": "sentinel_tick",
                    "fake_now": tick_iso,
                    "action": r.action,
                    "route": r.route,
                    "delivered": r.delivered,
                    "reason": (r.reason or "")[:200],
                    "priority": r.priority,
                    "target_session": r.target_session,
                    "nudge_message": r.nudge_message,
                    "topic_tag": r.topic_tag,
                    "content": r.nudge_message or "",  # scorecard reads this
                }
            )

        # Daily-batch discovery menus: any PendingDecision created during
        # this batch is a TaskDiscoverer fire that the per-tick JSON
        # doesn't surface. Emit them as ``kind=sentinel_tick`` with
        # ``action=discovery_menu`` + ``delivered=True`` so the existing
        # scorecard counter (``_count_proactive_messages`` / line ~561)
        # picks them up without scorecard changes.
        post_decisions = self._load_pending_decisions()
        new_decisions = [d for d in post_decisions if d.get("decision_id") and d["decision_id"] not in pre_decision_ids]
        for d in new_decisions:
            options = d.get("options") or []
            titles = [o.get("title", "") for o in options]
            # ``created_at_ms`` is fake_now epoch — TaskDiscoverer threads
            # ``now_fn`` through, so this aligns with the surrounding
            # tick stream. Strip tz: tick events use naive isoformat.
            menu_dt = datetime.fromtimestamp(int(d.get("created_at_ms", 0)) / 1000)
            emit(
                {
                    "kind": "sentinel_tick",
                    "fake_now": menu_dt.isoformat(),
                    "action": "discovery_menu",
                    "route": "task_discovery",
                    "delivered": True,
                    "reason": "daily discovery batch",
                    "priority": "medium",
                    "target_session": f"{d.get('channel', '')}:{d.get('to', '')}",
                    "nudge_message": " | ".join(t for t in titles if t),
                    "topic_tag": None,
                    "content": " | ".join(t for t in titles if t),
                    # Extras for downstream / debugging (scorecard ignores).
                    "decision_id": d.get("decision_id"),
                    "n_options": len(options),
                    "option_ids": [o.get("id") for o in options],
                    "option_titles": titles,
                }
            )

        self._last_sentinel_tick = last_tick
        # Crons created DURING the sentinel batch are the agent's own
        # decisions — scan here, before the next user turn's scan would
        # mislabel them trigger="user_turn" with unrelated content.
        self._scan_new_crons(fake_now=last_tick, trigger="between_turns", trigger_content="")
        self._flush_cron_events(emit)
        return target_fake_now

    # ──────────────────────────────────────────────────────────────────
    # F-J-medium: cron polling (production CronService surrogate)
    #
    # Reads ec-home/cron/jobs.json directly and synthesizes cron_fire
    # events for jobs whose nextRunAtMs falls in the (current, target]
    # window. Also mirrors F-G's ledger writes (state.json topic_fired_at
    # + sentinel/feedback.jsonl) so the next sentinel ticks subprocess
    # sees the cron fires when it loads state.

    _CRON_FIRE_CAP = 200  # match HermesAdapter; runaway-loop safety net

    def _fire_due_crons(
        self,
        current_fake_now: datetime,
        target_fake_now: datetime,
        emit: EventEmitter,
    ) -> None:
        jobs_file = self.workspace / "ec-home" / "cron" / "jobs.json"
        if not jobs_file.exists():
            return
        try:
            data = json.loads(jobs_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not jobs:
            return

        def _norm(dt: datetime) -> datetime:
            return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

        def _ms_to_dt(ms: int | None) -> datetime | None:
            if not ms:
                return None
            try:
                return _norm(datetime.fromtimestamp(ms / 1000))
            except (OSError, ValueError):
                return None

        cur_n = _norm(current_fake_now)
        tgt_n = _norm(target_fake_now)

        def _next_fire(j: dict) -> datetime | None:
            """Resolve a job's next fire time using fake_now semantics.

            ALWAYS recomputes from schedule — never trusts
            state.nextRunAtMs because EC's production CronService
            populates that field with real-wall-clock arithmetic (it
            doesn't know about fake_now). In eval, real_now is far in
            the future of fake_now, so state.nextRunAtMs lands either
            None ("in the past") or jumps to "tomorrow real-time" for
            recurring crons. Recompute from schedule.kind + cur_n.
            """
            sched = j.get("schedule") or {}
            kind = (sched.get("kind") or "").lower()
            state = j.get("state") or {}
            # Track our own per-job last_fire in eval mode (separate
            # from production's lastRunAtMs which is real-clock).
            last_fire = _ms_to_dt(state.get("evalLastFiredMs"))
            if kind == "at":
                # One-shot: never re-fire once evalLastFiredMs is set —
                # required because the due window has no lower bound.
                if last_fire is not None:
                    return None
                return _ms_to_dt(sched.get("atMs"))
            if kind == "every":
                ev_ms = sched.get("everyMs") or 0
                if not ev_ms:
                    return None
                # For recurring "every", anchor at the LATER of (eval
                # last_fire, job createdAtMs, cur_n - 1*every). Without
                # any prior fire, fire first time at cur_n + ev_ms.
                if last_fire is not None:
                    return last_fire + timedelta(milliseconds=int(ev_ms))
                created = _ms_to_dt(j.get("createdAtMs"))
                if created is not None and created <= cur_n:
                    # Job was created within fake window — fire first
                    # at created + ev_ms (matches real CronService).
                    return created + timedelta(milliseconds=int(ev_ms))
                return cur_n + timedelta(milliseconds=int(ev_ms))
            if kind == "cron" and sched.get("expr"):
                try:
                    from croniter import croniter

                    # Anchor at later of eval last_fire or cur_n. Note:
                    # croniter returns the FIRST run strictly after the
                    # anchor; daily "0 7 * * *" with anchor 06:50 returns
                    # same-day 07:00 (✓), with anchor 07:00 returns
                    # next-day 07:00.
                    base = last_fire if last_fire is not None else cur_n
                    return _norm(croniter(sched["expr"], base).get_next(datetime))
                except Exception:
                    return None
            return None

        def _collect_due() -> list[tuple[datetime, dict]]:
            out: list[tuple[datetime, dict]] = []
            for j in jobs:
                if not j.get("enabled", True):
                    continue
                nxt = _next_fire(j)
                if nxt is None:
                    continue
                # No lower bound: jobs registered mid-tick with a fire time
                # inside the already-processed window fire late instead of
                # never. Refire safety: "at" returns None once fired;
                # "every"/"cron" anchor on evalLastFiredMs.
                if nxt <= tgt_n:
                    out.append((nxt, j))
            out.sort(key=lambda x: x[0])
            return out

        due = _collect_due()
        fire_count = 0
        ledger_writes: list[tuple[datetime, str | None, dict]] = []

        while due and fire_count < self._CRON_FIRE_CAP:
            fire_time, job = due.pop(0)
            fire_count += 1
            job_id = job.get("id") or "unknown"
            job_name = job.get("name") or job_id
            payload = job.get("payload") or {}
            message = payload.get("message") or job_name
            topic_tag = payload.get("topicTag")  # may be None — F-G optional

            emit(
                {
                    "kind": "cron_fire",
                    "fake_now": fire_time.isoformat(),
                    "delivered": True,
                    "action": "nudge",
                    "route": "cron",
                    "nudge_message": message,
                    "topic_tag": topic_tag or f"cron_{job_id[:12]}",
                    "cron_id": job_id,
                    "cron_name": job_name,
                    "priority": "low",
                    "target_session": "default",
                    "reason": f"cron fired: {job_name}",
                    "content": message,
                }
            )

            # Ledger writes: mirror what F-G's
            # _record_cron_dispatch_to_ledger does in production. Defer
            # the actual mutation until after we've processed all due
            # fires so we only open/write state.json + feedback.jsonl
            # once per tick_to (cheap, but jobs.json is per-fire updated).
            ledger_writes.append((fire_time, topic_tag, job))

            # Bookkeeping under our own ``evalLastFiredMs`` field so we
            # don't fight production's lastRunAtMs (which is real-clock-
            # based and updated only when a real CronService tick fires).
            # _next_fire reads evalLastFiredMs to anchor the next fire.
            sched = job.get("schedule") or {}
            kind = (sched.get("kind") or "").lower()
            job_state = job.setdefault("state", {})
            job_state["evalLastFiredMs"] = int(fire_time.timestamp() * 1000)
            if kind == "at":
                # One-shot — disable so it can't fire again.
                job["enabled"] = False

            # Re-queue if recurring next_run still falls in this window.
            # _next_fire now sees the updated evalLastFiredMs and returns
            # the correct next anchor.
            if job.get("enabled", True):
                new_nxt = _next_fire(job)
                if new_nxt is not None and cur_n < new_nxt <= tgt_n:
                    due.append((new_nxt, job))
                    due.sort(key=lambda x: x[0])

        if fire_count == 0:
            return

        # Persist jobs.json with updated nextRunAtMs / enabled flags.
        try:
            tmp = jobs_file.with_suffix(jobs_file.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, jobs_file)
        except OSError as exc:
            logger.warning("F-J-medium jobs.json save failed: {}", exc)

        # F-G mirror: write to state.json topic_fired_at + sentinel/feedback.jsonl.
        self._apply_cron_to_sentinel_ledger(ledger_writes)

    def _apply_cron_to_sentinel_ledger(
        self,
        writes: list[tuple[datetime, str | None, dict]],
    ) -> None:
        """F-G mirror in eval: append each cron fire to state.json's
        topic_fired_at + sentinel/feedback.jsonl. The next sentinel ticks
        subprocess will hydrate from these on startup."""
        if not writes:
            return

        state_path = self.workspace / "ec-home" / "sentinel" / "state.json"
        feedback_path = self.workspace / "ec-home" / "sentinel" / "feedback.jsonl"

        # state.json: load → mutate policy.topic_fired_at → save.
        # Schema: { "policy": { "topic_fired_at": {tag: [iso, ...]}, ... }, ... }
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                state = {}
        else:
            state = {}
        pol = state.setdefault("policy", {})
        tfa = pol.setdefault("topic_fired_at", {})
        for fire_time, topic_tag, _job in writes:
            if not topic_tag:
                continue
            tfa.setdefault(topic_tag, []).append(fire_time.isoformat())
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = state_path.with_suffix(state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(state, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, state_path)
        except OSError as exc:
            logger.warning("F-J-medium state.json save failed: {}", exc)

        # sentinel/feedback.jsonl: append one dispatched line per fire.
        # Use the same field shape NudgeFeedbackTracker writes so the
        # tracker's load() picks them up unchanged.
        try:
            feedback_path.parent.mkdir(parents=True, exist_ok=True)
            with feedback_path.open("a", encoding="utf-8") as f:
                import uuid as _uuid

                for fire_time, topic_tag, job in writes:
                    rec = {
                        "ts": fire_time.isoformat(),
                        "id": _uuid.uuid4().hex[:16],
                        "signal": "dispatched",
                        "action": "nudge",
                        "session_key": f"cron:{job.get('id', 'unknown')}",
                        "priority": "low",
                        "proactivity_score": 0.0,
                        "source": "cron",
                        "details": (
                            {"topic_tag": topic_tag, "cron_id": job.get("id")}
                            if topic_tag
                            else {"cron_id": job.get("id")}
                        ),
                    }
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("F-J-medium sentinel/feedback.jsonl append failed: {}", exc)

    async def stop(self) -> None:
        # No persistent process — subprocess invocations exited already.
        pass

    def cleanup(self) -> None:
        if self._owns_workspace and self.workspace.exists():
            shutil.rmtree(self.workspace, ignore_errors=True)

    def final_memory_md(self) -> str | None:
        # ``self.workspace`` is the tempdir ROOT (build passes workspace=root);
        # the agent's actual workspace lives at root/workspace/.
        mem = self.workspace / "workspace" / "memory" / "MEMORY.md"
        return mem.read_text(encoding="utf-8") if mem.exists() else None


# ─────────────────────────────────────────────────────────────────────────────
# Hermes — subprocess per turn with patched hermes_time.now


_HERMES_INNER_SCRIPT = Path(__file__).resolve().parent / "backends" / "hermes_longrun_inner.py"


class HermesAdapter(AgentAdapter):
    agent_name = "hermes"

    def __init__(
        self,
        persona: dict,
        root: Path,
        *,
        hermes_src: Path,
        python_exe: str,
    ) -> None:
        self.persona = persona
        self.root = root
        self.hermes_home = root / "hermes_home"
        self.hermes_src = hermes_src
        self.python_exe = python_exe
        self._session_id: str | None = None
        self._pending_cron_events: list[dict] = []
        self._known_cron_ids: set[str] = {str(j["id"]) for j in self._cron_jobs_snapshot()}

    def _cron_jobs_snapshot(self) -> list[dict]:
        path = self.hermes_home / "cron" / "jobs.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        jobs = data.get("jobs") if isinstance(data, dict) else data
        return [j for j in jobs or [] if isinstance(j, dict) and j.get("id")]

    def _scan_new_crons(self, *, fake_now: datetime, trigger: str, trigger_content: str) -> None:
        for j in self._cron_jobs_snapshot():
            jid = str(j["id"])
            if jid in self._known_cron_ids:
                continue
            self._known_cron_ids.add(jid)
            self._pending_cron_events.append(
                _cron_registered_event(j, fake_now=fake_now, trigger=trigger, trigger_content=trigger_content)
            )

    def _flush_cron_events(self, emit: EventEmitter) -> None:
        for ev in self._pending_cron_events:
            emit(ev)
        self._pending_cron_events.clear()

    @classmethod
    async def build(
        cls,
        persona: dict[str, Any],
        *,
        resume_root: Path | None = None,
    ) -> "HermesAdapter":
        from .config import get_config

        cfg = get_config()
        if cfg.hermes_src is None:
            raise RuntimeError("Hermes longrun requires systems.hermes_src in runners.config.yaml")
        if resume_root is not None:
            root = resume_root
        else:
            root = Path(tempfile.mkdtemp(prefix=f"longrun-hermes-{persona['id']}-"))
        inst = cls(persona, root, hermes_src=cfg.hermes_src, python_exe=_find_hermes_python(cfg.hermes_src))
        inst._seed_home_if_fresh()
        return inst

    def _seed_home_if_fresh(self) -> None:
        """Copy ~/.hermes/{config.yaml,.env,auth.json} into isolated home if
        not already populated (from resume).

        Honors HERMES_HOME_OVERRIDE (same contract as the pbench
        HermesBackend) so eval runs can pin a model/endpoint config
        without touching the user's live ~/.hermes.
        """
        self.hermes_home.mkdir(parents=True, exist_ok=True)
        override = os.environ.get("HERMES_HOME_OVERRIDE")
        real = Path(override).expanduser() if override else Path.home() / ".hermes"
        for fn in ("config.yaml", ".env", "auth.json"):
            src, dst = real / fn, self.hermes_home / fn
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
        # Seed persona-level memory if available
        memory_dir = self.hermes_home / "memories"
        memory_dir.mkdir(exist_ok=True)
        soul_path = self.hermes_home / "SOUL.md"
        init_mem = (self.persona.get("initial_memory_md") or "").strip()
        if init_mem and not soul_path.exists():
            soul_path.write_text(init_mem + "\n", encoding="utf-8")

    async def start(self) -> None:
        pass  # nothing to start — subprocess per turn

    async def send_user_message(
        self,
        content: str,
        *,
        session_key: str,
        fake_now: datetime,
    ) -> str:
        """Subprocess into hermes_longrun_inner.py which patches
        hermes_time.now and invokes the conversational agent."""
        spec = {
            "user_message": content,
            "session_id": self._session_id,  # None on first turn → hermes creates one
            "resume": self._session_id is not None,
        }
        env = os.environ.copy()
        # strip proxy
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env.pop(k, None)
        env.update(
            {
                "HERMES_HOME": str(self.hermes_home),
                "HERMES_AGENT_SRC": str(self.hermes_src),
                "HERMES_EVAL_FAKE_NOW": _iso_with_tz(fake_now),
                "HERMES_EVAL_TURN_SPEC": json.dumps(spec, ensure_ascii=False),
                "PYTHONPATH": f"{self.hermes_src}{os.pathsep}{env.get('PYTHONPATH', '')}",
            }
        )
        cmd = [self.python_exe, str(_HERMES_INNER_SCRIPT)]

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            self._scan_new_crons(fake_now=fake_now, trigger="user_turn", trigger_content=content)
            return "[hermes timeout]"

        self._scan_new_crons(fake_now=fake_now, trigger="user_turn", trigger_content=content)
        if proc.returncode != 0:
            logger.warning("hermes turn failed rc={} stderr={}", proc.returncode, proc.stderr[-500:])
            return f"[hermes error rc={proc.returncode}]"

        tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
        if not tail:
            return "[hermes no output]"
        try:
            payload = json.loads(tail[-1])
        except json.JSONDecodeError:
            return f"[hermes bad json: {tail[-1][:120]}]"

        if payload.get("session_id"):
            self._session_id = payload["session_id"]
        return payload.get("response", "") or ""

    async def tick_to(
        self,
        target_fake_now: datetime,
        *,
        current_fake_now: datetime,
        emit: EventEmitter,
    ) -> datetime:
        """Poll hermes/cron/jobs.json. Fire any cron whose next_run_at falls
        in (current_fake_now, target_fake_now] by invoking hermes_inner with
        the cron's prompt and the cron's scheduled time as fake_now. Emit
        ``kind=cron_fire`` events. Update next_run_at + last_run_at in
        jobs.json so the same cron doesn't fire twice in one window.
        """
        # Stamp with the window START — target can be hours/days later
        # (overnight gaps) and would shift suggestions across scoring
        # windows.
        self._emit_pending_suggestions(emit, current_fake_now)
        self._flush_cron_events(emit)
        jobs_file = self.hermes_home / "cron" / "jobs.json"
        if not jobs_file.exists():
            return target_fake_now
        try:
            data = json.loads(jobs_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return target_fake_now
        # Hermes stores {"version": 1, "jobs": [...], "updated_at": "..."};
        # support both shapes (legacy was a flat list).
        if isinstance(data, dict):
            jobs = data.get("jobs") or []
            jobs_envelope = data
        elif isinstance(data, list):
            jobs = data
            jobs_envelope = None
        else:
            return target_fake_now
        if not jobs:
            return target_fake_now

        # state.fake_now is naive (treated as UTC by everything else in the
        # benchmark); strip tz from next_run_at so comparisons don't blow up
        # with offset-naive vs offset-aware errors.
        def _norm(dt: datetime) -> datetime:
            return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

        cur_n = _norm(current_fake_now)
        tgt_n = _norm(target_fake_now)

        # Collect due jobs sorted by next_run_at. Iteratively expand the
        # set: a daily cron's first fire at 5/2 06:50 in a 3-day window
        # should also fire on 5/3, 5/4 — after each fire we recompute
        # next_run_at and re-check against tgt_n.
        def _collect_due() -> list[tuple[datetime, dict]]:
            out: list[tuple[datetime, dict]] = []
            for j in jobs:
                if not j.get("enabled", True):
                    continue
                nxt_iso = j.get("next_run_at")
                if not isinstance(nxt_iso, str):
                    continue
                try:
                    nxt = _norm(datetime.fromisoformat(nxt_iso))
                except ValueError:
                    continue
                # No lower bound: a job registered mid-tick with next_run_at
                # inside the already-processed window fires late instead of
                # never (next_run_at advance / enabled=False prevent refires).
                if nxt <= tgt_n:
                    out.append((nxt, j))
            out.sort(key=lambda x: x[0])
            return out

        due = _collect_due()

        # Iteratively fire — recurring crons can fire multiple times in
        # one window. Cap at 200 fires per tick_to to avoid runaway loops.
        fire_count = 0
        while due and fire_count < 200:
            fire_time, job = due.pop(0)
            fire_count += 1
            prompt = job.get("prompt") or ""
            job_id = job.get("id") or "unknown"
            job_name = job.get("name") or job_id
            if not prompt:
                continue
            # Invoke hermes with the cron prompt at fire_time
            spec = {
                "user_message": prompt,
                "session_id": self._session_id,
                "resume": self._session_id is not None,
            }
            env = os.environ.copy()
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env.pop(k, None)
            env.update(
                {
                    "HERMES_HOME": str(self.hermes_home),
                    "HERMES_AGENT_SRC": str(self.hermes_src),
                    "HERMES_EVAL_FAKE_NOW": _iso_with_tz(fire_time),
                    "HERMES_EVAL_TURN_SPEC": json.dumps(spec, ensure_ascii=False),
                    "PYTHONPATH": f"{self.hermes_src}{os.pathsep}{env.get('PYTHONPATH', '')}",
                }
            )
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [self.python_exe, str(_HERMES_INNER_SCRIPT)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except subprocess.TimeoutExpired:
                response = "[hermes cron timeout]"
            else:
                if proc.returncode != 0:
                    response = f"[hermes cron rc={proc.returncode}]"
                else:
                    tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
                    if not tail:
                        response = "[hermes cron no output]"
                    else:
                        try:
                            payload = json.loads(tail[-1])
                            response = payload.get("response", "") or ""
                            if payload.get("session_id"):
                                self._session_id = payload["session_id"]
                        except json.JSONDecodeError:
                            response = f"[hermes cron bad json: {tail[-1][:120]}]"

            # Emit cron_fire event mirroring sentinel_tick shape so the
            # scorecard / viewer can treat them uniformly.
            emit(
                {
                    "kind": "cron_fire",
                    "fake_now": fire_time.isoformat(),
                    "delivered": True,
                    "action": "nudge",
                    "route": "cron",
                    "nudge_message": response,
                    "topic_tag": f"cron_{job_id[:12]}",
                    "cron_id": job_id,
                    "cron_name": job_name,
                    "priority": "medium",
                    "target_session": "default",
                    "reason": f"cron fired: {job_name}",
                }
            )

            # Hermes can register NEW jobs while answering a cron prompt —
            # that's the agent's own initiative, not a user order.
            self._scan_new_crons(fake_now=fire_time, trigger="cron_fire", trigger_content=prompt)
            self._flush_cron_events(emit)

            # Mark as fired; recompute next_run for recurring crons.
            # Hermes schedule schema: {"kind": "cron"|"interval"|"once",
            # "expr": "<cronexpr>" | "every_seconds": N | "run_at": "iso"}.
            job["last_run_at"] = fire_time.isoformat()
            schedule = job.get("schedule") or {}
            kind = (schedule.get("kind") or "").lower()
            if kind == "interval":
                seconds = schedule.get("every_seconds") or schedule.get("seconds") or 0
                if not seconds and schedule.get("minutes"):
                    seconds = int(schedule["minutes"]) * 60
                if seconds:
                    job["next_run_at"] = (fire_time + timedelta(seconds=int(seconds))).isoformat()
                else:
                    job["enabled"] = False
            elif kind == "cron" and schedule.get("expr"):
                try:
                    from croniter import croniter

                    nxt = croniter(schedule["expr"], fire_time).get_next(datetime)
                    job["next_run_at"] = nxt.isoformat()
                except Exception:
                    job["enabled"] = False
            else:
                # one-shot ("once") / unknown — disable so it doesn't refire.
                job["enabled"] = False

            # If this job's NEW next_run_at also falls in the window
            # (recurring cron crossing multiple days), re-add to due queue.
            if job.get("enabled", True):
                new_iso = job.get("next_run_at")
                if isinstance(new_iso, str):
                    try:
                        new_nxt = _norm(datetime.fromisoformat(new_iso))
                        if cur_n < new_nxt <= tgt_n:
                            due.append((new_nxt, job))
                            due.sort(key=lambda x: x[0])
                    except ValueError:
                        pass

        # Persist updated jobs.json so subsequent ticks see new next_run_at.
        # Merge into a FRESH read instead of writing our stale snapshot: a
        # cron-fire turn can register new jobs (hermes writes jobs.json
        # itself mid-tick) and persisting the snapshot would drop them.
        if fire_count > 0:
            try:
                try:
                    fresh = json.loads(jobs_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    fresh = None
                # Graft only our scheduler bookkeeping onto the fresh jobs —
                # replacing whole objects would revert any OTHER field
                # hermes edited mid-tick (prompt, schedule, name).
                ours_by_id = {j.get("id"): j for j in jobs if j.get("id")}

                def _graft(fresh_job: dict) -> dict:
                    upd = ours_by_id.get(fresh_job.get("id"))
                    if upd is not None:
                        for k in ("last_run_at", "next_run_at", "enabled"):
                            if k in upd:
                                fresh_job[k] = upd[k]
                    return fresh_job

                if isinstance(fresh, dict) and isinstance(fresh.get("jobs"), list):
                    fresh["jobs"] = [_graft(j) for j in fresh["jobs"]]
                    fresh["updated_at"] = target_fake_now.isoformat()
                    payload = fresh
                elif isinstance(fresh, list):
                    payload = [_graft(j) for j in fresh]
                elif jobs_envelope is not None:
                    jobs_envelope["jobs"] = jobs
                    jobs_envelope["updated_at"] = target_fake_now.isoformat()
                    payload = jobs_envelope
                else:
                    payload = jobs
                jobs_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                logger.warning("Failed to persist hermes jobs.json: {}", exc)

        return target_fake_now

    def _emit_pending_suggestions(self, emit: EventEmitter, fake_now: datetime) -> None:
        """Surface hermes cron suggestions (v0.19 pull-only channel) as
        ``kind=cron_suggestion`` trajectory events. Observability only —
        nothing is accepted or delivered on the user's behalf, because
        pull-only delivery is hermes's own product semantics; the events
        let the scorecard count proposals the user never saw.
        """
        sf = self.hermes_home / "cron" / "suggestions.json"
        if not sf.exists():
            return
        try:
            records = json.loads(sf.read_text(encoding="utf-8")).get("suggestions", [])
        except (json.JSONDecodeError, OSError, AttributeError):
            return
        seen_file = self.root / "suggestions_seen.json"
        try:
            seen = set(json.loads(seen_file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            seen = set()
        new = [r for r in records if isinstance(r, dict) and r.get("id") and r["id"] not in seen]
        if not new:
            return
        for r in new:
            emit(
                {
                    "kind": "cron_suggestion",
                    "fake_now": fake_now.isoformat(),
                    "delivered": False,
                    "action": "suggest",
                    "route": "suggestion",
                    "topic_tag": f"suggestion_{str(r['id'])[:12]}",
                    "suggestion_id": r["id"],
                    "title": r.get("title") or "",
                    "source": r.get("source") or "",
                    "status": r.get("status") or "",
                    "created_at": r.get("created_at") or "",
                    "reason": "hermes suggestion registered (pull-only; not auto-delivered)",
                }
            )
            seen.add(r["id"])
        try:
            seen_file.write_text(json.dumps(sorted(seen)), encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to persist suggestions_seen.json: {}", exc)

    async def stop(self) -> None:
        pass

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def final_memory_md(self) -> str | None:
        # Hermes writes into SOUL.md / memories/ — aggregate
        parts: list[str] = []
        for path in (self.hermes_home / "SOUL.md",):
            if path.exists():
                parts.append(path.read_text(encoding="utf-8"))
        mem_dir = self.hermes_home / "memories"
        if mem_dir.exists():
            for p in sorted(mem_dir.glob("*.md")):
                parts.append(f"\n## {p.name}\n" + p.read_text(encoding="utf-8"))
        return "\n\n".join(parts) if parts else None


def _find_hermes_python(hermes_src: Path) -> str:
    """Prefer venv in hermes_src; else infer from `hermes` CLI on PATH; else sys.exec."""
    # 1. venv in source tree
    for cand in (
        hermes_src / "venv" / "bin" / "python3",
        hermes_src / "venv" / "bin" / "python",
        hermes_src / ".venv" / "bin" / "python3",
        hermes_src / ".venv" / "bin" / "python",
    ):
        if cand.exists():
            return str(cand)
    # 2. infer from `hermes` on PATH — resolve symlink + take sibling python
    import shutil as _shutil

    hermes_bin = _shutil.which("hermes")
    if hermes_bin:
        py = Path(hermes_bin).resolve().parent / "python"
        if py.exists():
            return str(py)
        py3 = Path(hermes_bin).resolve().parent / "python3"
        if py3.exists():
            return str(py3)
    # 3. fallback
    import sys

    return sys.executable


def _iso_with_tz(dt: datetime) -> str:
    if dt.tzinfo is None:
        from datetime import timezone

        return dt.replace(tzinfo=timezone.utc).isoformat()
    return dt.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# OpenClaw — docker run per turn, session persisted in mounted OPENCLAW_HOME


def _sim_time_preamble(fake_now: datetime) -> str:
    """Prepend simulated time so agents without native fake-clock (OpenClaw,
    and as a belt-and-suspenders safety for Hermes) align replies with
    the sim timeline instead of real wall-clock."""
    weekday = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][fake_now.weekday()]
    return (
        f"[sim_context]\n"
        f"This conversation is part of a simulated day. "
        f"Treat the canonical 'current time' as: {fake_now.strftime('%Y-%m-%d %H:%M')} ({weekday}). "
        f"Base any date/time reasoning on this, not on your system clock.\n\n"
    )


# Bundled MCP cron server (Node stdio). Copied into each persona's
# bind-mounted OPENCLAW_HOME at seed time so the container can spawn it
# at /home/node/.openclaw/mcp_cron_server.mjs.
_MCP_CRON_SERVER_JS = Path(__file__).resolve().parent / "mcp_cron_server.mjs"

# Container-internal mount point for OPENCLAW_HOME — matches the existing
# bind mount ``-v <host>/.openclaw:/home/node/.openclaw``.
_OC_HOME_IN_CONTAINER = "/home/node/.openclaw"


def _resolve_oc_provider(persona_id: str) -> dict:
    """Pick OpenClaw LLM provider config for one persona.

    Default (OPENCLAW_USE_OPENROUTER unset) returns {} so build_openclaw_config
    falls back to local vLLM. With OPENCLAW_USE_OPENROUTER=1, splits
    OPENROUTER_API_KEY on ',' and picks one key deterministically by hashing
    persona_id — so concurrent dockers spread across whatever account quotas
    are listed.
    """
    if os.environ.get("OPENCLAW_USE_OPENROUTER") != "1":
        return {}
    raw = os.environ.get("OPENROUTER_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise RuntimeError(
            "OPENCLAW_USE_OPENROUTER=1 requires OPENROUTER_API_KEY (one or "
            "more keys, comma-separated for multi-account rotation)"
        )
    import hashlib as _hashlib

    idx = int(_hashlib.sha256(persona_id.encode()).hexdigest()[:8], 16) % len(keys)
    return {
        "model_id": os.environ.get("OPENCLAW_OR_MODEL", "qwen/qwen3.5-27b"),
        "base_url": "https://openrouter.ai/api/v1",
        "provider_key": "openrouter",
        "api_key_override": keys[idx],
        "context_window": 262144,
        "max_tokens": 8192,
    }


class OpenClawAdapter(AgentAdapter):
    agent_name = "openclaw"

    def __init__(self, persona: dict, root: Path) -> None:
        self.persona = persona
        self.root = root
        self.oc_home = root / "oc_home"
        self._session_prefix = f"sim-{persona['id']}-{os.getpid()}"
        # longrun needs MCP server support (OC ≥ 2026.3.31). The legacy
        # ``openclaw:local`` image is 2026.2.x — no MCP. ``openclaw:local-mcp``
        # is the official ghcr.io/openclaw/openclaw image retagged locally;
        # rebuild with: ``docker tag ghcr.io/openclaw/openclaw:latest openclaw:local-mcp``
        # Override with $OPENCLAW_LONGRUN_IMAGE for ablations.
        self.docker_image = os.environ.get(
            "OPENCLAW_LONGRUN_IMAGE",
            "openclaw:local-mcp",
        )
        # MCP cron store: LLM-driven set_reminder calls land here (writer is
        # the in-container Node MCP child; host-side tick_to reads via the
        # bind mount). Replaces the prior v9-era synthetic_cron_fire path
        # (which fabricated fires from the intent calendar without the LLM
        # actually choosing to register them).
        self._cron_store = self.oc_home / ".openclaw" / "cron-store.json"
        self._pending_cron_events: list[dict] = []
        self._known_cron_ids: set[str] = {str(r["id"]) for r in self._cron_jobs_snapshot()}

    def _cron_jobs_snapshot(self) -> list[dict]:
        try:
            data = json.loads(self._cron_store.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        reminders = (data or {}).get("reminders") or []
        return [r for r in reminders if isinstance(r, dict) and r.get("id")]

    def _scan_new_crons(self, *, fake_now: datetime, trigger: str, trigger_content: str) -> None:
        for r in self._cron_jobs_snapshot():
            rid = str(r["id"])
            if rid in self._known_cron_ids:
                continue
            self._known_cron_ids.add(rid)
            self._pending_cron_events.append(
                _cron_registered_event(r, fake_now=fake_now, trigger=trigger, trigger_content=trigger_content)
            )

    def _flush_cron_events(self, emit: EventEmitter) -> None:
        for ev in self._pending_cron_events:
            emit(ev)
        self._pending_cron_events.clear()

    @classmethod
    async def build(
        cls,
        persona: dict[str, Any],
        *,
        resume_root: Path | None = None,
    ) -> "OpenClawAdapter":
        if resume_root is not None:
            root = resume_root
        else:
            root = Path(tempfile.mkdtemp(prefix=f"longrun-openclaw-{persona['id']}-"))
        inst = cls(persona, root)
        inst._seed_home()
        return inst

    def _seed_home(self) -> None:
        from .openclaw import build_openclaw_config, write_openclaw_home

        self.oc_home.mkdir(parents=True, exist_ok=True)
        ws = self.oc_home / "workspace"
        ws.mkdir(exist_ok=True)
        # Seed memory if fresh
        init_mem = (self.persona.get("initial_memory_md") or "").strip()
        mem_path = ws / "MEMORY.md"
        if init_mem and not mem_path.exists():
            mem_path.write_text(init_mem + "\n", encoding="utf-8")
        # Copy the MCP cron server script into OPENCLAW_HOME so the container
        # can spawn it via the existing bind mount (no extra -v needed).
        oc_home_inner = self.oc_home / ".openclaw"
        oc_home_inner.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_MCP_CRON_SERVER_JS, oc_home_inner / "mcp_cron_server.mjs")
        mcp_servers = {
            "longrun_cron": {
                "command": "node",
                "args": [f"{_OC_HOME_IN_CONTAINER}/mcp_cron_server.mjs"],
                "env": {
                    "LONGRUN_STORE": f"{_OC_HOME_IN_CONTAINER}/cron-store.json",
                    "LONGRUN_MCP_LOG": f"{_OC_HOME_IN_CONTAINER}/cron-mcp.log",
                },
            }
        }
        provider_overrides = _resolve_oc_provider(self.persona["id"])
        cfg = build_openclaw_config(
            workspace=f"{_OC_HOME_IN_CONTAINER}/workspace",
            bootstrap_max_chars=20000 if init_mem else 1,
            mcp_servers=mcp_servers,
            **provider_overrides,
        )
        if not (self.oc_home / ".openclaw" / "openclaw.json").exists():
            write_openclaw_home(self.oc_home, cfg)

    def _sync_mcp_sim_clock(self, fake_now: datetime) -> None:
        """Write the current sim time into the MCP cron bridge's env in
        openclaw.json. Each docker turn spawns a fresh MCP child, so the
        bridge picks this up at spawn and can validate ``when`` against the
        fake clock — without it, past-time registrations are accepted
        silently and can never fire (host window is strictly cur < when).
        """
        cfg_path = self.oc_home / ".openclaw" / "openclaw.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            env = cfg["mcp"]["servers"]["longrun_cron"].setdefault("env", {})
            env["LONGRUN_FAKE_NOW"] = fake_now.isoformat()
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Failed to sync sim clock into openclaw.json: {}", exc)

    async def start(self) -> None:
        pass

    async def send_user_message(
        self,
        content: str,
        *,
        session_key: str,
        fake_now: datetime,
    ) -> str:
        return await self._run_oc_turn(content, fake_now)

    async def _run_oc_turn(
        self,
        content: str,
        fake_now: datetime,
        *,
        timeout_s: int = 180,
    ) -> str:
        """One openclaw agent --local turn (docker) with sim_time preamble."""
        import time as _time

        from .openclaw import extract_response_text

        wrapped = _sim_time_preamble(fake_now) + content
        self._sync_mcp_sim_clock(fake_now)
        # One session per sim-day (boundary 04:00), mirroring OpenClaw's own
        # production default (config session_reset: at_hour 4, mode both).
        # Embedded `agent --local` never rotates or compacts on its own —
        # a fixed 30-day session hits "Context overflow" around day 5
        # (observed on 2026.6.34) and every later turn dies. Cross-day
        # continuity comes from its workspace MEMORY.md bootstrap, exactly
        # as in production after a session reset.
        session_id = f"{self._session_prefix}-{(fake_now - timedelta(hours=4)).date().isoformat()}"
        container_name = f"oc-lr-{self.persona['id']}-{_time.monotonic_ns()}"
        cmd = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--name",
            container_name,
            "-v",
            f"{self.oc_home}/.openclaw:{_OC_HOME_IN_CONTAINER}",
            self.docker_image,
            "node",
            "dist/index.js",
            "agent",
            "--local",
            "--session-id",
            session_id,
            "--message",
            wrapped,
            "--thinking",
            "medium",
            "--timeout",
            "90",
            "--json",
        ]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True,
                timeout=5,
            )
            self._scan_new_crons(fake_now=fake_now, trigger="user_turn", trigger_content=content)
            return "[openclaw timeout]"
        self._scan_new_crons(fake_now=fake_now, trigger="user_turn", trigger_content=content)
        text = extract_response_text(proc.stdout, proc.stderr)
        if text is None:
            dump_dir = os.environ.get("OC_NO_TEXT_DUMP")
            if dump_dir:
                from pathlib import Path as _P

                _d = _P(dump_dir)
                _d.mkdir(parents=True, exist_ok=True)
                stamp = f"{self.persona['id']}-{_time.monotonic_ns()}"
                (_d / f"{stamp}.input.txt").write_text(wrapped, encoding="utf-8")
                (_d / f"{stamp}.stdout").write_text(proc.stdout or "", encoding="utf-8")
                (_d / f"{stamp}.stderr").write_text(proc.stderr or "", encoding="utf-8")
            return "[openclaw no-text]"
        return text

    async def tick_to(
        self,
        target_fake_now: datetime,
        *,
        current_fake_now: datetime,
        emit: EventEmitter,
    ) -> datetime:
        """Poll the MCP cron store and fire any reminder whose `when` falls
        in ``(current_fake_now, target_fake_now]`` by re-entering OC with a
        synthetic '[Reminder] ...' user turn at the fire time. Emits
        ``kind=cron_fire`` events mirroring HermesAdapter.tick_to.
        """
        self._flush_cron_events(emit)
        if not self._cron_store.exists():
            return target_fake_now
        try:
            store = json.loads(self._cron_store.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return target_fake_now
        reminders = (store or {}).get("reminders") or []
        if not reminders:
            return target_fake_now

        # state.fake_now is naive (treated as UTC by everything else); strip
        # tz from reminder.when so naive/aware comparisons don't blow up.
        def _norm(dt: datetime) -> datetime:
            return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

        tgt_n = _norm(target_fake_now)

        def _next_occurrence(dt: datetime, repeat: str) -> datetime:
            if repeat == "weekly":
                return dt + timedelta(days=7)
            nxt = dt + timedelta(days=1)
            if repeat == "weekdays":
                while nxt.weekday() >= 5:
                    nxt += timedelta(days=1)
            return nxt

        # Drain loop: a recurring reminder advances `when` after each fire
        # and may legitimately fire several times inside one window (e.g. a
        # daily med reminder across a multi-day tick). Cap matches the
        # Hermes safeguard.
        fires_done = 0
        while fires_done < 200:
            due: list[tuple[datetime, dict]] = []
            for r in reminders:
                if r.get("fired"):
                    continue
                when_iso = r.get("when")
                if not isinstance(when_iso, str):
                    continue
                try:
                    when_dt = _norm(datetime.fromisoformat(when_iso))
                except ValueError:
                    continue
                # No lower bound: an unfired reminder whose `when` slipped
                # behind the window (registered mid-tick, or clock drift
                # past a +60s bump) fires late instead of never — the
                # fired flag / repeat advance prevent double fires.
                if when_dt <= tgt_n:
                    due.append((when_dt, r))
            if not due:
                break
            due.sort(key=lambda x: x[0])
            fire_time, reminder = due[0]

            repeat = (reminder.get("repeat") or "").strip().lower()
            if repeat in ("daily", "weekdays", "weekly"):
                reminder["when"] = _next_occurrence(fire_time, repeat).isoformat()
                reminder["fire_count"] = int(reminder.get("fire_count") or 0) + 1
                reminder["last_fired_at"] = fire_time.isoformat()
            else:
                reminder["fired"] = True
                reminder["fired_at"] = fire_time.isoformat()
            # Persist immediately so a mid-tick crash doesn't double-fire.
            try:
                self._cron_store.write_text(
                    json.dumps(store, indent=2),
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning("OpenClaw cron store write failed: {}", exc)

            msg = reminder.get("message", "") or ""
            synthetic = f"[Reminder fired at {fire_time.strftime('%Y-%m-%d %H:%M')}] {msg}"
            response = await self._run_oc_turn(synthetic, fire_time)

            # The container can rewrite the store during the fire turn
            # (set_reminder / cancel_reminder). It built on top of our
            # pre-turn persist, so the FILE is authoritative — reload it
            # for the next iteration instead of re-persisting this stale
            # snapshot (which would delete new registrations and resurrect
            # cancellations).
            try:
                fresh = json.loads(self._cron_store.read_text(encoding="utf-8"))
                if isinstance(fresh, dict):
                    store = fresh
                    reminders = (store or {}).get("reminders") or []
            except (OSError, json.JSONDecodeError):
                pass
            # Reminders the agent registered while ANSWERING a reminder are
            # its own initiative — mirror HermesAdapter's cron_fire scan.
            self._scan_new_crons(fake_now=fire_time, trigger="cron_fire", trigger_content=msg)
            self._flush_cron_events(emit)

            emit(
                {
                    "kind": "cron_fire",
                    "fake_now": fire_time.isoformat(),
                    "delivered": True,
                    "action": "nudge",
                    "route": "cron",
                    "nudge_message": response,
                    "topic_tag": f"cron_{reminder.get('id', 'unknown')[:16]}",
                    "cron_id": reminder.get("id", "unknown"),
                    "cron_name": (msg[:60] or "reminder"),
                    "priority": "medium",
                    "target_session": "default",
                    "reason": f"reminder fired: {msg[:80]}",
                }
            )
            fires_done += 1

        return target_fake_now

    async def stop(self) -> None:
        pass

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def final_memory_md(self) -> str | None:
        mem_path = self.oc_home / "workspace" / "MEMORY.md"
        return mem_path.read_text(encoding="utf-8") if mem_path.exists() else None


# ─────────────────────────────────────────────────────────────────────────────
# Factory


async def build_adapter(
    system: str,
    persona: dict[str, Any],
    *,
    resume_root: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> AgentAdapter:
    system = (system or "raven").lower()
    if system == "raven":
        return await RavenAdapter.build(
            persona,
            resume_root=resume_root,
            overrides=overrides,
        )
    if system == "hermes":
        return await HermesAdapter.build(persona, resume_root=resume_root)
    if system == "openclaw":
        return await OpenClawAdapter.build(persona, resume_root=resume_root)
    raise ValueError(f"unknown agent system: {system}")


__all__ = ["AgentAdapter", "RavenAdapter", "HermesAdapter", "OpenClawAdapter", "build_adapter"]
