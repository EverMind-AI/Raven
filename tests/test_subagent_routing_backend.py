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

from raven.agent.subagent.backends.routing import HOST_PREFIX, RouteTarget, RoutingBackend
from raven.agent.subagent.mode_tiers import turn_tier, turn_tier_in_force
from raven.agent.subagent.registry import AgentRegistry
from raven.agent.subagent.vendored_agents import _read_route_notes as read_route_notes
from raven.config.schema import ThirdPartyAcpSubagentConfig

REPO = Path(__file__).resolve().parent.parent

#: A route's own words for the lane that keeps its work. Short here because what
#: is under test is that the declaration travels, not what the shipped one says.
NOTE = "Hand back a .pptx you designed yourself."


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


#: What the shipped deck route declares; the default for an entry under test,
#: so a case about the gate does not have to restate why the gate applies.
NEEDS = ("image_generation", "image_search")
MIN_TIER = "max"


def _entry(
    router: _Router | None = None,
    instances: _Instances | None = None,
    target_ready: Any = None,
    *,
    owes: str = ".pptx",
    note: str = NOTE,
    needs: tuple[str, ...] = NEEDS,
    min_tier: str = MIN_TIER,
) -> tuple[RoutingBackend, _Backend, _Backend]:
    design, deck = _Backend("Design"), _Backend("Deck")
    entry = RoutingBackend(
        "Design",
        design,
        [RouteTarget("Deck", "builds a .pptx", deck, owes, note, needs, min_tier)],
        instances=instances or _Instances(),
        target_ready=target_ready,
    )
    entry.set_router(router)
    return entry, design, deck


async def _run(entry: RoutingBackend, task: str, **kwargs: Any) -> str:
    return await entry.run(task, task_id="t1", workspace=Path("/tmp"), executor=None, **kwargs)


def _declared_route() -> dict[str, Any]:
    """Raven-Design's own route entry, straight off the shipped manifest."""
    manifest = json.loads((REPO / "agents" / "raven-design" / "subagent.json").read_text(encoding="utf-8"))
    ((route,)) = manifest["routes"]
    return route


def _declared_note() -> str:
    """The prose that route points at, read the way discovery reads it."""
    route = _declared_route()
    return (REPO / "agents" / "raven-design" / route["noteFile"]).read_text(encoding="utf-8").strip()


async def test_a_task_naming_the_deliverable_still_uses_the_classifier() -> None:
    router = _Router("Deck")
    entry, design, deck = _entry(router)

    assert (
        await _run(entry, "Turn report.md into a .pptx", session_key="s1", instance="h1", mode="max") == "Deck did it"
    )
    assert deck.runs[0]["instance"] == "h1" and deck.runs[0]["session_key"] == "s1"
    # The lane's own keywords reach the implementation untouched; the mode is
    # the one that went missing once (see test_subagent_mode_wiring.py).
    assert deck.runs[0]["mode"] == "max" and deck.runs[0]["task_id"] == "t1"
    assert design.runs == []
    assert len(router.asked) == 1


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
    entry = RoutingBackend("Design", design, [("Deck", "builds a .pptx", deck)], instances=_Instances())
    entry.set_router(_Router("Deck"))

    assert (
        await _run(entry, "Turn {{ ref:/x/report.md }} into a .pptx", authored_task="Turn it into a .pptx")
        == "Deck did it"
    )
    entry.set_router(_Router("Design"))
    assert await _run(entry, "a poster of {{ ref:/x/report.md }}", authored_task="a poster of it") == "Design did it"
    assert deck.runs == ["Turn {{ ref:/x/report.md }} into a .pptx"] and design.runs == [
        "a poster of {{ ref:/x/report.md }}"
    ]


