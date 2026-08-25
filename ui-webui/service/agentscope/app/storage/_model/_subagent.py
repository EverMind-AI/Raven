# -*- coding: utf-8 -*-
"""The sub-agent record."""

from pydantic import Field

from ...._utils._common import _generate_id
from ._base import _RecordBase


class SubAgentRecord(_RecordBase):
    """The record used for storing sub-agent configs."""

    user_id: str = Field(
        default_factory=_generate_id,
    )

    data: dict
    """The sub-agent config data."""
