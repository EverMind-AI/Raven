"""Ops tools: launch and inspect a long-running tuning campaign from chat.

``ops_tune_launch`` fires an adaptive tuning campaign as a detached subprocess
(``raven ops tune --adaptive``) that survives this agent process -- the whole
point of Ops is durable, resume-across-crash orchestration -- and returns at
once with a handle. ``ops_tune_status`` reads that campaign's ledger (the on-disk
source of truth) and its log tail to report progress and the best config so far.
The LLM that proposes configs is Raven's own configured endpoint, passed in at
construction; scoring stays deterministic in the container, so the reward the
campaign optimizes stays verifiable.
"""

from __future__ import annotations

import dataclasses as _dataclasses
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.agent.tools.base import Tool
from raven.ops.budget import from_meta as budget_from_meta

if TYPE_CHECKING:
    from raven.proactive_engine.schedulers.cron.service import CronService

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _ops_home() -> Path:
    """The ops root for this instance, resolved per call rather than bound at
    import so that it follows ``--config``. See ``config.paths.get_ops_home``."""
    from raven.config.paths import get_ops_home

    return get_ops_home()


def _slug(text: str, limit: int = 24) -> str:
    from raven.ops.instrument import campaign_slug

    return campaign_slug(text, limit)


def _campaign_dir(host: str, objective: str) -> Path:
    return _ops_home() / f"{host.replace('.', '-')}_{_slug(objective)}"


def _resolve_campaign_dir(campaign: str | None, ledger: str | None = None) -> Path:
    """The campaign's state directory.

    An explicit ledger path wins, then an explicit name. With neither, fall back to
    the one campaign under the ops home -- because a campaign name and its ledger
    path are an API handle ("state lives over there") that had to be typed into
    every task statement, and nobody asking for the work has a reason to know it.

    Exactly one, or an error naming the candidates. Silently picking one of several
    would be the worst of the three outcomes: the wrong campaign reads exactly like
    the right one, and the loop would drive somebody else's experiment.
    """
    if ledger and str(ledger).strip():
        return Path(ledger).expanduser().parent
    if campaign:
        return _ops_home() / _slug(campaign)
    home = _ops_home()
    found = sorted(d.name for d in home.iterdir() if d.is_dir()) if home.exists() else []
    if len(found) == 1:
        return home / found[0]
    if not found:
        raise ValueError(f"no campaign under {home}; name one or create it first")
    raise ValueError(f"{len(found)} campaigns under {home} ({', '.join(found)}); say which one")


def _read_notes(campaign_dir: Path) -> list[dict]:
    """Read the campaign's user-instruction notes (oldest first)."""
    import json

    path = campaign_dir / "notes.jsonl"
    if not path.exists():
        return []
    notes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            notes.append(json.loads(line))
        except ValueError:
            continue
    return notes


_HISTORY_LIMIT = 12

# A failed job's own last words, capped. Long enough for a stack frame or an
# OpenFOAM FATAL line, short enough that the campaign's state stays readable.
_FAILURE_REASON_CHARS = 400


# Keys this layer writes itself. They are readings about the run, not the score
# the run is being judged by, so they can never be the answer to "which metric".
_INFRASTRUCTURE_METRICS = frozenset({"gpu_minutes_used"})


def _campaign_metric(meta_path: Path, asked: str, records: "list | None" = None) -> str:
    """The metric name to read back: asked for, else declared, else observed.

    This tool layer is shared with every domain that runs jobs, so it must not
    know any metric by name. It used to fall back to "ndcg", which is how a CFD
    campaign came to read back "trials succeeded but none reported metric 'ndcg'"
    for runs that reported theirs perfectly well.

    The third step replaces that fallback. Campaigns predating ``objective`` (r13
    and r14) are still on disk, and rewriting their meta to suit today's code
    would edit the record of what the device was while they ran. What the records
    themselves report is a fact rather than a guess: exactly one candidate is the
    answer, and zero or several leave the name unresolved -- the caller is then
    told which names exist, which is a better prompt than a wrong key.
    """
    if asked:
        return asked
    try:
        import json as _json

        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        declared = (meta.get("objective") or {}).get("metric")
        if declared:
            return str(declared)
    except Exception:
        pass
    observed = {
        name
        for r in (records or [])
        if getattr(r, "result", None) is not None
        for name in (r.result.metrics or {})
        if name not in _INFRASTRUCTURE_METRICS
    }
    return observed.pop() if len(observed) == 1 else ""


def _budget_line(
    meta: dict,
    *,
    spent: float | None = None,
    remaining: float | None = None,
    unmeasured: dict | None = None,
    spend_error: str | None = None,
) -> str:
    """The campaign's allowance and what is left of it, in its own unit.

    The unit comes from the declaration and is printed, never interpreted: this
    layer is shared by every domain that runs jobs, and it used to say "GPU
    minutes" to a solver campaign spending core-minutes.

    An undeclared budget says so rather than printing nothing. Silence reads as
    "there was no line about budget", which is indistinguishable from "this
    campaign has no limit" -- and the second is the case where the loop most needs
    to know, since nothing will stop it.

    **The spend is printed, and that is a change.** This line used to carry the
    total alone, on the reasoning that subtracting is the caller's judgement. That
    reasoning holds only if the caller can obtain the spend, and it cannot: what a
    solver log offers is one job's ExecutionTime, which is not the campaign's spend
    once there are several jobs, or a killed one, or a core count. Measured
    2026-08-12 -- a loop derived the spend from ExecutionTime and multiplied by a
    core count its job script silently ignores, read 70.9 core-minutes against a
    true 25.33, and cut the task's endTime in half inside a budget that covered the
    original. Its arithmetic was right; the premise was missing from the only place
    that could supply it.

    What is still left to the caller is the judgement: whether the remainder covers
    finishing. That needs a rate extrapolated from readings, and none of it is here.

    A spend that could not be read says so. A zero would read as "nothing spent
    yet", which is the state a loop acts on most freely.
    """
    declared = budget_from_meta(meta)
    if declared is None:
        return "Campaign compute budget: none declared (nothing will stop a run on the total)."
    line = f"Campaign compute budget: {declared.total:g} {declared.unit} in total"
    if spend_error:
        return f"{line}; spend could not be read from the host ({spend_error})."
    if spent is None:
        return f"{line}."
    # Two decimals: this is core-minutes off a wall clock, and a raw float
    # printed 25.3333 where the reading is 25.33.
    left = f", {round(remaining, 2):g} left" if remaining is not None else ""
    line = f"{line}; {round(spent, 2):g} used{left} (measured on the host)."
    if unmeasured:
        named = "; ".join(f"{k} ({v})" for k, v in sorted(unmeasured.items()))
        # Named rather than folded into the total: a spend summed over the jobs that
        # happen to be measurable reads complete and is not. Measured on the ML
        # line, three of six trials recorded no duration at all.
        line += f" Spend not measurable for: {named}."
    return line


def _meta_lines(meta_path: Path) -> list[str]:
    """Everything the campaign's own setup states, ready to print.

    Extracted so the pre-first-submit path can print it too. Before this, a status
    call with only meta.json on disk returned a single "no ledger yet" line and
    dropped the budget, the starting value and the operating policy -- the early
    return sat above the code that printed them. That is exactly the moment the
    policy matters most, because it is when the loop decides how much of the budget
    to ask for. Measured 2026-08-07: the arm then spent six ssh calls hunting for
    the port and the budget it had just been denied.

    Operands only: no remainder is computed, no comparison to the starting value is
    made, nothing is ranked. Those are the judgement under measurement.
    """
    import json as _j

    if not meta_path.exists():
        return []
    try:
        meta = _j.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[str] = []
    out.append(_budget_line(meta))
    refs = meta.get("reference_values")
    if isinstance(refs, dict) and refs:
        stated = ", ".join(f"{k} = {v}" for k, v in sorted(refs.items()))
        out.append(f"Campaign recorded starting value: {stated}")
    seed = meta.get("seed_config")
    if isinstance(seed, dict) and seed:
        out.append(
            f"Campaign seed config (round 0 runs this unless you change it): {_j.dumps(seed, ensure_ascii=False)}"
        )
    policy = _policy_lines(meta.get("operating_policy"))
    if policy:
        out.append("Campaign operating policy (as configured):")
        out.extend(policy)
    else:
        out.append("Campaign operating policy: (none configured)")
    return out