async def test_deck_words_and_file_references_do_not_override_the_classifier() -> None:
    router = _Router("Design")
    entry, _, deck = _entry(router)
    tasks = (
        "Do not make a PPT; make a poster",
        "create a poster from /data/keynote.pptx",
        "{{ ref:/data/report.pptx }}\nMake a poster of it",
        "summarise @deck.pptx as a poster",
        "build a 10-slide deck from /data/keynote.pptx",
        "Export the summary as a .pptx",
    )
    for task in tasks:
        assert await _run(entry, task) == "Design did it"
    assert [task for _, task, _ in router.asked] == list(tasks)
    assert deck.runs == []


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
                ThirdPartyAcpSubagentConfig(name="Design", command="design-agent", routes=[{"to": "Deck"}]),
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

    def test_the_readiness_probe_set_on_the_table_reaches_the_entry_before_and_after_a_build(self) -> None:
        """Injected where the classifier is, and for the same reason: the table
        knows which rows route, the host knows what it is credentialed for, and a
        hot apply rebuilds every entry."""
        registry = self._table()

        def probe() -> bool:
            return False

        registry.set_target_ready(probe)
        assert registry.backend("Design")._target_ready is probe
        registry.apply(
            [
                ThirdPartyAcpSubagentConfig(name="Design", command="design-agent", routes=[{"to": "Deck"}]),
                ThirdPartyAcpSubagentConfig(name="Deck", command="deck-agent", hidden=True),
            ]
        )
        assert registry.backend("Design")._target_ready is probe

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


