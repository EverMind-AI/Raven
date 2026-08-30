"""The launcher library: the product-agnostic machines behind agents/ launchers.

``tests/test_agents_research_launcher.py`` stays the behavioral pin for the
composed render; these pin each machine alone, with product-neutral tables,
so the next product inherits tested parts rather than a copy of run.py.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from raven.config import product_render as render


def test_env_wins_over_the_env_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\n\nKEY=from-file\nOTHER = spaced \n", encoding="utf-8")
    monkeypatch.setenv("KEY", "from-env")
    assert render.env_value("KEY", env_file=f) == "from-env"
    monkeypatch.delenv("KEY")
    assert render.env_value("KEY", env_file=f) == "from-file"
    assert render.env_value("OTHER", env_file=f) == "spaced"
    assert render.env_value("ABSENT", env_file=f) is None
    assert render.env_value("KEY") is None, "no file, no env, no value"


def test_dig_and_put_roundtrip():
    data: dict = {}
    render.put(data, ("a", "b", "c"), "v")
    assert data == {"a": {"b": {"c": "v"}}}
    assert render.dig(data, ("a", "b", "c")) == "v"
    assert render.dig(data, ("a", "missing", "c")) == ""
    assert render.dig({"a": "leaf"}, ("a", "b")) == "", "a non-dict on the way is not an error"


def test_secret_slots_fall_back_to_the_host_except_required():
    slots = {"P_KEY": ("providers", "x", "apiKey"), "P_EXTRA": ("tools", "extra")}
    host = {"providers": {"x": {"apiKey": "host-key"}}, "tools": {"extra": "host-extra"}}

    config: dict = {}
    render.apply_secret_slots(
        config, host, slots=slots, required=("P_KEY",), lookup={"P_KEY": None, "P_EXTRA": None}.get
    )
    assert render.dig(config, ("tools", "extra")) == "host-extra"
    assert render.dig(config, ("providers", "x", "apiKey")) == "", "a required secret never inherits per-slot"

    config = {}
    render.apply_secret_slots(
        config, host, slots=slots, required=("P_KEY",), lookup={"P_KEY": "own", "P_EXTRA": "mine"}.get
    )
    assert render.dig(config, ("providers", "x", "apiKey")) == "own"
    assert render.dig(config, ("tools", "extra")) == "mine", "an own value beats the host fallback"


def test_inherit_llm_takes_the_hosts_brains_not_its_limits():
    host = {
        "providers": {"open": {"apiKey": "k"}},
        "routing": {"rules": []},
        "agents": {"defaults": {"provider": "open", "model": "m", "maxToolIterations": 40}},
    }
    config = {"agents": {"defaults": {"maxToolIterations": 20}}}
    taken = render.inherit_llm(config, host)
    assert "provider=open" in taken and "model=m" in taken
    assert config["providers"] == host["providers"]
    assert config["routing"] == {"rules": []}
    assert config["agents"]["defaults"]["provider"] == "open"
    assert config["agents"]["defaults"]["maxToolIterations"] == 20, "operating limits stay the product's"


def test_inherit_llm_declines_without_a_host_key():
    assert render.inherit_llm({}, {"providers": {"open": {"baseUrl": "u"}}}) == ""


def test_state_root_prefers_the_override(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    assert render.product_state_root("prod-x", override=str(tmp_path / "s")) == tmp_path / "s"
    assert render.product_state_root("prod-x") == tmp_path / "home" / "workspace" / "subagent_sessions" / "prod-x"


def test_seed_once_writes_once(tmp_path):
    target = tmp_path / "deep" / "soul.md"
    calls: list[int] = []
    assert render.seed_once(target, lambda: calls.append(1) or "first") is True
    assert target.read_text() == "first"
    assert render.seed_once(target, lambda: calls.append(1) or "second") is False
    assert target.read_text() == "first", "a product update must not overwrite what an operator tuned"
    assert calls == [1], "the render is not evaluated when the target exists"


def test_mode_catalogue_assembles_the_acp_modes_contract(tmp_path):
    (tmp_path / "deep.json").write_text(json.dumps({"flow": {"maxIterations": 60}}), encoding="utf-8")
    labels = {"fast": ("Fast", "the default"), "deep": ("Deep", "longer"), "ghost": ("Ghost", "no file")}
    caps = {False: 20, True: 60}

    def resolve(overlay):
        return caps[bool(overlay)], {"flow": overlay.get("flow", {})}

    catalogue = render.mode_catalogue(
        tmp_path, labels, baseline="fast", overlay_keys=frozenset({"flow"}), resolve=resolve
    )
    assert set(catalogue) == {"fast", "deep"}, "a labeled mode without its overlay file is skipped"
    assert catalogue["fast"]["overlay"] == {"flow": {}, "maxToolIterations": 20}, (
        "the baseline's diff is empty but its cap is not"
    )
    assert catalogue["deep"] == {
        "name": "Deep",
        "description": "longer",
        "maxToolIterations": 60,
        "overlay": {"flow": {"maxIterations": 60}, "maxToolIterations": 60},
    }


def test_mode_catalogue_refuses_an_unknown_overlay_key(tmp_path):
    (tmp_path / "deep.json").write_text(json.dumps({"surprise": 1}), encoding="utf-8")
    labels = {"fast": ("Fast", "d"), "deep": ("Deep", "d")}
    with pytest.raises(SystemExit, match="surprise"):
        render.mode_catalogue(
            tmp_path, labels, baseline="fast", overlay_keys=frozenset({"flow"}), resolve=lambda o: (None, {})
        )


def test_mode_catalogue_is_empty_without_a_modes_dir(tmp_path):
    out = render.mode_catalogue(
        tmp_path / "absent",
        {"fast": ("F", "d")},
        baseline="fast",
        overlay_keys=frozenset(),
        resolve=lambda o: (None, {}),
    )
    assert out == {}


def test_sweep_removes_only_the_dead(tmp_path):
    own = tmp_path / f".config.rendered.{os.getpid()}.json"
    own.write_text("{}")
    dead = tmp_path / ".config.rendered.999999.json"
    dead.write_text("{}")
    malformed = tmp_path / ".config.rendered.notapid.json"
    malformed.write_text("{}")

    render.sweep_stale_renders(tmp_path)

    assert own.exists(), "a live server's render survives the sweep"
    assert not dead.exists()
    assert not malformed.exists()


def test_write_rendered_is_owner_only_and_pid_named(tmp_path):
    rendered = render.write_rendered({"a": 1}, tmp_path)
    assert rendered.name == f".config.rendered.{os.getpid()}.json"
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600
    assert json.loads(rendered.read_text()) == {"a": 1}
