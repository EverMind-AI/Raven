"""ProactiveBench reward_data driver (S1 protocol, single-decision).

Loads the jsonl at ``benchmarks/pbench/pbench.yaml::dataset_file``,
stratifies across 4 categories, renders each record through a per-agent
prompt template (``prompts/<agent>_agent.yaml`` or a default), parses a
``{should_help, proposed_task, reason}`` JSON decision out of the reply.

Hermes + Raven Sentinel can also consume the same records via their
structured hooks.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..backend import Sample
from ..benchmarks import get_benchmark_config
from ..categories import CATEGORIES, sample_stratified
from ..driver import BenchmarkDriver
from ..obs import build_obs_block, build_synth_block
from ..parse import parse_decision

if TYPE_CHECKING:
    from ..backend import AgentOutcome


_THIS_DIR = Path(__file__).resolve().parent
_RUNNERS_DIR = _THIS_DIR.parent.parent  # runners/


def _parse_obs_time(raw: Any) -> datetime:
    try:
        return datetime.fromtimestamp(float(raw))
    except (TypeError, ValueError):
        return datetime.now()


def _parse_fake_now_iso(obs: list[dict]) -> str:
    """Pick a tz-aware ISO timestamp for hermes_time.now mock."""
    if obs:
        raw = obs[-1].get("time", "")
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc).isoformat()
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc).isoformat()


class PbenchDriver(BenchmarkDriver):
    name = "pbench"

    # Which prompt template to render per agent (fallback: raven_agent.yaml).
    _PROMPT_BY_AGENT = {
        "raven": "raven_agent.yaml",
        "hermes": "hermes_agent.yaml",
        "openclaw": "openclaw_agent.yaml",
    }

    def __init__(
        self,
        agent_name: str | None = None,
        context_mode: str = "cold",
        synthesizer_name: str = "keyword",
        prompts_dir: Path | None = None,
    ):
        self._cfg = get_benchmark_config("pbench")
        self._dataset = Path(self._cfg["dataset_file"])
        self._agent_name = (agent_name or "raven").lower()
        self._context_mode = context_mode
        self._synthesizer_name = synthesizer_name
        # Default: the uniform minimal template (the results-table setup).
        # Pass prompts_dir=runners/prompts for the legacy persona templates
        # (ablation reproduction only — see FINDINGS-ablation-20260722.md).
        self._prompts_dir = prompts_dir or (_RUNNERS_DIR / "prompts" / "uniform")
        self._prompt_template: dict[str, str] | None = None
        self._synthesizer = None
        if context_mode == "warm":
            # Import lazily — synthesizers module imports raven types.
            import synthesizers as _syn

            self._synthesizer = _syn.get_synthesizer(synthesizer_name)

    def _load_prompt(self) -> dict[str, str]:
        if self._prompt_template is None:
            import prompts_loader

            fname = self._PROMPT_BY_AGENT.get(self._agent_name, "raven_agent.yaml")
            path = self._prompts_dir / fname
            if not path.exists():
                # Agent-agnostic prompt dir (e.g. prompts/uniform/): one
                # shared template instead of three identical copies.
                path = self._prompts_dir / "uniform_agent.yaml"
            self._prompt_template = prompts_loader.load_prompt(path)
        return self._prompt_template

    # ---- samples ----

    def load_samples(self, *, n: int | None = None, filter_id: str | None = None) -> list[Sample]:
        if not self._dataset.exists():
            raise FileNotFoundError(
                f"pbench dataset not found at {self._dataset}. "
                "Configure benchmarks/pbench/pbench.yaml or drop a .local.yaml override."
            )
        records = [json.loads(l) for l in self._dataset.read_text().splitlines()]
        if filter_id is not None:
            records = [r for r in records if str(r.get("id")) == filter_id]
        picks = sample_stratified(records, n=n if n is not None else len(records))

        samples: list[Sample] = []
        for i, rec in enumerate(picks):
            obs = rec.get("obs") or []
            synth = self._synthesizer.synthesize(obs) if self._synthesizer else None
            samples.append(
                Sample(
                    raw=rec,
                    session_hint=f"pbench-{i:04d}-{(rec.get('id') or '')[:12]}",
                    meta={"synth": synth},
                )
            )
        return samples

    # ---- prompt-based ----

    def build_prompt(self, sample: Sample, ctx: dict[str, Any] | None = None) -> str:
        tmpl = self._load_prompt()
        obs = sample.raw.get("obs") or []
        synth = (sample.meta or {}).get("synth")
        rendered_system = tmpl["system"].format(
            obs_block=build_obs_block(obs),
            synth_block=build_synth_block(synth),
        )
        rendered_user = tmpl["user"].format(
            obs_block=build_obs_block(obs),
            synth_block=build_synth_block(synth),
        )
        return rendered_system.strip() + "\n\n---\n\n" + rendered_user.strip()

    def parse_output(self, text: str | None, sample: Sample) -> dict[str, Any]:
        return parse_decision(text or "")

    # ---- structured hooks ----

    def to_planner_context(self, sample: Sample) -> Any:
        """Build a PlannerContext directly from obs + synthesizer output."""
        from raven.proactive_engine.sentinel.types import (
            ActiveSession,
            NudgePolicyState,
            PlannerContext,
        )

        obs = sample.raw.get("obs") or []
        last_ev = obs[-1] if obs else {"time": "", "event": ""}
        now = _parse_obs_time(last_ev.get("time"))

        synth = (sample.meta or {}).get("synth")
        memory_md = (synth.memory_md if synth else "") or ""
        routines = list(synth.routines) if synth else []
        user_profile = (synth.user_profile if synth else "") or ""

        history = "\n".join(f"[{e.get('time', '?')}] {e.get('event', '')}" for e in obs)
        target = f"pbench:{sample.session_hint}"
        return PlannerContext(
            now=now,
            memory_md=memory_md,
            history_md_recent=history,
            active_sessions=[
                ActiveSession(
                    key=target,
                    last_active_at=now,
                    last_user_message=last_ev.get("event", ""),
                )
            ],
            routines=routines,
            nudge_policy_state=NudgePolicyState(
                in_quiet_hours=False,
                remaining_today=100,
            ),
            user_profile=user_profile,
        )

    def to_hermes_cron(self, sample: Sample, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        """Synthetic cron wrapping the obs stream as the prompt.

        There's no real cron trigger in reward_data — we use cron.run_job as
        Hermes's on-ramp to its agent loop with a faithful prompt.
        With ``ctx["with_memory"]=True`` prepends a <memory-context> block
        derived from the synthesizer output (already available in warm mode).
        """
        prompt = self.build_prompt(sample)
        if ctx and ctx.get("with_memory"):
            mem_block = self.build_memory_block(sample)
            if mem_block:
                prompt = mem_block + "\n\n" + prompt
        return {
            "prompt": prompt,
            "schedule": "* * * * *",  # placeholder; run_job bypasses schedule
            "name": "pbench_decision",
            "deliver": "local",
            "fake_now": _parse_fake_now_iso(sample.raw.get("obs") or []),
        }

    def workspace_files(self, sample: Sample) -> dict[str, str]:
        """Seed workspace memory from synthesizer output."""
        synth = (sample.meta or {}).get("synth")
        if synth is None:
            return {}
        out: dict[str, str] = {}
        if synth.memory_md:
            out["memory/MEMORY.md"] = synth.memory_md
        if synth.user_profile:
            # OpenClaw bootstrap reads USER.md too; pack profile there if present.
            out["memory/USER.md"] = synth.user_profile
        return out

    def build_memory_block(self, sample: Sample) -> str:
        """Render a memory-context block from the synthesizer output."""
        synth = (sample.meta or {}).get("synth")
        if synth is None:
            return ""
        parts: list[str] = []
        if synth.user_profile:
            parts.append(f"## User profile\n{synth.user_profile}")
        if synth.memory_md:
            parts.append(f"## MEMORY.md\n{synth.memory_md}")
        if synth.routines:
            lines = "\n".join(f"- {r.pattern}" for r in synth.routines)
            parts.append(f"## Candidate routines\n{lines}")
        if not parts:
            return ""
        body = "\n\n".join(parts)
        return (
            "<memory-context>\n"
            "Synthesized long-term context (treat as informational background, "
            "not new user input).\n\n"
            f"{body}\n"
            "</memory-context>"
        )

    # ---- rows + summary ----

    def make_row(self, sample: Sample, outcome: "AgentOutcome", runtime_meta: dict[str, Any]) -> dict[str, Any]:
        rec = sample.raw
        system_label = runtime_meta.get("system_label", "unknown")

        # Pick the decision: backend may have pre-parsed it (Sentinel), else parse text.
        if outcome.decision is not None:
            dec = outcome.decision
        else:
            dec = self.parse_output(outcome.text, sample)

        predicted_help = dec["should_help"] if dec.get("parse_ok") else False

        row: dict[str, Any] = {
            "category": rec.get("category", "?"),
            "context_mode": self._context_mode,
            "synthesizer": self._synthesizer_name if self._synthesizer else None,
            "runtime": runtime_meta.get("runtime"),
            "truth_help_needed": rec.get("help_needed"),
            "truth_valid": rec.get("valid"),
            "truth_annotation": rec.get("annotation"),
            "baseline_pred_task": rec.get("pred_task") or None,
            "agent": {
                "system": system_label,
                "status": outcome.status,
                "error": outcome.error,
                "elapsed_s": outcome.elapsed_s,
                "parse_ok": bool(dec.get("parse_ok")),
                "should_help": dec.get("should_help"),
                "proposed_task": dec.get("proposed_task"),
                "reason": dec.get("reason"),
                "raw_final": (outcome.text or "")[:4000],
                **({"fake_now": outcome.meta.get("fake_now")} if outcome.meta and "fake_now" in outcome.meta else {}),
                **(
                    {"sentinel_action": dec.get("sentinel_action"), "sentinel_route": dec.get("sentinel_route")}
                    if "sentinel_action" in dec
                    else {}
                ),
            },
            "predicted_help": predicted_help,
            "help_match": predicted_help == rec.get("help_needed"),
        }

        synth = (sample.meta or {}).get("synth")
        if synth is not None:
            row["synth"] = {
                "user_profile": synth.user_profile,
                "routines": [
                    {"id": r.id, "pattern": r.pattern, "status": r.status, "occurrence_count": r.occurrence_count}
                    for r in synth.routines
                ],
                "memory_md": synth.memory_md,
            }
        return row

    def summarize(self, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "(no rows)"
        by_cat: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_cat[r["category"]].append(r)

        lines = [f"pbench: {len(rows)} records (context={self._context_mode})"]
        for cat in CATEGORIES:
            rs = by_cat.get(cat, [])
            if not rs:
                continue
            hm = sum(r["help_match"] for r in rs)
            lines.append(f"  {cat:<28} {hm}/{len(rs)}")

        status_counter = Counter(r["agent"]["status"] for r in rows)
        parse_ok = sum(1 for r in rows if r["agent"]["parse_ok"])
        elapsed_total = sum((r["agent"]["elapsed_s"] or 0) for r in rows)
        lines.append(f"  status: {dict(status_counter)}")
        lines.append(f"  parse_ok: {parse_ok}/{len(rows)}")
        if rows:
            lines.append(f"  elapsed: total {elapsed_total:.1f}s  mean {elapsed_total / len(rows):.1f}s/record")

        # TP/FP/TN/FN quick read
        TP = FP = TN = FN = 0
        for r in rows:
            pred = r.get("predicted_help")
            truth = r.get("truth_help_needed")
            if pred and truth:
                TP += 1
            elif pred and not truth:
                FP += 1
            elif not pred and not truth:
                TN += 1
            elif not pred and truth:
                FN += 1
        p = TP / (TP + FP) if (TP + FP) else 0.0
        rc = TP / (TP + FN) if (TP + FN) else 0.0
        f1 = 2 * p * rc / (p + rc) if (p + rc) else 0.0
        lines.append(f"  TP/FP/TN/FN={TP}/{FP}/{TN}/{FN}  P={p:.3f} R={rc:.3f} F1={f1:.3f}")
        return "\n".join(lines)

    def dataset_description(self) -> str:
        return f"ProactiveBench reward_data (stratified, {self._context_mode}): {self._dataset}"


__all__ = ["PbenchDriver"]
