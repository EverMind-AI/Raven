"""read_file's image branch: preprocessing, capability routing, persistence.

Covers the invariant that makes the feature safe to ship — base64 reaches the
model for exactly one turn and never reaches disk.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from pathlib import Path

import pytest

from raven.agent.loop.main import _strip_inline_images
from raven.agent.tools import media
from raven.agent.tools.base import ToolOutput, ToolResult
from raven.agent.tools.filesystem import ReadFileTool
from raven.agent.tools.registry import ToolRegistry
from raven.providers import rates as _pricing
from raven.providers.base import LLMProvider

# Captured before the autouse _no_openrouter_network fixture swaps it out.
_REAL_FETCH = _pricing._fetch_openrouter_models

from raven.providers.capabilities import (  # noqa: E402
    IMAGE_TOOL_RESULT_TARGETS,
    image_placeholder_text,
    supports_image_tool_result,
)


def _join_warm() -> None:
    """Wait out any background catalog warm this test started."""
    for thread in threading.enumerate():
        if thread.name == "raven-model-catalog-warm":
            thread.join(timeout=10)


def _write_image(path: Path, size: tuple[int, int], fmt: str = "PNG") -> Path:
    from PIL import Image

    Image.new("RGB", size, (12, 130, 90)).save(path, format=fmt)
    return path


def _read(tmp_path: Path, name: str, **kwargs) -> str | ToolResult:
    tool = ReadFileTool(workspace=tmp_path)
    return asyncio.run(tool.execute(path=str(tmp_path / name), **kwargs))


# --------------------------------------------------------------------------
# preprocessing
# --------------------------------------------------------------------------


def test_prepare_image_passes_through_a_small_inline_format_unchanged(tmp_path: Path) -> None:
    raw = _write_image(tmp_path / "small.png", (120, 90)).read_bytes()
    payload, mime, meta = media.prepare_image(raw, "image/png")

    assert payload == raw
    assert mime == "image/png"
    assert (meta["width"], meta["height"]) == (120, 90)
    assert meta["resized"] is False and meta["recompressed"] is False


def test_prepare_image_downscales_to_satisfy_both_pixel_and_token_caps(tmp_path: Path) -> None:
    raw = _write_image(tmp_path / "huge.png", (4000, 3000)).read_bytes()
    _, mime, meta = media.prepare_image(raw, "image/png")

    assert meta["resized"] is True
    assert max(meta["width"], meta["height"]) <= media.MAX_DIMENSION_PX
    # The point of checking both constraints: staying under 2000px is not enough
    # if the patch count still exceeds the tier ceiling.
    assert meta["tokens"] <= media.MAX_IMAGE_TOKENS
    assert mime == "image/jpeg"
    assert meta["original_width"] == 4000


def test_prepare_image_converts_a_format_no_provider_inlines(tmp_path: Path) -> None:
    raw = _write_image(tmp_path / "pic.bmp", (300, 200), fmt="BMP").read_bytes()
    payload, mime, meta = media.prepare_image(raw, "image/bmp")

    assert mime == "image/jpeg"
    assert meta["recompressed"] is True
    assert payload[:3] == b"\xff\xd8\xff"


def test_describe_image_names_the_path_so_a_later_turn_can_re_read_it(tmp_path: Path) -> None:
    raw = _write_image(tmp_path / "chart.png", (4000, 3000)).read_bytes()
    _, _, meta = media.prepare_image(raw, "image/png")
    text = media.describe_image(tmp_path / "chart.png", meta)

    assert f"[image: {tmp_path / 'chart.png'}]" in text
    assert "downscaled from 4000x3000" in text
    assert "tokens" in text


# --------------------------------------------------------------------------
# read_file routing
# --------------------------------------------------------------------------


def test_read_file_still_returns_numbered_text_for_a_text_file(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n")
    out = _read(tmp_path, "notes.txt")

    assert isinstance(out, str)
    assert "1| alpha" in out and "2| beta" in out


def test_read_file_returns_text_and_image_blocks_for_an_image(tmp_path: Path) -> None:
    _write_image(tmp_path / "chart.png", (300, 200))
    out = _read(tmp_path, "chart.png")

    assert isinstance(out, ToolResult)
    assert [b["type"] for b in out.blocks] == ["text", "image_url"]
    assert out.blocks[1]["image_url"]["url"].startswith("data:image/")
    # model_text must stand alone: it is what a text-only provider and the
    # session transcript get instead of the picture.
    assert str(tmp_path / "chart.png") in out.model_text
    assert "300x200px" in out.model_text
    assert out.display_text and "chart.png" in out.display_text


def test_read_file_detects_an_image_by_magic_bytes_not_extension(tmp_path: Path) -> None:
    """A PNG named .txt must not be decoded as UTF-8 (it used to raise)."""
    png = _write_image(tmp_path / "real.png", (60, 40)).read_bytes()
    (tmp_path / "disguised.txt").write_bytes(png)
    out = _read(tmp_path, "disguised.txt")

    assert isinstance(out, ToolResult)
    assert out.blocks[1]["type"] == "image_url"


def test_read_file_reports_a_corrupt_image_instead_of_raising(tmp_path: Path) -> None:
    (tmp_path / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage")
    out = _read(tmp_path, "broken.png")

    assert isinstance(out, str)
    assert out.startswith("Error")


def test_registry_boundary_stays_a_str_with_blocks_riding_along(tmp_path: Path) -> None:
    """Callers that put the result straight into a message (sentinel, subagents,
    curator, tracing) must keep receiving something that *is* a str."""
    _write_image(tmp_path / "chart.png", (300, 200))
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tmp_path))

    out = asyncio.run(registry.execute("read_file", {"path": str(tmp_path / "chart.png")}))

    assert isinstance(out, ToolOutput) and isinstance(out, str)
    assert "300x200px" in str(out)
    assert [b["type"] for b in out.blocks] == ["text", "image_url"]


def test_registry_drops_blocks_when_the_tool_reports_an_error() -> None:
    from raven.agent.tools.base import Tool

    class _Failing(Tool):
        @property
        def name(self) -> str:
            return "failing"

        @property
        def description(self) -> str:
            return "always fails"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return ToolResult(
                model_text="Error: nope",
                blocks=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}],
            )

    registry = ToolRegistry()
    registry.register(_Failing())
    out = asyncio.run(registry.execute("failing", {}))

    assert str(out).startswith("Error: nope")
    # An error replaces the result, so the picture it came with is stale.
    assert out.blocks is None


# --------------------------------------------------------------------------
# capability routing
# --------------------------------------------------------------------------


class _FakeLiteLLM:
    pass


def test_supports_image_tool_result_is_fail_safe_for_unverified_targets() -> None:
    from raven.providers.litellm_provider import LiteLLMProvider

    provider = object.__new__(LiteLLMProvider)

    assert supports_image_tool_result(provider, "claude-opus-4-5") is True
    assert supports_image_tool_result(provider, "gpt-4o") is False
    assert supports_image_tool_result(provider, "this-model-does-not-exist") is False


@pytest.mark.parametrize(
    "model,expected",
    [
        # Measured on every host OpenRouter serves them from, each pinned with
        # provider.only + allow_fallbacks=false.
        ("openrouter/anthropic/claude-sonnet-4.5", True),
        ("openrouter/google/gemini-2.5-flash", True),
        # Same creator segment as the row above, but served only by third-party
        # OpenAI-compatible hosts -- DeepInfra and Novita reject the list content
        # with a 422 while Parasail and Nebius accept it, and OpenRouter picks the
        # host per request. A creator-level whitelist would buy intermittent
        # failure here, which is why the key is a family prefix.
        ("openrouter/google/gemma-3-27b-it", False),
        # Refuses loudly.
        ("openrouter/openai/gpt-4o", False),
        # Accepts, drops the image, confabulates an answer. Unrecoverable, hence
        # a whitelist of what was measured rather than a blacklist.
        ("openrouter/openai/gpt-4.1-mini", False),
        ("openrouter/deepseek/deepseek-chat", False),
        # A family prefix must not match mid-string.
        ("openrouter/someone/not-anthropic/claude-clone", False),
        # No route segment to read -> placeholder path.
        ("openrouter/some-bare-model", False),
    ],
)
def test_gateway_capability_follows_the_measured_model_family(model: str, expected: bool) -> None:
    """A gateway hands the request to one of several serving hosts, so the model
    family decides, not the creator segment. Both halves matter: the True rows
    keep the feature working, and the False rows are what stops an unmeasured
    serving stack from silently discarding a picture."""
    from raven.providers.litellm_provider import LiteLLMProvider

    provider = object.__new__(LiteLLMProvider)

    assert supports_image_tool_result(provider, model) is expected


def test_gateway_route_extraction() -> None:
    from raven.providers.capabilities import _gateway_route

    assert _gateway_route("openrouter/anthropic/claude-sonnet-4.5") == "anthropic/claude-sonnet-4.5"
    assert _gateway_route("openrouter/Google/Gemini-2.5-Flash") == "google/gemini-2.5-flash"
    assert _gateway_route("openrouter/bare") == "bare"
    assert _gateway_route("claude-opus-4-5") == ""


def test_supports_image_tool_result_honours_an_explicit_spec_override() -> None:
    from raven.providers.registry import ProviderSpec

    provider = object.__new__(_FakeLiteLLM)
    spec = ProviderSpec(name="x", keywords=(), env_key="", image_tool_result_override=True)
    assert supports_image_tool_result(provider, "gpt-4o", spec) is True

    spec_off = ProviderSpec(name="y", keywords=(), env_key="", image_tool_result_override=False)
    assert supports_image_tool_result(provider, "claude-opus-4-5", spec_off) is False


def test_supports_image_tool_result_is_false_for_non_litellm_transports() -> None:
    assert supports_image_tool_result(_FakeLiteLLM(), "claude-opus-4-5") is False


def test_verified_target_set_is_the_anthropic_family() -> None:
    assert IMAGE_TOOL_RESULT_TARGETS == {"anthropic", "vertex_ai", "bedrock"}


def test_image_placeholder_text_keeps_the_path_and_never_leaks_base64() -> None:
    blocks = [
        {"type": "text", "text": "[image: /tmp/x.png] | 300x200px | ~88 tokens"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}},
    ]
    out = image_placeholder_text(blocks)

    assert "/tmp/x.png" in out
    assert "1 image attached" in out
    assert "AAAA" not in out and len(out) < 300


def test_blind_placeholder_does_not_promise_a_picture_that_never_arrives() -> None:
    """Two reasons for the same substitution, and they must not share wording.

    The transport case attaches the picture to the next message, so the note
    says so. A model with no vision gets nothing afterwards -- telling it to
    expect an attachment leaves it waiting, and the useful thing to say instead
    is which tool can read the file for it.
    """
    blocks = [
        {"type": "text", "text": "[image: /tmp/x.png] | 300x200px | ~88 tokens"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}},
    ]
    out = image_placeholder_text(blocks, blind=True, describe_tool="understand_media")

    assert "/tmp/x.png" in out
    assert "attached to the following message" not in out
    assert "understand_media" in out
    assert "AAAA" not in out and len(out) < 300


def test_the_blind_placeholder_names_no_tool_when_none_is_registered() -> None:
    """The description tool ships with the EverOS plugin and is absent on a
    default install. Naming it anyway is an instruction the model cannot follow,
    so the note then says only that a picture exists and stops."""
    blocks = [
        {"type": "text", "text": "[image: /tmp/x.png] | 300x200px"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    out = image_placeholder_text(blocks, blind=True)

    assert "you cannot see images directly" in out
    assert "tool" not in out
    assert "/tmp/x.png" in out


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def test_strip_inline_images_replaces_base64_and_copies_the_list() -> None:
    live = [
        {"type": "text", "text": "a chart"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    out = _strip_inline_images(live)

    assert out == [{"type": "text", "text": "a chart"}, {"type": "text", "text": "[image]"}]
    # The live message is still being used by the in-flight request.
    assert live[1]["type"] == "image_url"
    assert out is not live


def test_strip_inline_images_leaves_remote_urls_alone() -> None:
    blocks = [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]
    assert _strip_inline_images(blocks) == blocks


@pytest.mark.parametrize("role", ["tool", "user"])
def test_save_turn_never_writes_base64_to_the_session(role: str, tmp_path: Path) -> None:
    """Regression: a list-shaped tool result skipped the char-cap guard entirely,
    so a multi-megabyte data URI landed in the JSONL — unrecoverably."""
    from unittest.mock import MagicMock

    from raven.agent.loop.main import AgentLoop
    from raven.session.manager import Session

    loop = object.__new__(AgentLoop)
    loop._now_fn = MagicMock(return_value=__import__("datetime").datetime(2026, 7, 30))
    session = Session(key="cli:u")

    big = "data:image/png;base64," + "A" * 4_000_000
    messages = [
        {
            "role": role,
            "tool_call_id": "c1",
            "name": "read_file",
            "content": [
                {"type": "text", "text": "[image: /tmp/x.png] | 300x200px"},
                {"type": "image_url", "image_url": {"url": big}},
            ],
        }
    ]

    AgentLoop._save_turn(loop, session, messages, skip=0)

    stored = session.messages[0]["content"]
    assert not any("base64" in str(part) for part in stored)
    assert {"type": "text", "text": "[image]"} in stored
    # The path survives, so the model can ask to see the image again.
    assert any("/tmp/x.png" in str(part) for part in stored)
    # And the in-flight message still holds the picture.
    assert messages[0]["content"][1]["type"] == "image_url"


# --------------------------------------------------------------------------
# emergency shrink
# --------------------------------------------------------------------------


def _img_msg(role: str, marker: str) -> dict:
    return {
        "role": role,
        "content": [
            {"type": "text", "text": marker},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }


def test_emergency_shrink_drops_older_images_and_keeps_the_newest() -> None:
    from raven.agent.loop.main import AgentLoop

    messages = [
        {"role": "system", "content": "sys"},
        _img_msg("tool", "first"),
        _img_msg("tool", "second"),
        _img_msg("tool", "third"),
    ]
    out, elided = AgentLoop._emergency_shrink(messages)

    assert elided >= 2
    assert [p["type"] for p in out[1]["content"]] == ["text", "text"]
    assert out[1]["content"][1]["text"] == "[image elided to fit the context window]"
    # The most recent picture is the one the model is reasoning about.
    assert out[3]["content"][1]["type"] == "image_url"
    # Surrounding text is preserved, so the model still knows what was there.
    assert out[1]["content"][0]["text"] == "first"


def test_emergency_shrink_reaches_images_attached_to_user_messages() -> None:
    """On the fallback path the picture rides in a user message, which the
    tool-only text pass can never touch."""
    from raven.agent.loop.main import AgentLoop

    messages = [
        {"role": "system", "content": "sys"},
        _img_msg("user", "attached-old"),
        _img_msg("user", "attached-new"),
    ]
    out, elided = AgentLoop._emergency_shrink(messages)

    assert elided == 1
    assert out[1]["content"][1]["text"] == "[image elided to fit the context window]"
    assert out[2]["content"][1]["type"] == "image_url"


def test_emergency_shrink_does_not_mutate_the_caller_messages() -> None:
    from raven.agent.loop.main import AgentLoop

    messages = [_img_msg("tool", "a"), _img_msg("tool", "b")]
    AgentLoop._emergency_shrink(messages)

    assert messages[0]["content"][1]["type"] == "image_url"


def test_emergency_shrink_is_a_noop_with_a_single_image() -> None:
    from raven.agent.loop.main import AgentLoop

    messages = [{"role": "system", "content": "sys"}, _img_msg("tool", "only")]
    out, elided = AgentLoop._emergency_shrink(messages)

    assert elided == 0
    assert out[1]["content"][1]["type"] == "image_url"


# --------------------------------------------------------------------------
# the shape lives in one place
# --------------------------------------------------------------------------


def test_image_block_is_the_only_place_the_shape_is_written() -> None:
    """Nothing here type-checks a dict literal, so a mistyped key would silently
    drop the picture. Construction goes through one function instead."""
    import subprocess

    from raven.utils.helpers import image_block

    assert image_block("data:image/png;base64,AA") == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AA"},
    }

    # No module outside helpers.py may hand-write the literal.
    hits = subprocess.run(
        ["grep", "-rn", '"type": "image_url"', "raven/", "--include=*.py"],
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    offenders = [h for h in hits if "raven/utils/helpers.py" not in h]
    assert offenders == [], f"hand-written image block outside helpers.py: {offenders}"


def test_is_inline_image_separates_payloads_from_references() -> None:
    from raven.utils.helpers import is_image_part, is_inline_image

    inline = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}
    remote = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
    text = {"type": "text", "text": "hi"}

    # Both are images; only one carries bytes worth accounting for or stripping.
    assert is_image_part(inline) and is_image_part(remote)
    assert is_inline_image(inline)
    assert not is_inline_image(remote)

    for junk in (text, "not a dict", None, {"type": "image_url"}):
        assert not is_inline_image(junk)


# --------------------------------------------------------------------------
# MCP content conversion
# --------------------------------------------------------------------------


def test_mcp_image_content_becomes_a_block_not_a_pydantic_repr() -> None:
    """Regression: the MCP wrapper ended with str(block), and a pydantic repr
    dumps the whole base64 payload into the prompt as prose."""
    from mcp import types

    from raven.agent.tools.media import blocks_from_mcp_content

    payload = "iVBORw0KGgoAAAANSUhEUg=="
    content = [
        types.TextContent(type="text", text="here is the screenshot"),
        types.ImageContent(type="image", data=payload, mimeType="image/png"),
    ]
    text, blocks = blocks_from_mcp_content(content)

    assert payload not in text
    assert "here is the screenshot" in text
    assert [b["type"] for b in blocks] == ["text", "text", "image_url"]
    assert blocks[2]["image_url"]["url"] == f"data:image/png;base64,{payload}"


def test_mcp_text_only_result_returns_no_blocks() -> None:
    """A text MCP tool must behave exactly as before -- plain string, no blocks."""
    from mcp import types

    from raven.agent.tools.media import blocks_from_mcp_content

    text, blocks = blocks_from_mcp_content([types.TextContent(type="text", text="ok")])

    assert text == "ok"
    assert blocks == []


def test_mcp_audio_content_is_labelled_never_stringified() -> None:
    from mcp import types

    from raven.agent.tools.media import blocks_from_mcp_content

    payload = "QUJDRA=="
    text, blocks = blocks_from_mcp_content([types.AudioContent(type="audio", data=payload, mimeType="audio/wav")])

    assert payload not in text
    assert "unsupported MCP content: audio" in text
    assert "audio/wav" in text


def test_content_part_types_describe_what_raven_produces() -> None:
    """The TypedDicts are documentation-grade: CI runs no type checker, so they
    are asserted at runtime here to keep them from drifting from reality."""
    from raven.utils.helpers import ImagePart, TextPart, image_block, text_block

    img = image_block("data:image/png;base64,AA")
    txt = text_block("hello")

    assert set(img) == set(ImagePart.__annotations__) == {"type", "image_url"}
    assert set(txt) == set(TextPart.__annotations__) == {"type", "text"}
    assert set(img["image_url"]) == {"url"}
    assert img["type"] == "image_url" and txt["type"] == "text"


def test_read_side_helpers_stay_permissive_about_unknown_parts() -> None:
    """Inbound content carries parts the union does not model (Anthropic
    cache_control, MCP audio, provider extensions). Pass-through must not break."""
    from raven.agent.loop.main import _strip_inline_images
    from raven.utils.helpers import estimate_content_part_tokens, is_inline_image

    exotic = {"type": "text", "text": "cached", "cache_control": {"type": "ephemeral"}}
    unknown = {"type": "some_future_part", "payload": {"a": 1}}

    for part in (exotic, unknown):
        assert is_inline_image(part) is False
        assert estimate_content_part_tokens(part) is None

    # And they survive the persistence pass unchanged.
    assert _strip_inline_images([exotic, unknown]) == [exotic, unknown]


# --------------------------------------------------------------------------
# refused-image recovery
# --------------------------------------------------------------------------

# Verbatim body from a live OpenRouter -> OpenAI call on 2026-07-31 that put an
# image in a role="tool" message. Kept exact so the classifier is pinned to real
# wire text, not to a paraphrase of it.
_REFUSAL_400 = (
    "litellm.BadRequestError: OpenrouterException - "
    '{"error":{"message":"Provider returned error","code":400,"metadata":{"raw":'
    '"{\\n  \\"error\\": {\\n    \\"message\\": \\"Invalid \'messages[2]\'. '
    "Image URLs are only allowed for messages with role 'user', but this message "
    'with role \'tool\' contains an image URL.\\",\\n    \\"type\\": '
    '\\"invalid_request_error\\"\\n  }\\n}"}}}'
)


def test_classify_error_flags_a_refused_tool_image() -> None:
    from raven.providers.base import LLMProvider

    verdict = LLMProvider.classify_error(content=_REFUSAL_400)

    assert verdict.should_drop_tool_images is True
    assert verdict.category == "tool_image_unsupported"
    # Not a fallback: another model on the same transport refuses identically,
    # and the fix is ours to apply rather than a different endpoint's to provide.
    assert verdict.should_fallback is False
    assert verdict.retryable is False


def test_classify_error_flags_a_refused_tool_image_from_a_live_exception() -> None:
    """The live path classifies the exception, not swallowed text, so the status
    code and class name carry the 400 rather than a substring."""
    from raven.providers.base import LLMProvider

    class BadRequestError(Exception):
        status_code = 400

    exc = BadRequestError(
        "Invalid 'messages[2]'. Image URLs are only allowed for messages with "
        "role 'user', but this message with role 'tool' contains an image URL."
    )

    assert LLMProvider.classify_error(exc=exc).should_drop_tool_images is True


@pytest.mark.parametrize(
    "body",
    [
        # OpenAI, measured live.
        "Image URLs are only allowed for messages with role 'user'",
        # Xiaomi MiMo.
        '{"code":"400","message":"Param Incorrect","param":"text is not set"}',
        # Generic string-only tool content.
        "tool message content must be a string",
        "tool content must be a string",
        "tool message must be a string",
        # OpenAI-compatible servers failing schema validation.
        "expected string, got list",
        "expected string, got array",
        # Alibaba / DashScope.
        "tool_call.content must be string",
    ],
)
def test_classify_error_flags_every_known_rejection_wording(body: str) -> None:
    """One pattern per vendor wording actually observed in the wild. A provider
    Raven has not met yet fails closed instead -- fatal, not a blind retry."""
    from raven.providers.base import LLMProvider

    verdict = LLMProvider.classify_error(content=f"litellm.BadRequestError: {body}")

    assert verdict.should_drop_tool_images is True, body
    assert verdict.category == "tool_image_unsupported", body


def test_classify_error_keeps_an_unrelated_400_fatal() -> None:
    """The image branch sits above the generic 400 bucket and mirrors its
    condition, so it must narrow that bucket, never widen it: a 400 that is not
    about a tool-role image still classifies as invalid_request."""
    from raven.providers.base import LLMProvider

    for body in (
        "litellm.BadRequestError: invalid_request_error - unknown parameter 'foo'",
        # Mentions an image but not the tool role -- an inbound image problem,
        # which demoting tool images would not fix.
        "invalid_request_error: You uploaded an unsupported image. Formats: png",
        # Mentions the tool role but not an image.
        "invalid_request: messages with role 'tool' must follow a tool_calls message",
    ):
        verdict = LLMProvider.classify_error(content=body)
        assert verdict.should_drop_tool_images is False, body
        assert verdict.category == "invalid_request", body


def test_demote_tool_images_produces_the_placeholder_path_shape() -> None:
    """The retry must land on the already-tested fallback shape, not a third one:
    tool content becomes exactly what image_placeholder_text builds, and the
    picture follows in its own user message."""
    from raven.agent.loop.main import AgentLoop
    from raven.providers.capabilities import image_placeholder_text

    blocks = [
        {"type": "text", "text": "Read image /w/shot.png (800x600 PNG)."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": blocks},
    ]

    out, demoted = AgentLoop._demote_tool_images(messages)

    assert demoted == 1
    assert len(out) == 4
    assert out[2]["role"] == "tool"
    assert out[2]["tool_call_id"] == "c1"
    assert out[2]["content"] == image_placeholder_text(blocks)
    assert "base64" not in out[2]["content"]
    # The path survives, so the model can still re-read the file.
    assert "/w/shot.png" in out[2]["content"]
    assert out[3] == {"role": "user", "content": [blocks[1]], "_attached_image": True}


def test_demote_tool_images_is_a_noop_when_no_tool_result_has_an_image() -> None:
    """Guards the retry: a refusal that was not about a tool image demotes
    nothing, the caller sees 0, and the error stays fatal instead of burning a
    second call on the identical request."""
    from raven.agent.loop.main import AgentLoop

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "c1", "content": "plain text result"},
        # An image already on the fallback path must not be demoted twice.
        _img_msg("user", "already-attached"),
    ]

    out, demoted = AgentLoop._demote_tool_images(messages)

    assert demoted == 0
    assert out == messages


def test_demote_tool_images_does_not_mutate_the_caller_messages() -> None:
    from raven.agent.loop.main import AgentLoop

    messages = [{"role": "tool", "tool_call_id": "c1", "content": _img_msg("tool", "x")["content"]}]
    AgentLoop._demote_tool_images(messages)

    assert len(messages) == 1
    assert messages[0]["content"][1]["type"] == "image_url"


def test_demote_tool_images_attaches_once_after_a_batch_of_tool_results() -> None:
    """A run of tool messages answers one assistant message, so the pictures go
    after the last of them. One in between would leave c2's tool_call unanswered
    at the point the API validates the sequence."""
    from raven.agent.loop.main import AgentLoop

    messages = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}, {"id": "c2"}]},
        {"role": "tool", "tool_call_id": "c1", "content": _img_msg("tool", "one")["content"]},
        {"role": "tool", "tool_call_id": "c2", "content": _img_msg("tool", "two")["content"]},
    ]

    out, demoted = AgentLoop._demote_tool_images(messages)

    assert demoted == 2
    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "user"]
    assert out[1]["tool_call_id"] == "c1"
    assert out[2]["tool_call_id"] == "c2"
    # Both pictures ride in the single attachment.
    assert len(out[3]["content"]) == 2


def test_demote_tool_images_flushes_before_a_non_tool_message() -> None:
    """Two separate batches must not have their pictures merged into one message
    placed after the second: each batch's images belong to its own iteration."""
    from raven.agent.loop.main import AgentLoop

    messages = [
        {"role": "tool", "tool_call_id": "c1", "content": _img_msg("tool", "one")["content"]},
        {"role": "assistant", "content": "thinking", "tool_calls": [{"id": "c2"}]},
        {"role": "tool", "tool_call_id": "c2", "content": _img_msg("tool", "two")["content"]},
    ]

    out, demoted = AgentLoop._demote_tool_images(messages)

    assert demoted == 2
    assert [m["role"] for m in out] == ["tool", "user", "assistant", "tool", "user"]


