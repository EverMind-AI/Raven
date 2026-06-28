"""Tests for raven.channels.registry — adapter auto-discovery (spec-based)."""

from raven.channels.contract import Capabilities, ChannelSpec
from raven.channels.registry import discover_channel_names, discover_specs


def test_discover_channel_names_lists_adapter_packages():
    names = discover_channel_names()
    assert {"telegram", "qq", "slack"} <= set(names)
    assert "__pycache__" not in names


def test_discover_specs_returns_channel_specs():
    specs = discover_specs()
    assert specs                                   # non-empty
    assert set(specs) <= set(discover_channel_names())
    for spec in specs.values():
        assert isinstance(spec, ChannelSpec)
        assert callable(spec.factory)
        assert isinstance(spec.display_name, str) and spec.display_name
        assert isinstance(spec.capabilities, Capabilities)


def test_discover_specs_declares_interactive_login_for_qr_channels():
    specs = discover_specs()
    assert specs["whatsapp"].capabilities.interactive_login is True
    assert specs["weixin"].capabilities.interactive_login is True
    assert specs["telegram"].capabilities.interactive_login is False


def test_discover_specs_is_cheap():
    """Discovery imports only each spec.py — never the channel SDKs (those are
    deferred into the spec factories)."""
    import subprocess
    import sys
    code = (
        "import sys; from raven.channels.registry import discover_specs; discover_specs();"
        "pulled = {m for m in ('botpy', 'telegram', 'slack_sdk', 'lark_oapi', 'nio') if m in sys.modules};"
        "assert not pulled, f'discovery pulled in channel SDKs: {pulled}'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
