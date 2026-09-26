"""How judgments and lifecycle actions affect continuation, resampling and completion."""

from ..view import project

NAME = "execution_control"


def current(mechanisms):
    """The current mechanisms acting on this channel, independent of what the declaration grants."""
    return project(mechanisms, channel=NAME)
