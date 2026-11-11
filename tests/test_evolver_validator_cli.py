"""The validator's runner and the ``python -m evolver validate`` subcommand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolver.cli import main
from evolver.validator.report import exit_code
from evolver.validator.runner import ValidateOptions, validate
from raven.contracts.llm_provider import LLMResponse
from tests.evolver_validator_stub import make_stub_agent

FLASH = "deepseek/deepseek-flash"


class ScriptedProvider:
    def __init__(self, reply: str):
        self.reply = reply

    async def chat_with_retry(self, messages, tools=None, model=None, temperature=None, **kwargs):
        return LLMResponse(content=self.reply)


PASS = json.dumps({"criteria": [{"id": "c1", "verdict": "pass", "reason": "said pong"}]})
FAIL = json.dumps({"criteria": [{"id": "c1", "verdict": "fail", "reason": "did not"}]})


def _suite(root: Path) -> Path:
    (root / "cases").mkdir(parents=True)
    (root / "suite.json").write_text(json.dumps({"name": "s", "version": "1", "target": None}), encoding="utf-8")
    case = {"id": "ping", "task": "Answer pong.", "criteria": [{"id": "c1", "statement": "The reply is pong."}]}
    (root / "cases" / "ping.json").write_text(json.dumps(case), encoding="utf-8")
    return root


def _host(tmp_path: Path, providers: dict | None = None) -> Path:
    path = tmp_path / "host.json"
    data = {"providers": providers if providers is not None else {"openrouter": {"apiKey": "sk-test"}}}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _options(tmp_path: Path, folder: Path, *, judge: str = PASS, **extra) -> ValidateOptions:
    defaults = dict(
        folder=folder,
        suite=_suite(tmp_path / "suite"),
        host_config=_host(tmp_path),
        model=FLASH,
        judge_model=FLASH,
        run_dir=tmp_path / "run",
        judge_provider_factory=lambda _config, _model: ScriptedProvider(judge),
    )
    defaults.update(extra)
    return ValidateOptions(**defaults)


def test_missing_folder_exits_2(tmp_path, capsys):
    assert main(["validate", str(tmp_path / "nope"), "--suite", str(_suite(tmp_path / "suite"))]) == 2
    assert "nope" in capsys.readouterr().err


def test_missing_suite_without_static_only_exits_2(tmp_path):
    folder = make_stub_agent(tmp_path / "src", mode="ok")
    assert main(["validate", str(folder), "--host-config", str(_host(tmp_path))]) == 2


def test_first_failing_layer_stops_the_run(tmp_path):
    folder = make_stub_agent(tmp_path / "src", mode="ok")
    (folder / "run.py").unlink()
    report = validate(_options(tmp_path, folder))
    assert report.outcome == "FAIL"
    assert [layer.layer for layer in report.layers] == ["F"]
    assert not (tmp_path / "run" / "home").exists()
    assert (tmp_path / "run" / "report.json").is_file()


def test_static_only_reports_static_ok(tmp_path, capsys):
    folder = make_stub_agent(tmp_path / "src", mode="ok")
    code = main(
        [
            "validate",
            str(folder),
            "--static-only",
            "--host-config",
            str(_host(tmp_path)),
            "--model",
            FLASH,
            "--report",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["outcome"] == "STATIC-OK"
    assert [layer["layer"] for layer in report["layers"]] == ["F", "D", "C", "A"]
    assert "STATIC-OK" in capsys.readouterr().out


def test_full_pass_with_stub_agent_and_judge(tmp_path):
    report = validate(_options(tmp_path, make_stub_agent(tmp_path / "src", mode="ok", reply="pong")))
    assert report.outcome == "PASS", report.layers
    assert [layer.layer for layer in report.layers] == ["F", "D", "C", "A", "E", "J"]
    assert report.cases[0].reply == "pong" and report.verdicts[0].passed
    assert exit_code(report) == 0


def test_judge_fail_exits_1(tmp_path):
    report = validate(_options(tmp_path, make_stub_agent(tmp_path / "src", mode="ok"), judge=FAIL))
    assert report.outcome == "FAIL" and exit_code(report) == 1


def test_environment_failure_exits_3(tmp_path):
    folder = make_stub_agent(tmp_path / "src", mode="ok")
    code = main(
        [
            "validate",
            str(folder),
            "--suite",
            str(_suite(tmp_path / "suite")),
            "--host-config",
            str(_host(tmp_path, providers={})),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 3
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["outcome"] == "FAIL" and "providers" in report["error"]


def test_home_removed_when_a_layer_raises(tmp_path, monkeypatch):
    import evolver.validator.runner as runner

    def boom(*_args, **_kwargs):
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(runner, "run_cases", boom)
    with pytest.raises(RuntimeError, match="dispatch exploded"):
        validate(_options(tmp_path, make_stub_agent(tmp_path / "src", mode="ok")))
    assert not (tmp_path / "run" / "home").exists()
    report = json.loads((tmp_path / "run" / "report.json").read_text(encoding="utf-8"))
    assert "dispatch exploded" in report["error"]


def test_keep_retains_home(tmp_path):
    validate(_options(tmp_path, make_stub_agent(tmp_path / "src", mode="ok"), keep=True))
    assert (tmp_path / "run" / "home" / "config.json").is_file()


def test_report_json_has_digests_models_and_layers(tmp_path):
    validate(_options(tmp_path, make_stub_agent(tmp_path / "src", mode="ok")))
    report = json.loads((tmp_path / "run" / "report.json").read_text(encoding="utf-8"))
    assert len(report["folder_digest"]) == 64 and len(report["suite_digest"]) == 64
    assert report["agent_model"] == FLASH and report["judge_model"] == FLASH
    assert report["rendered"] == {"model": FLASH, "provider": "", "memory_backend": None}
    assert report["verdicts"][0]["raw"] == [PASS]


@pytest.mark.parametrize("args", [["--judge-repeats", "0"], ["--min-pass-rate", "1.5"]])
def test_bad_numbers_exit_2(tmp_path, args):
    folder = make_stub_agent(tmp_path / "src", mode="ok")
    assert main(["validate", str(folder), "--suite", str(_suite(tmp_path / "suite")), *args]) == 2


def test_package_snapshot_removed_unless_kept(tmp_path):
    validate(_options(tmp_path, make_stub_agent(tmp_path / "src", mode="ok")))
    assert not (tmp_path / "run" / "package").exists()
    assert (tmp_path / "run" / "report.json").is_file()
    validate(_options(tmp_path / "kept", make_stub_agent(tmp_path / "kept" / "src", mode="ok"), keep=True))
    assert (tmp_path / "kept" / "run" / "package" / "stub-agent" / "subagent.json").is_file()
