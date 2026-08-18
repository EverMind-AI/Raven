"""Tests for ``raven.config.loader.load_config``.

Covers the migrations that drop / relocate retired blocks from old
configs, plus the default-config fallback path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven.config.loader import CURRENT_CONFIG_VERSION, _stamp_path, drain_migration_notices, load_config


def _write(path: Path, body: dict) -> None:
    path.write_text(json.dumps(body), encoding="utf-8")


def test_missing_file_uses_defaults(tmp_path: Path) -> None:
    """No file → default Config — loader must not raise."""
    cfg = load_config(tmp_path / "does_not_exist.json")
    # AgentDefaults no longer carries the everos field;
    # check a stable default instead.
    assert cfg.agents.defaults.max_tool_iterations == 40


def test_legacy_cron_forward_channels_stripped(tmp_path: Path) -> None:
    """Old configs may still carry ``cron.forward_channels`` (retired with
    trigger-time delivery routing). Nested schema models ignore extra keys,
    so this is not a crash guard — the strip exists so stale keys don't
    linger silently and the one-time migration is logged."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "cron": {
                "forwardChannels": ["*"],
                "forward_channels": ["telegram"],
                "defaultTimezone": "UTC",
            },
        },
    )
    cfg = load_config(p)
    assert cfg.cron.default_timezone == "UTC"
    assert not hasattr(cfg.cron, "forward_channels")


def test_legacy_everos_block_silently_dropped(tmp_path: Path) -> None:
    """Old configs may still carry ``agents.defaults.everos``. The
    migration strips it so model_validate doesn't reject the file."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "agents": {
                "defaults": {
                    "everos": {"enabled": True, "enableSkill": True},
                },
            },
        },
    )
    cfg = load_config(p)
    assert not hasattr(cfg.agents.defaults, "everos")


def test_legacy_everos_skill_light_relocated_under_agents_defaults(
    tmp_path: Path,
) -> None:
    """Old configs put ``everosSkillLight`` under ``agents.defaults``.
    The migration removes it from that location (the new home is under
    ``skillForge.everos``; see test_config_raven_loader for the
    receiving side)."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "agents": {
                "defaults": {
                    "everosSkillLight": {"enabled": True},
                },
            },
        },
    )
    cfg = load_config(p)
    assert not hasattr(cfg.agents.defaults, "everosSkillLight")
    assert not hasattr(cfg.agents.defaults, "everos_skill_light")


def test_legacy_everos_skill_light_retired_keys_stripped() -> None:
    """everosSkillLight carrying the retired minMessages/minToolCalls must
    relocate to skillForge.everos with those keys dropped (EverOSConfig is
    extra='forbid'), while the surviving fields are kept."""
    from raven.config.loader import _migrate_config

    out = _migrate_config(
        {
            "agents": {
                "defaults": {
                    "everosSkillLight": {
                        "enabled": True,
                        "minMessages": 4,
                        "minToolCalls": 2,
                        "maxSkillsTopK": 5,
                    },
                },
            },
        },
        pop_extension_keys=False,
    )
    everos = out["skillForge"]["everos"]
    assert "minMessages" not in everos
    assert "minToolCalls" not in everos
    assert everos["maxSkillsTopK"] == 5
    assert everos["enabled"] is True


def test_legacy_everos_skill_light_retired_keys_stripped_snake_case() -> None:
    """snake_case variant (min_messages / min_tool_calls) is stripped too."""
    from raven.config.loader import _migrate_config

    out = _migrate_config(
        {
            "agents": {
                "defaults": {
                    "everos_skill_light": {
                        "min_messages": 4,
                        "min_tool_calls": 2,
                        "enabled": False,
                    },
                },
            },
        },
        pop_extension_keys=False,
    )
    everos = out["skillForge"]["everos"]
    assert "min_messages" not in everos
    assert "min_tool_calls" not in everos


