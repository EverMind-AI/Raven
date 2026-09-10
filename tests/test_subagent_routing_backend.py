"""The routing entry: one row's backend picking its implementation on ``run``.

Every caller that dispatches to a row -- spawn, a DAG node, a direct chat --
resolves ``registry.backend(name)`` and calls ``run`` on it, so the table
handing back a ``RoutingBackend`` for a row with ``routes`` is what puts the
gate on every path at once. The shipped manifests are routed here too: the
fakes cannot show that the real roster lines send a deck the right way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from raven.agent.subagent.backends.routing import RoutingBackend, without_file_references
from raven.agent.subagent.registry import AgentRegistry
from raven.config.schema import ThirdPartyAcpSubagentConfig

REPO = Path(__file__).resolve().parent.parent


class _Backend:
    kind = "acp"
    streams = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.runs: list[dict[str, Any]] = []
        self.bound: list[Any] = []

    async def run(self, task: str, **kwargs: Any) -> str:
        self.runs.append({"task": task, **kwargs})
        return f"{self.name} did it"

    def bind_session_dir(self, resolver: Any) -> None:
        self.bound.append(resolver)


class _Instances:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    def list_instances(self, session_key: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["sessionKey"] == session_key]


class _Router:
    def __init__(self, answer: str | Exception | None) -> None:
        self.answer = answer
        self.asked: list[tuple[list[tuple[str, str]], str, str]] = []

    async def __call__(self, menu: list[tuple[str, str]], task: str, default: str) -> str | None:
        self.asked.append((menu, task, default))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _entry(
    router: _Router | None = None, instances: _Instances | None = None
) -> tuple[RoutingBackend, _Backend, _Backend]:
    design, deck = _Backend("Design"), _Backend("Deck")
    entry = RoutingBackend(
        "Design", design, [("Deck", r"\.pptx\b|\bdeck\b", "builds a .pptx", deck)], instances=instances or _Instances()
    )
    entry.set_router(router)
    return entry, design, deck


async def _run(entry: RoutingBackend, task: str, **kwargs: Any) -> str:
    return await entry.run(task, task_id="t1", workspace=Path("/tmp"), executor=None, **kwargs)


async def test_a_task_naming_the_deliverable_routes_without_the_classifier() -> None:
    router = _Router("Design")
    entry, design, deck = _entry(router)

    assert (
        await _run(entry, "Turn report.md into a .pptx", session_key="s1", instance="h1", mode="high") == "Deck did it"
    )
    assert deck.runs[0]["instance"] == "h1" and deck.runs[0]["session_key"] == "s1"
    # The lane's own keywords reach the implementation untouched; the mode is
    # the one that went missing once (see test_subagent_mode_wiring.py).
    assert deck.runs[0]["mode"] == "high" and deck.runs[0]["task_id"] == "t1"
    assert design.runs == [] and router.asked == []


async def test_an_ambiguous_task_is_classified_against_the_targets_lines_only() -> None:
    router = _Router("Deck")
    entry, _, deck = _entry(router)

    assert await _run(entry, "Something for the board meeting", session_key="s1") == "Deck did it"
    menu, task, default = router.asked[0]
    assert menu == [("Deck", "builds a .pptx")], "the entry's own line argues the host's case, not this one"
    assert task == "Something for the board meeting" and default == "Design"
    assert deck.runs[0]["session_key"] == "s1"


async def test_no_answer_a_stranger_or_a_failure_keeps_the_task_on_the_entry() -> None:
    for answer in ("Design", "Coder", None, RuntimeError("down")):
        entry, design, deck = _entry(_Router(answer))
        assert await _run(entry, "a poster") == "Design did it", answer
        assert deck.runs == []


async def test_without_a_classifier_an_ambiguous_task_stays_on_the_entry() -> None:
    entry, design, _ = _entry(None)

    assert await _run(entry, "a poster") == "Design did it"


async def test_the_route_reads_the_authored_task_and_never_the_rendered_one() -> None:
    """The two-value boundary. A lane renders file inputs into ``task`` and keeps
    the model's own words in ``authored_task``; the rendered text here begins
    with a presentation's source, and the authored text asks for a poster."""
    router = _Router("Design")
    entry, design, deck = _entry(router)
    rendered = "SOURCE PRESENTATION NOTES: slide deck for the board\n" * 200 + "Make a poster of the highlights"

    assert await _run(entry, rendered, authored_task="Make a poster of the highlights") == "Design did it"
    assert deck.runs == []
    ((menu, classified, _),) = router.asked
    assert classified == "Make a poster of the highlights"
    assert "SOURCE PRESENTATION NOTES" not in classified
    # The implementation still gets the rendered task, and the authored one beside it.
    assert design.runs[0]["task"] == rendered and design.runs[0]["authored_task"] == "Make a poster of the highlights"


