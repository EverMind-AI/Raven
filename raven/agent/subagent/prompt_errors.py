# -*- coding: utf-8 -*-
"""Errors raised while validating or running a sub-agent DAG."""


class DagValidationError(ValueError):
    """Raised when a submitted DAG spec is structurally invalid."""