def test_capability_cache_is_keyed_by_model() -> None:
    """The loop is a long-lived singleton taking a per-call model, so a verdict
    learned for one model must not answer for another."""
    from raven.agent.loop.main import AgentLoop
    from raven.providers.litellm_provider import LiteLLMProvider

    loop = object.__new__(AgentLoop)
    loop.provider = object.__new__(LiteLLMProvider)
    loop.model = "claude-opus-4-5"
    loop._image_tool_result_ok = {}

    assert loop._supports_image_tool_result("claude-opus-4-5") is True
    assert loop._supports_image_tool_result("gpt-4o") is False
    # A refusal caches False for that model only.
    loop._image_tool_result_ok["claude-opus-4-5"] = False
    assert loop._supports_image_tool_result("claude-opus-4-5") is False
    assert loop._supports_image_tool_result("claude-sonnet-4-5") is True


class _RefuseToolImageThenAnswer(LLMProvider):
    """Refuses an image in a tool result once, then answers.

    Mirrors ``_OverflowThenAnswerProvider`` in test_agent_loop_context_overflow:
    the classifier flags the failure, the loop applies a specific repair, and the
    retry succeeds. Here the repair is moving the picture to a user message.
    """

    def __init__(self, image_path: str):
        super().__init__(api_key="test")
        self.image_path = image_path
        self.refused = False
        self.seen: list[list[dict]] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        from raven.providers.base import LLMResponse, ToolCallRequest

        self.seen.append([dict(m) for m in messages])
        if not any(m.get("role") == "tool" for m in messages):
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={"path": self.image_path})],
                finish_reason="tool_calls",
            )
        if not self.refused:
            self.refused = True
            return LLMResponse(
                content=(
                    "litellm.BadRequestError: invalid_request_error - Invalid "
                    "'messages[2]'. Image URLs are only allowed for messages with role "
                    "'user', but this message with role 'tool' contains an image URL."
                ),
                finish_reason="error",
            )
        return LLMResponse(content="it is blue", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_refused_tool_image_is_demoted_and_the_turn_recovers(tmp_path: Path) -> None:
    from raven.agent.loop import AgentLoop
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    img = _write_image(tmp_path / "shot.png", (64, 64))
    provider = _RefuseToolImageThenAnswer(str(img))
    agent = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        max_iterations=8,
        restrict_to_workspace=True,
    )
    # Stand in for a static table that said yes, so the image rides in the tool
    # result and gets refused -- the case the recovery exists for.
    agent._image_tool_result_ok = {"stub": True}

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="what colour is shot.png",
        ),
        session_key="s1",
    )

    assert provider.refused is True
    assert out is not None
    assert out[0] == "it is blue"  # recovered, not surfaced as an error

    # The refused call put the picture in the tool result...
    refused_call = provider.seen[-2]
    tool_msg = next(m for m in refused_call if m.get("role") == "tool")
    assert isinstance(tool_msg["content"], list)
    assert any(p.get("type") == "image_url" for p in tool_msg["content"])

    # ...and the retry moved it to a user message, leaving text behind.
    retry_call = provider.seen[-1]
    tool_msg = next(m for m in retry_call if m.get("role") == "tool")
    assert isinstance(tool_msg["content"], str)
    assert "shot.png" in tool_msg["content"]
    attached = [
        m
        for m in retry_call
        if m.get("role") == "user"
        and isinstance(m.get("content"), list)
        and any(p.get("type") == "image_url" for p in m["content"])
    ]
    assert len(attached) == 1

    # And the verdict is remembered, so the next image skips the wasted call.
    assert agent._image_tool_result_ok["stub"] is False


