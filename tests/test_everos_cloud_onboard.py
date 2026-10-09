"""The EverOS Cloud onboard screen: where the key comes from, what it says, what it writes."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from raven.plugins import OnboardUI, PluginContext, ServiceLocator, StepOutcome
from raven_everos_cloud.onboard import CloudKeyScreen, make_onboard_step
from tests._everos_cloud_fake import FakeCloud

BACK = object()


@dataclass
class _Console:
    lines: list[str] = field(default_factory=list)

    def print(self, *args: Any, **_: Any) -> None:
        self.lines.append(" ".join(str(a) for a in args))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _t(s: str, **kw: Any) -> str:
    return s.format(**kw) if kw else s


def _ui(console: _Console, keys: list[Any], choices: list[str], seen_options: list, on_choice=None) -> OnboardUI:
    def failure_choice(options, non_interactive=False):
        seen_options.append(list(options))
        if on_choice is not None:
            on_choice()
        return choices.pop(0)

    return OnboardUI(
        console=console,
        t=_t,
        require_questionary=lambda: None,
        qmark="?",
        back=BACK,
        step_header=lambda n, title: console.print(f"[{n}] {title}"),
        failure_choice=failure_choice,
        back_placeholder=lambda *a, **k: None,
        prompt_api_key=lambda name, allow_back=True: keys.pop(0),
        style=None,
        lend_provider_credentials=lambda p: {},
        keep_provider_credentials=lambda *a, **k: None,
        resolve_main_model=lambda m: {},
        set_embedding_endpoint=lambda f: "",
    )


def _ctx(tmp_path: Path, config: dict | None = None) -> PluginContext:
    return PluginContext(
        config={"base_url": "http://fake", **(config or {})},
        services=ServiceLocator(workspace=tmp_path, user_id="ecm-user", agent_id="ecm-agent"),
        logger=logging.getLogger("t"),
    )


def _screen(tmp_path: Path, fake: FakeCloud, config: dict | None = None) -> CloudKeyScreen:
    return CloudKeyScreen(_ctx(tmp_path, config), client_factory=lambda: httpx.AsyncClient(transport=fake.transport()))


def _run(screen: CloudKeyScreen, ui: OnboardUI, *, skip_test: bool = False) -> StepOutcome:
    return screen.run(ui, step_no=4, non_interactive=False, main_model=None, warnings=[], skip_test=skip_test)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    monkeypatch.delenv("EVEROS_CLOUD_API_KEY", raising=False)
    monkeypatch.setattr(
        "raven.config.update.set_plugin_config_fields", lambda pid, fields, **kw: calls.append((pid, fields))
    )
    return calls


def test_typed_key_is_probed_and_recorded(tmp_path: Path, recorded) -> None:
    fake = FakeCloud()
    console, options = _Console(), []
    outcome = _run(_screen(tmp_path, fake), _ui(console, ["ecm-key-7f3a"], [], options))
    assert outcome is StepOutcome.CONFIGURED
    assert recorded == [("everos-cloud-memory", {"api_key": "ecm-key-7f3a"})]
    probe = [e for e in fake.ledger if e["path"].endswith("/get")][0]
    assert probe["headers"]["authorization"] == "Bearer ecm-key-7f3a" and probe["body"]["page_size"] == 1
    assert "EverOS Cloud connected." in console.text and "Keys: https://everos.evermind.ai" in console.text


def test_env_key_is_used_and_not_written(tmp_path: Path, recorded, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVEROS_CLOUD_API_KEY", "ecm-env-key")
    fake = FakeCloud()
    console = _Console()
    outcome = _run(_screen(tmp_path, fake), _ui(console, [], [], []))
    assert outcome is StepOutcome.CONFIGURED and recorded == []
    assert "Using the API key from EVEROS_CLOUD_API_KEY" in console.text
    assert fake.ledger[0]["headers"]["authorization"] == "Bearer ecm-env-key"


def test_key_on_file_is_used_without_a_prompt(tmp_path: Path, recorded) -> None:
    console = _Console()
    outcome = _run(_screen(tmp_path, FakeCloud(), {"api_key": "on-file"}), _ui(console, [], [], []))
    assert outcome is StepOutcome.CONFIGURED and recorded == []
    assert "Using the API key on file." in console.text


def test_401_and_403_get_their_own_sentences_and_offer_reenter(tmp_path: Path, recorded) -> None:
    console, options = _Console(), []
    outcome = _run(_screen(tmp_path, FakeCloud(mode="401")), _ui(console, ["bad"], ["skip"], options))
    assert outcome is StepOutcome.DISABLED and recorded == []
    assert "API key rejected (401)" in console.text
    assert options[0][0] == ("Re-enter", "rekey") and options[0][1] == ("Skip long-term memory", "skip")

    console, options = _Console(), []
    outcome = _run(_screen(tmp_path, FakeCloud(mode="403")), _ui(console, ["bad"], ["skip"], options))
    assert outcome is StepOutcome.DISABLED
    assert "refused (403); this may mean the account is not authorized for the v2 memory API" in console.text


def test_reenter_asks_again_and_records_the_second_key(tmp_path: Path, recorded) -> None:
    fake = FakeCloud(mode="401")
    console, options = _Console(), []

    ui = _ui(console, ["bad", "good"], ["rekey"], options, on_choice=lambda: fake.set_mode("ok"))
    assert _run(_screen(tmp_path, fake), ui) is StepOutcome.CONFIGURED
    assert options[0][0] == ("Re-enter", "rekey")
    assert recorded == [("everos-cloud-memory", {"api_key": "good"})]


def test_env_key_failure_offers_no_reenter(tmp_path: Path, recorded, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVEROS_CLOUD_API_KEY", "ecm-env-key")
    console, options = _Console(), []
    outcome = _run(_screen(tmp_path, FakeCloud(mode="401")), _ui(console, [], ["skip"], options))
    assert outcome is StepOutcome.DISABLED
    assert options == [[("Skip long-term memory", "skip")]]


def test_unreachable_names_the_url(tmp_path: Path, recorded) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    ctx = _ctx(tmp_path, {"base_url": "http://127.0.0.1:9"})
    screen = CloudKeyScreen(ctx, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    console = _Console()
    assert _run(screen, _ui(console, ["k"], ["skip"], [])) is StepOutcome.DISABLED
    assert "cannot reach http://127.0.0.1:9" in console.text


def test_back_returns_back(tmp_path: Path, recorded) -> None:
    console = _Console()
    assert _run(_screen(tmp_path, FakeCloud()), _ui(console, [BACK], [], [])) is StepOutcome.BACK
    assert recorded == []


def test_hint_names_memory_user_id_and_the_locator_id(tmp_path: Path, recorded) -> None:
    console = _Console()
    _run(_screen(tmp_path, FakeCloud()), _ui(console, ["k"], [], []))
    assert "memory.userId" in console.text and "under user id ecm-user" in console.text


def test_skip_test_records_without_probing(tmp_path: Path, recorded) -> None:
    fake = FakeCloud()
    assert _run(_screen(tmp_path, fake), _ui(_Console(), ["k"], [], []), skip_test=True) is StepOutcome.CONFIGURED
    assert fake.ledger == [] and recorded == [("everos-cloud-memory", {"api_key": "k"})]


def test_configured_reads_file_then_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVEROS_CLOUD_API_KEY", raising=False)
    assert make_onboard_step(_ctx(tmp_path)).configured() is False
    assert make_onboard_step(_ctx(tmp_path, {"api_key": "k"})).configured() is True
    monkeypatch.setenv("EVEROS_CLOUD_API_KEY", "e")
    assert make_onboard_step(_ctx(tmp_path)).configured() is True
