"""Tests for the attempt conversation rebuild (`raven.trajectory.conversation`)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven.trajectory import conversation as tconv

_T = "2026-09-01T10:00:"


def _ts(seconds: int) -> str:
    return f"{_T}{seconds:02d}+00:00"


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _write_log(state: Path, spans: list[dict]) -> None:
    path = state / "logs" / "audit-spans.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")


def _artifact(state: Path, payload, name: str) -> str:
    directory = state / "logs" / "audit-artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _span(trace, span_id, name, *, parent=None, start=0, end=None, attrs=None, status=None, raw_times=None):
    times = raw_times or {}
    return {
        "traceId": trace,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": name,
        "startTime": times.get("start", _ts(start)),
        "endTime": times.get("end", _ts(end if end is not None else start)),
        "status": status if status is not None else {"code": "OK", "message": ""},
        "attributes": attrs if attrs is not None else {},
    }


def _turn_attrs(state, name, content_in="fix the bug", content_out="done"):
    attrs = {"turn.input_preview": content_in[:20]}
    attrs["turn.input.artifact_path"] = _artifact(state, {"content": content_in}, f"{name}-in")
    if content_out is not None:
        attrs["turn.output_preview"] = content_out[:20]
        attrs["turn.output.artifact_path"] = _artifact(state, {"content": content_out}, f"{name}-out")
    return attrs


def _llm_attrs(state, name, messages, output=None, extra=None):
    attrs = {
        "llm.input.artifact_path": _artifact(state, {"messages": messages, "tools": [{"big": "schema"}]}, f"{name}-in")
    }
    if output is not None:
        attrs["llm.output.artifact_path"] = _artifact(state, output, f"{name}-out")
        preview = output.get("content") if isinstance(output, dict) else None
        if isinstance(preview, str):
            attrs["llm.output_preview"] = preview[:20]
    if extra:
        attrs.update(extra)
    return attrs


def _tool_attrs(state, name, params, result):
    return {
        "tool.name": name,
        "tool.args_preview": json.dumps(params)[:20],
        "tool.result_preview": str(result)[:20],
        "tool.input.artifact_path": _artifact(state, {"name": name, "params": params}, f"{name}-in"),
        "tool.output.artifact_path": _artifact(state, {"result": result}, f"{name}-out"),
    }


def _labels(records):
    return [r.label for r in records]


def _by_label(records, label):
    return [r for r in records if r.label == label]


# ── causal order ──────────────────────────────────────────────────────


def test_single_turn_causal_order(state):
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "fix the bug"}]
    _write_log(
        state,
        [
            _span("t1", "turn", "session.turn", start=0, end=30, attrs=_turn_attrs(state, "turn")),
            _span(
                "t1",
                "llm",
                "llm.call",
                parent="turn",
                start=1,
                end=5,
                attrs=_llm_attrs(state, "llm", messages, {"content": "on it", "tool_calls": []}),
            ),
            _span(
                "t1",
                "tool",
                "tool.call",
                parent="turn",
                start=6,
                end=8,
                attrs=_tool_attrs(state, "run", {"x": 1}, "ok"),
            ),
        ],
    )
    records = tconv.attempt_conversation(["t1"], state)
    assert _labels(records) == ["User input", "LLM input", "LLM output", "Tool input", "Tool output", "Agent reply"]
    assert records[0].text == "fix the bug"
    assert records[-1].text == "done"
    assert all(r.turn_span_id == "turn" for r in records)
    assert all(r.degraded is None for r in records)


def test_parent_output_after_children_on_shared_end_time(state):
    _write_log(
        state,
        [
            _span(
                "t1",
                "curate",
                "context.curate",
                start=1,
                end=3,
                attrs={"context.curate.output.artifact_path": _artifact(state, {"produced": True}, "cur-out")},
            ),
            _span(
                "t1",
                "llm",
                "llm.call",
                parent="curate",
                start=2,
                end=3,
                attrs=_llm_attrs(state, "llm", [{"role": "user", "content": "q"}], {"content": "a", "tool_calls": []}),
            ),
        ],
    )
    labels = _labels(tconv.attempt_conversation(["t1"], state))
    assert labels.index("LLM output") < labels.index("Context curate output")


@pytest.mark.parametrize(("turn_id", "llm_id"), [("a-turn", "z-llm"), ("z-turn", "a-llm")])
def test_identical_start_end_orders_by_nesting_not_span_id(state, turn_id, llm_id):
    messages = [{"role": "user", "content": "q"}]
    _write_log(
        state,
        [
            _span("t1", turn_id, "session.turn", start=0, end=0, attrs=_turn_attrs(state, "turn")),
            _span(
                "t1",
                llm_id,
                "llm.call",
                parent=turn_id,
                start=0,
                end=0,
                attrs=_llm_attrs(state, "llm", messages, {"content": "a", "tool_calls": []}),
            ),
        ],
    )
    assert _labels(tconv.attempt_conversation(["t1"], state)) == [
        "User input",
        "LLM input",
        "LLM output",
        "Agent reply",
    ]


def test_missing_end_time_keeps_output_after_input(state):
    span = _span(
        "t1", "turn", "session.turn", attrs=_turn_attrs(state, "turn"), raw_times={"start": _ts(1), "end": None}
    )
    _write_log(state, [span])
    labels = _labels(tconv.attempt_conversation(["t1"], state))
    assert labels == ["User input", "Agent reply"]


def test_missing_start_time_and_parent_cycle_degrade(state):
    spans = [
        _span("t1", "a", "tool.call", parent="b", start=1, attrs=_tool_attrs(state, "one", {}, "r1")),
        _span("t1", "b", "tool.call", parent="a", start=2, attrs=_tool_attrs(state, "two", {}, "r2")),
        _span("t1", "c", "session.turn", attrs=_turn_attrs(state, "turn"), raw_times={"start": None, "end": None}),
    ]
    _write_log(state, spans)
    records = tconv.attempt_conversation(["t1"], state)
    assert len(_by_label(records, "Tool output")) == 2
    assert records[0].label == "User input"


def test_logical_span_dedup_last_write_wins(state):
    checkpoint = _span("t1", "turn", "session.turn", start=0, end=0, attrs=_turn_attrs(state, "cp", content_out=None))
    checkpoint["attributes"]["turn.in_progress"] = True
    final = _span("t1", "turn", "session.turn", start=0, end=9, attrs=_turn_attrs(state, "fin"))
    _write_log(state, [checkpoint, final])
    records = tconv.attempt_conversation(["t1"], state)
    assert _labels(records) == ["User input", "Agent reply"]
    assert records[1].text == "done"


# ── LLM input increments ──────────────────────────────────────────────


def _two_calls(state, first, second, *, trace="t1", parent="turn", turn=True):
    spans = []
    if turn:
        spans.append(_span(trace, "turn", "session.turn", start=0, end=30, attrs=_turn_attrs(state, f"{trace}-turn")))
    spans.append(
        _span(trace, "l1", "llm.call", parent=parent, start=1, end=2, attrs=_llm_attrs(state, f"{trace}-l1", first))
    )
    spans.append(
        _span(trace, "l2", "llm.call", parent=parent, start=3, end=4, attrs=_llm_attrs(state, f"{trace}-l2", second))
    )
    return spans


def test_increment_omits_verified_prefix(state):
    base = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
    grown = [*base, {"role": "assistant", "content": "a"}, {"role": "tool", "content": "r", "tool_call_id": "c1"}]
    _write_log(state, _two_calls(state, base, grown))
    first, second = _by_label(tconv.attempt_conversation(["t1"], state), "LLM input")
    assert "sys" in first.text and first.degraded is None
    assert second.text.startswith("(… 2 earlier messages unchanged)")
    assert "sys" not in second.text
    assert "[tool #c1]" in second.text
    assert second.degraded is None


def test_equal_length_identical_input_is_annotation_only(state):
    base = [{"role": "user", "content": "q"}]
    _write_log(state, _two_calls(state, base, list(base)))
    _first, second = _by_label(tconv.attempt_conversation(["t1"], state), "LLM input")
    assert second.text == "(… 1 earlier messages unchanged)"


@pytest.mark.parametrize(
    "second",
    [
        [{"role": "user", "content": "REWRITTEN"}, {"role": "assistant", "content": "a"}],
        [
            {"role": "user", "content": "REWRITTEN"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "x"},
        ],
        [{"role": "user", "content": "q"}],
    ],
)
def test_rewritten_history_shows_full_messages(state, second):
    base = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    _write_log(state, _two_calls(state, base, second))
    _first, rec = _by_label(tconv.attempt_conversation(["t1"], state), "LLM input")
    assert rec.degraded == "history rewritten — full messages shown"
    assert "unchanged" not in rec.text
    for message in second:
        assert message["content"] in rec.text


def test_nested_chain_does_not_offset_main_loop(state):
    base = [{"role": "user", "content": "q"}]
    grown = [*base, {"role": "assistant", "content": "a"}]
    spans = _two_calls(state, base, grown)
    spans.append(_span("t1", "curate", "context.curate", parent="turn", start=2, end=2, attrs={}))
    spans.append(
        _span(
            "t1",
            "l-cur",
            "llm.call",
            parent="curate",
            start=2,
            end=2,
            attrs=_llm_attrs(state, "cur-llm", [{"role": "user", "content": "curator prompt"}]),
        )
    )
    _write_log(state, spans)
    records = _by_label(tconv.attempt_conversation(["t1"], state), "LLM input")
    curator = next(r for r in records if "curator prompt" in r.text)
    assert "unchanged" not in curator.text and curator.degraded is None
    main_second = next(r for r in records if r.span_id == "l2")
    assert main_second.text.startswith("(… 1 earlier messages unchanged)")


def test_merged_traces_do_not_offset_each_other(state):
    spans = _two_calls(
        state,
        [{"role": "user", "content": "q"}],
        [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
    )
    spans.append(_span("t2", "turn2", "session.turn", start=10, end=20, attrs=_turn_attrs(state, "t2-turn")))
    spans.append(
        _span(
            "t2",
            "l3",
            "llm.call",
            parent="turn2",
            start=11,
            end=12,
            attrs=_llm_attrs(state, "t2-l3", [{"role": "user", "content": "unrelated"}]),
        )
    )
    _write_log(state, spans)
    records = _by_label(tconv.attempt_conversation(["t1", "t2"], state), "LLM input")
    other = next(r for r in records if r.trace_id == "t2")
    assert other.text == "[user]\nunrelated"
    assert other.degraded is None


def test_main_chain_continues_across_turns(state):
    first = [{"role": "user", "content": "q"}]
    second_turn = [*first, {"role": "assistant", "content": "a"}, {"role": "user", "content": "next"}]
    spans = [
        _span("t1", "turn1", "session.turn", start=0, end=5, attrs=_turn_attrs(state, "turn1")),
        _span("t1", "la", "llm.call", parent="turn1", start=1, end=2, attrs=_llm_attrs(state, "la", first)),
        _span("t2", "turn2", "session.turn", start=10, end=15, attrs=_turn_attrs(state, "turn2")),
        _span("t2", "lb", "llm.call", parent="turn2", start=11, end=12, attrs=_llm_attrs(state, "lb", second_turn)),
    ]
    _write_log(state, spans)
    records = _by_label(tconv.attempt_conversation(["t1", "t2"], state), "LLM input")
    cross = next(r for r in records if r.trace_id == "t2")
    assert cross.text.startswith("(… 1 earlier messages unchanged)")
    assert "next" in cross.text


def test_unreadable_previous_call_recovers(state):
    base = [{"role": "user", "content": "q"}]
    grown = [*base, {"role": "assistant", "content": "a"}]
    more = [*grown, {"role": "user", "content": "again"}]
    spans = [
        _span("t1", "turn", "session.turn", start=0, end=30, attrs=_turn_attrs(state, "turn")),
        _span("t1", "l1", "llm.call", parent="turn", start=1, end=2, attrs=_llm_attrs(state, "l1", base)),
        _span(
            "t1",
            "l2",
            "llm.call",
            parent="turn",
            start=3,
            end=4,
            attrs={"llm.input.artifact_path": str(state / "logs" / "gone.json")},
        ),
        _span("t1", "l3", "llm.call", parent="turn", start=5, end=6, attrs=_llm_attrs(state, "l3", grown)),
        _span("t1", "l4", "llm.call", parent="turn", start=7, end=8, attrs=_llm_attrs(state, "l4", more)),
    ]
    _write_log(state, spans)
    records = {r.span_id: r for r in _by_label(tconv.attempt_conversation(["t1"], state), "LLM input")}
    assert records["l2"].degraded == "content unavailable — artifact missing"
    assert records["l3"].degraded == "previous call unreadable — full messages shown"
    assert "unchanged" not in records["l3"].text
    assert records["l4"].text.startswith("(… 2 earlier messages unchanged)")


# ── LLM output and message rendering ──────────────────────────────────


def test_output_keeps_content_and_tool_calls(state):
    output = {
        "content": "let me check",
        "tool_calls": [{"id": "c9", "name": "run_command", "arguments": {"cmd": "ls"}}],
        "reasoning_content": "think hard",
    }
    attrs = _llm_attrs(
        state,
        "llm",
        [{"role": "user", "content": "q"}],
        output,
        extra={"llm.model": "claude-sonnet-4-6", "llm.usage.input_tokens": 12, "llm.usage.output_tokens": 3},
    )
    _write_log(state, [_span("t1", "llm", "llm.call", start=1, end=2, attrs=attrs)])
    records = tconv.attempt_conversation(["t1"], state)
    thinking = _by_label(records, "LLM thinking")[0]
    output_rec = _by_label(records, "LLM output")[0]
    assert thinking.text == "think hard"
    assert "let me check" in output_rec.text
    assert '→ tool call run_command#c9({"cmd": "ls"})' in output_rec.text
    assert output_rec.meta == "claude-sonnet-4-6 · in 12 / out 3 tok"


def test_unexecuted_tool_call_arguments_stay_visible(state):
    output = {"content": None, "tool_calls": [{"id": "c1", "name": "danger", "arguments": '{"a": 1}'}]}
    attrs = _llm_attrs(state, "llm", [{"role": "user", "content": "q"}], output)
    _write_log(state, [_span("t1", "llm", "llm.call", start=1, end=2, attrs=attrs)])
    output_rec = _by_label(tconv.attempt_conversation(["t1"], state), "LLM output")[0]
    assert output_rec.text == '→ tool call danger#c1({"a": 1})'


def test_history_message_structured_fields_render(state):
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "h1", "type": "function", "function": {"name": "old_tool", "arguments": '{"k": 2}'}}],
        },
        {
            "role": "assistant",
            "content": "hm",
            "reasoning_content": "prior thought",
            "cache_control": {"type": "ephemeral"},
        },
        {"role": "user", "content": "q"},
    ]
    attrs = _llm_attrs(state, "llm", messages)
    _write_log(state, [_span("t1", "llm", "llm.call", start=1, end=2, attrs=attrs)])
    text = _by_label(tconv.attempt_conversation(["t1"], state), "LLM input")[0].text
    assert '→ tool call old_tool#h1({"k": 2})' in text
    assert "[reasoning]\nprior thought" in text
    assert 'cache_control: {"type": "ephemeral"}' in text


def test_long_system_message_is_fully_rendered(state):
    system = "S" * 2000
    attrs = _llm_attrs(state, "llm", [{"role": "system", "content": system}, {"role": "user", "content": "q"}])
    _write_log(state, [_span("t1", "llm", "llm.call", start=1, end=2, attrs=attrs)])
    text = _by_label(tconv.attempt_conversation(["t1"], state), "LLM input")[0].text
    assert system in text


# ── span kind mappings ────────────────────────────────────────────────


def test_real_artifact_key_mappings(state):
    spans = [
        _span(
            "t1",
            "recall",
            "memory.recall",
            start=1,
            attrs={"memory.recall.artifact_path": _artifact(state, [{"text": "remembered"}], "recall")},
        ),
        _span(
            "t1",
            "store",
            "memory.store",
            start=2,
            attrs={"memory.store.artifact_path": _artifact(state, {"messages": ["m"]}, "store")},
        ),
        _span(
            "t1",
            "pers",
            "personalize.classify",
            start=3,
            attrs={
                "personalize.input.artifact_path": _artifact(state, {"query": "who"}, "pers-in"),
                "personalize.output.artifact_path": _artifact(state, {"result": "casual"}, "pers-out"),
            },
        ),
        _span(
            "t1",
            "sread",
            "skill.read",
            start=4,
            attrs={
                "skill.name": "weather",
                "tool.output.artifact_path": _artifact(state, {"result": "## weather\nbody"}, "sread"),
            },
        ),
        _span(
            "t1",
            "sinj",
            "skill.inject",
            start=5,
            attrs={
                "skill.inject.artifact_path": _artifact(
                    state,
                    {"via": "skills_segment", "skills": [{"name": "weather", "id": "hub/weather"}], "body_len": 42},
                    "sinj",
                )
            },
        ),
        _span(
            "t1", "sub", "subagent.run", start=6, attrs={"subagent.task": "explore repo", "subagent.label": "explorer"}
        ),
        _span(
            "t1",
            "custom",
            "custom.thing",
            start=7,
            attrs={"custom.blob.artifact_path": _artifact(state, {"z": 1}, "custom")},
        ),
        _span("t1", "plug", "plugin.load", start=8, attrs={"plugin.name": "everos", "plugin.contribution": "tool"}),
    ]
    _write_log(state, spans)
    records = tconv.attempt_conversation(["t1"], state)
    by_label = {r.label: r for r in records}
    assert "remembered" in by_label["Memory recall"].text
    assert "messages" in by_label["Memory store"].text
    assert "who" in by_label["Personalize classify input"].text
    assert "casual" in by_label["Personalize classify output"].text
    assert by_label["Skill read"].text == "## weather\nbody"
    assert by_label["Skill read"].meta == "weather"
    assert by_label["Skill inject"].text == "skills: weather\nvia skills_segment · body 42 chars"
    assert by_label["Subagent"].text == "explore repo"
    assert by_label["Subagent"].meta == "explorer"
    assert '"z": 1' in by_label["Custom blob"].text
    assert '"plugin.name": "everos"' in by_label["Plugin load"].text
    assert by_label["Memory recall"].kind == "memory"
    assert by_label["Skill read"].kind == "skill"


# ── payload dedup ─────────────────────────────────────────────────────


def _pair_span(state, first_payload, second_payload, *, first_name="p1", second_name="p2"):
    attrs = {
        "foo.a.artifact_path": first_payload
        if isinstance(first_payload, str)
        else _artifact(state, first_payload, first_name),
        "foo.b.artifact_path": _artifact(state, second_payload, second_name),
    }
    return _span("t1", "s1", "foo.pair", start=1, attrs=attrs)


def test_identical_full_payloads_dedup_by_content(state):
    _write_log(state, [_pair_span(state, {"same": True}, {"same": True})])
    records = tconv.attempt_conversation(["t1"], state)
    assert '"same": true' in records[0].text
    assert records[1].text == "same content as Foo a above"


def test_same_sha1_attribute_different_content_never_dedups(state):
    span = _pair_span(state, {"v": 1}, {"v": 2})
    span["attributes"]["foo.a.artifact_sha1"] = "deadbeef"
    span["attributes"]["foo.b.artifact_sha1"] = "deadbeef"
    _write_log(state, [span])
    records = tconv.attempt_conversation(["t1"], state)
    assert '"v": 1' in records[0].text
    assert '"v": 2' in records[1].text


def test_unreadable_or_truncated_first_payload_is_no_dedup_target(state):
    missing = str(state / "logs" / "gone.json")
    _write_log(state, [_pair_span(state, missing, {"x": 1})])
    records = tconv.attempt_conversation(["t1"], state)
    assert records[0].degraded == "content unavailable — artifact missing"
    assert '"x": 1' in records[1].text

    big = "B" * (tconv._ARTIFACT_LIMIT + 10)
    span = _span(
        "t1",
        "s2",
        "foo.pair",
        start=1,
        attrs={
            "foo.a.artifact_path": _artifact(state, big, "big1"),
            "foo.b.artifact_path": _artifact(state, big, "big2"),
        },
    )
    _write_log(state, [span])
    records = tconv.attempt_conversation(["t1"], state)
    assert records[0].degraded == "content over 512 KiB — truncated"
    assert records[1].text != "same content as Foo a above"


# ── degradation and safety ────────────────────────────────────────────


def test_missing_artifact_falls_back_to_preview(state):
    attrs = {"turn.input.artifact_path": str(state / "logs" / "gone.json"), "turn.input_preview": "short preview"}
    _write_log(state, [_span("t1", "turn", "session.turn", attrs=attrs)])
    record = tconv.attempt_conversation(["t1"], state)[0]
    assert record.text == "short preview"
    assert record.degraded == "artifact missing — truncated preview"


def test_artifact_path_outside_store_is_rejected(state, tmp_path):
    outside = tmp_path / "evil.json"
    outside.write_text('{"stolen": true}', encoding="utf-8")
    attrs = {"turn.input.artifact_path": str(outside)}
    _write_log(state, [_span("t1", "turn", "session.turn", attrs=attrs)])
    record = tconv.attempt_conversation(["t1"], state)[0]
    assert record.text == ""
    assert record.degraded == "content unavailable — artifact path outside the trace store"


def test_invalid_json_artifact_shows_raw_text(state):
    attrs = {"turn.input.artifact_path": _artifact(state, "not json {{", "raw")}
    _write_log(state, [_span("t1", "turn", "session.turn", attrs=attrs)])
    record = tconv.attempt_conversation(["t1"], state)[0]
    assert record.text == "not json {{"
    assert record.degraded == "artifact is not valid JSON — shown raw"


def test_error_span_without_body_still_yields_record(state):
    _write_log(state, [_span("t1", "boom", "tool.call", start=1, status={"code": "ERROR", "message": "exploded"})])
    records = tconv.attempt_conversation(["t1"], state)
    assert len(records) == 1
    assert records[0].error == "exploded"
    assert records[0].label == "Tool call"


def test_error_attaches_to_last_record(state):
    attrs = _tool_attrs(state, "run", {"x": 1}, "Error: nope")
    _write_log(
        state,
        [_span("t1", "tool", "tool.call", start=1, end=2, attrs=attrs, status={"code": "ERROR", "message": "bad"})],
    )
    records = tconv.attempt_conversation(["t1"], state)
    assert records[-1].label == "Tool output"
    assert records[-1].error == "bad"
    assert records[0].error is None


def test_one_sided_artifact_loss_keeps_placeholder_and_other_side(state):
    attrs = _tool_attrs(state, "run", {"x": 1}, "fine")
    attrs["tool.input.artifact_path"] = str(state / "logs" / "gone.json")
    del attrs["tool.args_preview"]
    _write_log(state, [_span("t1", "tool", "tool.call", start=1, end=2, attrs=attrs)])
    records = tconv.attempt_conversation(["t1"], state)
    assert records[0].label == "Tool input"
    assert records[0].degraded == "content unavailable — artifact missing"
    assert records[1].text == "fine"
    assert records[1].degraded is None


def test_malformed_spans_stay_visible_among_normal_ones(state):
    good = _span("t1", "turn", "session.turn", start=0, end=9, attrs=_turn_attrs(state, "turn"))
    bad_attrs = _span("t1", "bad1", "tool.call", parent="turn", start=1)
    bad_attrs["attributes"] = "garbage"
    bad_status = _span("t1", "bad2", "llm.call", parent="turn", start=2)
    bad_status["status"] = "ERROR"
    bad_pointer = _span("t1", "bad3", "tool.call", parent="turn", start=3, attrs={"tool.input.artifact_path": 123})
    _write_log(state, [good, bad_attrs, bad_status, bad_pointer])
    records = tconv.attempt_conversation(["t1"], state)
    by_span = {r.span_id: r for r in records}
    assert by_span["bad1"].degraded == "span record malformed — original status/attributes unreadable"
    assert by_span["bad2"].degraded == "span record malformed — original status/attributes unreadable"
    assert by_span["bad3"].degraded == "content unavailable — artifact missing"
    assert _by_label(records, "User input")[0].text == "fix the bug"
    assert _labels(records)[0] == "User input"
    assert _labels(records)[-1] == "Agent reply"


def test_error_status_survives_malformed_attributes(state):
    span = _span("t1", "bad", "tool.call", start=1, status={"code": "ERROR", "message": "kept"})
    span["attributes"] = ["not", "a", "dict"]
    _write_log(state, [span])
    records = tconv.attempt_conversation(["t1"], state)
    assert len(records) == 1
    assert records[0].degraded == "span record malformed — original status/attributes unreadable"
    assert records[0].error == "kept"


def test_empty_attempt_and_silent_events(state):
    assert tconv.attempt_conversation(["t1"], state) == []
    _write_log(state, [_span("t1", "quiet", "custom.noop", start=1, attrs={"session.key": "cli:a"})])
    assert tconv.attempt_conversation(["t1"], state) == []


def test_other_traces_are_ignored(state):
    _write_log(
        state,
        [
            _span("t1", "turn", "session.turn", attrs=_turn_attrs(state, "turn")),
            _span("other", "turn2", "session.turn", attrs=_turn_attrs(state, "other-turn")),
        ],
    )
    records = tconv.attempt_conversation(["t1"], state)
    assert {r.trace_id for r in records} == {"t1"}


def test_artifact_read_is_bounded_and_rejects_non_regular_files(state, monkeypatch):
    big = state / "logs" / "audit-artifacts" / "big.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"B" * (2 * 1024 * 1024))
    reads: list[int] = []
    original_open = Path.open

    class _SpyHandle:
        def __init__(self, handle):
            self._handle = handle

        def read(self, n=-1):
            reads.append(n)
            return self._handle.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

    def spy_open(self, *args, **kwargs):
        handle = original_open(self, *args, **kwargs)
        return _SpyHandle(handle) if self.name == "big.bin" else handle

    monkeypatch.setattr(Path, "open", spy_open)
    text, degraded = tconv._read_artifact(state, str(big))
    assert degraded == "content over 512 KiB — truncated"
    assert len(text) == tconv._ARTIFACT_LIMIT
    assert reads == [tconv._ARTIFACT_LIMIT + 1]

    fifo = state / "logs" / "audit-artifacts" / "pipe"
    os.mkfifo(fifo)
    assert tconv._read_artifact(state, str(fifo)) == (None, "artifact is not a regular file")


def test_empty_turn_yields_marker_record(state):
    _write_log(state, [_span("t1", "turn", "session.turn", start=1, end=2)])
    records = tconv.attempt_conversation(["t1"], state)
    assert len(records) == 1
    marker = records[0]
    assert marker.label == "Turn"
    assert marker.text == ""
    assert marker.degraded is None
    assert marker.turn_span_id == "turn"
    assert marker.event_time == _ts(1)


def test_empty_turn_between_full_turns_keeps_identity(state):
    spans = [
        _span("t1", "turn1", "session.turn", start=0, end=1, attrs=_turn_attrs(state, "t1-turn")),
        _span("t2", "turn2", "session.turn", start=2, end=3),
        _span("t3", "turn3", "session.turn", start=4, end=5, attrs=_turn_attrs(state, "t3-turn")),
    ]
    _write_log(state, spans)
    records = tconv.attempt_conversation(["t1", "t2", "t3"], state)
    assert _labels(records) == ["User input", "Agent reply", "Turn", "User input", "Agent reply"]
    assert records[2].turn_span_id == "turn2"


def test_bodyless_turn_root_with_children_yields_marker_first(state):
    spans = [
        _span("t1", "turn", "session.turn", start=0, end=9),
        _span(
            "t1",
            "llm",
            "llm.call",
            parent="turn",
            start=1,
            end=2,
            attrs=_llm_attrs(state, "llm", [{"role": "user", "content": "q"}], {"content": "a", "tool_calls": []}),
        ),
    ]
    _write_log(state, spans)
    records = tconv.attempt_conversation(["t1"], state)
    assert _labels(records) == ["Turn", "LLM input", "LLM output"]
    assert all(r.turn_span_id == "turn" for r in records)


def test_thinking_blocks_render_when_reasoning_content_absent(state):
    output = {
        "content": "answer",
        "tool_calls": [],
        "reasoning_content": None,
        "thinking_blocks": [
            {"type": "thinking", "thinking": "block thought", "signature": "sig"},
            {"type": "redacted_thinking", "data": "opaque"},
        ],
    }
    attrs = _llm_attrs(state, "llm", [{"role": "user", "content": "q"}], output)
    _write_log(state, [_span("t1", "llm", "llm.call", start=1, end=2, attrs=attrs)])
    records = tconv.attempt_conversation(["t1"], state)
    thinking = _by_label(records, "LLM thinking")[0]
    assert "block thought" in thinking.text
    assert '"redacted_thinking"' in thinking.text
    assert _by_label(records, "LLM output")[0].text == "answer"


def test_thinking_blocks_do_not_duplicate_reasoning_content(state):
    output = {
        "content": "answer",
        "tool_calls": [],
        "reasoning_content": "same thought",
        "thinking_blocks": [{"type": "thinking", "thinking": "same thought"}],
    }
    attrs = _llm_attrs(state, "llm", [{"role": "user", "content": "q"}], output)
    _write_log(state, [_span("t1", "llm", "llm.call", start=1, end=2, attrs=attrs)])
    thinking = _by_label(tconv.attempt_conversation(["t1"], state), "LLM thinking")[0]
    assert thinking.text == "same thought"


def test_malformed_status_with_readable_body_stays_visible(state):
    span = _span("t1", "tool", "tool.call", start=1, end=2, attrs=_tool_attrs(state, "run", {"x": 1}, "normal"))
    span["status"] = "ERROR"
    _write_log(state, [span])
    records = tconv.attempt_conversation(["t1"], state)
    output = _by_label(records, "Tool output")[0]
    assert output.text == "normal"
    assert output.degraded is None
    evidence = _by_label(records, "Tool call")[0]
    assert evidence.degraded == "span record malformed — original status/attributes unreadable"


def test_output_only_turn_root_with_children_keeps_start_marker(state):
    spans = [
        _span("t1", "turn", "session.turn", start=1, end=9, attrs={"turn.output_preview": "late reply"}),
        _span("t1", "tool", "tool.call", parent="turn", start=3, end=4, attrs=_tool_attrs(state, "run", {}, "ok")),
    ]
    _write_log(state, spans)
    records = tconv.attempt_conversation(["t1"], state)
    assert _labels(records) == ["Turn", "Tool input", "Tool output", "Agent reply"]
    assert records[0].event_time == _ts(1)
    assert records[0].turn_span_id == "turn"


def test_output_only_turn_root_without_children_keeps_start_marker(state):
    _write_log(
        state, [_span("t1", "turn", "session.turn", start=1, end=9, attrs={"turn.output_preview": "late reply"})]
    )
    records = tconv.attempt_conversation(["t1"], state)
    assert _labels(records) == ["Turn", "Agent reply"]
    assert records[0].event_time == _ts(1)
    assert records[1].text == "late reply"


def test_error_on_output_only_turn_binds_to_reply_not_marker(state):
    span = _span(
        "t1",
        "turn",
        "session.turn",
        start=1,
        end=9,
        attrs={"turn.output_preview": "failed reply"},
        status={"code": "ERROR", "message": "failure at end"},
    )
    _write_log(state, [span])
    records = tconv.attempt_conversation(["t1"], state)
    assert _labels(records) == ["Turn", "Agent reply"]
    marker, reply = records
    assert marker.error is None
    assert reply.error == "failure at end"
    assert reply.event_time == _ts(9)


def test_error_on_bodyless_turn_gets_completion_placeholder(state):
    span = _span("t1", "turn", "session.turn", start=1, end=9, status={"code": "ERROR", "message": "boom"})
    _write_log(state, [span])
    records = tconv.attempt_conversation(["t1"], state)
    assert _labels(records) == ["Turn", "Session turn"]
    marker, placeholder = records
    assert marker.error is None
    assert marker.event_time == _ts(1)
    assert placeholder.error == "boom"
    assert placeholder.event_time == _ts(9)
