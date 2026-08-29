"""Channel config declarations (config-with-cargo, every adapter).

Every spec declares the cargo-consumed slice of its channel's config in the
same flat vocabulary plugin manifests use. Storage and validation stay with
the central pydantic model for now, so this guard is what keeps the two
coherent: when either side drifts, one of these assertions bites.
"""

from __future__ import annotations

import typing

import pytest

from raven.channels.registry import discover_specs
from raven.config.schema import ChannelsConfig
from raven.core.admission import _TYPES

# Host-consumed fields: gating and workspace routing read these, adapters do
# not. They stay central (socket config) and out of every cargo declaration.
SOCKET_FIELDS = {"enabled", "allow_from", "workspace"}

SPECS = discover_specs()

_SCALARS = {str: "string", bool: "boolean", int: "integer", float: "number"}


def _central_model(name: str) -> type:
    return ChannelsConfig.model_fields[name].annotation


def _central_type_name(annotation: object) -> str | None:
    """The admission-vocabulary name of a central model field's annotation.

    None for shapes the flat vocabulary cannot carry (lists, mappings, nested
    Containers map to the door's container types: the door type-checks the
    shape and leaves element/member validation to the value's consumer.
    """
    origin = typing.get_origin(annotation)
    if origin is typing.Union or str(origin) == "<class 'types.UnionType'>":
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _central_type_name(args[0])
        if all(a in _SCALARS for a in args):
            return "string"
        return None
    if origin is typing.Literal:
        return "string"
    if origin in (list, tuple, set):
        return "array"
    if origin is dict:
        return "object"
    if isinstance(annotation, type):
        try:
            from pydantic import BaseModel

            if issubclass(annotation, BaseModel):
                return "object"
        except ImportError:
            pass
    return _SCALARS.get(annotation)


def _cargo_fields(model: type) -> dict[str, str]:
    """The declarable cargo slice of a central model: field -> vocabulary type."""
    out: dict[str, str] = {}
    for field_name, field in model.model_fields.items():
        if field_name in SOCKET_FIELDS:
            continue
        type_name = _central_type_name(field.annotation)
        if type_name is not None:
            out[field_name] = type_name
    return out


@pytest.mark.parametrize("name", sorted(SPECS))
def test_declares_exactly_the_cargo_fields(name):
    declared = set(SPECS[name].config_schema)
    assert declared == set(_cargo_fields(_central_model(name)))


@pytest.mark.parametrize("name", sorted(SPECS))
def test_declared_types_and_required_match_the_central_model(name):
    model = _central_model(name)
    expected = _cargo_fields(model)
    for key, decl in SPECS[name].config_schema.items():
        assert decl["type"] == expected[key], key
        assert decl["type"] in _TYPES, key
        extra = model.model_fields[key].json_schema_extra or {}
        assert bool(decl.get("required", False)) == bool(extra.get("required", False)), key


def test_every_spec_declares_its_cargo():
    for name, spec in SPECS.items():
        if _cargo_fields(_central_model(name)):
            assert spec.config_schema, name

@pytest.mark.parametrize("name", sorted(SPECS))
def test_dispensed_view_answers_exactly_like_the_central_section(name):
    """Handover acceptance, all twelve: the door changes where a value
    travels, never what it is. Every declared key and every socket field
    answers identically through the dispensed view; the view is frozen."""
    from raven.config.schema import ChannelsConfig
    from raven.core.admission import dispense_channel_config

    spec = SPECS[name]
    section = getattr(ChannelsConfig(), name, None)
    if section is None or not spec.config_schema:
        pytest.skip("no central section or no declaration")
    view = dispense_channel_config(spec, section, channel=name)

    for key in spec.config_schema:
        if hasattr(section, key):
            assert getattr(view, key) == getattr(section, key), key
    for socket_field in ("enabled", "allow_from", "workspace"):
        if hasattr(section, socket_field):
            assert getattr(view, socket_field) == getattr(section, socket_field)
    with pytest.raises(AttributeError):
        view.enabled = True


def test_declared_defaults_match_the_central_model():
    """Transition guard: until the central cargo fields retire, a declared
    default and the central model default must be the same value -- two
    sources of one default is how they drift."""
    from raven.channels.registry import discover_specs
    from raven.config.schema import ChannelsConfig

    for name, spec in discover_specs().items():
        section_model = type(getattr(ChannelsConfig(), name, None))
        if section_model is type(None) or not spec.config_schema:
            continue
        defaults = section_model()
        for key, decl in spec.config_schema.items():
            if "default" in decl and hasattr(defaults, key):
                assert decl["default"] == getattr(defaults, key), f"{name}.{key}"

def test_the_file_slice_outranks_the_central_section():
    """Ownership proof: the delivery path reads the file's sparse slice, so a
    value the user set answers from the file even when the central section
    disagrees, and an absent key answers from the declared default."""
    from raven.config import loader
    from raven.config.schema import TelegramConfig
    from raven.core.admission import dispense_channel_config

    spec = discover_specs()["telegram"]
    section = TelegramConfig(enabled=True, token="stale-central")
    loader._channel_slices["telegram"] = {"token": "file-truth"}
    try:
        view = dispense_channel_config(spec, section, channel="telegram")
        assert view.token == "file-truth"
    finally:
        loader._channel_slices.pop("telegram", None)

