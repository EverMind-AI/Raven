"""The failures a provider raises, in a module neither half of the pair owns.

``raven.providers.auth`` declares what a provider needs and ``raven.providers.endpoints``
resolves what a section offers, and each reaches the other: the gate asks the
reader which material a section carries, and the reader is asked for the
requirement that decides the gate. The error both halves raise used to live in
``auth``, which made the pair a cycle -- and ``tests/test_import_cycle_budget.py``
counts a function-local import exactly like a top-level one, so deferring the
import would not have helped.

The same shape as ``raven/trajectory/errors.py``: the shared exception lives in
a leaf that imports nothing from ``raven``, and both halves import it from
there.
"""

from __future__ import annotations


class MissingCredentialsError(Exception):
    """A provider cannot be used because its credentials are absent.

    Raised where the gate is decided, not where it is reported: the check runs
    behind three entry points (the CLI, the gateway and the TUI) and each renders
    the refusal its own way.
    """

    def __init__(self, summary: str, *, provider: str = "", remedy: str = ""):
        super().__init__(summary)
        self.summary = summary
        self.provider = provider
        #: The command that fixes it, when there is one to name.
        self.remedy = remedy