def _policy_lines(policy: Any) -> list[str]:
    """Render a campaign's operating policy for printing, verbatim.

    Three shapes accepted, because a campaign with two rules should not have to
    wrap them in the full structure:

      - a list of strings          -> read as hard triggers
      - {"hard_triggers": [...], "exemplars": [...], "playbook_skill": "..."}
      - anything else / empty      -> no lines, and the caller says "(none configured)"

    Exemplars are printed as saw / thought / did, in the configured order. The
    order matters and is preserved: an exemplar list whose cases all end in "act"
    reads as "always act", so whoever authors it puts a wait-case in on purpose --
    a loop that always kills is broken in the mirror direction of one that never
    does. Reordering here would defeat that.
    """
    if not policy:
        return []
    out: list[str] = []
    if isinstance(policy, (list, tuple)):
        return [f"  - {str(item)}" for item in policy if str(item).strip()]
    if not isinstance(policy, dict):
        return [f"  - {policy}"]

    triggers = policy.get("hard_triggers") or []
    if isinstance(triggers, (list, tuple)) and triggers:
        out.append("  Stop-now conditions:")
        out.extend(f"    - {t}" for t in triggers if str(t).strip())

    exemplars = policy.get("exemplars") or []
    if isinstance(exemplars, (list, tuple)) and exemplars:
        out.append("  How this kind of run has been read before:")
        for i, ex in enumerate(exemplars, 1):
            if not isinstance(ex, dict):
                out.append(f"    {i}. {ex}")
                continue
            out.append(f"    {i}. saw:     {ex.get('saw', '')}")
            out.append(f"       thought: {ex.get('thought', '')}")
            out.append(f"       did:     {ex.get('did', '')}")

    skill = policy.get("playbook_skill")
    if skill:
        out.append(f"  Remedies for these situations: skill {skill} (read its SKILL.md).")

    # Anything the three known keys do not cover, printed as written. The
    # structured shape is this line's; the CFD line writes flat keys of its own
    # ({"example_1": ...}), and a renderer that understands only its own schema
    # dropped those silently -- the operator wrote a rule, the status output had
    # no heading for it, and nothing anywhere said a rule had been discarded.
    # Whoever authors a policy is the one who decides what belongs in it.
    known = {"hard_triggers", "exemplars", "playbook_skill"}
    for key, value in policy.items():
        if key in known:
            continue
        out.append(f"  {key}: {value}")
    return out


def _campaign_history(campaign_dir: Path) -> list[str]:
    """The campaign's own events, oldest first, one line each, verbatim.

    A wake runs in a fresh session, so a conclusion that lives only in one turn's
    text is gone by the time it matters. Measured 2026-08-06 on two independent
    lines: a turn worked out that a fluid property was a thousand times too large,
    said so, and the wake thirty minutes later knew nothing of it; and on the ML
    line the same quantity (steps per epoch) came out right inside one continuous
    turn and wrong by 2.5x on every cold-start wake.

    Rendered at call time rather than baked into the wake message, because that
    message is written when the wake is *scheduled* and read back unchanged when
    it fires -- an injection there would be a snapshot from up to an hour earlier
    while looking entirely current.

    Deliberately not summarised, ranked, or counted ("you have waited 3 times"):
    those are conclusions, and drawing them is the loop's job. This prints
    operands, the same rule the metric series follows.
    """
    import json as _j

    path = campaign_dir / "events.jsonl"
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = _j.loads(line)
        except ValueError:
            continue
        ts = str(e.pop("ts", "?"))
        kind = str(e.pop("kind", "?"))
        rest = " ".join(f"{k}={_j.dumps(v, ensure_ascii=False)}" for k, v in e.items())
        out.append(f"[{ts}] {kind} {rest}".rstrip())
    return out[-_HISTORY_LIMIT:]


def _append_note(campaign_dir: Path, note: str) -> None:
    import json
    from datetime import datetime

    campaign_dir.mkdir(parents=True, exist_ok=True)
    with open(campaign_dir / "notes.jsonl", "a", encoding="utf-8") as f:
        f.write(
            json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "note": note}, ensure_ascii=False) + "\n"
        )


