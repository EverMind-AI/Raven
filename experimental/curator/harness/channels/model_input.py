"""Sources, placement and lifetime of information entering a model request."""

from ..view import project

NAME = "model_input"


def current(mechanisms):
    """The current mechanisms acting on this channel, independent of what the declaration grants."""
    return project(mechanisms, channel=NAME)
