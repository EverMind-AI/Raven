"""The code flow's exec: trunk's shell tool with the fork's fixes.

A long timeout is clamped instead of refused; a timed-out command keeps the
output it produced and says so in as many words; an oversized output is saved
whole under Agent home with the path in the result. Keeping the output is no
longer a difference -- trunk does that itself now -- so what this adds on that
axis is the explicit marker. And the sandbox discipline: the factory serves this
host-running replacement only where the host's own exec would run on the host
too, and declines under any configured sandbox backend.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.tools.registry import ToolRegistry
from raven.agent.tools.shell import ExecTool as TrunkExecTool
from raven.plugins.context import PluginContext, ServiceLocator

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow.tools import plugin as factories  # noqa: E402
from code_flow.tools.exec import (  # noqa: E402
    MAX_OUTPUT,
    SPILL_SUBDIR,
    TIMED_OUT_NOTE,
    CodeExecResult,
    CodeExecTool,
    CodeExecutor,
)


def _run(coro):
    return asyncio.run(coro)


def _ctx(tmp_path: Path, tools: dict | None = None) -> PluginContext:
    return PluginContext(
        config={"enabled": True, "tools": {"enabled": True, **(tools or {})}},
        services=ServiceLocator(workspace=tmp_path / "home", user_id="u", agent_id="a"),
    )


def _tool(tmp_path: Path, max_timeout: int = 1200) -> CodeExecTool:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return CodeExecTool(
        timeout=60,
        working_dir=str(home),
        executor=CodeExecutor(max_timeout=max_timeout, spill_dir=home / SPILL_SUBDIR),
        extra_allowed_dirs=(home,),
        max_timeout=max_timeout,
    )


def test_exec_created_python_gets_name_diagnostics_in_the_command_result(tmp_path):
    tool = _tool(tmp_path)
    out = _run(tool.execute(command="printf 'result = missing_value\\n' > created.py"))
    assert "Exit code: 0" in str(out) and "F821" in str(out) and "missing_value" in str(out)
    assert getattr(out, "ok", None) is True
    assert (tmp_path / "home" / "created.py").read_text() == "result = missing_value\n"


def test_exec_reuses_the_hosts_write_record_and_keeps_it_in_the_result(tmp_path):
    tool = _tool(tmp_path)
    tool.measure_writes(None)
    out = _run(tool.execute(command="printf 'result = missing_value\\n' > recorded.py"))
    assert "F821" in str(out)
    assert len(out.written) == 1 and out.written[0].path.endswith("recorded.py")


def test_exec_modified_python_labels_unknown_baseline_and_ignores_untouched_files(tmp_path):
    tool = _tool(tmp_path)
    home = tmp_path / "home"
    (home / "untouched.py").write_text("old = untouched_missing\n")
    (home / "changed.py").write_text("value = 1\n")
    out = str(_run(tool.execute(command="printf 'value = newly_missing\\n' > changed.py")))
    assert "newly_missing" in out and "pre-command baseline is unknown" in out
    assert "untouched_missing" not in out
    assert "Python diagnostics" not in str(_run(tool.execute(command="cat changed.py")))


def test_exec_syntax_feedback_keeps_command_failure_and_does_not_rollback(tmp_path):
    tool = _tool(tmp_path)
    out = _run(tool.execute(command="printf 'def f(:\\n' > broken.py; exit 7"))
    assert "Exit code: 7" in str(out) and "syntax error" in str(out)
    assert getattr(out, "ok", None) is False
    assert (tmp_path / "home" / "broken.py").read_text() == "def f(:\n"


def test_exec_diagnostics_follow_explicit_working_directory_and_have_a_switch(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    tool = factories.make_exec(_ctx(tmp_path))
    out = str(_run(tool.execute(command="printf 'x = missing\\n' > other.py", working_dir=str(elsewhere))))
    assert "other.py" in out and "F821" in out
    quiet = factories.make_exec(_ctx(tmp_path, {"execPythonDiagnostics": False}))
    out = str(_run(quiet.execute(command="printf 'def f(:\\n' > quiet.py")))
    assert "Python diagnostics" not in out


def test_exec_notes_preserve_file_metadata_and_control_flags():
    from code_flow.tools.exec import _prefixed

    from raven.contracts.tool import Continuation, FileRemoval, FileWrite, ToolOutput, ToolResult

    for cls in (ToolOutput, ToolResult):
        original = cls(
            "command failed",
            ok=False,
            retryable=False,
            blocks_call=True,
            continuation=Continuation.CONTINUE,
            written=(FileWrite(path="a.py", created=True, size=1),),
            removed=(FileRemoval(path="gone.py"),),
        )
        annotated = _prefixed(" diagnostic", original, append=True)
        assert annotated.written == original.written and annotated.removed == original.removed
        assert annotated.ok is False and annotated.retryable is False and annotated.blocks_call is True


def test_exec_diagnostics_do_not_read_files_withheld_by_the_host(tmp_path):
    class Shadow:
        async def stage_tree(self):
            return None

        async def trackable(self, paths):
            return {path for path in paths if Path(path).name != "private.py"}

    tool = _tool(tmp_path)
    tool.measure_writes(lambda root: Shadow())
    out = _run(
        tool.execute(
            command="printf 'private_value = private_missing\n' > private.py; "
            "printf 'public_value = public_missing\n' > public.py"
        )
    )
    assert "public_missing" in str(out)
    assert "private_missing" not in str(out)
    assert out.ok is True and len(out.written) == 2


@pytest.mark.parametrize("kind", ["missing", "exception"])
def test_unavailable_pre_command_snapshot_does_not_block_or_scan_all_files(tmp_path, monkeypatch, kind):
    from code_flow.tools import workspace_scan

    tool = _tool(tmp_path)

    def unavailable(*args, **kwargs):
        if kind == "exception":
            raise RuntimeError("listing failed")
        return None

    monkeypatch.setattr(workspace_scan, "take", unavailable)
    out = _run(tool.execute(command="printf 'result = missing\n' > kept.py"))
    assert out.ok is True and "Python diagnostics" not in str(out)
    assert (tmp_path / "home" / "kept.py").exists()


@pytest.mark.parametrize("kind", ["timeout", "exception"])
def test_post_command_checker_failure_keeps_original_exit_and_output(tmp_path, monkeypatch, kind):
    from code_flow.tools import exec as module

    tool = _tool(tmp_path)

    async def broken(*args):
        if kind == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("diagnostic failed")

    monkeypatch.setattr(tool, "_diagnose_writes", broken)
    monkeypatch.setattr(module, "_DIAGNOSTIC_WAIT_SECONDS", 0.01)
    out = _run(tool.execute(command="echo evidence; exit 9"))
    assert "evidence" in str(out) and "Exit code: 9" in str(out)
    assert out.ok is False


def test_refused_command_does_not_scan_the_working_directory(tmp_path, monkeypatch):
    tool = _tool(tmp_path)
    calls = []

    async def listing(root):
        calls.append(root)
        return {}

    monkeypatch.setattr(tool, "_listing", listing)
    monkeypatch.setattr(tool, "_guard_command", lambda *args: "refused for this test")
    out = _run(tool.execute(command="echo refused"))
    assert not calls and out.blocks_call


def test_exec_diagnostics_limit_changed_files_and_output(tmp_path):
    tool = _tool(tmp_path)
    out = str(
        _run(tool.execute(command="for i in 0 1 2 3 4 5 6 7 8 9; do printf 'result = missing\n' > file$i.py; done"))
    )
    assert out.count("Python diagnostics for") == 8
    assert "2 more were skipped" in out
    assert len(out) < 6500


def test_exec_diagnostics_skip_symlinks_cache_and_non_python_files(tmp_path):
    tool = _tool(tmp_path)
    home = tmp_path / "home"
    outside = tmp_path / "outside.py"
    outside.write_text("value = external_missing\n")
    (home / "link.py").symlink_to(outside)
    out = str(
        _run(
            tool.execute(
                command="mkdir -p .venv; printf 'x = cached_missing\n' > .venv/cached.py; printf 'def f(:\n' > text.txt"
            )
        )
    )
    assert "Python diagnostics" not in out


def test_coding_snapshot_deadline_discards_partial_results(tmp_path, monkeypatch):
    from code_flow.tools import workspace_scan

    (tmp_path / "a.py").touch()
    (tmp_path / "b.py").touch()
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(workspace_scan, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert workspace_scan.take(tmp_path, deadline=1) is None
    monkeypatch.setattr(workspace_scan, "time", SimpleNamespace(monotonic=lambda: 0))
    assert len(workspace_scan.take(tmp_path, deadline=1)) == 2


def test_coding_snapshot_entry_limit_and_invalid_directory(tmp_path, monkeypatch):
    from code_flow.tools import workspace_scan

    (tmp_path / "a.py").touch()
    (tmp_path / "b.py").touch()
    monkeypatch.setattr(workspace_scan, "MAX_ENTRIES", 1)
    assert workspace_scan.take(tmp_path, deadline=float("inf")) is None
    assert workspace_scan.take(tmp_path / "missing", deadline=float("inf")) is None


@pytest.mark.parametrize("params", [{"machine": "elsewhere"}, {"run_in_background": True}])
def test_nonlocal_or_background_commands_do_not_take_local_diagnostic_snapshots(tmp_path, monkeypatch, params):
    from raven.contracts.tool import ToolOutput

    tool = _tool(tmp_path)
    calls = []

    async def listing(root):
        calls.append(root)
        return {}

    async def host(*args, **kwargs):
        return ToolOutput("host response", ok=True)

    monkeypatch.setattr(tool, "_listing", listing)
    monkeypatch.setattr(TrunkExecTool, "execute", host)
    assert str(_run(tool.execute(command="test command", **params))) == "host response"
    assert calls == []


def test_missing_visibility_verdict_never_falls_back_to_reading_the_file(tmp_path, monkeypatch):
    from raven.contracts.tool import FileWrite, ToolOutput

    class Shadow:
        async def trackable(self, paths):
            raise RuntimeError("visibility unavailable")

    tool = _tool(tmp_path)
    target = tmp_path / "home" / "withheld.py"
    target.write_text("value = confidential_name\n")
    tool.measure_writes(lambda root: Shadow())

    async def host(*args, **kwargs):
        return ToolOutput("Exit code: 0", ok=True, written=(FileWrite(path=str(target), created=True, size=1),))

    monkeypatch.setattr(TrunkExecTool, "execute", host)
    out = _run(tool.execute(command="test command"))
    assert out.ok is True and len(out.written) == 1
    assert "confidential_name" not in str(out)


# --- the schema and the clamp -------------------------------------------------------


def test_the_timeout_has_a_ceiling_but_no_schema_maximum(tmp_path):
    """Trunk refuses ``timeout > 600`` at validation; here the schema carries no
    maximum, so the call runs and the clamp is reported instead."""
    tool = _tool(tmp_path)
    timeout = tool.parameters["properties"]["timeout"]
    assert "maximum" not in timeout
    assert "clamped" in timeout["description"] and "1200" in timeout["description"]
    trunk = TrunkExecTool().parameters["properties"]["timeout"]
    assert trunk["maximum"] == 600, "the trunk shape this replacement departs from"
    assert tool.timeout_seconds == 1260.0


def test_a_timeout_above_the_ceiling_is_clamped_and_said_so(tmp_path):
    tool = _tool(tmp_path)
    out = _run(tool.execute(command="echo clamped-run", timeout=99_999))
    text = str(out)
    assert text.startswith("[note: timeout 99999s clamped to the 1200s ceiling")
    assert "clamped-run" in text and "Exit code: 0" in text
    assert getattr(out, "ok", None) is True, "the note does not change the verdict"


def test_a_timeout_within_the_ceiling_runs_untouched(tmp_path):
    tool = _tool(tmp_path)
    out = str(_run(tool.execute(command="echo fine", timeout=900)))
    assert not out.startswith("[note") and "fine" in out


def test_the_registry_runs_a_long_timeout_instead_of_refusing_it(tmp_path):
    """Through the registry, which validates against the schema: trunk's tool
    would answer a validation error, this one runs."""
    reg = ToolRegistry()
    reg.register(_tool(tmp_path))
    out = str(_run(reg.execute("exec", {"command": "echo via-registry", "timeout": 5000})))
    assert "via-registry" in out and "clamped to the 1200s ceiling" in out


# --- partial output on timeout -----------------------------------------------------------


def test_a_timed_out_command_keeps_the_output_it_produced(tmp_path):
    tool = _tool(tmp_path)
    out = _run(tool.execute(command="echo started; echo more >&2; sleep 30", timeout=1))
    text = str(out)
    assert "started" in text, "what the command printed before the kill is kept"
    assert "STDERR:\nmore" in text
    assert "Timed out after 1s" in text and TIMED_OUT_NOTE in text
    assert "Exit code: -1" in text
    assert getattr(out, "ok", None) is False


def test_trunks_executor_preserves_output_on_timeout(tmp_path):
    """The shared executor preserves output produced before the timeout.

    This is the retired half of the reason the fork replaced it: trunk used to
    wait on ``communicate()`` and lose the buffered output when that raised,
    which is why this file's own partial-output fix was written. Trunk drains
    the pipes itself now, so that axis no longer tells the two apart.

    What is still this fork's on the same axis is the explicit marker. Trunk
    reports the kill through the stderr line and exit ``-1`` and nothing more,
    while ``CodeExecResult`` also flags ``timed_out`` and renders
    ``TIMED_OUT_NOTE``, which tells the model its output is a fragment. The
    last assertion is what keeps the two from quietly converging.
    """
    from raven.sandbox.direct_executor import DirectExecutor

    command = "echo started; sleep 30"
    result = _run(DirectExecutor().exec(command, timeout=0.3))
    assert result.stdout == "started\n"
    assert "Timed out" in result.stderr
    assert result.exit_code == -1

    # Both sides measured in one run, because the absence on its own is the
    # vacuous kind of assertion: it also holds for a fork that stopped marking
    # anything. The pair says the difference exists and which way round it is.
    forked = str(_run(_tool(tmp_path).execute(command=command, timeout=0.3)))
    assert TIMED_OUT_NOTE in forked, "the fork tells the model its output is a fragment"
    assert TIMED_OUT_NOTE not in result.as_text(), "trunk does not, and that is what is left"


def test_a_finished_command_reports_its_exit_code_and_both_streams(tmp_path):
    tool = _tool(tmp_path)
    text = str(_run(tool.execute(command="echo out; echo err >&2; exit 3")))
    assert "out" in text and "STDERR:\nerr" in text and "Exit code: 3" in text
    assert TIMED_OUT_NOTE not in text


# --- the output budget and the spill ---------------------------------------------------


def test_an_oversized_output_is_cut_at_the_budget_and_saved_whole(tmp_path):
    tool = _tool(tmp_path)
    text = str(_run(tool.execute(command="i=0; while [ $i -lt 4000 ]; do echo line-$i-0123456789; i=$((i+1)); done")))
    assert len(text) < MAX_OUTPUT + 600
    assert "chars truncated; full output" in text
    assert "full output saved to " in text
    saved = Path(text.split("full output saved to ", 1)[1].split(";", 1)[0])
    assert saved.parent == tmp_path / "home" / SPILL_SUBDIR, "under Agent home, never in the repository"
    body = saved.read_text()
    assert body.count("line-") == 4000 and "line-3999-0123456789" in body
    assert "line-0-0123456789" in text and "line-3999-0123456789" in text, "head and tail stay in view"


def test_the_budget_is_thirty_thousand_not_trunks_ten(tmp_path):
    result = CodeExecResult(stdout="x" * 20_000, stderr="", exit_code=0, spill_dir=tmp_path / "spill")
    assert "truncated" not in result.as_text(), "20k fits the new budget"
    assert not (tmp_path / "spill").exists(), "nothing is saved while nothing was cut"
    assert "truncated" in result.as_text(10_000), "and would not have fit trunk's"
    assert (tmp_path / "spill").exists(), "cut at trunk's budget, the whole output is saved"


def test_a_spill_that_cannot_be_written_still_returns_the_head_and_tail(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the directory should be")
    result = CodeExecResult(stdout="y" * 40_000, stderr="", exit_code=0, spill_dir=blocked)
    text = result.as_text()
    assert "truncated" in text and "re-run redirecting to a file" in text


# --- the environment discipline --------------------------------------------------------


def _child_env_keys(text: str) -> set[str]:
    body = text.split("\nExit code:", 1)[0]
    return set(json.loads(body))


def test_the_child_sees_exactly_the_host_executors_baseline(tmp_path, monkeypatch):
    """No private import of the trunk allowlist (a cargo redline), yet no drift
    from it either: the keys a child gets here equal the keys a child gets from
    the host's own executor, and a credential in the process environment
    reaches neither."""
    from raven.sandbox.direct_executor import DirectExecutor

    monkeypatch.setenv("CODE_FLOW_TEST_SECRET", "do-not-leak")
    probe = f"{sys.executable} -c 'import json,os;print(json.dumps(sorted(os.environ)))'"
    theirs = set(json.loads(_run(DirectExecutor().exec(probe, timeout=15)).stdout))
    ours = _child_env_keys(str(_run(_tool(tmp_path).execute(command=probe))))
    shell_added = {"PWD", "OLDPWD", "SHLVL", "_"}
    assert ours - shell_added == theirs - shell_added
    assert "CODE_FLOW_TEST_SECRET" not in ours and "CODE_FLOW_TEST_SECRET" not in theirs
    assert "PATH" in ours


def test_the_baseline_is_learned_once_and_read_fresh(tmp_path, monkeypatch):
    tool = _tool(tmp_path)
    executor = tool._executor
    first = _run(executor.allowed_keys())
    assert first is _run(executor.allowed_keys()), "one probe per executor"
    monkeypatch.setenv("PATH", os.environ["PATH"] + os.pathsep + str(tmp_path / "fresh-bin"))
    text = str(_run(tool.execute(command="echo $PATH")))
    assert str(tmp_path / "fresh-bin") in text, "values come from the process environment at spawn time"


def test_a_failed_probe_leaves_exec_erroring_rather_than_guessing(tmp_path, monkeypatch):
    tool = _tool(tmp_path)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "no-such-python"))
    out = str(_run(tool.execute(command="echo never")))
    assert out.startswith("Error executing command:") and "environment baseline" in out


# --- the factory and the sandbox discipline ---------------------------------------------


def test_the_factory_serves_the_replacement_only_on_the_host_backend(tmp_path):
    served = factories.make_exec(
        _ctx(tmp_path, {"exec": {"sandboxBackend": "none", "maxTimeout": 900, "timeout": 120}})
    )
    assert isinstance(served, CodeExecTool)
    assert served._MAX_TIMEOUT == 900 and served.timeout == 120
    assert isinstance(served._executor, CodeExecutor) and served._executor.is_sandboxed is False
    for backend in ("boxlite", "auto", "docker"):
        assert factories.make_exec(_ctx(tmp_path, {"exec": {"sandboxBackend": backend}})) is None, backend


def test_the_factory_declines_with_the_rest_of_the_face(tmp_path):
    off = PluginContext(
        config={"enabled": True, "tools": {"enabled": False}},
        services=ServiceLocator(workspace=tmp_path / "home", user_id="u", agent_id="a"),
    )
    assert factories.make_exec(off) is None


def test_the_same_name_replacement_answers_the_call(tmp_path):
    reg = ToolRegistry()
    reg.register(TrunkExecTool(working_dir=str(tmp_path)))
    reg.register(factories.make_exec(_ctx(tmp_path)))
    served = reg.get("exec")
    assert type(served) is CodeExecTool
    assert "clamped" in reg.get("exec").parameters["properties"]["timeout"]["description"]


def test_the_published_config_carries_the_ceiling_in_the_slice_not_in_a_dead_host_key():
    """Trunk's ``tools.exec`` knows ``timeout`` and ``pathAppend`` only; a
    ``maxTimeout`` there was silently ignored. It lives in this plugin's slice."""
    published = json.loads((REPO / "agents" / "raven-code" / "config.json").read_text())
    assert "maxTimeout" not in published["tools"]["exec"]
    assert published["plugins"]["config"]["code-flow"]["tools"]["exec"] == {"maxTimeout": 1200}
