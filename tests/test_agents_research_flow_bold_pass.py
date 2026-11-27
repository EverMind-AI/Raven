"""The bold pass: a researched report's bold, checked and chosen anew by a side call when it does not hold.

Pinned here: what the check counts (names and figures, not clauses; the body, not the
title, tables or the closing sources), that the chosen bold replaces the body's own and
leaves every other character, the caps a chosen span must meet, the side call's failure
modes, the gate's once-a-turn and researched-only scope, and its place last in the chain.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import FlowConfig  # noqa: E402
from research_flow.flow import ToolHandles, build_chain  # noqa: E402
from research_flow.gates.bold_pass import BoldPassGate  # noqa: E402
from research_flow.gates.report_shape import ReportShapeGate  # noqa: E402
from research_flow.state import SessionStore  # noqa: E402
from research_flow.support import report_bold  # noqa: E402
from research_flow.support.report_bold import apply_bold, body_spans, check_bold, choose_bold, unbold  # noqa: E402
from research_flow.support.report_html import render_page  # noqa: E402

from raven.contracts.llm_provider import LLMResponse  # noqa: E402
from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402

_LINE = "Averaging the weights of several fine-tuned models lifts accuracy at no inference cost"
SEARCHED = [{"role": "user", "content": "q"}, {"role": "tool", "name": "web_search", "content": "results"}]


def _report(*paragraphs: str) -> str:
    body = "\n\n".join(paragraphs)
    return (
        "# Merging\n\n> **Model Soups** wins.\n\n## 1. Methods\n\n"
        f"{body}\n\n| a | b |\n|--|--|\n| **x** | y |\n\n## Sources\n- **A page**: https://a.example/x\n"
    )


def test_the_check_reads_names_and_figures_not_sentences_or_sources():
    named = f"{_LINE}, as **Model Soups** shows with **81.03%** top-1 ([paper](https://arxiv.org/abs/2203.05482))."
    sentence = f"**{_LINE}.** It needs no extra pass."
    check = check_bold(_report(named, sentence, _LINE, "Too short to count."))
    assert (check.held, check.total, check.long) == (1, 3, 1)
    listed = f"- {_LINE}, as **DARE** does.\n- {_LINE}, plainly."
    assert (check_bold(_report(listed)).held, check_bold(_report(listed)).total) == (1, 2)
    # Only the closing section is the list of sources: one on data sources mid-body is body.
    data = _report(_LINE + ".").replace("## 1. Methods", "## 1. Data sources")
    assert check_bold(data).total == 1
    # A Latin name runs longer than a CJK one and is still a name; a CJK comparison is not.
    assert report_bold.fits("Multi-Teacher On-Policy Distillation")
    assert not report_bold.fits("OpenHands SDK \u4ece 64.0% \u5230 70.4%")


@pytest.mark.parametrize(
    "paragraphs, fails",
    [
        ([f"{_LINE}, as **DARE** shows."] * 2 + [_LINE + "."] * 2, False),
        ([f"{_LINE}, as **DARE** shows."] + [_LINE + "."] * 3, True),
        ([_LINE + ", " + ", ".join(f"**{n}%**" for n in range(7)) + "."] + [f"{_LINE}, **DARE**."] * 3, True),
        ([f"**{_LINE}.**"] + [f"{_LINE}, **DARE**."] * 3, True),
        ([_LINE + "."] * 3, False),
    ],
    ids=["held", "short_of_the_floor", "dense", "a_clause", "too_short_to_judge"],
)
def test_what_fails_the_check(paragraphs, fails):
    assert check_bold(_report(*paragraphs)).fails(0.4) is fails


def test_the_spans_are_the_units_in_place():
    report = _report(f"{_LINE}.\ncontinued here.", f"- {_LINE}, first.\n  wrapped.\n- {_LINE}, second.")
    units = [report[a:b] for a, b in body_spans(report)]
    assert units == [f"{_LINE}.\ncontinued here.", f"- {_LINE}, first.\n  wrapped.", f"- {_LINE}, second."]
    fenced = _report("```\n## Not a heading\n" + _LINE + "\n```", _LINE + ".")
    assert [fenced[a:b] for a, b in body_spans(fenced)] == [_LINE + "."]


def test_chosen_bold_replaces_the_bodys_own_and_leaves_every_other_character():
    first = f"**{_LINE}.** **Model Soups** reached 81.03% ([Model Soups](https://a.example/Model-Soups))."
    second = f"{_LINE}, as `DARE` and DARE show: 90% ([2203.05482](https://arxiv.org/abs/2203.05482))."
    report = _report(first, second)
    picks = {
        1: ["Model Soups", "81.03%", "too many", "accuracy"],
        2: ["DARE", "not in it", "Averaging the weights of several fine-tuned models", "2203.05482"],
    }
    bolded, counters = apply_bold(report, picks)
    assert unbold(bolded) == unbold(report)
    body = bolded.split("## 1. Methods")[1].split("| a | b |")[0]
    assert body.count("**") == 2 * 4
    assert "**Model Soups** reached **81.03%**" in body and "[Model Soups](https://a.example/Model-Soups)" in body
    assert "`DARE` and **DARE** show" in body
    assert counters == {"set": 4, "dropped": 4} and "[2203.05482]" in body
    # Title, quote, table and sources keep their bold.
    assert "> **Model Soups** wins." in bolded and "| **x** |" in bolded and "- **A page**:" in bolded


def test_a_figure_bolded_beside_cjk_text_renders_bold_on_the_page():
    report = "# T\n\n## 1. \u7ed3\u679c\n\n" + "\u541e\u5410\u8fbe\u5230 Megatron \u7684 92%\u7684\u6c34\u5e73" * 4
    bolded, _ = apply_bold(report, {1: ["92%"]})
    assert "<strong>92%</strong>" in render_page(bolded, day="2026-11-26", language="zh")


class _Side:
    def __init__(self, content: str | None = None, *, finish: str = "stop", raises: BaseException | None = None):
        self.content, self.finish, self.raises = content, finish, raises
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return LLMResponse(content=self.content, finish_reason=self.finish)


_PLAIN = _report(*[_LINE + "."] * 4)


def test_the_side_call_gets_the_body_numbered_without_bold_and_its_picks_are_set():
    side = _Side('```json\n{"1": ["Averaging"], "3": ["accuracy"], "x": ["no"]}\n```')
    bolded, record = asyncio.run(choose_bold(side, _report(f"**{_LINE}.**", *[_LINE + "."] * 3)))
    listing = side.calls[0]["messages"][1]["content"]
    assert listing.startswith(f"[1] {_LINE}.\n\n[2] ") and "**" not in listing and "Sources" not in listing
    assert record == {"chosen": True, "set": 2, "dropped": 0}
    assert bolded.count("**Averaging**") == 1 and bolded.count("**accuracy**") == 1


@pytest.mark.parametrize(
    "side, reason",
    [
        (_Side("no json here"), "no_json"),
        (_Side('{"1": ["absent"]}'), "nothing_set"),
        (_Side('{"1": ["Averaging"]}', finish="length"), "no_answer:length"),
        (_Side(raises=RuntimeError("down")), "call_failed:RuntimeError"),
        (_Side(raises=asyncio.TimeoutError()), "timeout"),
    ],
)
def test_a_side_call_that_gives_nothing_usable_keeps_the_report(side, reason):
    bolded, record = asyncio.run(choose_bold(side, _PLAIN))
    assert bolded is None and record["reason"] == reason


class _Response:
    def __init__(self, content: str, has_tool_calls: bool = False) -> None:
        self.content = content
        self.has_tool_calls = has_tool_calls
        self.reasoning_content = None


def _after(gate: BoldPassGate, meta: dict, response: _Response, messages=None):
    ctx = AgentHookContext(
        session_key="t",
        iteration=5,
        messages=list(SEARCHED if messages is None else messages),
        response=response,
        metadata=meta,
    )
    return asyncio.run(gate.after_iteration(ctx))


def test_a_researched_report_short_of_bold_ships_with_the_chosen_bold_once_a_turn():
    side = _Side('{"1": ["Averaging"], "2": ["accuracy"], "3": ["Averaging"]}')
    gate, meta = BoldPassGate(side, floor=0.4), {}
    assert _after(gate, meta, _Response("calling", has_tool_calls=True)).short_circuit_result is None
    decision = _after(gate, meta, _Response(_PLAIN))
    assert unbold(decision.short_circuit_result) == unbold(_PLAIN).strip()
    assert decision.short_circuit_result.count("**Averaging**") == 2 and len(side.calls) == 1
    assert _after(gate, meta, _Response(_PLAIN)).short_circuit_result is None and len(side.calls) == 1


@pytest.mark.parametrize(
    "report, messages",
    [
        (_report(*[f"{_LINE}, as **DARE** shows."] * 4), None),
        (_PLAIN, [{"role": "user", "content": "q"}]),
    ],
    ids=["held", "not_researched"],
)
def test_a_report_that_holds_or_a_turn_that_did_not_research_is_left_alone(report, messages):
    side = _Side('{"1": ["Averaging"]}')
    decision = _after(BoldPassGate(side, floor=0.4), {}, _Response(report), messages)
    assert decision.short_circuit_result is None and side.calls == []


def test_a_failed_side_call_ships_the_report_as_written():
    decision = _after(BoldPassGate(_Side("nothing"), floor=0.4), {}, _Response(_PLAIN))
    assert decision.short_circuit_result is None and not decision.rollback


def _chain(tmp_path, cfg, side=None):
    observers = build_chain(
        cfg,
        _Side("unused"),
        max_iterations=40,
        context_window_tokens=65536,
        tools=ToolHandles(),
        store=SessionStore(tmp_path),
        side=side,
    )

    def inner(hook):
        while hasattr(hook, "inner"):
            hook = hook.inner
        return hook

    return [inner(o) for o in observers]


def test_off_by_default_and_last_in_the_chain_on_the_side_asked_for(tmp_path):
    assert not any(isinstance(g, BoldPassGate) for g in _chain(tmp_path, FlowConfig(enabled=True)))
    cfg = FlowConfig(enabled=True)
    cfg.final_shape.skim_floor = 0.4
    cfg.final_shape.report_bounce = True
    gates = _chain(tmp_path, cfg)
    assert isinstance(gates[-1], BoldPassGate) and gates[-1]._floor == 0.4 and gates[-1]._model is None
    kinds = [type(g) for g in gates]
    assert kinds.index(ReportShapeGate) < kinds.index(BoldPassGate)
    digest = _Side("digest")
    cfg.final_shape.bold_on_digest_provider = True
    gates = _chain(tmp_path, cfg, side=(digest, "deepseek/flash"))
    assert gates[-1]._provider is digest and gates[-1]._model == "deepseek/flash"