class OpsTuneLaunchTool(Tool):
    """Launch a detached, adaptive BM25 tuning campaign on a remote Docker host."""

    def __init__(self, llm_base_url: str | None = None, llm_model: str | None = None) -> None:
        self._llm_base_url = llm_base_url or ""
        self._llm_model = llm_model or ""

    def _endpoint(self) -> tuple[str, str]:
        """Resolve the proposer LLM (base_url, model). Prefer constructor overrides,
        else read Raven's config -- which is stable across provider wrappers (a lazy
        provider exposes no ``api_base``, so reading the provider object is unreliable)."""
        base, model = self._llm_base_url, self._llm_model
        if base and model:
            return base, model
        try:
            from raven.config import load_config

            cfg = load_config()
            model = model or (cfg.agents.defaults.model or "")
            base = base or (cfg.get_api_base(model) or "")
        except Exception:
            pass
        return base, model

    @property
    def name(self) -> str:
        return "ops_tune_launch"

    @property
    def description(self) -> str:
        return (
            "Launch a long-running adaptive tuning campaign on a remote Docker host and "
            "return immediately with a handle. Each round an LLM proposes hyperparameter "
            "configs, each config is scored in a container, and the best is kept. The "
            "campaign runs detached and survives this session (durable via a ledger), so "
            "use ops_tune_status with the returned ledger path to check progress. Use this "
            "when the user asks to tune / search / optimize hyperparameters on a remote machine."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Remote host running Docker (IP or name)."},
                "port": {"type": "integer", "description": "SSH port (default 22)."},
                "key": {"type": "string", "description": "SSH private key path (default ~/.ssh/id_rsa)."},
                "objective": {
                    "type": "string",
                    "description": "What to optimize, in words, handed to the proposer LLM.",
                },
                "remote_dir": {"type": "string", "description": "Remote working dir (default /root/raven-ops)."},
                "image": {"type": "string", "description": "Container image (default python:3.12-slim)."},
                "app_dir": {"type": "string", "description": "Local trial dir to sync (default benchmarks/ops_bm25)."},
                "k1": {"type": "array", "items": {"type": "number"}, "description": "Seed BM25 k1 values (round 0)."},
                "b": {"type": "array", "items": {"type": "number"}, "description": "Seed BM25 b values (round 0)."},
                "metric": {"type": "string", "description": "Metric to optimize (default ndcg)."},
                "goal": {"type": "string", "enum": ["max", "min"], "description": "max or min (default max)."},
                "max_rounds": {"type": "integer", "description": "Max proposer rounds (default 5)."},
            },
            "required": ["host"],
        }

    async def execute(
        self,
        host: str,
        port: int = 22,
        key: str = "~/.ssh/id_rsa",
        objective: str = "Tune BM25 k1 and b to maximize nDCG@10 on the corpus.",
        remote_dir: str = "/root/raven-ops",
        image: str = "python:3.12-slim",
        app_dir: str = "benchmarks/ops_bm25",
        k1: list[float] | None = None,
        b: list[float] | None = None,
        metric: str = "ndcg",
        goal: str = "max",
        max_rounds: int = 5,
        **kwargs: Any,
    ) -> str:
        llm_base_url, llm_model = self._endpoint()
        if not (llm_base_url and llm_model):
            return (
                "Cannot launch: no LLM endpoint is configured for the proposer. "
                "Configure a chat provider (base URL + model) first."
            )
        cdir = _campaign_dir(host, objective)
        cdir.mkdir(parents=True, exist_ok=True)
        ledger_path = cdir / "ledger.json"
        log_path = cdir / "run.log"

        argv = [
            sys.executable,
            "-m",
            "raven",
            "ops",
            "tune",
            "--adaptive",
            "--host",
            host,
            "--port",
            str(port),
            "--key",
            key,
            "--remote-dir",
            remote_dir,
            "--image",
            image,
            "--app-dir",
            app_dir,
            "--metric",
            metric,
            "--goal",
            goal,
            "--max-rounds",
            str(max_rounds),
            "--objective",
            objective,
            "--llm-base-url",
            llm_base_url,
            "--llm-model",
            llm_model,
            "--ledger",
            str(ledger_path),
        ]
        for v in k1 or [0.6, 1.0, 1.4]:
            argv += ["--k1", str(v)]
        for v in b or [0.3, 0.6, 0.9]:
            argv += ["--b", str(v)]

        log_fh = open(log_path, "w")  # noqa: SIM115 -- handed to the detached child; closed on its exit
        proc = subprocess.Popen(
            argv,
            cwd=str(_REPO_ROOT),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return (
            f"Launched adaptive tuning campaign (pid {proc.pid}) on {host}.\n"
            f"objective: {objective}\n"
            f"ledger: {ledger_path}\n"
            f"log: {log_path}\n"
            f"It runs detached and survives this session. Check progress with "
            f"ops_tune_status(ledger='{ledger_path}')."
        )


class OpsTuneStatusTool(Tool):
    """Report a tuning campaign's progress and best config from its ledger."""

    @property
    def name(self) -> str:
        return "ops_tune_status"

    @property
    def description(self) -> str:
        return (
            "Report the progress of a tuning campaign launched by ops_tune_launch: how many "
            "configs have finished, whether it is still running, and the best config so far. "
            "Pass the ledger path that ops_tune_launch returned."
            "Call it with NO ARGUMENTS to read the campaign that is already set up: that is where the host and port, the starting config, the compute budget, the starting value and the operating policy live, so this is the first thing to do on an ops task rather than looking for the machine yourself. Name the campaign only when more than one exists. "
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ledger": {"type": "string", "description": "Ledger path returned by ops_tune_launch."},
                "metric": {
                    "type": "string",
                    "description": "Metric to rank by; defaults to the campaign's declared objective.",
                },
            },
            "required": [],
        }

    async def execute(self, ledger: str = "", metric: str = "", **kwargs: Any) -> str:
        import json as _json

        from raven.ops import JobStatus, Ledger
        from raven.ops.backends import backend_from_meta

        # No ledger given: fall back to the one campaign under the ops home, so the
        # handle does not have to be typed into every task statement. Ambiguity is
        # reported, never guessed (see _resolve_campaign_dir).
        if not str(ledger).strip():
            try:
                path = _resolve_campaign_dir(kwargs.get("campaign") or "", None) / "ledger.json"
            except ValueError as exc:
                return f"Cannot tell which campaign to read: {exc}"
        else:
            path = Path(ledger).expanduser()
        if not path.exists():
            # Not started yet is not a reason to withhold what the campaign states:
            # this is precisely when the loop decides how much budget to ask for.
            pre = _meta_lines(path.with_name("meta.json"))
            notice = f"No campaign ledger at {path} yet; round 0 has not been submitted."
            return "\n".join([notice, *pre]) if pre else notice

        led = Ledger(path)
        # Reconcile against the remote before reporting: submit only records a
        # trial as pending, so without this poll+fetch the ledger never advances
        # and every check would read stale "pending". meta.json (written by
        # ops_submit) says how to reach the host. Best-effort: on any error, fall
        # back to the stored records.
        meta_path = path.with_name("meta.json")
        # Resolved here, above the progress rendering that reads it, and again
        # after the reconcile below. Both are needed and the order is the whole
        # point: the running-trial series is labelled and filtered by this name,
        # so resolving it only afterwards left the series keyed on the empty
        # string. Measured 2026-08-07 on r15 and r16 -- the loop was shown
        # ``, as logged: None:69.89 100:100 220:220`` instead of the evaluation
        # curve, and spent its first fifty minutes recording "no nDCG eval
        # results captured yet" while the job was writing them all along.
        metric = _campaign_metric(meta_path, metric, led.all())
        progress_lines: list[str] = []
        progress_samples: list[dict] = []
        # Read alongside the reconcile below, from the same backend, because the
        # spend is a measurement and the only place holding it is the host.
        budget_spent: float | None = None
        budget_remaining: float | None = None
        budget_unmeasured: dict = {}
        budget_spend_error: str | None = None
        if meta_path.exists():
            try:
                from raven.ops.instrument import log_event

                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                backend = backend_from_meta(meta)
                try:
                    budget_spent = await backend.spent_minutes()
                    budget_remaining = await backend.remaining_minutes()
                    if hasattr(backend, "unmeasured_spend"):
                        budget_unmeasured = backend.unmeasured_spend() or {}
                except Exception as exc:  # noqa: BLE001 -- an unread spend is reported, not raised
                    # Separate try from the reconcile: a host that cannot answer the
                    # spend must not also cost the trial statuses, and a spend that
                    # silently became 0 is worse than one that says it is unknown.
                    budget_spend_error = type(exc).__name__
                for rec in led.all():
                    if rec.is_terminal or rec.handle is None:
                        continue
                    st = await backend.poll(rec.handle)
                    if st.is_terminal:
                        led.set_result(rec.idem_key, await backend.fetch_result(rec.handle))
                        log_event(path.parent, "trial_terminal_observed", trial=rec.idem_key, status=st.value)
                    else:
                        if st is not rec.status:
                            led.set_status(rec.idem_key, st)
                        # The tail of the still-running trial's progress feed. What
                        # the numbers in it mean is the caller's reading, so neither
                        # this line nor the header that precedes it names a verdict:
                        # a phrase like "non-finite = diverged" is a criterion, and
                        # it is exclusive -- it implies finite values are fine, which
                        # points away from any failure whose numbers stay finite. The
                        # CFD line measures exactly such a failure: a phase fraction
                        # of -122 is impossible and every number in the log is a
                        # number. Wording from that line's 7a92c1f.
                        #
                        # The newest line alone is not enough. A job that logs
                        # loss ten times per evaluation makes the newest line an
                        # evaluation reading only about 9% of the time (measured:
                        # 23 of 253 lines), and loss alone does not separate a
                        # good configuration from a bad one. So also surface the
                        # newest line that actually carries the metric, with its
                        # own step, and let the caller see how stale it is.
                        #
                        # Both series as well, oldest first. This used to print
                        # one reading per look, on the reasoning that assembling
                        # readings into a trend is the judgement being measured.
                        # That conflated two things: a series is the operands, a
                        # trend word is the comparison. Withholding the operands
                        # left the loop unable to answer "has it moved since the
                        # last look" at all -- over a 90 minute run it looked
                        # three times, the fetch held 25, 36 and 36 loss samples
                        # and 2, 4 and 4 evaluation samples, one of each was
                        # printed, and it waited every time. Training loss
                        # falling two orders of magnitude while the evaluation
                        # reading does not improve is the signature that decides
                        # this case, and it exists only across the two series.
                        # Wide enough to cover a whole run, because the series is
                        # the point: 40 samples reach back 800 steps on a job that
                        # logs every 20, and a 90 minute run is 4500. One `tail`
                        # over a few hundred JSON lines costs one ssh round trip.
                        samples = await backend.fetch_progress(rec.handle, tail=400)
                        progress_samples.extend(samples)
                        if samples:
                            progress_lines.append(f"  {rec.idem_key}: {_json.dumps(samples[-1], ensure_ascii=False)}")
                            latest_metric = _latest_with_metric(samples[:-1], metric)
                            if latest_metric is not None:
                                progress_lines.append(
                                    f"  {rec.idem_key} (most recent {metric} sample): "
                                    f"{_json.dumps(latest_metric, ensure_ascii=False)}"
                                )
                            for field, cap in (("loss", _LOSS_SERIES_LIMIT), (metric, _METRIC_SERIES_LIMIT)):
                                series = _as_logged_series(samples, field, limit=cap)
                                if series is not None:
                                    progress_lines.append(f"  {rec.idem_key} {field}, as logged: {series}")
                # Record what was just probed, so a later report's claims about
                # budget, job state and which checkpoints exist can be checked
                # against a reading rather than against nothing. Written here
                # because this is where the probe happens; the loop cannot write it.
                from raven.ops.state_claims import collect_facts, read_facts, write_facts

                facts = await collect_facts(backend, led.all(), now_ms=int(datetime.now().timestamp() * 1000))
                # Carry the readings this probe saw, and advance a probe counter.
                # A decision tool can then ask "did an observation happen since
                # your last one?" without a clock or a turn boundary: citing a
                # reading is only current if the counter moved.
                # Exactly the readings the series printed, via the same thinning,
                # so the basis check never accepts a number that was not shown.
                seen: list[float] = [
                    float(value) for _, value in _series_points(progress_samples, metric, limit=_METRIC_SERIES_LIMIT)[0]
                ]
                for rec in led.all():
                    if rec.result is not None:
                        value = (rec.result.metrics or {}).get(metric)
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            seen.append(float(value))
                facts = _dataclasses.replace(
                    facts,
                    metric_readings={metric: tuple(seen)} if seen else {},
                    probe_seq=read_facts(path.parent).probe_seq + 1,
                )
                write_facts(path.parent, facts)
            except Exception:
                pass

        records = led.all()
        if not records:
            # The campaign's own facts belong on this exit too. It is the turn that
            # decides what to submit, and it is exactly the turn a rule like "check
            # the magnitudes before submitting" exists for -- but with the rules
            # only on the has-trials path it got back one sentence and nothing
            # else. Found on the CFD line's dry run; the same shape as the earlier
            # fix for "no ledger file yet", which left this one standing because a
            # file that exists and holds nothing is a different branch.
            pre = _meta_lines(meta_path)
            notice = f"Campaign ledger {path} is empty (starting up)."
            return "\n".join([notice, *pre]) if pre else notice

        # Resolved after the reconcile above, so a result fetched on this pass can
        # supply the name for a campaign that never declared one.
        metric = _campaign_metric(meta_path, metric, records)

        counts: dict[str, int] = {}
        succeeded = []
        terminal_records = []
        for r in records:
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
            if r.is_terminal and r.result is not None:
                terminal_records.append(r)
            if r.status is JobStatus.SUCCEEDED and r.result and metric in r.result.metrics:
                succeeded.append(r)

        running = any(not r.is_terminal for r in records)
        state = "in progress" if running else "done"
        lines = [
            f"Campaign {state}: {len(records)} trials ({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}).",
        ]
        # The campaign's compute budget, as a fact and not a derived remainder.
        # It was previously invisible here: the training job writes it once in its
        # opening progress sample and this tool prints only the latest sample, so a
        # loop that woke mid-run saw elapsed time with nothing to measure it
        # against, and had to carry the total from its task text. The subtraction
        # is left to the caller on purpose -- deciding when to look again is the
        # judgement under measurement.
        meta_path = path.with_name("meta.json")
        if meta_path.exists():
            try:
                lines.append(
                    _budget_line(
                        _json.loads(meta_path.read_text(encoding="utf-8")),
                        spent=budget_spent,
                        remaining=budget_remaining,
                        unmeasured=budget_unmeasured,
                        spend_error=budget_spend_error,
                    )
                )
            except (OSError, ValueError):
                pass
        # The starting values this campaign recorded, as a campaign-level fact, next
        # to the budget and in the same shape: operands, never a result. No
        # difference, no "below/above", no ordering -- the comparison is the
        # judgement being measured. It is a campaign line and NOT paired onto each
        # progress sample, because pairing would align the two operands for the
        # caller once per reading, which is the comparison itself.
        #
        # Read from `reference_values` rather than from `expected_baseline`, which
        # the report gate checks. Same number in practice, two separate roles: once
        # a value is printed here, "copied it correctly" stops being evidence about
        # the loop and becomes evidence that the answer was on screen -- so the gate
        # needs its own field to keep its refusal count meaningful.
        if meta_path.exists():
            try:
                refs = _json.loads(meta_path.read_text(encoding="utf-8")).get("reference_values")
            except (OSError, ValueError):
                refs = None
            if isinstance(refs, dict) and refs:
                stated = ", ".join(f"{k} = {v}" for k, v in sorted(refs.items()))
                lines.append(f"Campaign recorded starting value: {stated}")
        # The campaign's operating procedure, verbatim and in the configured order.
        #
        # A wake turn starts cold from disk, so procedure stated in the task text
        # reaches only the first turn and is absent from every turn where a decision
        # gets made. Measured 2026-08-06: a loop woke six times, each time
        # re-deriving the situation from scratch, its basis identical in structure
        # every time -- and the task text had spelled the rule out.
        #
        # Neither other route closes this. A skill must survive an LLM relevance
        # gate whose empty answer is legal (measured: injected on 0 of 2 wake turns,
        # 2 of 9 calls, and the failure is silent). The system prompt is global, so
        # it cannot carry one procedure per domain. Campaign meta is the only place
        # that is per-campaign AND reprinted on every look, which is how the budget
        # and the starting value already travel.
        #
        # Exemplars rather than a longer rule list, because the rule list has been
        # tried: the same 2026-08-06 run had "if the score is clearly below the
        # starting value, do not wait -- change the configuration and rerun" in its
        # task text and did not act, six times. A worked example shows how to read
        # the numbers; another rule competes in a channel that is already saturated.
        # Domain timing knowledge rides inside the reasoning ("this kind of run
        # shows its direction inside the first 20% of steps") rather than being
        # declared.
        #
        # Printed, never applied: no summary, no reordering, no "you should".
        if meta_path.exists():
            try:
                policy = _json.loads(meta_path.read_text(encoding="utf-8")).get("operating_policy")
            except (OSError, ValueError):
                policy = None
            policy_lines = _policy_lines(policy)
            if policy_lines:
                lines.append("Campaign operating policy (as configured):")
                lines.extend(policy_lines)
            else:
                # Absent must not read as satisfied. Same failure shape as a report
                # gate printing Accepted while reading a directory that held nothing
                # to check against.
                lines.append("Campaign operating policy: (none configured)")
        # The campaign's durable "chart": the user's conclusion and mid-campaign
        # instructions live on disk so every role (wake turn, heartbeat, chat)
        # sees the same facts regardless of which conversation they run in.
        concluded_path = path.with_name("concluded.json")
        if concluded_path.exists():
            try:
                c = _json.loads(concluded_path.read_text(encoding="utf-8"))
                lines.insert(
                    0,
                    (
                        f"\u26a0\ufe0f CONCLUDED at {c.get('concluded_at', '?')}"
                        + (f" ({c['outcome']})" if c.get("outcome") else "")
                        + (f" -- {c['reason']}" if c.get("reason") else "")
                        + ". This campaign is over: do NOT submit more rounds, and do "
                        "not report again. There is nothing left to do here."
                    ),
                )
            except (OSError, ValueError):
                lines.insert(
                    0,
                    "\u26a0\ufe0f CONCLUDED. This campaign is over: do NOT submit "
                    "more rounds, and do not report again.",
                )
        notes = _read_notes(path.parent)
        if notes:
            lines.append("User instructions (latest first):")
            lines.extend(f"  [{n.get('ts', '?')}] {n.get('note', '')}" for n in reversed(notes[-5:]))
        history = _campaign_history(path.parent)
        if history:
            lines.append("This campaign's record so far, oldest first:")
            lines.extend(f"  {h}" for h in history)
        if progress_lines:
            lines.append("Running-trial progress, as logged:")
            lines.extend(progress_lines)
        if terminal_records:
            # Every finished trial, in submission order, with its numbers as
            # reported. Deliberately not ranked and not reduced to a winner.
            #
            # Gated on there being ANY terminal trial, not on there being a
            # successful one. Measured 2026-08-12 on the OpenFOAM divergence leg:
            # a campaign whose only trial had died rendered the single sentence
            # "No successful trial yet." -- no trial name, no status, and nothing
            # about the SIGFPE stack trace the backend had already captured. The
            # loop guessed at the cause, guessed wrong, and rewrote the case's
            # initial conditions; the next job "reached endTime" in 51 seconds
            # with a third of the water. A failure is a reading, and it was the
            # only reading this campaign had.
            #
            # Sorting them and printing "Best so far" answers the question the
            # agent is there to answer -- whether what it just tried is better or
            # worse than what came before -- and it answers it on a single scalar,
            # which is a poor summary of a training run (a final value, a peak and
            # a stable plateau are different things). Listing is strictly more
            # informative than ranking: the ordering is still derivable, the
            # judgement is left where it belongs.
            lines.append("Finished trials, in submission order:")
            for r in records:
                if not r.is_terminal or r.result is None:
                    continue
                cfg = r.result.output.get("config", r.idem_key)
                numbers = " ".join(f"{k}={v}" for k, v in sorted((r.result.metrics or {}).items()))
                note = "" if r.status is JobStatus.SUCCEEDED else f" [{r.status.value}]"
                lines.append(f"  {cfg}{note}  {numbers or 'no metrics reported'}")
                if r.status is not JobStatus.SUCCEEDED:
                    # What the job itself said on the way out, verbatim and
                    # truncated. Backends put a log tail here exactly for this,
                    # and it was never rendered -- so "the job failed" and "the
                    # job failed because the solver hit a floating point
                    # exception" reached the reader as the same sentence.
                    #
                    # Truncated because status output is read on every wake and a
                    # full log tail would push the rest of the campaign out of
                    # view; whoever wants the rest has ops_outputs. Absence is
                    # printed too: a killed process leaves nothing behind, and
                    # that is a different fact from not having looked.
                    reason = (r.result.error or "").strip()
                    if reason:
                        shown = reason[:_FAILURE_REASON_CHARS]
                        more = " ..." if len(reason) > _FAILURE_REASON_CHARS else ""
                        flat = " / ".join(x.strip() for x in shown.splitlines() if x.strip())
                        lines.append(f"      why: {flat}{more}")
                    else:
                        lines.append("      why: no reason captured (the job left no output)")
                # What this run would hand over, named by the backend. Printed
                # verbatim: the ref and the label come from whoever knows the domain,
                # so nothing here assumes a checkpoint, a step, or that a larger
                # number is the better one. Absent for a backend that cannot name
                # one -- a transient run has no best moment inside it.
                deliverable = r.result.deliverable or {}
                ref = str(deliverable.get("ref") or "")
                if ref:
                    label = str(deliverable.get("label") or "")
                    value = deliverable.get("value")
                    tail = f"  {label}={value}" if label and value is not None else ""
                    lines.append(f"    would hand over: {ref}{tail}")
        elif any(r.status is JobStatus.SUCCEEDED for r in records):
            # Trials finished but none reported the metric asked for. Saying "no
            # successful trial yet" here contradicts the count on the first line and
            # reads as "nothing came back", which is the opposite of the truth.
            available = sorted({name for r in records if r.result is not None for name in (r.result.metrics or {})})
            if metric:
                lines.append(
                    f"Trials succeeded but none reported metric '{metric}'. "
                    + (
                        f"Metrics they do report: {', '.join(available)}."
                        if available
                        else "They reported no metrics at all."
                    )
                )
            else:
                # No name asked for, none declared, and the records do not settle it
                # either. Picking one of several would rank the campaign by a number
                # nobody chose, and that reads exactly like a real ranking.
                lines.append(
                    "This campaign declares no objective, so there is no metric to rank by. "
                    + (
                        f"The trials report: {', '.join(available)}. Pass metric= to read one of them, "
                        if available
                        else "The trials report no metrics at all. "
                    )
                    + "or set objective {metric, direction} on the campaign so every round reads the same one."
                )
        else:
            lines.append("No successful trial yet.")

        log_path = path.with_name("run.log")
        if log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
            if tail:
                lines.append("Recent log:\n  " + "\n  ".join(tail))
        rendered = "\n".join(lines)
        # Record every number this probe actually put in front of the caller, so a
        # basis may cite any of them -- the configuration being run, step counts,
        # elapsed time, the budget -- not only the metric readings. Taken from the
        # rendered text, because that is precisely what was shown: a number that
        # never appeared here is still refused.
        #
        # Written here, after the text exists, and NOT next to the earlier facts
        # write: `lines` is built further down, so computing this there referenced a
        # name that did not exist yet and the surrounding except swallowed it,
        # leaving the field silently empty. Measured 2026-08-06.
        try:
            import dataclasses as _dc

            from raven.ops.state_claims import BASIS_NUMBER_RE, read_facts, write_facts

            current = read_facts(path.parent)
            write_facts(
                path.parent,
                _dc.replace(
                    current,
                    shown_values=tuple(float(m.group()) for m in BASIS_NUMBER_RE.finditer(rendered)),
                ),
            )
        except Exception:
            pass
        return rendered


