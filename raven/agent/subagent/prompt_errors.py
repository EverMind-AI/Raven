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