# --------------------------------------------------------------------------
# R1: an extension that lies about being an image
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,body",
    [
        # SVG is XML with no Pillow decoder, and it is a format agents read and
        # edit -- it must stay readable.
        ("diagram.svg", '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'),
        # A .png that actually holds text: the magic bytes say so, the name lies.
        ("stub.png", "not really a png\nsecond line\n"),
    ],
)
def test_read_file_falls_back_to_text_when_the_extension_lied(tmp_path: Path, name: str, body: str) -> None:
    """Before the image branch existed these read as text. An extension-only
    guess that will not decode must not turn a readable file into an error."""
    (tmp_path / name).write_text(body)
    out = _read(tmp_path, name)

    assert isinstance(out, str)
    assert not out.startswith("Error")
    assert body.splitlines()[0] in out


def test_read_file_still_reports_a_corrupt_file_that_really_sniffed_as_an_image(
    tmp_path: Path,
) -> None:
    """The fallback must not swallow genuine decode failures: PNG magic bytes
    present means it *is* a PNG, so a broken one is an error, not text."""
    (tmp_path / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage")
    out = _read(tmp_path, "broken.png")

    assert isinstance(out, str)
    assert out.startswith("Error decoding image")


def test_read_file_empty_svg_matches_the_pre_image_branch_wording(tmp_path: Path) -> None:
    (tmp_path / "empty.svg").write_text("")
    out = _read(tmp_path, "empty.svg")

    assert str(out).startswith("(Empty file:")


# --------------------------------------------------------------------------
# R2 / R5: batching and persistence of the attachment message
# --------------------------------------------------------------------------


class _TwoToolsThenAnswer(LLMProvider):
    """One assistant message with two tool calls, the *first* returning an image.

    The naive fallback appends the picture right after the first tool result,
    leaving the second tool_call unanswered where Chat Completions validates the
    sequence. This provider reproduces that ordering.
    """

    def __init__(self, image_path: str, text_path: str):
        super().__init__(api_key="test")
        self.image_path = image_path
        self.text_path = text_path
        self.seen: list[list[dict]] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        from raven.providers.base import LLMResponse, ToolCallRequest

        self.seen.append([dict(m) for m in messages])
        if not any(m.get("role") == "tool" for m in messages):
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="c1", name="read_file", arguments={"path": self.image_path}),
                    ToolCallRequest(id="c2", name="read_file", arguments={"path": self.text_path}),
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="saw both", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_attached_images_land_after_every_tool_result_in_the_batch(tmp_path: Path) -> None:
    from raven.agent.loop import AgentLoop
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    img = _write_image(tmp_path / "a.png", (40, 40))
    (tmp_path / "b.txt").write_text("plain\n")
    provider = _TwoToolsThenAnswer(str(img), str(tmp_path / "b.txt"))
    agent = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        max_iterations=6,
        restrict_to_workspace=True,
    )
    # Force the fallback path -- the one this ordering bug lives on.
    agent._image_tool_result_ok = {"stub": False}

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="look at a.png and b.txt",
        ),
        session_key="s1",
    )

    assert out is not None and out[0] == "saw both"

    sent = provider.seen[-1]
    roles = [m["role"] for m in sent]
    # Both tool results must be adjacent; the picture comes after the last one.
    first_tool = roles.index("tool")
    assert roles[first_tool : first_tool + 2] == ["tool", "tool"]
    assert roles[first_tool + 2] == "user"
    assert any(p.get("type") == "image_url" for p in sent[first_tool + 2]["content"])


