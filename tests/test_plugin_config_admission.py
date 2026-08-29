"""EM-2 admission machinery — validation + defaults at dispensing (S rehearsal).

Every test doubles as the built-in mutation audit for one admission rule:
defaults fill, required bites, types bite (including the bool-is-int trap),
unknown keys tolerate, empty declarations keep the historic verbatim
pass-through, and the bundled everos declaration stays zero-behavior for
valid configs while going loud on invalid ones.
"""

import logging

import pytest

from raven.core.admission import PluginConfigError, admit_slice


def test_empty_schema_is_verbatim_passthrough():
    slice_ = {"anything": 1, "at": "all"}
    assert admit_slice({}, slice_, plugin_id="p") == slice_


def test_missing_slice_reads_as_empty_then_defaults_apply():
    schema = {"base_url": {"type": "string", "default": "http://127.0.0.1:8000"}}
    assert admit_slice(schema, None, plugin_id="p") == {"base_url": "http://127.0.0.1:8000"}


def test_present_value_beats_default():
    schema = {"k": {"type": "integer", "default": 5}}
    assert admit_slice(schema, {"k": 9}, plugin_id="p")["k"] == 9


def test_required_missing_names_plugin_and_key():
    schema = {"token": {"type": "string", "required": True}}
    with pytest.raises(PluginConfigError, match=r"'p'.*'token'.*required"):
        admit_slice(schema, {}, plugin_id="p")


def test_wrong_type_bites():
    schema = {"base_url": {"type": "string"}}
    with pytest.raises(PluginConfigError, match=r"'base_url' must be string"):
        admit_slice(schema, {"base_url": 8000}, plugin_id="p")


def test_bool_is_rejected_where_integer_declared():
    schema = {"port": {"type": "integer"}}
    with pytest.raises(PluginConfigError, match="got boolean"):
        admit_slice(schema, {"port": True}, plugin_id="p")


def test_unknown_key_passes_through_with_warning(caplog):
    schema = {"known": {"type": "string"}}
    with caplog.at_level(logging.WARNING):
        out = admit_slice(schema, {"known": "x", "extra": 1}, plugin_id="p")
    assert out == {"known": "x", "extra": 1}
    assert any("extra" in r.getMessage() for r in caplog.records)


def test_unknown_declared_type_is_a_schema_error():
    with pytest.raises(PluginConfigError, match="unknown type"):
        admit_slice({"k": {"type": "uuid"}}, {"k": "x"}, plugin_id="p")


# ── the wire: registry dispenses admitted slices ─────────────────────────


def _registry_with(tmp_path, schema_toml: str):
    from raven.plugins.bootstrap import assemble_plugin_registry

    plug = tmp_path / "plugins" / "demo"
    plug.mkdir(parents=True)
    plug.joinpath("raven-plugin.toml").write_text(
        '[plugin]\nid = "demo"\nversion = "1.0"\nenabled_by_default = true\n'
        "[[plugin.contributes.memory_backends]]\n"
        'name = "demo"\nfactory = "demo_mod:make"\n' + schema_toml
    )
    plug.joinpath("demo_mod.py").write_text("def make(ctx):\n    return dict(ctx.config)\n")
    return assemble_plugin_registry(user_dir=tmp_path / "plugins", entry_points_group=None)


def _locator(tmp_path):
    from raven.plugins.context import ServiceLocator

    return ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a")


def test_registry_applies_defaults_before_factory_boards(tmp_path):
    reg = _registry_with(tmp_path, '[plugin.config_schema]\nmode = { type = "string", default = "fast" }\n')
    got = reg.build_memory_backend("demo", config={}, services=_locator(tmp_path))
    assert got == {"mode": "fast"}


def test_registry_bites_bad_type_at_the_door(tmp_path):
    reg = _registry_with(tmp_path, '[plugin.config_schema]\nmode = { type = "string" }\n')
    with pytest.raises(PluginConfigError, match="'demo'"):
        reg.build_memory_backend("demo", config={"mode": 3}, services=_locator(tmp_path))


def test_everos_declaration_is_zero_behavior_for_valid_config(tmp_path):
    from raven.core.plugin_stack import plugin_discovery_sources
    from raven.plugins.bootstrap import assemble_plugin_registry

    reg = assemble_plugin_registry(**plugin_discovery_sources())
    mf = reg.manifest_for("everos-memory")
    assert mf is not None and "base_url" in mf.config_schema
    schema = mf.config_schema
    assert admit_slice(schema, {"base_url": "http://x:9"}, plugin_id="everos-memory") == {"base_url": "http://x:9"}
    assert admit_slice(schema, {}, plugin_id="everos-memory") == {}
    with pytest.raises(PluginConfigError):
        admit_slice(schema, {"base_url": 9}, plugin_id="everos-memory")

    # What the onboarding wizard records is declared too, so a boot no longer
    # warns about every key it wrote; the values pass through unchanged.
    recorded = {"root": "/data/everos", "owned": True, "agent_id": "a1", "user_id": "u1"}
    assert set(recorded) <= set(schema)
    assert admit_slice(schema, recorded, plugin_id="everos-memory") == recorded
    with pytest.raises(PluginConfigError):
        admit_slice(schema, {"owned": "yes"}, plugin_id="everos-memory")


def test_container_types_are_shape_checked_at_the_door():
    """array/object check the container shape only; element and member
    validation stays with the value's consumer."""
    schema = {"rooms": {"type": "array"}, "routing": {"type": "object"}}
    ok = admit_slice(schema, {"rooms": ["a", "b"], "routing": {"x": 1}}, plugin_id="p")
    assert ok == {"rooms": ["a", "b"], "routing": {"x": 1}}
    with pytest.raises(PluginConfigError):
        admit_slice(schema, {"rooms": "a,b"}, plugin_id="p")
    with pytest.raises(PluginConfigError):
        admit_slice(schema, {"routing": ["not", "a", "mapping"]}, plugin_id="p")


def test_nested_tables_read_by_attribute_and_mapping():
    """A file-set object key must read exactly like the sub-model it replaces:
    mochat does ``config.mention.require_in_groups`` and
    ``config.groups.get(chat).require_mention``; slack does
    ``config.dm.policy``. Before the nested view, a file-set table crashed
    those attribute reads with AttributeError on dict."""
    from raven.core.admission import DispensedSlice

    class _Section:
        pass

    slice_ = DispensedSlice(
        _Section(),
        {
            "mention": {"require_in_groups": True},
            "groups": {"room-1": {"require_mention": False}},
            "panels": [{"id": "p1"}],
        },
    )

    assert slice_.mention.require_in_groups is True
    assert slice_.groups.get("room-1").require_mention is False
    assert slice_.groups.get("room-2") is None
    assert "room-1" in slice_.groups
    assert slice_.panels[0].id == "p1"


def test_nested_view_is_frozen_and_typed_values_pass_through():
    from raven.core.admission import DispensedSlice

    class _Typed:
        require_in_groups = False

    class _Section:
        pass

    slice_ = DispensedSlice(_Section(), {"mention": _Typed(), "groups": {}})

    assert slice_.mention is not None
    assert isinstance(slice_.mention, _Typed)
    assert not slice_.groups
    try:
        slice_.groups.x = 1
    except AttributeError:
        pass
    else:
        raise AssertionError("nested view accepted a write")