def test_corrupted_json_falls_back_to_defaults(tmp_path: Path) -> None:
    """A mid-write race can leave the file half-flushed; tolerate it."""
    p = tmp_path / "config.json"
    p.write_text("{this is not json", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.agents.defaults.max_tool_iterations == 40


def test_schema_validation_error_raises(tmp_path: Path) -> None:
    """A user / programmer config error must NOT silently fall back to
    defaults — that masks misconfig as "feature X did nothing"."""
    p = tmp_path / "config.json"
    # ``max_tool_iterations`` is an int — pass a string to force a
    # pydantic ValidationError, which is a ValueError subclass we
    # explicitly re-raise rather than swallow.
    _write(
        p,
        {
            "agents": {"defaults": {"max_tool_iterations": "not-an-int"}},
        },
    )
    with pytest.raises(ValueError, match="schema validation"):
        load_config(p)


def test_read_raw_or_raise_absent_returns_empty(tmp_path: Path) -> None:
    from raven.config.loader import read_raw_or_raise

    assert read_raw_or_raise(tmp_path / "nope.json") == {}


def test_read_raw_or_raise_valid(tmp_path: Path) -> None:
    from raven.config.loader import read_raw_or_raise

    p = tmp_path / "c.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    assert read_raw_or_raise(p) == {"a": 1}


def test_read_raw_or_raise_malformed_raises(tmp_path: Path) -> None:
    from raven.config.loader import ConfigReadError, read_raw_or_raise

    p = tmp_path / "bad.json"
    p.write_text("{  // comment\n}", encoding="utf-8")
    with pytest.raises(ConfigReadError):
        read_raw_or_raise(p)


def test_load_config_malformed_warns_loudly_and_uses_defaults(tmp_path: Path, capsys) -> None:
    from raven.config.loader import load_config
    from raven.config.schema import Config

    p = tmp_path / "bad.json"
    p.write_text("{  // comment\n}", encoding="utf-8")
    cfg = load_config(p)  # must NOT raise (boot resilience)
    assert isinstance(cfg, Config)
    assert "IGNORING" in capsys.readouterr().err  # loud stderr warning, not silent


def test_read_raw_or_raise_empty_file_is_empty_dict(tmp_path: Path) -> None:
    from raven.config.loader import read_raw_or_raise

    p = tmp_path / "empty.json"
    p.write_text("   \n", encoding="utf-8")
    assert read_raw_or_raise(p) == {}  # empty = no data to lose, not malformed


def test_read_raw_or_raise_json_null_is_empty_dict(tmp_path: Path) -> None:
    from raven.config.loader import read_raw_or_raise

    p = tmp_path / "null.json"
    p.write_text("null", encoding="utf-8")
    assert read_raw_or_raise(p) == {}  # valid JSON but not an object -> {} (no AttributeError)


def test_config_read_error_is_not_runtimeerror() -> None:
    # Intentional: the CLI write commands wrap ops in `except RuntimeError`
    # (OAuth-refusal etc.); ConfigReadError must NOT be a RuntimeError so a parse
    # error bypasses those and reaches the single run() handler. Do not "fix"
    # this to RuntimeError.
    from raven.config.loader import ConfigReadError

    assert not issubclass(ConfigReadError, RuntimeError)
    assert issubclass(ConfigReadError, Exception)


def test_bad_config_warns_exactly_once(tmp_path: Path, capsys) -> None:
    """The user-visible bad-config warning fires once per path per process,
    even across repeated ``load_config`` calls (status/doctor load twice)."""
    p = tmp_path / "config.json"
    p.write_text('{"providers": {},}', encoding="utf-8")
    load_config(p)
    load_config(p)
    captured = capsys.readouterr()
    assert (captured.out + captured.err).count("not valid JSON") == 1


def test_bad_config_warns_again_after_recovery(tmp_path: Path, capsys) -> None:
    """bad -> fixed -> bad again must warn on the second breakage.

    The dedup exists to silence repeated loads of the same broken state
    within one command; in a long-lived process (the TUI RPC server calls
    load_config every turn) a permanent suppression would let a later
    re-breakage run silently on defaults forever."""
    p = tmp_path / "config.json"
    p.write_text('{"providers": {},}', encoding="utf-8")
    load_config(p)
    p.write_text("{}", encoding="utf-8")
    load_config(p)
    p.write_text('{"agents": {},}', encoding="utf-8")
    load_config(p)
    captured = capsys.readouterr()
    assert (captured.out + captured.err).count("not valid JSON") == 2


# ── The retired 65536 context-window pin ────────────────────────────────
#
# Pre-0.1.11 bootstraps dumped every schema default to disk, and back then
# ``contextWindowTokens`` defaulted to 65536. A pin outranks the model's real
# window by design, so on upgraded installs that fossil silently caps every
# model at 64k. It is cleared once, under a watermark kept in a sidecar next to
# the config (see ``_stamp_path``) -- the value itself carries no provenance, so
# the watermark is the only thing separating "we planted this" from "the user
# chose this".


def _defaults(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["agents"]["defaults"]


def test_legacy_context_window_pin_is_dropped_and_stamped(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"contextWindowTokens": 65536, "model": "anthropic/claude-opus-4-5"}}})

    cfg = load_config(p)

    assert cfg.agents.defaults.context_window_tokens is None
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert "contextWindowTokens" not in on_disk["agents"]["defaults"]
    # Untouched neighbours: the write is surgical, not a re-dump of the model.
    assert on_disk["agents"]["defaults"]["model"] == "anthropic/claude-opus-4-5"
    # And no key of ours lands in the user's file -- Config is extra='forbid',
    # so a stamp in there is a hard boot failure for any build without it.
    assert set(on_disk) == {"agents"}
    assert json.loads(_stamp_path(p).read_text(encoding="utf-8")) == {"version": CURRENT_CONFIG_VERSION}