def _latest_with_metric(samples: list[dict], metric: str) -> dict | None:
    """The newest sample carrying a numeric value for metric, or None.

    Matches on the key containing the metric name because the job names its
    own fields (a metric asked for as "ndcg" arrives as "eval_ndcg").
    """
    for sample in reversed(samples):
        for key, value in sample.items():
            if metric in key and isinstance(value, (int, float)) and not isinstance(value, bool):
                return sample
    return None


_METRIC_SERIES_LIMIT = 24
_LOSS_SERIES_LIMIT = 12


def _series_points(samples: list[dict], field: str, *, limit: int) -> tuple[list[tuple], bool]:
    """``(step, value)`` pairs carrying field, oldest first, thinned to limit.

    Thinning keeps the first and the last and spaces the rest evenly. Taking the
    newest limit instead would cover only the tail: a job logging every 20 steps
    puts 12 consecutive readings inside 240 steps, one noise band, while the
    change worth seeing spans thousands of steps.

    Shared with the recorded facts on purpose. The basis check accepts a decision
    whose cited number is among the readings this probe recorded, so recording
    more than was printed would accept a number the loop was never shown.
    """
    points = []
    for sample in samples:
        step = sample.get("step")
        for key, value in sample.items():
            if field in key and isinstance(value, (int, float)) and not isinstance(value, bool):
                points.append((step, value))
                break
    if len(points) <= limit:
        return points, False
    spacing = (len(points) - 1) / (limit - 1)
    return [points[round(i * spacing)] for i in range(limit)], True