@pytest.mark.asyncio
async def test_the_attachment_message_is_never_persisted(tmp_path: Path) -> None:
    """R5: it would read as the user having sent '[image]', which they did not.
    The tool result above it already names the path, so nothing is lost."""
    from raven.agent.loop import AgentLoop
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    img = _write_image(tmp_path / "a.png", (40, 40))
    (tmp_path / "b.txt").write_text("plain\n")
    provider = _TwoToolsThenAnswer(str(img), str(tmp_path / "b.txt"))
    agent = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        max_iterations=6,
        restrict_to_workspace=True,
    )
    agent._image_tool_result_ok = {"stub": False}

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="look at a.png",
        ),
        session_key="s1",
    )

    session = agent.sessions.get_or_create("s1")
    persisted = [m for m in session.messages if m.get("role") == "user"]
    # The real user turn survives; the synthetic image carrier does not.
    assert any("look at a.png" in str(m.get("content", "")) for m in persisted)
    assert not any(m.get("_attached_image") for m in session.messages)
    assert not any(
        isinstance(m.get("content"), list) and m["content"] == [{"type": "text", "text": "[image]"}]
        for m in session.messages
    )
    # And the path is still in the transcript, via the tool result.
    assert any("a.png" in str(m.get("content", "")) for m in session.messages)


def test_save_turn_drops_the_attachment_message(tmp_path: Path) -> None:
    """Direct unit cover for the last gate before an irreversible write."""
    from unittest.mock import MagicMock

    from raven.agent.loop.main import AgentLoop
    from raven.session.manager import Session

    loop = object.__new__(AgentLoop)
    loop._now_fn = MagicMock(return_value=__import__("datetime").datetime(2026, 7, 31))
    session = Session(key="cli:u")

    loop._save_turn(
        session,
        [
            {"role": "tool", "tool_call_id": "c1", "content": "[image: /w/a.png] | 40x40px"},
            _img_msg("user", "x") | {"_attached_image": True},
        ],
        0,
    )

    assert [m["role"] for m in session.messages] == ["tool"]