def test_legacy_context_window_pin_dropped_in_snake_case_too(tmp_path: Path) -> None:
    """Configs in the wild use either casing; the schema accepts both."""
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"context_window_tokens": 65536}}})

    assert load_config(p).agents.defaults.context_window_tokens is None
    assert "context_window_tokens" not in _defaults(p)


def test_context_window_pin_survives_once_stamped(tmp_path: Path) -> None:
    """The user's own 65536 is theirs. Same value, same file -- but the stamp
    says this config already had its one pass, so the pin stands."""
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"contextWindowTokens": 65536}}})
    _stamp_path(p).write_text(json.dumps({"version": CURRENT_CONFIG_VERSION}), encoding="utf-8")

    assert load_config(p).agents.defaults.context_window_tokens == 65536
    assert _defaults(p)["contextWindowTokens"] == 65536


def test_other_context_window_pins_are_never_touched(tmp_path: Path) -> None:
    """Only the one retired default is a fossil; every other number was typed
    by someone."""
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"contextWindowTokens": 32768}}})

    assert load_config(p).agents.defaults.context_window_tokens == 32768
    assert _defaults(p)["contextWindowTokens"] == 32768


def test_migration_write_back_keeps_extension_blocks(tmp_path: Path) -> None:
    """The mapping ``load_config`` migrates has the extension blocks popped, so
    writing *that* back would delete the user's memory / plugins / skillForge
    sections. Guards the re-read the persist step does instead."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "agents": {"defaults": {"contextWindowTokens": 65536}},
            "memory": {"backend": "everos"},
            "plugins": {"enabled": ["demo"]},
            "skillForge": {"detect_min_tool_calls": 3},
        },
    )

    load_config(p)

    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["memory"] == {"backend": "everos"}
    assert on_disk["plugins"] == {"enabled": ["demo"]}
    assert on_disk["skillForge"] == {"detect_min_tool_calls": 3}


def test_migration_write_back_does_not_materialise_defaults(tmp_path: Path) -> None:
    """``save_config`` dumps every default (~8 KB). Using it here would re-plant
    exactly the kind of fossil this migration pulls out."""
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"contextWindowTokens": 65536, "model": "x/y"}}})

    load_config(p)

    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert set(on_disk["agents"]["defaults"]) == {"model"}
    assert "tools" not in on_disk


def test_migration_is_correct_even_when_the_file_cannot_be_written(tmp_path: Path) -> None:
    """A read-only home must not brick the boot: the in-memory migration is
    what makes the process correct, the write only keeps the file honest."""
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"contextWindowTokens": 65536}}})
    tmp_path.chmod(0o500)
    try:
        cfg = load_config(p)
    finally:
        tmp_path.chmod(0o700)

    assert cfg.agents.defaults.context_window_tokens is None
    assert _defaults(p)["contextWindowTokens"] == 65536


def test_migration_notice_is_told_once_per_process(tmp_path: Path) -> None:
    """Several loads per command (status and doctor do; the RPC server reloads
    every turn) owe the user one telling."""
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"contextWindowTokens": 65536}}})

    drain_migration_notices()
    load_config(p)
    load_config(p)
    notices = drain_migration_notices()

    assert len(notices) == 1
    assert "contextWindowTokens" in notices[0]
    assert drain_migration_notices() == []


def test_no_notice_when_nothing_was_migrated(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"model": "x/y"}}})

    drain_migration_notices()
    load_config(p)

    assert drain_migration_notices() == []


def test_config_without_the_fossil_is_left_byte_identical(tmp_path: Path) -> None:
    """The stamp lands only on a file we actually edited. Commands like
    ``provider use`` promise to leave the config alone when they decide not to
    act, and a load-time stamp would quietly break that promise for everyone."""
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"model": "x/y"}}})
    before = p.read_bytes()

    load_config(p)

    assert p.read_bytes() == before


def test_the_stamp_never_lands_in_the_user_config(tmp_path: Path) -> None:
    """``Config`` is ``extra='forbid'`` and ``load_config`` raises rather than
    falling back, so a key this build knows and an older one does not is a hard
    boot failure for the older build -- a reverted release, a pinned version, or
    a second checkout sharing the same ~/.raven. The watermark is ours to keep in
    a sidecar, not the user's file."""
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"contextWindowTokens": 65536}}})

    load_config(p)

    assert "configVersion" not in json.loads(p.read_text(encoding="utf-8"))
    assert _stamp_path(p).exists()


