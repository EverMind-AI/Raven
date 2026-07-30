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
