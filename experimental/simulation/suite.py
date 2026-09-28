"""Mine for the value case: run cultivation runs over disclosure plans within a budget, judge each, rank them.

A candidate is one disclosure plan: a chain preset, a partition given as JSON, or a random partition of the scenario's
materials (the owner still knows everything from the start and has handed everything over before the last round; only
the partition differs, see `experimental.simulation.__main__`). Each finished run is judged by
`experimental.simulation.value`; with `--stop-on-value` the first run that qualifies stops the others.

Each run gets its own Raven home under `--state-root` (the only place a configuration with credentials is written,
readable by its owner only) and its own record directory under `--runs-dir`. While runs go, their spend is read from
the model calls their Raven homes record; when the suite's total passes `--budget`, the running runs are stopped. After
a run ends, credentials are removed from the configuration copies the worker left in its records, and everything the
run left in its Raven home except that configuration is copied beside the records: the traces, the employee's working
directory (tickets, briefs, research, deck projects), the run log and the command line, so a run's records hold its
whole process. Then the cultivation record is exported and judged, and a summary is written.
"""

import argparse
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .__main__ import CHAINS
from .employee import FORWARDED
from .record import REDACTED, SUMMARY, build_record, export_record, redact
from .scenario import Scenario

KEPT_FILES = ("run.log", "command.json")
VALUE = "value.json"


# USD per million tokens (fresh input, output, cache-read input) for models whose provider reports no cost.
PRICES = {"deepseek/deepseek-flash": (0.30, 1.20, 0.006)}


def call_cost(attributes: dict, prices=PRICES) -> float:
    """A model call's cost: what the provider reported, else its tokens at the model's price (0 when unpriced)."""
    try:
        reported = float(attributes.get("llm.usage.cost_total") or 0)
    except (TypeError, ValueError):
        reported = 0.0
    model = str(attributes.get("llm.model") or "")
    price = prices.get(model) or next((p for name, p in prices.items() if name.split("/")[-1] == model), None)
    if reported or not price:
        return reported
    tokens = [attributes.get(f"llm.usage.{key}") or 0 for key in ("input_tokens", "output_tokens", "cache_read_tokens")]
    return sum(float(count) * rate for count, rate in zip(tokens, price, strict=True)) / 1_000_000


def spend(state: Path, records: Path | None = None, prices=PRICES) -> dict[str, float]:
    """Model spend of one run from the calls its Raven home recorded, split by who made them.

    A child harness hosted beside the employee or one of its replicas keeps its own log under the run's records
    (`children/<id>/native/traces/logs`, or under `replicas/<label>/`); every call there counts as a subagent's.
    Replicas' own calls land in the run's Raven home with the employee's; once the home is gone, its logs are read
    from the copy the records keep under `traces/`.
    """
    parts = {"simulation": 0.0, "employee": 0.0, "subagents": 0.0}
    logs = [(log, None) for log in (Path(state) / "traces" / "logs").glob("audit-spans*.log*")]
    if not logs and records is not None:
        logs = [(log, None) for log in (Path(records) / "traces").glob("audit-spans*.log*")]
    if records is not None:
        for owner in ("", "replicas/*/"):
            found = Path(records).glob(f"{owner}children/*/native/traces/logs/audit-spans*.log*")
            logs += [(log, "subagents") for log in found]
    for log, fixed in logs:
        for line in log.open(errors="replace"):
            if '"llm.call"' not in line:
                continue
            try:
                attributes = json.loads(line).get("attributes", {})
            except ValueError:
                continue
            session = str(attributes.get("session.id") or "")
            session = "" if session in ("None", "null") else session
            who = fixed or ("subagents" if session.startswith("acp:") else ("employee" if session else "simulation"))
            parts[who] += call_cost(attributes, prices)
    return {**parts, "total": sum(parts.values())}


def _scrub(value) -> tuple[object, int]:
    if isinstance(value, dict):
        count = 0
        out = {}
        for key, item in value.items():
            if (
                isinstance(item, str)
                and item
                and (key.lower() in ("apikey", "api_key") or key.endswith("_API_KEY") or redact(item) != item)
            ):
                out[key], count = REDACTED, count + 1
            else:
                out[key], found = _scrub(item)
                count += found
        return out, count
    if isinstance(value, list):
        pairs = [_scrub(item) for item in value]
        return [item for item, _ in pairs], sum(found for _, found in pairs)
    return value, 0


CONFIG_COPIES = ("config.json", "deployment.json", ".config.rendered.*.json")


