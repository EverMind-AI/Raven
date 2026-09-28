"""How a model request becomes a response or a proposal to call tools."""

from ..view import project

NAME = "model_decision"


def current(mechanisms):
    """The current mechanisms acting on this channel, independent of what the declaration grants."""
    return project(mechanisms, channel=NAME)
