# -*- coding: utf-8 -*-
"""Errors raised while validating or running a sub-agent DAG."""


class DagValidationError(ValueError):
    """Raised when a submitted DAG spec is structurally invalid."""


class RoundBudgetSpentError(Exception):
    """The hourly dispatch budget refused one round of a multi-round stint.

    Raised rather than returned because a stint's caller is a driver, not a
    model reading a sentence: the one thing it has to tell apart is a refusal
    that time fixes from one that never will, and a receipt beginning "Error"
    says both. It carries the budget's own refusal so whoever reports the pause
    can quote it.
    """

    def __init__(self, refusal: str) -> None:
        super().__init__(refusal)
        self.refusal = refusal


class RoundNotApprovedError(Exception):
    """The person was asked to approve a stint's first round and did not.

    Raised for the same reason as :class:`RoundBudgetSpentError`: the driver
    has a record to close. Handed the denial as a sentence, it read as a
    started round -- the record stayed ``running`` with round one open, and a
    later ``resume`` ran, unasked, the graph the person had just refused.
    """
