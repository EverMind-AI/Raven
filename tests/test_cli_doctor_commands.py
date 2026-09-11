"""CLI tests for ``raven doctor``.

Static checks are validated against on-disk config produced by the
``tmp_config`` / ``healthy_config`` fixtures. The probe boundary
(:func:`raven.cli.doctor_commands.send_probe`) is monkeypatched
so tests never touch the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from raven.cli import doctor_commands
from raven.cli.commands import app
from raven.config.loader import save_config, set_config_path
from raven.config.schema import Config
from tests._everos_presence import everos_plugin_absent, everos_plugin_broken

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_raven_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`doctor` now inspects the installation on every run, which reads -- and
    can clear -- the upgrade marker in the agent home. Left unisolated, this
    file would report on, and tidy up after, a real upgrade in flight."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "agent-home"))
    return tmp_path / "agent-home"


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Point the loader at a tmp config file; tests opt-in via save_config."""
    cfg = tmp_path / "config.json"
    set_config_path(cfg)
    yield cfg
    set_config_path(None)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def no_memory_server(monkeypatch: pytest.MonkeyPatch):
    """Keep the memory probe away from whatever server the developer is running.

    `memory.backend` defaults to everos, so without this every test here reaches
    localhost:18791 and reports whatever that server happens to answer -- a
    machine whose embedding provider is broken would fail the healthy-exit-0
    case. Tests that care about capabilities install their own answer.
    """
    from raven_everos import health as _health

    monkeypatch.setattr(
        _health,
        "probe_capabilities",
        lambda *_a, **_kw: _health.CapabilityReport(reachable=False, error="probe disabled in tests"),
    )
    return monkeypatch


@pytest.fixture
def healthy_config(tmp_config: Path, tmp_path: Path) -> Path:
    """Persist a config that routes cleanly to a real provider name."""
    cfg = Config()
    cfg.agents.defaults.model = "anthropic/claude-sonnet-4-5"
    cfg.agents.defaults.workspace = str(tmp_path / "workspace")
    cfg.providers.anthropic.api_key = "sk-fake"
    save_config(cfg)
    return tmp_config


# --------------------------------------------------------------------------- help


def test_doctor_help_lists_all_flags() -> None:
    """``--help`` exposes the full flag surface."""
    r = runner.invoke(app, ["doctor", "--help"])
    assert r.exit_code == 0, r.stdout
    for flag in ("--probe", "--json", "--timeout", "--install-summary"):
        assert flag in r.stdout, f"missing flag in help: {flag}"


# --------------------------------------------------------------------------- default mode


def test_doctor_default_on_missing_config_exit1(tmp_config: Path) -> None:
    """No config file → exit 1 with a hint to run ``onboard``."""
    assert not tmp_config.exists()
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 1, r.stdout
    assert "not configured" in r.stdout
    assert "raven onboard" in r.stdout


def test_doctor_default_healthy_exit0(healthy_config: Path) -> None:
    """Resolved routing + no probe → exit 0, no network call made."""
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0, r.stdout
    # Routing section should mention the resolved provider name
    assert "anthropic" in r.stdout.lower()
    assert "Configuration looks healthy" in r.stdout or "All checks passed" in r.stdout


def test_doctor_unresolved_routing_exit1(tmp_config: Path) -> None:
    """Model that no configured provider can serve → exit 1."""
    cfg = Config()
    cfg.agents.defaults.model = "anthropic/claude-sonnet-4-5"
    # Leave every api_key empty so ``_match_provider`` returns ``(None, None)``.
    save_config(cfg)
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 1, r.stdout
    assert "unresolved" in r.stdout.lower() or "could not be routed" in r.stdout


# --------------------------------------------------------------------------- gateway status


