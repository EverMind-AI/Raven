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


# Rows the installed LiteLLM catalogue lacks and raven vouches for itself.
# ``deepseek/deepseek-flash`` is DeepSeek's own API name for V4.1-Flash (the
# retired ``deepseek-v4-flash`` routes to it); LiteLLM files nothing under it,
# so the window resolver fell back to 65,536 for a 1,048,576-token model and
# the cost recorder wrote 0 (2026-09-11). Prices are DeepSeek's peak-hour
# rates per token -- off-peak is half, and a flat table cannot say so, so the
# recorded cost is an upper bound. Keyed by the exact ids the wire uses; the
# OpenRouter-routed ``openrouter/deepseek/...`` ids are deliberately absent,
# OpenRouter prices those itself.
_DEEPSEEK_FLASH_ROW = {
    "max_tokens": 384_000,
    "max_input_tokens": 1_048_576,
    "max_output_tokens": 384_000,
    "input_cost_per_token": 0.30 / 1_000_000,
    "output_cost_per_token": 1.20 / 1_000_000,
    "cache_read_input_token_cost": 0.006 / 1_000_000,
    "input_cost_per_token_cache_hit": 0.006 / 1_000_000,
    "litellm_provider": "deepseek",
    "mode": "chat",
    "supports_function_calling": True,
    "supports_parallel_function_calling": True,
    "supports_prompt_caching": True,
    "supports_reasoning": True,
    "source": "https://api-docs.deepseek.com/quick_start/pricing (peak-hour rates)",
}
RAVEN_MODEL_ROWS: dict[str, dict] = {
    "deepseek/deepseek-flash": _DEEPSEEK_FLASH_ROW,
    "deepseek-flash": _DEEPSEEK_FLASH_ROW,
}


def _register_raven_model_rows(litellm) -> None:
    """Add :data:`RAVEN_MODEL_ROWS` to LiteLLM's live table, once per row.

    ``register_model`` writes into ``litellm.model_cost`` -- the dict the
    window resolver, the output-ceiling resolver and the cost recorder all
    read -- so one registration answers all three. A row already present
    (a newer LiteLLM that learned the id) is left as it is.
    """
    missing = {mid: row for mid, row in RAVEN_MODEL_ROWS.items() if mid not in litellm.model_cost}
    if missing:
        litellm.register_model(missing)


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
        _register_raven_model_rows(litellm)
    finally:
        for lg, prev in zip(loggers, prev_levels):
            lg.setLevel(prev)

    _detach_tty_handlers(loggers)

    return litellm