@pytest.mark.asyncio
async def test_the_attachment_message_never_reaches_extraction(tmp_path: Path) -> None:
    """The persistence guard alone is not enough: the returned message list also
    feeds ``context_engine.after_turn`` and ``backend.store``, so the base64 has
    to be filtered upstream of all three, not just before the JSONL write."""
    from raven.agent.loop import AgentLoop
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    img = _write_image(tmp_path / "a.png", (40, 40))
    (tmp_path / "b.txt").write_text("plain\n")
    provider = _TwoToolsThenAnswer(str(img), str(tmp_path / "b.txt"))
    agent = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        max_iterations=6,
        restrict_to_workspace=True,
    )
    agent._image_tool_result_ok = {"stub": False}

    captured: list[dict] = []
    original = agent.context_engine.after_turn

    async def spy(key, payload):
        captured.append(payload)
        return await original(key, payload)

    agent.context_engine.after_turn = spy

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="look at a.png",
        ),
        session_key="s1",
    )

    assert captured, "after_turn was not called"
    seen = captured[-1]["messages"]
    assert not any(m.get("_attached_image") for m in seen)
    assert "base64" not in json.dumps(seen)


# --------------------------------------------------------------------------
# vision capability — can the model see a picture at all
# --------------------------------------------------------------------------


def _catalog(monkeypatch, models: dict[str, list[str] | None]) -> None:
    """Install a catalog table directly, keyed the way the fetch keys it.

    Never the live one: LiteLLM and the gateway catalog are both fetched over
    the network at import/first-use, and the gateway's file changes several
    times a day, so an assertion against it is an assertion about someone
    else's deploy.
    """
    from raven.providers import rates as pricing

    built: dict[str, dict] = {}
    for model_id, mods in models.items():
        entry = {"pricing": {}, "context_length": 1, "input_modalities": mods}
        built[model_id] = entry
        if "/" in model_id:
            built.setdefault(model_id.split("/", 1)[1], entry)
    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE", built)


def test_a_routed_id_matches_the_catalog_across_case(monkeypatch) -> None:
    """The catalog spells every id it publishes in lower case; a routed id need
    not. Case is the only spelling difference normalized away."""
    from raven.providers.capabilities import supports_vision

    _catalog(monkeypatch, {"minimax/minimax-m2": ["text"]})
    assert supports_vision("minimax/MiniMax-M2") is False
    assert supports_vision("minimax-global/MiniMax-M2") is False


def test_a_deployment_name_is_never_matched_by_stripping_punctuation(monkeypatch) -> None:
    """Azure and the local runtimes take a user-chosen deployment or tag name
    where every other provider takes a model id, so a fuzzier join would answer
    for a model the caller never named.

    ``azure/gpt4`` may well serve gpt-4o. A key that dropped the hyphen would
    join it to text-only ``openai/gpt-4`` and lose every picture with no error
    anywhere -- the one failure this module is built to avoid. Punctuation
    therefore stays significant: the fuzzy tier can only ever manufacture a
    denial, since a match that grants vision is what absence already gives.
    """
    from raven.providers.capabilities import supports_vision

    _catalog(monkeypatch, {"openai/gpt-4": ["text"], "microsoft/phi-4": ["text"]})
    assert supports_vision("azure/gpt4") is True
    assert supports_vision("azure/GPT4") is True
    assert supports_vision("ollama/phi4") is True
    # The real vendor spelling still resolves, and still denies.
    assert supports_vision("openai/gpt-4") is False


def test_the_catalog_is_warmed_in_the_background_when_it_has_no_answer(monkeypatch) -> None:
    """The pricing path asks LiteLLM's static table first and only reaches this
    catalog when that misses, so for every model Raven ships a default for it
    would never be fetched at all and this probe would answer optimistically
    forever. Warmed off the request path because the fetch takes a 10s timeout.
    """
    from raven.providers import rates as pricing
    from raven.providers.capabilities import supports_vision

    calls: list[str] = []
    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE", {})
    monkeypatch.setattr(pricing, "_WARM_AT", 0.0)
    monkeypatch.setattr(pricing, "_fetch_openrouter_models", lambda: calls.append("fetch") or {})
    monkeypatch.setattr(pricing.model_catalog_cache, "load", lambda: None)

    assert supports_vision("deepseek/deepseek-v4-pro") is True
    _join_warm()
    assert calls == ["fetch"]

    # A second cold answer inside the cooldown must not start a second fetch.
    assert supports_vision("some-other/model") is True
    _join_warm()
    assert calls == ["fetch"]


def test_a_failed_warm_is_retried_once_the_cooldown_passes(monkeypatch) -> None:
    """A machine whose first turn runs before the VPN is up must not be left
    answering from an empty catalog for the rest of the process -- an attempt
    that failed says nothing about the next one."""
    from raven.providers import rates as pricing
    from raven.providers.capabilities import supports_vision

    calls: list[str] = []

    def _fail() -> dict:
        calls.append("fetch")
        return {}

    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE", {})
    monkeypatch.setattr(pricing, "_WARM_AT", 0.0)
    monkeypatch.setattr(pricing, "_fetch_openrouter_models", _fail)
    monkeypatch.setattr(pricing.model_catalog_cache, "load", lambda: None)

    assert supports_vision("deepseek/deepseek-v4-pro") is True
    _join_warm()
    assert calls == ["fetch"]

    # Still on cooldown.
    assert supports_vision("deepseek/deepseek-v4-pro") is True
    _join_warm()
    assert calls == ["fetch"]

    # Cooldown elapsed -> tried again.
    monkeypatch.setattr(pricing, "_WARM_AT", time.monotonic() - pricing._WARM_RETRY_SECONDS - 1)
    assert supports_vision("deepseek/deepseek-v4-pro") is True
    _join_warm()
    assert calls == ["fetch", "fetch"]


def test_the_warm_resolves_its_fetch_before_the_thread_starts(monkeypatch) -> None:
    """``Thread.start()`` returns before the thread runs its first bytecode. A
    body that looked the fetch up on entry could therefore lose a race with
    whoever patched it -- a restored test seam would send a real request from
    inside the suite and write the real cache file."""
    from raven.providers import rates as pricing

    calls: list[str] = []
    captured: dict[str, object] = {}

    class _CapturedThread:
        """Holds the body at the starting line so the window can be closed by
        hand. Racing a real thread would make the assertion depend on which side
        of the window the scheduler happened to land on."""

        def __init__(self, target=None, name=None, daemon=None) -> None:
            captured["target"] = target

        def start(self) -> None:
            pass

    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE", {})
    monkeypatch.setattr(pricing, "_WARM_AT", 0.0)
    monkeypatch.setattr(pricing, "_fetch_openrouter_models", lambda: calls.append("stub") or {})
    monkeypatch.setattr(threading, "Thread", _CapturedThread)

    pricing.warm_catalog_in_background()
    # Exactly the window: start() has returned, the body has not run.
    monkeypatch.setattr(pricing, "_fetch_openrouter_models", lambda: calls.append("REAL") or {})
    captured["target"]()

    assert calls == ["stub"]


def test_a_warm_that_cannot_reach_the_host_does_not_raise(monkeypatch) -> None:
    """The fetch degrades internally, but a thread that dies loudly writes a
    traceback into a user's terminal for a probe that has already answered."""
    from raven.providers import rates as pricing

    def _boom() -> dict:
        raise RuntimeError("no route to host")

    seen: list[BaseException] = []
    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE", {})
    monkeypatch.setattr(pricing, "_WARM_AT", 0.0)
    monkeypatch.setattr(pricing, "_fetch_openrouter_models", _boom)
    monkeypatch.setattr(threading, "excepthook", lambda args: seen.append(args.exc_value))

    pricing.warm_catalog_in_background()
    _join_warm()

    # Asserted, not merely "did not blow up in the caller": the raise happens on
    # another thread, where pytest downgrades an escape to a warning and a test
    # with no assertion passes either way.
    assert seen == []
    assert pricing._OPENROUTER_CACHE == {}


def test_a_lazy_wrapped_azure_provider_still_reads_as_caller_chosen(monkeypatch) -> None:
    """The TUI hands the loop a lazy proxy, and an isinstance probe against
    the proxy answers about the proxy: a bare Azure deployment name then
    joined the vendor catalog and silently lost its pictures. The probe reads
    through ``unwrapped``."""
    from raven.providers.azure_openai_provider import AzureOpenAIProvider
    from raven.providers.base import GenerationSettings
    from raven.providers.capabilities import vision_verdict
    from raven.providers.lazy import LazyProvider
    from raven.providers.registry import find_by_model

    _catalog(monkeypatch, {"openai/gpt-4": ["text"]})
    lazy = LazyProvider(
        factory=lambda: AzureOpenAIProvider(api_key="k", api_base="https://x.openai.azure.com", default_model="gpt-4"),
        default_model="gpt-4",
        generation=GenerationSettings(),
    )
    # Before the prewarm materializes anything there is no inner class to
    # read, and the probe must answer what the proxy itself answered -- not
    # crash and not guess Azure.
    assert vision_verdict("gpt-4", find_by_model("gpt-4"), lazy) is False

    lazy._built()

    assert vision_verdict("gpt-4", find_by_model("gpt-4"), lazy) is None