def _as_logged_series(samples: list[dict], field: str, *, limit: int = _LOSS_SERIES_LIMIT) -> str | None:
    """The values of field across samples as ``step:value`` pairs, oldest first.

    Chronological and unreduced. Not sorted by value, no marked best, no
    difference against anything, no word for the direction: those are the
    comparison the loop is scored on. The order they were logged in is not a
    comparison, and without it "below the starting value" is available on every
    look while "still not moving after another 600 steps" is available on none.

    Returns None rather than an empty string when nothing carries the field, so
    a field the job never logs gets no line at all -- an empty series reads as
    "measured, and there is nothing there".
    """
    points, sampled = _series_points(samples, field, limit=limit)
    if not points:
        return None
    line = " ".join(f"{step}:{value}" for step, value in points)
    return f"{line} (sampled)" if sampled else line


_BASIS_HELP = (
    "What you read that supports this decision -- cite the actual number you just "
    "observed. Required on every decision (wait, kill, or submit a further round) so "
    "that what a decision rested on is recorded, not inferred later."
)


def _basis_refusal(cdir: "Path", basis: str, action: str) -> str | None:
    """Refuse a decision whose basis does not rest on an observation just made.

    Applied to waiting, killing and re-submitting alike. Requiring it of only one
    of them would put friction on one side of a choice and none on the others,
    and that is a nudge toward the cheaper side -- which is why it is the same
    check in all three places.
    """
    from raven.ops.instrument import log_event
    from raven.ops.state_claims import basis_problems, read_decision_seq, read_facts

    facts = read_facts(cdir)
    problems = basis_problems(basis or "", facts, last_seq=read_decision_seq(cdir))
    if not problems:
        return None
    log_event(cdir, "basis_refused", action=action, reasons=problems)
    return "REFUSED: " + "; ".join(problems) + "."


def _record_basis(cdir: "Path", basis: str, action: str) -> None:
    from raven.ops.instrument import log_event
    from raven.ops.state_claims import read_facts, write_decision_seq

    write_decision_seq(cdir, read_facts(cdir).probe_seq)
    log_event(cdir, "basis_accepted", action=action, basis=(basis or "")[:400])


def _schedule_ops_wake(
    cron: "CronService | None",
    channel: str,
    chat_id: str,
    *,
    name: str,
    message: str,
    eta_seconds: int,
) -> str:
    """Schedule a one-shot self-wake via the CronService directly.

    Bypasses the cron *tool* (which refuses to self-schedule inside a
    cron-triggered turn) so the campaign loop can keep rescheduling across its
    own wakes. Returns a human note describing what was scheduled, or why not.

    Idempotent per campaign: an existing pending wake for the same campaign is
    replaced, not joined. Without that, a turn that schedules twice ends up with
    two futures for one campaign -- measured 2026-08-06, when a turn alternated
    ops_check_later and ops_tune_status fifteen times (every basis citing a real
    freshly probed reading, so the basis gate accepted each one) and left sixteen
    pending wakes, i.e. sixteen drivers each deciding again from scratch. Which
    wake to keep is not a judgement call: the last request is what the loop asked
    for, so the last one wins.
    """
    if cron is None:
        return "No scheduler available; check ops_tune_status manually when results are due."
    if not (channel and chat_id):
        return "No session context to schedule a wake; check ops_tune_status manually when results are due."
    from raven.proactive_engine.schedulers.cron.types import CronSchedule

    replaced = 0
    parts = name.split(":")
    campaign = parts[1] if name.startswith("ops:") and len(parts) >= 2 and parts[1] else None
    if campaign:
        # The name prefix is the half that reaches jobs written before the campaign
        # field existed; add_job's own check below keys on the field and reaches the
        # producers that never followed the naming rule. Both halves are needed
        # until no pre-field job can still be pending.
        prefix = f"ops:{campaign}:"
        try:
            for existing in cron.list_jobs(include_disabled=True):
                tagged = (getattr(existing.payload, "campaign", None) or "") == campaign
                if (tagged or existing.name.startswith(prefix)) and cron.remove_job(existing.id):
                    replaced += 1
        except Exception:
            replaced = 0

    at = datetime.now() + timedelta(seconds=max(1, int(eta_seconds)))
    try:
        job = cron.add_job(
            campaign=campaign,
            name=name,
            schedule=CronSchedule(kind="at", at_ms=int(at.timestamp() * 1000)),
            message=message,
            deliver=True,
            channel=channel,
            to=chat_id,
            delete_after_run=True,
            dedup=False,  # Ops wakes fire in quick succession to the same channel/to; never treat as duplicate reminders
        )
    except ValueError as exc:
        return f"Scheduling the wake failed ({exc}); check ops_tune_status manually."
    note = f"Scheduled a wake at ~{at.isoformat(timespec='seconds')} (job {job.id})."
    if replaced:
        # Say it moved rather than added: "Scheduled a wake" alone reads as "now
        # there are two", which is what a loop scheduling in a tight sequence
        # would have to assume.
        note += f" This replaced the campaign's previous pending wake ({replaced}); one is pending."
    return note


