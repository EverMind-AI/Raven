"""Longrun driver: LLM-simulator × 30-day proactivity benchmark.

Pairs an LLM-driven ``UserSimulator`` (default: claude-sonnet-4.5 on
OpenRouter; see ``_build_simulator_provider``) with a real Raven stack
(Agent + Sentinel + Cron) writing into a
per-persona isolated workspace. Fast-forwards fake_now through 30 days,
firing Sentinel ticks at each 30-min boundary in between simulator
actions, logging every event to ``trajectory.jsonl``.

Commit-1 scope: core loop + trajectory logging (no checkpoint, no
scorecard). ``--day-limit`` caps for smoke tests.
"""

from __future__ import annotations

import json
import os
import sys
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from ..backend import Sample
from ..driver import BenchmarkDriver
from ..longrun_adapters import AgentAdapter, build_adapter
from ..user_simulator import SimAction, SimContext, UserSimulator


_DATA_DIR_ENV = "LONGRUN_DATA_DIR"
_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "longrun"


def _data_dir() -> Path:
    override = os.environ.get(_DATA_DIR_ENV)
    return Path(override) if override else _DEFAULT_DATA_DIR


# How long of HISTORY.md tail to feed simulator (bytes).
_MEMORY_TAIL_BYTES = 1500
# Sentinel tick interval inside the sim. Matches production default.
_SENTINEL_TICK_SECONDS = 1800
# Per-action cap — simulator must make progress each step.
_MAX_IDLE_MINUTES = 540
# Hard cap on actions per scenario — safety net vs. runaway LLM loops.
_MAX_ACTIONS_PER_DAY = 80


@dataclass
class _ScenarioState:
    persona: dict[str, Any]
    day_index: int = 0
    fake_now: datetime = field(default=None)  # set at run start
    pending_nudges: list[dict[str, Any]] = field(default_factory=list)
    last_action_kind: str | None = None
    last_sentinel_tick: datetime | None = None
    action_count_today: int = 0
    trajectory_path: Path | None = None
    # Track when we last injected a synthetic ``/new`` to force memory
    # consolidation. Production users naturally start new sessions
    # (close IM, switch device, type /new) which flushes the session
    # into HISTORY.md; longrun's single 30-day session never rolls over
    # naturally, so HISTORY stays empty and IntentExtractor has nothing
    # to extract from. We inject /new each Monday 07:00 to mirror the
    # "Monday-morning fresh chat" pattern.
    last_new_session_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "persona_id": self.persona.get("id"),
            "day_index": self.day_index,
            "fake_now": self.fake_now.isoformat() if self.fake_now else None,
            "pending_nudges": list(self.pending_nudges),
            "last_action_kind": self.last_action_kind,
            "last_sentinel_tick": self.last_sentinel_tick.isoformat() if self.last_sentinel_tick else None,
            "action_count_today": self.action_count_today,
            "last_new_session_at": (
                self.last_new_session_at.isoformat()
                if self.last_new_session_at else None
            ),
        }

    def apply_dict(self, d: dict[str, Any]) -> None:
        self.day_index = int(d.get("day_index", 0))
        fn = d.get("fake_now")
        if fn:
            self.fake_now = datetime.fromisoformat(fn)
        self.pending_nudges = list(d.get("pending_nudges") or [])
        self.last_action_kind = d.get("last_action_kind")
        lst = d.get("last_sentinel_tick")
        if lst:
            self.last_sentinel_tick = datetime.fromisoformat(lst)
        self.action_count_today = int(d.get("action_count_today", 0))
        lns = d.get("last_new_session_at")
        if lns:
            self.last_new_session_at = datetime.fromisoformat(lns)