def test_a_custom_gateways_served_name_never_joins_the_vendor_catalog(monkeypatch) -> None:
    """A ``custom`` endpoint serves whatever its operator called the model; a
    served ``gpt-4`` is not OpenAI's ``gpt-4``, and the vendor spec the bare
    id resolves to can neither say so nor carry the override escape hatch --
    only the configured provider knows which section built it."""
    from raven.providers.capabilities import vision_verdict
    from raven.providers.litellm_provider import LiteLLMProvider
    from raven.providers.registry import find_by_model

    _catalog(monkeypatch, {"openai/gpt-4": ["text"]})
    provider = LiteLLMProvider(
        api_key="sk-local", api_base="http://gw.example:8000/v1", default_model="gpt-4", provider_name="custom"
    )

    assert vision_verdict("gpt-4", find_by_model("gpt-4"), provider) is None
    # An OpenRouter-built provider keeps consulting the catalog: it serves
    # vendor ids, which is exactly what the catalog speaks for.
    routed = LiteLLMProvider(api_key="sk-or", default_model="gpt-4", provider_name="openrouter")
    assert vision_verdict("openai/gpt-4", find_by_model("openai/gpt-4"), routed) is False


def test_a_stale_disk_table_does_not_suppress_the_warm(monkeypatch) -> None:
    """A long-lived session that boots on a days-old cache file must still
    warm: the reader adopts the disk table at any age (timestamp left at
    zero), and a warm gate that only checked non-emptiness never ran again
    for the life of the process."""
    from raven.providers import rates as pricing

    calls: list[str] = []
    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE", {"old/model": {"input_modalities": ["text"]}})
    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE_TIME", 0.0)
    monkeypatch.setattr(pricing, "_WARM_AT", 0.0)
    monkeypatch.setattr(pricing, "_fetch_openrouter_models", lambda: calls.append("fetch") or {})

    pricing.warm_catalog_in_background()
    _join_warm()

    assert calls == ["fetch"]

    # A fresh in-process table is still the guard: no second thread.
    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE_TIME", time.time())
    pricing.warm_catalog_in_background()
    _join_warm()
    assert calls == ["fetch"]


def test_a_model_the_catalog_calls_text_only_is_blind(monkeypatch) -> None:
    from raven.providers.capabilities import supports_vision

    _catalog(monkeypatch, {"deepseek/deepseek-v4-pro": ["text"]})
    assert supports_vision("deepseek/deepseek-v4-pro") is False
    # The gateway spelling of the same model resolves to the same entry -- the
    # case this whole lookup exists for.
    assert supports_vision("openrouter/deepseek/deepseek-v4-pro") is False


def test_a_provider_prefix_the_catalog_never_uses_still_resolves(monkeypatch) -> None:
    """dashscope/ and gemini/ are routing names; the catalog files the same
    models under qwen/ and google/."""
    from raven.providers.capabilities import supports_vision

    _catalog(monkeypatch, {"qwen/qwen-plus": ["text"], "google/gemini-2.5-flash": ["text", "image"]})
    assert supports_vision("dashscope/qwen-plus") is False
    assert supports_vision("gemini/gemini-2.5-flash") is True


def test_a_model_the_catalog_never_heard_of_keeps_its_pictures(monkeypatch) -> None:
    """Absent is not declared blind. An unlisted model is left exactly where it
    was before this check existed -- being wrong this way fails loudly at the
    endpoint, while being wrong the other way silently turns images into prose
    with nothing to notice."""
    from raven.providers.capabilities import supports_vision

    _catalog(monkeypatch, {"google/gemini-2.5-flash": ["text", "image"]})
    assert supports_vision("no-such-vendor/no-such-model-9000") is True


def test_an_entry_written_before_modalities_were_kept_is_not_a_denial(monkeypatch) -> None:
    """A cache file from an older Raven carries no modality data. Reading that
    silence as "no modalities" would read as "cannot see"."""
    from raven.providers.capabilities import supports_vision

    _catalog(monkeypatch, {"some/model": None})
    assert supports_vision("some/model") is True


def test_a_catalog_that_blows_up_does_not_break_the_probe(monkeypatch) -> None:
    """A capability probe must never be the thing that fails a turn."""
    from raven.providers import capabilities
    from raven.providers import rates as pricing

    def _boom(model):
        raise RuntimeError("catalog on fire")

    monkeypatch.setattr(pricing, "openrouter_input_modalities", _boom)
    assert capabilities.supports_vision("gpt-4o") is True


def test_provider_spec_override_beats_the_catalogue(monkeypatch) -> None:
    """The escape hatch, both directions: a model the catalog gets wrong, and
    one it never lists."""
    from raven.providers.capabilities import supports_vision
    from raven.providers.registry import ProviderSpec

    _catalog(monkeypatch, {"openai/gpt-4o": ["text", "image"], "some/text-model": ["text"]})

    seeing = ProviderSpec(name="selfhost", keywords=("selfhost",), env_key="", vision_override=True)
    assert supports_vision("some/text-model", seeing) is True

    blind = ProviderSpec(name="textonly", keywords=("textonly",), env_key="", vision_override=False)
    assert supports_vision("gpt-4o", blind) is False


# --------------------------------------------------------------------------
# wiring — one-line hand-offs a refactor can drop with every other test green
# --------------------------------------------------------------------------


def test_the_loop_hands_the_verdict_to_the_context_engine(monkeypatch) -> None:
    """`can_see_images` is decided in the loop and consumed three layers down.
    Nothing else asserts the hand-off, so dropping it would be silent."""
    from raven.agent.loop.main import AgentLoop

    loop = object.__new__(AgentLoop)
    loop._vision_ok = {}
    loop.model = "some/model"
    monkeypatch.setattr(AgentLoop, "_supports_vision", lambda self, m=None: False)
    monkeypatch.setattr(AgentLoop, "_describe_tool_name", lambda self: "understand_media")

    seen = {}

    class _Engine:
        owns_compaction = True

        async def assemble(self, session_key, session_messages, budget, *, turn):
            seen["can_see_images"] = turn.can_see_images
            seen["describe_tool"] = turn.describe_tool
            raise _Stop

    class _Stop(Exception):
        pass

    loop.context_engine = _Engine()
    monkeypatch.setattr(AgentLoop, "_context_messages_for_session", lambda self, s: [])
    monkeypatch.setattr(AgentLoop, "_make_token_budget", lambda self, s=None: None)
    loop._last_injected_skill_ids = None

    with pytest.raises(_Stop):
        asyncio.run(
            loop._assemble_context_messages(session=object(), session_key="s", current_message="hi", media=["/x.png"])
        )
    assert seen == {"can_see_images": False, "describe_tool": "understand_media"}


def test_the_verdict_is_asked_of_the_routed_model_not_the_configured_one(monkeypatch) -> None:
    """Assembly is handed the model the request will actually reach.

    The router can send the turn somewhere other than ``self.model``, and the
    tool-result probe already asks about the routed id -- so asking about the
    configured one here would let a blind primary's verdict shape a message a
    vision model receives, or the reverse. Asserted because the argument is the
    whole fix: without it the two halves of one turn disagree and nothing fails.
    """
    from raven.agent.loop.main import AgentLoop

    asked: list[str | None] = []
    loop = object.__new__(AgentLoop)
    loop._vision_ok = {}
    loop.model = "configured/model"
    monkeypatch.setattr(AgentLoop, "_supports_vision", lambda self, m=None: asked.append(m) or True)
    monkeypatch.setattr(AgentLoop, "_describe_tool_name", lambda self: None)

    class _Stop(Exception):
        pass

    class _Engine:
        owns_compaction = True

        async def assemble(self, session_key, session_messages, budget, *, turn):
            raise _Stop

    loop.context_engine = _Engine()
    monkeypatch.setattr(AgentLoop, "_context_messages_for_session", lambda self, s: [])
    monkeypatch.setattr(AgentLoop, "_make_token_budget", lambda self, s=None: None)
    loop._last_injected_skill_ids = None

    with pytest.raises(_Stop):
        asyncio.run(
            loop._assemble_context_messages(
                session=object(),
                session_key="s",
                current_message="hi",
                media=["/x.png"],
                model="routed/vision-model",
            )
        )
    assert asked == ["routed/vision-model"]


def test_the_assembler_forwards_the_verdict_to_the_renderer(monkeypatch) -> None:
    from raven.context_engine.assembler import ContextAssembler
    from raven.context_engine.base import AssemblyContext

    seen = {}

    def _spy(text, media, *, can_see_images=True, describe_tool=None):
        seen.update(can_see_images=can_see_images, describe_tool=describe_tool)
        return text

    from raven.context_engine.segments import render as render_mod

    monkeypatch.setattr(render_mod, "build_user_content", _spy)
    engine = ContextAssembler([], lambda: [])
    engine._build_user(
        AssemblyContext(
            session_key="s",
            current_message="hi",
            media=["/x.png"],
            channel=None,
            chat_id=None,
            session_messages=[],
            budget=None,
            can_see_images=False,
            describe_tool="understand_media",
        )
    )
    assert seen == {"can_see_images": False, "describe_tool": "understand_media"}


