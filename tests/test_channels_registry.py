"""Tests for raven.channels.registry — adapter auto-discovery (spec-based)."""

import importlib
import sys
from pathlib import Path

import pytest
from loguru import logger

from raven.channels.contract import Capabilities, ChannelSpec
from raven.channels.registry import discover_channel_names, discover_specs


def test_discover_channel_names_lists_adapter_packages():
    names = discover_channel_names()
    assert {"telegram", "qq", "slack"} <= set(names)
    assert "__pycache__" not in names


def test_discover_specs_returns_channel_specs():
    specs = discover_specs()
    assert specs  # non-empty
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


@pytest.fixture
def scratch_adapters(tmp_path: Path, monkeypatch):
    """A second directory the adapters package spans for one test.

    Whatever discovery imported from it is dropped from ``sys.modules`` again,
    so a fake adapter cannot outlive the test that wrote it."""
    import raven.channels.adapters as pkg

    monkeypatch.setattr(pkg, "__path__", [*pkg.__path__, str(tmp_path)])
    yield tmp_path
    for child in tmp_path.iterdir():
        dotted = f"{pkg.__name__}.{child.name}"
        for key in [k for k in sys.modules if k == dotted or k.startswith(dotted + ".")]:
            del sys.modules[key]
        if hasattr(pkg, child.name):
            delattr(pkg, child.name)


def _adapter(root: Path, name: str, *, spec: str | None = None) -> None:
    (root / name).mkdir()
    (root / name / "__init__.py").write_text("")
    if spec is not None:
        (root / name / "spec.py").write_text(spec)
    importlib.invalidate_caches()


def _discover_with_warnings() -> tuple[dict, list[str]]:
    warnings: list[str] = []
    sink_id = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        return discover_specs(), warnings
    finally:
        logger.remove(sink_id)


def test_an_adapter_without_a_spec_is_skipped_silently(scratch_adapters: Path):
    _adapter(scratch_adapters, "specless")

    specs, warnings = _discover_with_warnings()

    assert "specless" in discover_channel_names()
    assert "specless" not in specs
    assert not [w for w in warnings if "specless" in w]


def test_a_spec_that_imports_a_missing_module_is_skipped_with_a_warning(scratch_adapters: Path):
    """The adapters defer their SDK imports into ``SPEC.factory``, so a spec that
    fails to import is a broken adapter, not an absent one. It must not take
    gateway startup down with it, and it must not disappear without a word."""
    _adapter(scratch_adapters, "broken", spec="import sdk_nobody_installed\n")

    specs, warnings = _discover_with_warnings()

    assert "broken" not in specs
    assert "telegram" in specs
    about_broken = [w for w in warnings if "broken" in w]
    assert len(about_broken) == 1
    assert "sdk_nobody_installed" in about_broken[0]
