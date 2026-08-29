"""Channel config declarations (config-with-cargo, every adapter).

Every spec declares the cargo slice of its channel's config in the same
vocabulary plugin manifests use, and since the central per-channel classes
retired (M2), the declaration is the only truth: defaults, secrecy,
requiredness, choices and nesting all live here. This guard keeps the
declarations complete and the dispensing honest -- there is no second
source left to drift against, so what it pins now is the door's own
contract.
"""

from __future__ import annotations

import pytest

from raven.channels.registry import discover_specs
from raven.config.schema import ChannelsConfig, ChannelSocket
from raven.core.admission import _TYPES, dispense_channel_config

# Host-consumed fields: gating and workspace routing read these, adapters do
# not. They stay with the host (socket config) and out of every cargo
# declaration.
SOCKET_FIELDS = {"enabled", "allow_from", "workspace"}

SPECS = discover_specs()


@pytest.mark.parametrize("name", sorted(SPECS))
def test_every_spec_declares_its_cargo(name):
    """A channel with no declaration has no fields at all now -- the door
    would dispense nothing but the socket. Every adapter must declare."""
    assert SPECS[name].config_schema, f"{name} declares no config_schema"


@pytest.mark.parametrize("name", sorted(SPECS))
def test_no_spec_declares_a_socket_field(name):
    overlap = SOCKET_FIELDS & set(SPECS[name].config_schema)
    assert not overlap, f"{name} declares socket fields {sorted(overlap)}"


def _walk(schema, prefix=""):
    for key, decl in schema.items():
        path = f"{prefix}{key}"
        yield path, decl
        sub = decl.get("fields")
        if isinstance(sub, dict):
            yield from _walk(sub, prefix=f"{path}.")


@pytest.mark.parametrize("name", sorted(SPECS))
def test_declarations_speak_the_door_vocabulary(name):
    """Types name door types; every leaf carries a default (required fields
    keep one too -- requiredness is a UX marker, and an undefaulted field
    would dispense as an accidental None)."""
    for path, decl in _walk(SPECS[name].config_schema):
        want = decl.get("type")
        assert want in _TYPES, f"{name}.{path}: unknown type {want!r}"
        if isinstance(decl.get("fields"), dict):
            assert want == "object", f"{name}.{path}: fields on non-object"
            continue
        assert "default" in decl, f"{name}.{path}: no declared default"


@pytest.mark.parametrize("name", sorted(SPECS))
def test_dispensed_view_answers_the_declared_defaults(name):
    """File-less dispensing materializes every declared field from its
    default, the socket fields answer from the socket, and the view is
    frozen."""
    spec = SPECS[name]
    section = ChannelSocket()
    view = dispense_channel_config(spec, section, channel=name)

    for key, decl in spec.config_schema.items():
        if isinstance(decl.get("fields"), dict):
            nested = getattr(view, key)
            for sk, sd in decl["fields"].items():
                if "default" in sd:
                    assert getattr(nested, sk) == sd["default"], f"{key}.{sk}"
        elif "default" in decl:
            assert getattr(view, key) == decl["default"], key
    for socket_field in SOCKET_FIELDS:
        assert getattr(view, socket_field) == getattr(section, socket_field)
    with pytest.raises(AttributeError):
        view.enabled = True


def test_the_file_slice_outranks_the_declared_default(monkeypatch):
    """A value the operator wrote wins over the declaration's default --
    the door changes where a value travels, never what it is. camelCase
    file keys resolve to their snake_case declarations."""
    from raven.config import loader

    monkeypatch.setattr(
        loader,
        "_channel_slices",
        {"telegram": {"token": "tok-123", "groupPolicy": "open", "proxy": "socks5://p:1"}},
    )
    view = dispense_channel_config(SPECS["telegram"], ChannelSocket(), channel="telegram")

    assert view.token == "tok-123"
    assert view.group_policy == "open"
    assert view.proxy == "socks5://p:1"
    assert view.reply_to_message is False


def test_the_central_model_carries_no_cargo_fields():
    """The retirement stays retired: ChannelsConfig knows the two stream
    toggles and nothing per-channel; a cargo field class growing back here
    would put two truths back in play."""
    assert set(ChannelsConfig.model_fields) == {"send_progress", "send_tool_hints"}
    assert set(ChannelSocket.model_fields) == {"workspace", "enabled", "allow_from"}


def test_dynamic_sections_answer_for_known_adapters():
    """config.channels.<adapter> answers a socket view whether or not the
    file has that section; a name no adapter owns still raises."""
    channels = ChannelsConfig()
    for name in SPECS:
        socket = getattr(channels, name)
        assert socket.enabled is False
        assert socket.allow_from == ["*"]
    with pytest.raises(AttributeError):
        channels.not_a_channel_anyone_ships
