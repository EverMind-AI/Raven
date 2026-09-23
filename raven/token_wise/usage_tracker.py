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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from raven.contracts.token_strategy import TokenStrategy, UsageSnapshot
from raven.token_wise import turn_spend


def _default_telemetry_dir() -> Path:
    # Through raven_home() rather than a literal ~/.raven: RAVEN_HOME moves the
    # whole installation, and telemetry written outside it is telemetry the
    # matching reader (settings.usage) will never find.
    from raven.config.loader import raven_home

    return raven_home() / "telemetry"


def read_usage_rows(path: Path) -> list[dict[str, Any]]:
    """Parsed rows of one telemetry file.

    A line that does not parse is skipped rather than raised on: the recorder
    flushes per call, so a process killed mid-write leaves a partial line, and
    that is not a reason to fail a report about the calls that did land.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def row_root(row: dict[str, Any]) -> Any:
    """The session a row is billed to: its delegation root, else its own."""
    return row.get("root_session_key") or row.get("session_key")


def delegated_usage(
    root: str,
    since: datetime,
    *,
    until: datetime | None = None,
    telemetry_dir: Path | None = None,
) -> UsageSnapshot:
    """What sub-agents in other processes spent for ``root`` in ``[since, until)``.

    A product sub-agent runs as its own ACP process with its own tracker, so
    none of its calls reach the tracker in the process that delegated to it; the
    one account of them is the rows that process writes, named for the root it
    was handed. Those are the rows here: billed to ``root`` but recorded under
    another session. The root's own calls are excluded on purpose -- the caller
    already has them from its own tracker, and counting the file too would count
    them twice.

    Cost follows ``session.usage``: only a provider-reported price (schema
    version 2) counts, and anything else is a call with unknown cost.
    """
    from raven.providers.usage import reported_cost, token_count

    since = since.astimezone(timezone.utc)
    until = (until or datetime.now(timezone.utc)).astimezone(timezone.utc)
    directory = telemetry_dir or _default_telemetry_dir()
    acc = UsageSnapshot(model="__delegated__", session_key=root)
    # Files are named by the writer's local date and rows are stamped in UTC, so
    # a day either side of the window is read and the stamp alone decides.
    day = since.date() - timedelta(days=1)
    while day <= until.date() + timedelta(days=1):
        for row in read_usage_rows(directory / f"usage-{day.isoformat()}.jsonl"):
            if row.get("_type") == "tool_call" or row_root(row) != root:
                continue
            if not row.get("session_key") or row.get("session_key") == root:
                continue
            try:
                ts = datetime.fromisoformat(str(row.get("ts")))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if not since <= ts < until:
                continue
            UsageTracker._add_into(
                acc,
                UsageSnapshot(
                    model=str(row.get("model") or ""),
                    input_tokens=token_count(row.get("input_tokens")),
                    output_tokens=token_count(row.get("output_tokens")),
                    cache_read_tokens=token_count(row.get("cache_read_tokens")),
                    cache_write_tokens=token_count(row.get("cache_write_tokens")),
                    reasoning_tokens=token_count(row.get("reasoning_tokens")) or 0,
                    cost_usd=reported_cost(row.get("cost_usd")) if row.get("schema_version") == 2 else None,
                ),
            )
        day += timedelta(days=1)
    return acc


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
        from raven.token_wise import usage_context

        session = usage.session_key or usage_context.session_key()
        usage = replace(
            usage,
            session_key=session,
            root_session_key=usage.root_session_key or usage_context.root_session_key(session),
        )
        self._call_count += 1
        self._accumulate(usage)

        if self.persist:
            # Why the call ended, beside what it spent: a row of counts cannot
            # say whether the reply was finished or cut at the output ceiling.
            finish_reason = response.get("finish_reason") if isinstance(response, dict) else None
            self._buffer.append(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "schema_version": 2,
                    "_telemetry_dir": usage_context.telemetry_dir() or str(self.telemetry_dir),
                    **{k: v for k, v in asdict(usage).items() if k != "calls" and not k.endswith("_missing_calls")},
                    "finish_reason": finish_reason,
                }
            )
            if self._call_count % self.flush_every == 0:
                self._flush()

    async def record_tool_call(self, name: str, tool_call_id: str | None = None) -> None:
        """Persist one tool call with the active usage ownership."""
        from raven.token_wise import usage_context

        session = usage_context.session_key()
        self._buffer.append(
            {
                "_type": "tool_call",
                "ts": datetime.now(timezone.utc).isoformat(),
                "schema_version": 2,
                "_telemetry_dir": usage_context.telemetry_dir() or str(self.telemetry_dir),
                "session_key": session,
                "root_session_key": usage_context.root_session_key(session),
                "name": name,
                "tool_call_id": tool_call_id,
            }
        )
        if len(self._buffer) % self.flush_every == 0:
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
        if u.root_session_key and u.root_session_key != key:
            # A delegated call: spent in a session of its own, under a turn
            # somewhere else that is still running and still has to report what
            # it cost. This is the one hook that sees both.
            turn_spend.note_delegated(u.root_session_key, u.cost_usd)
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
        pending = self._buffer
        self._buffer = []
        groups: dict[Path, list[dict[str, Any]]] = {}
        for row in pending:
            groups.setdefault(Path(row["_telemetry_dir"]), []).append(row)
        for destination, rows in groups.items():
            try:
                destination.mkdir(parents=True, exist_ok=True)
                path = destination / f"usage-{date.today().isoformat()}.jsonl"
                with path.open("a", encoding="utf-8") as f:
                    for row in rows:
                        f.write(
                            json.dumps({k: v for k, v in row.items() if k != "_telemetry_dir"}, ensure_ascii=False)
                            + "\n"
                        )
            except Exception as e:
                logger.warning("UsageTracker flush failed for {} ({}); dropping {} rows", destination, e, len(rows))
