"""The served-model cache: what it reads back, and how an upgrade lands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.providers import served_models


@pytest.fixture
def cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(served_models, "get_cache_dir", lambda: tmp_path)
    return tmp_path


def _write(cache_dir: Path, payload: dict) -> None:
    (cache_dir / served_models.CACHE_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


def test_a_version_1_cache_is_read_as_the_kind_and_facts_shape(cache_dir: Path) -> None:
    """Version 1 stored id to kind; the mapping is direct, so an upgrade keeps
    the labels instead of dropping every provider's list until it is asked."""
    _write(
        cache_dir,
        {
            "version": 1,
            "providers": {
                "openrouter": {"at": 1.0, "models": {"openrouter/a": "image", "openrouter/b": "text"}},
            },
        },
    )

    assert served_models.recall("openrouter") == {
        "openrouter/a": {"kind": "image"},
        "openrouter/b": {"kind": "text"},
    }


def test_remembering_one_provider_keeps_the_migrated_entries(cache_dir: Path) -> None:
    """A write for one provider used to rewrite the file with only that one,
    because the v1 read answered nothing and the providers were gone."""
    _write(
        cache_dir,
        {
            "version": 1,
            "providers": {
                "openrouter": {"at": 1.0, "models": {"openrouter/a": "image"}},
                "deepseek": {"at": 1.0, "models": {"deepseek/b": "text"}},
            },
        },
    )

    served_models.remember("deepseek", {"deepseek/c": {"kind": "text"}})

    assert served_models.recall("openrouter") == {"openrouter/a": {"kind": "image"}}
    assert served_models.recall("deepseek") == {"deepseek/c": {"kind": "text"}}
    stored = json.loads((cache_dir / served_models.CACHE_FILENAME).read_text(encoding="utf-8"))
    assert stored["version"] == served_models.CACHE_VERSION