class TestTheTierAndTheCredentialsDecideWhichLaneBuilds:
    """The same deck, two lanes: which one builds it is the deployment's answer.

    The classifier still says *what the task is* -- nothing here reads the task
    text. What is gated is whether the specialist lane may run the work it was
    named for, and when it may not the deck stays on the row's own
    implementation carrying the deliverable it still owes.
    """

    async def test_below_the_top_tier_the_deck_stays_here_and_the_deliverable_travels_with_it(self) -> None:
        for tier in ("medium", "high"):
            entry, design, deck = _entry(_Router("Deck"))

            with turn_tier(tier):
                answer = await _run(entry, "Turn report.md into a deck", authored_task="a deck of the report")

            assert answer == "Design did it", tier
            assert deck.runs == [], tier
            run = design.runs[0]
            # The rendered task and the authored one both carry it: which of the
            # two an implementation reads is its own choice.
            assert run["task"] == f"Turn report.md into a deck{HOST_PREFIX}{NOTE}", tier
            assert run["authored_task"] == f"a deck of the report{HOST_PREFIX}{NOTE}", tier

    async def test_the_top_tier_routes_to_the_target_with_the_task_untouched(self) -> None:
        """Max is the template lane, and it is told nothing it did not already know."""
        entry, design, deck = _entry(_Router("Deck"))

        with turn_tier("max"):
            assert await _run(entry, "a deck of the report", authored_task="a deck") == "Deck did it"

        assert design.runs == []
        assert deck.runs[0]["task"] == "a deck of the report" and deck.runs[0]["authored_task"] == "a deck"

    async def test_no_turn_scope_an_empty_tier_and_a_foreign_one_all_leave_the_target_open(self) -> None:
        """Three different facts, one answer, and none of them is a low tier.

        ``None`` is no turn scope at all -- a direct-chat dispatch or this test
        rig; ``""`` is a turn that began without one; a word off the ladder is
        another vocabulary. Reading any of them as "below max" would close the
        template lane on every direct chat ever opened against this row.
        """
        assert turn_tier_in_force() is None
        entry, _, deck = _entry(_Router("Deck"))
        assert await _run(entry, "a deck") == "Deck did it"
        assert deck.runs[0]["task"] == "a deck"

        for tier in ("", "swift"):
            entry, _, deck = _entry(_Router("Deck"))
            with turn_tier(tier):
                assert await _run(entry, "a deck") == "Deck did it", tier
            assert deck.runs[0]["task"] == "a deck", tier

    async def test_without_the_credentials_its_lane_spends_the_top_tier_stays_here_too(self) -> None:
        """The flat answer: a lane that cannot buy a picture builds the same deck
        at every rung, so the tier stops deciding anything."""
        for tier in ("medium", "high", "max"):
            entry, design, deck = _entry(_Router("Deck"), target_ready=lambda _target, _needs: False)

            with turn_tier(tier):
                assert await _run(entry, "a deck") == "Design did it", tier

            assert deck.runs == [], tier
            assert design.runs[0]["task"] == f"a deck{HOST_PREFIX}{NOTE}", tier

    async def test_outside_a_turn_the_credentials_still_decide(self) -> None:
        """The two conditions are independent: no turn scope silences the tier
        rule and nothing else."""
        entry, design, deck = _entry(_Router("Deck"), target_ready=lambda _target, _needs: False)

        assert await _run(entry, "a deck") == "Design did it"
        assert deck.runs == [] and design.runs[0]["task"] == f"a deck{HOST_PREFIX}{NOTE}"

    async def test_work_the_classifier_left_here_is_never_told_it_owes_a_deck(self) -> None:
        """A poster at medium is not a deck that lost its lane. The note rides the
        classifier's answer, not the tier, or every visual request below max
        would be told to hand back a deck nobody asked for."""
        entry, design, deck = _entry(_Router("Design"))

        with turn_tier("medium"):
            assert await _run(entry, "a poster for the spring concert") == "Design did it"

        assert design.runs[0]["task"] == "a poster for the spring concert"
        assert HOST_PREFIX not in design.runs[0]["task"]

    async def test_a_handle_the_target_already_bound_is_not_re_gated(self) -> None:
        """A conversation already open on the target continues there at any tier:
        the gate picks a lane for new work, and moving a bound conversation
        mid-thread would answer as an agent holding none of its history."""
        instances = _Instances([{"sessionKey": "s1", "agent": "Deck", "handle": "h1", "agentId": "acp-session-9"}])
        router = _Router("Design")
        entry, _, deck = _entry(router, instances, target_ready=lambda _target, _needs: False)

        with turn_tier("medium"):
            assert await _run(entry, "apply the review notes", session_key="s1", instance="h1") == "Deck did it"

        assert router.asked == []
        assert deck.runs[0]["task"] == "apply the review notes"

    async def test_a_route_that_declares_no_note_hands_the_closed_task_over_untouched(self) -> None:
        """The gate is generic and the words are the row's. A route that declares
        none is a route with nothing to say, and the lane gets the task as the
        dispatching model wrote it -- not a sentence about somebody else's
        deliverable that happens to be compiled into the gate."""
        for tier in ("medium", "high"):
            entry, design, deck = _entry(_Router("Deck"), owes="", note="")

            with turn_tier(tier):
                assert await _run(entry, "a deck", authored_task="a deck") == "Design did it", tier

            assert deck.runs == [], tier
            assert design.runs[0]["task"] == "a deck", tier
            assert design.runs[0]["authored_task"] == "a deck", tier

    async def test_an_owed_file_with_nothing_to_say_still_appends_nothing(self) -> None:
        """``owes`` is the structured fact the gate logs; the note is the prose.
        Declaring the first without the second says nothing to the lane."""
        entry, design, _ = _entry(_Router("Deck"), note="")

        with turn_tier("medium"):
            await _run(entry, "a deck")

        assert design.runs[0]["task"] == "a deck"

    async def test_a_readiness_probe_that_raises_leaves_the_route_as_it_was(self) -> None:
        """A probe is best-effort storage of a fact about the deployment; a
        failure to read it must not silently re-route every deck."""

        def boom(_target: str, _needs: Any) -> bool:
            raise RuntimeError("the config could not be read")

        entry, _, deck = _entry(_Router("Deck"), target_ready=boom)

        with turn_tier("max"):
            assert await _run(entry, "a deck") == "Deck did it"

        assert deck.runs[0]["task"] == "a deck"


