"""Layer E, parent side: prepare each case's workspace, run the dispatch child, collect results.

The parent never talks ACP itself; ``_dispatch`` does, in a child started with the
isolated home's environment. Files the agent creates or changes in a case's
working directory are recorded with their size and hash, and with their text when it is
UTF-8 (capped at :data:`TEXT_CAP` characters).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

from evolver.validator.children import module_command
from evolver.validator.home import IsolatedHome
from evolver.validator.report import CaseRun, FileRecord, LayerResult, RuleResult
from evolver.validator.suite import Suite

TEXT_CAP = 65536
CHILD_SLACK_S = 120


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Raven's own Checkpoint repository and its notice at a working directory's root, not output.
BOOKKEEPING_DIR = ".raven"


def _walk(workspace: Path) -> dict[str, Path]:
    return {
        path.relative_to(workspace).as_posix(): path
        for path in sorted(workspace.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.relative_to(workspace).parts[0] != BOOKKEEPING_DIR
    }


def collect_files(workspace: Path, *, before: dict[str, str]) -> list[FileRecord]:
    records = []
    for rel, path in _walk(workspace).items():
        data = path.read_bytes()
        digest = _sha(data)
        if before.get(rel) == digest:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        truncated = text is not None and len(text) > TEXT_CAP
        records.append(
            FileRecord(
                path=rel,
                size=len(data),
                sha256=digest,
                text=text[:TEXT_CAP] if text is not None else None,
                truncated=truncated,
            )
        )
    return records


def _prepare(run_dir: Path, suite: Suite) -> dict[str, tuple[Path, dict[str, str]]]:
    prepared = {}
    for case in suite.cases:
        workspace = run_dir / "cases" / case.id
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True)
        for rel, data in case.files.items():
            target = workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        prepared[case.id] = (workspace, {rel: _sha(p.read_bytes()) for rel, p in _walk(workspace).items()})
    return prepared


def _dispatch(home: IsolatedHome, job_path: Path, timeout: float) -> str | None:
    """Run the child; return an error text when it did not finish cleanly."""
    proc = subprocess.Popen(
        module_command("evolver.validator._dispatch", str(job_path)),
        env=home.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return f"dispatch child exceeded {timeout:.0f}s and was killed"
    if proc.returncode != 0:
        return f"dispatch child failed ({proc.returncode}): {err.strip()[-800:]}"
    return None


def run_cases(home: IsolatedHome, *, agent_name: str, suite: Suite, run_dir: Path, run_id: str) -> list[CaseRun]:
    run_dir = Path(run_dir)
    prepared = _prepare(run_dir, suite)
    out_path = run_dir / "dispatch-out.json"
    out_path.unlink(missing_ok=True)
    job = {
        "agents_root": str(home.path / "agents"),
        "agent_name": agent_name,
        "run_id": run_id,
        "out": str(out_path),
        "cases": [
            {"id": c.id, "task": c.task, "workspace": str(prepared[c.id][0]), "timeout_s": c.timeout_s}
            for c in suite.cases
        ],
    }
    job_path = run_dir / "dispatch-job.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    failure = _dispatch(home, job_path, sum(c.timeout_s for c in suite.cases) + CHILD_SLACK_S)
    finished = {r["case_id"]: r for r in json.loads(out_path.read_text(encoding="utf-8"))} if out_path.exists() else {}

    runs = []
    for case in suite.cases:
        workspace, before = prepared[case.id]
        result = finished.get(case.id, {})
        runs.append(
            CaseRun(
                case_id=case.id,
                reply=result.get("reply", ""),
                full_text=result.get("full_text", ""),
                files=collect_files(workspace, before=before),
                seconds=float(result.get("seconds", 0.0)),
                error=result.get("error") if result else (failure or "the dispatch child returned no result"),
            )
        )
    return runs


def e2e_layer(runs: list[CaseRun]) -> LayerResult:
    results = []
    for run in runs:
        if run.error:
            results.append(RuleResult("E1", "fail", run.error, run.case_id))
        elif not run.reply.strip():
            results.append(RuleResult("E1", "fail", "the turn returned no reply", run.case_id))
        else:
            results.append(RuleResult("E1", "pass", f"answered in {run.seconds:.1f}s", run.case_id))
    return LayerResult("E", results)
