"""The history strategy registry -- every scheme that can fill the history slot.

``context.history.strategy`` names one of these. This module is the only place
that knows which strategies exist and the only place that imports one, so the
boundary is mechanical rather than a convention: the assembler and the Memory
role read the capabilities a :class:`HistoryStrategy` declares and never the
strategy's name.

Every ``build`` imports its strategy inside the function body. That is not
tidiness: constructing the Curator's builder creates its archive directories on
disk, and the compacting strategy keeps its checkpoints there too, so "not
selected" has to mean "never imported", not "imported and unused".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from raven.contracts.context import SegmentBuilder


@dataclass(frozen=True)
class HistoryStrategy:
    """One registered strategy: how to build its builder, and what the host must know.

    The flags are declared here rather than read off a built builder, because
    the host acts on them before any turn runs -- whether to hand over the whole
    log and keep the lossy consolidator out of the loop, and whether the turn
    needs the tool that reads a ref back.
    """

    name: str
    build: Callable[..., "SegmentBuilder"]
    owns_compaction: bool
    mints_source_refs: bool = False


_strategies: dict[str, HistoryStrategy] = {}


def register(strategy: HistoryStrategy) -> HistoryStrategy:
    """Add ``strategy`` under its name, replacing any prior registration of it."""
    _strategies[strategy.name] = strategy
    return strategy


def registered() -> list[str]:
    """Every accepted value of ``context.history.strategy``, in registration order."""
    return list(_strategies)


def resolve(name: str | None) -> HistoryStrategy:
    """The strategy ``name`` names.

    Raises rather than falling back, matching ``extra='forbid'`` on the config
    models: a misspelled strategy that silently ran a different one would be
    found by its cost, months later.
    """
    found = _strategies.get((name or "").strip().lower())
    if found is None:
        raise ValueError(f"unknown context.history.strategy {name!r}; expected one of {registered()}")
    return found


def _build_curator(**kwargs: Any) -> "SegmentBuilder":
    from raven.context_engine.segments.curator import CuratorSegmentBuilder

    return CuratorSegmentBuilder(
        pin_resolver=kwargs["pin_resolver"],
        workspace=kwargs["workspace"],
        config=kwargs["config"],
        provider=kwargs["provider"],
        model=kwargs["model"],
        context_window_tokens=kwargs["context_window_tokens"],
        get_tool_definitions=kwargs["get_tool_definitions"],
        now_fn=kwargs.get("now_fn"),
    )


def _build_compacting(**kwargs: Any) -> "SegmentBuilder":
    from raven.context_engine.history.compacting import CompactingHistoryBuilder

    return CompactingHistoryBuilder(
        workspace=kwargs["workspace"],
        config=kwargs["config"],
        provider=kwargs["provider"],
        model=kwargs["model"],
        context_window_tokens=kwargs["context_window_tokens"],
        get_tool_definitions=kwargs["get_tool_definitions"],
        now_fn=kwargs.get("now_fn"),
    )


# It owns compaction in the strong sense -- it takes the full log because it
# budgets history itself: the loop must keep MemoryConsolidator out
# of the way, since a lossy pass that advances ``last_consolidated`` would remove
# the very messages this strategy is deciding about.
register(
    HistoryStrategy(
        "compacting",
        build=_build_compacting,
        owns_compaction=True,
        mints_source_refs=True,
    )
)

# The earlier strategy, unchanged and kept selectable.
register(
    HistoryStrategy(
        "curator",
        build=_build_curator,
        owns_compaction=True,
    )
)


__all__ = ["HistoryStrategy", "register", "registered", "resolve"]
