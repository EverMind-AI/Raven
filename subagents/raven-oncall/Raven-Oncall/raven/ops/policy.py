"""Decision policy for the Ops loop (the minimal decision brain).

For now the only decision is retry-vs-escalate on a failed attempt, keyed on the
attempt count. Richer diagnosis (adjust the trial config, pick the next trial)
will extend this module; it is kept separate from Campaign so the policy can
evolve independently of the orchestration mechanics.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to retry a failed attempt before escalating.

    ``max_retries`` counts retries *after* the first attempt, so the total
    number of attempts allowed is ``max_retries + 1``.
    """

    max_retries: int = 0

    def should_retry(self, failed_attempt: int) -> bool:
        """Given the 1-based attempt number that just failed, allow another?"""
        return failed_attempt <= self.max_retries
