"""Layer A: discover the folder in the isolated home, start it, and probe its plugins.

A1 discovery lists exactly one enabled row with the manifest's name. A2 the row's own
launcher answers an ACP ``initialize``; while it runs, A3 reads the config it
rendered (model under test, long-term memory off). A4 every package contribution
builds or declines without error; A5 an engine plugin registers nothing once its
config slice is gone. Rules that need an earlier one are not reported when it fails.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any

from evolver.validator.children import child_env, module_command
from evolver.validator.home import IsolatedHome
from evolver.validator.report import LayerResult, RuleResult, ValidatorEnvironmentError
from evolver.validator.scope import package_manifests
from evolver.validator.snapshot import Snapshot

PROBE_TIMEOUT_S = 180
# What a launcher prints when it refuses to serve for want of a credential (the
# heuristic `raven agents new` applies to its own smoke check).
_REFUSALS = ("_API_KEY is not set", "no provider key to inherit from")
_KIND_FIELDS = {
    "tools": "tools",
    "hooks": "hooks",
    "services": "services",
    "tool_gates": "tool_gates",
    "session_observers": "session_observers",
}


def _manifest_name(snap: Snapshot) -> str:
    return str(json.loads((snap.root / "subagent.json").read_text(encoding="utf-8")).get("name") or snap.source.name)


def _discover(home: IsolatedHome, name: str) -> tuple[Any, list[RuleResult]]:
    from raven.agent.subagent.vendored_agents import discover_product_rows, product_state

    root = home.path / "agents"
    rows = [row for row in discover_product_rows(root) if row.name == name]
    if len(rows) != 1:
        return None, [RuleResult("A1", "fail", f"discovery found {len(rows)} rows named {name!r}, expected 1")]
    row = rows[0]
    if not row.enabled:
        state = product_state(root).get(name)
        reason = state.detail if state is not None else "disabled"
        return None, [RuleResult("A1", "fail", f"discovered but disabled: {reason}", "subagent.json")]
    return row, [RuleResult("A1", "pass", f"discovered {name!r}, enabled")]


def _start(row: Any, home: IsolatedHome) -> subprocess.Popen:
    from raven.utils.commands import command_argv

    return subprocess.Popen(
        command_argv(str(row.command)),
        cwd=str(row.cwd) if row.cwd else None,
        env=home.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _handshake(proc: subprocess.Popen, timeout: float) -> tuple[str, str]:
    """Send ``initialize``; return (first stdout line, stderr so far)."""
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": 1, "clientCapabilities": {}},
    }
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None  # noqa: S101 - Popen(PIPE)
    try:
        proc.stdin.write(json.dumps(frame) + "\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    line: dict[str, str] = {}
    errors: list[str] = []
    reader = threading.Thread(target=lambda: line.setdefault("text", proc.stdout.readline()), daemon=True)
    drain = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True)
    reader.start()
    drain.start()
    reader.join(timeout)
    if proc.poll() is not None:
        drain.join(2)
    return line.get("text", ""), "".join(errors)


def _a2(row: Any, proc: subprocess.Popen) -> list[RuleResult]:
    timeout = max(float(getattr(row, "ready_timeout_ms", 120000) or 120000) / 1000.0, 10.0)
    text, stderr = _handshake(proc, timeout)
    try:
        reply = json.loads(text) if text.strip() else None
    except ValueError:
        reply = None
    if isinstance(reply, dict) and "result" in reply:
        return [RuleResult("A2", "pass", "ACP initialize answered")]
    if proc.poll() not in (None, 0) and any(marker in stderr for marker in _REFUSALS):
        raise ValidatorEnvironmentError(f"the launcher refused for a missing credential: {stderr.strip()[-300:]}")
    detail = (json.dumps(reply.get("error")) if isinstance(reply, dict) else text.strip()) or stderr.strip()[-500:]
    return [RuleResult("A2", "fail", f"no ACP initialize result: {detail or 'no output'}", "run.py")]


def _rendered(home: IsolatedHome, proc: subprocess.Popen) -> Path | None:
    found = sorted(home.path.rglob(f".config.rendered.{proc.pid}.json"))
    if not found:
        return None
    copy = home.path / "rendered.json"
    shutil.copyfile(found[0], copy)
    os.chmod(copy, 0o600)
    return copy


def check_rendered(rendered: dict, *, model: str, provider: str | None) -> list[RuleResult]:
    defaults = ((rendered.get("agents") or {}).get("defaults") or {}) if isinstance(rendered, dict) else {}
    memory = rendered.get("memory") if isinstance(rendered, dict) else None
    problems = []
    if defaults.get("model") != model:
        problems.append(f"model is {defaults.get('model')!r}, expected {model!r}")
    if provider and defaults.get("provider") != provider:
        problems.append(f"provider is {defaults.get('provider')!r}, expected {provider!r}")
    if not isinstance(memory, dict) or "backend" not in memory or memory["backend"] is not None:
        backend = memory.get("backend", "<default>") if isinstance(memory, dict) else "<default>"
        problems.append(f"long-term memory backend is {backend!r}, expected off")
    if problems:
        return [RuleResult("A3", "fail", "; ".join(problems), "rendered config")]
    return [RuleResult("A3", "pass", f"renders {model!r} with long-term memory off")]


def run_probe(config: Path, plugin_ids: list[str], *, env: dict[str, str], workspace: Path) -> dict:
    args = ["--config", str(config), "--workspace", str(workspace)]
    for plugin_id in plugin_ids:
        args += ["--plugin", plugin_id]
    proc = subprocess.run(
        module_command("evolver.validator._probe", *args),
        env=child_env(env),
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_S,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    if proc.returncode != 0 or not lines:
        return {"error": (proc.stderr or proc.stdout).strip()[-800:]}
    return json.loads(lines[-1])


def _declared(snap: Snapshot, *, engine_only: bool) -> dict[str, dict[str, list[str]]]:
    declared: dict[str, dict[str, list[str]]] = {}
    for path, manifest in package_manifests(snap):
        is_engine = snap.engine is not None and path.is_relative_to(snap.engine)
        if engine_only and not is_engine:
            continue
        contributes = manifest.contributes
        declared[manifest.id] = {
            kind: [item.name for item in getattr(contributes, field)] for kind, field in _KIND_FIELDS.items()
        }
    return declared


def judge_a4(probe: dict, declared: dict[str, dict[str, list[str]]]) -> list[RuleResult]:
    if "error" in probe:
        return [RuleResult("A4", "fail", f"plugin probe failed: {probe['error']}")]
    failures = [
        RuleResult("A4", "fail", f"plugin {f['plugin']} did not activate: {f['cause']}")
        for f in probe.get("activation_failures", [])
        if f.get("plugin") in declared
    ]
    observed = probe.get("plugins", {})
    for plugin_id, kinds in declared.items():
        for kind, names in kinds.items():
            statuses = (observed.get(plugin_id) or {}).get(kind) or {}
            for name in names:
                status = statuses.get(name)
                if status is None:
                    failures.append(RuleResult("A4", "fail", f"{kind[:-1]} {name} not discovered by the registry"))
                elif status.startswith("failed"):
                    failures.append(RuleResult("A4", "fail", f"{kind[:-1]} {name} {status}"))
    return failures or [RuleResult("A4", "pass", "every contribution builds or declines")]


def judge_a5(probe: dict, declared: dict[str, dict[str, list[str]]]) -> list[RuleResult]:
    if "error" in probe:
        return [RuleResult("A5", "fail", f"plugin probe failed: {probe['error']}")]
    observed = probe.get("plugins", {})
    leaks = [
        RuleResult("A5", "fail", f"{kind[:-1]} {name} registers without its config slice")
        for plugin_id, kinds in declared.items()
        for kind, names in kinds.items()
        for name in names
        if ((observed.get(plugin_id) or {}).get(kind) or {}).get(name) == "registered"
    ]
    return leaks or [RuleResult("A5", "pass", "nothing registers without its config slice")]


def _probe_layer(home: IsolatedHome, snap: Snapshot, rendered_path: Path) -> list[RuleResult]:
    declared = _declared(snap, engine_only=False)
    if not declared:
        return [
            RuleResult("A4", "pass", "the package contributes no plugins"),
            RuleResult("A5", "pass", "the package contributes no plugins"),
        ]
    workspace = home.path / "probe-workspace"
    results = judge_a4(run_probe(rendered_path, list(declared), env=home.env, workspace=workspace), declared)
    engine = _declared(snap, engine_only=True)
    if not engine:
        results.append(
            RuleResult(
                "A5", "pass", "not applicable: directory plugins load only through their launcher's plugins.dirs"
            )
        )
        return results
    data = json.loads(rendered_path.read_text(encoding="utf-8"))
    slices = (data.get("plugins") or {}).get("config") or {}
    for plugin_id in engine:
        slices.pop(plugin_id, None)
    off = home.path / "rendered-without-slices.json"
    off.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(off, 0o600)
    results += judge_a5(run_probe(off, list(engine), env=home.env, workspace=workspace), engine)
    return results


def check_activation(home: IsolatedHome, snap: Snapshot, *, model: str) -> tuple[LayerResult, dict]:
    row, results = _discover(home, _manifest_name(snap))
    if row is None:
        return LayerResult("A", results), {}
    proc = _start(row, home)
    try:
        a2 = _a2(row, proc)
        results += a2
        if any(r.status == "fail" for r in a2):
            return LayerResult("A", results), {}
        rendered_path = _rendered(home, proc)
        if rendered_path is None:
            results.append(RuleResult("A3", "fail", "the launcher wrote no rendered config", "run.py"))
            return LayerResult("A", results), {}
        rendered = json.loads(rendered_path.read_text(encoding="utf-8"))
    finally:
        _stop(proc)
    results += check_rendered(rendered, model=model, provider=home.provider or None)
    defaults = (rendered.get("agents") or {}).get("defaults") or {}
    summary = {
        "model": defaults.get("model"),
        "provider": defaults.get("provider", ""),
        "memory_backend": (rendered.get("memory") or {}).get("backend", "<default>"),
    }
    results += _probe_layer(home, snap, rendered_path)
    return LayerResult("A", results), summary
