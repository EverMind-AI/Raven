"""Tool exposure, calls, results and state shared with Loop decisions."""

from ..view import project

NAME = "tool_interaction"


def current(mechanisms):
    """The current mechanisms acting on this channel, independent of what the declaration grants."""
    return project(mechanisms, channel=NAME)