def test_an_unregistered_describe_tool_is_not_named(monkeypatch) -> None:
    """Pointing a model at a tool it was never given is an instruction it cannot
    follow. The tool ships with an optional plugin, so absence is the default."""
    from raven.agent.loop.main import AgentLoop

    loop = object.__new__(AgentLoop)

    class _Registry:
        def __init__(self, has):
            self._has = has

        def get(self, name):
            return object() if self._has else None

    loop.tools = _Registry(False)
    assert loop._describe_tool_name() is None
    loop.tools = _Registry(True)
    assert loop._describe_tool_name() == "understand_media"


# --------------------------------------------------------------------------
# attachment preprocessing — the inline path used to skip it entirely
# --------------------------------------------------------------------------


def test_an_attachment_is_downscaled_before_it_is_inlined(tmp_path: Path) -> None:
    """A phone photo is several megabytes and thousands of patch tokens. Inlined
    raw it is refused, or downsized server-side and billed at full size."""
    from raven.context_engine.segments import render

    big = _write_image(tmp_path / "photo.jpg", (4032, 3024), fmt="JPEG")
    out = render.build_user_content("look", [str(big)], can_see_images=True)

    b64 = out[0]["image_url"]["url"].split(",", 1)[1]
    raw_b64 = len(base64.b64encode(big.read_bytes()))
    assert len(b64) < raw_b64 / 4
    note = out[-1]["text"]
    assert "downscaled from 4032x3024" in note
    assert "px" in note


def test_an_attachment_that_cannot_be_prepared_is_named_not_dropped(tmp_path: Path) -> None:
    """The user chose this file. A silent drop leaves them believing the model
    saw something it never received."""
    from raven.context_engine.segments import render

    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage")
    out = render.build_user_content("look", [str(broken)], can_see_images=True)

    assert isinstance(out, str)
    assert "broken.png" in out
    assert "could not be prepared" in out


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 000 does not block root")
def test_an_attachment_that_cannot_be_read_costs_a_note_not_the_turn(tmp_path: Path) -> None:
    """Resolution only proved the path pointed at a file. Permissions can change
    between then and the read, and the file can be gone -- and this renderer runs
    deep inside turn assembly, where an ``OSError`` reaches the caller as a failed
    turn rather than as a message about one attachment.
    """
    from raven.context_engine.segments import render

    locked = _write_image(tmp_path / "locked.png", (60, 40))
    os.chmod(locked, 0o000)
    try:
        out = render.build_user_content("look", [str(locked)], can_see_images=True)
    finally:
        os.chmod(locked, 0o644)

    assert isinstance(out, str)
    assert "locked.png" in out
    assert "could not be read" in out


def test_a_media_list_cannot_inline_an_unbounded_number_of_images(tmp_path: Path) -> None:
    """``prepare_image`` caps each picture; nothing capped the count. A caller is
    free to hand over any number of paths, and each survivor still costs its own
    patch tokens -- so the overflow is named in the text instead of inlined."""
    from raven.context_engine.segments import render

    paths = [str(_write_image(tmp_path / f"p{i}.png", (40, 30))) for i in range(render._MAX_INLINE_IMAGES + 3)]
    out = render.build_user_content("look", paths, can_see_images=True)

    blocks = [b for b in out if b["type"] == "image_url"]
    assert len(blocks) == render._MAX_INLINE_IMAGES
    note = out[-1]["text"]
    assert note.count("not shown, this message is already carrying") == 3
    # read_file, not the description tool: this model can see, so the useful
    # next step is fetching the picture itself.
    assert "read_file" in note and "understand_media" not in note


def test_an_image_too_large_is_refused_instead_of_read_whole(tmp_path: Path, monkeypatch) -> None:
    """A caller may name a file of any size, and the bytes are only needed to
    inline a picture -- so the ceiling is checked from ``stat`` and the file is
    never read past its header."""
    from raven.context_engine.segments import render

    monkeypatch.setattr(render, "_MAX_IMAGE_BYTES", 16)
    fat = _write_image(tmp_path / "fat.png", (200, 200))
    assert fat.stat().st_size > 16

    out = render.build_user_content("look", [str(fat)], can_see_images=True)

    assert isinstance(out, str)
    assert "too large" in out and "fat.png" in out


def test_a_non_image_attachment_is_never_read_past_its_header(tmp_path: Path) -> None:
    """The bytes exist only to sniff the magic number. Reading a 60MB PDF in full
    to look at its first 8 bytes is pure waste, and with a media list of 64 it is
    gigabytes of it per turn."""
    from raven.context_engine.segments import render

    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4" + b"\0" * (4 * 1024 * 1024))

    reads: list[int | None] = []
    real_read = render.Path.open

    class _CountingHandle:
        def __init__(self, inner):
            self._inner = inner

        def read(self, n=None):
            reads.append(n)
            return self._inner.read(n) if n is not None else self._inner.read()

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    def _open(self, *a, **k):
        return _CountingHandle(real_read(self, *a, **k))

    render.Path.open = _open
    try:
        out = render.build_user_content("summarize", [str(doc)])
    finally:
        render.Path.open = real_read

    assert "report.pdf" in out
    # Exactly one bounded read: the header. No unbounded read() followed.
    assert reads == [render._SNIFF_BYTES]


def test_the_inlined_payload_is_bounded_in_bytes_not_only_in_count(tmp_path: Path, monkeypatch) -> None:
    """The count ceiling alone permits 16 images at the per-image byte cap, which
    is a request body every major provider refuses -- so a legitimate batch would
    fail the turn rather than degrade."""
    from raven.context_engine.segments import render

    monkeypatch.setattr(render, "_MAX_INLINE_BASE64_BYTES", 4000)
    paths = [str(_write_image(tmp_path / f"p{i}.png", (300, 300))) for i in range(6)]
    out = render.build_user_content("look", paths, can_see_images=True)

    blocks = [b for b in out if b["type"] == "image_url"]
    assert 0 < len(blocks) < 6
    total = sum(len(b["image_url"]["url"]) for b in blocks)
    # Stops at the first image that crosses the budget, so the overshoot is
    # bounded by one image rather than by the list length.
    assert total < 4000 + len(blocks[-1]["image_url"]["url"])
    assert "not shown, this message is already carrying" in out[-1]["text"]


def test_the_attachment_note_names_no_tool_when_none_is_registered(tmp_path: Path) -> None:
    """Mirror of the tool-result placeholder: ``describe_tool=None`` means the
    default install has no such tool, and the note must not invent one."""
    from raven.context_engine.segments import render

    pic = _write_image(tmp_path / "chart.png", (60, 40))
    with_tool = render.build_user_content("look", [str(pic)], can_see_images=False, describe_tool="understand_media")
    without = render.build_user_content("look", [str(pic)], can_see_images=False, describe_tool=None)

    assert "understand_media" in with_tool
    assert "tool" not in without
    assert "chart.png" in without and "you cannot see images directly" in without


def test_a_blind_model_gets_no_image_in_a_tool_result(tmp_path: Path) -> None:
    """The third branch at the routing point, and the only one with no picture
    anywhere afterwards -- so its wording must not promise one.

    The transport branch says "attached to the following message" because the
    image really does follow. Saying that here would leave the model waiting for
    something that was never sent.
    """
    _write_image(tmp_path / "chart.png", (300, 200))
    result = _read(tmp_path, "chart.png")

    blind = image_placeholder_text(result.blocks, blind=True, describe_tool="understand_media")
    transport = image_placeholder_text(result.blocks)

    assert "attached to the following message" in transport
    assert "attached to the following message" not in blind
    assert "understand_media" in blind
    # Both keep the tool's own text, which is the only thing naming the file.
    assert str(tmp_path / "chart.png") in blind
    assert "base64" not in blind and "iVBOR" not in blind


# --------------------------------------------------------------------------
# round-3: the branches and constraints a mutation test found unguarded
# --------------------------------------------------------------------------


def _loop_for_routing(monkeypatch, *, sees: bool, tool_result_ok: bool, describe: str | None):
    from raven.agent.loop.main import AgentLoop

    loop = object.__new__(AgentLoop)
    monkeypatch.setattr(AgentLoop, "_supports_vision", lambda self, m=None: sees)
    monkeypatch.setattr(AgentLoop, "_supports_image_tool_result", lambda self, m=None: tool_result_ok)
    monkeypatch.setattr(AgentLoop, "_describe_tool_name", lambda self: describe)
    return loop


_ROUTING_BLOCKS = [
    {"type": "text", "text": "[image: /w/shot.png] | 300x200px"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 500}},
]


def test_a_blind_model_is_told_the_picture_is_not_coming(monkeypatch) -> None:
    """The one branch with no picture anywhere afterwards. Saying "attached to
    the following message" here leaves the model waiting for something that is
    never sent, and nothing downstream can notice."""
    loop = _loop_for_routing(monkeypatch, sees=False, tool_result_ok=True, describe="understand_media")
    text, blocks, attach = loop._route_result_images("orig", list(_ROUTING_BLOCKS), "some/blind-model")

    assert blocks is None and attach is None
    assert "attached to the following message" not in text
    assert "you cannot see images directly" in text
    assert "understand_media" in text
    assert "AAAA" not in text


