"""BenchmarkDriver protocol.

A driver encapsulates everything benchmark-specific:
- loading samples from the dataset
- building the prompt string (for prompt-based backends)
- optionally exposing structured views (to_planner_context, to_hermes_context)
  that structured backends consume
- parsing the free-text agent reply into a decision dict (when the backend
  returns raw text rather than a pre-parsed decision)
- shaping the final output row
- summarizing a completed run

Drivers never touch LLM endpoints directly — the backend owns that.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backend import AgentOutcome, Sample


class BenchmarkDriver(ABC):
    """Protocol every benchmark must implement.

    ``name`` must match ``benchmarks/<name>/<name>.yaml`` directory.
    """

    name: str = "abstract"

    # ---- required ------------------------------------------------------------

    @abstractmethod
    def load_samples(self, *, n: int | None = None, filter_id: str | None = None) -> list["Sample"]: ...

    @abstractmethod
    def build_prompt(self, sample: "Sample", ctx: dict[str, Any] | None = None) -> str: ...

    @abstractmethod
    def parse_output(self, text: str | None, sample: "Sample") -> dict[str, Any]:
        """Parse a backend's free-text reply into the row-level decision dict."""

    @abstractmethod
    def make_row(self, sample: "Sample", outcome: "AgentOutcome", runtime_meta: dict[str, Any]) -> dict[str, Any]:
        """Shape the final result row written to the output JSON."""

    # ---- optional ------------------------------------------------------------

    def summarize(self, rows: list[dict[str, Any]]) -> str:
        """Return a text summary (printed to stderr at end of run)."""
        return f"{len(rows)} records"

    def dataset_description(self) -> str:
        """Return a short string describing what this benchmark tests."""
        return self.name

    # ---- structured hooks (opt-in) -------------------------------------------
    # Structured backends (Planner, Sentinel) call getattr(driver, hook) and
    # fail loudly if the driver doesn't implement it. Prompt-based backends
    # never touch these, so drivers can implement them lazily.

    # def to_planner_context(self, sample) -> PlannerContext: ...
    # def to_hermes_cron(self, sample) -> dict | None: ...


__all__ = ["BenchmarkDriver"]