class _OpsScheduler(Tool):
    """Base for ops tools that schedule a self-wake; carries the session context."""

    def __init__(self, cron_service: "CronService | None" = None) -> None:
        self._cron = cron_service
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id


class OpsSubmitTool(_OpsScheduler):
    """Submit one round of trials to a remote host and schedule a self-wake to decide the next.

    This is the agent-in-the-loop primitive: the agent (not a baked-in proposer)
    picks the config(s), submits them as detached jobs, and -- because job runtime
    ranges from seconds to days -- estimates when to check back and schedules a
    one-shot wake at that time. On that wake the agent reads the ledger and decides
    the next round: ops_submit again, ops_check_later to wait more, ops_finish to
    finish, or ops_ask_owner when the decision is genuinely the owner's.
    ``max_rounds`` is the runaway backstop.
    """

    timeout_seconds = 120.0

    @property
    def name(self) -> str:
        return "ops_submit"

    @property
    def description(self) -> str:
        return (
            "Train, fine-tune, tune, sweep or optimize a MODEL on a remote machine -- ONE ROUND AT A "
            "TIME, steering it yourself. Use this for any such request, however it is worded: "
            '"fine-tune a model on host X", "train this", "run this config and watch it", '
            '"tune these hyperparameters". DO NOT do it yourself with exec or ssh: routing the job '
            "through this tool is what makes its compute budget enforceable, its state survive a "
            "restart, and its results wake you when they are due.\n"
            "A campaign is usually already set up, and then its host, port, backend, compute budget "
            "and starting config all come from its own state -- omit them and omit the campaign name; "
            "supply only what THIS round changes. Read it with ops_tune_status first.\n"
            "Each round YOU choose the config(s) and submit them: start small -- one config, or a "
            "few only if the host has the capacity to run them at once, and NEVER a full grid -- "
            "because the whole point is to read each round's results "
            "and choose the next configs from them. Because a job may take seconds or days, estimate "
            "eta_seconds -- when to look next, NOT when the job will finish -- and this tool wakes you then. On that wake, "
            "read the ledger with ops_tune_status and decide: ops_submit the next config(s) "
            "(increment round), ops_check_later if still running, ops_finish to end it and hand the "
            "result back, or ops_ask_owner if the decision is genuinely the owner's. Every branch "
            "names the tool that performs it, because a wake turn is a cold start and a branch with "
            "no tool behind it is one the loop can only act out in prose. State is durable in the "
            "ledger, so the campaign resumes across a restart.\n"
            "DO NOT run the experiment yourself: do not reproduce the computation locally with exec, "
            "and do not pull a domain skill (use_skill) to compute the result in this process. The "
            "job must run on the remote host through this tool. Reading a skill or reference to help "
            "CHOOSE configs is fine; executing the experiment anywhere but here is not."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Remote host running Docker."},
                "configs": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": 'Config dict(s) to run this round, e.g. [{"k1": 1.5, "b": 0.75}]. Batch = many.',
                },
                "objective": {"type": "string", "description": "What you are optimizing, in words."},
                "ledger": {
                    "type": "string",
                    "description": "Ledger path for this campaign; reuse the same path every round so history accumulates.",
                },
                "eta_seconds": {
                    "type": "integer",
                    "description": (
                        "When to look next, in seconds of wall-clock time. Not an estimate of when the "
                        "job finishes. The compute budget is counted in GPU minutes, which on a "
                        "multi-GPU host is a different quantity. The pace is yours to choose."
                    ),
                },
                "round": {"type": "integer", "description": "This round index (start at 0, increment each call)."},
                "basis": {
                    "type": "string",
                    "description": (_BASIS_HELP + " Required from round 1 onward; round 0 has nothing observed yet."),
                },
                "max_rounds": {
                    "type": "integer",
                    "description": "Runaway backstop; refuses to submit at/after this (default 8).",
                },
                "port": {"type": "integer", "description": "SSH port (default 22)."},
                "key": {"type": "string", "description": "SSH key path (default ~/.ssh/id_rsa)."},
                "remote_dir": {"type": "string", "description": "Remote working dir (default /root/raven-ops)."},
                "image": {"type": "string", "description": "Container image (default python:3.12-slim)."},
                "app_dir": {"type": "string", "description": "Local trial dir to sync (default benchmarks/ops_bm25)."},
                "metric": {
                    "type": "string",
                    "description": "Metric the campaign optimises, e.g. 'ndcg', 'residual'. Recorded on the campaign at round 0; later calls read it from there.",
                },
                "goal": {
                    "type": "string",
                    "enum": ["max", "min"],
                    "description": "Whether the metric should be maximised or minimised. Recorded with the metric at round 0; without it no artifact can be named as the one to hand over, because the direction cannot be inferred from the metric name.",
                },
                "campaign": {
                    "type": "string",
                    "description": "Campaign name grouping the ledger records. Omit when the ops home holds exactly one campaign.",
                },
            },
            "required": ["objective", "eta_seconds"],
        }

    async def execute(
        self,
        objective: str,
        eta_seconds: int,
        host: str = "",
        configs: list[dict] | None = None,
        ledger: str = "",
        round: int = 0,
        basis: str = "",
        max_rounds: int = 8,
        port: int = 22,
        key: str = "~/.ssh/id_rsa",
        remote_dir: str = "/root/raven-ops",
        image: str = "python:3.12-slim",
        app_dir: str = "benchmarks/ops_bm25",
        # metric + goal are recorded on the campaign at round 0 as its objective, and
        # every later reader takes the metric name from there. Neither is guessed:
        # this backend is shared with every domain that runs a plain command, and the
        # direction cannot be inferred from the name -- for a loss the best value is
        # the smallest, so a guess would name the worst checkpoint as confidently as
        # the best one.
        metric: str = "",
        goal: str = "",
        # Empty, not "ops": a literal default made an unnamed submit anchor to a
        # campaign called "ops" while every later call used the real name, so one
        # experiment ended up with two drivers -- observed on the cfd line as
        # ops:ops:r1 alongside ops:cfd-transient:recheck. Empty means "the one
        # campaign under the ops home", and ambiguity is reported, never guessed.
        campaign: str = "",
        **kwargs: Any,
    ) -> str:
        from raven.ops import JobSpec, Ledger, config_key
        from raven.ops.backend import JobBackendError, JobResult, JobStatus
        from raven.ops.backends import backend_from_meta, prepare_from_meta

        if round >= max_rounds:
            return (
                f"Reached max_rounds ({max_rounds}) for campaign '{campaign}'. Not submitting more. "
                f"Read ops_tune_status(ledger='{ledger}') and report the best config to the user, or "
                f"raise max_rounds only if the user asks to keep going."
            )
        if not campaign:
            try:
                campaign = _resolve_campaign_dir("", ledger).name
            except ValueError as exc:
                return f"Cannot tell which campaign to submit to: {exc}"
        if not configs:
            # Round zero's config belongs to whoever set the campaign up -- the code
            # and its defaults are the upstream deliverable -- not to the person
            # asking for the work. Read it from the campaign, and say plainly when
            # there is none: a submit with nothing to run must not look like it ran.
            seed = None
            try:
                import json as _seed_json

                seed_meta = _seed_json.loads(
                    (_resolve_campaign_dir(campaign, ledger) / "meta.json").read_text(encoding="utf-8")
                )
                seed = seed_meta.get("seed_config")
            except (OSError, ValueError):
                seed = None
            if isinstance(seed, dict) and seed:
                configs = [seed]
            elif isinstance(seed, (list, tuple)) and seed:
                configs = [dict(c) for c in seed if isinstance(c, dict)]
            if not configs:
                return "No configs given and the campaign has no seed_config; nothing to submit."

        # Tolerate the agent packing the port into host ("1.2.3.4:64106") -- the
        # prompt phrasing "host (SSH port N)" invites it. Split it so the SSH runner
        # gets a bare host instead of an unresolvable "host:port" name.
        def _split_hostport(h: str, p: int) -> tuple[str, int]:
            if isinstance(h, str) and ":" in h:
                head, _, tail = h.rpartition(":")
                if head and tail.isdigit():
                    return head, int(tail)
            return h, p

        host, port = _split_hostport(host, port)

        # Anchor a relative/empty ledger to a stable absolute path keyed by campaign:
        # the path travels through the wake message to later rounds and to
        # ops_tune_status, and a relative path would resolve against a cwd that can
        # differ across turns (especially a cron-triggered wake).
        led = Path(ledger).expanduser() if ledger else Path()
        if not led.is_absolute():
            led = _ops_home() / _slug(campaign) / "ledger.json"
        ledger = str(led)

        # The user's conclusion is durable state every role must respect: once a
        # campaign is concluded, no turn -- a late wake, the heartbeat, anyone --
        # may submit more rounds, regardless of what its own context says.
        if (led.parent / "concluded.json").exists():
            return (
                f"Campaign '{campaign}' was CONCLUDED by the user; not submitting. "
                f"Report the recorded best instead, or start a new campaign name if more experiments are wanted."
            )

        # The declaration itself, before anything else is asked of this round. It is
        # a local file, so no host is involved, and a campaign whose meta has moved
        # is not one whose basis is worth grading. Measured 2026-08-12: an arm
        # rewrote remote_dir/staged_case/command because the task text named a
        # different case path, and the job then ran out of an undeclared directory
        # while the budget guard watched the declared one.
        from raven.ops.apparatus import load_baseline as _load_baseline
        from raven.ops.apparatus import meta_sha as _meta_sha
        from raven.ops.instrument import log_event

        _base = _load_baseline(led.parent)
        if _base and _base.get("meta_sha") not in ("-", None) and _meta_sha(led.parent) != _base["meta_sha"]:
            log_event(led.parent, "apparatus_refused", reason="meta_changed", round=round)
            return (
                "REFUSED: this campaign's meta.json has changed since its first submit. "
                "That file is the apparatus' declaration -- what the target is, what the budget is, "
                "which host and which case the job runs on -- so changing it changes what this "
                "experiment measures, and where it runs.\n"
                "If you believe something in it is wrong, say so with ops_ask_owner. To carry on, "
                "restore it to what it was. Nothing was submitted."
            )

        if round >= 1:
            # From round 1 on there has been something to observe, so the same basis
            # requirement applies here as to waiting and killing. Round 0 is exempt
            # because nothing has been observed yet -- that is the absence of the
            # thing being cited, not a concession.
            #
            # After the runaway backstop and the concluded check: those two are "you
            # cannot submit at all", and a refusal should name the more fundamental
            # reason, not a missing justification for an action that was not going to
            # happen anyway.
            refusal = _basis_refusal(led.parent, basis, "submit")
            if refusal:
                return refusal

        # Persist how to reach the host next to the ledger so ops_tune_status can
        # reconcile the ledger against the remote (poll + fetch) without the agent
        # re-supplying connection details on every check.
        import json as _json

        led.parent.mkdir(parents=True, exist_ok=True)
        meta_file = led.parent / "meta.json"
        meta: dict[str, Any] = {
            "host": host,
            "port": port,
            "key": key,
            "remote_dir": remote_dir,
            "image": image,
        }
        if metric and goal in ("max", "min"):
            meta["objective"] = {"metric": metric, "direction": goal}
        if meta_file.exists():
            # Campaign state wins over this round's arguments: the connection (and
            # the backend the campaign runs on) is fixed at round 0, so a later
            # round that omits port/key -- agents routinely drop them -- cannot
            # fall back to a wrong default such as SSH port 22.
            try:
                stored = _json.loads(meta_file.read_text(encoding="utf-8"))
                meta.update(stored)
                meta["host"], meta["port"] = _split_hostport(meta.get("host", host), int(meta.get("port", port)))
                if "objective" not in stored and meta.get("objective"):
                    # A campaign whose meta was written by hand before the first
                    # submit -- how every experiment here is set up -- reached this
                    # branch, so the objective assembled above lived in memory for
                    # one call and was never on disk. Only this key is added back:
                    # rewriting the merged dict would also push this round's
                    # connection arguments into a file that deliberately outranks
                    # them.
                    stored["objective"] = meta["objective"]
                    meta_file.write_text(_json.dumps(stored), encoding="utf-8")
            except (OSError, ValueError):
                pass
        else:
            meta_file.write_text(_json.dumps(meta), encoding="utf-8")

        if round == 0:
            prepare_from_meta(meta, app_dir=app_dir)

        backend = backend_from_meta(meta)

        # The apparatus, checked against what this campaign was set up with. The
        # first submit is where the baseline is taken -- it is the last moment the
        # setup is known to be as the operator left it.
        #
        # Two halves, deliberately unequal. meta.json is the campaign's own
        # declaration and an agent editing it is out of role in any domain, so a
        # changed meta refuses; the case is the agent's to edit and its changes are
        # only reported. Measured 2026-08-12: one arm rewrote remote_dir/staged_case
        # /command because the task text named a different path than the meta did,
        # and the job then ran out of an undeclared directory while a budget guard
        # watched the declared one. Another arm rewrote the case's initial
        # conditions and solved a different problem in 51 seconds. Neither concealed
        # anything -- both wrote a reason -- so recording is not the gap; noticing is.
        from raven.ops.apparatus import baseline_of, compare, load_baseline, save_baseline

        cdir = led.parent
        _runner = getattr(backend, "_run", None)
        try:
            _now = baseline_of(cdir, _runner)
            _drift = compare(load_baseline(cdir), _now)
        except Exception:  # noqa: BLE001 -- a fingerprint failure must not block work
            _now, _drift = None, None
        _drift_lines: list[str] = []
        if _drift is not None and not _drift.is_clean:
            _drift_lines = _drift.describe()
            log_event(cdir, "apparatus_drift", round=round, changes=_drift_lines)
        if _now is not None and load_baseline(cdir) is None:
            save_baseline(cdir, _now)

        ledger_obj = Ledger(ledger)
        submitted = []
        refused = []
        for cfg in configs:
            key_id = config_key(cfg)
            ledger_obj.record(key_id, campaign=campaign)
            try:
                handle = await backend.submit(JobSpec(cfg, idem_key=key_id, labels={"campaign": campaign}))
            except JobBackendError as exc:
                # Recording before submitting is deliberate -- a crash in between
                # leaves an orphan a later reconcile can find. But a submit that
                # was *refused* has no job to find, and a record with no handle is
                # skipped by reconciliation, so it would read as pending forever
                # and keep the agent waiting on something that never started.
                ledger_obj.set_result(key_id, JobResult(JobStatus.FAILED, error=str(exc)[:400]))
                refused.append(f"{key_id}: {exc}")
                continue
            ledger_obj.set_handle(key_id, handle)
            submitted.append(key_id)

        if not submitted:
            detail = "\n".join(refused) or "no reason reported"
            return (
                f"No jobs were submitted for campaign '{campaign}' round {round}; the backend refused:\n"
                f"{detail}\nNothing is running and nothing is scheduled. Read "
                f"ops_tune_status(ledger='{ledger}') and report the best result so far."
            )

        next_round = round + 1
        if _drift_lines:
            # Said on the way out, not only written to the trail: the turn that
            # made the edit is the one that can still explain or undo it.
            submitted_note = "\n".join(f"  - {x}" for x in _drift_lines)
        else:
            submitted_note = ""
        message = (
            f"[Ops campaign '{campaign}' round {round} due] Jobs on {meta['host']} for objective "
            f"'{objective}' should be ready. Call ops_tune_status(ledger='{ledger}') "
            f"to read results, then decide: ops_submit the next config(s) with round={next_round} "
            f"exactly (always increment the round, never reuse a previous number; max_rounds={max_rounds}) "
            f"if ready, ops_check_later if still running, ops_finish to end it and hand the result "
            f"back, or ops_ask_owner if the decision is genuinely the owner's. If trials FAILED with a "
            f"code error in the trial script, do NOT rewrite the trial code yourself: triage it -- "
            f"ops_finish with outcome='failed' to hand it off."
        )
        wake_note = _schedule_ops_wake(
            self._cron,
            self._channel,
            self._chat_id,
            name=f"ops:{campaign}:r{next_round}",
            message=message,
            eta_seconds=eta_seconds,
        )
        from raven.ops.instrument import log_event

        log_event(led.parent, "submit", round=round, trials=submitted)
        if round >= 1:
            _record_basis(led.parent, basis, "submit")
        log_event(led.parent, "wake_scheduled", round_due=round, eta_seconds=eta_seconds)
        drift_note = ""
        if submitted_note:
            # Stated as fact, with no verdict attached. Whether a given edit was the
            # job being done or the question being changed depends on the domain,
            # and that call belongs to whoever wrote this task's pre-registration.
            drift_note = (
                "\nThe case has changed since this campaign's first submit:\n"
                f"{submitted_note}\n"
                "That is recorded. If any of it was not you, or not intended, say so before "
                "this round's results are read against the earlier ones.\n"
            )
        return (
            f"Submitted {len(submitted)} job(s) for campaign '{campaign}' round {round} on {meta['host']}: "
            f"{', '.join(submitted)}.\nLedger: {ledger}\n{drift_note}{wake_note}"
        )


