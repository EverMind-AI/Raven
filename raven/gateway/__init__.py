"""Gateway plumbing: the host-side machinery that moves turns and replies
between the agent loop and the outside world.

``manager`` starts/stops channel adapters, ``intake`` normalizes inbound
messages into turns, ``outlet`` renders replies back onto a channel, and
``live_probe`` / ``lock`` are the daemon's liveness and single-run guards.
Channel *adapters* (the per-service protocol code) stay in ``raven.channels``;
this package is the pipe work they plug into.
"""