def test_doctor_shows_gateway_running(healthy_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A held instance lock surfaces as ``running (pid …)`` in the Gateway section."""
    from raven.gateway import lock as _gateway_lock

    monkeypatch.setattr(
        _gateway_lock,
        "read_status",
        lambda now: _gateway_lock.LockInfo(pid=999, started_at=1_700_000_000.0, config_path=""),
    )
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0, r.stdout
    assert "Gateway" in r.stdout
    assert "running" in r.stdout
    assert "999" in r.stdout


def test_doctor_shows_gateway_not_running(healthy_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.gateway import lock as _gateway_lock

    monkeypatch.setattr(_gateway_lock, "read_status", lambda now: None)
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0, r.stdout
    assert "not running" in r.stdout


# --------------------------------------------------------------------------- --probe


def test_doctor_probe_success(healthy_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--probe`` invokes send_probe → exit 0, response shown in output."""
    monkeypatch.setattr(
        doctor_commands,
        "send_probe",
        lambda **_: ("Hello!", 42, 1.5),
    )
    r = runner.invoke(app, ["doctor", "--probe"])
    assert r.exit_code == 0, r.stdout
    assert "Hello!" in r.stdout
    assert "42 tokens" in r.stdout


def test_doctor_probe_failure_exit2(healthy_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Static checks pass but probe raises → exit 2."""

    def _boom(**_):
        raise RuntimeError("auth failed")

    monkeypatch.setattr(doctor_commands, "send_probe", _boom)
    r = runner.invoke(app, ["doctor", "--probe"])
    assert r.exit_code == 2, r.stdout
    assert "auth failed" in r.stdout


def test_doctor_timeout_flag_passed_through(healthy_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--timeout 3`` reaches ``send_probe`` as ``timeout_s=3``."""
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return ("ok", 1, 0.1)

    monkeypatch.setattr(doctor_commands, "send_probe", _capture)
    r = runner.invoke(app, ["doctor", "--probe", "--timeout", "3"])
    assert r.exit_code == 0, r.stdout
    assert captured.get("timeout_s") == 3


# --------------------------------------------------------------------------- --json


def test_doctor_json_default_structure(healthy_config: Path) -> None:
    """``--json`` emits a parseable doc with the documented top-level keys."""
    r = runner.invoke(app, ["doctor", "--json"])
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout)
    assert data["version"] == 1
    for key in ("paths", "routing", "features", "gateway"):
        assert key in data, f"missing top-level key: {key}"
    assert "running" in data["gateway"]
    # No probe was requested → key present but null
    assert data["probe"] is None


def test_doctor_json_with_probe_structure(healthy_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json --probe`` populates the probe key with the result fields."""
    monkeypatch.setattr(
        doctor_commands,
        "send_probe",
        lambda **_: ("hi", 10, 0.2),
    )
    r = runner.invoke(app, ["doctor", "--json", "--probe"])
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout)
    assert isinstance(data["probe"], dict)
    assert data["probe"]["ok"] is True
    assert data["probe"]["text"] == "hi"
    assert data["probe"]["tokens"] == 10


# --------------------------------------------------------------------------- memory


def _capabilities(no_memory_server, **caps: bool) -> None:
    """Make the memory probe answer as a reachable server with `caps`."""
    from raven_everos import health as _health

    no_memory_server.setattr(
        _health,
        "probe_capabilities",
        lambda *_a, **_kw: _health.CapabilityReport(reachable=True, capabilities=caps),
    )


def _configured(no_memory_server, *sections: str) -> None:
    from raven.config import update_everos

    no_memory_server.setattr(update_everos, "everos_role_configured", lambda s: s in sections)


def test_the_probe_follows_the_configured_address(healthy_config: Path, no_memory_server) -> None:
    """Probing the default while the backend reads `plugins.config` reports on a
    server nobody is using: someone who moved everos off 18791 is told it is not
    running, by the very check added to make a degraded server visible.
    """
    import json as _json

    from raven_everos import health as _health

    raw = _json.loads(healthy_config.read_text())
    raw.setdefault("plugins", {}).setdefault("config", {})["everos-memory"] = {"base_url": "http://localhost:29999"}
    healthy_config.write_text(_json.dumps(raw))

    seen: list[str] = []

    def _probe(base_url: str = "", *_a, **_kw):
        seen.append(base_url)
        return _health.CapabilityReport(reachable=True, capabilities={"llm": True})

    no_memory_server.setattr(_health, "probe_capabilities", _probe)
    _configured(no_memory_server, "llm")

    r = runner.invoke(app, ["doctor"])

    assert r.exit_code == 0, r.stdout
    assert seen == ["http://localhost:29999"], seen


def test_an_unbuilt_multimodal_role_is_reported(healthy_config: Path, no_memory_server) -> None:
    """multimodal is config surface the wizard writes, so it can fail to build --
    and while it was absent from DEGRADING_SECTIONS neither consumer looked at it,
    making the section-name mapping dead code.
    """
    _configured(no_memory_server, "llm", "multimodal")
    _capabilities(no_memory_server, llm=True, multimodal_llm=False)

    r = runner.invoke(app, ["doctor"])

    assert r.exit_code == 0, r.stdout
    out = " ".join(r.stdout.split())
    assert "multimodal" in out
    assert "could not build it" in out
    # Not "recall runs degraded": an unbuilt multimodal llm costs ingest, and
    # recall never saw those inputs either way.
    assert "so memory runs degraded" in out


def test_doctor_reports_a_reachable_memory_server(healthy_config: Path, no_memory_server) -> None:
    _configured(no_memory_server, "llm", "embedding")
    _capabilities(no_memory_server, llm=True, embed=True, rerank=False)

    r = runner.invoke(app, ["doctor"])

    assert r.exit_code == 0, r.stdout
    assert "Memory" in r.stdout
    assert "running" in r.stdout


def test_doctor_answers_where_the_memories_are(healthy_config: Path, no_memory_server, tmp_path) -> None:
    """Nothing used to answer this. The wizard printed the root once while
    converging and no command showed it again, so a user asking "where are my
    memories" had to read config.json by hand. Doctor is where that question
    gets asked."""
    from raven.config import update_everos as ue

    _configured(no_memory_server, "llm")
    _capabilities(no_memory_server, llm=True)
    no_memory_server.setattr(ue, "everos_root", lambda: tmp_path / "mem-root")
    no_memory_server.setattr(ue, "everos_owned", lambda: True)

    r = runner.invoke(app, ["doctor"])

    assert r.exit_code == 0, r.stdout
    # Asserting the tail segment, not the whole path: rich wraps long paths and
    # the full string is not contiguous in stdout.
    assert "Memories:" in r.stdout
    assert "mem-root" in r.stdout
    assert "Address:" in r.stdout
    assert "Managed by you" not in r.stdout


def test_doctor_says_when_the_memories_are_not_ravens_to_touch(
    healthy_config: Path, no_memory_server, tmp_path
) -> None:
    """A user-managed root is read-only, and doctor is the one place a user is
    asking about state rather than being walked through a decision -- so it has to
    say which of the two situations they are in."""
    from raven.config import update_everos as ue

    _configured(no_memory_server, "llm")
    _capabilities(no_memory_server, llm=True)
    no_memory_server.setattr(ue, "everos_root", lambda: tmp_path / "theirs")
    no_memory_server.setattr(ue, "everos_owned", lambda: False)

    r = runner.invoke(app, ["doctor"])

    out = " ".join(r.stdout.split())
    assert "Managed by you" in out
    assert "never writes, starts or stops it" in out
    # And it must not name a directory: nothing records where their memories live.
    assert str(tmp_path / "theirs") not in out


def test_an_unbuilt_optional_role_is_reported_without_failing(healthy_config: Path, no_memory_server) -> None:
    """Without embedding the adapter searches lexically instead of semantically:
    weaker memory, not broken memory. Worth saying, not worth an exit code."""
    _configured(no_memory_server, "llm", "embedding")
    _capabilities(no_memory_server, llm=True, embed=False)

    r = runner.invoke(app, ["doctor"])

    assert r.exit_code == 0, r.stdout
    assert "could not build it" in r.stdout
    assert "runs degraded" in r.stdout


def test_an_unbuilt_required_role_is_a_failure(healthy_config: Path, no_memory_server) -> None:
    """Nothing works without the llm, so this one does set the exit code."""
    _configured(no_memory_server, "llm")
    _capabilities(no_memory_server, llm=False, embed=True)

    r = runner.invoke(app, ["doctor"])

    assert r.exit_code == 2, r.stdout
    assert "cannot work" in r.stdout


def test_an_unconfigured_optional_role_names_what_it_costs(healthy_config: Path, no_memory_server) -> None:
    """A user deciding whether to configure embedding needs to know it buys
    semantic recall specifically, not a vague "better memory"."""
    _configured(no_memory_server, "llm")
    _capabilities(no_memory_server, llm=True, embed=True)

    r = runner.invoke(app, ["doctor"])

    assert r.exit_code == 0, r.stdout
    assert "matches keywords, not meaning" in r.stdout


def test_a_missing_optional_role_is_not_a_failure(healthy_config: Path, no_memory_server) -> None:
    """rerank is optional -- agent-track recall falls back to the LLM lane."""
    _configured(no_memory_server, "llm", "embedding")
    _capabilities(no_memory_server, llm=True, embed=True, rerank=False)

    r = runner.invoke(app, ["doctor"])

    assert r.exit_code == 0, r.stdout
    assert "rerank" in r.stdout


def test_a_server_that_reports_no_capabilities_is_not_condemned(healthy_config: Path, no_memory_server) -> None:
    """Pre-1.2.1 servers answer a bare {"status": "ok"}; reading that silence as
    "unavailable" would fail a working install."""
    _configured(no_memory_server, "llm", "embedding")
    _capabilities(no_memory_server)

    r = runner.invoke(app, ["doctor"])

    assert r.exit_code == 0, r.stdout
    assert "does not report capabilities" in r.stdout


def test_a_server_that_is_not_running_is_not_a_failure(healthy_config: Path) -> None:
    """Raven starts the server on demand, so "not running" is a normal state."""
    r = runner.invoke(app, ["doctor"])

    assert r.exit_code == 0, r.stdout
    assert "not running" in r.stdout


def test_memory_section_reaches_the_json_output(healthy_config: Path, no_memory_server) -> None:
    _configured(no_memory_server, "llm", "embedding")
    _capabilities(no_memory_server, llm=True, embed=False)

    r = runner.invoke(app, ["doctor", "--json"])

    payload = json.loads(r.stdout)
    assert payload["memory"]["capabilities"] == {"llm": True, "embed": False}
    assert payload["memory"]["configured"] == ["llm", "embedding"]


# --------------------------------------------------------------------------- config visibility


def _run_doctor_subprocess(home: Path) -> tuple[str, int]:
    """Run ``raven doctor`` in a subprocess with a sandbox HOME.

    A subprocess (not CliRunner) is required here: the loader's duplicate
    warning went out over two channels (print + loguru), and loguru's sink
    holds the real stderr, invisible to in-process capture.
    """
    import os
    import subprocess
    import sys

    # RAVEN_HOME is dropped rather than passed through: the point of this helper
    # is a sandbox HOME, and an inherited RAVEN_HOME (the isolation fixture sets
    # one) would out-rank it and send the subprocess to a different config.
    env = {k: v for k, v in os.environ.items() if k != "RAVEN_HOME"}
    env.update({"HOME": str(home), "COLUMNS": "250"})
    r = subprocess.run(
        [sys.executable, "-m", "raven", "doctor"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    return r.stdout + r.stderr, r.returncode


def _write_home_config(tmp_path: Path, name: str, body: str | None) -> Path:
    home = tmp_path / name
    (home / ".raven").mkdir(parents=True)
    if body is not None:
        (home / ".raven" / "config.json").write_text(body, encoding="utf-8")
    return home


def test_doctor_bad_config_warns_exactly_once(tmp_path: Path) -> None:
    home = _write_home_config(tmp_path, "bad", '{"providers": {},}')
    out, _ = _run_doctor_subprocess(home)
    assert out.count("not valid JSON") == 1, out


def test_doctor_config_line_three_states(tmp_path: Path) -> None:
    import re

    home_bad = _write_home_config(tmp_path, "bad", '{"providers": {},}')
    out_bad, _ = _run_doctor_subprocess(home_bad)
    config_line = next(line for line in out_bad.splitlines() if "Config:" in line)
    assert "✓" not in config_line, out_bad
    assert re.search(r"invalid JSON", out_bad), out_bad

    home_missing = _write_home_config(tmp_path, "missing", None)
    out_missing, _ = _run_doctor_subprocess(home_missing)
    assert re.search(r"missing|not found", out_missing), out_missing

    home_good = _write_home_config(tmp_path, "good", "{}")
    out_good, _ = _run_doctor_subprocess(home_good)
    config_line = next(line for line in out_good.splitlines() if "Config:" in line)
    assert "✓" in config_line, out_good


def test_doctor_empty_config_is_invalid(tmp_path: Path) -> None:
    """An empty config.json runs on defaults (load_config sees a JSON syntax
    error), so doctor must not paint the Config line green."""
    home = _write_home_config(tmp_path, "empty", "")
    out, code = _run_doctor_subprocess(home)
    config_line = next(line for line in out.splitlines() if "Config:" in line)
    assert "✓" not in config_line, out
    assert "empty" in config_line, out
    assert code == 1, out


def test_doctor_non_object_config_is_invalid(tmp_path: Path) -> None:
    """A valid-JSON non-object top level (e.g. null) carries no settings, so
    doctor must classify it invalid instead of green."""
    home = _write_home_config(tmp_path, "nonobject", "null")
    out, code = _run_doctor_subprocess(home)
    config_line = next(line for line in out.splitlines() if "Config:" in line)
    assert "✓" not in config_line, out
    assert "not a JSON object" in config_line, out
    assert code == 1, out


def test_doctor_everos_without_embedding_shows_keyword_only(tmp_path: Path) -> None:
    """The Memory section must say recall is keyword-only when the embedding
    role is not configured in the user-level everos.toml."""
    import re

    home = _write_home_config(tmp_path, "everos_nokey", json.dumps({"memory": {"backend": "everos"}}))
    out, _ = _run_doctor_subprocess(home)
    out = " ".join(out.split())
    assert re.search(r"Retrieval:\s*keyword-only", out), out
    assert "no embedding key" in out, out


def test_doctor_everos_with_embedding_shows_semantic(tmp_path: Path) -> None:
    import re

    home = _write_home_config(tmp_path, "everos_key", json.dumps({"memory": {"backend": "everos"}}))
    everos_dir = home / ".everos" / "raven"
    everos_dir.mkdir(parents=True)
    (everos_dir / "everos.toml").write_text(
        '[embedding]\nmodel = "m"\napi_key = "sk-x"\nbase_url = "https://api.example.com/v1"\n',
        encoding="utf-8",
    )
    out, _ = _run_doctor_subprocess(home)
    out = " ".join(out.split())
    assert re.search(r"Retrieval:\s*semantic", out), out


def test_memory_retrieval_reaches_the_json_output(tmp_path: Path) -> None:
    home = _write_home_config(tmp_path, "everos_json", json.dumps({"memory": {"backend": "everos"}}))
    import os
    import subprocess
    import sys

    # Same reason as _run_doctor_subprocess: a sandbox HOME only decides where
    # the config is read from while no RAVEN_HOME out-ranks it.
    env = {k: v for k, v in os.environ.items() if k != "RAVEN_HOME"}
    env.update({"HOME": str(home), "COLUMNS": "250"})
    r = subprocess.run(
        [sys.executable, "-m", "raven", "doctor", "--json"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    payload = json.loads(r.stdout[r.stdout.index("{") :])
    assert payload["memory"]["retrieval"] == "keyword-only"


class TestDoctorDoesNotInventASelfManagedRoot:
    """A self-managed install records no root, so doctor must not name one.

    ``everos_root()`` answers with a fallback when nothing is recorded, which
    is right for the code that needs somewhere to write and wrong for the code
    that reports where the memories are. Printing it labelled "Managed by you"
    points the user at a directory that is not theirs and has nothing in it.

    The roles are worse than cosmetic. They were read out of that same
    fabricated root's toml, so an install whose server has embedding
    configured was told its recall was keyword-only. Doctor cannot read the
    user's toml -- that is the whole read-only promise -- so what the server
    reports about itself is the only honest source.
    """

    @staticmethod
    def _collect(monkeypatch, *, caps: dict, reachable: bool = True):
        from raven.cli import doctor_commands as dc

        monkeypatch.setattr("raven.config.update_everos.everos_owned", lambda: False)
        monkeypatch.setattr("raven.config.update_everos.everos_root", lambda: Path("/fallback/everos"))
        monkeypatch.setattr(
            "raven.config.update_everos.everos_role_configured",
            lambda _s: pytest.fail("read the local toml for a root raven does not own"),
        )
        from raven_everos import health as _health

        monkeypatch.setattr(
            _health,
            "probe_capabilities",
            # reports_capabilities is derived from capabilities, not a field.
            lambda *_a, **_kw: _health.CapabilityReport(reachable=reachable, capabilities=caps),
        )
        return dc

    def test_no_root_is_claimed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dc = self._collect(monkeypatch, caps={"llm": True, "embed": True})
        info = dc._probe_memory(SimpleNamespace(memory=SimpleNamespace(backend="everos")))

        assert info.root is None, "named a directory for an install that records none"

    def test_retrieval_follows_what_the_server_reports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dc = self._collect(monkeypatch, caps={"llm": True, "embed": True})
        info = dc._probe_memory(SimpleNamespace(memory=SimpleNamespace(backend="everos")))

        assert info.retrieval == "semantic", "told the user recall was keyword-only while the server has embedding"

    def test_no_embedding_on_the_server_reads_as_keyword_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dc = self._collect(monkeypatch, caps={"llm": True, "embed": False})
        info = dc._probe_memory(SimpleNamespace(memory=SimpleNamespace(backend="everos")))

        assert info.retrieval == "keyword-only"

    def test_an_unreachable_server_claims_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Down is not the same as misconfigured. With nothing to ask, doctor
        has no basis for either answer and must not pick one."""
        dc = self._collect(monkeypatch, caps={}, reachable=False)
        info = dc._probe_memory(SimpleNamespace(memory=SimpleNamespace(backend="everos")))

        assert info.root is None
        assert info.retrieval is None


class TestASelfManagedServerCanStillBeBroken:
    """ "What the server knows about" and "what it built" must not be one list.

    For a not-owned install ``configured`` was the sections the server reports
    as available, and ``unbuilt`` the subset of those it reports as
    unavailable. Over one capability map those conditions are mutually
    exclusive, so ``unbuilt`` -- and with it ``broken`` and the exit code --
    was structurally always empty. A self-managed server whose LLM failed to
    build reported healthy, and any CI gating on ``raven doctor`` saw green.

    Raven cannot read their toml to learn what they configured. It does not
    have to: a server that reports a section as unavailable tried to build it
    and failed, and that is the same evidence.
    """

    @staticmethod
    def _info(monkeypatch, caps: dict):
        from raven.cli import doctor_commands as dc
        from raven_everos import health as _health

        monkeypatch.setattr("raven.config.update_everos.everos_owned", lambda: False)
        monkeypatch.setattr(
            _health,
            "probe_capabilities",
            lambda *_a, **_kw: _health.CapabilityReport(reachable=True, capabilities=caps),
        )
        return dc._probe_memory(SimpleNamespace(memory=SimpleNamespace(backend="everos")))

    def test_a_failed_required_role_is_reported_as_broken(self, monkeypatch: pytest.MonkeyPatch) -> None:
        info = self._info(monkeypatch, {"llm": False, "embed": True})

        assert "llm" in info.unbuilt
        assert info.broken == ["llm"], "a server that cannot build its LLM reported healthy"

    def test_it_reaches_the_exit_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from raven.cli import doctor_commands as dc

        info = self._info(monkeypatch, {"llm": False, "embed": True})
        report = dc.DoctorReport()
        # The earlier gates return first; this case is about the memory one.
        report.paths = SimpleNamespace(config_exists=True, config_valid=True)
        report.config_loaded = True
        report.routing = SimpleNamespace(provider="openai")
        report.memory = info

        assert report.exit_code() == 2, "doctor exited 0 with a memory service that cannot work"

    def test_a_failed_optional_role_costs_quality_not_function(self, monkeypatch: pytest.MonkeyPatch) -> None:
        info = self._info(monkeypatch, {"llm": True, "embed": False})

        assert "embedding" in info.unbuilt
        assert info.broken == [], "a missing embedding is a worse memory, not a broken one"

    def test_a_working_server_is_not_accused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        info = self._info(monkeypatch, {"llm": True, "embed": True})

        assert info.unbuilt == []

    def test_a_role_the_server_says_nothing_about_is_not_claimed_either_way(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``rerank`` absent from the map is silence, not a failure."""
        info = self._info(monkeypatch, {"llm": True, "embed": True})

        assert "rerank" not in info.configured
        assert "rerank" not in info.unbuilt


class TestTheInstallationSection:
    """An upgrade killed part way through leaves an installation that answers
    some questions and not others. Every later verdict in this report is then a
    verdict about the wrong thing, so this one is checked first and, when it
    fails, it is the only one printed."""

    def _fault(self, monkeypatch: pytest.MonkeyPatch, reason: str, detail: str, missing: list[str]) -> None:
        from raven.updates import install_guard as _install_guard

        monkeypatch.setattr(_install_guard, "inspect_install", lambda: _install_guard.InstallFault(reason, detail))
        monkeypatch.setattr(_install_guard, "missing_pieces", lambda: missing)

    def test_a_sound_installation_is_not_mentioned(self, healthy_config: Path) -> None:
        r = runner.invoke(app, ["doctor"])
        assert r.exit_code == 0, r.stdout
        assert "Installation" not in r.stdout

    def test_a_half_written_installation_fails_the_check(
        self, healthy_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fault(
            monkeypatch,
            "incomplete",
            "this installation is missing the packaged page (raven/ui/dist)",
            ["the packaged page (raven/ui/dist)"],
        )
        r = runner.invoke(app, ["doctor"])

        assert r.exit_code == 1, r.stdout
        assert "incomplete" in r.stdout
        assert "install.sh" in r.stdout

    def test_it_outranks_a_config_that_also_looks_wrong(
        self, tmp_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no config at all, the report would normally stop at "not
        configured" -- which is the wrong thing to send a reader to fix."""
        self._fault(monkeypatch, "incomplete", "this installation is missing its own package metadata", ["x"])
        r = runner.invoke(app, ["doctor"])

        assert r.exit_code == 1, r.stdout
        assert "incomplete" in r.stdout
        assert "raven onboard" not in r.stdout

    def test_a_running_upgrade_is_a_note_not_a_failure(
        self, healthy_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is broken yet; the rest of the report is still worth reading."""
        self._fault(monkeypatch, "upgrading", "an upgrade to 9.9.9 is replacing this installation", [])
        r = runner.invoke(app, ["doctor"])

        assert r.exit_code == 0, r.stdout
        assert "9.9.9" in r.stdout
        assert "anthropic" in r.stdout.lower()

    def test_the_verdict_reaches_the_json_output(self, healthy_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._fault(monkeypatch, "incomplete", "this installation is missing the packaged page", ["the page"])
        r = runner.invoke(app, ["doctor", "--json"])

        payload = json.loads(r.stdout[r.stdout.index("{") :])
        assert payload["install"]["complete"] is False
        assert payload["install"]["missing"] == ["the page"]


# ── raven doctor --fix ──────────────────────────────────────────────────
#
# The migrations run at load, so nothing is ever "pending" by the time this
# command looks. What is left to ask about is what they deliberately do not
# decide: a window the user pinned below what the model holds, and a provider
# nothing could resolve. Both are legitimate configurations, which is why they
# are reported and only written with --fix.


def _pinned_config(home: Path, **defaults: object) -> Path:
    cfg = home / ".raven" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "providers": {"anthropic": {"apiKey": "sk-a"}},
                "agents": {
                    "defaults": {
                        "model": "anthropic/claude-opus-4-5",
                        "provider": "anthropic",
                        **defaults,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return cfg


def test_doctor_reports_a_pin_that_caps_the_model(tmp_path, monkeypatch) -> None:
    from raven.cli.doctor_commands import _inspect_config_health
    from raven.config.loader import load_config
    from raven.providers import rates

    # Not 65536: that is the retired default the loader clears on its own, so a
    # fixture using it would be testing the migration instead of this check.
    cfg = _pinned_config(tmp_path, contextWindowTokens=32768)
    monkeypatch.setattr(rates, "resolve_context_window", lambda *a, **k: 1_000_000)

    health = _inspect_config_health(load_config(cfg), fix=False)

    assert any("32,768" in f and "1,000,000" in f for f in health.findings)
    assert health.fixes and not health.applied
    # Reported only: no consent, no write.
    assert json.loads(cfg.read_text())["agents"]["defaults"]["contextWindowTokens"] == 32768


def test_doctor_fix_removes_the_pin_and_keeps_the_file_mode(tmp_path, monkeypatch) -> None:
    from raven.cli.doctor_commands import _inspect_config_health
    from raven.config import loader
    from raven.config.loader import load_config
    from raven.providers import rates

    # Not 65536: that is the retired default the loader clears on its own, so a
    # fixture using it would be testing the migration instead of this check.
    cfg = _pinned_config(tmp_path, contextWindowTokens=32768)
    cfg.chmod(0o600)
    monkeypatch.setattr(rates, "resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(loader, "get_config_path", lambda: cfg)

    health = _inspect_config_health(load_config(cfg), fix=True)

    assert health.applied and not health.fixes
    assert "contextWindowTokens" not in json.loads(cfg.read_text())["agents"]["defaults"]
    # config.json holds providers.*.apiKey, so a replacing writer owns the mode.
    assert cfg.stat().st_mode & 0o777 == 0o600


def test_doctor_says_nothing_about_a_pin_that_matches_the_model(tmp_path, monkeypatch) -> None:
    from raven.cli.doctor_commands import _inspect_config_health
    from raven.config.loader import load_config
    from raven.providers import rates

    cfg = _pinned_config(tmp_path, contextWindowTokens=1_000_000)
    monkeypatch.setattr(rates, "resolve_context_window", lambda *a, **k: 1_000_000)

    assert _inspect_config_health(load_config(cfg), fix=False).findings == []


def test_doctor_fix_reports_a_write_it_could_not_make(tmp_path, monkeypatch) -> None:
    """A read-only home is a reason to say so, not to crash the health check --
    the rest of the report is still worth printing."""
    from raven.cli.doctor_commands import _inspect_config_health
    from raven.config import loader
    from raven.config.loader import load_config
    from raven.providers import rates

    cfg = _pinned_config(tmp_path, contextWindowTokens=32768)
    monkeypatch.setattr(rates, "resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(loader, "get_config_path", lambda: cfg)
    monkeypatch.setattr(
        "raven.cli.doctor_commands._write_config_preserving_mode",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only file system")),
    )

    health = _inspect_config_health(load_config(cfg), fix=True)

    assert any("could not write the fix" in f for f in health.findings)
    assert health.applied == []
    assert json.loads(cfg.read_text())["agents"]["defaults"]["contextWindowTokens"] == 32768


def test_the_fix_writer_survives_a_mode_it_cannot_read(tmp_path, monkeypatch) -> None:
    """Preserving the mode is best-effort: a filesystem that will not answer
    `stat` is not a reason to leave the fix unwritten."""
    from raven.cli.doctor_commands import _write_config_preserving_mode

    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("os.chmod", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))

    _write_config_preserving_mode(cfg, {"agents": {"defaults": {"model": "x/y"}}})

    assert json.loads(cfg.read_text())["agents"]["defaults"]["model"] == "x/y"


def test_doctor_reports_a_config_that_names_no_provider(tmp_path) -> None:
    """The check that had to wait for the explicit-provider rule.

    Before it, ``provider`` defaulted to ``auto`` and blank never happened. After
    it, blank means the load-time migration could not resolve the vendor -- so
    every call falls back to deriving it from the model id, which is the guess
    the rule exists to end.

    The fixture has to be genuinely unresolvable, which is the check's whole
    scope: with a configured vendor that serves the model, the migration fills
    the blank in during ``load_config`` and there is nothing left to report.
    """
    from raven.cli.doctor_commands import _inspect_config_health
    from raven.config.loader import load_config

    cfg = tmp_path / ".raven" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps({"agents": {"defaults": {"model": "some/unclaimed-model", "provider": ""}}}),
        encoding="utf-8",
    )

    health = _inspect_config_health(load_config(cfg), fix=False)

    assert any("provider is not set" in f for f in health.findings)
    assert any("raven provider use" in f for f in health.findings)
    # Reported, never fixed: the migration already tried the derivation and had
    # no answer, so only the user knows which vendor they meant to pay.
    assert not health.fixes


def test_doctor_reads_a_leftover_auto_as_unset_not_as_a_typo(tmp_path) -> None:
    """`auto` is the state this check exists for, and it is reachable.

    `_migrate_auto_provider` leaves the literal in place when it cannot resolve a
    vendor, so a config can still say `auto` after `load_config` -- and
    `Config._match_provider` already treats it as unset (`forced != "auto"`).
    Read as a name instead, it is unroutable, and the user is told to fix a typo
    they did not make while the advice that would help goes unsaid.

    Neither of the checks around this one used a literal `auto`, which is how
    the gap survived to review.
    """
    from raven.cli.doctor_commands import _inspect_config_health
    from raven.config.loader import load_config

    cfg = tmp_path / ".raven" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps({"agents": {"defaults": {"model": "some/unclaimed-model", "provider": "auto"}}}),
        encoding="utf-8",
    )

    health = _inspect_config_health(load_config(cfg), fix=False)

    assert any("provider is not set" in f for f in health.findings)
    assert any("raven provider use" in f for f in health.findings)
    assert any("retired spelling of unset" in f for f in health.findings)
    assert not any("nothing routes to" in f for f in health.findings)
    assert not health.fixes


def test_doctor_reports_a_provider_nothing_routes_to(tmp_path) -> None:
    """A typo written before `provider use` started checking the name. Every
    call then resolves against a vendor that does not exist.
    """
    from raven.cli.doctor_commands import _inspect_config_health
    from raven.config.loader import load_config

    cfg = _pinned_config(tmp_path)
    raw = json.loads(cfg.read_text())
    raw["agents"]["defaults"]["provider"] = "antropic"
    cfg.write_text(json.dumps(raw), encoding="utf-8")

    health = _inspect_config_health(load_config(cfg), fix=False)

    assert any("nothing routes to" in f for f in health.findings)
    assert not health.fixes


def test_doctor_accepts_a_vendor_only_litellm_knows(tmp_path) -> None:
    """The counterweight. Raven carries no spec for mistral, so "no spec of
    ours" cannot be the test -- reporting it as broken would be worse than
    saying nothing.
    """
    from raven.cli.doctor_commands import _inspect_config_health
    from raven.config.loader import load_config

    cfg = _pinned_config(tmp_path)
    raw = json.loads(cfg.read_text())
    raw["agents"]["defaults"]["provider"] = "mistral"
    raw["agents"]["defaults"]["model"] = "mistral/mistral-large-latest"
    cfg.write_text(json.dumps(raw), encoding="utf-8")

    health = _inspect_config_health(load_config(cfg), fix=False)

    assert health.findings == []


def test_doctor_prints_the_config_section_it_found(tmp_path, monkeypatch, capsys) -> None:
    """The renderer, not just the check: a finding nothing prints is a finding
    the user never gets."""
    from raven.cli.doctor_commands import ConfigHealth, DoctorReport, PathsInfo, _render_human_output

    report = DoctorReport(
        config_loaded=True,
        paths=PathsInfo(config_path=str(tmp_path / "config.json"), config_exists=True, config_valid=True),
        config_health=ConfigHealth(
            findings=["contextWindowTokens is pinned to 32,768"],
            fixes=["remove agents.defaults.contextWindowTokens"],
        ),
    )

    _render_human_output(report)

    out = capsys.readouterr().out
    assert "Config" in out
    assert "pinned to 32,768" in out
    assert "raven doctor --fix" in out
    assert "remove agents.defaults.contextWindowTokens" in out


def test_doctor_prints_what_the_fix_applied(tmp_path, capsys) -> None:
    from raven.cli.doctor_commands import ConfigHealth, DoctorReport, PathsInfo, _render_human_output

    report = DoctorReport(
        config_loaded=True,
        paths=PathsInfo(config_path=str(tmp_path / "config.json"), config_exists=True, config_valid=True),
        config_health=ConfigHealth(applied=["remove agents.defaults.contextWindowTokens"]),
    )

    _render_human_output(report)

    out = capsys.readouterr().out
    assert "fixed" in out
    assert "raven doctor --fix" not in out, "nothing left to apply, so nothing to advertise"


# ------------------------------------------------------- tool capabilities


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """These credentials resolve from the environment too, so a developer who
    exported one would see these assert the wrong branch.

    ``OPENROUTER_API_KEY`` counts as much as the search key: it is what the
    media family falls back to, so exporting it turns the "nothing to borrow"
    rows into "borrowing from the environment" rows.
    """
    for var in ("SERPER_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_doctor_lists_a_capability_that_is_not_configured(healthy_config: Path) -> None:
    """The reason this section exists: an unconfigured tool is not registered,
    so without it nothing in the running system says the capability is there."""
    result = runner.invoke(app, ["doctor"])

    assert "Tool capabilities" in result.stdout
    assert "web_search" in result.stdout
    assert "serper.dev" in result.stdout, "a deployer cannot act without being told where to go"
    assert "tools.web.providers.serper.apiKey" in result.stdout


def test_doctor_says_a_paid_capability_bills_before_it_is_switched_on(healthy_config: Path) -> None:
    result = runner.invoke(app, ["doctor"])

    assert "Billed per image." in result.stdout
    assert "prepaid OpenRouter credit" in result.stdout, "video cannot run at all without it"


def test_doctor_reports_a_configured_capability_and_where_its_key_came_from(tmp_config: Path, tmp_path: Path) -> None:
    cfg = Config()
    cfg.agents.defaults.model = "anthropic/claude-sonnet-4-5"
    cfg.agents.defaults.workspace = str(tmp_path / "workspace")
    cfg.providers.anthropic.api_key = "sk-fake"
    cfg.providers.openrouter.api_key = "sk-or-fake"
    cfg.tools.media.image.model = "some/model"
    save_config(cfg)

    result = runner.invoke(app, ["doctor"])

    assert "image_generate" in result.stdout
    assert "borrowed" in result.stdout, "the deployer should see it reused a key, not that it needs one"


def test_doctor_does_not_offer_a_borrow_it_cannot_make(healthy_config: Path) -> None:
    """This config has no OpenRouter key, so "reuse the one you have" is false.

    It is false in the expensive direction: acting on it means setting a model,
    getting a registered tool -- a model alone counts -- and every call failing
    on a credential the deployer was told they already had.
    """
    result = runner.invoke(app, ["doctor"])

    assert "image_generate" in result.stdout
    assert "borrow" not in result.stdout, "claimed a reuse with nothing to reuse"
    assert "tools.media.image.apiKey" in result.stdout, "must name the key it actually needs"
    assert "OPENROUTER_API_KEY" in result.stdout


def test_doctor_flags_a_capability_registered_without_a_key(tmp_config: Path, tmp_path: Path) -> None:
    """A media model with no key from any source is offered to the model and
    fails on every call. A satisfied row is the one thing that must not say."""
    cfg = Config()
    cfg.agents.defaults.model = "anthropic/claude-sonnet-4-5"
    cfg.agents.defaults.workspace = str(tmp_path / "workspace")
    cfg.providers.anthropic.api_key = "sk-fake"
    cfg.tools.media.image.model = "some/model"
    save_config(cfg)

    result = runner.invoke(app, ["doctor"])

    assert "no key resolves" in result.stdout
    assert "tools.media.image.apiKey" in result.stdout
    # Warned, not failed. Unlike a memory role the server could not build, this
    # failure is loud where it happens -- the tool returns its missing-key error
    # to the model -- so the section stays advisory, as the rest of it is.
    assert result.exit_code == 0

    # Two fields rather than one, because collapsing them is what hid this
    # state: registered *and* unusable is not reachable through either alone.
    payload = json.loads(runner.invoke(app, ["doctor", "--json"]).stdout)
    image = next(c for c in payload["tools"]["capabilities"] if c["tool"] == "image_generate")
    assert image["configured"] is True and image["has_credential"] is False


def test_an_unconfigured_capability_is_not_a_failure(healthy_config: Path) -> None:
    """An install without image generation is a choice, not a fault."""
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0


def test_tool_capabilities_reach_the_json_output(healthy_config: Path) -> None:
    result = runner.invoke(app, ["doctor", "--json"])

    payload = json.loads(result.stdout)
    tools = payload["tools"]["capabilities"]
    by_name = {c["tool"]: c for c in tools}
    assert "web_search" in by_name and "web_fetch" in by_name
    assert by_name["web_search"]["configured"] is False
    assert by_name["web_search"]["obtain_from"] == "https://serper.dev"
    assert by_name["web_fetch"]["configured"] is True
    assert by_name["image_generate"]["key_path"] == "tools.media.image.apiKey", (
        "the key path is not the model path this row switches on"
    )


def test_a_config_path_is_never_split_across_lines(healthy_config: Path) -> None:
    """These rows exist to be copied. A key wrapped mid-path is unusable, which
    is why each fact is printed on its own line rather than in a sentence."""
    result = runner.invoke(app, ["doctor"])

    for path in ("tools.web.providers.serper.apiKey", "tools.media.image.model", "SERPER_API_KEY"):
        assert path in result.stdout, f"{path} was broken across a line wrap"


def _search_config(tmp_path: Path, *, key: bool, off: bool) -> None:
    """One cell of the web_search state matrix, persisted.

    ``key`` and ``off`` are independent in production -- a deployment can set
    neither, either, or both -- so they are independent here.
    """
    cfg = Config()
    cfg.agents.defaults.model = "anthropic/claude-sonnet-4-5"
    cfg.agents.defaults.workspace = str(tmp_path / "workspace")
    cfg.providers.anthropic.api_key = "sk-fake"
    if key:
        cfg.tools.web.search.api_key = "sk-serper"
    if off:
        cfg.tools.disabled_tools = ["web_search"]
    save_config(cfg)


def _switched_off_search(tmp_path: Path) -> None:
    """A credentialed web_search that the deployment has switched off by name."""
    _search_config(tmp_path, key=True, off=True)


def test_doctor_says_a_capability_is_switched_off_rather_than_ticking_it(tmp_config: Path, tmp_path: Path) -> None:
    """A key plus `disabledTools` used to print a green tick for a tool the
    agent does not hold -- the report claiming a capability is on offer when
    Raven has removed it."""
    _switched_off_search(tmp_path)

    result = runner.invoke(app, ["doctor"])

    assert "tools.disabledTools" in result.stdout
    # Not the unconfigured path: the key is set, and telling them to set it
    # again is how a report sends someone in a circle.
    assert "switch on:" not in result.stdout.split("web_search")[-1][:200]


def test_the_switched_off_state_reaches_the_json_output(tmp_config: Path, tmp_path: Path) -> None:
    _switched_off_search(tmp_path)

    result = runner.invoke(app, ["doctor", "--json"])
    payload = json.loads(result.stdout)

    row = next(c for c in payload["tools"]["capabilities"] if c["tool"] == "web_search")
    assert row["configured"] is True, "the key is set; calling it unconfigured is the wrong repair"
    assert row["disabled"] is True


@pytest.mark.parametrize("key", [True, False], ids=["keyed", "keyless"])
def test_the_off_switch_is_named_whether_or_not_a_key_is_set(key: bool, tmp_config: Path, tmp_path: Path) -> None:
    """The cell the first version of this rendering got wrong.

    With no key, the row used to print only the credential advice -- so a
    deployer could set `tools.web.search.apiKey`, restart, and still not have
    search, because `_apply_disabled_tools` removes it either way.
    """
    _search_config(tmp_path, key=key, off=True)

    result = runner.invoke(app, ["doctor"])

    assert "tools.disabledTools" in result.stdout


@pytest.mark.parametrize(
    ("key", "off", "configured", "disabled"),
    [(True, True, True, True), (True, False, True, False), (False, True, False, True), (False, False, False, False)],
    ids=["keyed-off", "keyed-on", "keyless-off", "keyless-on"],
)
def test_the_json_row_reports_the_two_states_independently(
    key: bool, off: bool, configured: bool, disabled: bool, tmp_config: Path, tmp_path: Path
) -> None:
    """Both flags, all four combinations. Collapsing either into the other is
    what made the report tell a deployer to set a key that was already set."""
    _search_config(tmp_path, key=key, off=off)

    payload = json.loads(runner.invoke(app, ["doctor", "--json"]).stdout)
    row = next(c for c in payload["tools"]["capabilities"] if c["tool"] == "web_search")

    assert row["configured"] is configured
    assert row["disabled"] is disabled


class TestDoctorWithoutTheMemoryPlugin:
    """The backend ships as its own distribution, and this install lacks it.

    ``memory.backend`` still defaults to ``everos``, so the report has to say
    what is missing instead of dying on the import: doctor is the command a
    person runs precisely when something is wrong.
    """

    def test_the_report_names_the_distribution_and_fails(self, healthy_config: Path) -> None:
        with everos_plugin_absent():
            r = runner.invoke(app, ["doctor"])

        assert r.exit_code == 2, r.stdout
        assert "everos-memory" in r.stdout
        assert "memory.backend" in r.stdout

    def test_the_json_report_carries_it_as_a_field(self, healthy_config: Path) -> None:
        with everos_plugin_absent():
            r = runner.invoke(app, ["doctor", "--json"])

        payload = json.loads(r.stdout)
        assert payload["memory"]["plugin_missing"] is True
        assert payload["memory"]["configured"] == []

    def test_an_installed_but_broken_plugin_is_not_called_absent(self) -> None:
        """A plugin whose own import fails is a bug to fix, not a degrade to
        report -- swallowing it would hide the fault behind an install hint."""
        from raven.cli import doctor_commands as dc

        with everos_plugin_broken(), pytest.raises(ImportError):
            dc._probe_memory(SimpleNamespace(memory=SimpleNamespace(backend="everos")))


def test_doctor_names_the_libreoffice_it_found(healthy_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The row exists to answer "will my decks render", so it prints the path
    that would be run rather than a tick."""
    monkeypatch.setattr("raven.utils.office.find_soffice", lambda: "/usr/bin/soffice")

    result = runner.invoke(app, ["doctor"])

    assert "LibreOffice" in result.stdout
    assert "/usr/bin/soffice" in result.stdout
    assert "not found" not in result.stdout.split("External tools")[1]


def test_doctor_reports_libreoffice_missing_with_the_command_that_installs_it(
    healthy_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing LibreOffice is reported and does not move the exit code: decks
    still build, the render-truth gates simply do not run. The remedy is printed
    because this is not a package `uv` will fetch."""
    monkeypatch.setattr("raven.utils.office.find_soffice", lambda: None)
    monkeypatch.setattr("raven.utils.office.install_hint", lambda: "apt install libreoffice")

    result = runner.invoke(app, ["doctor"])

    external = result.stdout.split("External tools")[1]
    assert "not found" in external
    assert "apt install libreoffice" in external


def test_doctor_finds_the_windows_install_the_remedy_it_prints_creates(
    healthy_config: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Windows remedy is `winget install TheDocumentFoundation.LibreOffice`,
    which puts nothing on PATH. `raven doctor` has to find what it told the user
    to install, or the row calls LibreOffice missing on a host that has it and
    the ppt engine's render gate says the same.
    """
    from raven.utils import office

    launcher = tmp_path / "Program Files" / "LibreOffice" / "program" / "soffice.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("")
    monkeypatch.setattr(office.sys, "platform", "win32")
    monkeypatch.setattr(office.shutil, "which", lambda name: None)
    for variable in office._WINDOWS_PROGRAM_ROOT_VARS:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))

    assert doctor_commands._gather_external_tools().soffice == str(launcher)


# --------------------------------------------------------------------------- chromium


def _browsers_registry(directory: Path, *, chromium: str = "1234", shell: str = "1234") -> Path:
    registry = directory / "browsers.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps(
            {
                "browsers": [
                    {"name": "chromium", "revision": chromium},
                    {"name": "chromium-headless-shell", "revision": shell},
                    {"name": "firefox", "revision": "9999"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return registry


def _downloaded(root: Path, directory: str) -> None:
    (root / directory).mkdir(parents=True, exist_ok=True)
    (root / directory / "INSTALLATION_COMPLETE").write_text("")


def test_both_chromium_markers_read_as_downloaded(tmp_path: Path) -> None:
    registry = _browsers_registry(tmp_path)
    root = tmp_path / "cache"
    _downloaded(root, "chromium-1234")
    _downloaded(root, "chromium_headless_shell-1234")

    assert doctor_commands._chromium_markers(registry, root) == (True, True)


def test_chromium_alone_is_not_a_working_browser(tmp_path: Path) -> None:
    """raven's driver launches headless by default, which runs the separate
    headless-shell install -- a cache holding only chromium-<rev> still cannot
    serve the browser tool, so the shell's absence must be reported."""
    registry = _browsers_registry(tmp_path)
    root = tmp_path / "cache"
    _downloaded(root, "chromium-1234")

    assert doctor_commands._chromium_markers(registry, root) == (True, False)


def test_a_marker_for_another_revision_does_not_count(tmp_path: Path) -> None:
    """The revision comes from the installed package's own registry: a cache
    left by an older playwright is not the browser this one would launch."""
    registry = _browsers_registry(tmp_path, chromium="1300", shell="1300")
    root = tmp_path / "cache"
    _downloaded(root, "chromium-1234")
    _downloaded(root, "chromium_headless_shell-1234")

    assert doctor_commands._chromium_markers(registry, root) == (False, False)


def test_the_browsers_path_env_is_honored(tmp_path: Path) -> None:
    root = doctor_commands._resolve_browsers_root(tmp_path / "pkg", str(tmp_path / "elsewhere"))
    assert root == tmp_path / "elsewhere"


def test_browsers_path_zero_means_package_local(tmp_path: Path) -> None:
    root = doctor_commands._resolve_browsers_root(tmp_path / "pkg", "0")
    assert root == tmp_path / "pkg" / ".local-browsers"


def test_no_env_falls_back_to_the_user_cache(tmp_path: Path) -> None:
    root = doctor_commands._resolve_browsers_root(tmp_path / "pkg", None)
    assert root.name == "ms-playwright"
    assert Path.home() in root.parents


def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assertions on literal path fragments must not depend on where Rich
    wraps: xdist's longer tmp paths cross the 80-column default and split a
    token like pw-cache mid-word."""
    from rich.console import Console

    monkeypatch.setattr(doctor_commands, "console", Console(width=400))


def _fake_playwright(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, markers: tuple[str, ...]) -> Path:
    """Install a fake playwright package dir plus a cache holding ``markers``."""
    package_dir = tmp_path / "playwright"
    _browsers_registry(package_dir / "driver" / "package")
    cache = tmp_path / "pw-cache"
    for name in markers:
        _downloaded(cache, name)
    monkeypatch.setattr(doctor_commands, "_playwright_package_dir", lambda: package_dir)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    return cache


def test_doctor_reports_a_missing_browser_package(healthy_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No playwright means no `playwright install` to run: the remedy is the
    installer, whose engines carry the library. Non-fatal, like LibreOffice."""
    monkeypatch.setattr(doctor_commands, "_playwright_package_dir", lambda: None)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.stdout
    external = " ".join(result.stdout.split("External tools")[1].split())
    assert "Chromium:" in external
    assert "install.sh" in external


def test_doctor_reports_an_undownloaded_chromium_with_the_command_that_installs_it(
    healthy_config: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_playwright(tmp_path, monkeypatch, markers=("chromium-1234",))

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.stdout
    flat = " ".join(result.stdout.split())
    assert "playwright install chromium" in flat
    assert "install.sh" not in flat.split("Chromium:")[1].split("Design render:")[0]


def test_doctor_names_the_browser_cache_it_found(
    healthy_config: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wide_console(monkeypatch)
    _fake_playwright(tmp_path, monkeypatch, markers=("chromium-1234", "chromium_headless_shell-1234"))

    result = runner.invoke(app, ["doctor"])

    flat = " ".join(result.stdout.split())
    chromium_row = flat.split("Chromium:")[1].split("Design render:")[0]
    assert "pw-cache" in chromium_row
    assert "playwright install" not in chromium_row
    assert "install.sh" not in chromium_row


def test_the_design_lane_is_its_own_row(healthy_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The browser tool and the design engine find chromium by different
    policies, so one verdict labeled for both would be wrong for one of them."""
    monkeypatch.setattr(doctor_commands, "_playwright_package_dir", lambda: None)
    monkeypatch.setattr(doctor_commands.shutil, "which", lambda name: None)

    result = runner.invoke(app, ["doctor"])

    external = " ".join(result.stdout.split("External tools")[1].split())
    assert "Design render:" in external
    assert "no chromium found" in external


def test_the_browser_state_reaches_the_json_output(
    healthy_config: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = _fake_playwright(tmp_path, monkeypatch, markers=("chromium-1234", "chromium_headless_shell-1234"))

    r = runner.invoke(app, ["doctor", "--json"])

    data = json.loads(r.stdout)
    external = data["external_tools"]
    assert external["browser_package"] is True
    assert external["chromium"] is True
    assert external["headless_shell"] is True
    assert external["browsers_root"] == str(cache)


# --------------------------------------------------------------------------- --install-summary

_SUMMARY_LABELS = ("Long-term memory", "Design engine", "PPT engine", "Deck preview", "Browser")


def test_install_summary_answers_without_a_config(tmp_config: Path) -> None:
    """The whole point of the flag: five verdicts and exit 0 on a machine whose
    missing config would fail every other doctor check."""
    assert not tmp_config.exists()

    r = runner.invoke(app, ["doctor", "--install-summary"])

    assert r.exit_code == 0, r.stdout
    for label in _SUMMARY_LABELS:
        assert label in r.stdout, f"missing row: {label}"
    assert "Paths" not in r.stdout
    assert "raven onboard" not in r.stdout


def test_install_summary_answers_on_an_invalid_config(tmp_config: Path) -> None:
    tmp_config.write_text("{not json", encoding="utf-8")

    r = runner.invoke(app, ["doctor", "--install-summary"])

    assert r.exit_code == 0, r.stdout


def test_install_summary_prints_exactly_five_rows(tmp_config: Path) -> None:
    r = runner.invoke(app, ["doctor", "--install-summary"])

    labeled = [line for line in r.stdout.splitlines() if any(label in line for label in _SUMMARY_LABELS)]
    assert len(labeled) == 5, r.stdout


def test_install_summary_browser_row_names_the_installer_when_the_package_is_missing(
    tmp_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_commands, "_playwright_package_dir", lambda: None)

    r = runner.invoke(app, ["doctor", "--install-summary"])

    browser_row = " ".join(r.stdout.split()).split("Browser:")[1]
    assert "engines carry the browser library" in browser_row


def test_install_summary_browser_row_names_the_download_when_only_the_binary_is_missing(
    tmp_config: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_playwright(tmp_path, monkeypatch, markers=())

    r = runner.invoke(app, ["doctor", "--install-summary"])

    browser_row = " ".join(r.stdout.split()).split("Browser:")[1]
    assert "playwright install chromium" in browser_row


def test_install_summary_browser_row_ticks_a_complete_download(
    tmp_config: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wide_console(monkeypatch)
    _fake_playwright(tmp_path, monkeypatch, markers=("chromium-1234", "chromium_headless_shell-1234"))

    r = runner.invoke(app, ["doctor", "--install-summary"])

    browser_row = " ".join(r.stdout.split()).split("Browser:")[1]
    assert "pw-cache" in browser_row
    assert "playwright install" not in browser_row
    assert "install.sh" not in browser_row
