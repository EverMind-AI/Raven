"""The hosted backends' shared onboarding screen: one key, one probe, one slice.

The wizard shell is a recording fake of ``OnboardUI``; the probe talks to the
fake service over ``MockTransport``; the slice lands in a temporary
``config.json`` through the host's own ``set_plugin_config_fields``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from raven.config.loader import set_config_path
from raven.memory_engine.api_key_onboard import ApiKeyOnboardStep
from raven.plugins import OnboardUI, PluginContext, ServiceLocator, StepOutcome
from raven_mem0.backend import Mem0Backend
from raven_mem0.onboard import make_onboard_step as make_mem0_step
from raven_memos.onboard import make_onboard_step as make_memos_step
from raven_zep.backend import ZepBackend
from raven_zep.onboard import make_onboard_step as make_zep_step
from tests._hosted_memory_fakes import FakeMem0, FakeZep, client_for

_BACK = object()


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args: Any, **kwargs: Any) -> None:
        self.lines.append(" ".join(str(a) for a in args))


class _UI:
    """A recording ``OnboardUI`` with scripted answers."""

    def __init__(self, *, keys: list[Any] | None = None, choices: list[str] | None = None) -> None:
        self.console = _Console()
        self.keys = list(keys or [])
        self.choices = list(choices or [])
        self.prompted = 0
        self.failure_options: list[list[tuple[str, str]]] = []
        self.headers: list[tuple[int, str]] = []

    def prompt_api_key(self, provider: str, *, allow_back: bool = False, **_: Any) -> Any:
        self.prompted += 1
        return self.keys.pop(0)

    def failure_choice(self, options: list[tuple[str, str]], *, non_interactive: bool) -> str:
        self.failure_options.append(options)
        return self.choices.pop(0)

    def build(self) -> OnboardUI:
        return OnboardUI(
            console=self.console,
            t=lambda text, **kw: text.format(**kw) if kw else text,
            require_questionary=lambda: None,
            qmark="?",
            back=_BACK,
            step_header=lambda n, title: self.headers.append((n, title)),
            failure_choice=self.failure_choice,
            back_placeholder=lambda *a, **k: "",
            prompt_api_key=self.prompt_api_key,
            style=None,
            lend_provider_credentials=lambda p: {},
            resolve_main_model=lambda m: {"provider": ""},
            set_embedding_endpoint=lambda fields: None,
        )


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("MEM0_API_KEY", "ZEP_API_KEY", "MEMOS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"memory": {"backend": None}, "plugins": {"config": {}}}), encoding="utf-8")
    set_config_path(cfg)
    return cfg


def _slices(cfg: Path) -> dict[str, Any]:
    return json.loads(cfg.read_text(encoding="utf-8"))["plugins"]["config"]


def _step(tmp_path: Path, fake, backend_cls=Mem0Backend, config: dict | None = None) -> ApiKeyOnboardStep:
    ctx = PluginContext(
        config=dict(config or {}),
        services=ServiceLocator(workspace=tmp_path, user_id="alice", agent_id="a"),
        logger=logging.getLogger("test.onboard"),
    )
    return ApiKeyOnboardStep(ctx, backend_cls, client_factory=lambda: client_for(fake))


def _run(step: ApiKeyOnboardStep, ui: _UI, **kw: Any) -> StepOutcome:
    args = {"step_no": 4, "non_interactive": False, "main_model": None, "warnings": [], "skip_test": False}
    args.update(kw)
    return step.run(ui.build(), **args)


# ── O-1 / O-2: the environment variable ─────────────────────────────


def test_key_from_the_environment_is_used_not_prompted_and_not_copied(
    tmp_path: Path, _isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeMem0()
    monkeypatch.setenv("MEM0_API_KEY", fake.KEY)
    ui = _UI()
    assert _run(_step(tmp_path, fake), ui) is StepOutcome.CONFIGURED
    assert ui.prompted == 0
    assert any("MEM0_API_KEY" in line for line in ui.console.lines)
    assert _slices(_isolated_config) == {"mem0": {"base_url": Mem0Backend.DEFAULT_BASE_URL}}
    assert fake.calls("GET", "/v1/memories/")


def test_configured_reads_the_environment_or_the_slice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeMem0()
    assert _step(tmp_path, fake).configured() is False
    assert _step(tmp_path, fake, config={"api_key": "k-12345678"}).configured() is True
    monkeypatch.setenv("MEM0_API_KEY", fake.KEY)
    assert _step(tmp_path, fake).configured() is True


def test_host_non_interactive_lane_keeps_a_configured_backend_and_clears_an_unconfigured_one(
    tmp_path: Path, _isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host never calls ``run`` headless; it asks ``configured()`` and
    clears ``memory.backend`` when the answer is no."""
    from raven.cli import onboard_commands

    fake = FakeMem0()
    _isolated_config.write_text(
        json.dumps({"memory": {"backend": "mem0"}, "plugins": {"config": {}}}), encoding="utf-8"
    )
    monkeypatch.setattr(onboard_commands, "_memory_steps", lambda: [("mem0", _step(tmp_path, fake))])
    onboard_commands._step4_memory(skip=False, non_interactive=True, main_model=None, warnings=[])
    assert json.loads(_isolated_config.read_text())["memory"]["backend"] is None

    _isolated_config.write_text(
        json.dumps({"memory": {"backend": "mem0"}, "plugins": {"config": {}}}), encoding="utf-8"
    )
    monkeypatch.setenv("MEM0_API_KEY", fake.KEY)
    onboard_commands._step4_memory(skip=False, non_interactive=True, main_model=None, warnings=[])
    assert json.loads(_isolated_config.read_text())["memory"]["backend"] == "mem0"


