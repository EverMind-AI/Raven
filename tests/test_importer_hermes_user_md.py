"""USER.md entries -> native user_memory/profile/user.md sections."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from raven.importer.hermes_user_md import (
    _HEADING_MAX_TOKENS,
    FALLBACK_HEADING,
    import_user_md_sections,
)
from raven.memory_engine.consolidate.consolidator import MemoryStore
from raven.providers.base import LLMResponse


class _Provider:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "model": model, "max_tokens": max_tokens, "temperature": temperature})
        return LLMResponse(content=self._replies.pop(0), finish_reason="stop")


class _Boom:
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        raise TimeoutError("nope")


class _ErrorReply:
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(content=None, finish_reason="error")


async def test_each_entry_becomes_its_own_section_verbatim(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entries = ["Interested in agent internals", "Fitness: 173cm 62.7kg"]
    provider = _Provider(["Topics of Interest", "Routine schedule"])
    headings = await import_user_md_sections(entries, store, provider=provider, model="m")
    assert headings == ["## Topics of Interest", "## Routine schedule"]
    body = store.read_long_term()
    assert "Interested in agent internals" in body
    assert "Fitness: 173cm 62.7kg" in body


async def test_provider_called_with_classification_settings(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    provider = _Provider(["Goals"])
    await import_user_md_sections(["a fact"], store, provider=provider, model="m")
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["model"] == "m"
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == _HEADING_MAX_TOKENS
    assert call["max_tokens"] < 4096


async def test_no_provider_falls_back_to_notes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    headings = await import_user_md_sections(["a fact"], store, provider=None)
    assert headings == [FALLBACK_HEADING]
    assert "a fact" in store.read_long_term()


async def test_provider_failure_falls_back_to_notes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    headings = await import_user_md_sections(["a fact"], store, provider=_Boom(), model="m")
    assert headings == [FALLBACK_HEADING]


async def test_provider_error_finish_reason_falls_back_to_notes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    headings = await import_user_md_sections(["a fact"], store, provider=_ErrorReply(), model="m")
    assert headings == [FALLBACK_HEADING]
    assert "a fact" in store.read_long_term()


async def test_illegal_heading_falls_back_to_notes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    bad = _Provider(["## nope\nwith newline"])
    headings = await import_user_md_sections(["a fact"], store, provider=bad, model="m")
    assert headings == [FALLBACK_HEADING]


async def test_entry_appended_to_existing_heading_preserves_body(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    with store.locked():
        store.update_section("## Goals", "hand written", at_end=False)
    headings = await import_user_md_sections(["imported"], store, provider=_Provider(["Goals"]), model="m")
    assert headings == ["## Goals"]
    body = store.read_long_term()
    assert "hand written" in body
    assert "imported" in body


async def test_two_entries_same_heading_both_present_one_heading(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entries = ["first fact", "second fact"]
    provider = _Provider(["Goals", "Goals"])
    headings = await import_user_md_sections(entries, store, provider=provider, model="m")
    assert headings == ["## Goals", "## Goals"]
    body = store.read_long_term()
    assert "first fact" in body
    assert "second fact" in body
    assert body.count("## Goals") == 1


async def test_rerun_same_entries_is_noop(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entries = ["repeat fact one", "repeat fact two"]
    await import_user_md_sections(entries, store, provider=_Provider(["Goals", "Preferences"]), model="m")
    before = store.read_long_term()
    headings = await import_user_md_sections(entries, store, provider=_Provider(["Goals", "Preferences"]), model="m")
    assert headings == []
    assert store.read_long_term() == before


async def test_entry_text_containing_heading_string_does_not_false_skip_later_entry(
    tmp_path: Path,
) -> None:
    store = MemoryStore(tmp_path)
    entries = [
        "Random fact that literally mentions ## Notes inline",
        "A separate legit note",
    ]
    provider = _Provider(["Goals", "Notes"])
    headings = await import_user_md_sections(entries, store, provider=provider, model="m")
    assert headings == ["## Goals", "## Notes"]
    assert "A separate legit note" in store.read_long_term()


async def test_note_singular_and_notes_plural_are_distinct_sections(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    with store.locked():
        store.update_section("## Notes", "catchall body", at_end=False)
    headings = await import_user_md_sections(["singular section fact"], store, provider=_Provider(["Note"]), model="m")
    assert headings == ["## Note"]
    body = store.read_long_term()
    assert "## Note\n\nsingular section fact" in body
    assert "catchall body" in body


async def test_landed_entry_is_retrievable_by_relevance(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    await import_user_md_sections(
        ["Fitness coaching: recomp 130g protein per day"],
        store,
        provider=_Provider(["Routine schedule"]),
        model="m",
    )
    ctx = store.get_memory_context(current_message="what is my protein target")
    assert "130g protein" in ctx


async def test_blank_entries_are_dropped(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    provider = _Provider(["Goals"])
    written = await import_user_md_sections(["   ", "", "\n\t", "a real fact"], store, provider=provider, model="m")
    assert written == ["## Goals"]
    # A blank entry must not reach the classifier at all.
    assert len(provider.calls) == 1
    assert provider.calls[0]["messages"][0]["content"].endswith("a real fact")
