"""Whether this instance has the on-call surface, which the owner decides.

Everything the on-call loop adds is inert for someone who never runs work on
another machine, and none of it is free: thirteen tools carry about 4800 tokens
of schema into every turn, the judgement that reads a request as work-to-watch
costs one model call per turn that touches a path, and ``exec`` grows a parameter
plus a description naming tools that owner will never call. Measured 2026-08-19
while integrating with upstream, by upstream's own tests: the tool schemas ate
enough of the window that a budget test dropped from 130000 to 127933 tokens of
room for history, and the extra call made a usage total read 4000 where the
scripted calls added to 6000.

So it is off unless the owner says otherwise, in ``tools.oncall.enabled``.

Inferring it instead from the connection registry was tried first and dropped.
Registering a machine says this installation COULD run work somewhere, which is
not the same as wanting the apparatus today: an owner who connects a simulation
platform and then spends the afternoon in ordinary conversation would pay for it
every turn, and would have no way to say no -- the condition being a side effect
of setup means there is nothing to switch off. It also tied two capabilities to
one answer, since reaching another machine with ``exec`` and running a watched
campaign are different things to want.

Read per call rather than cached, so an owner who flips it mid-session has it
take effect on the next turn.
"""

from __future__ import annotations


def on_call_available() -> bool:
    """Whether the on-call surface should be present in this instance.

    Never raises. This sits in front of tool registration and of every
    path-touching tool result, so an unreadable or half-written config must leave
    ordinary work running rather than take the turn down; unreadable reads as
    off, which is also what an untouched config says.
    """
    try:
        from raven.config.loader import load_config

        return bool(load_config().tools.oncall.enabled)
    except Exception:  # noqa: BLE001
        return False
