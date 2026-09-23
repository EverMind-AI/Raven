"""Remembered subagent test verdicts and their fingerprint-based invalidation."""

from __future__ import annotations

import json
from pathlib import Path

from raven.agent.subagent.probe_state import LastTest, Remedy, TestStateStore, fingerprint
from raven.config.schema import ThirdPartyCliSubagentConfig, ThirdPartyOpenAISubagentConfig


def _cli(**over) -> ThirdPartyCliSubagentConfig:
    base = {"name": "Coder", "command": "claude -p {prompt}"}
    return ThirdPartyCliSubagentConfig(**{**base, **over})


def _openai(**over) -> ThirdPartyOpenAISubagentConfig:
    base = {"name": "DeepResearcher", "base_url": "https://x/v1", "model": "m1", "api_key": "k1"}
    return ThirdPartyOpenAISubagentConfig(**{**base, **over})


def test_record_then_load_round_trips(tmp_path: Path) -> None:
    store = TestStateStore(path=tmp_path / "state.json")
    cfg = _cli()
    store.record(cfg, "config", ok=False, detail="exited 1: ProviderAuthError", tested_at_ms=1700)
    got = store.load([(cfg, "config")])
    assert got == {"config:Coder": LastTest(ok=False, detail="exited 1: ProviderAuthError", tested_at_ms=1700)}


def test_a_failed_verdict_remembers_the_fix_it_named(tmp_path: Path) -> None:
    """The fix is part of the verdict, so the row can still draw it after a reload.

    Without it on disk the sheet would go back to the English sentence the
    moment the page was reopened, which is when a reader who went off to a
    terminal comes back to press Test.
    """
    store = TestStateStore(path=tmp_path / "state.json")
    cfg = _cli()
    store.record(
        cfg,
        "config",
        ok=False,
        detail="no usable credential",
        tested_at_ms=1700,
        remedy=Remedy("setup", "hermes model"),
    )
    got = store.load([(cfg, "config")])["config:Coder"]
    assert got.remedy == Remedy("setup", "hermes model")

    # A verdict with no fix writes no `remedy` key, so a file written before this
    # field existed and one written after for a plain failure read the same.
    store.record(cfg, "config", ok=False, detail="exited 1", tested_at_ms=1800)
    raw = json.loads((tmp_path / "state.json").read_text())
    assert "remedy" not in raw["verdicts"][0]
    assert store.load([(cfg, "config")])["config:Coder"].remedy is None


def test_a_remembered_fix_that_is_not_one_is_dropped(tmp_path: Path) -> None:
    """A hand-edited or stale file cannot put a kind on the page it has no words for."""
    path = tmp_path / "state.json"
    store = TestStateStore(path=path)
    cfg = _cli()
    for junk in ({"kind": "reboot"}, "sign_in", ["sign_in"], {"command": "x"}):
        store.record(cfg, "config", ok=False, detail="no", tested_at_ms=1)
        raw = json.loads(path.read_text())
        raw["verdicts"][0]["remedy"] = junk
        path.write_text(json.dumps(raw))
        got = store.load([(cfg, "config")])["config:Coder"]
        assert got.remedy is None, junk
        assert got.detail == "no", "the verdict itself still loads"


def test_changing_the_command_discards_the_verdict(tmp_path: Path) -> None:
    store = TestStateStore(path=tmp_path / "state.json")
    store.record(_cli(), "config", ok=False, detail="bad", tested_at_ms=1700)
    assert store.load([(_cli(command="claude -p {prompt} --verbose"), "config")]) == {}


def test_changing_the_api_key_discards_the_verdict(tmp_path: Path) -> None:
    # Fixing a rejected key must clear the old failure, or the page keeps showing a
    # verdict the user has already acted on.
    store = TestStateStore(path=tmp_path / "state.json")
    store.record(_openai(), "config", ok=False, detail="401", tested_at_ms=1700)
    assert store.load([(_openai(api_key="k2"), "config")]) == {}


def test_redescribing_or_toggling_preserves_the_verdict(tmp_path: Path) -> None:
    # Neither changes whether the agent runs, so a still-true verdict must survive.
    # This is what the fingerprint's field list buys, so it is worth pinning.
    store = TestStateStore(path=tmp_path / "state.json")
    store.record(_cli(), "config", ok=True, detail="ran", tested_at_ms=1700)
    assert "config:Coder" in store.load([(_cli(description="new words", enabled=False), "config")])


def test_a_rename_does_not_find_the_verdict(tmp_path: Path) -> None:
    # Intended, and pinned so nobody later "fixes" it into a stale-verdict bug: the
    # record is keyed source:name because that is how the page looks one up, so a
    # renamed agent finds nothing. Re-testing is one click.
    store = TestStateStore(path=tmp_path / "state.json")
    store.record(_cli(), "config", ok=True, detail="ran", tested_at_ms=1700)
    assert store.load([(_cli(name="Coder2"), "config")]) == {}


def test_source_is_part_of_the_key(tmp_path: Path) -> None:
    store = TestStateStore(path=tmp_path / "state.json")
    cfg = _cli(name="codex", command="codex exec {prompt}")
    store.record(cfg, "preset", ok=True, detail="ran", tested_at_ms=1700)
    assert store.load([(cfg, "config")]) == {}
    assert "preset:codex" in store.load([(cfg, "preset")])


def test_record_replaces_rather_than_appends(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = TestStateStore(path=path)
    cfg = _cli()
    store.record(cfg, "config", ok=False, detail="first", tested_at_ms=1700)
    store.record(cfg, "config", ok=True, detail="second", tested_at_ms=1800)
    assert len(json.loads(path.read_text())["verdicts"]) == 1
    assert store.load([(cfg, "config")])["config:Coder"].detail == "second"


def test_stored_payload_carries_no_api_key(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    TestStateStore(path=path).record(_openai(api_key="sk-super-secret"), "config", ok=True, detail="ok", tested_at_ms=1)
    assert "sk-super-secret" not in path.read_text()


def test_missing_file_yields_no_verdicts(tmp_path: Path) -> None:
    assert TestStateStore(path=tmp_path / "absent.json").load([(_cli(), "config")]) == {}


def test_malformed_file_yields_no_verdicts(tmp_path: Path) -> None:
    # The page must not break because this convenience file is corrupt.
    path = tmp_path / "state.json"
    path.write_text("{not json at all")
    assert TestStateStore(path=path).load([(_cli(), "config")]) == {}


def test_unwritable_directory_does_not_raise(tmp_path: Path) -> None:
    doomed = tmp_path / "nope"
    doomed.write_text("i am a file, not a directory")
    TestStateStore(path=doomed / "state.json").record(_cli(), "config", ok=True, detail="ok", tested_at_ms=1)


def test_fingerprint_ignores_identity_fields_and_tracks_execution_fields() -> None:
    assert fingerprint(_cli()) == fingerprint(_cli(name="other", description="d", enabled=False))
    assert fingerprint(_cli()) != fingerprint(_cli(cwd="/tmp"))
    assert fingerprint(_cli()) != fingerprint(_cli(env={"A": "b"}))
    assert fingerprint(_openai()) != fingerprint(_openai(base_url="https://y/v1"))
    assert fingerprint(_openai()) != fingerprint(_openai(model="m2"))
