"""Regression tests for the WebUI launcher preflight and failure handling."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO_ROOT / "start_webapp.sh"


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo"
    (workspace / "ui-webui" / "service").mkdir(parents=True)
    shutil.copy2(_LAUNCHER, workspace / "start_webapp.sh")
    return workspace


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_functions(
    workspace: Path,
    body: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    run_env.update(env or {})
    return subprocess.run(
        ["bash", "-c", 'source "$1"\n' + body, "bash", str(workspace / "start_webapp.sh")],
        cwd=workspace,
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_incomplete_service_python_falls_back_to_raven_tool_environment(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    tool_bin = tmp_path / "tool" / "bin"
    override_python = tmp_path / "override" / "python"
    _write_executable(tool_bin / "raven", "exit 0")
    _write_executable(
        tool_bin / "python",
        """
if [[ "${1-}" == "-c" && "${2-}" == *"import raven,"* && "${2-}" == *raven_gateway_agent* && "${2-}" == *uvicorn* ]]; then
    exit 0
fi
exit 64
""".strip(),
    )
    _write_executable(
        override_python,
        "printf '%s\\n' \"ModuleNotFoundError: No module named 'shortuuid'\" >&2\nexit 1",
    )

    result = _run_functions(
        workspace,
        """
if ! resolve_service_python; then
    exit 20
fi
printf 'resolved=%s\\nsource=%s\\n' "$RESOLVED_SERVICE_PYTHON" "$RESOLVED_SERVICE_PYTHON_SOURCE"
""",
        env={
            "PATH": f"{tool_bin}:/usr/bin:/bin",
            "SERVICE_PYTHON": str(override_python),
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"resolved={tool_bin / 'python'}" in result.stdout
    assert "source=raven tool env" in result.stdout
    assert "SERVICE_PYTHON failed the service import preflight" in result.stderr
    service_log = (workspace / "ui-webui" / ".webapp_logs" / "service.log").read_text()
    assert "No module named 'shortuuid'" in service_log
    assert f"[service] python: {tool_bin / 'python'} (raven tool env)" in service_log


def test_service_python_preflight_fails_before_startup_when_no_candidate_works(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    override_python = tmp_path / "override" / "python"
    _write_executable(override_python, "printf '%s\\n' 'broken import chain' >&2\nexit 1")

    result = _run_functions(
        workspace,
        """
if resolve_service_python; then
    exit 21
fi
printf 'preflight-failed\\n'
""",
        env={
            "PATH": "/usr/bin:/bin",
            "SERVICE_PYTHON": str(override_python),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "preflight-failed" in result.stdout
    assert "no Python interpreter can import the web service" in result.stderr
    assert "uv tool install --force --editable ." in result.stderr


def test_wait_for_port_returns_failure_after_timeout(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    result = _run_functions(
        workspace,
        """
port_in_use() { return 1; }
sleep() { :; }
if wait_for_port 8000 service 2; then
    exit 22
fi
printf 'wait-failed\\n'
""",
    )

    assert result.returncode == 0, result.stderr
    assert "wait-failed" in result.stdout
    assert "FAILED" in result.stderr
    assert "service.log" in result.stderr


def test_wait_for_port_fails_immediately_when_process_exits(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    result = _run_functions(
        workspace,
        """
printf '424242\\n' >"$SERVICE_PID_FILE"
pid_alive() { return 1; }
port_in_use() { return 1; }
sleep() { printf 'slept\\n' >>"$TRACE_FILE"; }
if wait_for_port 8000 service 30; then
    exit 24
fi
""",
        env={"TRACE_FILE": str(tmp_path / "sleep.trace")},
    )

    assert result.returncode == 0, result.stderr
    assert "pid 424242 exited" in result.stderr
    assert not (tmp_path / "sleep.trace").exists()


def test_unidentified_port_holder_blocks_startup(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    result = _run_functions(
        workspace,
        """
port_in_use() { return 0; }
pids_on_port() { :; }
claim_rc=0
claim_port service 8000 'main[.]py' || claim_rc=$?
printf 'claim=%s\\n' "$claim_rc"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "claim=2" in result.stdout
    assert "refusing to continue" in result.stderr


def test_mixed_owned_and_foreign_port_holders_block_startup(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    result = _run_functions(
        workspace,
        """
port_in_use() { return 0; }
pids_on_port() { printf '11\\n22\\n'; }
holder_is_ours() { [[ "$1" == "11" ]]; }
cmd_of_pid() { printf 'foreign-command'; }
claim_rc=0
claim_port service 8000 'main[.]py' || claim_rc=$?
printf 'claim=%s\\n' "$claim_rc"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "claim=2" in result.stdout
    assert "pid 22: foreign-command" in result.stderr
    assert "pid 11:" not in result.stderr


def test_bringup_stops_partial_stack_when_a_component_fails(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    trace_file = tmp_path / "cleanup.trace"
    result = _run_functions(
        workspace,
        """
resolve_service_python() { RESOLVED_SERVICE_PYTHON=/fake/python; return 0; }
ensure_redis() { :; }
ensure_logo() { :; }
ensure_deps() { :; }
port_in_use() { return 1; }
start_gateway() { :; }
start_service() { :; }
start_frontend() { :; }
wait_for_port() { [[ "$2" != "service" ]]; }
stop_all() { printf 'stopped\\n' >>"$TRACE_FILE"; }
if bringup; then
    exit 23
fi
""",
        env={"TRACE_FILE": str(trace_file)},
    )

    assert result.returncode == 0, result.stderr
    assert trace_file.read_text() == "stopped\n"
    assert "service failed to start; stopping the partial stack" in result.stderr


def test_service_token_is_passed_in_environment_not_process_arguments(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    trace_file = tmp_path / "spawn.trace"
    service_python = tmp_path / "service-python"
    token = "synthetic token ' ; $(not-a-command)"
    _write_executable(service_python, "exit 0")
    result = _run_functions(
        workspace,
        """
claim_port() { return 0; }
spawn_session() {
    : >"$TRACE_FILE"
    if [[ "${RAVEN_GATEWAY_WS_TOKEN-}" == "$EXPECTED_TOKEN" ]]; then
        printf 'env=ok\\n' >>"$TRACE_FILE"
    else
        printf 'env=missing\\n' >>"$TRACE_FILE"
    fi
    for argument in "$@"; do
        printf 'arg=%s\\n' "$argument" >>"$TRACE_FILE"
    done
}
RESOLVED_SERVICE_PYTHON="$TEST_SERVICE_PYTHON"
RESOLVED_SERVICE_PYTHON_SOURCE="test"
start_service
wait "$(<"$SERVICE_PID_FILE")"
""",
        env={
            "EXPECTED_TOKEN": token,
            "RAVEN_GATEWAY_WS_TOKEN": token,
            "TEST_SERVICE_PYTHON": str(service_python),
            "TRACE_FILE": str(trace_file),
        },
    )

    assert result.returncode == 0, result.stderr
    trace = trace_file.read_text()
    assert "env=ok" in trace
    assert f"arg={service_python}" in trace
    assert "arg=main.py" in trace
    assert token not in trace