class OpsCheckLaterTool(_OpsScheduler):
    """Re-schedule a later check on a running campaign without submitting anything.

    For the "woke too early" case: the agent wakes, sees via ops_tune_status that
    the jobs are still running, and simply wants to wait more. This schedules
    another wake at the agent's re-estimated ETA -- no new trials -- so it does not
    duplicate work, and (like ops_submit) schedules through the CronService directly
    so it works inside a cron-triggered turn.
    """

    timeout_seconds = 30.0

    @property
    def ends_turn(self) -> bool:
        """Waiting is the whole point of this tool, so the turn is over once it
        has been called: the next decision belongs to the scheduled wake."""
        return True

    @property
    def name(self) -> str:
        return "ops_check_later"

    @property
    def description(self) -> str:
        return (
            "Wait longer on a campaign whose jobs are not finished yet: schedule another wake to "
            "re-check, without submitting any new trials. Use this when you wake and ops_tune_status "
            "shows work still running. Pass eta_seconds = your re-estimate of the remaining time."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "campaign": {"type": "string", "description": "Campaign name (same as used in ops_submit)."},
                "ledger": {"type": "string", "description": "Ledger path for this campaign."},
                "eta_seconds": {
                    "type": "integer",
                    "description": (
                        "When to look next, in seconds of wall-clock time. Not an estimate of when the "
                        "job finishes. The compute budget is counted in GPU minutes, which on a "
                        "multi-GPU host is a different quantity. The pace is yours to choose."
                    ),
                },
                "basis": {"type": "string", "description": _BASIS_HELP},
                "metric": {
                    "type": "string",
                    "description": "Metric name to read back; defaults to the campaign's declared objective.",
                },
                "objective": {"type": "string", "description": "Objective text, for the wake message (optional)."},
            },
            "required": ["eta_seconds", "basis"],
        }

    async def execute(
        self,
        eta_seconds: int,
        campaign: str = "",
        ledger: str = "",
        basis: str = "",
        metric: str = "",
        objective: str = "",
        **kwargs: Any,
    ) -> str:
        cdir = _resolve_campaign_dir(campaign, ledger)
        # Backfill both handles from the resolved campaign. Omitting them is allowed,
        # and without this the wake was named "ops::recheck" -- outside the
        # "ops:<campaign>:" prefix that every dedup and re-arm check keys on, so the
        # one-pending-wake invariant did not apply to it -- and its message read
        # "[Ops campaign '' re-check] Call ops_tune_status(ledger='')", which is the
        # entire context a wake turn gets.
        campaign = campaign or cdir.name
        ledger = ledger or str(cdir / "ledger.json")
        refusal = _basis_refusal(cdir, basis, "check_later")
        if refusal:
            return refusal
        # No metric named here: the status tool reads the campaign's declared
        # objective, so pinning one in the wake message can only override it.
        # Every branch names the tool that performs it. "stop" was a verb with no
        # tool behind it, and a wake turn is a cold start whose whole context is
        # this message: measured 2026-08-11 across four runs in two domains, the
        # loop finished its work, wrote a complete report into the chat, offered
        # the user three options and waited -- for an answer nobody was there to
        # give. It re-armed until the cap each time. It had written the report;
        # what it lacked was the name of the door. ops_check_later is named here
        # and was called every time; ops_finish's predecessor was named nowhere and called never.
        message = (
            f"[Ops campaign '{campaign}' re-check] Call ops_tune_status(ledger='{ledger}') "
            f"again. If results are ready, decide the next round: ops_submit to run another, "
            f"ops_finish to end it and hand the result back, or ops_ask_owner if the decision "
            f"is genuinely the owner's. If still running, ops_check_later again."
        )
        from raven.ops.instrument import log_event

        log_event(cdir, "check_later", eta_seconds=eta_seconds)
        _record_basis(cdir, basis, "check_later")
        return _schedule_ops_wake(
            self._cron,
            self._channel,
            self._chat_id,
            name=f"ops:{campaign}:recheck",
            message=message,
            eta_seconds=eta_seconds,
        )


