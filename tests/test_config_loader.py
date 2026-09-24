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
    ``skillForge.extraction``; see test_config_raven_loader for the
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
    relocate to skillForge.extraction with those keys dropped (ExtractionConfig is
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
    block = out["skillForge"]["extraction"]
    assert "minMessages" not in block
    assert "minToolCalls" not in block
    assert block["maxSkillsTopK"] == 5
    assert block["enabled"] is True


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
    block = out["skillForge"]["extraction"]
    assert "min_messages" not in block
    assert "min_tool_calls" not in block


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


# ---------------------------------------------------------------------------
# RAVEN_HOME decides where everything lives, including this
# ---------------------------------------------------------------------------


def test_raven_home_moves_the_config_path(tmp_path, monkeypatch) -> None:
    """Five other places already honoured RAVEN_HOME -- the installer, the node
    runtime lookup, the tracing directory, the serve state file and the file
    server -- and this one did not. Setting it gave a split installation: the
    runtime in one place, the configuration in another."""
    from raven.config.loader import get_config_path

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "elsewhere"))

    assert get_config_path() == tmp_path / "elsewhere" / "config.json"


def test_the_runtime_subdirs_follow_it(tmp_path, monkeypatch) -> None:
    """The cron store and the rest hang off the config path's parent, so moving
    the config has to move them with it or the split is only narrower."""
    from raven.config.paths import get_cron_dir

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "elsewhere"))

    assert get_cron_dir() == tmp_path / "elsewhere" / "cron"


def test_an_explicit_path_still_wins(tmp_path, monkeypatch) -> None:
    """`set_config_path` is how a test or a second instance pins one; an
    environment variable must not override a caller who was specific."""
    from raven.config.loader import get_config_path, set_config_path

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "env"))
    explicit = tmp_path / "explicit" / "config.json"
    set_config_path(explicit)
    try:
        assert get_config_path() == explicit
    finally:
        set_config_path(None)  # type: ignore[arg-type]


def test_an_empty_value_is_not_a_home(tmp_path, monkeypatch) -> None:
    """`RAVEN_HOME=` in a shell profile is unset, not "the current directory"."""
    from pathlib import Path

    from raven.config.loader import get_config_path

    monkeypatch.setenv("RAVEN_HOME", "   ")

    assert get_config_path() == Path.home() / ".raven" / "config.json"


def test_home_implements_the_layout_paper(tmp_path, monkeypatch) -> None:
    """raven.home answers with the paper's vocabulary, not its own copies."""
    from pathlib import Path

    from raven import home
    from raven.contracts.path_policy import (
        CONFIG_FILENAME,
        DEFAULT_HOME_DIRNAME,
        HOME_ENV_VAR,
    )

    monkeypatch.setattr(home, "_current_config_path", None)
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path))
    assert home.raven_home() == tmp_path
    assert home.get_config_path() == tmp_path / CONFIG_FILENAME
    monkeypatch.setenv(HOME_ENV_VAR, "   ")
    assert home.raven_home() == Path.home() / DEFAULT_HOME_DIRNAME


def test_the_workspace_follows_raven_home_too(tmp_path, monkeypatch) -> None:
    """The half that matters most.

    Sessions, uploads, exports and the skill pool live in the workspace. An
    instance pointed at another home that kept the default workspace read and
    wrote the first installation's conversations -- which is the one thing a
    separate home exists to prevent.
    """
    from raven.config.loader import load_config

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "elsewhere"))

    assert load_config().workspace_path == tmp_path / "elsewhere" / "workspace"


def test_both_derivations_of_the_workspace_agree(tmp_path, monkeypatch) -> None:
    """`Config.workspace_path` is not the only one: `get_workspace_path()` is the
    second, and it is what the CLI session commands and `raven onboard` reach
    for. Moving one and not the other is worse than moving neither -- the
    gateway then writes sessions into a tree the CLI does not read."""
    from raven.config.loader import load_config
    from raven.config.paths import get_workspace_path

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "elsewhere"))

    assert get_workspace_path() == tmp_path / "elsewhere" / "workspace"
    assert get_workspace_path() == load_config().workspace_path


def test_a_named_workspace_still_wins_over_the_home(tmp_path, monkeypatch) -> None:
    """Same rule on this side as on the config's: only the default follows."""
    from raven.config.paths import get_workspace_path

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "elsewhere"))

    assert get_workspace_path(str(tmp_path / "mine")) == tmp_path / "mine"


