"""Tests for the Ops chat tools (launch + status)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import raven.ops as ops_pkg
from raven.config import paths as config_paths
from raven.agent.tools import ops as ops_tools
from raven.agent.tools.ops import OpsTuneLaunchTool, OpsTuneStatusTool



def _write_running_ledger(cdir: Path) -> None:
    (cdir / "ledger.json").write_text(
        json.dumps({
            "version": 1,
            "records": {
                "t1": {
                    "idem_key": "t1", "status": "running", "campaign": "c",
                    "handle": {"backend": "process", "job_id": "ops-t1"},
                    "result": None, "attempts": 1, "escalated": False,
                }
            },
        }),
        encoding="utf-8",
    )


class _FakeProc:
    pid = 4321


async def test_launch_spawns_detached_and_returns_handle(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config_paths, "get_ops_home", lambda: tmp_path / "ops")
    captured: dict = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return _FakeProc()

    monkeypatch.setattr(ops_tools.subprocess, "Popen", fake_popen)

    tool = OpsTuneLaunchTool(llm_base_url="https://x/v1", llm_model="qwen3.6-27B")
    out = await tool.execute(host="1.2.3.4", port=64106, objective="tune bm25", k1=[0.8, 1.2], b=[0.4])

    argv = captured["argv"]
    assert argv[1:6] == ["-m", "raven", "ops", "tune", "--adaptive"]
    assert "--host" in argv and argv[argv.index("--host") + 1] == "1.2.3.4"
    assert argv[argv.index("--llm-model") + 1] == "qwen3.6-27B"
    assert argv[argv.index("--llm-base-url") + 1] == "https://x/v1"
    # seed grid values are threaded through as repeated flags
    assert argv.count("--k1") == 2 and argv.count("--b") == 1
    assert captured["kw"]["start_new_session"] is True  # detached from this process
    assert "pid 4321" in out and "ledger" in out and "ops_tune_status" in out


async def test_launch_refuses_without_llm_endpoint(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config_paths, "get_ops_home", lambda: tmp_path / "ops")
    monkeypatch.setattr(
        ops_tools.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn without an endpoint")
    )
    import raven.config as raven_config

    monkeypatch.setattr(raven_config, "load_config", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no config")))
    tool = OpsTuneLaunchTool(llm_base_url="", llm_model="")
    out = await tool.execute(host="1.2.3.4")
    assert "no LLM endpoint" in out.lower() or "cannot launch" in out.lower()


async def test_launch_resolves_endpoint_from_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config_paths, "get_ops_home", lambda: tmp_path / "ops")
    captured: dict = {}
    monkeypatch.setattr(ops_tools.subprocess, "Popen", lambda argv, **kw: captured.update(argv=argv) or _FakeProc())

    class _Defaults:
        model = "qwen3.6-27B"

    class _Agents:
        defaults = _Defaults()

    class _Cfg:
        agents = _Agents()

        def get_api_base(self, model):
            return "https://gw/v1"

    import raven.config as raven_config

    monkeypatch.setattr(raven_config, "load_config", lambda *a, **k: _Cfg())
    # Constructed WITHOUT a base_url, as the agent loop does under a lazy provider
    # (which exposes no api_base) -- must fall back to config, not refuse.
    tool = OpsTuneLaunchTool(llm_model="qwen3.6-27B")
    out = await tool.execute(host="1.2.3.4")

    argv = captured["argv"]
    assert argv[argv.index("--llm-base-url") + 1] == "https://gw/v1"
    assert argv[argv.index("--llm-model") + 1] == "qwen3.6-27B"
    assert "Launched" in out


async def test_status_missing_ledger_is_graceful() -> None:
    out = await OpsTuneStatusTool().execute(ledger="/nonexistent/ledger.json")
    assert "no campaign ledger" in out.lower()


async def test_status_reconciles_pending_against_remote(monkeypatch, tmp_path: Path) -> None:
    # A pending trial whose container already finished on the host: status must
    # poll the remote, pick up result.json (container reaped), and report it done.
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "version": 1,
        "records": {
            "b0p6_k11p5": {
                "idem_key": "b0p6_k11p5", "status": "pending", "campaign": "bm25",
                "handle": {"backend": "docker", "job_id": "ops-b0p6_k11p5"},
                "result": None, "attempts": 1, "escalated": False,
            }
        },
    }), encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps(
        {"host": "h", "port": 22, "key": "~/.ssh/id_rsa", "remote_dir": "/root/raven-ops", "image": "img"}
    ), encoding="utf-8")

    def fake_runner(cmd: str):
        if cmd.startswith("docker inspect"):
            return 1, "no such container"  # reaped after exit
        if cmd.startswith("test -f") and "b0p6_k11p5/result.json" in cmd:
            return 0, ""
        if cmd.startswith("cat ") and "b0p6_k11p5/result.json" in cmd:
            return 0, json.dumps({"metrics": {"ndcg": 0.3078}, "config": {"k1": 1.5, "b": 0.6}})
        return 0, ""

    monkeypatch.setattr(ops_pkg, "make_ssh_runner", lambda *a, **k: fake_runner)

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert "ndcg=0.3078" in out  # reconciled from the remote result.json
    assert "succeeded=1" in out
    # the reconcile was persisted back to the ledger
    assert json.loads(ledger.read_text())["records"]["b0p6_k11p5"]["status"] == "succeeded"


async def test_status_reports_best_and_progress(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    payload = {
        "version": 1,
        "records": {
            "k1p0p8_b0p4": {
                "idem_key": "k1p0p8_b0p4", "status": "succeeded", "campaign": "bm25_tune",
                "handle": None, "attempts": 1, "escalated": False,
                "result": {"status": "succeeded", "metrics": {"ndcg": 0.28},
                           "output": {"config": {"k1": 0.8, "b": 0.4}}, "error": None},
            },
            "k1p1p2_b0p6": {
                "idem_key": "k1p1p2_b0p6", "status": "succeeded", "campaign": "bm25_tune",
                "handle": None, "attempts": 1, "escalated": False,
                "result": {"status": "succeeded", "metrics": {"ndcg": 0.31},
                           "output": {"config": {"k1": 1.2, "b": 0.6}}, "error": None},
            },
            "k1p2p0_b0p9": {
                "idem_key": "k1p2p0_b0p9", "status": "running", "campaign": "bm25_tune",
                "handle": None, "result": None, "attempts": 1, "escalated": False,
            },
        },
    }
    ledger.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "run.log").write_text("round 1 done\nround 2 proposing\n", encoding="utf-8")

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert "in progress" in out
    # A trial now prints its config as key=value rather than a Python dict repr:
    # the config comes from the ledger instead of from whatever the job echoed,
    # so it reads the same whether or not the job wrote one back.
    assert "ndcg=0.31" in out and "k1=1.2" in out  # best of the two succeeded
    assert "round 2 proposing" in out  # log tail surfaced


async def test_status_states_the_compute_budget_as_a_fact(tmp_path: Path) -> None:
    """The total was invisible here: the job writes it once in its opening progress
    sample and this tool prints only the latest one, so a loop waking mid-run saw
    elapsed time with nothing to measure it against. The remainder is deliberately
    not computed -- deciding when to look again is the judgement being measured."""
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "version": 1,
        "records": {
            "t1": {
                "idem_key": "t1", "status": "running", "campaign": "c",
                "handle": {"backend": "process", "job_id": "ops-t1"},
                "result": None, "attempts": 1, "escalated": False,
            }
        },
    }), encoding="utf-8")
    (tmp_path / "meta.json").write_text(
        json.dumps({"backend": "process", "host": "h", "budget_minutes_total": 90}), encoding="utf-8"
    )

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert "Campaign compute budget: 90 gpu-minute in total." in out
    assert "remaining" not in out.lower()


async def test_status_says_so_when_the_campaign_declares_no_budget(tmp_path: Path) -> None:
    """This reverses an earlier decision here, which was to print nothing.

    An absent operating policy prints no heading, and that is still right: the
    operator declared no rules and nothing follows from it. An absent budget is
    not the same kind of absence. It means no total will be enforced -- submits
    are not refused and requests are not clamped -- and that changes what the loop
    may do. Printing nothing left the two cases looking identical: "no line about
    budget" and "nothing will stop this run" read the same on screen, and the
    second is the one where the number on the next line is the only thing that
    can show a run has gone long.
    """
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "version": 1,
        "records": {
            "t1": {
                "idem_key": "t1", "status": "running", "campaign": "c",
                "handle": {"backend": "process", "job_id": "ops-t1"},
                "result": None, "attempts": 1, "escalated": False,
            }
        },
    }), encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps({"backend": "process", "host": "h"}), encoding="utf-8")

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert "none declared" in out


def test_latest_with_metric_picks_the_newest_carrying_the_metric() -> None:
    """The newest progress line is an evaluation reading only about 9% of the time
    (measured 23 of 253 on the real job), and loss alone does not separate a good
    configuration from a bad one -- so one reading per look has to be guaranteed."""
    from raven.agent.tools.ops import _latest_with_metric

    samples = [
        {"step": 200, "eval_ndcg": 0.3059, "elapsed_s": 240.0},
        {"step": 220, "loss": 0.12, "elapsed_s": 262.0},
        {"step": 400, "eval_ndcg": 0.3046, "elapsed_s": 478.0},
        {"step": 420, "loss": 0.09, "elapsed_s": 500.0},
    ]
    assert _latest_with_metric(samples, "ndcg")["step"] == 400
    assert _latest_with_metric(samples[1:2], "ndcg") is None
    assert _latest_with_metric([], "ndcg") is None


def test_latest_with_metric_ignores_booleans_and_non_numbers() -> None:
    from raven.agent.tools.ops import _latest_with_metric

    assert _latest_with_metric([{"eval_ndcg_ready": True}], "ndcg") is None
    assert _latest_with_metric([{"eval_ndcg": "n/a"}], "ndcg") is None


async def test_status_prints_both_series_as_logged(monkeypatch, tmp_path: Path) -> None:
    """One point per look cannot answer "has it moved since the last look".

    The measured incident: over a 90 minute run the loop looked three times and
    each look printed exactly two samples -- the newest line and the newest line
    carrying the metric -- while the fetch had already pulled 25, 36 and 36 loss
    samples and 2, 4 and 4 evaluation samples. So "below the starting value" was
    available every time and "still not moving after another 600 steps" never
    was, and the loop waited three times. Training loss collapsing two orders of
    magnitude while the evaluation reading does not improve is the signature the
    decision needed, and it exists only across the two series.

    Chronological and unreduced on purpose: supplying the series is supplying
    operands. A trend word, a ranking, or a difference would make the comparison
    the loop is being scored on.
    """
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "version": 1,
        "records": {
            "t1": {
                "idem_key": "t1", "status": "running", "campaign": "c",
                "handle": {"backend": "process", "job_id": "ops-t1"},
                "result": None, "attempts": 1, "escalated": False,
            }
        },
    }), encoding="utf-8")
    (tmp_path / "meta.json").write_text(
        json.dumps({"backend": "process", "host": "h", "remote_dir": "/r", "command": "c"}), encoding="utf-8"
    )

    samples = [
        {"step": 200, "eval_ndcg": 0.3059, "elapsed_s": 240.0},
        {"step": 220, "loss": 0.1336, "elapsed_s": 262.0},
        {"step": 400, "eval_ndcg": 0.3046, "elapsed_s": 478.0},
        {"step": 420, "loss": 0.0904, "elapsed_s": 500.0},
        {"step": 600, "eval_ndcg": 0.2980, "elapsed_s": 716.0},
        {"step": 620, "loss": 0.0247, "elapsed_s": 738.0},
    ]

    class _Backend:
        name = "process"

        async def fetch_progress(self, handle, tail: int = 40):
            return samples

        async def poll(self, handle):
            from raven.ops.backend import JobStatus

            return JobStatus.RUNNING

    from raven.ops import backends as ops_backends

    monkeypatch.setattr(ops_backends, "backend_from_meta", lambda meta: _Backend())

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    # every evaluation reading in the window, oldest first
    assert "200:0.3059" in out and "400:0.3046" in out and "600:0.298" in out
    # every loss reading in the window, oldest first
    assert "220:0.1336" in out and "420:0.0904" in out and "620:0.0247" in out
    # order is chronological, not by value
    assert out.index("200:0.3059") < out.index("400:0.3046") < out.index("600:0.298")
    # no comparison is made for the caller
    for word in ("declin", "improv", "trend", "worse", "better", "best", "below", "above", "delta"):
        assert word not in out.lower()


async def test_status_series_omitted_when_the_field_never_appears(monkeypatch, tmp_path: Path) -> None:
    """A field the job never logs gets no line at all, rather than an empty one:
    an empty series reads as "measured, and there is nothing there"."""
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "version": 1,
        "records": {
            "t1": {
                "idem_key": "t1", "status": "running", "campaign": "c",
                "handle": {"backend": "process", "job_id": "ops-t1"},
                "result": None, "attempts": 1, "escalated": False,
            }
        },
    }), encoding="utf-8")
    (tmp_path / "meta.json").write_text(
        json.dumps({"backend": "process", "host": "h", "remote_dir": "/r", "command": "c"}), encoding="utf-8"
    )

    class _Backend:
        name = "process"

        async def fetch_progress(self, handle, tail: int = 40):
            return [{"step": 20, "loss": 0.5, "elapsed_s": 25.0}]

        async def poll(self, handle):
            from raven.ops.backend import JobStatus

            return JobStatus.RUNNING

    from raven.ops import backends as ops_backends

    monkeypatch.setattr(ops_backends, "backend_from_meta", lambda meta: _Backend())

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert "loss, as logged" in out
    assert "ndcg, as logged" not in out


def test_as_logged_series_is_chronological_and_bounded() -> None:
    from raven.agent.tools.ops import _as_logged_series

    samples = [{"step": s, "eval_ndcg": 0.3 + s / 10000} for s in range(20, 401, 20)]
    line = _as_logged_series(samples, "ndcg", limit=4)

    assert line is not None
    picked = line.split()
    # evenly spaced across the whole run, not the tail: keeping only the newest
    # `limit` readings would cover one noise band and hide the change
    assert picked[0].startswith("20:") and picked[-2].startswith("400:")
    assert picked[-1] == "(sampled)"
    assert len(picked) == 5
    steps = [int(p.split(":")[0]) for p in picked[:-1]]
    assert steps == sorted(steps)

    # under the limit, every reading, and no "sampled" note
    short = _as_logged_series(samples[:3], "ndcg", limit=4)
    assert short is not None and "sampled" not in short and len(short.split()) == 3

    assert _as_logged_series([{"step": 1, "loss": "n/a"}], "loss") is None
    assert _as_logged_series([], "loss") is None


async def test_recorded_readings_match_what_the_series_printed(monkeypatch, tmp_path: Path) -> None:
    """The basis check accepts a decision whose cited number is among the readings
    the probe recorded. If the probe recorded more readings than the series
    printed, a number the loop was never shown would pass -- so the thinning has
    to be the same on both sides, not merely similar."""
    from raven.ops.state_claims import read_facts

    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "version": 1,
        "records": {
            "t1": {
                "idem_key": "t1", "status": "running", "campaign": "c",
                "handle": {"backend": "process", "job_id": "ops-t1"},
                "result": None, "attempts": 1, "escalated": False,
            }
        },
    }), encoding="utf-8")
    (tmp_path / "meta.json").write_text(
        json.dumps({"backend": "process", "host": "h", "remote_dir": "/r", "command": "c"}), encoding="utf-8"
    )

    # more evaluation readings than the series prints, so thinning is exercised
    samples = [{"step": s, "eval_ndcg": 0.30 + (s % 7) / 1000} for s in range(200, 8001, 200)]

    class _Backend:
        name = "process"

        async def fetch_progress(self, handle, tail: int = 40):
            return samples

        async def poll(self, handle):
            from raven.ops.backend import JobStatus

            return JobStatus.RUNNING

    from raven.ops import backends as ops_backends

    monkeypatch.setattr(ops_backends, "backend_from_meta", lambda meta: _Backend())

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    printed = set()
    for line in out.splitlines():
        if "ndcg, as logged:" in line:
            printed = {p.split(":")[1] for p in line.split(":", 1)[1].split() if ":" in p}
    assert printed, out

    recorded = read_facts(tmp_path).metric_readings.get("ndcg", ())
    assert {str(v) for v in recorded} <= printed


async def test_status_prints_the_campaigns_operating_policy(monkeypatch, tmp_path: Path) -> None:
    """A wake turn starts cold from disk: no conversation, no history. So domain
    procedure stated in the task text reaches only the first turn and is absent
    from every turn where a decision is actually made -- measured 2026-08-06,
    where a loop woke six times and each time re-derived the situation from
    scratch, its basis identical in structure every time.

    The other two delivery routes do not close this. A skill has to survive an
    LLM relevance gate whose empty answer is legal: measured injection rate was
    0 of 2 on wake turns, 2 of 9 overall, and the failure is silent. The system
    prompt is global, so it cannot hold one procedure per domain.

    Campaign meta is the one place that is per-campaign AND reprinted verbatim on
    every look -- the budget total and the starting value already travel this way.

    Printed verbatim, in the configured order. Not summarised and not reordered:
    the same rule the metric series and the decision record follow.
    """
    cdir = tmp_path
    _write_running_ledger(cdir)
    (cdir / "meta.json").write_text(
        json.dumps(
            {
                "backend": "process",
                "host": "h",
                "operating_policy": {
                    "hard_triggers": [
                        "NaN or Inf in the loss: stop, do not wait.",
                        "A physical property off by an order of magnitude: fix the property first.",
                    ],
                    "exemplars": [
                        {
                            "saw": "step 400 ndcg=0.3046, starting value 0.3674, 10 of 140 minutes used",
                            "thought": "17% below on the first reading; this kind of fine-tune shows its "
                            "direction inside the first 20% of steps, so waiting 90 more minutes mostly "
                            "confirms a bad result.",
                            "did": "killed it, resubmitted at a quarter of the learning rate, asked for 30 minutes",
                        },
                        {
                            "saw": "0.3474 -> 0.3586 -> 0.3604 -> 0.3607, starting value 0.3674",
                            "thought": "low, but climbing, and only 1.8% short. Killing this throws away a "
                            "run that is converging.",
                            "did": "changed nothing, looked again 20 minutes later",
                        },
                    ],
                    "playbook_skill": "oncall-embed",
                },
            }
        ),
        encoding="utf-8",
    )

    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"), metric="ndcg")

    assert "Campaign operating policy (as configured):" in out
    # hard triggers, verbatim
    assert "NaN or Inf in the loss: stop, do not wait." in out
    # the exemplar that ACTS
    assert "resubmitted at a quarter of the learning rate" in out
    # the exemplar that WAITS -- without it the policy reads as "always act", and a
    # loop that always kills is broken in the mirror direction
    assert "changed nothing, looked again 20 minutes later" in out
    assert "0.3474 -> 0.3586 -> 0.3604 -> 0.3607" in out
    # where the remedies live, named so looking them up is an instruction
    assert "oncall-embed" in out
    # order preserved, no reordering
    assert out.index("step 400 ndcg=0.3046") < out.index("0.3474 -> 0.3586")
    # no conclusion drawn for the caller
    for word in ("you should", "recommend", "therefore", "conclusion"):
        assert word not in out.lower()


async def test_status_says_so_when_no_policy_is_configured(tmp_path: Path) -> None:
    """Absent must not read as satisfied. A campaign with no procedure has to say
    it has none, or "nobody configured one" and "the procedure was followed" look
    the same -- the shape that let a report gate print Accepted while reading a
    directory with nothing in it (2026-08-06)."""
    cdir = tmp_path
    _write_running_ledger(cdir)
    (cdir / "meta.json").write_text(json.dumps({"backend": "process", "host": "h"}), encoding="utf-8")

    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"), metric="ndcg")

    assert "Campaign operating policy: (none configured)" in out


async def test_policy_accepts_a_plain_list_too(tmp_path: Path) -> None:
    """A campaign that only has a few rules should not have to wrap them in the
    full structure; a bare list is read as hard triggers."""
    cdir = tmp_path
    _write_running_ledger(cdir)
    (cdir / "meta.json").write_text(
        json.dumps({"backend": "process", "host": "h", "operating_policy": ["look every 10 minutes"]}),
        encoding="utf-8",
    )

    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"), metric="ndcg")

    assert "look every 10 minutes" in out
    assert "(none configured)" not in out


async def test_policy_and_budget_print_before_the_first_submit(tmp_path: Path) -> None:
    """The moment the policy matters most is before anything has been submitted --
    that is when the loop decides how much of the budget to ask for and whether to
    run the seed config as given.

    Measured 2026-08-07: with only meta.json present, this tool returned a single
    "no ledger yet" line and dropped the budget, the starting value and the whole
    operating policy on the floor, because the early return sits above the code
    that prints them. The arm then spent six exec/ssh calls hunting for the port and
    the budget it had just been denied.
    """
    cdir = tmp_path
    (cdir / "meta.json").write_text(
        json.dumps(
            {
                "backend": "process",
                "host": "h",
                "budget_minutes_total": 140,
                "reference_values": {"ndcg": 0.3674},
                "seed_config": {"lr": 2e-5},
                "operating_policy": {"hard_triggers": ["NaN: stop, do not wait."]},
            }
        ),
        encoding="utf-8",
    )

    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"), metric="ndcg")

    # it still says the run has not started
    assert "not started" in out.lower() or "yet" in out.lower()
    # and it hands over everything the campaign holds
    assert "140 gpu-minute in total" in out
    assert "ndcg = 0.3674" in out
    assert "NaN: stop, do not wait." in out
    assert "declared starting config" in out and '"lr"' in out


def _ledger_with(tmp_path: Path, deliverable) -> Path:
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "version": 1,
        "records": {
            "lr1em06": {
                "idem_key": "lr1em06", "status": "succeeded", "campaign": "c",
                "handle": None, "attempts": 1, "escalated": False,
                "result": {
                    "status": "succeeded", "metrics": {"ndcg": 0.3564},
                    "output": {"config": {"lr": 1e-6},
                               "eval_points": [[1200, 0.362], [1518, 0.3564]]},
                    "error": None, "deliverable": deliverable,
                },
            },
        },
    }), encoding="utf-8")
    return ledger


async def test_a_finished_trial_names_what_it_would_hand_over(tmp_path: Path) -> None:
    """The number a report calls "best" was unanswerable from this output: a finished
    record collapsed to the value the run ended on, and the curve it came from was
    only ever rendered while the job was still running. Measured 2026-08-07: r13
    probed 31 minutes after submitting a 30-minute job, four times out of four, so
    that path never once fired -- and that loop shape (submit, ask for 30 minutes,
    come back when it is done) is the shape we want it to have.

    The wording carries no domain vocabulary. The tool prints the ref and the label
    the backend gave it; it does not know that a checkpoint is a checkpoint or that
    higher is better.
    """
    ledger = _ledger_with(
        tmp_path, {"ref": "/w/jobs/lr1em06/step-1200", "label": "ndcg", "value": 0.362}
    )

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert "/w/jobs/lr1em06/step-1200" in out
    assert "0.362" in out
    assert "ndcg=0.3564" in out, "the value the run ended on must still be there"


async def test_a_finished_trial_without_a_deliverable_prints_no_extra_line(tmp_path: Path) -> None:
    """A backend that cannot name a deliverable says nothing, and the tool stays quiet
    with it. Same rule as everywhere else here: a fact the harness does not hold
    changes no output. A transient CFD run has no best moment inside it."""
    ledger = _ledger_with(tmp_path, None)

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert "0.362" not in out
    assert "ndcg=0.3564" in out


async def test_the_metric_name_comes_from_the_campaign_not_from_a_default(tmp_path: Path) -> None:
    """"ndcg" was the default in five tool signatures, and this tool layer is shared
    with every domain -- a CFD campaign had to pass the metric on every single call or
    read back nothing. The campaign already declares what it optimises; the tool
    should ask the campaign, not assume an embedding benchmark.
    """
    (tmp_path / "meta.json").write_text(
        json.dumps({"backend": "process", "objective": {"metric": "residual", "direction": "min"}}),
        encoding="utf-8",
    )
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "version": 1,
        "records": {
            "run1": {
                "idem_key": "run1", "status": "succeeded", "campaign": "c",
                "handle": None, "attempts": 1, "escalated": False,
                "result": {"status": "succeeded", "metrics": {"residual": 0.004},
                           "output": {"config": {"nuWater": "1e-3"}}, "error": None,
                           "deliverable": None},
            },
        },
    }), encoding="utf-8")

    out = await OpsTuneStatusTool().execute(ledger=str(ledger))

    assert "residual=0.004" in out
    assert "none reported metric 'ndcg'" not in out.lower()


async def test_without_a_declared_objective_the_old_default_still_reads(tmp_path: Path) -> None:
    """Campaigns created before ``objective`` existed keep working; r13 and r14 are on
    disk and their reports have to stay readable."""
    (tmp_path / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "version": 1,
        "records": {
            "run1": {
                "idem_key": "run1", "status": "succeeded", "campaign": "c",
                "handle": None, "attempts": 1, "escalated": False,
                "result": {"status": "succeeded", "metrics": {"ndcg": 0.3579},
                           "output": {"config": {"lr": 5e-7}}, "error": None, "deliverable": None},
            },
        },
    }), encoding="utf-8")

    out = await OpsTuneStatusTool().execute(ledger=str(ledger))

    assert "ndcg=0.3579" in out
