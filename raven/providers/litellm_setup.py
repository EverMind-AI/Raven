"""Import litellm with its terminal noise silenced, at import and afterwards.

litellm prints a "Provider List" banner (gated by ``suppress_debug_info``) and,
because it installs its own stderr ``StreamHandler`` on its ``LiteLLM*`` loggers,
emits DEBUG to the terminal *while importing*. Raise those loggers' levels across
the import so that DEBUG never reaches the terminal, then restore them.

Then detach that handler for good. raven's ``_strip_tty_stream_handlers`` cannot
do it: it runs while the CLI sets up logging, and every litellm import in raven is
deferred, so the handler is installed *after* the strip has already run and
nothing removes it. What follows is a session where every litellm record --
including DEBUG, because raven's stdlib intercept sets the root level to 0 -- is
written straight to the terminal, over the Ink screen. Detaching leaves the
records propagating to root, so they still reach the log file sink.
"""

import logging
import os
import sys

# litellm attaches its stderr handler to all three (litellm/_logging.py).
_LITELLM_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy")


def _detach_tty_handlers(loggers: list[logging.Logger]) -> None:
    """Remove the terminal ``StreamHandler``s litellm put on its own loggers."""
    tty_streams = (sys.stderr, sys.stdout)
    for lg in loggers:
        for handler in list(lg.handlers):
            if isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) in tty_streams:
                lg.removeHandler(handler)


def _point_oauth_tokens_at_raven() -> None:
    """Send the credentials LiteLLM's drivers own to raven's OAuth directory.

    Both authenticators read their variable in ``__init__`` and create the
    directory, so these have to be set before litellm is imported at all. An
    explicit setting by the user wins.
    """
    from raven.config.paths import get_oauth_dir

    oauth_dir = get_oauth_dir()
    os.environ.setdefault("GITHUB_COPILOT_TOKEN_DIR", str(oauth_dir / "github_copilot"))
    os.environ.setdefault("CHATGPT_TOKEN_DIR", str(oauth_dir / "chatgpt"))


def _use_local_model_cost_map() -> None:
    """Read the model catalogue from the installed wheel, not over the network.

    ``litellm/__init__`` fetches the catalogue by HTTP on the way up (a 5s
    timeout, then the wheel's copy as fallback), and it reads this variable
    while doing so, so it has to be set before litellm is imported at all.
    Every raven import of litellm sits on a startup path, where a slow or
    blocked network becomes a slow start, and it is what the test suite already
    pins (``tests/conftest.py``). The catalogue carries pricing as well as
    context windows, so this also freezes ``cost_usd`` to the installed wheel:
    a model repriced upstream reads stale until the wheel is bumped. That is
    the trade -- the fetch it removes is one every start paid for, and the
    fallback it lands on is the file a slow network already produced. An
    explicit setting by the user wins.
    """
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


def import_litellm():
    """Import litellm with its banner disabled and its terminal handler detached."""
    _point_oauth_tokens_at_raven()
    _use_local_model_cost_map()
    loggers = [logging.getLogger(name) for name in _LITELLM_LOGGERS]
    prev_levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.WARNING)
    try:
        import litellm

        litellm.suppress_debug_info = True
    finally:
        for lg, prev in zip(loggers, prev_levels):
            lg.setLevel(prev)

    _detach_tty_handlers(loggers)

    return litellm
