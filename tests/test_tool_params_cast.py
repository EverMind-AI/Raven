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

And the rejection is the model's only feedback, so it has to carry what the model
cannot see: where the problem is, what was wanted, and what actually arrived.
"""

from __future__ import annotations

import pytest

from raven.agent.tools.params import ParamError, cast_params, render_param_errors, validate_params

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


QUESTIONS = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "header": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "recommended": {"type": "integer"},
                },
                "required": ["question"],
            },
        }
    },
    "required": ["questions"],
}

NODES = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "node_summary": {"type": "string"}},
                "required": ["id", "node_summary"],
            },
        }
    },
    "required": ["nodes"],
}


def _round(schema: dict, params: dict) -> tuple[dict, list[str]]:
    """Cast then validate, the order the registry runs them in."""
    cast = cast_params(schema, params)
    return cast, validate_params(schema, cast)


def _got(error: str) -> str:
    """The half of a type error that describes what arrived, so a key name
    found there cannot have come from the path the error starts with."""
    return error.split(", got ", 1)[1]


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
    assert len(errors) == 1
    assert errors[0].startswith("files should be array")
    assert "string" in _got(errors[0])


def test_json_of_the_wrong_type_is_also_left_alone() -> None:
    """Parsing succeeded and still did not produce what the schema asked for."""
    cast, errors = _round(FILES, {"files": '{"path": "/w/a.png"}'})

    assert cast["files"] == '{"path": "/w/a.png"}'
    assert len(errors) == 1
    assert errors[0].startswith("files should be array")
    assert "object" in _got(errors[0])


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
    errors = validate_params(FILES, cast)
    assert len(errors) == 1
    assert errors[0].startswith("files should be array")


def test_an_object_where_a_string_belongs_is_reported_with_its_keys() -> None:
    """The caster used to `str()` anything a string field received, so an object
    passed as its own Python repr and the validator never saw it at all."""
    cast, errors = _round(
        QUESTIONS,
        {"questions": [{"question": "Q?", "options": [{"description": "d", "question": "q"}, "plain"]}]},
    )

    assert cast["questions"][0]["options"][0] == {"description": "d", "question": "q"}
    assert len(errors) == 1
    assert errors[0].startswith("questions[0].options[0] should be string")
    assert "description" in _got(errors[0])
    assert "question" in _got(errors[0])


def test_a_number_for_a_string_is_still_taken_but_a_boolean_is_not() -> None:
    """A number spells the same text either way; `str(True)` is Python's "True",
    which is not what a model that sent JSON `true` wrote."""
    schema = {"type": "object", "properties": {"label": {"type": "string"}}}

    cast, errors = _round(schema, {"label": 3})
    assert cast["label"] == "3"
    assert errors == []

    cast, errors = _round(schema, {"label": True})
    assert cast["label"] is True
    assert len(errors) == 1
    assert errors[0].startswith("label should be string")


def test_a_required_key_written_one_level_too_deep_is_found_where_it_went() -> None:
    """The live ask_user failure: `question` sat inside each option, and all the
    model heard was that `question` was missing -- so it resent the same shape."""
    _, errors = _round(
        QUESTIONS, {"questions": [{"header": "Scope", "options": [{"question": "A?"}, {"question": "B?"}]}]}
    )

    missing = [e for e in errors if e.startswith("missing required questions[0].question")]
    assert len(missing) == 1
    assert "questions[0].options[0]" in missing[0]
    assert "questions[0].options[1]" in missing[0]


def test_a_required_key_sent_under_a_near_name_is_suggested() -> None:
    """Live: DAG nodes carried `task_summary` where the schema wants `node_summary`."""
    _, errors = _round(NODES, {"nodes": [{"id": "a", "task_summary": "t"}]})

    assert len(errors) == 1
    assert errors[0].startswith("missing required nodes[0].node_summary")
    assert "task_summary" in errors[0]
    assert errors[0].count("node_summary") >= 2, "the suggestion names the key it meant"


def test_keys_the_schema_does_not_define_are_listed_when_a_required_one_is_missing() -> None:
    """Nothing near enough to suggest, so the model is at least shown what it sent."""
    schema = {
        "type": "object",
        "properties": {"project": {"type": "string"}, "takeaway": {"type": "string"}},
        "required": ["project", "takeaway"],
    }

    _, errors = _round(schema, {"ppt_title": "Q3 review", "argument": "costs fell"})

    text = "\n".join(errors)
    assert "missing required project" in text
    assert "missing required takeaway" in text
    assert "ppt_title" in text
    assert "argument" in text


STRICT = {
    "type": "object",
    "properties": {"question": {"type": "string"}, "multi_select": {"type": "boolean"}},
    "required": ["question"],
    "additionalProperties": False,
}


def test_a_key_a_closed_schema_forbids_is_rejected_with_the_name_it_was_meant_to_be() -> None:
    """`additionalProperties: false` was declared and never read: the DAG node
    schema says it, and calls carrying an extra key ran anyway."""
    _, errors = _round(STRICT, {"question": "Q?", "multiSelect": True})

    assert len(errors) == 1
    assert "multiSelect" in errors[0]
    assert "multi_select" in errors[0]


def test_a_key_an_open_schema_does_not_mention_is_still_allowed() -> None:
    """Only a schema that closes itself turns an unknown key into an error; MCP
    servers publish loose schemas, and their calls must keep running."""
    open_schema = {k: v for k, v in STRICT.items() if k != "additionalProperties"}

    assert _round(open_schema, {"question": "Q?", "multiSelect": True})[1] == []


def test_extra_keys_are_cast_and_checked_against_an_additional_properties_schema() -> None:
    schema = {"type": "object", "properties": {}, "additionalProperties": {"type": "integer"}}

    cast, errors = _round(schema, {"retries": "3"})
    assert cast == {"retries": 3}
    assert errors == []

    _, errors = _round(schema, {"retries": "three"})
    assert len(errors) == 1
    assert errors[0].startswith("retries should be integer")


def test_an_array_followed_by_more_text_in_its_string_names_the_extra_text() -> None:
    """Live `image_generate`: the array was complete, and the rest of the arguments
    -- `, "quality": "high"}` -- had ended up inside the same string."""
    _, errors = _round(FILES, {"files": '[{"path": "/w/a.png"}], "message": "uploaded both images"}'})

    assert len(errors) == 1
    assert errors[0].startswith("files should be array")
    assert ', "message": "uploaded both images"}' in errors[0], "longer than a window around one point"


def test_a_string_of_broken_json_says_where_parsing_stopped() -> None:
    _, errors = _round(FILES, {"files": '[{"path": "/w/a.png" "keep": true}]'})

    assert len(errors) == 1
    assert errors[0].startswith("files should be array")
    assert "Expecting ',' delimiter" in errors[0]
    assert "char 21" in errors[0]
    assert '"keep"' in errors[0], "the text around the failure is quoted"


def test_one_object_where_an_array_belongs_is_told_to_wrap_it() -> None:
    """Live `update_task_state`: `operations` was the single operation itself."""
    _, errors = _round(FILES, {"files": {"path": "/w/a.png"}})

    assert len(errors) == 1
    assert errors[0].startswith("files should be array")
    assert "path" in _got(errors[0])
    assert "wrap" in _got(errors[0])


def test_a_long_value_is_described_not_echoed() -> None:
    """The error is model text that also lands in history and on screen."""
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
    value = "x" * 300

    _, errors = _round(schema, {"count": value})

    assert len(errors) == 1
    assert errors[0].startswith("count should be integer")
    assert value not in errors[0]
    assert "300" in _got(errors[0])


def test_an_enum_miss_names_the_value_that_arrived() -> None:
    schema = {"type": "object", "properties": {"role": {"type": "string", "enum": ["cover", "agenda"]}}}

    _, errors = _round(schema, {"role": "content"})

    assert len(errors) == 1
    assert errors[0].startswith("role must be one of")
    assert "'content'" in errors[0]


def test_schema_patterns_are_left_to_the_tools_own_validator() -> None:
    schema = {"type": "object", "properties": {"handle": {"type": "string", "pattern": r"^\S(?:.*\S)?$"}}}

    assert _round(schema, {"handle": "build-scanner"})[1] == []
    assert _round(schema, {"handle": " padded "})[1] == []


def test_a_pattern_python_cannot_compile_is_skipped_rather_than_raising() -> None:
    r"""JSON Schema patterns are ECMA-262: `\p{L}` is legal there and an error in `re`."""
    schema = {"type": "object", "properties": {"name": {"type": "string", "pattern": r"^\p{L}+$"}}}

    assert _round(schema, {"name": "anything"})[1] == []


def test_declared_item_counts_are_enforced() -> None:
    too_few = _round(FILES, {"files": []})[1]
    assert len(too_few) == 1
    assert too_few[0].startswith("files")
    assert "at least 1" in too_few[0]

    schema = {"type": "object", "properties": {"tags": {"type": "array", "maxItems": 2}}}
    too_many = _round(schema, {"tags": ["a", "b", "c"]})[1]
    assert len(too_many) == 1
    assert "at most 2" in too_many[0]
    assert "3" in too_many[0]


def test_a_repeated_item_is_named_when_items_must_be_unique() -> None:
    schema = {"type": "object", "properties": {"tags": {"type": "array", "uniqueItems": True}}}

    assert _round(schema, {"tags": ["a", "b"]})[1] == []
    errors = _round(schema, {"tags": ["a", "b", "a"]})[1]
    assert len(errors) == 1
    assert "tags[2]" in errors[0]
    assert "tags[0]" in errors[0]


def test_a_const_is_enforced_and_the_value_that_arrived_is_named() -> None:
    schema = {"type": "object", "properties": {"version": {"const": "v1"}}}

    assert _round(schema, {"version": "v1"})[1] == []
    errors = _round(schema, {"version": "v2"})[1]
    assert len(errors) == 1
    assert "'v1'" in errors[0]
    assert "'v2'" in errors[0]

    numeric = {"type": "object", "properties": {"version": {"const": 1}}}
    assert _round(numeric, {"version": 1})[1] == []
    assert len(_round(numeric, {"version": True})[1]) == 1, "JSON's true is not the number 1, though Python's is"


def test_a_wrap_hint_is_given_only_where_a_list_of_objects_is_wanted() -> None:
    """Wrapping is the fix only when the object could be one of the items."""
    schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}

    _, errors = _round(schema, {"tags": {"name": "a"}})

    assert len(errors) == 1
    assert "name" in _got(errors[0])
    assert "wrap" not in errors[0]


def test_a_case_only_difference_is_suggested_too() -> None:
    """Similarity alone scores `URL` against `url` at zero; spelling is folded first."""
    schema = {"type": "object", "properties": {"url": {"type": "string"}}, "additionalProperties": False}

    _, errors = _round(schema, {"URL": "https://example.com"})

    assert len(errors) == 1
    assert "URL" in errors[0]
    assert "did you mean url?" in errors[0], "a suggestion, not the list of allowed keys"


@pytest.mark.parametrize(
    ("value", "described"),
    [
        (None, "null"),
        (True, "boolean true"),
        (3.5, "number 3.5"),
        ([1, 2], "an array of 2 items"),
        ({}, "an empty object"),
        ("ten", "a string 'ten'"),
        ((1, 2), "tuple"),
    ],
)
def test_what_arrived_is_named_by_its_type(value: object, described: str) -> None:
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}}

    _, errors = _round(schema, {"count": value})

    assert len(errors) == 1
    assert described in _got(errors[0])


def test_an_object_with_many_keys_lists_some_and_counts_the_rest() -> None:
    schema = {"type": "object", "properties": {"label": {"type": "string"}}}

    _, errors = _round(schema, {"label": {f"k{i}": i for i in range(10)}})

    got = _got(errors[0])
    assert "k0" in got and "k7" in got
    assert "k8" not in got
    assert "2 more" in got


def test_a_key_found_in_many_places_names_a_few_and_counts_the_rest() -> None:
    options = [{"question": f"Q{i}?"} for i in range(5)]

    _, errors = _round(QUESTIONS, {"questions": [{"options": options}]})

    missing = next(e for e in errors if e.startswith("missing required questions[0].question"))
    assert "questions[0].options[2]" in missing
    assert "questions[0].options[3]" not in missing
    assert "2 more" in missing


def test_the_shape_line_spells_each_field_with_its_type() -> None:
    """What the model sees of the level it lost track of: names, optionality, types."""
    item = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "role": {"type": "string", "enum": ["cover", "agenda"]},
            "note": {"type": ["string", "null"]},
            "pages": {"type": "array", "items": {"type": "object", "properties": {"n": {"type": "integer"}}}},
            "version": {"const": 2},
            "meta": {"type": "object"},
            "anything": True,
        },
        "required": ["id"],
    }

    shape = render_param_errors([ParamError("missing required slides[0].id", "slides[0]", item)]).splitlines()[-1]

    assert shape.startswith("Expected slides[0]: {")
    for field in (
        "id: string",
        'role?: "cover" | "agenda"',
        "note?: string | null",
        "pages?: {...}[]",
        "version?: 2",
        "meta?: object",
        "anything?: any",
    ):
        assert field in shape


def test_a_wide_shape_is_cut_rather_than_spelled_out() -> None:
    item = {"type": "object", "properties": {f"field_{i:02d}": {"type": "string"} for i in range(40)}}

    shape = render_param_errors([ParamError("x", "rows[0]", item)]).splitlines()[-1]

    assert shape.endswith(", ...}")
    assert "field_00" in shape
    assert "field_39" not in shape


def test_a_closed_schema_that_declares_no_keys_says_so() -> None:
    schema = {"type": "object", "properties": {}, "additionalProperties": False}

    _, errors = _round(schema, {"anything": 1})

    assert len(errors) == 1
    assert "anything is not allowed here" in errors[0]
    assert errors[0].endswith("allowed: none")


def test_a_declared_range_is_enforced_with_the_value_that_arrived() -> None:
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer", "minimum": 3, "maximum": 7}},
    }

    assert _round(schema, {"count": 5})[1] == []

    low = _round(schema, {"count": 2})[1]
    assert len(low) == 1
    assert low[0].startswith("count must be >= 3")
    assert "2" in low[0]

    high = _round(schema, {"count": 9})[1]
    assert len(high) == 1
    assert high[0].startswith("count must be <= 7")
    assert "9" in high[0]


def test_a_declared_length_range_is_enforced_with_the_length_that_arrived() -> None:
    schema = {
        "type": "object",
        "properties": {"handle": {"type": "string", "minLength": 3, "maxLength": 5}},
    }

    assert _round(schema, {"handle": "abcd"})[1] == []

    short = _round(schema, {"handle": "ab"})[1]
    assert len(short) == 1
    assert short[0].startswith("handle must be at least 3 chars")
    assert "2" in short[0]

    long = _round(schema, {"handle": "abcdefgh"})[1]
    assert len(long) == 1
    assert long[0].startswith("handle must be at most 5 chars")
    assert "8" in long[0]


def test_an_item_that_cannot_be_compared_for_uniqueness_is_not_a_crash() -> None:
    """`json.dumps` answers a self-referencing item with `ValueError`; a schema
    asking for unique items must not take the turn down over one."""
    schema = {"type": "object", "properties": {"tags": {"type": "array", "uniqueItems": True}}}
    circular: list[object] = []
    circular.append(circular)

    errors = _round(schema, {"tags": [circular]})[1]

    assert errors == [], "nothing to compare against yet, and no exception either"


@pytest.mark.parametrize("extra", [False, {"type": "integer"}])
def test_pattern_properties_keep_their_original_values_for_the_tool(extra) -> None:
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "patternProperties": {"^x_": {"type": "string"}},
        "additionalProperties": extra,
    }
    original = {"count": "2", "x_a": "007"}
    cast, errors = _round(schema, original)
    assert errors == []
    assert cast == {"count": 2, "x_a": "007"}
    assert original == {"count": "2", "x_a": "007"}
    assert _round(schema, {"x_a": "v"})[1][0].startswith("missing required count")
    assert _round(schema, {"count": "bad", "x_a": "v"})[1][0].startswith("count should be integer")


@pytest.mark.parametrize("keyword", ["minItems", "maxItems"])
@pytest.mark.parametrize("bound", ["1", None, True, False, -1, 1.5, [], {}])
def test_invalid_array_bounds_do_not_reject_valid_arguments(keyword, bound) -> None:
    schema = {"type": "object", "properties": {"items": {"type": "array", keyword: bound}}}
    assert _round(schema, {"items": []})[1] == []
    assert _round(schema, {"items": ["a", "b"]})[1] == []


def test_zero_array_bounds_remain_constraints() -> None:
    schema = {"type": "object", "properties": {"items": {"type": "array", "maxItems": 0}}}
    assert _round(schema, {"items": []})[1] == []
    assert "at most 0 items" in _round(schema, {"items": ["a"]})[1][0]


@pytest.mark.parametrize("nested", [False, True])
def test_wide_schema_suggestions_have_a_call_wide_comparison_budget(monkeypatch, nested) -> None:
    from raven.agent.tools import params as module

    comparisons = []
    original = module.difflib.get_close_matches

    def counted(word, possibilities, **kwargs):
        possibilities = list(possibilities)
        comparisons.extend((word, candidate) for candidate in possibilities)
        assert len(comparisons) <= 256, "diagnostic work must stop before comparing every pair"
        assert len(word) <= 64 and all(len(p) <= 64 for p in possibilities)
        return original(word, possibilities, **kwargs)

    monkeypatch.setattr(module.difflib, "get_close_matches", counted)
    schema = {
        "type": "object",
        "properties": {f"field_{i:04d}": {"type": "integer"} for i in range(400)},
        "additionalProperties": False,
    }
    payload = {f"fielx_{i:04d}": i for i in range(400)}
    if nested:
        schema = {"type": "object", "properties": {"rows": {"type": "array", "items": schema}}}
        payload = {"rows": [payload, payload]}
    errors = validate_params(schema, payload)
    assert errors and comparisons
    assert any("fielx_0399 is not allowed" in error for error in errors)
    valid = {"field_0399": 1}
    assert validate_params(schema, {"rows": [valid, valid]} if nested else valid) == []


@pytest.mark.parametrize("nested", [False, True])
def test_misplaced_key_search_has_a_budget_shared_by_missing_fields(nested) -> None:
    class CountedDict(dict):
        visits = 0

        def __contains__(self, key):
            type(self).visits += 1
            assert type(self).visits <= 256, "wide input must not be walked for every missing field"
            return super().__contains__(key)

    payload = {"rows": [CountedDict({"other": i}) for i in range(1000)]}
    schema = {"type": "object", "required": [f"k{i}" for i in range(25)]}
    if nested:
        schema = {"type": "object", "properties": {"groups": {"type": "array", "items": schema}}}
        payload = {"groups": [payload, payload]}
    errors = validate_params(schema, payload)
    path = "groups[1]." if nested else ""
    assert any(f"missing required {path}k24" in e for e in errors)
    assert CountedDict.visits > 0


def test_long_key_diagnostics_are_bounded_without_changing_lookup() -> None:
    key = "nested_" + "z" * 2000
    keys = [f"k{i}" for i in range(25)]
    schema = {"type": "object", "required": keys, "properties": {k: {"type": "integer"} for k in keys}}
    errors = validate_params(schema, {key: dict.fromkeys(keys, 1)})
    text = render_param_errors(errors)
    assert len(text) <= 3500
    assert all(len(e) <= 512 for e in errors)
    assert "missing required k0" in text and "nested_" in text and "..." in text
    assert key not in text
    exact = {"type": "object", "required": [key], "properties": {key: {"type": "integer"}}}
    assert _round(exact, {key: "2"}) == ({key: 2}, [])


def test_a_long_first_shape_field_and_path_cannot_expand_the_rejection() -> None:
    key = "field_" + "z" * 10000
    shape = {"type": "object", "properties": {key: {"type": "string"}}, "required": [key]}
    text = render_param_errors([ParamError("x" * 10000, "rows." + key, shape)])
    assert len(text) <= 3500
    assert "Expected rows." in text and "field_" in text and "..." in text
    assert key not in text
