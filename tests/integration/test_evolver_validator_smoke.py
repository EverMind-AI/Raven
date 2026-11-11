"""The shipped agents pass every model-free layer of the agent package validator.

Opt-in (``-m integration``): layer A starts each agent's launcher, which needs a
provider to inherit, and the engine agents need their wheels importable here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolver.validator.runner import ValidateOptions, validate
from raven.home import get_config_path

REPO = Path(__file__).resolve().parents[2]
FLASH = "deepseek/deepseek-flash"
SHIPPED = [
    ("raven-code", None),
    ("raven-research", None),
    ("raven-oncall", None),
    ("raven-design", "plugins-dist/design-engine"),
    ("raven-ppt", "plugins-dist/ppt-engine"),
]


# Resolved at import: tests/conftest.py gives each test its own temporary HOME, and the
# credentials live in the operator's config.
HOST_CONFIG = get_config_path()


def _has_providers() -> bool:
    try:
        return bool(json.loads(HOST_CONFIG.read_text(encoding="utf-8")).get("providers"))
    except (OSError, ValueError):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(not _has_providers(), reason="no providers in the active raven config"),
]


@pytest.mark.parametrize("name, engine", SHIPPED)
def test_shipped_agent_passes_activation(name, engine, tmp_path):
    report = validate(
        ValidateOptions(
            folder=REPO / "agents" / name,
            engine=REPO / engine if engine else None,
            model=FLASH,
            host_config=HOST_CONFIG,
            static_only=True,
            run_dir=tmp_path / "run",
        )
    )
    failures = [(r.rule, r.message) for layer in report.layers for r in layer.results if r.status == "fail"]
    assert report.outcome == "STATIC-OK", failures
    assert report.rendered["model"] == FLASH and report.rendered["memory_backend"] is None
    assert report.rendered["provider"] == "deepseek"
