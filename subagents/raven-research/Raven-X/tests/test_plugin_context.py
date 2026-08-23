"""PG-1 — PluginContext + ServiceLocator structural tests."""

from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from raven.plugin import PluginContext, ServiceLocator


class TestServiceLocator:
    def test_holds_workspace(self, tmp_path: Path) -> None:
        loc = ServiceLocator(workspace=tmp_path)
        assert loc.workspace == tmp_path

    def test_frozen(self, tmp_path: Path) -> None:
        loc = ServiceLocator(workspace=tmp_path)
        with pytest.raises(FrozenInstanceError):
            loc.workspace = tmp_path / "other"  # type: ignore[misc]


class TestPluginContext:
    def test_constructed_with_minimum_fields(self, tmp_path: Path) -> None:
        ctx = PluginContext(
            config={"mode": "embedded"},
            services=ServiceLocator(workspace=tmp_path),
        )
        assert ctx.config == {"mode": "embedded"}
        assert ctx.services.workspace == tmp_path

    def test_default_logger_assigned(self, tmp_path: Path) -> None:
        ctx = PluginContext(
            config={},
            services=ServiceLocator(workspace=tmp_path),
        )
        assert isinstance(ctx.logger, logging.Logger)
        # Default name lands under the raven.plugin namespace so
        # plugin output is grep-able alongside host output.
        assert ctx.logger.name.startswith("raven.plugin")

    def test_explicit_logger(self, tmp_path: Path) -> None:
        my_logger = logging.getLogger("raven.plugin.everos")
        ctx = PluginContext(
            config={},
            services=ServiceLocator(workspace=tmp_path),
            logger=my_logger,
        )
        assert ctx.logger is my_logger

    def test_frozen(self, tmp_path: Path) -> None:
        ctx = PluginContext(
            config={},
            services=ServiceLocator(workspace=tmp_path),
        )
        with pytest.raises(FrozenInstanceError):
            ctx.config = {"changed": True}  # type: ignore[misc]