class LongRunDriver(BenchmarkDriver):
    name = "longrun"

    def __init__(self) -> None:
        self.total_days = 30

    # ---- BenchmarkDriver required -----------------------------------------

    def load_samples(self, *, n: int | None = None,
                     filter_id: str | None = None) -> list[Sample]:
        data_dir = _data_dir()
        if not data_dir.exists():
            raise FileNotFoundError(
                f"longrun data dir not found: {data_dir} "
                f"(set {_DATA_DIR_ENV} to override)"
            )
        samples: list[Sample] = []
        for p in sorted(data_dir.glob("persona-*.yaml")):
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data.get("id"):
                continue
            if filter_id and data["id"] != filter_id:
                continue
            samples.append(Sample(
                meta={"file": str(p)},
                raw=data,
                session_hint=f"sim:{data['id']}",
            ))
        if n is not None:
            samples = samples[:n]
        return samples

    def build_prompt(self, sample: Sample,
                     ctx: dict[str, Any] | None = None) -> str:
        # Not used — longrun is run via run_scenario, not the normal
        # backend.run_one path.
        return ""

    def parse_output(self, text: str | None, sample: Sample) -> dict[str, Any]:
        return {}

    def make_row(self, sample: Sample, outcome, runtime_meta) -> dict[str, Any]:
        # run_scenario produces rows directly; this is a placeholder for
        # protocol conformance.
        raise NotImplementedError("longrun rows are produced by run_scenario.")

    def dataset_description(self) -> str:
        n_files = len(list(_data_dir().glob("persona-*.yaml"))) if _data_dir().exists() else 0
        return (
            f"longrun benchmark — {n_files} persona × 30-day LLM-simulator "
            f"trajectories ({_data_dir()})"
        )

    # ---- run.py multi-tick entry -------------------------------------------

    async def run_scenario(
        self, sample: Sample, system: str, mode: str | None,
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        # All agents go through the generic AgentAdapter path. The old
        # Raven "direct path" was an in-process IsolatedWorkspace
        # wrapper that imported raven at module load; Phase 4 of the
        # migration replaced it with RavenAdapter (subprocess via
        # proactivity_eval.RavenDriver), so Raven can now share
        # the same loop as Hermes / OpenClaw.
        return await self._run_scenario_adapter(sample, system, mode, overrides)

    # ---- adapter-based scenario (hermes / openclaw) ----------------------

    async def _run_scenario_adapter(
        self, sample: Sample, system: str, mode: str | None,
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        """Run a longrun scenario via the generic AgentAdapter path.

        Used for agents without in-process Raven integration (Hermes,
        OpenClaw). Simpler loop — no Sentinel ticks during advance (those
        agents don't have an LLM-driven proactivity tick). Relies on
        intent calendar; errors out if fixture missing.
        """
        persona = sample.raw
        day_limit = int(overrides.get("day_limit") or self.total_days)
        day_limit = max(1, min(self.total_days, day_limit))
        output_dir_raw = overrides.get("output_dir")
        output_dir = Path(output_dir_raw) if output_dir_raw else _default_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        ckpt_dir = output_dir / f"ckpt-{persona['id']}-{system}"
        ckpt_dir.mkdir(exist_ok=True)

        intents = _load_intent_calendar(persona["id"])
        if intents is None:
            raise RuntimeError(
                f"adapter path requires intent calendar; "
                f"generate with generate_longrun_fixtures.py --persona {persona['id']} --kind intents"
            )

        sim_provider, sim_model = _build_simulator_provider(overrides)
        sim = UserSimulator(persona, sim_provider, sim_model)

        adapter = await build_adapter(system, persona, overrides=overrides)
        trajectory_path = output_dir / f"longrun-{persona['id']}-{system}-trajectory.jsonl"
        trajectory_path.write_text("", encoding="utf-8")

        state = _ScenarioState(
            persona=persona,
            fake_now=_day_start_datetime(persona, 0),
            trajectory_path=trajectory_path,
        )
        totals = {"actions": 0, "nudges": 0}

        def _emit(ev: dict) -> None:
            """Adapter-emitted events → trajectory."""
            self._log_event(state, ev.get("kind", "agent_initiated"), ev)
            if ev.get("delivered"):
                totals["nudges"] += 1

        last_day_end = _day_start_datetime(persona, day_limit)
        pending = [i for i in intents
                   if _parse_intent_time(i) >= state.fake_now
                   and _parse_intent_time(i) < last_day_end]
        logger.info("adapter[{}] persona={} {} intents to run",
                    system, persona['id'], len(pending))

        try:
            await adapter.start()

            for intent in pending:
                intent_time = _parse_intent_time(intent)
                if intent_time > state.fake_now:
                    state.fake_now = await adapter.tick_to(
                        intent_time,
                        current_fake_now=state.fake_now,
                        emit=_emit,
                    )
                    await self._maybe_roll_day(state, None)

                # React to any nudges that arrived during idle
                await self._react_to_pending_nudges(state, sim, adapter=adapter)

                # Materialize first user message via LLM simulator
                sim_ctx = self._build_adapter_sim_context(state, adapter)
                first_msg = await sim.materialize_intent(intent, sim_ctx)
                self._log_event(state, "user_send", {
                    "intent_id": intent.get("topic", "")[:50],
                    "intent_kind": intent.get("kind", ""),
                    "content": first_msg,
                    "turn": "first",
                })
                session_key = f"sim:{persona['id']}:main"
                reply = await adapter.send_user_message(
                    first_msg, session_key=session_key, fake_now=state.fake_now,
                )
                self._log_event(state, "agent_reply", {"content": reply, "turn": "first"})
                totals["actions"] += 1
                state.fake_now += timedelta(seconds=30)

                max_fu = intent.get("expected_followups", 0)
                for i in range(max(max_fu + 1, 5)):
                    followup_ctx = self._build_adapter_sim_context(state, adapter)
                    decision, content, reasoning = await sim.decide_followup(
                        intent, followup_ctx, followups_taken=i,
                    )
                    self._log_event(state, "sim_action", {
                        "action_kind": "followup",
                        "decision": decision,
                        "content": content,
                        "reasoning": reasoning,
                    })
                    if decision != "send" or not content:
                        break
                    reply = await adapter.send_user_message(
                        content, session_key=session_key, fake_now=state.fake_now,
                    )
                    self._log_event(state, "agent_reply", {
                        "content": reply, "turn": f"followup-{i+1}",
                    })
                    totals["actions"] += 1
                    state.fake_now += timedelta(seconds=30)

                prev_day = state.day_index
                await self._maybe_roll_day(state, None)
                # Checkpoint adapter path: just dump state.json (workspace tar
                # is up to the adapter type — skipped for now)
                if state.day_index > prev_day:
                    (ckpt_dir / f"day{prev_day:02d}.json").write_text(
                        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )

                if state.day_index >= day_limit:
                    break

            # Final tick to day_limit end
            end_fake = _day_start_datetime(persona, day_limit)
            if state.fake_now < end_fake:
                state.fake_now = await adapter.tick_to(
                    end_fake, current_fake_now=state.fake_now, emit=_emit,
                )
                prev_day = state.day_index
                await self._maybe_roll_day(state, None)
                if state.day_index > prev_day:
                    (ckpt_dir / f"day{prev_day:02d}.json").write_text(
                        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
        finally:
            # Persist final memory snapshot for scorecard access
            final_mem = adapter.final_memory_md()
            if final_mem:
                (ckpt_dir / "final_memory.md").write_text(final_mem, encoding="utf-8")
            await adapter.stop()
            adapter.cleanup()

        return {
            "persona_id": persona["id"],
            "system": system,
            "mode": mode,
            "days_ran": state.day_index,
            "total_actions": totals["actions"],
            "total_nudges": totals["nudges"],
            "trajectory_path": str(trajectory_path),
            "checkpoint_dir": str(ckpt_dir),
            "calendar_driven": True,
            "passed": True,
        }

    async def _react_to_pending_nudges(
        self, state: _ScenarioState, sim: UserSimulator,
        *, adapter: AgentAdapter,
    ) -> None:
        """If nudges arrived during the latest idle window, give simulator a
        chance to engage / dismiss / ignore. Fires before the next intent's
        first message."""
        if not state.pending_nudges:
            return
        # Snapshot before resetting (sim consumes regardless of choice)
        nudges = list(state.pending_nudges)
        state.pending_nudges.clear()

        sim_ctx = self._build_adapter_sim_context(state, adapter)
        sim_ctx.pending_nudges = nudges  # ensure simulator sees them

        try:
            reaction, content, reasoning = await sim.react_to_nudges(sim_ctx)
        except Exception as exc:
            logger.warning("react_to_nudges raised: {}", exc)
            return

        self._log_event(state, "sim_action", {
            "action_kind": "react_to_nudge",
            "reaction": reaction,
            "content": content,
            "reasoning": reasoning,
            "n_nudges": len(nudges),
        })

        if reaction == "ignore":
            return  # no message to agent
        if reaction == "engage" and not content:
            return  # engage without content is a no-op

        # Dismiss without content still needs to register — send a bare
        # /dismiss so NudgePolicy.record_dismissed fires.
        if reaction == "dismiss":
            if not content:
                content = "/dismiss " + (reasoning[:80] if reasoning else "知道了别催")
            elif not content.startswith("/dismiss"):
                content = "/dismiss " + content
        reply = await adapter.send_user_message(
            content,
            session_key=f"sim:{state.persona['id']}:main",
            fake_now=state.fake_now,
        )
        self._log_event(state, "agent_reply", {
            "content": reply, "turn": f"react:{reaction}",
        })
        state.fake_now += timedelta(seconds=30)

    def _build_adapter_sim_context(
        self, state: _ScenarioState, adapter: AgentAdapter,
    ) -> SimContext:
        """SimContext for adapter path — memory comes from adapter, sessions are
        reconstructed by re-reading recent trajectory turns."""
        # Recent turns from trajectory tail
        recent_turns: list[dict[str, str]] = []
        if state.trajectory_path and state.trajectory_path.exists():
            with state.trajectory_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()[-40:]
            for line in lines:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                k = e.get("kind")
                if k == "user_send" and e.get("content"):
                    recent_turns.append({"role": "user", "content": str(e["content"])[:400]})
                elif k == "agent_reply" and e.get("content"):
                    recent_turns.append({"role": "assistant", "content": str(e["content"])[:400]})
            recent_turns = recent_turns[-16:]

        mem = adapter.final_memory_md() or ""
        return SimContext(
            fake_now=state.fake_now,
            persona=state.persona,
            recent_turns=recent_turns,
            memory_tail=mem[-_MEMORY_TAIL_BYTES:],
            pending_nudges=list(state.pending_nudges),
            last_action_kind=state.last_action_kind,
            day_index=state.day_index,
        )

    async def _maybe_roll_day(self, state: _ScenarioState, _ws_legacy: Any = None) -> None:
        """Detect if fake_now crossed midnight → increment day_index.

        The unused ``_ws_legacy`` positional is kept so existing callsites
        (``self._maybe_roll_day(state, None)``) compile unchanged; remove
        it in a follow-up pass that also drops the trailing ``None``."""
        current_day_start = _day_start_datetime(state.persona, state.day_index)
        next_day_start = _day_start_datetime(state.persona, state.day_index + 1)
        if state.fake_now >= next_day_start:
            state.day_index += 1
            state.action_count_today = 0
            # Clear pending nudges (new day, stale nudges expire naturally)
            state.pending_nudges.clear()
            self._log_event(state, "day_rollover", {"day_index": state.day_index})

    # ---- trajectory logging ------------------------------------------------

    def _log_event(self, state: _ScenarioState, kind: str, data: dict[str, Any]) -> None:
        entry = {
            "ts_wall": datetime.now().isoformat(),
            "fake_now": state.fake_now.isoformat(),
            "day": state.day_index,
            "kind": kind,
            **data,
        }
        with state.trajectory_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# -------------------------------------------------------------------------
# Helpers


def _default_output_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "output" / "longrun"


def _load_intent_calendar(persona_id: str) -> list[dict[str, Any]] | None:
    """Return chronologically-sorted intents list, or None if fixture missing."""
    path = _data_dir() / f"persona-{persona_id}-intents.yaml"
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    events = data.get("events") or []
    return sorted(events, key=_parse_intent_time)


def _parse_intent_time(intent: dict[str, Any]) -> datetime:
    at = intent.get("at")
    if isinstance(at, datetime):
        return at
    return datetime.fromisoformat(str(at))


def _day_start_datetime(persona: dict, day_index: int) -> datetime:
    """Return persona-specific day-start on day N (counting from sim day 0).

    Day 0 is a persona-defined anchor date (default 2026-05-01). wake_hours[0]
    is the hour they get up.
    """
    anchor = persona.get("anchor_date", "2026-05-01")
    base = datetime.fromisoformat(anchor)
    wake_hour = (persona.get("wake_hours") or [7, 23])[0]
    day = base + timedelta(days=day_index)
    return day.replace(hour=wake_hour, minute=0, second=0, microsecond=0)


def _build_simulator_provider(overrides: dict[str, Any]):
    """Simulator LLM. Default = openrouter/anthropic/claude-sonnet-4.5

    Sonnet is significantly more nuanced for persona simulation +
    react-to-nudge dismissal decisions; deepseek-chat tends to be too
    polite to push back. Override via --simulator-model.

    Cost: Sonnet 4.5 ≈ $3 in / $15 out per 1M; deepseek-chat ≈ $0.14 / $0.28.
    For dev-01 30-day full run: Sonnet ~$3-5, deepseek ~$0.30.
    """
    from ..provider import make_provider
    model = overrides.get("simulator_model") or "openrouter/anthropic/claude-sonnet-4.5"
    provider, model_resolved = make_provider({}, model_override=model)
    return provider, model_resolved


def _build_planner_provider(model: str):
    """Sentinel ProactivePlanner LLM override. Independent from agent provider.

    Currently routes through the same `make_provider` factory the simulator
    uses (auto-detects OpenRouter ids by prefix). Returns (provider, model).
    """
    from ..provider import make_provider
    provider, model_resolved = make_provider({}, model_override=model)
    return provider, model_resolved


def _build_agent_provider(overrides: dict[str, Any]):
    """Agent + Sentinel = local qwen (from ~/.hermes/config.yaml)."""
    from ..hermes_home import load_config_from_hermes_home, load_env_from_hermes_home
    from ..provider import make_provider
    load_env_from_hermes_home()
    cfg = load_config_from_hermes_home()
    provider, _model = make_provider(cfg)
    return provider


def _write_checkpoint(
    ws_root: Path, state: _ScenarioState, ckpt_dir: Path, *, day_completed: int,
) -> Path:
    """Tar the workspace + scenario state into ``ckpt_dir/dayNN.tar``.

    Content layout inside the tar:
      workspace/       (memory/, sessions/, skills/)
      sentinel/        (state.json — cross-process Sentinel state)
      cron/            (jobs.json + .lock)
      state.json       (_ScenarioState.to_dict())

    Resume reads state.json → picks up at day_completed + 1.
    """
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = ckpt_dir / f"day{day_completed:02d}.tar"
    # Dump state.json alongside the workspace snapshot
    state_path = ws_root / "state.json"
    state_path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp = ckpt_path.with_suffix(ckpt_path.suffix + ".tmp")
    with tarfile.open(tmp, "w") as tar:
        # Include the 4 dirs + the state file. Use arcnames so the tar
        # is relocatable (not tied to the tmp dir absolute path).
        for entry in ("workspace", "sentinel", "cron"):
            sub = ws_root / entry
            if sub.exists():
                tar.add(sub, arcname=entry)
        tar.add(state_path, arcname="state.json")
    os.replace(tmp, ckpt_path)
    try:
        state_path.unlink()  # don't leave state.json inside live workspace
    except OSError:
        pass
    logger.info("checkpoint written: {} (day={})", ckpt_path, day_completed)
    return ckpt_path


def _untar_checkpoint(ckpt_path: Path, dest_root: Path) -> None:
    """Expand a checkpoint tar into ``dest_root`` (must exist, typically fresh tmp)."""
    dest_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(ckpt_path, "r") as tar:
        tar.extractall(dest_root)


__all__ = ["LongRunDriver"]