class OpsNoteTool(Tool):
    """Record a user instruction on a campaign's durable state (its "chart").

    Wake turns start cold from disk and never see the chat conversation, so a
    mid-campaign instruction said in chat is invisible to them unless it is
    written down. This appends the instruction to the campaign's notes file;
    ops_tune_status surfaces the latest notes to every subsequent turn.
    """

    @property
    def name(self) -> str:
        return "ops_note"

    @property
    def description(self) -> str:
        return (
            "Record the user's mid-campaign instruction or decision for a running ops campaign "
            "(e.g. 'explore larger k1 next round', 'prefer fewer trials'). Wake turns cannot see "
            "this chat -- writing the note is the ONLY way your instruction reaches the next round. "
            "Use whenever the user gives guidance about an ongoing campaign."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "campaign": {"type": "string", "description": "Campaign name (as used in ops_submit)."},
                "note": {"type": "string", "description": "The user's instruction, verbatim or faithfully summarized."},
                "ledger": {"type": "string", "description": "Ledger path if known (locates the campaign dir)."},
            },
            "required": ["campaign", "note"],
        }

    async def execute(self, campaign: str, note: str, ledger: str | None = None, **kwargs: Any) -> str:
        cdir = _resolve_campaign_dir(campaign, ledger)
        _append_note(cdir, note)
        from raven.ops.instrument import log_event

        log_event(cdir, "note", note=note)
        return f"Noted for campaign '{campaign}': {note}\nThe next wake turn will see it via ops_tune_status."


class OpsKillTool(Tool):
    """Stop a running trial early (the early-stop lever).

    The saving-compute half of on-call lives here: a trial the caller judges not
    worth its remaining compute frees the machine for the next round instead of
    being paid for to the end. The killed trial is recorded FAILED with the
    reason, so history stays honest.

    When to use it is deliberately absent, here and in the description. Naming a
    condition -- "diverged", "clearly dominated" -- hands over the judgement the
    round is measuring, and any condition named is also a condition implied to be
    the only one.
    """

    timeout_seconds = 120.0

    @property
    def name(self) -> str:
        return "ops_kill"

    @property
    def description(self) -> str:
        return (
            "Kill specific RUNNING trials of an ops campaign early, when you judge a trial is not "
            "worth the compute it has left, so that compute is available for what you do next. "
            "Killed trials are recorded as failed with your reason. This kills individual trials; to conclude the whole campaign use ops_finish."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "campaign": {"type": "string", "description": "Campaign name."},
                "trials": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Trial keys to kill (as shown in ops_tune_status).",
                },
                "reason": {"type": "string", "description": "Why, in your own words."},
                "basis": {"type": "string", "description": _BASIS_HELP},
                "ledger": {"type": "string", "description": "Ledger path (locates the campaign dir)."},
            },
            "required": ["trials", "basis"],
        }

    async def execute(
        self,
        trials: list[str],
        campaign: str = "",
        basis: str = "",
        reason: str = "",
        ledger: str | None = None,
        **kwargs: Any,
    ) -> str:
        import json as _json

        from raven.ops import JobResult, JobStatus, Ledger
        from raven.ops.backends import backend_from_meta
        from raven.ops.instrument import log_event

        cdir = _resolve_campaign_dir(campaign, ledger)
        ledger_path = cdir / "ledger.json"
        meta_path = cdir / "meta.json"
        if not ledger_path.exists() or not meta_path.exists():
            return f"No campaign state under {cdir} (need ledger.json + meta.json)."
        refusal = _basis_refusal(cdir, basis, "kill")
        if refusal:
            return refusal
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        backend = backend_from_meta(meta)
        led = Ledger(ledger_path)
        killed, skipped = [], []
        for key in trials:
            rec = led.get(key)
            if rec is None or rec.is_terminal or rec.handle is None:
                skipped.append(key)
                continue
            await backend.cancel(rec.handle)
            led.set_result(
                rec.idem_key, JobResult(JobStatus.FAILED, error=f"killed early: {reason or 'agent decision'}")
            )
            log_event(cdir, "kill", trial=key, reason=reason)
            killed.append(key)
        if killed:
            _record_basis(cdir, basis, "kill")
        out = f"Killed {len(killed)} trial(s): {', '.join(killed) or '-'}."
        if skipped:
            out += f" Skipped (not running/unknown): {', '.join(skipped)}."
        return out
