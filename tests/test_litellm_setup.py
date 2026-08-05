"""Unit tests for ``import_litellm`` -- banner + import-time DEBUG silencing."""

import logging

from raven.providers.litellm_setup import import_litellm

_LITELLM_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy")


def test_import_litellm_disables_banner() -> None:
    module = import_litellm()

    assert module.suppress_debug_info is True


def test_import_litellm_restores_logger_levels() -> None:
    """The import-time level bump must not persist, or runtime DEBUG would stop
    propagating to the file sink."""
    for name in _LITELLM_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)

    import_litellm()

    for name in _LITELLM_LOGGERS:
        assert logging.getLogger(name).level == logging.DEBUG


def test_import_litellm_is_idempotent() -> None:
    first = import_litellm()
    second = import_litellm()

    assert first is second
