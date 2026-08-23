"""Tools the operator switched off: withheld per assembly, not unregistered.

The behaviour these exist for is one sentence -- flip a switch, and the next
request reflects it, in both directions. It used to be neither: the off switches
were read once into the constructor and applied by *unregistering* the tool, so
turning one off needed a restart and turning one back on was not merely
unimplemented but unimplementable, because nothing remembered what to put back.

So what is pinned here is that the registry keeps the tool and leaves it out of
the array, that it asks for the current answer rather than a remembered one, and
that both surfaces a tool can be reached through agree -- a tool hidden from the
schema but findable through tool-search would be worse than one that was never
hidden, because the model would call something that is not supposed to be there.
"""

from __future__ import annotations

from typing import Any

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry


class _Stub(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "stub"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ran"


def _registry(*names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in names:
        reg.register(_Stub(name))
    return reg


def _offered(reg: ToolRegistry) -> set[str]:
    return {d["function"]["name"] for d in reg.get_definitions()}


class TestTheSwitchTakesEffectOnTheNextAssembly:
    def test_a_withheld_tool_is_left_out_of_the_array(self) -> None:
        reg = _registry("exec", "grep")
        reg.set_withheld_source(lambda: frozenset({"exec"}))

        assert _offered(reg) == {"grep"}

    def test_and_is_still_registered(self) -> None:
        """The whole reason this is a filter. Unregistering expressed the
        preference by destroying its subject."""
        reg = _registry("exec", "grep")
        reg.set_withheld_source(lambda: frozenset({"exec"}))

        assert reg.has("exec")
        assert reg.get("exec") is not None

    def test_the_source_is_asked_again_every_time(self) -> None:
        """Read now, not read once -- which is the difference between a switch
        that works and a switch that needs a restart."""
        switches = {"exec"}
        reg = _registry("exec", "grep")
        reg.set_withheld_source(lambda: frozenset(switches))
        assert _offered(reg) == {"grep"}

        switches.clear()

        assert _offered(reg) == {"exec", "grep"}

    def test_turning_one_off_and_back_on_needs_nothing_else(self) -> None:
        switches: set[str] = set()
        reg = _registry("exec", "grep")
        reg.set_withheld_source(lambda: frozenset(switches))

        seen = [_offered(reg)]
        switches.add("grep")
        seen.append(_offered(reg))
        switches.clear()
        seen.append(_offered(reg))

        assert seen == [{"exec", "grep"}, {"exec"}, {"exec", "grep"}]

    def test_the_source_is_asked_once_per_assembly_not_once_per_tool(self) -> None:
        """It is a file read behind a stat in production, so the cost has to scale
        with assemblies rather than with the size of the tool table."""
        calls = 0

        def source() -> frozenset[str]:
            nonlocal calls
            calls += 1
            return frozenset({"exec"})

        reg = _registry("exec", "grep", "find", "read_file", "write_file")
        reg.set_withheld_source(source)
        reg.get_definitions()

        assert calls == 1


class TestBothSurfacesAgree:
    """A tool hidden from one surface and reachable through the other is worse
    than one that was never hidden: the model would reach a tool the operator
    switched off, through a door nobody thought to close. Folding the switch into
    `offers` -- the predicate both surfaces already shared for the channel
    restriction -- is what makes that impossible rather than merely unlikely."""

    def _controller(self, withheld: set[str]):
        from raven.agent.tools.tool_search import ToolSearchController

        reg = _registry("exec", "grep")
        reg.set_withheld_source(lambda: frozenset(withheld))
        ctrl = ToolSearchController(reg, always_visible=set())
        ctrl.refresh()
        return reg, ctrl

    def test_tool_search_does_not_list_what_the_schema_withheld(self) -> None:
        reg, ctrl = self._controller({"exec"})

        assert "exec" not in _offered(reg)
        assert [h["name"] for h in ctrl.search("exec")] == []

    def test_and_does_list_it_when_the_switch_is_off(self) -> None:
        """The assertion above was passing on an empty index rather than on a
        withheld tool: ``ToolSearchController`` starts with nothing indexed, so
        ``search`` answered ``[]`` for every query and the check could not fail.
        This is the pairing case that makes it mean something."""
        _reg, ctrl = self._controller(set())

        assert [h["name"] for h in ctrl.search("exec")] == ["exec"]

    def test_tool_call_refuses_to_reach_it(self) -> None:
        """The one that matters most: search only lists, `tool_call` invokes."""
        _reg, ctrl = self._controller({"exec"})

        target = ctrl.resolve_target("exec")

        assert target.tool is None
        assert "exec" in str(target.refusal)

    def test_and_reaches_it_again_once_the_switch_is_off(self) -> None:
        """The pairing case, so neither assertion above can pass by refusing
        everything."""
        switches = {"exec"}
        reg = _registry("exec", "grep")
        reg.set_withheld_source(lambda: frozenset(switches))
        from raven.agent.tools.tool_search import ToolSearchController

        ctrl = ToolSearchController(reg, always_visible=set())
        assert ctrl.resolve_target("exec").tool is None

        switches.clear()

        assert ctrl.resolve_target("exec").tool is not None


class TestTheDoorExecuteOpens:
    """`offers` covers the two surfaces a tool is *found* through. `execute` is a
    third door and it does not consult either: a withheld tool is still in
    `_tools` by construction, which is what makes the switch reversible, so
    without a check here the switch is an omission from the array rather than a
    block. Two ways a name arrives without being in the array -- a model calling
    from habit (an eval harness leaving only `execute` still gets asked for
    `read_file`), and a switch flipped mid-conversation, where the array is fresh
    and the history is not."""

    def _run(self, reg: ToolRegistry, name: str) -> str:
        import asyncio

        return asyncio.run(reg.execute(name, {}))

    def test_a_withheld_tool_does_not_run(self) -> None:
        reg = _registry("exec", "grep")
        reg.set_withheld_source(lambda: frozenset({"exec"}))

        assert self._run(reg, "exec") != "ran"

    def test_and_is_refused_as_absent_rather_than_failing_oddly(self) -> None:
        """The same answer unregistering used to give, so a model that asks for a
        switched-off tool is told the one true thing about it."""
        reg = _registry("exec")
        reg.set_withheld_source(lambda: frozenset({"exec"}))

        from raven.agent.tools.registry import absent_tool_error

        assert self._run(reg, "exec") == absent_tool_error("exec")

    def test_a_tool_that_is_not_withheld_still_runs(self) -> None:
        """The pairing case, so the assertions above cannot pass by refusing
        everything."""
        reg = _registry("exec", "grep")
        reg.set_withheld_source(lambda: frozenset({"exec"}))

        assert self._run(reg, "grep") == "ran"

    def test_the_switch_is_read_now_not_at_registration(self) -> None:
        switches = {"exec"}
        reg = _registry("exec")
        reg.set_withheld_source(lambda: frozenset(switches))
        assert self._run(reg, "exec") != "ran"

        switches.clear()

        assert self._run(reg, "exec") == "ran"


class TestAskingByNameRatherThanByRegistration:
    """`get(name) is not None` meant "may the model use this" everywhere it was
    written, and that was true while the off switch unregistered. It is not true
    now, and the difference escapes the registry wherever the answer becomes
    prompt text -- the tool-failure nudge tells the model to call `find_skill`,
    and `execute` then refuses it as absent. One wasted call on a turn that is
    already failing, but it is model-visible text advertising a tool the operator
    switched off, which is the invariant the switch exists for."""

    def test_a_withheld_name_is_not_offered(self) -> None:
        reg = _registry("find_skill")
        reg.set_withheld_source(lambda: frozenset({"find_skill"}))

        assert reg.offers_by_name("find_skill") is False

    def test_but_is_still_registered(self) -> None:
        """The pair that makes the distinction necessary rather than cosmetic."""
        reg = _registry("find_skill")
        reg.set_withheld_source(lambda: frozenset({"find_skill"}))

        assert reg.get("find_skill") is not None

    def test_an_offered_name_is_offered(self) -> None:
        reg = _registry("find_skill")
        reg.set_withheld_source(lambda: frozenset())

        assert reg.offers_by_name("find_skill") is True

    def test_an_absent_name_is_not_offered(self) -> None:
        """Answers the caller's question in one call, so a caller does not have to
        keep the `is not None` check it was replacing."""
        reg = _registry("grep")

        assert reg.offers_by_name("find_skill") is False

    def test_the_channel_half_counts_too(self) -> None:
        reg = ToolRegistry()
        reg.register(_Stub("find_skill"))
        reg._tools["find_skill"].channels = ["cli"]  # type: ignore[attr-defined]
        reg.set_channel("web")

        assert reg.offers_by_name("find_skill") is False


class TestOneAnswerPerScan:
    """A search walks the whole ranked catalog, so asking the source per candidate
    both pays the read N times and lets one list disagree with itself -- a tool
    offered and its neighbour withheld because the file moved between them. The
    schema assembly already asked once; the search surface now does too."""

    def _flipping_source(self, calls: list[int]):
        """Withholds nothing on odd calls and everything on even ones, so a
        per-candidate reader produces a mixed list and a per-scan reader cannot."""

        def source() -> frozenset[str]:
            calls.append(1)
            return frozenset() if len(calls) % 2 else frozenset({"exec", "grep"})

        return source

    def _controller(self, reg: ToolRegistry):
        """``refresh()`` is not optional here: the index starts empty, so a
        controller that skips it makes ``search`` return ``[]`` for everything and
        every assertion below pass without exercising a thing."""
        from raven.agent.tools.tool_search import ToolSearchController

        ctrl = ToolSearchController(reg, always_visible=set())
        ctrl.refresh()
        return ctrl

    def test_a_scan_cannot_disagree_with_itself(self) -> None:
        calls: list[int] = []
        reg = _registry("exec", "grep")
        reg.set_withheld_source(self._flipping_source(calls))
        ctrl = self._controller(reg)

        names = [h["name"] for h in ctrl.search("exec grep")]

        assert names in ([], ["exec", "grep"]), f"mixed list from one scan: {names}"

    def test_the_source_is_asked_once_for_the_whole_scan(self) -> None:
        calls: list[int] = []
        reg = _registry("exec", "grep")
        reg.set_withheld_source(self._flipping_source(calls))
        ctrl = self._controller(reg)

        before = len(calls)
        ctrl.search("exec grep")

        assert len(calls) - before == 1, f"asked {len(calls) - before} times"


class TestNothingInstalledIsNotEverythingWithheld:
    def test_no_source_offers_everything(self) -> None:
        """The sub-agent and curator registries install none, and a registry that
        withheld everything by default would offer them nothing at all."""
        reg = _registry("exec", "grep")

        assert _offered(reg) == {"exec", "grep"}

    def test_a_source_that_raises_offers_everything(self) -> None:
        """Degrading to "no preferences" rather than to "no tools": a config that
        cannot be read for a moment must not cost the turn its whole toolset."""
        reg = _registry("exec", "grep")

        def boom() -> frozenset[str]:
            raise OSError("disk gone")

        reg.set_withheld_source(boom)

        assert _offered(reg) == {"exec", "grep"}

    def test_a_source_can_be_uninstalled(self) -> None:
        reg = _registry("exec", "grep")
        reg.set_withheld_source(lambda: frozenset({"exec"}))
        assert _offered(reg) == {"grep"}

        reg.set_withheld_source(None)

        assert _offered(reg) == {"exec", "grep"}


class TestTheChannelHalfStillHolds:
    def test_a_channel_bound_tool_is_still_withheld_off_its_channel(self) -> None:
        """`offers` is the conjunction; neither half may be lost in adding the
        other."""

        class _WebOnly(_Stub):
            @property
            def channels(self) -> list[str]:
                return ["web"]

        reg = ToolRegistry()
        reg.register(_WebOnly("browse"))
        reg.register(_Stub("exec"))
        reg.set_withheld_source(lambda: frozenset())
        reg.set_channel("telegram")

        assert _offered(reg) == {"exec"}
