"""The describe call behind an indexed picture: the prompt, the answer, the pin."""

from __future__ import annotations

import asyncio
import base64
import io

import pytest

from raven.knowledge._vision import (
    DESCRIPTION_BUDGET,
    VisionError,
    VisionModel,
    build_prompt,
    clean_description,
    configured_language,
    load_vision_model,
)

#: One transparent pixel. Small enough to pass through preparation untouched,
#: which is what keeps these tests about the call rather than about Pillow.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class _Answer:
    def __init__(self, content: object) -> None:
        self.content = content


class _Provider:
    """Records what it was sent and answers with whatever it was given."""

    def __init__(self, content: object = "a bar chart", error: Exception | None = None, delay: float = 0.0) -> None:
        self.content = content
        self.error = error
        self.delay = delay
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs) -> _Answer:
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return _Answer(self.content)


def _model(**kwargs) -> tuple[VisionModel, _Provider]:
    provider = _Provider(**kwargs)
    return VisionModel(provider=provider, model="qwen-vl", timeout_s=5.0), provider


# -- the prompt ----------------------------------------------------


def test_the_prompt_names_the_language_it_wants_back() -> None:
    assert "descriptions and field values in Chinese" in build_prompt(language="Chinese")


def test_a_prompt_with_no_context_carries_no_context_section() -> None:
    """An empty "CONTEXT (ABOVE)" heading reads as a region the model failed to
    see, and the models worth using are the ones that would try to account for
    it."""
    plain = build_prompt()

    assert "CONTEXT" not in plain
    assert "Surrounding context" not in plain


def test_context_travels_when_there_is_some() -> None:
    """A figure rarely explains itself: the sentence introducing it names what
    it is of, and without that a bar chart is described as a bar chart."""
    prompt = build_prompt(context_above="Revenue held up in Q2.", context_below="Q3 is not in yet.")

    assert "Revenue held up in Q2." in prompt
    assert "Q3 is not in yet." in prompt
    assert "only for minimal clarification" in prompt


def test_one_side_of_context_is_enough_to_carry_it() -> None:
    prompt = build_prompt(context_above="Figure 4.")

    assert "Figure 4." in prompt
    assert "(none)" in prompt, "and the empty side says so rather than looking cut off"


# -- the answer ----------------------------------------------------


def test_a_fenced_answer_is_unwrapped() -> None:
    """A model asked for prose sometimes answers in a code block, and the
    fences would be indexed as content."""
    assert clean_description("```\nA bar chart.\n```") == "A bar chart."
    assert clean_description("```text\nA bar chart.\n```") == "A bar chart."


def test_an_answer_past_the_budget_is_clamped_not_dropped() -> None:
    """The opposite of what a session title does with one: half a description
    still describes the top half of the figure."""
    assert len(clean_description("x" * (DESCRIPTION_BUDGET * 2))) == DESCRIPTION_BUDGET


async def test_the_image_and_the_prompt_travel_as_one_message() -> None:
    model, provider = _model()

    text = await model.describe(PNG)

    assert text == "a bar chart"
    content = provider.calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1]["type"] == "text"
    assert provider.calls[0]["model"] == "qwen-vl"


async def test_an_answer_in_content_parts_is_read() -> None:
    """Some providers answer a multimodal call in parts rather than a string."""
    model, _ = _model(content=[{"type": "text", "text": "a pie chart"}])

    assert await model.describe(PNG) == "a pie chart"


async def test_an_endpoint_that_refuses_says_which_model() -> None:
    model, _ = _model(error=RuntimeError("model_not_found"))

    with pytest.raises(VisionError) as caught:
        await model.describe(PNG)

    assert "qwen-vl" in str(caught.value)
    assert "model_not_found" in str(caught.value)


async def test_a_slow_endpoint_is_given_up_on() -> None:
    """An indexing run is not a turn somebody is waiting on, but it is also not
    allowed to hang the queue behind it."""
    provider = _Provider(delay=1.0)
    model = VisionModel(provider=provider, model="qwen-vl", timeout_s=0.01)

    with pytest.raises(VisionError) as caught:
        await model.describe(PNG)

    assert "did not answer" in str(caught.value)


async def test_bytes_that_are_not_an_image_are_the_callers_problem() -> None:
    model, provider = _model()

    with pytest.raises(VisionError):
        await model.describe(b"this is not a picture")

    assert provider.calls == [], "and nothing was sent"


async def test_a_large_image_is_downscaled_before_it_is_sent() -> None:
    """Endpoints that do not refuse an oversized image downscale it themselves
    and bill for the original."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4000, 4000), "white").save(buffer, format="PNG")
    model, provider = _model()

    await model.describe(buffer.getvalue())

    sent = provider.calls[0]["messages"][0]["content"][0]["image_url"]["url"]
    assert sent.startswith("data:image/jpeg;base64,"), "re-encoded rather than passed through"
    assert len(sent) < len(buffer.getvalue())


# -- the pin -------------------------------------------------------


def test_no_pin_is_no_model_rather_than_a_failure(monkeypatch) -> None:
    """An install with no vision model is an ordinary state: a document parses
    without its figures described, and only a bare picture has to fail."""

    class _Config:
        class vision:  # noqa: N801 - standing in for the config block
            model = None
            provider = None

    monkeypatch.setattr("raven.config.raven.load_raven_config", lambda: _Config())

    assert load_vision_model() is None


def test_an_unusable_pin_is_no_model_either(monkeypatch) -> None:
    """`bind_pin` answers None for a provider with no credential, and a pin
    that cannot be bound must not become a call that fails per figure."""

    class _Config:
        class vision:  # noqa: N801 - standing in for the config block
            model = "qwen-vl"
            provider = "nobody"
            timeout_seconds = 90.0
            max_figures = 64

    monkeypatch.setattr("raven.config.raven.load_raven_config", lambda: _Config())
    monkeypatch.setattr("raven.providers.pool.ProviderPool.bind_pin", lambda self, model, provider=None: None)

    assert load_vision_model() is None


def test_the_language_falls_back_to_english(monkeypatch) -> None:
    """A config that cannot be read still describes pictures, in English."""
    monkeypatch.setattr("raven.config.loader.load_config", lambda: (_ for _ in ()).throw(OSError("gone")))

    assert configured_language() == "English"


def test_the_language_follows_the_install(monkeypatch) -> None:
    class _Config:
        language = "zh"

    monkeypatch.setattr("raven.config.loader.load_config", lambda: _Config())

    assert configured_language() == "Chinese"