class TestADirectChatIsGatedByItsOwnMode:
    """The surface the user actually talks to a row through.

    A direct chat opens no turn, so the gate that reads the turn's frozen tier
    read nothing and let every chat through to the target at every setting --
    which meant no setting on the page could exercise the other lane, and a user
    who believed they were talking to Raven-Design had the whole exchange handed
    to Raven-PPT. The tier is not absent there, it is somewhere else: the
    ``mode`` the manager resolved for the dispatch, which is the instance's own
    override or the session's standing tier.
    """

    async def test_below_the_top_tier_the_chat_keeps_the_deck_here(self) -> None:
        for mode in ("medium", "high"):
            entry, design, deck = _entry(_Router("Deck"))

            assert turn_tier_in_force() is None
            assert await _run(entry, "a deck", instance="h1", mode=mode) == "Design did it", mode

            assert deck.runs == [], mode
            assert design.runs[0]["task"] == f"a deck{HOST_PREFIX}{NOTE}", mode
            # The mode still reaches the implementation: the gate reads it, it
            # does not consume it.
            assert design.runs[0]["mode"] == mode, mode

    async def test_the_top_tier_chat_reaches_the_target_untouched(self) -> None:
        entry, design, deck = _entry(_Router("Deck"))

        assert await _run(entry, "a deck", instance="h1", mode="max") == "Deck did it"

        assert design.runs == []
        assert deck.runs[0]["task"] == "a deck" and deck.runs[0]["mode"] == "max"

    async def test_a_chat_that_names_no_mode_is_left_as_it_was(self) -> None:
        """A deployment that never set a tier did not ask for this gate, and
        reading "nothing declared" as "below max" would move every one of its
        decks onto the other lane the day this shipped."""
        for kwargs in ({}, {"mode": None}, {"mode": ""}):
            entry, _, deck = _entry(_Router("Deck"))

            assert await _run(entry, "a deck", instance="h1", **kwargs) == "Deck did it", kwargs
            assert deck.runs[0]["task"] == "a deck", kwargs

    async def test_a_word_the_ladder_cannot_rank_leaves_the_chat_open(self) -> None:
        entry, _, deck = _entry(_Router("Deck"))

        assert await _run(entry, "a deck", instance="h1", mode="swift") == "Deck did it"
        assert deck.runs[0]["task"] == "a deck"

    async def test_the_credentials_close_the_top_tier_chat_too(self) -> None:
        """The flat condition stays flat: a lane that cannot buy a picture
        builds the same deck at every rung, on every surface."""
        entry, design, deck = _entry(_Router("Deck"), target_ready=lambda _target, _needs: False)

        assert await _run(entry, "a deck", instance="h1", mode="max") == "Design did it"

        assert deck.runs == [] and design.runs[0]["task"] == f"a deck{HOST_PREFIX}{NOTE}"

    async def test_inside_a_turn_the_turns_own_tier_answers_and_the_mode_does_not(self) -> None:
        """Both lanes carry the tier and only one of them is frozen. A dispatch
        late in a turn must answer on what the turn started under, so where
        there is a turn its snapshot is the whole answer -- a ``mode`` that
        disagrees is not a second opinion to weigh."""
        entry, design, deck = _entry(_Router("Deck"))
        with turn_tier("max"):
            assert await _run(entry, "a deck", mode="medium") == "Deck did it"
        assert deck.runs[0]["task"] == "a deck"

        entry, design, deck = _entry(_Router("Deck"))
        with turn_tier("medium"):
            assert await _run(entry, "a deck", mode="max") == "Design did it"
        assert design.runs[0]["task"] == f"a deck{HOST_PREFIX}{NOTE}"

    async def test_a_handle_the_target_already_bound_is_still_not_re_gated(self) -> None:
        """The mode gate picks a lane for new work. A chat already open on the
        target continues there, or the answer comes from an agent holding none
        of the conversation it is answering."""
        instances = _Instances([{"sessionKey": "s1", "agent": "Deck", "handle": "h1", "agentId": "acp-session-9"}])
        router = _Router("Design")
        entry, _, deck = _entry(router, instances)

        assert await _run(entry, "apply the notes", session_key="s1", instance="h1", mode="medium") == "Deck did it"
        assert router.asked == [] and deck.runs[0]["task"] == "apply the notes"


