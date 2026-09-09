"""UsageTracker — records token usage and cost for every LLM call.

Accumulates into three tiers:
    - ``per_session[session_key]`` — cumulative usage within one session
    - ``per_day[date]``             — daily roll-up, useful for budgeting
    - ``total``                      — lifetime of this process

Every call is also appended to ``{telemetry_dir}/usage-YYYY-MM-DD.jsonl``
as a single JSON object per line, enabling post-hoc analysis with ``jq``.

The tracker is purely a recorder — it never modifies the outgoing request,
so its ``before_llm_call`` inherits the default no-op pass-through.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from raven.contracts.token_strategy import TokenStrategy, UsageSnapshot


def _default_telemetry_dir() -> Path:
    # Through raven_home() rather than a literal ~/.raven: RAVEN_HOME moves the
    # whole installation, and telemetry written outside it is telemetry the
    # matching reader (settings.usage) will never find.
    from raven.config.loader import raven_home

    return raven_home() / "telemetry"


class UsageTracker(TokenStrategy):
    """Observes every LLM call; persists & rolls up token and cost stats."""

    name = "usage_tracker"

    def __init__(
        self,
        telemetry_dir: Path | None = None,
        flush_every: int = 1,
        persist: bool = True,
    ):
        """Create a tracker.

        Args:
            telemetry_dir: Where to write ``usage-YYYY-MM-DD.jsonl``. Defaults
                to ``<raven home>/telemetry`` (``raven_home()``, so RAVEN_HOME
                moves it).
            flush_every: Buffer N calls before writing to disk. 1 = write every
                call (safest, default). Larger values amortize IO.
            persist: If False, accumulate in memory only (useful for tests).
        """
        self.telemetry_dir = telemetry_dir or _default_telemetry_dir()
        self.flush_every = max(1, flush_every)
        self.persist = persist

        self.per_session: dict[str, UsageSnapshot] = {}
        self.per_day: dict[date, UsageSnapshot] = {}
        self.total: UsageSnapshot = UsageSnapshot(model="__total__")

        self._call_count: int = 0
        self._buffer: list[dict[str, Any]] = []

    # ---- TokenStrategy hook ----

    async def after_llm_call(self, response: dict[str, Any], usage: UsageSnapshot) -> None:
        """Record one LLM call."""
        self._call_count += 1
        self._accumulate(usage)

        if self.persist:
            self._buffer.append(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "schema_version": 2,
                    **{k: v for k, v in asdict(usage).items() if k != "calls" and not k.endswith("_missing_calls")},
                }
            )
            if self._call_count % self.flush_every == 0:
                self._flush()

    # ---- Public introspection ----

    def snapshot(self, session_key: str | None = None) -> UsageSnapshot:
        """Return a *copy* of the session accumulator, or the lifetime total."""
        if session_key is not None:
            src = self.per_session.get(session_key) or UsageSnapshot(model="__empty__", session_key=session_key)
        else:
            src = self.total
        return self._copy(src)

    def close(self) -> None:
        """Flush any remaining buffered rows to disk."""
        self._flush()

    # ---- Internals ----

    def _accumulate(self, u: UsageSnapshot) -> None:
        key = u.session_key or "__no_session__"
        session_acc = self.per_session.get(key)
        if session_acc is None:
            session_acc = UsageSnapshot(model=u.model, session_key=key)
            self.per_session[key] = session_acc
        self._add_into(session_acc, u)

        today = date.today()
        day_acc = self.per_day.get(today)
        if day_acc is None:
            day_acc = UsageSnapshot(model="__day__")
            self.per_day[today] = day_acc
        self._add_into(day_acc, u)

        self._add_into(self.total, u)

    @staticmethod
    def _add_into(acc: UsageSnapshot, add: UsageSnapshot) -> None:
        acc.calls += 1
        acc.reasoning_tokens += add.reasoning_tokens
        for field, missing in (
            ("input_tokens", "input_missing_calls"),
            ("output_tokens", "output_missing_calls"),
            ("cost_usd", "cost_missing_calls"),
            ("cache_read_tokens", "cache_read_missing_calls"),
            ("cache_write_tokens", "cache_write_missing_calls"),
        ):
            value = getattr(add, field)
            if value is None:
                setattr(acc, missing, getattr(acc, missing) + 1)
            else:
                setattr(acc, field, (getattr(acc, field) or 0) + value)

    @staticmethod
    def _copy(src: UsageSnapshot) -> UsageSnapshot:
        return replace(src)

    def _flush(self) -> None:
        if not self._buffer or not self.persist:
            self._buffer.clear()
            return
        try:
            self.telemetry_dir.mkdir(parents=True, exist_ok=True)
            path = self.telemetry_dir / f"usage-{date.today().isoformat()}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                for row in self._buffer:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._buffer.clear()
        except Exception as e:
            logger.warning("UsageTracker flush failed ({}); dropping {} rows", e, len(self._buffer))
            self._buffer.clear()