class _V14Implementation:
    """Enumerates the paper's parameters as they stood before ``authored_task``, no ``**kwargs``."""

    kind = "acp"
    streams = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.runs: list[str] = []

    async def run(
        self,
        task,
        *,
        task_id,
        workspace,
        executor,
        session_key=None,
        instance=None,
        provider=None,
        model=None,
        mcps=None,
        mcp_grant=None,
        mode=None,
        on_delta=None,
    ) -> str:
        self.runs.append(task)
        return f"{self.name} did it"


async def test_an_implementation_typed_against_the_earlier_paper_is_not_handed_the_authored_task() -> None:
    """The entry's own hand-off is a dispatch lane too: whichever implementation
    it selects, primary or target, may enumerate the paper's parameters with no
    ``**kwargs`` and must keep running with an authored task present."""
    design, deck = _V14Implementation("Design"), _V14Implementation("Deck")
    entry = RoutingBackend("Design", design, [("Deck", r"\.pptx\b", "builds a .pptx", deck)], instances=_Instances())
    entry.set_router(_Router("Design"))

    assert (
        await _run(entry, "Turn {{ ref:/x/report.md }} into a .pptx", authored_task="Turn it into a .pptx")
        == "Deck did it"
    )
    assert await _run(entry, "a poster of {{ ref:/x/report.md }}", authored_task="a poster of it") == "Design did it"
    assert deck.runs == ["Turn {{ ref:/x/report.md }} into a .pptx"] and design.runs == [
        "a poster of {{ ref:/x/report.md }}"
    ]


async def test_a_file_that_happens_to_be_a_deck_is_not_a_deck_being_asked_for() -> None:
    router = _Router("Design")
    entry, design, deck = _entry(router)

    assert await _run(entry, "create a poster from /data/keynote.pptx") == "Design did it"
    assert await _run(entry, "{{ ref:/data/report.pptx }}\nMake a poster of it") == "Design did it"
    assert await _run(entry, "summarise @deck.pptx as a poster") == "Design did it"
    assert len(router.asked) == 3, "with the references out, nothing named a deck; the classifier decides"
    assert await _run(entry, "build a 10-slide deck from /data/keynote.pptx") == "Deck did it"
    assert await _run(entry, "Export the summary as a .pptx") == "Deck did it"
    assert len(router.asked) == 3


def test_file_references_are_taken_out_and_deliverable_words_stay() -> None:
    assert without_file_references("a poster from /data/keynote.pptx and ~/x.key").split() == [
        "a",
        "poster",
        "from",
        "and",
    ]
    assert without_file_references(r"open C:\\decks\\q3.pptx").split() == ["open"]
    assert without_file_references("{{ inputs.source }}\nExport as a .pptx").split() == ["Export", "as", "a", ".pptx"]
    assert without_file_references("read @notes.md then make slides").split() == ["read", "then", "make", "slides"]


async def test_a_reused_handle_continues_where_its_transport_bound_it() -> None:
    """Two turns whose classifier answers differ: the follow-up has no deck word
    and would classify as Design, and the handle Deck already bound still wins.
    The manager's status row under the entry's own name carries no session and
    is not evidence; a session Design bound is."""
    instances = _Instances(
        [
            {"sessionKey": "s1", "agent": "Design", "handle": "h1", "agentId": None},
            {"sessionKey": "s1", "agent": "Deck", "handle": "h1", "agentId": "acp-session-9"},
            {"sessionKey": "s1", "agent": "Design", "handle": "h2", "agentId": "acp-session-4"},
        ]
    )
    router = _Router("Design")
    entry, design, deck = _entry(router, instances)

    assert await _run(entry, "apply the review notes", session_key="s1", instance="h1") == "Deck did it"
    assert router.asked == []
    # A handle the entry's own transport bound stays there even when the text names a deck.
    assert await _run(entry, "now also a .pptx of it", session_key="s1", instance="h2") == "Design did it"
    # The same handle in another session is nobody's yet.
    assert await _run(entry, "apply the review notes", session_key="s2", instance="h1") == "Design did it"
    assert len(router.asked) == 1