# ── O-3 … O-5: prompt, probe, failure menu, skip_test ───────────────


def test_typed_key_is_probed_then_recorded(tmp_path: Path, _isolated_config: Path) -> None:
    fake = FakeMem0()
    ui = _UI(keys=[fake.KEY])
    assert _run(_step(tmp_path, fake), ui) is StepOutcome.CONFIGURED
    assert ui.prompted == 1
    assert _slices(_isolated_config) == {"mem0": {"api_key": fake.KEY, "base_url": Mem0Backend.DEFAULT_BASE_URL}}
    assert len(fake.calls("GET", "/v1/memories/")) == 1
    assert ui.headers == [(4, "Long-term memory: mem0")]


def test_rejected_key_offers_reenter_then_records_the_good_one(tmp_path: Path, _isolated_config: Path) -> None:
    fake = FakeMem0()
    ui = _UI(keys=["wrong-key-12345678", fake.KEY], choices=["rekey"])
    assert _run(_step(tmp_path, fake), ui) is StepOutcome.CONFIGURED
    assert ui.prompted == 2
    assert [v for _, v in ui.failure_options[0]] == ["rekey", "skip"]
    assert _slices(_isolated_config)["mem0"]["api_key"] == fake.KEY


def test_rejected_key_then_skip_writes_nothing(tmp_path: Path, _isolated_config: Path) -> None:
    fake = FakeMem0()
    ui = _UI(keys=["wrong-key-12345678"], choices=["skip"])
    assert _run(_step(tmp_path, fake), ui) is StepOutcome.DISABLED
    assert _slices(_isolated_config) == {}


def test_environment_key_rejected_cannot_be_retyped(
    tmp_path: Path, _isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeMem0()
    monkeypatch.setenv("MEM0_API_KEY", "wrong-env-key-12345678")
    ui = _UI(choices=["skip"])
    assert _run(_step(tmp_path, fake), ui) is StepOutcome.DISABLED
    assert [v for _, v in ui.failure_options[0]] == ["skip"]
    assert _slices(_isolated_config) == {}


def test_back_on_the_key_prompt_rewinds(tmp_path: Path, _isolated_config: Path) -> None:
    fake = FakeMem0()
    ui = _UI(keys=[_BACK])
    assert _run(_step(tmp_path, fake), ui) is StepOutcome.BACK
    assert fake.requests == [] and _slices(_isolated_config) == {}


def test_skip_test_records_without_a_probe(tmp_path: Path, _isolated_config: Path) -> None:
    fake = FakeMem0()
    ui = _UI(keys=["unverified-key-12345678"])
    assert _run(_step(tmp_path, fake), ui, skip_test=True) is StepOutcome.CONFIGURED
    assert fake.requests == []
    assert _slices(_isolated_config)["mem0"]["api_key"] == "unverified-key-12345678"


def test_unreachable_service_reads_as_not_verified(tmp_path: Path, _isolated_config: Path) -> None:
    fake = FakeMem0()
    fake.timeout_next()
    ui = _UI(keys=[fake.KEY], choices=["skip"])
    assert _run(_step(tmp_path, fake), ui) is StepOutcome.DISABLED
    assert any("Couldn't verify" in line for line in ui.console.lines)


# ── O-7 … O-9: the slices stay apart; the host owns the pointer ─────


def test_configuring_a_second_service_keeps_the_first_key(tmp_path: Path, _isolated_config: Path) -> None:
    mem0, zep = FakeMem0(), FakeZep()
    assert _run(_step(tmp_path, mem0), _UI(keys=[mem0.KEY])) is StepOutcome.CONFIGURED
    assert _run(_step(tmp_path, zep, ZepBackend), _UI(keys=[zep.KEY])) is StepOutcome.CONFIGURED
    slices = _slices(_isolated_config)
    assert set(slices) == {"mem0", "zep"}
    assert slices["mem0"]["api_key"] == mem0.KEY
    assert slices["zep"]["api_key"] == zep.KEY
    assert not any(k.endswith("-memory") for k in slices)


def test_the_step_never_touches_memory_backend(tmp_path: Path, _isolated_config: Path) -> None:
    fake = FakeMem0()
    assert _run(_step(tmp_path, fake), _UI(keys=[fake.KEY])) is StepOutcome.CONFIGURED
    assert json.loads(_isolated_config.read_text())["memory"] == {"backend": None}


def test_recording_merges_and_keeps_the_file_mode(tmp_path: Path, _isolated_config: Path) -> None:
    os.chmod(_isolated_config, 0o600)
    _isolated_config.write_text(
        json.dumps({"memory": {"backend": None}, "plugins": {"config": {"mem0": {"base_url": "https://mirror.test"}}}}),
        encoding="utf-8",
    )
    fake = FakeMem0()
    _run(_step(tmp_path, fake, config={"base_url": "https://mirror.test"}), _UI(keys=[fake.KEY]))
    assert _slices(_isolated_config)["mem0"] == {"base_url": "https://mirror.test", "api_key": fake.KEY}
    assert oct(_isolated_config.stat().st_mode & 0o777) == "0o600"


def test_factories_name_their_service(tmp_path: Path) -> None:
    ctx = PluginContext(config={}, services=ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a"))
    assert [f(ctx)._cls.NAME for f in (make_mem0_step, make_zep_step, make_memos_step)] == ["mem0", "zep", "memos"]
