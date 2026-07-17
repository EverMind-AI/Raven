"""Import litellm with its import-time terminal noise silenced.

litellm prints a "Provider List" banner (gated by ``suppress_debug_info``) and,
because it installs its own stderr ``StreamHandler`` on its ``LiteLLM*`` loggers,
emits DEBUG to the terminal *while importing*. Raise those loggers' levels across
the import so that DEBUG never reaches the terminal, then restore them.

Stripping the handlers for the rest of the session stays with raven's
``_strip_tty_stream_handlers`` (it runs after this deferred import in the TUI
path); doing it here too would mutate global logging state as a side effect of
merely importing a provider module.
"""

import logging

# litellm attaches its stderr handler to all three (litellm/_logging.py).
_LITELLM_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy")


def import_litellm():
    """Import litellm with its banner disabled and import-time DEBUG suppressed."""
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

    return litellm