def test_a_blind_model_with_no_description_tool_is_promised_nothing(monkeypatch) -> None:
    loop = _loop_for_routing(monkeypatch, sees=False, tool_result_ok=True, describe=None)
    text, _, _ = loop._route_result_images("orig", list(_ROUTING_BLOCKS), "some/blind-model")

    assert "you cannot see images directly" in text
    assert "tool" not in text


def test_a_transport_that_carries_images_keeps_them_in_the_tool_result(monkeypatch) -> None:
    loop = _loop_for_routing(monkeypatch, sees=True, tool_result_ok=True, describe=None)
    text, blocks, attach = loop._route_result_images("orig", list(_ROUTING_BLOCKS), "anthropic/claude")

    assert text == "orig"
    assert blocks == _ROUTING_BLOCKS and attach is None


def test_a_transport_that_cannot_carry_images_attaches_them_after(monkeypatch) -> None:
    loop = _loop_for_routing(monkeypatch, sees=True, tool_result_ok=False, describe=None)
    text, blocks, attach = loop._route_result_images("orig", list(_ROUTING_BLOCKS), "openai/gpt-4o")

    assert blocks is None
    assert attach == [_ROUTING_BLOCKS[1]]
    assert "attached to the following message" in text
    assert "AAAA" not in text


def test_a_text_result_is_passed_through_untouched(monkeypatch) -> None:
    loop = _loop_for_routing(monkeypatch, sees=False, tool_result_ok=False, describe=None)
    assert loop._route_result_images("plain", None, "m") == ("plain", None, None)


def test_the_fetch_files_no_normalized_join_key_in_the_shared_table(monkeypatch) -> None:
    """Regression on the write side, which is where the defect lived.

    v2 filed a punctuation-stripped key beside each id. That key is reachable by
    an exact lookup, so ``ollama/phi4`` (bare alias ``phi4``) joined
    ``microsoft/phi-4`` and inherited its prices, its context window and its
    text-only verdict. Asserted through the real fetch: a hand-built table cannot
    see this, which is exactly why the mutation went unnoticed.
    """
    from raven.providers import model_catalog_cache
    from raven.providers import rates as pricing
    from raven.providers.capabilities import supports_vision

    payload = {
        "data": [
            {
                "id": "openai/gpt-4",
                "pricing": {"prompt": "0.00003", "completion": "0.00006"},
                "context_length": 8192,
                "architecture": {"input_modalities": ["text"]},
            },
            {
                "id": "microsoft/phi-4",
                "pricing": {"prompt": "0.00000007", "completion": "0.00000014"},
                "context_length": 16384,
                "architecture": {"input_modalities": ["text"]},
            },
        ]
    }

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return payload

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(pricing.httpx, "Client", _Client)
    monkeypatch.setattr(model_catalog_cache, "save", lambda models: None)
    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE", {})
    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE_TIME", 0.0)

    table = _REAL_FETCH()

    assert set(table) == {"openai/gpt-4", "gpt-4", "microsoft/phi-4", "phi-4"}
    assert "gpt4" not in table and "phi4" not in table

    # And the consequence the key set exists to prevent.
    assert supports_vision("ollama/phi4") is True
    assert supports_vision("azure/gpt4") is True
    assert pricing.resolve_context_window("ollama/phi4") is None


def test_a_deployment_name_is_never_answered_from_the_vendor_catalog(monkeypatch) -> None:
    """Azure takes the name of a deployment the user created and a local runtime
    takes whatever tag they pulled, either of which can be spelled exactly like a
    vendor id it does not serve -- ``gpt-4`` is the name Azure's own quickstarts
    use, and a team keeps the name while repointing the deployment at gpt-4o.

    Only a denial does damage here (a grant is what absence already gives), and a
    denial is the silent failure this module exists to avoid, so these providers
    do not consult the catalog at all.
    """
    from raven.providers.azure_openai_provider import AzureOpenAIProvider
    from raven.providers.capabilities import supports_vision, vision_verdict
    from raven.providers.registry import find_by_model

    _catalog(monkeypatch, {"openai/gpt-4": ["text"], "qwen/qwen-plus": ["text"]})
    azure = object.__new__(AzureOpenAIProvider)

    # Azure takes a bare deployment name, so no prefix resolves it to a spec --
    # the live provider is the only thing that knows. The alias matches the
    # catalog verbatim, so this is not about punctuation either.
    assert find_by_model("gpt-4") is not None, "resolves to OpenAI's spec, which is the trap"
    assert vision_verdict("gpt-4", find_by_model("gpt-4"), azure) is None
    assert supports_vision("gpt-4", find_by_model("gpt-4"), azure) is True

    # Routed through LiteLLM instead, Azure carries a prefix the registry does
    # not answer to, so neither the spec nor the provider identifies it.
    assert find_by_model("azure/gpt-4") is None
    assert supports_vision("azure/gpt-4", find_by_model("azure/gpt-4")) is True
    assert supports_vision("azure_ai/gpt-4", find_by_model("azure_ai/gpt-4")) is True

    # A local runtime does carry a resolvable prefix.
    assert supports_vision("ollama/qwen-plus", find_by_model("ollama/qwen-plus")) is True

    # The same id routed to the vendor itself is still answered, and denied.
    assert supports_vision("gpt-4", find_by_model("gpt-4")) is False


def test_a_cold_verdict_is_not_cached_for_the_life_of_the_loop(monkeypatch) -> None:
    """``AgentLoop`` is built once per process. Caching the optimistic answer the
    catalog gives before it is warm would freeze that guess forever and leave the
    background warm filling a table nothing re-reads -- so only a real verdict is
    remembered."""
    from raven.agent.loop.main import AgentLoop
    from raven.providers import rates as pricing

    loop = object.__new__(AgentLoop)
    loop._vision_ok = {}
    loop.model = "deepseek/deepseek-v4-pro"
    loop.provider = None

    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE", {})
    monkeypatch.setattr(pricing, "_WARM_AT", 0.0)
    monkeypatch.setattr(pricing, "_fetch_openrouter_models", lambda: {})
    monkeypatch.setattr(pricing.model_catalog_cache, "load", lambda: None)

    assert loop._supports_vision() is True
    assert loop._vision_ok == {}, "a cold guess must not be remembered"
    _join_warm()

    _catalog(monkeypatch, {"deepseek/deepseek-v4-pro": ["text"]})
    assert loop._supports_vision() is False
    assert loop._vision_ok == {"deepseek/deepseek-v4-pro": False}


def test_turn_send_refuses_an_unbounded_attachment_list() -> None:
    """Nothing downstream counts the list, and each survivor costs its own patch
    tokens, so the schema is where an absurd one is refused."""
    import pydantic

    from raven.tui_rpc.models import TurnSendParams

    ok = TurnSendParams(session_key="cli:local", content="hi", media=["a.png"] * 64)
    assert len(ok.media) == 64
    with pytest.raises(pydantic.ValidationError, match="at most 64"):
        TurnSendParams(session_key="cli:local", content="hi", media=["a.png"] * 65)


def test_a_blind_model_never_pays_to_read_the_picture(tmp_path: Path) -> None:
    """The bytes exist only to inline a picture. A model that cannot see one
    gets a note built from the path alone, so loading the file -- up to the
    64MB ceiling, per attachment -- buys nothing and must not happen."""
    from raven.context_engine.segments import render

    pic = _write_image(tmp_path / "chart.png", (400, 300))
    reads: list[int | None] = []
    real_open = render.Path.open

    class _CountingHandle:
        def __init__(self, inner):
            self._inner = inner

        def read(self, n=None):
            reads.append(n)
            return self._inner.read(n) if n is not None else self._inner.read()

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    render.Path.open = lambda self, *a, **k: _CountingHandle(real_open(self, *a, **k))
    try:
        blind = render.build_user_content("look", [str(pic)], can_see_images=False)
        blind_reads = list(reads)
        reads.clear()
        render.build_user_content("look", [str(pic)], can_see_images=True)
        sighted_reads = list(reads)
    finally:
        render.Path.open = real_open

    assert "you cannot see images directly" in blind
    # Header only for the blind model; the sighted one goes on to read the rest.
    assert blind_reads == [render._SNIFF_BYTES]
    assert sighted_reads == [render._SNIFF_BYTES, None]


def test_the_oversize_note_points_at_the_tool_that_could_still_help(tmp_path: Path, monkeypatch) -> None:
    """Only a model that can see images reaches this branch, and read_file
    downscales rather than refusing on size -- so it, not the description tool,
    is the useful next step."""
    from raven.context_engine.segments import render

    monkeypatch.setattr(render, "_MAX_IMAGE_BYTES", 16)
    fat = _write_image(tmp_path / "fat.png", (200, 200))
    out = render.build_user_content("look", [str(fat)], can_see_images=True, describe_tool="understand_media")

    assert "too large" in out
    assert "read_file" in out
    assert "understand_media" not in out
