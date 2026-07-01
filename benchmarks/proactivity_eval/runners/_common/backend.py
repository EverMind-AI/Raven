"""AgentBackend protocol + shared outcome type.

Each supported system (Raven Planner / Raven Agent / Raven Sentinel /
Hermes / OpenClaw) ships one backend implementation. The BenchmarkDriver
passes each sample through ``backend.run_one()``; the backend decides how to
turn the sample into an actual LLM call (prompt-based, structured context,
subprocess, CLI, …) and returns a uniform AgentOutcome.

Two backend flavors live here:

- **prompt-based** (e.g. OpenClaw): calls ``driver.build_prompt(sample, ctx)``,
  sends the string to its system, returns the raw text. The driver is
  responsible for parsing the text into a structured decision.

- **structured** (e.g. Raven Planner, Sentinel): asks the driver for a
  structured view (``driver.to_planner_context(sample)``) and returns a
  pre-parsed decision inside ``AgentOutcome.decision``. Not every benchmark
  supports structured mode; backends check via ``getattr`` and fail loudly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .driver import BenchmarkDriver


@dataclass
class AgentOutcome:
    """Uniform result type returned by every backend."""

    status: str  # "ok" | "skip" | "timeout" | "exception" | "subprocess_error" | "empty"
    elapsed_s: float
    text: str | None = None  # raw response text (None for pure-structured backends)
    error: str | None = None
    decision: dict[str, Any] | None = None  # pre-parsed: {should_help, proposed_task, reason, parse_ok, ...}
    meta: dict[str, Any] = field(default_factory=dict)  # backend-specific extras


@dataclass
class Sample:
    """Benchmark-neutral wrapper around one record.

    ``raw`` is the original data (case YAML dict / pbench JSONL record / …).
    ``session_hint`` is a stable identifier backends should use when they
    need a session key (OpenClaw --session-id, Raven session_key, …).
    ``meta`` carries benchmark-specific fields backends may consult.
    """

    raw: dict[str, Any]
    session_hint: str
    meta: dict[str, Any] = field(default_factory=dict)


class AgentBackend(ABC):
    """Protocol every system must implement.

    ``name`` must match the ``agents/<name>/<name>.yaml`` directory.
    """

    name: str = "abstract"

    @abstractmethod
    async def run_one(
        self,
        sample: Sample,
        driver: "BenchmarkDriver",
        *,
        session_id: str,
        ctx: dict[str, Any] | None = None,
    ) -> AgentOutcome: ...

    def close(self) -> None:
        """Optional cleanup hook (close connections, etc.). Default: no-op."""
        pass


__all__ = ["AgentBackend", "AgentOutcome", "Sample"]