def test_a_clean_config_is_stamped_without_being_touched(tmp_path: Path) -> None:
    """Nothing to remove still stamps: the sidecar is ours, so writing it costs
    the user nothing and closes the hole where a 65536 they set by hand later
    would be mistaken for the fossil."""
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"model": "x/y"}}})
    before = p.read_bytes()

    load_config(p)

    assert p.read_bytes() == before
    assert _stamp_path(p).exists()

    # Their own 65536, set after the stamp, is theirs.
    _write(p, {"agents": {"defaults": {"contextWindowTokens": 65536}}})
    assert load_config(p).agents.defaults.context_window_tokens == 65536


def test_migration_preserves_the_config_file_mode(tmp_path: Path) -> None:
    """``os.replace`` swaps the inode, so a replacing writer owns the mode of
    what it puts there. config.json holds providers.*.apiKey -- a user who
    tightened it to owner-only must not have it widened behind their back."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "agents": {"defaults": {"contextWindowTokens": 65536}},
            "providers": {"anthropic": {"apiKey": "sk-secret"}},
        },
    )
    p.chmod(0o600)

    load_config(p)

    assert p.stat().st_mode & 0o777 == 0o600


def test_migration_temp_file_is_process_scoped(tmp_path: Path) -> None:
    """Two processes migrating at once must not share one temp path: the
    second's truncating write would be visible as an empty config.json to
    anyone loading between it and the replace."""
    from raven.config import loader

    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"contextWindowTokens": 65536}}})
    seen: list[str] = []
    original = Path.write_text

    def _spy(self: Path, *args: object, **kwargs: object) -> int:
        seen.append(self.name)

        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    loader.Path.write_text = _spy  # type: ignore[method-assign]
    try:
        load_config(p)
    finally:
        loader.Path.write_text = original  # type: ignore[method-assign]

    assert any(name.startswith("config.json.migrating.") and name.endswith(str(os.getpid())) for name in seen)


def test_save_config_writes_only_what_differs_from_the_defaults(tmp_path: Path) -> None:
    """A dump of everything is lossless on reload, but it freezes today's
    defaults into the user's file -- and then a default we improve later never
    reaches anyone who already has one. `contextWindowTokens: 65536` got there
    exactly this way."""
    from raven.config.loader import save_config
    from raven.config.schema import Config

    p = tmp_path / "config.json"
    save_config(Config(), p)

    assert json.loads(p.read_text(encoding="utf-8")) == {}
    assert p.stat().st_size < 100


def test_save_config_keeps_every_value_the_user_chose(tmp_path: Path) -> None:
    from raven.config.loader import save_config
    from raven.config.schema import Config

    p = tmp_path / "config.json"
    chosen = Config.model_validate(
        {"agents": {"defaults": {"model": "x/y"}}, "providers": {"anthropic": {"apiKey": "sk-a"}}}
    )
    save_config(chosen, p)

    written = json.loads(p.read_text(encoding="utf-8"))
    assert written == {"agents": {"defaults": {"model": "x/y"}}, "providers": {"anthropic": {"apiKey": "sk-a"}}}
    # And it reloads to the same config: dropping a value equal to its default
    # is what makes this lossless.
    reloaded = load_config(p)
    assert reloaded.agents.defaults.model == "x/y"
    assert reloaded.providers.get("anthropic").api_key == "sk-a"
    assert reloaded.agents.defaults.max_tool_iterations == Config().agents.defaults.max_tool_iterations
# ── The implicit provider ───────────────────────────────────────────────


def test_an_auto_provider_is_written_down_as_what_it_resolved_to(tmp_path: Path) -> None:
    """``auto`` never detected anything: a bare id walked PROVIDERS in registry
    order and took the first configured claimant, so `gpt-4.1` went to
    openrouter over openai on an array index. The migration writes down the same
    answer -- behaviour unchanged, but now readable and arguable."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "providers": {"anthropic": {"apiKey": "sk-a"}, "openrouter": {"apiKey": "sk-o"}},
            "agents": {"defaults": {"model": "gpt-4.1", "provider": "auto"}},
        },
    )

    assert load_config(p).agents.defaults.provider == "openrouter"
    assert json.loads(p.read_text(encoding="utf-8"))["agents"]["defaults"]["provider"] == "openrouter"


