"""read_file's image branch: preprocessing, capability routing, persistence.

Covers the invariant that makes the feature safe to ship — base64 reaches the
model for exactly one turn and never reaches disk.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from raven.agent.loop.main import _strip_inline_images
from raven.agent.tools import media
from raven.agent.tools.base import ToolOutput, ToolResult
from raven.agent.tools.filesystem import ReadFileTool
from raven.agent.tools.registry import ToolRegistry
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
    # A proxy in front of Claude still takes the fallback: its tool-result
    # translation is unverified, and a wrong guess loses the image silently.
    assert supports_image_tool_result(provider, "openrouter/anthropic/claude-opus-4-5") is False
    assert supports_image_tool_result(provider, "this-model-does-not-exist") is False


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