def test_a_configured_workspace_is_used_as_written(tmp_path, monkeypatch) -> None:
    """Only the default follows the home; somebody who named a path meant it."""
    import json

    from raven.config.loader import get_config_path, load_config

    home = tmp_path / "elsewhere"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"agents": {"defaults": {"workspace": str(tmp_path / "mine")}}}))
    monkeypatch.setenv("RAVEN_HOME", str(home))

    assert get_config_path() == home / "config.json"
    assert load_config().workspace_path == tmp_path / "mine"


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
    says this config already had its one pass, so the pin stands.

    The stamp is the literal a shipped build wrote, not ``CURRENT_CONFIG_VERSION``.
    Written from the constant, this precondition moves every time the mark is
    bumped, so it can only ever test the generation it was run under -- which is
    how the re-run below went unnoticed.
    """
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"contextWindowTokens": 65536}}})
    _stamp_path(p).write_text(json.dumps({"version": 1}), encoding="utf-8")

    assert load_config(p).agents.defaults.context_window_tokens == 65536
    assert _defaults(p)["contextWindowTokens"] == 65536


def test_a_later_generation_does_not_reopen_a_migration_already_run(tmp_path: Path) -> None:
    """The user this protects read our own notice and acted on it.

    0.1.11 cleared their fossil and told them: "Put the line back if you did want
    that number". They did. Their stamp says 1. Bumping the mark to 2 for an
    unrelated migration must not delete it a second time -- and must not print
    the same invitation again.
    """
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"contextWindowTokens": 65536, "model": "anthropic/claude-opus-4-5"}}})
    _stamp_path(p).write_text(json.dumps({"version": 1}), encoding="utf-8")

    drain_migration_notices()
    cfg = load_config(p)

    assert cfg.agents.defaults.context_window_tokens == 65536
    assert _defaults(p)["contextWindowTokens"] == 65536
    assert [n for n in drain_migration_notices() if "contextWindowTokens" in n] == []


def test_the_legacy_leaves_migration_rewrites_the_file_once(tmp_path: Path) -> None:
    """A config stamped 3 carries the three leaves the models stopped accepting;
    the floor moves them (or drops them) in the file, tells the user once, and
    stamps 4 so the next load does not look again."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "skillForge": {"skillsDir": "/srv/skills", "massLibraryDb": "/tmp/old.db"},
            "context": {"engine": "legacy"},
        },
    )
    _stamp_path(p).write_text(json.dumps({"version": 3}), encoding="utf-8")

    drain_migration_notices()
    load_config(p)

    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["skillForge"] == {"localDirs": [{"path": "/srv/skills"}]}
    assert on_disk["context"] == {}
    assert json.loads(_stamp_path(p).read_text(encoding="utf-8")) == {"version": CURRENT_CONFIG_VERSION}
    notices = drain_migration_notices()
    assert len([n for n in notices if "skillForge." in n or "context.engine" in n]) == 3

    load_config(p)
    assert drain_migration_notices() == []