def scrub(records: Path) -> int:
    """Redact credentials in every configuration copy the worker wrote anywhere under a run's records (the root and
    each child harness keep their own, down to version folders); returns how many."""
    total = 0
    for pattern in CONFIG_COPIES:
        for path in Path(records).rglob(pattern):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                data, count = _scrub(json.loads(path.read_text()))
            except ValueError:
                continue
            if not count:
                continue
            mode = path.stat().st_mode & 0o777
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            os.chmod(path, mode)
            total += count
    return total


def random_partition(materials: list[str], rounds: int, rng: random.Random, onboard=()) -> list[list[str]]:
    """A partition of every material into 2 to `rounds` non-empty steps, each material in its scenario order.

    Materials in `onboard` always come at onboarding, so every round can reach the stages they serve.
    """
    rest = [name for name in materials if name not in onboard]
    if not rest:
        raise ValueError("a partition needs a material outside the onboarding set")
    while True:
        count = rng.randint(2, max(2, min(rounds, len(rest) + 1)))
        places = {name: 0 if name in onboard else rng.randrange(count) for name in materials}
        steps = [[name for name in materials if places[name] == step] for step in range(count)]
        if all(steps):
            return steps


def candidates(args) -> list[tuple[str, list[str]]]:
    """Each disclosure plan to try, as a label and the simulation arguments that select it."""
    found = [(chain, ["--chain", chain]) for chain in args.chains or ()]
    given = json.loads(Path(args.partitions).read_text()) if args.partitions else []
    found += [
        (f"p{index}", ["--partition", json.dumps(steps, ensure_ascii=False)]) for index, steps in enumerate(given, 1)
    ]
    if args.random_partitions:
        scenario = Scenario.load(args.scenario)
        materials = list(
            scenario.without(args.without).materials if getattr(args, "without", None) else scenario.materials
        )
        onboard = list(getattr(args, "onboard", None) or ())
        unknown = set(onboard) - set(materials)
        if unknown:
            raise ValueError(f"the scenario has no materials named {sorted(unknown)}")
        rng = random.Random(args.seed)
        for index in range(1, args.random_partitions + 1):
            steps = random_partition(materials, args.rounds, rng, onboard)
            found.append((f"r{args.seed}-{index}", ["--partition", json.dumps(steps, ensure_ascii=False)]))
    return found


def command(plan: list[str], args, *, config: Path, workdir: Path, records: Path) -> list[str]:
    """The simulation command line for one plan; every model and size setting is explicit so the run is reproducible."""
    argv = [sys.executable, "-m", "experimental.simulation", "--config", str(config), "--workdir", str(workdir)]
    argv += ["--home", str(args.home), "--state-dir", str(records), "--scenario", str(args.scenario), *plan]
    argv += ["--rounds", str(args.rounds), "--turns", str(args.turns), "--repeats", str(args.repeats)]
    argv += ["--timeout", str(args.timeout)]
    for flag, value in (
        ("--curator-model", args.curator_model),
        ("--analysis", getattr(args, "analysis", None)),
        ("--analyst-model", args.analyst_model),
        ("--simulation-model", args.simulation_model),
        ("--traveller-model", args.traveller_model),
        ("--curator-calls", getattr(args, "curator_calls", None)),
        ("--curator-queries", getattr(args, "curator_queries", None)),
        ("--subagent-model", args.subagent_model),
        ("--curator-effort", getattr(args, "curator_effort", None)),
        ("--traveller-effort", getattr(args, "traveller_effort", None)),
    ):
        if value:
            argv += [flag, str(value)]
    if args.cards:
        argv += ["--cards", *args.cards]
    if getattr(args, "concurrent_drills", 1) > 1:
        argv += ["--concurrent-drills", str(args.concurrent_drills)]
    if getattr(args, "without", None):
        argv += ["--without", *args.without]
    return [*argv, "--seed", str(args.seed)]


@dataclass
class Run:
    label: str
    plan: list[str]
    name: str
    state: Path
    records: Path
    process: subprocess.Popen | None = None
    started: float = 0.0
    ended: float | None = None
    stopped: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def log(self) -> Path:
        return self.state / "run.log"


