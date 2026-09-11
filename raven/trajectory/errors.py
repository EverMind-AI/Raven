"""Bug-report failure types, in a module neither half of the pipeline owns.

``review`` subclasses and raises these, and ``bugreport`` calls into ``review``
to adjudicate findings. Defining them here instead of in ``bugreport`` is what
keeps that dependency one-directional: the budget in
``tests/test_import_cycle_budget.py`` counts a function-local import exactly
like a top-level one, so two modules importing each other at all is the thing
it measures -- deferring the import would not have helped.
"""

from __future__ import annotations


class BugReportError(Exception):
    """Base for bug-report pipeline failures."""


class PreparationError(BugReportError):
    """Preparation failed before the record landed (nothing was created).

    Expected filesystem/archive failures (disk full, permissions, tar errors)
    are normalized into this so the UI can show the fixed pre-record failure
    block instead of crashing the browser.
    """
