"""The registry's two name lookups, now tables: they answer exactly what the scan did.

``find_by_name`` and ``canonical_provider_name`` walked every spec per call. The
tables that replaced the walk are checked here against the walk itself, for every
spelling a config or a command line can hand them.
"""

from __future__ import annotations

from raven.providers import registry
from raven.providers.registry import PROVIDERS, canonical_provider_name, find_by_name, normalize_provider_name


def _scan_canonical(name: str | None) -> str:
    normalized = normalize_provider_name(name)
    for spec in PROVIDERS:
        if normalized in {normalize_provider_name(a) for a in spec.name_aliases}:
            return spec.name
    return normalized


def _scan_find(name: str | None):
    name = normalize_provider_name(_scan_canonical(name))
    for spec in PROVIDERS:
        if normalize_provider_name(spec.name) == name:
            return spec
    return None


def _spellings():
    for spec in PROVIDERS:
        for word in (spec.name, *spec.name_aliases):
            yield word
            yield word.upper()
            yield word.replace("_", "-")
            yield f"  {word} "
    yield from ("", None, "no_such_vendor", "Nano-GPT")


def test_the_tables_answer_what_the_scan_answered() -> None:
    for word in _spellings():
        assert find_by_name(word) is _scan_find(word), word
        assert canonical_provider_name(word) == _scan_canonical(word), word


def test_a_registry_a_test_installs_is_read_not_remembered(monkeypatch) -> None:
    first, second = PROVIDERS[0], PROVIDERS[1]
    assert find_by_name(second.name) is second

    monkeypatch.setattr(registry, "PROVIDERS", (first,))

    assert find_by_name(first.name) is first
    assert find_by_name(second.name) is None