def test_the_phantom_knobs_migration_strips_both_spellings(tmp_path: Path) -> None:
    """A config stamped 4 may carry the two knobs nothing ever read; the floor
    drops them in the file, tells the user once, and stamps 5."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "agents": {"defaults": {"thinkingBudget": 2048}},
            "tokenWise": {"smartRouting": {"enabled": True}},
        },
    )
    _stamp_path(p).write_text(json.dumps({"version": 4}), encoding="utf-8")

    drain_migration_notices()
    load_config(p)

    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert "thinkingBudget" not in on_disk["agents"]["defaults"]
    assert on_disk["tokenWise"] == {}
    assert json.loads(_stamp_path(p).read_text(encoding="utf-8")) == {"version": CURRENT_CONFIG_VERSION}
    notices = drain_migration_notices()
    assert len([n for n in notices if "thinkingBudget" in n or "smartRouting" in n]) == 2

    load_config(p)
    assert drain_migration_notices() == []


def test_the_vendored_tree_migration_reaims_rows_and_stamps_six(tmp_path: Path) -> None:
    """A config stamped 5 may hold roster rows written against the retired
    ``subagents/`` tree. A row whose name matches the folder's current manifest
    is re-aimed at ``agents/`` (fork-venv interpreter swapped for the running
    one); a row under a name the products no longer use is left byte-identical
    with an orphan notice; rows naming no vendored folder pass through. One
    cleanup hint rides along, and the stamp moves to 6."""
    import sys

    from raven.home import raven_home

    ppt_old = "/Users/someone/.raven/subagents/raven-ppt"
    research_old = "/Users/someone/.raven/subagents/raven-research"
    untouched_cmd = "/usr/local/bin/claude -p {prompt}"
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "subagents": {
                "agents": [
                    {
                        "name": "Raven-PPT",
                        "kind": "acp",
                        "command": f"{ppt_old}/.venv/bin/python {ppt_old}/run.py --acp",
                        "cwd": f"{ppt_old}/",
                        "everos": {"userId": "raven-ppt", "agentId": "raven-ppt"},
                    },
                    {
                        "name": "Raven-Research",
                        "kind": "acp",
                        "command": f"{research_old}/.venv/bin/python {research_old}/run.py --acp",
                    },
                    {
                        "name": "claude-code",
                        "kind": "cli",
                        "command": untouched_cmd,
                        "description": "writes records under subagents/direct like everyone else",
                    },
                ]
            }
        },
    )
    _stamp_path(p).write_text(json.dumps({"version": 5}), encoding="utf-8")

    drain_migration_notices()
    load_config(p)

    on_disk = json.loads(p.read_text(encoding="utf-8"))
    rows = on_disk["subagents"]["agents"]
    new_root = raven_home() / "agents" / "raven-ppt"
    assert rows[0]["command"] == f"{sys.executable} {new_root / 'run.py'} --acp"
    assert rows[0]["cwd"] == str(new_root)
    assert rows[0]["everos"] == {"userId": "raven-ppt", "agentId": "raven-ppt"}
    assert rows[1]["command"] == f"{research_old}/.venv/bin/python {research_old}/run.py --acp"
    assert rows[2]["command"] == untouched_cmd
    assert rows[2]["description"] == "writes records under subagents/direct like everyone else"
    assert json.loads(_stamp_path(p).read_text(encoding="utf-8")) == {"version": CURRENT_CONFIG_VERSION}
    notices = [n for n in drain_migration_notices() if "vendored" in n or "retired" in n]
    assert len(notices) == 3, notices
    assert any("Raven-PPT" in n and "engine wheel" in n for n in notices)
    assert any("Raven-Research" in n and "re-run onboarding" in n for n in notices)
    assert any("can be deleted" in n for n in notices)

    load_config(p)
    assert drain_migration_notices() == []


def test_the_vendored_tree_migration_handles_windows_and_glued_tokens() -> None:
    """Both separators, a ``--flag=path`` glued token and a trailing-separator
    folder path are all recognized; the rewrite is idempotent because the
    result no longer names the old tree."""
    import sys

    from raven.config.loader import _migrate_vendored_tree_rows
    from raven.home import raven_home

    row = {
        "name": "Raven-PPT",
        "kind": "acp",
        "command": (
            "C:\\Users\\x\\.raven\\subagents\\raven-ppt\\.venv\\Scripts\\python.exe "
            "C:\\Users\\x\\.raven\\subagents\\raven-ppt\\run.py "
            "--config=/Users/x/.raven/subagents/raven-ppt/config.json"
        ),
        "cwd": "C:\\Users\\x\\.raven\\subagents\\raven-ppt\\",
    }
    data = {"subagents": {"agents": [row]}}

    assert _migrate_vendored_tree_rows(data) is True
    root = raven_home() / "agents" / "raven-ppt"
    assert row["command"] == f"{sys.executable} {root / 'run.py'} --config={root / 'config.json'}"
    assert row["cwd"] == str(root)
    assert _migrate_vendored_tree_rows(data) is False


def test_the_vendored_tree_migration_refuses_a_space_split_path() -> None:
    """An unquoted space splits a path across tokens; rewriting the fragment
    would weld half a path onto the new root, so the whole row is left alone
    and the notice says to fix it by hand."""
    from raven.config.loader import _migrate_vendored_tree_rows, _migration_notices

    original = "C:\\Users\\John Doe\\.raven\\subagents\\raven-code\\run.py --acp"
    row = {"name": "Raven-Code", "kind": "acp", "command": original}
    data = {"subagents": {"agents": [row]}}

    _migration_notices.clear()
    assert _migrate_vendored_tree_rows(data, notify=True) is False
    assert row["command"] == original
    assert any("by hand" in n for n in _migration_notices), _migration_notices
    _migration_notices.clear()


def test_the_vendored_tree_migration_reads_the_legacy_spellings() -> None:
    """A config from the thirdParty era migrates the same way."""
    import sys

    from raven.config.loader import _migrate_vendored_tree_rows
    from raven.home import raven_home

    old = "/Users/x/.raven/subagents/raven-oncall"
    row = {"name": "Raven-Oncall", "kind": "acp", "command": f"{old}/.venv/bin/python {old}/run.py"}
    data = {"subagents": {"thirdParty": [row]}}

    assert _migrate_vendored_tree_rows(data) is True
    root = raven_home() / "agents" / "raven-oncall"
    assert row["command"] == f"{sys.executable} {root / 'run.py'}"


def test_the_manifest_name_and_engine_tables_match_the_shipped_manifests() -> None:
    """The migration's two hand-written tables are contract-pinned to the real
    manifests, so a product rename cannot silently disarm the name gate.
    ``raven-research`` is pinned to None, never its manifest name: the fork's
    research rows were cli-kind on a different flow, and the product now holds
    the fork's old display name, so a name match there would re-aim exactly
    the rows the gate exists to refuse."""
    from raven.config.loader import _ENGINE_WHEEL_FOLDERS, _VENDORED_MANIFEST_NAMES

    root = Path(__file__).resolve().parent.parent / "agents"
    manifests = {
        folder.name: json.loads((folder / "subagent.json").read_text(encoding="utf-8"))
        for folder in sorted(root.iterdir())
        if (folder / "subagent.json").is_file()
    }
    expected: dict[str, str | None] = {name: m["name"] for name, m in manifests.items()}
    expected["raven-research"] = None
    assert expected == _VENDORED_MANIFEST_NAMES
    assert manifests["raven-research"]["name"] == "Raven-Research"
    assert {name for name, m in manifests.items() if m.get("engine")} == set(_ENGINE_WHEEL_FOLDERS)


def test_the_vendored_tree_migration_swaps_only_the_interpreter_leaf() -> None:
    """A data file that happens to live under the fork's .venv is a path and
    moves like one; only the python leaf becomes the running interpreter."""
    import sys

    from raven.config.loader import _migrate_vendored_tree_rows
    from raven.home import raven_home

    old = "/h/.raven/subagents/raven-code"
    row = {
        "name": "Raven-Code",
        "kind": "acp",
        "command": (
            f"{old}/.venv/bin/python {old}/run.py --cert={old}/.venv/lib/python3.12/site-packages/certifi/cacert.pem"
        ),
    }
    data = {"subagents": {"agents": [row]}}

    assert _migrate_vendored_tree_rows(data) is True
    root = raven_home() / "agents" / "raven-code"
    assert row["command"] == (
        f"{sys.executable} {root / 'run.py'} --cert={root / '.venv/lib/python3.12/site-packages/certifi/cacert.pem'}"
    )


def test_a_row_tangled_across_two_folders_is_left_alone() -> None:
    """No single launcher can serve a row that references two product folders;
    it keeps its bytes and its notice says so."""
    from raven.config.loader import _migrate_vendored_tree_rows, _migration_notices

    row = {
        "name": "Raven-Code",
        "kind": "acp",
        "command": "/h/.raven/subagents/raven-code/run.py",
        "cwd": "/h/.raven/subagents/raven-ppt",
    }
    data = {"subagents": {"agents": [row]}}

    _migration_notices.clear()
    assert _migrate_vendored_tree_rows(data, notify=True) is False
    assert row["command"] == "/h/.raven/subagents/raven-code/run.py"
    assert any("more than one" in n for n in _migration_notices), _migration_notices
    _migration_notices.clear()


def test_a_neighbouring_name_in_prose_does_not_poison_the_verdict() -> None:
    """``raven-designer`` is not ``raven-design``: a prose mention of a longer
    neighbour must not flip a cleanly migratable row to manual."""
    import sys

    from raven.config.loader import _migrate_vendored_tree_rows
    from raven.home import raven_home

    old = "/h/.raven/subagents/raven-code"
    row = {
        "name": "Raven-Code",
        "kind": "acp",
        "command": f"{old}/run.py",
        "description": "see /elsewhere/subagents/raven-designer/x for the unrelated tool",
    }
    data = {"subagents": {"agents": [row]}}

    assert _migrate_vendored_tree_rows(data) is True
    assert row["command"] == str(raven_home() / "agents" / "raven-code" / "run.py")
    assert row["description"] == "see /elsewhere/subagents/raven-designer/x for the unrelated tool"
    assert sys.executable not in row["command"]


def test_an_orphan_only_config_is_stamped_without_being_touched(tmp_path: Path) -> None:
    """A config whose only tree reference is an orphan row (legacy thirdParty
    spelling included) is told once, byte-identical on disk, and still stamps
    6 so the telling never repeats."""
    old = "/Users/someone/.raven/subagents/raven-research"
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "subagents": {
                "thirdParty": [
                    {"name": "Raven-Research", "kind": "acp", "command": f"{old}/run.py"},
                ]
            }
        },
    )
    _stamp_path(p).write_text(json.dumps({"version": 5}), encoding="utf-8")
    before = p.read_bytes()

    drain_migration_notices()
    load_config(p)

    assert p.read_bytes() == before
    assert json.loads(_stamp_path(p).read_text(encoding="utf-8")) == {"version": CURRENT_CONFIG_VERSION}
    notices = [n for n in drain_migration_notices() if "retired" in n]
    assert len(notices) == 1 and "re-run onboarding" in notices[0], notices

    load_config(p)
    assert drain_migration_notices() == []


def test_the_research_rename_migration_renames_the_ng_row(tmp_path: Path) -> None:
    """A config already stamped six may hold the transition-era row name; floor
    seven renames it to the plain name, tells once, and never repeats."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "subagents": {
                "agents": [
                    {"name": "Raven-Research-NG", "kind": "acp", "command": "/opt/agents/raven-research/run.py"},
                    {"name": "claude-code", "kind": "cli", "command": "/usr/local/bin/claude -p {prompt}"},
                ]
            }
        },
    )
    _stamp_path(p).write_text(json.dumps({"version": 6}), encoding="utf-8")

    drain_migration_notices()
    load_config(p)

    rows = json.loads(p.read_text(encoding="utf-8"))["subagents"]["agents"]
    assert rows[0]["name"] == "Raven-Research"
    assert rows[1]["name"] == "claude-code"
    assert json.loads(_stamp_path(p).read_text(encoding="utf-8")) == {"version": CURRENT_CONFIG_VERSION}
    notices = [n for n in drain_migration_notices() if "renamed" in n]
    assert len(notices) == 1 and "-NG suffix retired" in notices[0], notices

    load_config(p)
    assert drain_migration_notices() == []