class TestTheShippedManifestsRoute:
    """The real roster lines, through the real entry."""

    @staticmethod
    def _table() -> AgentRegistry:
        """The shipped manifests through the loader that reads them.

        ``_read_route_notes`` rather than a bare ``model_validate``: the note a
        route points at is part of what ships, and a table built without the
        step that reads it would pass on a folder whose note file was never
        packaged.
        """
        rows = []
        for folder in ("raven-design", "raven-ppt"):
            path = REPO / "agents" / folder
            entry = json.loads((path / "subagent.json").read_text(encoding="utf-8"))
            read_route_notes(path, entry)
            rows.append(ThirdPartyAcpSubagentConfig.model_validate({**entry, "command": "true"}))
        registry = AgentRegistry(build_builtin=lambda row, narrowed: None)
        registry.apply(rows)
        return registry

    async def test_a_deck_request_reaches_the_deck_engine_through_the_classifier(self) -> None:
        entry = self._table().backend("Raven-Design")
        assert isinstance(entry, RoutingBackend)
        router = _Router("Raven-PPT")
        entry.set_router(router)

        for task in (
            "Build a 10-slide deck from /data/report.md",
            "Make a PPT about Shanghai's city plan",
            "Turn these notes into a presentation for Friday",
            "{{ inputs.source }}\nExport the summary as a .pptx",
        ):
            assert (await entry.pick(task, session_key="s1", instance=None))[0] == "Raven-PPT", task
        assert len(router.asked) == 4

    async def test_below_the_top_tier_the_shipped_row_keeps_the_deck_and_carries_its_own_words(self) -> None:
        """The real rows, through the real entry: the deck does not vanish below
        max, it changes which lane designs it and travels with what the manifest
        declares -- verbatim, under the host marker, and nothing else."""
        entry = self._table().backend("Raven-Design")
        entry.set_router(_Router("Raven-PPT"))

        with turn_tier("medium"):
            picked = await entry.pick("Make a PPT about Shanghai's city plan", session_key="s1", instance=None)

        assert picked.name == "Raven-Design"
        assert picked.note == f"{HOST_PREFIX}{_declared_note()}"

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


class TestWhatTheShippedRouteSaysWhenItKeepsTheDeck:
    """The note is the only channel that reaches the lane on every turn, so what
    it names has to be what the deployment really offers. Each of these reads the
    manifest against something other than itself: the agent's own tool config,
    the packaged icon data, the engine's own argument names.
    """

    @staticmethod
    def _note() -> str:
        return _declared_note()

    def test_the_route_declares_both_what_is_owed_and_what_to_say(self) -> None:
        route = _declared_route()

        assert route["to"] == "Raven-PPT"
        assert route["owes"] == ".pptx"
        assert route["owes"] in _declared_note(), "the prose has to name the file the route says is owed"

    def test_every_tool_the_note_tells_the_lane_to_use_is_one_the_lane_has(self) -> None:
        """A note naming a tool the agent's config switches off is guidance the
        model cannot follow, and it reads as the host being wrong about its own
        deployment."""
        config = json.loads((REPO / "agents" / "raven-design" / "config.json").read_text(encoding="utf-8"))
        disabled = set(config["tools"]["disabledTools"])
        note = self._note()

        for tool in ("web_search", "web_fetch", "image_generate"):
            assert f"`{tool}`" in note, tool
            assert tool not in disabled, tool

    def test_the_note_names_the_reference_argument_this_side_actually_takes(self) -> None:
        """Two lanes, two spellings: the host tool takes ``images`` and the deck
        engine's takes ``references``. A note that named the other one would be
        read by the lane that cannot use it, so the spelling is read off the
        tool this lane is offered rather than off the sentence that names it."""
        from raven.agent.tools.media_gen import ImageGenerateTool

        taken = set(ImageGenerateTool.parameters["properties"])
        note = self._note()

        assert "images" in taken and "references" not in taken
        assert "`images`" in note
        assert "references" not in note

    def test_the_icons_the_note_promises_are_in_the_packaged_set(self) -> None:
        data = json.loads(
            (REPO / "plugins-dist/ppt-engine/raven_ppt/services/assets/data/tabler_outline.json").read_text(
                encoding="utf-8"
            )
        )
        names = set(data["icons"])
        note = self._note()

        assert f"{len(names)} Tabler outline icons" in note
        for name in ("calendar_due", "warning", "building_warehouse", "map_pin"):
            assert name in names and name in note, name

    def test_the_note_says_the_four_things_the_measured_runs_showed_missing(self) -> None:
        """Two live runs made eight pages with no search and no generation on
        them, and a third generated a second bird onto a cover it had never
        looked at. Each of those is one clause here, and losing one silently is
        how the note goes back to being the sentence it started as."""
        note = self._note()

        assert "Where the material is thin, search" in note
        assert "Where a picture is missing, search for that too" in note
        assert "every section opener get a background picture" in note
        assert "Look at the page as it stands" in note
        assert "no text, no letters, no numbers" in note
        assert "Do not claim a search or a generation you did not run." in note

    def test_the_note_is_ascii(self) -> None:
        """It is appended to a task and read back out of logs and session files;
        a stray full-width character survives both and is noticed by neither."""
        assert self._note().isascii()


