"""What the harness forgives in a tool argument, and what it must not.

`cast_params` exists because a model's JSON is not the schema's JSON. Every
scalar type already forgives the string spelling of itself -- "3" for an
integer, "true" for a boolean. Arrays and objects did not, and a model that
means an array very often sends the array's JSON *as a string*: measured on one
install, every `deliver_files` call failed that way, five in a row in a single
session, and nothing was ever delivered.

The forgiving has an edge, and these say where it is: a string that is not the
type the schema asked for stays a string, so the validator can name the real
problem rather than the harness inventing one.
"""

from __future__ import annotations

import pytest

from raven.agent.tools.params import cast_params, validate_params

FILES = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "keep": {"type": "boolean"}},
                "required": ["path"],
            },
        },
        "message": {"type": "string"},
    },
    "required": ["files"],
}


def _round(schema: dict, params: dict) -> tuple[dict, list[str]]:
    """Cast then validate, the order the registry runs them in."""
    cast = cast_params(schema, params)
    return cast, validate_params(schema, cast)


def test_an_array_sent_as_its_own_json_is_taken_as_the_array() -> None:
    """The exact shape that failed: `files` carrying the array's JSON text."""
    cast, errors = _round(
        FILES,
        {
            "files": '[{"path": "/w/poster.png"}]',
            "message": "done",
        },
    )

    assert errors == []
    assert cast["files"] == [{"path": "/w/poster.png"}]


def test_the_items_inside_a_parsed_array_are_cast_too() -> None:
    """Otherwise the fix would stop one level short of where it started.

    A model that stringifies the array is the same model that writes "true" for
    a boolean, and the per-item casting the list branch already does has to
    reach the items that arrived this way.
    """
    cast, errors = _round(FILES, {"files": '[{"path": "/w/a.png", "keep": "true"}]'})

    assert errors == []
    assert cast["files"] == [{"path": "/w/a.png", "keep": True}]


def test_an_object_sent_as_its_own_json_is_taken_as_the_object() -> None:
    schema = {"type": "object", "properties": {"opts": {"type": "object"}}}

    cast, errors = _round(schema, {"opts": '{"depth": 2}'})

    assert errors == []
    assert cast["opts"] == {"depth": 2}


def test_a_string_that_is_not_json_is_left_for_the_validator_to_report() -> None:
    """The point is to widen what is accepted, not to swallow a type error.

    Returned untouched rather than turned into a one-element list: a model that
    sent prose where an array belongs made a mistake, and the message naming it
    is the useful thing.
    """
    cast, errors = _round(FILES, {"files": "the poster"})

    assert cast["files"] == "the poster"
    assert errors == ["files should be array"]


def test_json_of_the_wrong_type_is_also_left_alone() -> None:
    """Parsing succeeded and still did not produce what the schema asked for."""
    cast, errors = _round(FILES, {"files": '{"path": "/w/a.png"}'})

    assert cast["files"] == '{"path": "/w/a.png"}'
    assert errors == ["files should be array"]


def test_a_string_field_holding_json_text_stays_a_string() -> None:
    """A tripwire, and it is worth saying that it is only that.

    A `message`, a file's `content`, a commit body: JSON is ordinary text in a
    string field, and parsing it would replace the argument with something the
    tool never asked for. Today two independent things prevent it -- the
    `type == "string"` branch returns before the parse is reached, and the
    parse only wins when it yields the type the schema asked for -- so no
    single mutation makes this case red, and it is not load-bearing. It is here
    for the edit that widens the tuple or moves the block, which would have to
    defeat both.
    """
    cast, errors = _round(
        FILES,
        {
            "files": [{"path": "/w/a.png"}],
            "message": '{"not": "an object"}',
        },
    )

    assert errors == []
    assert cast["message"] == '{"not": "an object"}'


def test_a_real_list_is_unaffected() -> None:
    """The path that already worked keeps working, items and all."""
    cast, errors = _round(FILES, {"files": [{"path": "/w/a.png", "keep": "false"}]})

    assert errors == []
    assert cast["files"] == [{"path": "/w/a.png", "keep": False}]


@pytest.mark.parametrize("value", ["", "[", "null", "[1, 2", "NaN-ish"])
def test_no_input_makes_the_cast_raise(value: str) -> None:
    """Turning "your argument has the wrong type" into a traceback would be
    worse than the refusal it replaces."""
    cast = cast_params(FILES, {"files": value})

    assert isinstance(validate_params(FILES, cast), list)


def test_a_deeply_nested_string_is_refused_rather_than_blowing_the_stack() -> None:
    """`json.loads` answers this with `RecursionError`, which is not a
    `ValueError` -- so a decoder guarded on the usual two lets it through.

    The repository has met this before and wrote it down:
    `agent/tools/ask_user.py::_loads` carries the same exception list for the
    same reason, and `test_deeply_nested_json_does_not_kill_the_turn` pins it
    there. Escaping here is worse than escaping there: this runs on every tool
    call, and `ToolRegistry.execute` turns it into `Error executing ...
    maximum recursion depth exceeded` in place of the argument error the model
    could have acted on.
    """
    cast = cast_params(FILES, {"files": "[" * 20_000 + "]" * 20_000})

    assert isinstance(cast["files"], str)
    assert validate_params(FILES, cast) == ["files should be array"]