def test_a_blocked_rename_holds_the_stamp_and_completes_once_the_name_frees(tmp_path: Path) -> None:
    """A fork-era row still holding the plain name blocks the rename: two rows
    under one name would make every by-name verb ambiguous. The blocked pass
    holds the stamp at floor six, so the advisory repeats and the rename
    completes on the load after the blocking row is removed."""
    old = "/Users/someone/.raven/subagents/raven-research"
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "subagents": {
                "agents": [
                    {"name": "Raven-Research", "kind": "cli", "command": f"{old}/run.py"},
                    {"name": "Raven-Research-NG", "kind": "acp", "command": "/opt/agents/raven-research/run.py"},
                ]
            }
        },
    )
    _stamp_path(p).write_text(json.dumps({"version": 5}), encoding="utf-8")
    before = p.read_bytes()

    drain_migration_notices()
    load_config(p)

    assert p.read_bytes() == before
    assert json.loads(_stamp_path(p).read_text(encoding="utf-8")) == {"version": 6}
    notices = drain_migration_notices()
    assert any("cannot be re-aimed" in n for n in notices), notices
    assert any("keeps its transition-era name" in n for n in notices), notices
    assert not any("is renamed" in n for n in notices), notices

    load_config(p)
    repeat = drain_migration_notices()
    assert any("keeps its transition-era name" in n for n in repeat), repeat
    assert not any("cannot be re-aimed" in n for n in repeat), repeat

    body = json.loads(p.read_text(encoding="utf-8"))
    body["subagents"]["agents"] = [r for r in body["subagents"]["agents"] if r["name"] != "Raven-Research"]
    _write(p, body)
    load_config(p)

    rows = json.loads(p.read_text(encoding="utf-8"))["subagents"]["agents"]
    assert [r["name"] for r in rows] == ["Raven-Research"]
    assert json.loads(_stamp_path(p).read_text(encoding="utf-8")) == {"version": CURRENT_CONFIG_VERSION}
    assert any("is renamed" in n for n in drain_migration_notices())

    load_config(p)
    assert drain_migration_notices() == []