class TestOnlyARouteThatDeclaresTheRequirementIsGated:
    """``routes`` is a general facility, and the gate is one piece of code
    serving every row that declares one.

    A row routing for reasons of its own -- the deck lane is the only one today,
    but nothing in the config schema says so -- must not inherit the deck's
    conditions. It never declared them, it may have no pipeline that spends a
    picture credential, and the tier it is worth reaching at is its own
    business. So the declaration is what subjects a route to the gate, and a
    route that declares nothing is dispatched exactly as it was before the gate
    existed.
    """

    async def test_a_route_declaring_nothing_is_never_probed_and_never_tiered(self) -> None:
        """The regression this class exists for: an unrelated route, a probe that
        would refuse, and the cheapest tier -- and the task still reaches the
        target, with the probe never consulted at all."""
        asked: list[Any] = []

        def refuse(target: str, needs: Any) -> bool:
            asked.append((target, needs))
            return False

        for tier in ("medium", "high", "max"):
            entry, design, deck = _entry(_Router("Deck"), target_ready=refuse, needs=(), min_tier="")

            with turn_tier(tier):
                assert await _run(entry, "a deck") == "Deck did it", tier

            assert deck.runs[0]["task"] == "a deck", tier
            assert design.runs == [], tier
        assert asked == [], "a route that declared no requirement must not be asked about one"

    async def test_a_route_declaring_nothing_is_open_on_a_direct_chat_too(self) -> None:
        """The other lane into the gate. A chat carries its tier in ``mode``
        rather than in a turn, and an undeclared route is open on both."""
        entry, design, deck = _entry(_Router("Deck"), target_ready=lambda _t, _n: False, needs=(), min_tier="medium")

        assert await _run(entry, "a deck", instance="h1", mode="medium") == "Deck did it"

        assert deck.runs[0]["task"] == "a deck" and design.runs == []

    async def test_the_probe_is_asked_about_the_target_and_the_declared_requirement(self) -> None:
        """What the probe is handed is the route's own declaration, not a fact
        about this process: the target names whose credentials to read, and
        ``needs`` names which ones."""
        asked: list[Any] = []

        def probe(target: str, needs: Any) -> bool:
            asked.append((target, tuple(needs)))
            return True

        entry, _, deck = _entry(_Router("Deck"), target_ready=probe, needs=("image_search",), min_tier="")

        assert await _run(entry, "a deck") == "Deck did it"
        assert asked == [("Deck", ("image_search",))]
        assert deck.runs[0]["task"] == "a deck"

    async def test_the_two_declarations_are_independent(self) -> None:
        """Declaring one does not opt a route into the other. A route naming a
        credential is not thereby a route that is too expensive to open below
        the top rung, and a route naming a rung does not thereby have a pipeline
        whose credentials are anyone's business."""
        entry, design, _ = _entry(_Router("Deck"), target_ready=lambda _t, _n: True, needs=NEEDS, min_tier="")
        with turn_tier("medium"):
            assert await _run(entry, "a deck") == "Deck did it", "needs alone must not close a cheap tier"

        asked: list[Any] = []
        entry, design, deck = _entry(
            _Router("Deck"),
            target_ready=lambda t, n: asked.append((t, n)) or False,
            needs=(),
            min_tier="max",
        )
        with turn_tier("max"):
            assert await _run(entry, "a deck") == "Deck did it", "min_tier alone must not consult the probe"
        assert asked == []

    async def test_a_declared_tier_is_a_floor_and_not_the_top_rung(self) -> None:
        """The rung is read off the declaration, so a route may open at a middle
        one. Pinned because the rule it replaced was ``== TIER_LADDER[-1]``, and
        a floor that still meant "max" would pass every test about the deck."""
        for tier, expected in (("medium", "Design did it"), ("high", "Deck did it"), ("max", "Deck did it")):
            entry, _, _ = _entry(_Router("Deck"), needs=(), min_tier="high")
            with turn_tier(tier):
                assert await _run(entry, "a deck") == expected, tier
