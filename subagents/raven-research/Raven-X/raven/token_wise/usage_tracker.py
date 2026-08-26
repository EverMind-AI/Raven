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
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from raven.token_wise.base import TokenStrategy, UsageSnapshot


def _default_telemetry_dir() -> Path:
    return Path.home() / ".raven" / "telemetry"


def _serving_upstream(response: Any) -> dict[str, Any]:
    """Which upstream actually served this call, when the wire says so.

    A gateway model id names the gateway, not the machine that ran the weights:
    ``deepseek/deepseek-v4-flash`` had 17+ OpenRouter upstreams serving it at
    once on 2026-08-17. **Each upstream holds its own prompt cache**, so an
    ``auto`` policy that switches mid-conversation presents as a cache_read
    collapse with no local cause - and until this field existed there was no way
    to tell that apart from our own trimming, an idle timeout, or a genuinely
    new prefix. Measured on the dsv4f batch before this landed: collapses
    (previous call read >20k from cache, this one read 0) ran **6.44% on the
    anchor and 12.01% on the treated arm**, and nothing on disk could say who
    caused them.

    This is the same law the repo already applies one level up - "which model
    served this hop must be a first-class artifact field, not something
    reconstructed afterwards" - applied to the hop's *upstream* rather than its
    model. Reconstruction is strictly impossible here: the id is identical for
    every upstream, so no amount of post-hoc analysis recovers it.

    Recorded, never acted on: pinning is ``providers.<name>.routing``'s job and
    stays an explicit config decision. Absent keys rather than nulls when the
    wire does not report it, so a non-gateway provider's rows keep their old
    shape byte for byte and "we never asked" stays distinguishable from
    "it answered nothing".
    """
    if response is None:
        return {}
    get = response.get if isinstance(response, dict) else lambda k, d=None: getattr(response, k, d)
    out: dict[str, Any] = {}
    try:
        upstream = get("provider", None)
        if isinstance(upstream, str) and upstream:
            out["serving_upstream"] = upstream
        call_id = get("id", None)
        if isinstance(call_id, str) and call_id:
            out["upstream_call_id"] = call_id
        # Which source in ``_parse_response`` answered. Kept because a bare absent
        # ``serving_upstream`` cannot say whether the wire was silent or whether the
        # extraction looked in the wrong place - and the first version of this
        # recorder produced 0 rows out of 67,693 for the SECOND reason while reading
        # exactly like the first.
        src = get("upstream_source", None)
        if isinstance(src, str) and src:
            out["upstream_source"] = src
    except Exception:  # pragma: no cover - a recorder must never kill a call
        return {}
    return out


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
                to ``~/.raven/telemetry``.
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
                    **asdict(usage),
                    **_serving_upstream(response),
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
        acc.input_tokens += add.input_tokens
        acc.output_tokens += add.output_tokens
        acc.cache_read_tokens += add.cache_read_tokens
        acc.cache_write_tokens += add.cache_write_tokens
        acc.reasoning_tokens += add.reasoning_tokens
        acc.estimated_cost_usd += add.estimated_cost_usd

    @staticmethod
    def _copy(src: UsageSnapshot) -> UsageSnapshot:
        return UsageSnapshot(
            model=src.model,
            input_tokens=src.input_tokens,
            output_tokens=src.output_tokens,
            cache_read_tokens=src.cache_read_tokens,
            cache_write_tokens=src.cache_write_tokens,
            reasoning_tokens=src.reasoning_tokens,
            estimated_cost_usd=src.estimated_cost_usd,
            session_key=src.session_key,
        )

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