def test_a_second_transition_row_blocks_instead_of_minting_a_duplicate() -> None:
    """Two transition rows must not both take the plain name: the first
    renames, the second draws the advisory, whichever alias list it sits in."""
    from raven.config.loader import _migrate_research_rename, _research_rename_pending

    data = {
        "subagents": {
            "agents": [{"name": "Raven-Research-NG", "kind": "acp", "command": "/opt/a/run.py"}],
            "thirdParty": [{"name": "Raven-Research-NG", "kind": "acp", "command": "/opt/b/run.py"}],
        }
    }
    drain_migration_notices()
    assert _migrate_research_rename(data, notify=True) is True
    assert data["subagents"]["agents"][0]["name"] == "Raven-Research"
    assert data["subagents"]["thirdParty"][0]["name"] == "Raven-Research-NG"
    assert _research_rename_pending(data) is True
    notices = drain_migration_notices()
    blocked = [n for n in notices if "keeps its transition-era name" in n]
    assert len(blocked) == 1, notices
    assert "subagents.thirdParty[Raven-Research-NG]" in blocked[0], blocked
    assert "a row in subagents.agents already holds" in blocked[0], blocked
    assert "remove or rename either row" in blocked[0], blocked


def test_the_research_rename_reads_the_snake_case_spelling() -> None:
    """The oldest configs spell the roster ``third_party``; the rename walks
    that list too."""
    from raven.config.loader import _migrate_research_rename

    data = {"subagents": {"third_party": [{"name": "Raven-Research-NG", "kind": "acp", "command": "/opt/a/run.py"}]}}
    assert _migrate_research_rename(data) is True
    assert data["subagents"]["third_party"][0]["name"] == "Raven-Research"


def test_a_stamped_seven_config_keeps_its_transition_row(tmp_path: Path) -> None:
    """A literal stamp of 7 gates the rename off: a user who deliberately
    named a row back stays named back, whatever CURRENT grows to. The house
    lesson of test_context_window_pin_survives_once_stamped: a stamp written
    from CURRENT_CONFIG_VERSION can only ever test the generation it was run
    under."""
    p = tmp_path / "config.json"
    _write(
        p,
        {"subagents": {"agents": [{"name": "Raven-Research-NG", "kind": "acp", "command": "/opt/a/run.py"}]}},
    )
    _stamp_path(p).write_text(json.dumps({"version": 7}), encoding="utf-8")
    before = p.read_bytes()

    drain_migration_notices()
    load_config(p)

    assert p.read_bytes() == before
    assert drain_migration_notices() == []


def test_the_roster_migrations_skip_mangled_sections_instead_of_crashing() -> None:
    """A stamped config can still be hand-mangled afterwards; the migrations
    must leave the shape complaint to the schema rather than crash the load."""
    from raven.config.loader import (
        _migrate_research_rename,
        _migrate_vendored_tree_rows,
        _research_rename_pending,
    )

    for bad in (
        {"subagents": [{"name": "Raven-Research-NG"}]},
        {"subagents": "oops"},
        {"subagents": {"agents": 5}},
        {},
    ):
        assert _migrate_research_rename(bad) is False
        assert _migrate_vendored_tree_rows(bad) is False
        assert _research_rename_pending(bad) is False


def test_the_research_rename_reads_the_legacy_spellings_and_scans_names_across_them() -> None:
    """The rename walks the thirdParty-era aliases too, and the collision scan
    sees a name held in a different alias list than the row it blocks."""
    from raven.config.loader import _migrate_research_rename

    ng = {"name": "Raven-Research-NG", "kind": "acp", "command": "/opt/agents/raven-research/run.py"}
    data = {"subagents": {"thirdParty": [dict(ng)]}}
    assert _migrate_research_rename(data) is True
    assert data["subagents"]["thirdParty"][0]["name"] == "Raven-Research"

    blocked = {
        "subagents": {
            "agents": [{"name": "Raven-Research", "kind": "cli", "command": "/x/subagents/raven-research/run.py"}],
            "thirdParty": [dict(ng)],
        }
    }
    assert _migrate_research_rename(blocked) is False
    assert blocked["subagents"]["thirdParty"][0]["name"] == "Raven-Research-NG"


def test_a_transition_named_row_into_the_tree_is_an_orphan_not_a_reaim() -> None:
    """The research folder allows no re-aim at all: even the transition name
    over a tree path draws the orphan verdict, because the folder's launcher
    cannot serve what the fork's rows asked of it."""
    from raven.config.loader import _row_verdict

    row = {
        "name": "Raven-Research-NG",
        "kind": "acp",
        "command": "/Users/x/.raven/subagents/raven-research/run.py",
    }
    verdict, folders = _row_verdict(row)
    assert verdict == "orphan" and folders == {"raven-research"}