def start(run: Run, args) -> None:
    run.state.mkdir(parents=True, exist_ok=False)
    (run.state / "workdir").mkdir()
    config = run.state / "config.json"
    shutil.copyfile(args.config, config)
    os.chmod(config, 0o600)
    env = {**os.environ, "RAVEN_HOME": str(run.state)}
    env.update({name: os.environ[name] for name in FORWARDED if os.environ.get(name)})
    argv = command(run.plan, args, config=config, workdir=run.state / "workdir", records=run.records)
    (run.state / "command.json").write_text(json.dumps(argv[1:], indent=2))
    with run.log.open("w") as log:
        run.process = subprocess.Popen(
            argv, cwd=args.repository, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
    run.started = time.time()


def descendants(pid: int) -> list[int]:
    """Every process below `pid`, read from /proc: subagents start their own sessions, so a process group misses them."""
    children: dict[int, list[int]] = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        children.setdefault(int(fields[1]), []).append(int(stat.parent.name))
    found, queue = [], [pid]
    while queue:
        for child in children.get(queue.pop(), []):
            found.append(child)
            queue.append(child)
    return found


def _signal(pids, sig) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def stop(run: Run, reason: str, grace: float = 20) -> None:
    """Stop a run's process group and every process below it, worker and subagents included; say why in its record."""
    if run.process is None or run.process.poll() is not None:
        return
    run.stopped = reason
    below = descendants(run.process.pid)
    try:
        os.killpg(run.process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    _signal(below, signal.SIGTERM)
    try:
        run.process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(run.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _signal([pid for pid in below if Path(f"/proc/{pid}").exists()], signal.SIGKILL)
    for record in (run.records / "iteration").glob("*.json"):
        data = json.loads(record.read_text())
        if data.get("status") == "running":
            data["status"], data["error"] = "error", f"stopped by the suite: {reason}"
            record.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def finish(run: Run, args) -> dict:
    """Leave a run's records with its evidence and without credentials; returns its summary."""
    run.ended = run.ended or time.time()
    redacted = scrub(run.records) if run.records.is_dir() else 0
    traces, workdir = run.state / "traces" / "logs", run.state / "workdir"
    if run.records.is_dir():
        if traces.is_dir():
            shutil.copytree(traces, run.records / "traces", dirs_exist_ok=True)
        if workdir.is_dir():
            shutil.copytree(workdir, run.records / "workdir", dirs_exist_ok=True, symlinks=True)
        for name in KEPT_FILES:
            if (run.state / name).is_file():
                text = (run.state / name).read_text(errors="replace")
                (run.records / name).write_text(redact(text))
    isolation = None
    try:
        from .isolation import write as check_isolation

        isolation = check_isolation(run.records, run.state, args.forbid) if run.records.is_dir() else None
    except Exception as exc:  # noqa: BLE001 -- an unscanned run is judged not isolated, never lost
        run.notes.append(f"isolation scan failed: {exc!r}")
    from .files import page_images

    decks = sorted(run.records.rglob("*.pptx")) if run.records.is_dir() else []
    for deck in (deck for deck in decks if "deliverables" in deck.relative_to(run.records).parts):
        try:
            page_images(deck)
        except Exception as exc:  # noqa: BLE001 -- a deck without page images is still recorded
            run.notes.append(f"no page images for {deck.name}: {exc!r}")
    exported = None
    try:
        exported = str(export_record(run.records, run.records / "record", state_root=run.state.parent))
    except Exception as exc:  # noqa: BLE001 -- a failed export must not lose the run's summary
        run.notes.append(f"record export failed: {exc!r}")
    value = None
    try:
        value = build_record(run.records, state_root=run.state.parent)["value"]
        (run.records / VALUE).write_text(json.dumps(value, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001 -- an unjudged run still gets its summary
        run.notes.append(f"value judgement failed: {exc!r}")
    status, rounds = "unknown", 0
    for record in (run.records / "iteration").glob("*.json"):
        data = json.loads(record.read_text())
        status, rounds = data.get("status", "unknown"), len(data.get("rounds") or [])
    summary = {
        "label": run.label,
        "plan": run.plan,
        "name": run.name,
        "status": status,
        "rounds": rounds,
        "stopped": run.stopped,
        "exit": run.process.returncode if run.process else None,
        "seconds": round(run.ended - run.started),
        "spend": spend(run.state, run.records),
        "credentials_redacted": redacted,
        "record": exported,
        "value": {key: value[key] for key in ("verdict", "score")}
        | {"cultivated": len(value.get("cultivated", [])), "cases": len(value["cases"])}
        if value
        else None,
        "isolation": {key: isolation[key] for key in ("valid", "breaches")} if isolation else None,
        "notes": run.notes,
    }
    if run.records.is_dir():
        (run.records / SUMMARY).write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def over_budget(runs: list[Run], budget: float | None) -> bool:
    return budget is not None and sum(spend(run.state, run.records)["total"] for run in runs if run.started) >= budget


def main(args) -> list[dict]:
    stamp = time.strftime("%m%d-%H%M")
    runs = [
        Run(
            label,
            plan,
            f"{args.prefix}{label}-{stamp}",
            args.state_root / f"{args.prefix}{label}-{stamp}",
            args.runs_dir / f"{args.prefix}{label}-{stamp}",
        )
        for label, plan in candidates(args)
    ]
    if not runs:
        raise ValueError("name at least one chain, partition file or random partition")
    for run in runs:
        if run.records.exists() or run.state.exists():
            raise ValueError(f"a run named {run.name} already exists")
    pending, active, done = list(runs), [], []
    while pending or active:
        while pending and len(active) < args.parallel and not over_budget(runs, args.budget):
            run = pending.pop(0)
            start(run, args)
            active.append(run)
            print(f"started {run.name}: records {run.records}", flush=True)
        if over_budget(runs, args.budget):
            for run in active:
                stop(run, f"the suite reached its budget of ${args.budget}")
            pending.clear()
        for run in list(active):
            if run.process.poll() is not None:
                active.remove(run)
                summary = finish(run, args)
                done.append(summary)
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                if args.stop_on_value and (summary["value"] or {}).get("verdict") == "qualifies":
                    for other in active:
                        stop(other, f"{run.name} already showed the value case")
                    pending.clear()
        time.sleep(args.poll)
    ranking = sorted(done, key=lambda summary: -((summary["value"] or {}).get("score") or 0))
    total = sum(summary["spend"]["total"] for summary in done)
    (args.runs_dir / f"{args.prefix}mining-{stamp}.json").write_text(
        json.dumps({"spend": total, "ranking": ranking}, ensure_ascii=False, indent=2)
    )
    print(f"mining finished: {len(done)} runs, ${total:.2f}", flush=True)
    return ranking


def cli():
    parser = argparse.ArgumentParser(description="Mine for the value case over disclosure plans within a budget.")
    parser.add_argument("--config", type=Path, required=True, help="Raven JSON configuration with provider credentials")
    parser.add_argument("--home", type=Path, required=True, help="The employee's baseline agent home")
    parser.add_argument("--runs-dir", type=Path, required=True, help="Where each run's record directory is created")
    parser.add_argument("--state-root", type=Path, required=True, help="Where each run's Raven home is created")
    parser.add_argument("--scenario", type=Path, default=Path("travel_agency"))
    parser.add_argument("--chains", nargs="+", choices=sorted(CHAINS), default=[])
    parser.add_argument("--partitions", type=Path, help="A JSON file holding a list of partitions to try")
    parser.add_argument("--random-partitions", type=int, default=0, help="How many random partitions to try")
    parser.add_argument(
        "--seed", type=int, default=1, help="Seed of the random partitions and of the values drawn for the drill cards"
    )
    parser.add_argument("--without", nargs="+", help="Leave these materials out of the scenario in every run")
    parser.add_argument(
        "--onboard",
        nargs="+",
        help="Materials every random partition gives at onboarding; the rest are staged at random",
    )
    parser.add_argument("--stop-on-value", action="store_true", help="Stop the other runs once one qualifies")
    parser.add_argument("--prefix", default="", help="Prefix for run names")
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--turns", type=int, default=14)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--cards", nargs="+")
    parser.add_argument("--concurrent-drills", type=int, default=1, help="Drills each run plays at once in a round")
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--curator-model")
    parser.add_argument("--analysis", choices=("owner", "analyst"))
    parser.add_argument("--analyst-model")
    parser.add_argument("--simulation-model")
    parser.add_argument("--traveller-model")
    parser.add_argument("--curator-calls", type=int)
    parser.add_argument("--curator-queries", type=int)
    parser.add_argument("--subagent-model")
    parser.add_argument("--curator-effort", choices=("low", "medium", "high"))
    parser.add_argument("--traveller-effort", choices=("low", "medium", "high"))
    parser.add_argument(
        "--budget", type=float, help="Stop every running run once the suite has spent this many dollars"
    )
    parser.add_argument(
        "--forbid", type=Path, nargs="*", default=[], help="Places the employee must not reach (see isolation.py)"
    )
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--poll", type=float, default=60)
    args = parser.parse_args()
    args.config = args.config.expanduser().resolve()
    args.home, args.runs_dir, args.state_root = (
        path.expanduser().resolve() for path in (args.home, args.runs_dir, args.state_root)
    )
    args.repository = Path(__file__).resolve().parents[2]
    main(args)


if __name__ == "__main__":
    cli()
