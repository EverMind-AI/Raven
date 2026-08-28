"""Live-run questions about a graph (raven/agent/subagent/dag_live.py).

Both answers are advisory -- they decide whether a row draws as running and
whether a stop button reports anything -- and both are invoked lazily, only for a
row that still reads running. So the interesting cases are the degradations: a
loop that cannot answer, and a loop that answers by raising. Asserted here rather
than through the three RPC consumers, which each reach only one of the branches.
"""

from __future__ import annotations

from typing import Any

from raven.agent.subagent.dag_live import cancel_run, live_run_ids


class _Tool:
    def __init__(self, live: set[str] | None = None, owns: set[str] | None = None) -> None:
        self._live = live or set()
        self._owns = owns or set()

    def active_run_ids(self) -> set[str]:
        return self._live

    def request_cancel(self, run_id: str) -> bool:
        return run_id in self._owns


class _Registry:
    def __init__(self, tool: Any) -> None:
        self._tool = tool

    def get(self, name: str) -> Any:
        return self._tool if name == "run_subagent_dag" else None


class _Loop:
    """A duck-typed host: a tool registry and nothing else."""

    def __init__(self, tool: Any) -> None:
        self.tools = _Registry(tool)


class _RealLoop(_Loop):
    """What production passes: the accessor that spans both graph tools."""

    def __init__(self, tool: Any, across: set[str]) -> None:
        super().__init__(tool)
        self._across = across

    def active_dag_run_ids(self) -> set[str]:
        return self._across

    def cancel_dag_run(self, run_id: str) -> bool:
        return run_id in self._across


def test_the_loop_accessor_wins_over_the_registered_tool() -> None:
    """The accessor spans both instances; the registry knows only one.

    Preferring the registry is what reported a playbook's run as finished while
    it was still running, so the order here is the fix.
    """
    loop = _RealLoop(_Tool(live={"from-the-model"}), across={"from-the-model", "from-a-playbook"})

    assert live_run_ids(loop) == {"from-the-model", "from-a-playbook"}
    assert cancel_run(loop, "from-a-playbook") is True


def test_a_host_with_only_a_registry_still_gets_the_registered_half() -> None:
    """A floor, not the intended path: a test double or an older host answers
    for the registered instance rather than not at all."""
    loop = _Loop(_Tool(live={"r1"}, owns={"r1"}))

    assert live_run_ids(loop) == {"r1"}
    assert cancel_run(loop, "r1") is True
    assert cancel_run(loop, "r2") is False


def test_nothing_to_ask_is_nothing_live_rather_than_an_error() -> None:
    """No loop, no registry, no tool -- each reads as "nothing is running"."""

    class _Bare:
        pass

    for host in (None, _Bare(), _Loop(None)):
        assert live_run_ids(host) == set()
        assert cancel_run(host, "r1") is False


def test_a_host_that_answers_by_raising_degrades() -> None:
    """These run inside a list render. An exception would replace the panel's
    list with an error, which is a worse answer than "not live"."""

    class _Exploding:
        def active_dag_run_ids(self) -> set[str]:
            raise RuntimeError("gone")

        def cancel_dag_run(self, run_id: str) -> bool:
            raise RuntimeError("gone")

    assert live_run_ids(_Exploding()) == set()
    assert cancel_run(_Exploding(), "r1") is False