def test_the_provider_migration_survives_a_legacy_top_level_block(tmp_path: Path) -> None:
    """The probe validates a strict ``Config``, and this migration runs before
    the shims that relocate legacy blocks. A config still carrying a top-level
    ``skillRouter`` used to fail the probe, be skipped with a DEBUG line, and be
    stamped anyway -- so it was never retried. It lands on the oldest configs,
    which are the ones most likely to still say ``auto``.
    """
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "agents": {"defaults": {"model": "claude-opus-4-5", "provider": "auto"}},
            "providers": {"anthropic": {"apiKey": "sk-test"}},
            "skillRouter": {"enabled": True},
        },
    )

    assert load_config(p).agents.defaults.provider == "anthropic"
    assert _defaults(p)["provider"] == "anthropic"


def test_a_later_generation_still_runs_its_own_migration(tmp_path: Path) -> None:
    """The other half of the floor: gen-1 configs are behind on gen 2 and must
    pick it up. Without this, "do not re-run the old one" and "never run the new
    one" look identical from the outside.
    """
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "agents": {"defaults": {"model": "claude-opus-4-5", "provider": "auto"}},
            "providers": {"anthropic": {"apiKey": "sk-test"}},
        },
    )
    _stamp_path(p).write_text(json.dumps({"version": 1}), encoding="utf-8")

    assert load_config(p).agents.defaults.provider == "anthropic"
    assert _defaults(p)["provider"] == "anthropic"
    assert json.loads(_stamp_path(p).read_text(encoding="utf-8")) == {"version": CURRENT_CONFIG_VERSION}


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


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 0o500 does not block root")
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


def test_migration_rewrite_is_locked_and_leaves_no_residue(tmp_path: Path) -> None:
    """The persist pass rides the locked atomic door (``raven.utils.atomic_io``):
    concurrent migrators serialize on the config's sidecar lock, so the
    process-scoped ``config.json.migrating.<pid>`` temp name the old hand-rolled
    replace needed is gone. Only the config, its stamp and the lock anchors may
    be left behind."""
    p = tmp_path / "config.json"
    _write(p, {"agents": {"defaults": {"contextWindowTokens": 65536}}})

    load_config(p)

    data = json.loads(p.read_text(encoding="utf-8"))
    assert "contextWindowTokens" not in data.get("agents", {}).get("defaults", {})
    leftovers = sorted(f.name for f in tmp_path.iterdir())
    assert leftovers == [".lock", "config.json", "config.migrations.json"]


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


def test_the_retired_gateway_web_table_is_dropped_once_with_a_notice():
    """The shim is version-gated like its neighbours: a config behind the floor
    loses the table (in memory and, via _persist_migrations, on disk) and hears
    about it once; a config already at the floor is left alone."""
    from raven.config import loader

    loader._migration_notices.clear()
    data = {"gateway": {"web": {"enabled": True}, "port": 1}}
    loader._migrate_config(data, from_version=2)
    assert "web" not in data["gateway"] and data["gateway"]["port"] == 1
    notices = loader.drain_migration_notices()
    assert len(notices) == 1 and "gateway.web" in notices[0]

    untouched = {"gateway": {"web": {"enabled": True}}}
    loader._migrate_config(untouched, from_version=3)
    assert "web" in untouched["gateway"]
    assert loader.drain_migration_notices() == []


def test_the_retired_deep_research_section_goes_and_its_disabled_entry_stays():
    """Behind the floor a config loses the tool's section and hears about it
    once; its disabled-tools entry stays, because that list is the general name
    denylist and a plugin tool may carry the name; a second pass over the same
    dict has nothing left to say; a config at the floor keeps the lot.

    The floors are literals, not ``CURRENT_CONFIG_VERSION``: written from the
    constant they would move with every bump and only ever test the generation
    they were run under (the lesson of
    test_context_window_pin_survives_once_stamped).
    """
    from raven.config import loader

    loader._migration_notices.clear()
    data = {
        "tools": {
            "deepResearch": {"apiKey": "sk-test"},
            "disabledTools": ["deep_research", "exec"],
            "webSearch": {"provider": "serper"},
        },
        "providers": {"anthropic": {"apiKey": "sk-a"}},
    }
    loader._migrate_config(data, from_version=9)

    assert "deepResearch" not in data["tools"]
    assert data["tools"]["disabledTools"] == ["deep_research", "exec"]
    assert data["tools"]["webSearch"] == {"provider": "serper"}
    assert data["providers"] == {"anthropic": {"apiKey": "sk-a"}}
    notices = loader.drain_migration_notices()
    assert len(notices) == 1 and "tools.deepResearch" in notices[0], notices

    loader._migrate_config(data, from_version=9)
    assert data["tools"] == {"disabledTools": ["deep_research", "exec"], "webSearch": {"provider": "serper"}}
    assert loader.drain_migration_notices() == []

    untouched = {"tools": {"deepResearch": {"apiKey": "sk-test"}, "disabledTools": ["deep_research"]}}
    loader._migrate_config(untouched, from_version=10)
    assert untouched["tools"] == {"deepResearch": {"apiKey": "sk-test"}, "disabledTools": ["deep_research"]}
    assert loader.drain_migration_notices() == []