def test_binders_reach_every_implementation_and_reads_come_off_the_entrys_own() -> None:
    """A target's own row is on the table but nothing addresses it, so the
    manager binds only the entry; the entry has to pass the binding on."""
    entry, design, deck = _entry()
    resolver = object()

    entry.bind_session_dir(resolver)

    assert design.bound == [resolver] and deck.bound == [resolver]
    assert entry.kind == "acp" and entry.streams is True
    assert entry.implementations() == [design, deck]


class TestTheTableHandsBackTheEntry:
    """``registry.backend`` is what spawn, the DAG runner and a direct chat all
    resolve, so a routing entry there is a gate on every path."""

    @staticmethod
    def _table(*, deck_enabled: bool = True) -> AgentRegistry:
        registry = AgentRegistry(build_builtin=lambda row, narrowed: None)
        registry.apply(
            [
                ThirdPartyAcpSubagentConfig(
                    name="Design", command="design-agent", routes=[{"to": "Deck", "match": r"\.pptx"}]
                ),
                ThirdPartyAcpSubagentConfig(
                    name="Deck", command="deck-agent", description="builds a .pptx", hidden=True, enabled=deck_enabled
                ),
            ]
        )
        return registry

    def test_a_row_with_routes_is_served_as_a_routing_entry(self) -> None:
        registry = self._table()

        entry = registry.backend("Design")
        assert isinstance(entry, RoutingBackend)
        assert [b.name for b in entry.implementations()] == ["Design", "Deck"]
        assert not isinstance(registry.backend("Deck"), RoutingBackend)

    def test_a_disabled_target_leaves_the_row_unwrapped(self) -> None:
        assert not isinstance(self._table(deck_enabled=False).backend("Design"), RoutingBackend)

    async def test_the_classifier_set_on_the_table_reaches_the_entry_before_and_after_a_build(self) -> None:
        registry = self._table()
        router = _Router("Deck")

        registry.set_router(router)
        assert registry.backend("Design")._router is router
        registry.apply(
            [
                ThirdPartyAcpSubagentConfig(name="Design", command="design-agent", routes=[{"to": "Deck"}]),
                ThirdPartyAcpSubagentConfig(name="Deck", command="deck-agent", hidden=True),
            ]
        )
        assert registry.backend("Design")._router is router


class TestTheShippedManifestsRoute:
    """The real roster lines, through the real entry."""

    @staticmethod
    def _table() -> AgentRegistry:
        rows = []
        for folder in ("raven-design", "raven-ppt"):
            entry = json.loads((REPO / "agents" / folder / "subagent.json").read_text(encoding="utf-8"))
            rows.append(ThirdPartyAcpSubagentConfig.model_validate({**entry, "command": "true"}))
        registry = AgentRegistry(build_builtin=lambda row, narrowed: None)
        registry.apply(rows)
        return registry

    async def test_a_deck_request_reaches_the_deck_engine_without_a_model_call(self) -> None:
        entry = self._table().backend("Raven-Design")
        assert isinstance(entry, RoutingBackend)
        router = _Router("Raven-Design")
        entry.set_router(router)

        for task in (
            "Build a 10-slide deck from /data/report.md",
            "Make a PPT about Shanghai's city plan",
            "Turn these notes into a presentation for Friday",
            "{{ inputs.source }}\nExport the summary as a .pptx",
        ):
            assert (await entry.pick(task, session_key="s1", instance=None))[0] == "Raven-PPT", task
        assert router.asked == []

    async def test_the_classifier_reads_the_deck_engines_line_and_never_the_entrys_own(self) -> None:
        registry = self._table()
        entry = registry.backend("Raven-Design")
        router = _Router("Raven-Design")
        entry.set_router(router)

        assert (await entry.pick("A poster for the spring concert", session_key="s1", instance=None))[
            0
        ] == "Raven-Design"
        ((menu, _, default),) = router.asked
        design = registry.get("Raven-Design")
        assert design is not None and default == "Raven-Design"
        assert menu == [("Raven-PPT", registry.get("Raven-PPT").description)]
        assert design.owns not in str(menu) and design.description not in str(menu)
