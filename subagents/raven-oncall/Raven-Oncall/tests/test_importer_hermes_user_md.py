"""USER.md entries -> native user_memory/profile/user.md sections."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

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
    assert headings.written == ("## Topics of Interest", "## Routine schedule")
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


async def test_progress_counts_finished_calls_not_started_ones(tmp_path: Path) -> None:
    """The bar showed 3/3 while the third LLM call was still in flight, which is
    the 100%-then-wait it was added to remove. Each report has to be a count of
    what is done, and the last one still has to reach N/N.
    """
    store = MemoryStore(tmp_path)
    provider = _Provider(["Goals", "Preferences", "Notes"])
    seen: list[tuple[int, int]] = []
    reports: list[tuple[int, int]] = []

    def _on_progress(done: int, total: int) -> None:
        # Captured against the provider's call count: that is the only way to
        # tell "about to start #3" from "finished #2" at the same number.
        reports.append((done, total))
        seen.append((done, len(provider.calls)))

    await import_user_md_sections(
        ["a", "b", "c"],
        store,
        provider=provider,
        model="m",
        on_progress=_on_progress,
    )

    assert all(done == made for done, made in seen), f"a report ran ahead of the calls: {seen}"
    assert reports[-1] == (3, 3), reports


async def test_no_provider_falls_back_to_notes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    headings = await import_user_md_sections(["a fact"], store, provider=None)
    assert headings.written == (FALLBACK_HEADING,)
    assert "a fact" in store.read_long_term()


async def test_provider_failure_falls_back_to_notes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    headings = await import_user_md_sections(["a fact"], store, provider=_Boom(), model="m")
    assert headings.written == (FALLBACK_HEADING,)


async def test_provider_error_finish_reason_falls_back_to_notes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    headings = await import_user_md_sections(["a fact"], store, provider=_ErrorReply(), model="m")
    assert headings.written == (FALLBACK_HEADING,)
    assert "a fact" in store.read_long_term()


async def test_illegal_heading_falls_back_to_notes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    bad = _Provider(["## nope\nwith newline"])
    headings = await import_user_md_sections(["a fact"], store, provider=bad, model="m")
    assert headings.written == (FALLBACK_HEADING,)


async def test_entry_appended_to_existing_heading_preserves_body(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    with store.locked():
        store.update_section("## Goals", "hand written", at_end=False)
    headings = await import_user_md_sections(["imported"], store, provider=_Provider(["Goals"]), model="m")
    assert headings.written == ("## Goals",)
    body = store.read_long_term()
    assert "hand written" in body
    assert "imported" in body


async def test_two_entries_same_heading_both_present_one_heading(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entries = ["first fact", "second fact"]
    provider = _Provider(["Goals", "Goals"])
    headings = await import_user_md_sections(entries, store, provider=provider, model="m")
    assert headings.written == ("## Goals", "## Goals")
    body = store.read_long_term()
    assert "first fact" in body
    assert "second fact" in body
    assert body.count("## Goals") == 1


async def test_rerun_same_entries_is_noop(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entries = ["repeat fact one", "repeat fact two"]
    await import_user_md_sections(entries, store, provider=_Provider(["Goals", "Preferences"]), model="m")
    before = store.read_long_term()
    result = await import_user_md_sections(entries, store, provider=_Provider(["Goals", "Preferences"]), model="m")
    assert result.written == ()
    assert result.skipped == 2
    assert len(result) == 0
    assert store.read_long_term() == before


async def test_distinct_entry_that_is_a_substring_of_another_is_not_dropped(tmp_path: Path) -> None:
    """Reproduces the reported failure: a whole-file substring check would
    read "dark mode" as already present once "I prefer dark mode in every
    editor." has landed, even though it is a genuinely distinct entry."""
    store = MemoryStore(tmp_path)
    entries = ["I prefer dark mode in every editor.", "dark mode"]
    provider = _Provider(["Preferences", "Preferences"])
    result = await import_user_md_sections(entries, store, provider=provider, model="m")
    assert result.written == ("## Preferences", "## Preferences")
    assert result.skipped == 0
    body = store.read_long_term()
    assert "I prefer dark mode in every editor." in body
    assert "dark mode" in body


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
    assert headings.written == ("## Goals", "## Notes")
    assert "A separate legit note" in store.read_long_term()


async def test_note_singular_and_notes_plural_are_distinct_sections(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    with store.locked():
        store.update_section("## Notes", "catchall body", at_end=False)
    headings = await import_user_md_sections(["singular section fact"], store, provider=_Provider(["Note"]), model="m")
    assert headings.written == ("## Note",)
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
    result = await import_user_md_sections(["   ", "", "\n\t", "a real fact"], store, provider=provider, model="m")
    assert result.written == ("## Goals",)
    assert result.skipped == 0
    # A blank entry must not reach the classifier at all.
    assert len(provider.calls) == 1
    assert provider.calls[0]["messages"][0]["content"].endswith("a real fact")


class _LockTrackingStore(MemoryStore):
    """Real ``MemoryStore`` that also counts how many callers currently hold
    ``locked()``, so a test can assert an LLM call never overlaps a write."""

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.lock_depth = 0

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.lock_depth += 1
        try:
            with super().locked():
                yield
        finally:
            self.lock_depth -= 1


class _LockObservingProvider:
    """Records whether ``chat()`` was ever called while the store's lock was
    held -- the LLM call must happen before ``import_user_md_sections`` takes
    the write lock, not during it."""

    def __init__(self, replies: list[str], store: _LockTrackingStore) -> None:
        self._replies = list(replies)
        self._store = store
        self.saw_lock_held = False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        if self._store.lock_depth > 0:
            self.saw_lock_held = True
        return LLMResponse(content=self._replies.pop(0), finish_reason="stop")


async def test_llm_heading_pick_happens_outside_the_write_lock(tmp_path: Path) -> None:
    store = _LockTrackingStore(tmp_path)
    provider = _LockObservingProvider(["Goals", "Preferences"], store)
    await import_user_md_sections(["fact one", "fact two"], store, provider=provider, model="m")
    assert not provider.saw_lock_held