def test_the_deep_research_migration_reads_the_snake_case_spellings():
    """Configs in the wild spell the section either way; the list, whichever
    way it is spelled, is left as written."""
    from raven.config import loader

    loader._migration_notices.clear()
    data = {"tools": {"deep_research": {"apiKey": "sk-test"}, "disabled_tools": ["web_search", "deep_research"]}}
    loader._migrate_config(data, from_version=9)

    assert data["tools"] == {"disabled_tools": ["web_search", "deep_research"]}
    notices = loader.drain_migration_notices()
    assert len(notices) == 1 and "tools.deep_research" in notices[0], notices


def test_a_deep_research_config_that_only_switched_it_off_is_left_alone(tmp_path: Path) -> None:
    """Most configs never keyed the tool -- they only turned it off. That entry
    is a name on the general denylist, and a plugin tool may carry the name
    (plugin tools register last so one can shadow a built-in), so taking it out
    would put such a tool back on offer: the migration changes nothing, says
    nothing, and the persist pass leaves the file byte for byte alone."""
    from raven.config import loader

    loader._migration_notices.clear()
    data = {"tools": {"disabledTools": ["deep_research"]}}
    assert loader._migrate_retired_deep_research(data, notify=True) is False
    assert data == {"tools": {"disabledTools": ["deep_research"]}}
    assert loader.drain_migration_notices() == []

    p = tmp_path / "config.json"
    _write(p, {"tools": {"disabledTools": ["deep_research"]}})
    before = p.read_text(encoding="utf-8")
    _stamp_path(p).write_text(json.dumps({"version": 9}), encoding="utf-8")
    load_config(p)
    assert p.read_text(encoding="utf-8") == before
    assert json.loads(_stamp_path(p).read_text(encoding="utf-8")) == {"version": CURRENT_CONFIG_VERSION}
    assert [n for n in drain_migration_notices() if "deep_research" in n] == []


def test_the_deep_research_section_also_leaves_the_file_on_disk(tmp_path: Path) -> None:
    """A config stamped 9 still holds the tool's API key in the file itself; the
    persist pass takes the section out of it, leaves the disabled entry, and
    stamps the current mark. The 9 is the literal a shipped build wrote."""
    p = tmp_path / "config.json"
    _write(p, {"tools": {"deepResearch": {"apiKey": "sk-test"}, "disabledTools": ["deep_research", "exec"]}})
    _stamp_path(p).write_text(json.dumps({"version": 9}), encoding="utf-8")

    drain_migration_notices()
    load_config(p)

    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert "deepResearch" not in on_disk["tools"]
    assert on_disk["tools"]["disabledTools"] == ["deep_research", "exec"]
    assert json.loads(_stamp_path(p).read_text(encoding="utf-8")) == {"version": CURRENT_CONFIG_VERSION}
    assert len([n for n in drain_migration_notices() if "deep_research" in n]) == 1


_RETIRED_CODEX = "npx -y @agentclientprotocol/codex-acp@1.1.14"
_RETIRED_CLAUDE = (
    "npx -y @agentclientprotocol/claude-agent-acp@0.66.0",
    "npx -y @agentclientprotocol/claude-agent-acp@0.79.0",
)


def _codex_row(command: object, **extra: object) -> dict:
    return {
        "name": "Codex",
        "kind": "acp",
        "preset": "codex",
        "command": command,
        "env": {"INITIAL_AGENT_MODE": "agent-full-access"},
        "readyTimeoutMs": 120000,
        **extra,
    }


def _claude_row(command: object, **extra: object) -> dict:
    return {
        "name": "Claude Code",
        "kind": "acp",
        "preset": "claude_code",
        "command": command,
        "readyTimeoutMs": 120000,
        **extra,
    }


def test_rows_on_a_retired_shim_pin_follow_their_preset_in_the_file(tmp_path: Path) -> None:
    """The shim pin decides which models the agent can reach -- codex-acp 1.1.14
    bundles a codex whose ``model/list`` stops at GPT-5.6, claude-agent-acp 0.66.0
    offers no Fable 5.1 -- and ``subagents.add`` copies the preset's command into
    the row, so a bumped pin reached new rows only. A config stamped 10 (the
    literal a shipped build wrote) has each such row carried to the command its
    preset ships now, in the file itself, and hears about each once; the rest of
    every row is left as it was."""
    from raven.agent.subagent.presets import THIRD_PARTY_SUBAGENT_PRESETS

    p = tmp_path / "config.json"
    rows = [_codex_row(_RETIRED_CODEX, description="Mine.", model="gpt-5.6-sol"), _claude_row(_RETIRED_CLAUDE[0])]
    _write(p, {"subagents": {"agents": rows}})
    _stamp_path(p).write_text(json.dumps({"version": 10}), encoding="utf-8")

    drain_migration_notices()
    cfg = load_config(p)

    codex = THIRD_PARTY_SUBAGENT_PRESETS["codex"]["command"]
    claude = THIRD_PARTY_SUBAGENT_PRESETS["claude_code"]["command"]
    assert [a.command for a in cfg.subagents.agents] == [codex, claude]
    assert json.loads(p.read_text(encoding="utf-8"))["subagents"]["agents"] == [
        _codex_row(codex, description="Mine.", model="gpt-5.6-sol"),
        _claude_row(claude),
    ]
    assert json.loads(_stamp_path(p).read_text(encoding="utf-8")) == {"version": CURRENT_CONFIG_VERSION}
    notices = [n for n in drain_migration_notices() if "-acp@" in n]
    assert len(notices) == 2, notices
    assert "subagents.agents[Codex]" in notices[0] and codex in notices[0], notices
    assert "subagents.agents[Claude Code]" in notices[1] and claude in notices[1], notices

    load_config(p)
    assert drain_migration_notices() == []


