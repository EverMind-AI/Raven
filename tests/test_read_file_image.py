"""read_file's image branch: preprocessing, capability routing, persistence.

Covers the invariant that makes the feature safe to ship — base64 reaches the
model for exactly one turn and never reaches disk.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from raven.agent.loop.main import _strip_inline_images
from raven.agent.tools import media
from raven.agent.tools.base import ToolOutput, ToolResult
from raven.agent.tools.filesystem import ReadFileTool
from raven.agent.tools.registry import ToolRegistry
from raven.providers.base import LLMProvider
from raven.providers.capabilities import (
    IMAGE_TOOL_RESULT_TARGETS,
    image_placeholder_text,
    supports_image_tool_result,
)


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


def test_gateway_capability_follows_the_backend_not_the_gateway() -> None:
    """OpenRouter routes to a backend named in the model string, so the second
    segment decides. Measured live: anthropic and google carry the image (the
    model named the test image's colour); openai does not -- gpt-4o refuses with
    a 400 and gpt-4.1-mini silently drops it and confabulates a colour, which is
    why the set is a whitelist of measured backends rather than a blacklist."""
    from raven.providers.litellm_provider import LiteLLMProvider

    provider = object.__new__(LiteLLMProvider)

    assert supports_image_tool_result(provider, "openrouter/anthropic/claude-sonnet-4.5") is True
    assert supports_image_tool_result(provider, "openrouter/google/gemini-2.5-flash") is True
    assert supports_image_tool_result(provider, "openrouter/openai/gpt-4o") is False
    assert supports_image_tool_result(provider, "openrouter/deepseek/deepseek-chat") is False
    # No backend segment to read -> placeholder path.
    assert supports_image_tool_result(provider, "openrouter/some-bare-model") is False


def test_gateway_backend_extraction() -> None:
    from raven.providers.capabilities import _gateway_backend

    assert _gateway_backend("openrouter/anthropic/claude-sonnet-4.5") == "anthropic"
    assert _gateway_backend("openrouter/Google/gemini-2.5-flash") == "google"
    assert _gateway_backend("openrouter/bare") == ""
    assert _gateway_backend("claude-opus-4-5") == ""


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
