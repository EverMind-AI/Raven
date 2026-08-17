"""A user-configurable contract for when the on-call loop may wake a person.

"Do not bother me unless it matters" is the hardest thing on the metric list to
grade, because how much a given interruption was worth is a judgement. Making it
a contract the user writes down turns it into an invariant instead: the threshold
is a number, exceeding it is a bug, and the whole metric moves from an eval set
to a test case.

The guard both enforces and records, and the two serve different readers:

  - **enforcement is the product promise.** A denied ask never reaches the
    person, so the guarantee holds regardless of what the model decides. The
    compliance rate is therefore trivially perfect and is not worth reporting.
  - **recording is the model signal.** How often the loop *tried* to breach the
    contract still says something real about its judgement, and it is only
    visible because the guard refused rather than the person absorbing it.

One case is deliberately allowed through: a loop that cannot estimate the cost
at all. Forbidding "I do not know how bad this is" would silence the loop exactly
when something unfamiliar is happening, which is the moment a person most wants
to hear from it. Those asks are allowed and counted separately.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

_HOUR_MS = 60 * 60 * 1000
_DAY_MS = 24 * _HOUR_MS


@dataclass(frozen=True)
class InterruptionContract:
    """What the user has agreed to be woken for.

    ``quiet_hours`` is a half-open ``[start, end)`` range of wall-clock hours and
    may wrap midnight. Inside it, ``quiet_min_expected_loss_ms`` applies instead
    of the daytime threshold; leaving it None forbids interruption outright
    during those hours.
    """

    min_expected_loss_ms: int = 0
    quiet_hours: tuple[int, int] | None = None
    quiet_min_expected_loss_ms: int | None = None
    max_asks: int | None = None

    def threshold_at_hour(self, hour: int) -> int | None:
        """The bar an interruption must clear at that hour, or None if the
        contract forbids interrupting then."""
        if self.quiet_hours is None or not self._is_quiet(hour):
            return self.min_expected_loss_ms
        return self.quiet_min_expected_loss_ms

    def as_instruction(self) -> str:
        """The contract in a sentence, for the loop's prompt.

        Enforcement alone would let the loop spend every turn on asks that are
        refused, so it has to be told the same rule the guard applies.
        """
        parts = []
        if self.min_expected_loss_ms:
            parts.append(
                f"only contact a person when not doing so is expected to cost at least "
                f"{self.min_expected_loss_ms // 60_000} minutes of machine time"
            )
        else:
            parts.append("you may contact a person when you judge it necessary")
        if self.quiet_hours is not None:
            start, end = self.quiet_hours
            if self.quiet_min_expected_loss_ms is None:
                parts.append(f"never between {start:02d}:00 and {end:02d}:00")
            else:
                parts.append(
                    f"between {start:02d}:00 and {end:02d}:00 the bar rises to "
                    f"{self.quiet_min_expected_loss_ms // 60_000} minutes"
                )
        if self.max_asks is not None:
            parts.append(f"at most {self.max_asks} times in this campaign")
        parts.append("if you genuinely cannot estimate the cost, say so and ask anyway")
        return "; ".join(parts) + "."

    def _is_quiet(self, hour: int) -> bool:
        start, end = self.quiet_hours  # type: ignore[misc]
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end


@dataclass
class Decision:
    allowed: bool
    reason: str | None = None
    estimated: bool = True


class ContractGuard:
    """Gate on the escalation path.

    ``start_hour`` is the wall-clock hour the campaign began, which is what turns
    campaign milliseconds into an hour of day. A campaign that spans days keeps
    working because the hour wraps.
    """

    def __init__(
        self,
        contract: InterruptionContract,
        *,
        start_hour: int = 9,
    ) -> None:
        self._contract = contract
        self._start_hour = start_hour
        self._allowed = 0
        self._breach_attempts: list[tuple[int, str]] = []
        self._unestimated = 0

    def hour_at(self, at_ms: int) -> int:
        return (self._start_hour + (at_ms % _DAY_MS) // _HOUR_MS) % 24

    def check(self, *, at_ms: int, expected_loss_ms: int | None) -> Decision:
        hour = self.hour_at(at_ms)
        if self._contract.max_asks is not None and self._allowed >= self._contract.max_asks:
            return self._deny(at_ms, f"the contract allows {self._contract.max_asks} interruptions and they are used")
        if expected_loss_ms is None:
            self._allowed += 1
            self._unestimated += 1
            return Decision(True, estimated=False)
        threshold = self._contract.threshold_at_hour(hour)
        if threshold is None:
            return self._deny(at_ms, f"the contract forbids interruption at {hour:02d}:00")
        if expected_loss_ms < threshold:
            return self._deny(
                at_ms,
                f"expected loss {expected_loss_ms // 60_000}min is under the "
                f"{threshold // 60_000}min bar in force at {hour:02d}:00",
            )
        self._allowed += 1
        return Decision(True)

    def ask(
        self,
        human: Any,
        topic: str,
        message: str,
        *,
        at_ms: int,
        expected_loss_ms: int | None,
    ) -> Decision:
        """Escalate through the contract. A denied ask never reaches the person,
        which is what makes the guarantee hold independently of the model."""
        decision = self.check(at_ms=at_ms, expected_loss_ms=expected_loss_ms)
        if decision.allowed:
            human.ask(topic, message)
        return decision

    # ---- measurement surface ----

    def allowed_asks(self) -> int:
        return self._allowed

    def breach_attempts(self) -> list[tuple[int, str]]:
        """Asks the contract refused, as ``(campaign_ms, reason)``.

        Not a compliance figure -- compliance is enforced and therefore perfect.
        This is how often the loop's own judgement disagreed with the contract.
        """
        return list(self._breach_attempts)

    def unestimated_asks(self) -> int:
        return self._unestimated

    def summary(self) -> dict[str, Any]:
        return {
            "allowed": self._allowed,
            "breach_attempts": len(self._breach_attempts),
            "unestimated": self._unestimated,
            "reasons": sorted({reason for _, reason in self._breach_attempts}),
        }

    def _deny(self, at_ms: int, reason: str) -> Decision:
        self._breach_attempts.append((at_ms, reason))
        return Decision(False, reason=reason)

    # ---- durability ----
    #
    # A wake turn starts cold from disk, so a guard rebuilt empty every turn
    # would hand back the whole interruption budget on each wake. The counts are
    # the enforcement; without persisting them there is none.

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": asdict(self._contract),
            "start_hour": self._start_hour,
            "allowed": self._allowed,
            "unestimated": self._unestimated,
            "breach_attempts": [[at, why] for at, why in self._breach_attempts],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ContractGuard:
        fields = dict((payload.get("contract") or {}))
        quiet = fields.get("quiet_hours")
        if quiet is not None:
            fields["quiet_hours"] = tuple(quiet)
        guard = cls(InterruptionContract(**fields), start_hour=int(payload.get("start_hour", 9)))
        guard._allowed = int(payload.get("allowed", 0))
        guard._unestimated = int(payload.get("unestimated", 0))
        guard._breach_attempts = [(int(at), why) for at, why in payload.get("breach_attempts", [])]
        return guard