def test_the_shim_pin_migration_moves_only_a_stock_command_under_its_preset() -> None:
    """Only a row that names the preset and still carries the exact command that
    preset shipped: another pin, an added flag, a row carrying another preset's
    retired command, and a hand-written row that runs the same command without
    the provenance field are their owners' to keep, and
    a command that is not a string is left for config validation to name. The legacy
    list spelling is read too. A second pass has nothing left to say, and a
    config already at the floor keeps even the stock row.

    The floors are literals, for the reason
    test_the_retired_deep_research_section_goes_and_its_disabled_entry_stays gives.
    """
    from raven.agent.subagent.presets import THIRD_PARTY_SUBAGENT_PRESETS
    from raven.config import loader

    current = THIRD_PARTY_SUBAGENT_PRESETS["codex"]["command"]
    claude = THIRD_PARTY_SUBAGENT_PRESETS["claude_code"]["command"]
    kept = [
        _codex_row("npx -y @agentclientprotocol/codex-acp@1.1.13", name="Codex-older"),
        _codex_row(_RETIRED_CODEX + " --debug", name="Codex-flagged"),
        {"name": "my-codex", "kind": "acp", "command": _RETIRED_CODEX},
        _codex_row(["npx", "-y", "@agentclientprotocol/codex-acp@1.1.14"], name="Codex-listed"),
        _claude_row("npx -y @agentclientprotocol/claude-agent-acp@0.80.0", name="Claude-own-pin"),
        _claude_row(_RETIRED_CODEX, name="Claude-wearing-codex"),
    ]
    loader._migration_notices.clear()
    data = {
        "subagents": {
            "agents": [_codex_row(_RETIRED_CODEX), _claude_row(_RETIRED_CLAUDE[1]), *json.loads(json.dumps(kept))],
            "thirdParty": [_codex_row(_RETIRED_CODEX, name="Codex-legacy")],
        }
    }
    loader._migrate_config(data, from_version=10)

    assert data["subagents"]["agents"] == [_codex_row(current), _claude_row(claude), *kept]
    assert data["subagents"]["thirdParty"] == [_codex_row(current, name="Codex-legacy")]
    notices = loader.drain_migration_notices()
    assert sorted(n.split("]")[0] for n in notices) == [
        "Migrated: subagents.agents[Claude Code",
        "Migrated: subagents.agents[Codex",
        "Migrated: subagents.thirdParty[Codex-legacy",
    ], notices

    loader._migrate_config(data, from_version=10)
    assert loader.drain_migration_notices() == []

    untouched = {"subagents": {"agents": [_codex_row(_RETIRED_CODEX)]}}
    loader._migrate_config(untouched, from_version=11)
    assert untouched == {"subagents": {"agents": [_codex_row(_RETIRED_CODEX)]}}
    assert loader.drain_migration_notices() == []


def test_the_retired_pin_table_leads_to_the_command_its_preset_ships_now() -> None:
    """The migration spells the preset's command rather than importing it --
    config does not reach up into the agent package, for the reason
    ``raven.config.agent_names`` gives -- so the two spellings are pinned equal
    here. A pin bumped without this table would strand every row already
    configured on the pin before it, which is how the codex rows came to keep a
    menu that stopped at GPT-5.6.

    The table is not a history of every pin: an entry needs the new build
    measured reopening the old one's sessions, the property the migration rests
    on, so a pin that moves without that measurement stays out of it."""
    from raven.agent.subagent.presets import SHIM_LAUNCHED_PRESETS, THIRD_PARTY_SUBAGENT_PRESETS
    from raven.config import loader

    assert loader._RETIRED_SHIM_COMMANDS, "nothing to carry: drop the migration rather than this test"
    for preset, (retired, current) in loader._RETIRED_SHIM_COMMANDS.items():
        assert preset in SHIM_LAUNCHED_PRESETS, f"{preset} is not a shim: its row runs the user's own install"
        assert current == THIRD_PARTY_SUBAGENT_PRESETS[preset]["command"], (
            f"the {preset} pin moved to {THIRD_PARTY_SUBAGENT_PRESETS[preset]['command']!r} without its "
            f"migration: rows configured on {current!r} would keep it. Add that command to the retired set "
            "and give the migration a new floor."
        )
        assert retired and current not in retired, preset


def test_channels_section_settings_are_not_mistaken_for_channels(tmp_path: Path, caplog) -> None:
    """``channels.sendProgress`` is a setting of the section, not a channel whose
    table failed to parse; only an unknown scalar under ``channels`` warns."""
    import logging

    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {"channels": {"sendProgress": False, "sendToolHints": True, "telegram": {"enabled": True}, "oddity": 5}}
        ),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="raven.config.loader"):
        cfg = load_config(p)

    warned = [r.getMessage() for r in caplog.records if "is not a table" in r.getMessage()]
    assert warned == ["channels.oddity is not a table; its cargo reads as unset"]
    assert cfg.channels.send_progress is False
