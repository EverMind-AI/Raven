"""Unit tests for Raven-Design launcher configuration inheritance."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO_ROOT / "subagents" / "raven-design" / "run.py"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("raven_design_launcher", _LAUNCHER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config() -> dict:
    return {
        "tools": {
            "media": {
                "image": {
                    "allowModelOverride": False,
                    "apiBase": "https://openrouter.ai/api/v1",
                    "apiStyle": "openrouter_images",
                    "model": "openai/gpt-image-2",
                }
            }
        }
    }


def test_explicit_image_key_keeps_worker_model(launcher, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESIGN_IMAGE_API_KEY", "shared-key")
    config = _config()
    host = {
        "tools": {
            "media": {
                "image": {
                    "apiKey": "shared-key",
                    "apiBase": "https://openrouter.ai/api/v1",
                    "model": "google/gemini-2.5-flash-image",
                }
            }
        }
    }

    launcher.configure_image_generation(config, host)

    image = config["tools"]["media"]["image"]
    assert image["apiKey"] == "shared-key"
    assert image["model"] == "openai/gpt-image-2"
    assert image["apiStyle"] == "openrouter_images"


def test_borrowed_host_media_key_keeps_host_model(launcher, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DESIGN_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("DESIGN_API_KEY", raising=False)
    config = _config()
    host = {
        "tools": {
            "media": {
                "image": {
                    "apiKey": "host-key",
                    "apiBase": "https://openrouter.ai/api/v1",
                    "model": "google/gemini-2.5-flash-image",
                }
            }
        }
    }

    launcher.configure_image_generation(config, host)

    image = config["tools"]["media"]["image"]
    assert image["apiKey"] == "host-key"
    assert image["model"] == "google/gemini-2.5-flash-image"


def test_borrowed_images_backend_keeps_host_protocol(launcher, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DESIGN_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("DESIGN_API_KEY", raising=False)
    config = _config()
    host = {
        "tools": {
            "media": {
                "image": {
                    "apiKey": "host-key",
                    "apiBase": "http://images.test/v1",
                    "apiStyle": "images",
                    "model": "gpt-image-2",
                    "bypassProxy": True,
                    "allowModelOverride": False,
                }
            }
        }
    }

    launcher.configure_image_generation(config, host)

    assert config["tools"]["media"]["image"] == host["tools"]["media"]["image"]


def test_non_openrouter_backend_without_protocol_is_not_borrowed(
    launcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DESIGN_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("DESIGN_API_KEY", raising=False)
    config = _config()
    host = {
        "tools": {
            "media": {
                "image": {
                    "apiKey": "host-key",
                    "apiBase": "http://images.test/v1",
                    "model": "gpt-image-2",
                }
            }
        }
    }

    launcher.configure_image_generation(config, host)

    image = config["tools"]["media"]["image"]
    assert image["apiKey"] == ""
    assert image["model"] == ""


def test_inherit_llm_resolves_parent_model_and_reasoning_effort(
    launcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAVEN_PARENT_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("RAVEN_PARENT_REASONING_EFFORT", "max")
    config = {
        "agents": {
            "defaults": {
                "provider": "custom",
                "model": "old-model",
                "reasoningEffort": "high",
            }
        }
    }
    host = {
        "providers": {
            "custom": {
                "apiKey": "host-key",
                "models": ["gpt-5.6-luna", "gpt-5.6-sol"],
            }
        },
        "agents": {
            "defaults": {
                "provider": "",
                "model": "gpt-5.6-luna",
                "reasoningEffort": "high",
            }
        },
    }

    inherited = launcher.inherit_llm(config, host)

    assert inherited == "provider=custom model=gpt-5.6-sol reasoning_effort=max"
    assert config["agents"]["defaults"] == {
        "provider": "custom",
        "model": "gpt-5.6-sol",
        "reasoningEffort": "max",
    }
