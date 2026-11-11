"""Run the validator's layers in order and write the report.

Order: inputs -> snapshot -> F -> D -> C -> isolated home -> A -> (static-only stops
here) -> E -> J. The first failing layer ends the run, except that a case failing
E1 is not judged and counts as a failed case in J1's pass rate. The isolated home,
the only place a credential is written, is removed in ``finally`` unless kept; the
report is written for every run that got as far as a run directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evolver.validator.activation import check_activation
from evolver.validator.children import REPO_ROOT
from evolver.validator.dispatch import e2e_layer, run_cases
from evolver.validator.home import build_isolated_home
from evolver.validator.judge import judge_case, judge_layer
from evolver.validator.report import LayerResult, Report, ValidatorEnvironmentError, ValidatorInputError, exit_code
from evolver.validator.scope import check_code, check_declarations, check_folder, other_plugin_packages
from evolver.validator.snapshot import take_snapshot
from evolver.validator.suite import Suite, load_suite

DEFAULT_MODEL = "deepseek/deepseek-flash"


@dataclass
class ValidateOptions:
    folder: Path
    suite: Path | None = None
    engine: Path | None = None
    host_config: Path | None = None
    model: str = DEFAULT_MODEL
    judge_model: str = DEFAULT_MODEL
    judge_repeats: int = 1
    min_pass_rate: float = 1.0
    report_path: Path | None = None
    keep: bool = False
    static_only: bool = False
    run_dir: Path | None = None
    judge_provider_factory: Callable[[Path, str], Any] | None = None


def _default_provider(config_path: Path, model: str) -> Any:
    from raven.config.loader import load_config
    from raven.providers.factory import make_provider

    try:
        return make_provider(load_config(config_path), model=model)
    except Exception as exc:  # noqa: BLE001 - any construction failure is the environment's
        raise ValidatorEnvironmentError(f"cannot build the judge model {model!r}: {exc}") from exc


def _agent_name(folder: Path) -> str:
    try:
        name = json.loads((folder / "subagent.json").read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError, AttributeError):
        name = None
    return str(name or folder.name)


def _raven_identity() -> tuple[str, str | None]:
    import raven

    git = shutil.which("git")
    commit = None
    if git is not None:
        try:
            done = subprocess.run(
                [git, "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
            )
            commit = done.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            commit = None
    return str(getattr(raven, "__version__", "unknown")), commit


def _check_inputs(opts: ValidateOptions) -> tuple[Path, Suite | None]:
    folder = Path(opts.folder)
    if not folder.is_dir():
        raise ValidatorInputError(f"agent folder not found: {folder}")
    if opts.engine is not None and not Path(opts.engine).is_dir():
        raise ValidatorInputError(f"engine directory not found: {opts.engine}")
    if opts.judge_repeats < 1:
        raise ValidatorInputError("--judge-repeats must be at least 1")
    if not 0.0 < opts.min_pass_rate <= 1.0:
        raise ValidatorInputError("--min-pass-rate must be in (0, 1]")
    if opts.suite is None:
        if not opts.static_only:
            raise ValidatorInputError("--suite is required unless --static-only")
        return folder, None
    return folder, load_suite(Path(opts.suite), agent_name=_agent_name(folder))


def _host_config(opts: ValidateOptions) -> Path:
    if opts.host_config is not None:
        return Path(opts.host_config)
    from raven.home import get_config_path

    return get_config_path()


async def _judge_all(provider: Any, opts: ValidateOptions, suite: Suite, runs: list) -> list:
    verdicts = []
    for case, run in zip(suite.cases, runs, strict=True):
        if run.error or not run.reply.strip():
            continue
        verdicts.append(
            await judge_case(provider, model=opts.judge_model, case=case, run=run, repeats=opts.judge_repeats)
        )
    return verdicts


def _layers(report: Report, layer: LayerResult) -> bool:
    report.layers.append(layer)
    return layer.passed


def validate(opts: ValidateOptions) -> Report:
    folder, suite = _check_inputs(opts)
    run_dir = Path(opts.run_dir) if opts.run_dir is not None else Path(tempfile.mkdtemp(prefix="evolver-validate-"))
    run_dir.mkdir(parents=True, exist_ok=True)
    version, commit = _raven_identity()
    snap = take_snapshot(folder, run_dir, engine=Path(opts.engine) if opts.engine else None)
    report = Report(
        folder_digest=snap.digest,
        suite_digest=suite.digest if suite else None,
        raven_version=version,
        raven_commit=commit,
        agent_model=opts.model,
        judge_model=opts.judge_model,
        rendered={},
        layers=[],
        cases=[],
        verdicts=[],
        outcome="FAIL",
    )
    shipped = REPO_ROOT / "agents"
    shipped_root = shipped if shipped.is_dir() else None
    home = None
    try:
        if not (
            _layers(report, check_folder(snap))
            and _layers(report, check_declarations(snap, shipped_root=shipped_root))
            and _layers(report, check_code(snap, other_packages=other_plugin_packages(shipped_root, snap)))
        ):
            return report
        home = build_isolated_home(
            run_dir, snap, host_config=_host_config(opts), model=opts.model, shipped_root=shipped_root
        )
        activation, report.rendered = check_activation(home, snap, model=opts.model)
        if not _layers(report, activation):
            return report
        if opts.static_only or suite is None:
            report.outcome = "STATIC-OK"
            return report
        runs = run_cases(
            home, agent_name=_agent_name(folder), suite=suite, run_dir=run_dir, run_id=uuid.uuid4().hex[:12]
        )
        report.cases = runs
        e2e = e2e_layer(runs)
        report.layers.append(e2e)
        if not any(r.status == "pass" for r in e2e.results):
            return report
        factory = opts.judge_provider_factory or _default_provider
        provider = factory(home.path / "config.json", opts.judge_model)
        report.verdicts = asyncio.run(_judge_all(provider, opts, suite, runs))
        verdict = judge_layer(report.verdicts, len(suite.cases), min_pass_rate=opts.min_pass_rate)
        report.layers.append(verdict)
        report.outcome = "PASS" if verdict.passed else "FAIL"
        return report
    except ValidatorEnvironmentError as exc:
        report.error = str(exc)
        raise
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if not opts.keep:
            # The snapshot is a copy of the folder as handed in, credentials included
            # when the folder carries any; like the home, it does not outlive the run.
            for scratch in ([home.path] if home is not None else []) + [run_dir / "package", run_dir / "engine"]:
                shutil.rmtree(scratch, ignore_errors=True)
        path = Path(opts.report_path) if opts.report_path else run_dir / "report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_json(), indent=2, default=str), encoding="utf-8")
        print(f"report: {path}", file=sys.stderr)


def _print_summary(report: Report) -> None:
    for layer in report.layers:
        failures = [r for r in layer.results if r.status == "fail"]
        notes = [r for r in layer.results if r.status == "advisory"]
        line = f"{layer.layer}: {'pass' if not failures else 'FAIL'}"
        if failures:
            line += " -- " + "; ".join(f"{r.rule} {r.where}: {r.message}".strip() for r in failures[:5])
        if notes:
            line += f" ({len(notes)} advisory)"
        print(line)
    judged = {v.case_id: v for v in report.verdicts}
    for run in report.cases:
        verdict = judged.get(run.case_id)
        state = "runtime failure" if run.error else ("pass" if verdict and verdict.passed else "fail")
        print(f"  case {run.case_id}: {state} ({run.seconds:.1f}s)")
    print(report.outcome)


def cli_main(args: argparse.Namespace) -> int:
    opts = ValidateOptions(
        folder=Path(args.folder),
        suite=Path(args.suite) if args.suite else None,
        engine=Path(args.engine) if args.engine else None,
        host_config=Path(args.host_config) if args.host_config else None,
        model=args.model,
        judge_model=args.judge_model,
        judge_repeats=args.judge_repeats,
        min_pass_rate=args.min_pass_rate,
        report_path=Path(args.report) if args.report else None,
        keep=args.keep,
        static_only=args.static_only,
    )
    try:
        report = validate(opts)
    except ValidatorInputError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    except ValidatorEnvironmentError as exc:
        print(f"environment failure: {exc}", file=sys.stderr)
        return 3
    _print_summary(report)
    return exit_code(report)
