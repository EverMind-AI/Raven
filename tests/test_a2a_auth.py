"""Only a caller holding the configured token reaches a turn."""

import pytest

from raven.a2a.auth import is_authorized
from raven.config.schema import A2aServerConfig

CONFIG = A2aServerConfig(enabled=True, token="t0ken")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer t0ken", True),
        ("bearer t0ken", True),
        ("Bearer wrong", False),
        ("t0ken", False),
        ("", False),
        (None, False),
    ],
)
def test_only_the_configured_bearer_token_is_accepted(header, expected):
    assert is_authorized(CONFIG, header) is expected


def test_an_empty_configured_token_refuses_everyone():
    open_cfg = A2aServerConfig(enabled=True, token="")
    assert is_authorized(open_cfg, "Bearer ") is False
    assert is_authorized(open_cfg, None) is False