def test_an_absent_provider_is_migrated_too(tmp_path: Path) -> None:
    """Absent meant auto -- the field defaulted to it."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "providers": {"anthropic": {"apiKey": "sk-a"}},
            "agents": {"defaults": {"model": "claude-opus-4-5"}},
        },
    )

    assert load_config(p).agents.defaults.provider == "anthropic"


def test_an_explicit_provider_is_never_rewritten(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "providers": {"anthropic": {"apiKey": "sk-a"}, "openrouter": {"apiKey": "sk-o"}},
            "agents": {"defaults": {"model": "gpt-4.1", "provider": "anthropic"}},
        },
    )
    before = p.read_bytes()

    assert load_config(p).agents.defaults.provider == "anthropic"
    assert p.read_bytes() == before


def test_a_provider_that_cannot_be_resolved_is_left_blank(tmp_path: Path) -> None:
    """No configured provider serves that model. Filling the blank with a vendor
    picked to have something there is the guess this whole change removes; an
    empty provider is reported where it is used instead."""
    p = tmp_path / "config.json"
    _write(p, {"providers": {}, "agents": {"defaults": {"model": "some/unknown-model", "provider": "auto"}}})

    assert load_config(p).agents.defaults.provider in ("", "auto")
    assert json.loads(p.read_text(encoding="utf-8"))["agents"]["defaults"]["provider"] == "auto"
